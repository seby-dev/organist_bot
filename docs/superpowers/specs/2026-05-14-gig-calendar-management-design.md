# Gig Calendar Management — Design Spec

**Date:** 2026-05-14
**Status:** Approved

## Goal

Four related features that close the loop between the Google Calendar, the Telegram bot, and the unavailability filter:

1. **`/gigs`** — view upcoming gigs from Google Calendar as a numbered list
2. **`/deletegig <n>`** — delete a gig by number and remove its date from unavailable periods
3. **Auto-add to unavailable** — when `/addgig` confirms a gig, its date is silently added to unavailable periods
4. **Auto-purge past periods** — stale entries are removed from unavailable periods on every read/write

## Scope

Changes touch four files only:

| File | Change |
|---|---|
| `organist_bot/integrations/calendar_client.py` | Add `list_upcoming_events` and `delete_event` methods |
| `organist_bot/filter_store.py` | Add `purge_past_periods`; call it on every unavailable read/write |
| `organist_bot/integrations/telegram_bot.py` | Add `/gigs` and `/deletegig` commands; module-level listing cache |
| `organist_bot/integrations/gig_agent.py` | After confirmed calendar write, add date to unavailable |

No new modules. No changes to `filters.py`, `main.py`, config, or other integrations.

---

## Section 1: `GoogleCalendarClient` new methods

### `list_upcoming_events(max_results: int = 10) -> list[dict]`

Fetches calendar events starting from now (UTC), ordered by start time ascending.

Each returned dict contains:
- `id` — Google Calendar event ID (string)
- `summary` — event title (string)
- `start_dt` — event start as `datetime.datetime` (timezone-aware)
- `date_str` — ISO date `YYYY-MM-DD` of the event start (for use as a period token)

Fails open: returns `[]` if the API call raises, logging a warning. This is consistent with `has_event_on_date`.

### `delete_event(event_id: str) -> None`

Calls `events().delete(calendarId=self.calendar_id, eventId=event_id).execute()`.

**Raises** on failure — unlike `has_event_on_date`, a silent failure on delete would be dangerous (user believes the gig is gone but it isn't). The caller (`cmd_deletegig`) catches and reports the error.

---

## Section 2: `filter_store.py` — `purge_past_periods`

### New function: `purge_past_periods() -> int`

Rewrites `unavailable_periods` in place, removing tokens whose end date is strictly before `datetime.date.today()`. Returns the count of removed tokens (for logging).

Period end-date rules (consistent with `_parse_periods` in `filters.py`):
- Single day `2026-12-25` → end date is `2026-12-25`
- Range `2026-12-15:2027-01-05` → end date is `2027-01-05`
- Month `2026-12` → end date is last day of that month
- Unparseable tokens → left untouched (fail-safe)

### When it runs

`purge_past_periods()` is a standalone function that does a full `_read()` → filter → `_write()` cycle itself. It is called explicitly by:
- `unavailable_periods()` — called first, before the inner `_read()` that returns the value. This means the getter has a write side-effect, but that side-effect is fully encapsulated inside `purge_past_periods()` — `unavailable_periods()` itself never calls `_write()` directly.
- `add_period(key, period)` — when `key == "unavailable_periods"`, called before the add logic
- `remove_period(key, period)` — when `key == "unavailable_periods"`, called before the remove logic

This ensures the list is always clean before any unavailable operation, including the new auto-add from gig confirmation and the `/unavailable` Telegram commands.

**Concurrency note:** `filter_store.py` has no file locking — all operations are read-modify-write with no atomicity. This pre-existing risk is unchanged. It is acceptable because the bot is gated to a single `TELEGRAM_CHAT_ID` and concurrent writes are not possible in normal single-user operation.

---

## Section 3: New Telegram commands

### Module-level listing cache

```python
_gig_listing: dict[int, list[dict]] = {}
```

Keyed by `chat_id`. Each value is the list of event dicts returned by `list_upcoming_events` from the most recent `/gigs` call for that chat. Follows the same pattern as `_histories` in `gig_agent.py`.

The cache has no TTL. The `/gigs` reply includes a "fetched at HH:MM" note so the user knows when it was last refreshed and can re-run `/gigs` for an up-to-date list before deleting.

### `/gigs` — `cmd_gigs`

1. Check calendar is configured (`settings.google_calendar_id` and credentials); if not, reply "Google Calendar not configured."
2. Call `calendar_client.list_upcoming_events(max_results=10)`
3. Store result in `_gig_listing[chat_id]`
4. If empty, reply "No upcoming gigs found."
5. Otherwise, reply with a numbered Markdown list:

```
*Upcoming gigs*
1\. Sunday Service — St Mary's · Sun 1 Jun 2025 · 10:30am
2\. Evensong — Christ Church · Sat 14 Jun 2025 · 6:00pm
…
Use /deletegig \<number\> to remove one\.
```

### `/deletegig <n>` — `cmd_deletegig`

1. Parse `n` from `context.args`; if missing or non-integer, reply with usage hint.
2. Look up `_gig_listing.get(chat_id)`; if empty or `n` out of range:
   - If cache empty: reply "Run /gigs first to see your upcoming gigs."
   - If out of range: reply "No gig number *n*. Run /gigs to see the list."
3. Get the event dict at index `n - 1`.
4. Build a `GoogleCalendarClient` using `_make_calendar_client()` (same helper as `gig_agent.py`); if it returns `None`, reply "Google Calendar not configured." and return.
5. Call `calendar_client.delete_event(event["id"])`; on failure, catch and reply with the error message.
6. Call `filter_store.remove_period("unavailable_periods", event["date_str"])` — silently (the date may not be in unavailable; that's fine).
7. Remove the deleted entry from `_gig_listing[chat_id]` and re-number in memory (so subsequent `/deletegig` calls without re-running `/gigs` work correctly).
8. Reply: "✓ Deleted *[summary]*. Date removed from unavailable if it was there."

### Registration

Both commands are registered with `CommandHandler` in `run()`, after the existing filter commands. Both require `_is_authorised`.

### `/start` help text update

Add to `_HELP`:
```
*Gig calendar*
  /gigs           — View upcoming gigs
  /deletegig <n>  — Delete gig by number
  /addgig \<url\> — Add a gig by URL
  /addgig         — Add a gig via conversation
  /cancel         — Cancel gig entry
```

---

## Section 4: Auto-add to unavailable on gig confirmation

In `organist_bot/integrations/gig_agent.py`, inside `_execute_tool`, after the `confirmed=true` path successfully calls `cal.add_gig(gig)`:

1. Add `from organist_bot.filters import normalize_to_yyyymmdd` to the imports in `gig_agent.py` (it is **not** currently imported there)
2. Add `from organist_bot import filter_store` to the imports
3. Parse `fields["date"]` with `normalize_to_yyyymmdd` → `yyyymmdd_str` (returns `"YYYYMMDD"` format or `None`)
4. If not None, convert to `YYYY-MM-DD` with `datetime.datetime.strptime(yyyymmdd_str, "%Y%m%d").strftime("%Y-%m-%d")` and call `filter_store.add_period("unavailable_periods", date_str)`. The `YYYY-MM-DD` format is required — `filter_store` period tokens use ISO dates that `_parse_periods` in `filters.py` can parse; passing `YYYYMMDD` format would silently fail the parser.
5. If parsing fails, log a warning — the calendar write still succeeds; unavailable sync is best-effort

The `add_period` call triggers `purge_past_periods()` automatically (Section 2), so old entries are cleaned up at the same time.

The confirmation message returned to the user is unchanged.

---

## Data flow

```
/gigs
  → list_upcoming_events() → _gig_listing[chat_id] = [...]
  → formatted numbered list sent to user

/deletegig 2
  → _gig_listing[chat_id][1] → event dict
  → delete_event(event["id"])
  → remove_period("unavailable_periods", event["date_str"])
  → _gig_listing[chat_id] updated in memory
  → confirmation sent to user

/addgig → gig_agent → add_gig(confirmed=true)
  → cal.add_gig(gig) → event_id
  → normalize_to_yyyymmdd(fields["date"]) → date_str
  → add_period("unavailable_periods", date_str)
    → purge_past_periods() runs first
  → success response to user

/unavailable list  (or any unavailable operation)
  → purge_past_periods() runs first
  → returns clean list
```

---

## Out of scope

- Editing/updating existing calendar events
- Showing gig details beyond summary + date + time
- Syncing `available_only_periods` (not relevant to gig booking)
- Persisting `_gig_listing` across bot restarts
