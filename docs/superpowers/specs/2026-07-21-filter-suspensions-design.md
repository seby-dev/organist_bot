# Filter Suspensions by Date Range

**Date:** 2026-07-21

## Problem

The gig filter pipeline (`GigFilterChain` in `filters.py`) applies a fixed set of filters (fee, Sunday-time, blacklist, postcode, calendar, availability) to every scraped gig, toggled only via static `.env` flags (`ENABLE_*`). There is no way to temporarily exempt gigs in a specific date range from one or more filters — e.g. "ignore the postcode filter for gigs in December" or "ignore the fee filter from August 1st onward" — without editing `.env` and restarting the bot. The user wants to manage this from the Telegram bot using free-form text, the same way `unavailable_periods`/`available_only_periods` are already managed.

## Goal

- Suspend one named filter, or all filters, for a date range that keys off the **gig's own date** (consistent with how `AvailabilityFilter` already interprets periods) — not the wall-clock time the suspension was created.
- Support closed ranges (`2026-12-20:2026-12-31`), single dates, whole months (`2026-12`) — reusing the existing period-token format — **plus new open-ended ranges**: "from date X onward" and "up to and including date Y."
- Manage suspensions via free-form Telegram text (list / add / remove), mirroring `manage_unavailable`/`manage_available`.
- `seen` is explicitly **not** suspendable — suspending it wouldn't exempt a category of gig, it would just re-trigger a duplicate application email to the same organiser on every poll tick. This is a deliberate exclusion, not an oversight.

## Approach

New store + a wrapper class composed around existing filter instances, following the same "read fresh, mutate via JSON file" pattern as `filter_store.py` and the same "load once per tick, not per gig" performance pattern already used for `BlacklistFilter`/`AvailabilityFilter`.

---

## Design

### 1. `organist_bot/filter_suspension_store.py` (new file)

Mirrors `filter_store.py`. Backed by `data/filter_suspensions.json`:

```json
{"suspensions": [{"filter": "postcode", "period": "2026-12-20:2026-12-31"}]}
```

**`FILTER_KEYS`** = `("fee", "sunday_time", "blacklist", "postcode", "calendar", "availability", "all")` — deliberately excludes `"seen"`.

**Period token parsing** (local to this module, not shared with `filters._parse_periods` — it needs open-ended support that the existing parser doesn't have):
- `"2026-12-25"` → single day
- `"2026-12-15:2027-01-05"` → inclusive range
- `"2026-12"` → whole month
- `"2026-08-01:"` → from that date onward, no end (parsed as `end = date.max`)
- `":2026-08-01"` → up to and including that date, no start (parsed as `start = date.min`)

Invalid tokens are skipped with a `logger.warning`, matching existing convention.

**Functions:**
- `list_suspensions() -> list[dict]` — raw `{"filter": ..., "period": ...}` dicts, fresh read.
- `add_suspension(filter_name: str, period: str) -> bool` — adds if not an exact `(filter, period)` duplicate; validates `filter_name` is in `FILTER_KEYS` and the period token parses, raising `ValueError` otherwise (caught by the Telegram handler and surfaced as a user-facing error message).
- `remove_suspension(filter_name: str, period: str) -> bool` — exact match on both fields, same convention as `filter_store.remove_period`.
- `purge_past_suspensions() -> int` — removes entries whose parsed **end** date is in the past. Open-ended "from X" entries (`end == date.max`) are never purged — they're meant to be indefinite until manually removed.
- `load_active() -> list[tuple[str, date, date]]` — parses all current entries once into `(filter_name, start, end)` tuples, for a single per-tick snapshot (see §2). This is the function `main.py` calls once per tick, not `list_suspensions()`.
- `is_suspended(snapshot: list[tuple], filter_name: str, gig_date: date) -> bool` — pure function, no I/O: `True` if any snapshot entry matches `filter_name` (or `"all"`) with `start <= gig_date <= end`.

### 2. `SuspendableFilter` wrapper (`filters.py`)

```python
class SuspendableFilter:
    """Wraps a filter; passes the gig through unconditionally if a suspension
    snapshot entry covers the filter's name (or "all") for the gig's date."""

    def __init__(self, filter_name: str, inner: Callable[[Gig], bool], snapshot: list[tuple]):
        self.filter_name = filter_name
        self.inner = inner
        self._snapshot = snapshot

    def __call__(self, gig: Gig) -> bool:
        normalized = normalize_to_yyyymmdd(gig.date)
        if normalized is not None:
            d = datetime.datetime.strptime(normalized, "%Y%m%d").date()
            if filter_suspension_store.is_suspended(self._snapshot, self.filter_name, d):
                return True  # suspended — pass through
        return self.inner(gig)

    def __repr__(self):
        return f"SuspendableFilter({self.filter_name!r}, {self.inner!r})"
```

Fail-open on unparseable gig dates (consistent with every other filter in this file).

### 3. Wiring in `main.py`

Near the top of `run()`, alongside the other per-tick setup:

```python
suspension_snapshot = filter_suspension_store.load_active()
```

Each filter instance is wrapped **once**, at construction time, before it's added to either chain — critically, `_fee_filter` is wrapped before the NEG-drafts fee-partition block calls it directly (`if _fee_filter(gig):`), so a fee suspension takes effect there too, not just in the chains:

```python
_fee_filter = FeeFilter(...) if settings.enable_fee_filter else None
if _fee_filter is not None:
    _fee_filter = SuspendableFilter("fee", _fee_filter, suspension_snapshot)
```

Same pattern for `_sunday_time_filter` ("sunday_time"), `BlacklistFilter(...)` ("blacklist"), `PostcodeFilter(...)` ("postcode"), `CalendarFilter(cal_client)` ("calendar"), and each `AvailabilityFilter` in `_avail_filters` ("availability"). `SeenFilter` is left unwrapped entirely — so a `filter="all"` suspension never touches it either, since "all" only ever reaches filters that were wrapped in the first place.

Cleanup call, alongside the existing `application_store.expire_past_applied()` post-pipeline step:

```python
filter_suspension_store.purge_past_suspensions()
```

### 4. Telegram tool (`unified_agent.py`)

New tool `manage_filter_suspensions`:

```python
{
    "name": "manage_filter_suspensions",
    "description": (
        "Suspend or resume gig filters for a date range, keyed by the GIG's own date "
        "(not today's date) — e.g. 'ignore the postcode filter for gigs in December'. "
        "action: list, add, or remove. filter: fee, sunday_time, blacklist, postcode, "
        "calendar, availability, or all. The 'seen' filter cannot be suspended — doing so "
        "would just re-send the same application every poll tick instead of exempting a "
        "category of gig. period formats: YYYY-MM-DD, YYYY-MM-DD:YYYY-MM-DD, YYYY-MM, "
        "YYYY-MM-DD: (from that date onward, open-ended), :YYYY-MM-DD (up to and including "
        "that date). Also accepts the same relative phrases as manage_unavailable: today, "
        "tomorrow, this/next <weekday>, this weekend, next week, this/next month."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "add", "remove"]},
            "filter": {
                "type": "string",
                "enum": ["fee", "sunday_time", "blacklist", "postcode", "calendar", "availability", "all"],
            },
            "period": {"type": "string"},
        },
        "required": ["action"],
    },
}
```

Handler `_handle_manage_filter_suspensions` follows the exact shape of `_handle_manage_unavailable`:
- Resolves relative phrases via the existing `_resolve_period(period)` helper before parsing (shared, no duplication).
- For `add`/`remove`, catches `ValueError` from the store (bad filter name / unparsable period) and returns a plain user-facing error string rather than raising.
- `list` formats output via a new `_format_suspension(entry)` helper (extends the existing `_format_period` convention to also print the filter name, and to render open-ended bounds as "from ... onward" / "through ...").

System prompt gets new few-shot examples (alongside the existing `manage_unavailable` ones around line 67-70):
```
- "Turn off the postcode filter for all of December" → manage_filter_suspensions(action=add, filter=postcode, period=2026-12).
- "Ignore the fee filter from August 1st onward" → manage_filter_suspensions(action=add, filter=fee, period=2026-08-01:).
- "Disable every filter until the 5th of January" → manage_filter_suspensions(action=add, filter=all, period=:2026-01-05).
- "What filters are currently suspended?" → manage_filter_suspensions(action=list).
```

---

## Error Handling

- Bad filter name or unparsable period on `add`/`remove` → `ValueError` from the store, caught in the handler, surfaced as a plain-text error to the user (no exception leaks to Telegram).
- Unparseable gig date in `SuspendableFilter` → fail-open (filter still runs normally), matching every other filter's fail-open convention in this file.
- A `"seen"` suspension request is rejected at the schema level (not in the enum) — the agent will explain why rather than the tool erroring.

## Testing

- `filter_suspension_store`: open-ended token parsing (`"2026-08-01:"`, `":2026-08-01"`), `add_suspension`/`remove_suspension` dedup + validation, `purge_past_suspensions` (closed ranges expire, open-start ranges never do), `is_suspended` (exact filter match, `"all"` match, no match).
- `SuspendableFilter`: suspended date → passes through without calling inner; non-suspended date → delegates to inner; unparseable gig date → fails open to inner.
- `main.py` integration: a suspended `_fee_filter` still gets skipped correctly inside the NEG-drafts fee-partition block (not just inside `GigFilterChain`).
- `unified_agent`: `manage_filter_suspensions` add/list/remove round-trip; relative-phrase resolution reused from `_resolve_period`; rejecting `filter="seen"` at the schema/enum level.

## Files Changed

| File | Change |
|------|--------|
| `organist_bot/filter_suspension_store.py` | New file — store, open-ended period parsing, `load_active`, `is_suspended` |
| `organist_bot/filters.py` | Add `SuspendableFilter` wrapper class |
| `main.py` | Build suspension snapshot once per tick; wrap filter instances; call `purge_past_suspensions()` |
| `organist_bot/integrations/unified_agent.py` | New `manage_filter_suspensions` tool + handler + system-prompt examples |
| `tests/test_filter_suspension_store.py` | New — store + parsing tests |
| `tests/test_filters.py` | `SuspendableFilter` tests |
| `tests/test_unified_agent.py` | Tool handler tests |
| `CLAUDE.md` | Document the new store, wrapper, and tool under Filters / Data files |

## Out of Scope

- Suspending `seen` (deliberately excluded — see Problem/Goal).
- Wall-clock-keyed suspensions (suspend-during-this-window regardless of gig date) — explicitly not what was asked for.
- Calendar-visualizing suspensions (unlike unavailable periods, suspensions don't get mirrored to Google Calendar — they aren't "you are unavailable," they're "don't apply filter X").
- Retroactively re-evaluating gigs already processed before a suspension was added.
