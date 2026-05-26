# Application Tracking

**Date:** 2026-05-26
**Status:** Approved

## Problem

The bot auto-applies to gigs but has no memory of what it applied to. There is no way to ask "what's pending?", see which gigs were accepted, or know which applications never got a reply.

## Goal

Track every gig the bot applies to through its full lifecycle — from application sent to accepted, no response, or declined — with automatic status transitions where possible and a Telegram interface for querying and manual overrides.

## Scope

- Spec 1 of 3: core tracking + calendar trigger + date expiry
- Spec 2: email reply monitoring (separate)
- Spec 3: income forecasting (separate, consumes this data)

## Statuses

```
applied → accepted       (calendar event added via /addgig)
applied → no_response    (gig date passed, scheduler tick)
applied → declined       (manual via Telegram)
accepted → declined      (cancellation, manual via Telegram)
```

## Data Model

**`data/applications.json`** — flat JSON array, one object per application:

```json
{
  "url": "https://organistsonline.org/gig/123",
  "header": "Sunday Service",
  "organisation": "St. Mary's Church",
  "date": "2026-06-15",
  "fee": "£80",
  "email": "contact@stmarys.com",
  "status": "applied",
  "applied_at": "2026-05-26T10:30:00Z",
  "updated_at": "2026-05-26T10:30:00Z"
}
```

Fields `organisation`, `fee`, and `email` may be empty strings if unavailable at record time.

## Architecture

### 1. `organist_bot/application_store.py` (new)

Mirrors `filter_store.py` — a module-level JSON store backed by `data/applications.json`.

```python
def record_application(gig: Gig) -> bool:
    """Write a new 'applied' record. Returns False if URL already exists (idempotent)."""

def update_status(url: str, status: str) -> bool:
    """Update status and updated_at for the record with the given URL. Returns False if not found."""

def upsert_accepted(url: str | None, header: str, organisation: str, date: str, fee: str) -> None:
    """Create or update a record to 'accepted'.
    If url is given and matches an existing record, updates it.
    Otherwise creates a new accepted record (url may be None for manual entries).
    """

def expire_past_applied() -> int:
    """Mark all 'applied' records whose date < today as 'no_response'. Returns count changed."""

def list_applications(days: int = 30) -> list[dict]:
    """Return all records with applied_at within the last N days, newest first."""
```

Fails open on read errors (returns empty list / False). Writes are atomic via temp-file rename.

### 2. `organist_bot/notifier.py` — `apply_to_gig`

After dispatching the application email, call `application_store.record_application(gig)`. One line addition. No change to error handling — a store failure must not prevent the email from being reported as sent.

### 3. `organist_bot/integrations/unified_agent.py` — `add_gig` tool

After the calendar event is successfully created, call `application_store.upsert_accepted(...)`:

- **`/addgig <url>`** — pass the scraped URL. `upsert_accepted` finds the matching `applied` record and updates it, or creates a new `accepted` record if none exists.
- **Manual `/addgig` entry** — pass `url=None`. Always creates a new `accepted` record.

### 4. `main.py` — `_run`

At the end of each scheduler tick (after notify phase), call `application_store.expire_past_applied()`. Log the count at INFO level if > 0.

### 5. `manage_applications` Telegram tool (new, in `unified_agent.py`)

Three actions:

**`summary`** (default days=30):
```
📋 Applications — last 30 days

Applied:      12
Accepted:      3
No response:   2
Declined:      1
Pending:       6
```

**`list`** (default days=30) — numbered, most recent first:
```
📋 Applications — last 30 days

1. ✅ Sunday Service — St Mary's  (15 Jun)  £80
2. ⏳ Evensong — All Saints       (22 Jun)  £100
3. ❌ Matins — St John's          (8 Jun)   £60
```

Status emoji: ✅ accepted · ⏳ applied (pending) · 🔕 no response · ❌ declined

**`update`** — takes `number` (1-based from last `list` call) and `status`:
```
manage_applications(action=update, number=2, status=declined)
```

Cached listing stored in `_last_application_listing[chat_id]` (same pattern as `_last_gig_listing`).

The tool description routes:
- "what applications are pending?" / "show my applications" → `manage_applications(action=list)`
- "application summary" / "how many gigs have I got?" → `manage_applications(action=summary)`
- "mark gig 2 as declined" → `manage_applications(action=update, number=2, status=declined)`

## Files Changed

| File | Change |
|------|--------|
| `organist_bot/application_store.py` | New module |
| `organist_bot/notifier.py` | Call `record_application` after `apply_to_gig` sends |
| `organist_bot/integrations/unified_agent.py` | Call `upsert_accepted` in `add_gig`; add `manage_applications` tool |
| `main.py` | Call `expire_past_applied()` each tick |
| `tests/test_application_store.py` | New test file |
| `tests/test_notifier.py` | Assert `record_application` called |
| `tests/test_unified_agent.py` | Three new `add_gig` cases; `manage_applications` cases |
| `tests/test_main.py` | Assert `expire_past_applied` called each tick |

## Tests

### `test_application_store.py`

| Test | Scenario |
|------|----------|
| `test_record_application_writes_applied_record` | New gig → record created with status `applied` |
| `test_record_application_idempotent` | Same URL twice → only one record, returns `False` second time |
| `test_update_status_changes_status_and_updated_at` | `update_status` on existing URL → status and `updated_at` change |
| `test_update_status_returns_false_when_not_found` | Unknown URL → returns `False`, no write |
| `test_upsert_accepted_updates_existing_record` | URL matches applied record → status becomes `accepted` |
| `test_upsert_accepted_creates_new_when_no_match` | URL not in store → new `accepted` record created |
| `test_upsert_accepted_creates_new_when_url_none` | `url=None` (manual entry) → new `accepted` record created |
| `test_expire_past_applied_marks_old_records` | Record with past date and `applied` status → becomes `no_response` |
| `test_expire_past_applied_leaves_future_records` | Record with future date → unchanged |
| `test_expire_past_applied_leaves_non_applied_records` | `accepted` record with past date → unchanged |
| `test_expire_returns_count_of_changed_records` | Two expired, one future → returns 2 |
| `test_list_applications_filters_by_days` | Record outside window excluded |

### `test_notifier.py`

| Test | Scenario |
|------|----------|
| `test_apply_to_gig_records_application` | `apply_to_gig` called → `record_application` called once with the gig |

### `test_unified_agent.py` — `add_gig` additions

| Test | Scenario |
|------|----------|
| `test_add_gig_url_match_updates_to_accepted` | URL matches existing `applied` record → `upsert_accepted` called |
| `test_add_gig_url_no_match_creates_accepted` | URL not in store → `upsert_accepted` called with url |
| `test_add_gig_manual_entry_creates_accepted` | Manual entry → `upsert_accepted` called with `url=None` |

### `test_main.py`

| Test | Scenario |
|------|----------|
| `test_expire_past_applied_called_each_tick` | `_run` completes → `expire_past_applied` called once |

## Error Handling

- `record_application` failure must not propagate — wrapped in try/except in `notifier.py`, logged at WARNING
- `upsert_accepted` failure likewise — wrapped in `add_gig` tool handler
- `expire_past_applied` failure likewise — wrapped in `main.py`, logged at WARNING
- Store read failure → fails open (empty list), never raises to callers

## Out of Scope

- Email reply monitoring (Spec 2)
- Income forecasting (Spec 3)
- Editing application details after the fact (fee, org name, etc.)
- Pagination of the `list` action
