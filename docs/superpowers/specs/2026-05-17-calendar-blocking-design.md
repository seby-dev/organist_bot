# Calendar Blocking for Unavailable Periods

**Date:** 2026-05-17

## Problem

When a user marks themselves unavailable (via the Telegram agent), the period is saved to `data/filter_config.json` and the `AvailabilityFilter` rejects gig applications for those dates. However, Google Calendar shows no indication of the unavailability — blocking events are not created. This means the calendar is not useful as a human-visible schedule of commitments and unavailable time.

## Goal

- Unavailable periods should automatically appear as blocking events on Google Calendar.
- Removing an unavailable period should delete the corresponding blocking event.
- Existing unavailable periods should be synced to the calendar at bot startup.

## Approach

Approach B: hook at the agent level in `unified_agent.py`, with new methods on `GoogleCalendarClient`.

---

## Design

### 1. `GoogleCalendarClient` — new methods (`calendar_client.py`)

**`block_period(period: str) -> str | None`**

- Parses the period token into `(start_date, end_date)`:
  - `2026-12-25` → Dec 25 only (all-day end is exclusive: Dec 26)
  - `2026-12-01:2026-12-31` → Dec 1 to Dec 31
  - `2026-12` → Dec 1 to Dec 31
- Creates an all-day Google Calendar event tagged with `extendedProperties.private["organist_bot_block"] = "1"`.
- Event summary: `"Unavailable"`.
- Returns the created event ID on success, `None` on failure (fire-and-forget — calendar failure never blocks the filter store write).

**`unblock_period(period: str) -> bool`**

- Parses the period token into `(start_date, end_date)`.
- Lists events in that date range filtered by `privateExtendedProperty=organist_bot_block=1` (supported natively by the Google Calendar API `events.list` endpoint).
- Deletes all matching blocking events.
- Returns `True` if any events were deleted.

**`_parse_period_dates(period: str) -> tuple[datetime.date, datetime.date] | None`**

Shared helper used by both `block_period` and `unblock_period`. Returns `None` on parse failure.

---

### 2. Agent handler changes (`unified_agent.py`)

In the `manage_unavailable` handler (around line 739), after the `filter_store` call succeeds:

- `action == "add"`:
  ```python
  cal = _make_calendar_client()
  if cal:
      try:
          cal.block_period(period)
      except Exception:
          logger.warning("Failed to create calendar block for %r", period)
  ```
- `action == "remove"`: same pattern, calling `cal.unblock_period(period)`.

Calendar failures are logged but never surfaced to the user or allowed to prevent the filter store update.

---

### 3. One-time startup sync (`unified_agent.py` + `telegram_bot.py`)

**`sync_calendar_blocks(cal: GoogleCalendarClient) -> int`** in `unified_agent.py`:

1. Reads all current unavailable periods from `filter_store.unavailable_periods()`.
2. For each period, queries the calendar for existing blocks via `extendedProperties` (same query used by `unblock_period`). Skips the period if a block already exists.
3. Calls `cal.block_period(period)` for any period with no existing block.
4. Returns the count of blocks created, logged at INFO level.

Called once from `telegram_bot.py` during async bot startup (before polling begins). Idempotent — safe to run even if blocks already exist.

---

## Error Handling

All calendar operations are fire-and-forget:
- Failures are logged at WARNING level with the period and error.
- The filter store write always completes regardless of calendar API result.
- `sync_calendar_blocks` continues to the next period if one fails.

---

## Files Changed

| File | Change |
|------|--------|
| `organist_bot/integrations/calendar_client.py` | Add `block_period`, `unblock_period`, `_parse_period_dates` |
| `organist_bot/integrations/unified_agent.py` | Call `block_period`/`unblock_period` in `manage_unavailable`; add `sync_calendar_blocks` |
| `telegram_bot.py` | Call `sync_calendar_blocks` at startup |
| `tests/test_calendar_client.py` | Tests for new methods |

---

## Out of Scope

- Blocking `available_only_periods` on the calendar (different semantics — these are ranges you are available, not blocked).
- Syncing calendar block deletions back to the filter store (one-way: filter store is authoritative).
- UI for calendar blocking outside the Telegram agent.
