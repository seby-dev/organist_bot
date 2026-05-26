# Competing Gig Detection

**Date:** 2026-05-26
**Status:** Approved

## Problem

`CalendarFilter` silently rejects any gig whose date already has a calendar event. It cannot distinguish between:

- An **"Unavailable" block** the user set themselves → should still reject silently
- A **confirmed gig** → should reject but also notify the user via Telegram, because it represents a potentially valuable gig they are missing out on

## Goal

When a new scraped gig conflicts with an already-confirmed calendar event (a real gig, not an "Unavailable" block), send an immediate Telegram alert containing the full details of both gigs so the user can make an informed decision.

## Scope

- Notification channel: Telegram only (via existing `alert.send_alert`)
- Detection point: pre-filter pass (before detail-page fetch), so only basic gig fields are available
- No change to filter pipeline behaviour — competing gigs are still rejected

## Architecture

### 1. `GoogleCalendarClient.get_events_on_date(date_str: str) -> list[dict]`

New method alongside the existing `has_event_on_date`. Queries the calendar API for all events on `date_str` (format `YYYY-MM-DD`) and returns a list of `{id: str, summary: str}` dicts. Returns `[]` on API error (fail-open, matching existing behaviour).

`has_event_on_date` becomes a one-liner wrapper:

```python
def has_event_on_date(self, date_str: str) -> bool:
    return bool(self.get_events_on_date(date_str))
```

This preserves backwards compatibility for existing tests and callers.

### 2. `CalendarFilter` — updated logic

Calls `get_events_on_date` instead of `has_event_on_date`. After retrieving events:

```
events = client.get_events_on_date(normalized_date)

if not events:
    return True  # no conflict — pass

competing = [e for e in events if e["summary"] != "Unavailable"]

if competing:
    → send Telegram alert (see below)
    → log at INFO level
    return False

# only "Unavailable" blocks — silent reject (unchanged behaviour)
return False
```

### 3. Telegram alert format

Sent via `alert.send_alert(msg)`. Plain text, no Markdown (consistent with existing alerts):

```
⚠️ Competing gig — date already booked

New gig:  <gig.header> — <gig.organisation>
Date:     <gig.date>
Fee:      £<gig.fee>
URL:      <gig.url>

Conflicts with:
  • <event.summary>        ← one bullet per competing event
```

Fields available at pre-filter stage: `header`, `organisation`, `date`, `fee`, `url`.
`organisation` and `fee` are omitted from the alert line if blank/zero.

If multiple real events conflict (rare), each gets its own bullet under "Conflicts with".

## Data Flow

```
Scraper → basic Gig fields → pre-filter chain
                                  └── CalendarFilter
                                        ├── get_events_on_date(date)
                                        ├── competing events? → Telegram alert + reject
                                        └── only Unavailable? → silent reject
```

## Files Changed

| File | Change |
|------|--------|
| `organist_bot/integrations/calendar_client.py` | Add `get_events_on_date`; refactor `has_event_on_date` as wrapper |
| `organist_bot/filters.py` | Update `CalendarFilter.__call__` to use `get_events_on_date` and alert on competing events |
| `tests/test_calendar_client.py` | Add tests for `get_events_on_date` |
| `tests/test_filters.py` | Add 4 new `CalendarFilter` test cases |

## Tests

### `test_calendar_client.py` — `TestGetEventsOnDate`

| Test | Scenario |
|------|----------|
| `test_returns_events_on_matching_date` | API returns one event on the queried date → list with that event |
| `test_returns_empty_when_no_events` | API returns no items → `[]` |
| `test_returns_empty_on_api_error` | API raises → `[]` (fail-open) |
| `test_has_event_on_date_delegates_to_get_events` | `has_event_on_date` returns `True` iff `get_events_on_date` is non-empty |

### `tests/test_filters.py` — `TestCalendarFilterCompeting`

| Test | Scenario | Expected |
|------|----------|----------|
| `test_no_events_passes` | `get_events_on_date` returns `[]` | returns `True`, no alert |
| `test_unavailable_only_silent_reject` | only `"Unavailable"` event | returns `False`, no alert |
| `test_real_event_rejects_and_alerts` | one real event | returns `False`, alert fired with gig + event title |
| `test_mixed_events_alerts_real_only` | `"Unavailable"` + real event | returns `False`, alert fired listing only real event |

## Error Handling

- `get_events_on_date` fails open (returns `[]`) — same as `has_event_on_date` today. A broken calendar API should never silently drop gigs.
- `alert.send_alert` already handles Telegram failures internally (logs and continues). No additional error handling needed in `CalendarFilter`.

## Out of Scope

- Time-level conflict detection (two gigs same day, different times) — all calendar checks are date-granular today
- Email notification channel
- Surfacing competing gigs in the Telegram bot's `list_upcoming_gigs` tool
