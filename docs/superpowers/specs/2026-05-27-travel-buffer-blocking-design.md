# Travel Buffer Blocking Design

> **For agentic workers:** After approval, use `superpowers:writing-plans` to create the implementation plan.

**Goal:** When a gig is accepted, automatically block travel time before and after it in Google Calendar using real drive-time from Google Maps, falling back to `max_travel_minutes` when postcode is unavailable.

---

## Section 1: Data Layer

### `application_store.py`

Three new optional fields added to every application record:

| Field | Type | Set by |
|---|---|---|
| `postcode` | `str \| None` | `record_application(gig)` — stored from `gig.postcode` |
| `travel_before_event_id` | `str \| None` | `update_travel_buffer_ids()` after buffer creation |
| `travel_after_event_id` | `str \| None` | `update_travel_buffer_ids()` after buffer creation |

**New functions:**
- `update_travel_buffer_ids(url: str, before_id: str, after_id: str) -> bool` — sets both event ID fields on the matching record; returns False if URL not found.

**Modified functions:**
- `record_application(gig: Gig)` — add `"postcode": gig.postcode or ""` to the stored dict.
- `upsert_accepted(url, header, organisation, date, fee, email, *, postcode: str = "")` — add optional `postcode` kwarg; store it in the record.

Existing records without these fields degrade gracefully — all three fields are treated as absent/None.

### `config.py`

New optional setting:

```python
travel_home_postcode: str = ""
```

`TRAVEL_HOME_POSTCODE=ig117zw` in `.env`. Used as the origin for all travel time lookups. Falls back to `settings.home_postcode` if blank.

---

## Section 2: Travel Time Lookup

New module `organist_bot/travel.py`:

```python
def get_travel_minutes(postcode: str) -> int | None:
    """Return drive time in minutes from settings.travel_home_postcode to postcode.

    Returns None if postcode is blank, Google Maps API key is missing,
    or the API call fails. Caller should fall back to settings.max_travel_minutes.
    """
```

- Uses `googlemaps.Client(key=settings.google_maps_api_key)` — same library as `PostcodeFilter`.
- Calls Distance Matrix API: origin = `settings.travel_home_postcode or settings.home_postcode`, destination = `postcode`, mode = `"driving"`.
- Returns `element["duration"]["value"] // 60` on `status == "OK"`.
- Returns `None` on any exception or non-OK status (logs a warning).
- No caching — called once per acceptance event.

---

## Section 3: Calendar Buffer Creation

New method on `GoogleCalendarClient`:

```python
def add_travel_buffers(
    gig_summary: str,
    start_dt: datetime.datetime,
    end_dt: datetime.datetime,
    travel_minutes: int,
) -> tuple[str, str]:
    """Create travel buffer events before and after a gig.

    Returns (before_event_id, after_event_id).
    Raises on API failure.
    """
```

Creates two timed calendar events in `Europe/London` timezone:
- `"🚗 Travel to {gig_summary}"` — from `start_dt - timedelta(minutes=travel_minutes)` to `start_dt`
- `"🚗 Travel from {gig_summary}"` — from `end_dt` to `end_dt + timedelta(minutes=travel_minutes)`

Both events are tagged with extended property `organist_bot_travel=1` so they can be identified and deleted later.

---

## Section 4: Integration Points

### Buffer creation — two paths

**Path 1: `reply_monitor._create_calendar_event(record)`**

After `cal.add_gig(gig)` succeeds:
1. Read postcode from `record.get("postcode", "")`.
2. Call `travel.get_travel_minutes(postcode)` — result or `settings.max_travel_minutes` as fallback.
3. Call `cal.add_travel_buffers(gig_summary, start_dt, end_dt, travel_minutes)`.
4. Call `application_store.update_travel_buffer_ids(record["url"], before_id, after_id)`.
5. On any failure in steps 2–4: log a warning, do not raise (buffer failure must not block gig acceptance).

The gig's `start_dt` and `end_dt` are already computed inside `add_gig`; `_create_calendar_event` must compute them too using the same `normalize_to_yyyymmdd` + `parse_start_time` logic.

**Path 2: `unified_agent.add_gig` tool handler**

The `add_gig` tool definition gains an optional `postcode` field:
```json
{"name": "postcode", "type": "string", "description": "Gig venue postcode for travel buffer calculation"}
```

After `cal.add_gig(gig)` succeeds, same steps 1–5 as Path 1. `upsert_accepted` is also updated to pass `postcode=fields.get("postcode", "")`.

### Buffer removal — two paths

**Path 1: `reply_monitor.check_replies` cancellation**

When classification is `"cancellation"`, after deleting the main gig event, also delete buffer events:
```python
for field in ("travel_before_event_id", "travel_after_event_id"):
    event_id = record.get(field)
    if event_id:
        cal.delete_event(event_id)  # silently skip on failure
```

**Path 2: `unified_agent.manage_applications` status → `declined`**

Same deletion logic as Path 1, triggered at line ~1281 where `original_status == "accepted" and status == "declined"`.

---

## Error Handling

| Failure | Behaviour |
|---|---|
| Postcode missing | Fall back to `settings.max_travel_minutes` |
| Google Maps API error | Fall back to `settings.max_travel_minutes`, log warning |
| `add_travel_buffers` API error | Log warning, continue (gig event already created) |
| Buffer event ID missing at removal | Skip silently |

---

## Testing

- `tests/test_travel.py` — unit tests for `get_travel_minutes`: success path, non-OK status, exception, blank postcode, missing API key. Mock `googlemaps.Client`.
- `tests/test_calendar_client.py` (new or extend) — unit tests for `add_travel_buffers`: verifies correct event times, summaries, extended properties, return value.
- `tests/test_application_store.py` (extend) — tests for `record_application` storing postcode, `update_travel_buffer_ids` success and not-found paths.
- `tests/test_reply_monitor.py` (extend) — verify `_create_calendar_event` calls `add_travel_buffers` and `update_travel_buffer_ids`; verify cancellation path deletes buffer events.
- `tests/test_unified_agent.py` (extend) — verify `add_gig` creates buffers; verify `manage_applications` declined path deletes buffers.
