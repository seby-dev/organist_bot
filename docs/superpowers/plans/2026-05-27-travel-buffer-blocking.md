# Travel Buffer Blocking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically block travel time before and after every accepted gig in Google Calendar using real drive-time from Google Maps (falling back to `max_travel_minutes` when unavailable), and remove the buffer events when a gig is cancelled or declined.

**Architecture:** A new `organist_bot/travel.py` module handles Google Maps lookups. `application_store` gains `postcode`, `time`, and buffer event ID fields. `GoogleCalendarClient` gains `add_travel_buffers()`. Both acceptance paths (reply_monitor and unified_agent) create buffers; both cancellation paths delete them.

**Tech Stack:** Python 3.12, `googlemaps` (already installed), `google-calendar-api` (already installed), `pydantic-settings`, `pytest`, `unittest.mock`.

---

## File Map

| File | Change |
|---|---|
| `organist_bot/config.py` | Add `travel_home_postcode: str = ""` |
| `organist_bot/travel.py` | **CREATE** — `get_travel_minutes(postcode) -> int \| None` |
| `organist_bot/application_store.py` | Add `postcode`, `time`, `travel_before_event_id`, `travel_after_event_id` to records; new `update_travel_buffer_ids()`; update `record_application()` and `upsert_accepted()` |
| `organist_bot/integrations/calendar_client.py` | Add `add_travel_buffers()` method |
| `organist_bot/reply_monitor.py` | Update `_create_calendar_event()` to create buffers; update cancellation path to delete them |
| `organist_bot/integrations/unified_agent.py` | Add optional `postcode` to `add_gig` tool; create/delete buffers |
| `tests/test_travel.py` | **CREATE** |
| `tests/test_application_store.py` | Extend |
| `tests/test_calendar_client.py` | Extend |
| `tests/test_reply_monitor.py` | Extend |
| `tests/test_unified_agent.py` | Extend |

---

## Task 1: Config field and `travel.py` module

**Files:**
- Modify: `organist_bot/config.py`
- Create: `organist_bot/travel.py`
- Create: `tests/test_travel.py`

- [ ] **Step 1: Write failing tests for `get_travel_minutes`**

Create `tests/test_travel.py`:

```python
"""Tests for organist_bot.travel."""
from unittest.mock import MagicMock, patch

import organist_bot.travel as travel_mod


def _make_client(minutes: int | None = 30, status: str = "OK") -> MagicMock:
    client = MagicMock()
    if minutes is None:
        client.distance_matrix.return_value = {
            "rows": [{"elements": [{"status": "ZERO_RESULTS"}]}]
        }
    else:
        client.distance_matrix.return_value = {
            "rows": [{"elements": [{"status": status, "duration": {"value": minutes * 60}}]}]
        }
    return client


class TestGetTravelMinutes:
    def test_returns_drive_time_in_minutes(self):
        mock_client = _make_client(minutes=40)
        with (
            patch("organist_bot.travel.settings") as mock_settings,
            patch("organist_bot.travel.googlemaps.Client", return_value=mock_client),
        ):
            mock_settings.google_maps_api_key = "key123"
            mock_settings.travel_home_postcode = "IG11 7ZW"
            mock_settings.home_postcode = "E1 1AA"
            result = travel_mod.get_travel_minutes("CM1 1AA")
        assert result == 40

    def test_uses_travel_home_postcode_as_origin(self):
        mock_client = _make_client(minutes=20)
        with (
            patch("organist_bot.travel.settings") as mock_settings,
            patch("organist_bot.travel.googlemaps.Client", return_value=mock_client),
        ):
            mock_settings.google_maps_api_key = "key"
            mock_settings.travel_home_postcode = "IG11 7ZW"
            mock_settings.home_postcode = "E1 1AA"
            travel_mod.get_travel_minutes("SW1A 1AA")
        call_kwargs = mock_client.distance_matrix.call_args
        assert call_kwargs.kwargs["origins"] == ["IG11 7ZW"]

    def test_falls_back_to_home_postcode_when_travel_home_blank(self):
        mock_client = _make_client(minutes=25)
        with (
            patch("organist_bot.travel.settings") as mock_settings,
            patch("organist_bot.travel.googlemaps.Client", return_value=mock_client),
        ):
            mock_settings.google_maps_api_key = "key"
            mock_settings.travel_home_postcode = ""
            mock_settings.home_postcode = "E1 1AA"
            travel_mod.get_travel_minutes("SW1A 1AA")
        call_kwargs = mock_client.distance_matrix.call_args
        assert call_kwargs.kwargs["origins"] == ["E1 1AA"]

    def test_returns_none_for_blank_postcode(self):
        with patch("organist_bot.travel.settings") as mock_settings:
            mock_settings.google_maps_api_key = "key"
            mock_settings.travel_home_postcode = "IG11 7ZW"
            result = travel_mod.get_travel_minutes("")
        assert result is None

    def test_returns_none_when_api_key_missing(self):
        with patch("organist_bot.travel.settings") as mock_settings:
            mock_settings.google_maps_api_key = ""
            result = travel_mod.get_travel_minutes("CM1 1AA")
        assert result is None

    def test_returns_none_on_non_ok_status(self):
        mock_client = _make_client(minutes=None)
        with (
            patch("organist_bot.travel.settings") as mock_settings,
            patch("organist_bot.travel.googlemaps.Client", return_value=mock_client),
        ):
            mock_settings.google_maps_api_key = "key"
            mock_settings.travel_home_postcode = "IG11 7ZW"
            mock_settings.home_postcode = "E1 1AA"
            result = travel_mod.get_travel_minutes("ZZ1 1ZZ")
        assert result is None

    def test_returns_none_on_api_exception(self):
        mock_client = MagicMock()
        mock_client.distance_matrix.side_effect = Exception("network error")
        with (
            patch("organist_bot.travel.settings") as mock_settings,
            patch("organist_bot.travel.googlemaps.Client", return_value=mock_client),
        ):
            mock_settings.google_maps_api_key = "key"
            mock_settings.travel_home_postcode = "IG11 7ZW"
            mock_settings.home_postcode = ""
            result = travel_mod.get_travel_minutes("CM1 1AA")
        assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_travel.py -v
```

Expected: `ModuleNotFoundError: No module named 'organist_bot.travel'`

- [ ] **Step 3: Add `travel_home_postcode` to config**

In `organist_bot/config.py`, add after the `max_travel_minutes` line:

```python
    # ── Postcode / distance filter ────────────────────────────────────────────
    home_postcode: str = ""
    google_maps_api_key: str = ""
    max_travel_minutes: int = 45
    travel_home_postcode: str = ""  # origin for travel buffer lookups; falls back to home_postcode
```

- [ ] **Step 4: Create `organist_bot/travel.py`**

```python
"""organist_bot/travel.py
─────────────────────────
Travel time lookup via Google Maps Distance Matrix API.

get_travel_minutes(postcode)
    Returns drive time in minutes from settings.travel_home_postcode
    (falling back to settings.home_postcode) to the given gig postcode.
    Returns None if postcode is blank, API key is missing, or the API call fails.
"""

import logging

import googlemaps

from organist_bot.config import settings

logger = logging.getLogger(__name__)


def get_travel_minutes(postcode: str) -> int | None:
    """Return drive time in minutes from home to postcode.

    Uses settings.travel_home_postcode as origin; falls back to settings.home_postcode.
    Returns None if postcode is blank, API key is missing, or the API call fails.
    """
    if not postcode or not postcode.strip():
        return None
    api_key = settings.google_maps_api_key
    if not api_key:
        return None
    origin = settings.travel_home_postcode or settings.home_postcode
    if not origin:
        return None
    try:
        client = googlemaps.Client(key=api_key)
        result = client.distance_matrix(
            origins=[origin],
            destinations=[postcode],
            mode="driving",
            units="metric",
        )
        element = result["rows"][0]["elements"][0]
        if element["status"] != "OK":
            logger.debug(
                "travel: Distance Matrix non-OK status %s for postcode %r",
                element["status"],
                postcode,
            )
            return None
        minutes = element["duration"]["value"] // 60
        logger.debug("travel: %r → %r = %d min", origin, postcode, minutes)
        return minutes
    except Exception as exc:
        logger.warning("travel: get_travel_minutes failed for %r: %s", postcode, exc)
        return None
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_travel.py -v
```

Expected: 7 tests PASS

- [ ] **Step 6: Commit**

```bash
git add organist_bot/config.py organist_bot/travel.py tests/test_travel.py
git commit -m "feat: add travel_home_postcode config and get_travel_minutes module"
```

---

## Task 2: Application store additions

**Files:**
- Modify: `organist_bot/application_store.py`
- Modify: `tests/test_application_store.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_application_store.py` at the end of the file:

```python
# ── New fields: postcode, time, travel buffer IDs ────────────────────────────


class TestPostcodeStoredOnApplication:
    def test_record_application_stores_postcode(self):
        gig = _make_gig(postcode="CM1 1AA")
        store.record_application(gig)
        records = json.loads(store._PATH.read_text())
        assert records[0]["postcode"] == "CM1 1AA"

    def test_record_application_stores_time(self):
        gig = _make_gig(time="10:30 AM")
        store.record_application(gig)
        records = json.loads(store._PATH.read_text())
        assert records[0]["time"] == "10:30 AM"

    def test_record_application_stores_blank_postcode_when_none(self):
        gig = _make_gig()  # postcode not set → None
        store.record_application(gig)
        records = json.loads(store._PATH.read_text())
        assert records[0]["postcode"] == ""


class TestUpsertAcceptedPostcode:
    def test_upsert_accepted_stores_postcode(self):
        store.upsert_accepted(
            url="http://a.com/1",
            header="Wedding",
            organisation="St Mary's",
            date="2026-07-01",
            fee="£200",
            postcode="SW1A 1AA",
        )
        records = json.loads(store._PATH.read_text())
        assert records[0]["postcode"] == "SW1A 1AA"

    def test_upsert_accepted_postcode_defaults_to_empty(self):
        store.upsert_accepted(
            url="http://a.com/2",
            header="Funeral",
            organisation="St John's",
            date="2026-07-02",
            fee="£100",
        )
        records = json.loads(store._PATH.read_text())
        assert records[0]["postcode"] == ""


class TestUpdateTravelBufferIds:
    def test_sets_buffer_ids_on_existing_record(self):
        gig = _make_gig()
        store.record_application(gig)
        result = store.update_travel_buffer_ids(
            "https://organistsonline.org/gig/123", "before_id_123", "after_id_456"
        )
        assert result is True
        records = json.loads(store._PATH.read_text())
        assert records[0]["travel_before_event_id"] == "before_id_123"
        assert records[0]["travel_after_event_id"] == "after_id_456"

    def test_returns_false_for_unknown_url(self):
        result = store.update_travel_buffer_ids("http://not-found.com", "b", "a")
        assert result is False
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_application_store.py::TestPostcodeStoredOnApplication tests/test_application_store.py::TestUpsertAcceptedPostcode tests/test_application_store.py::TestUpdateTravelBufferIds -v
```

Expected: FAIL — `postcode` not in record, `update_travel_buffer_ids` not defined.

- [ ] **Step 3: Update `record_application` and `upsert_accepted` in `application_store.py`**

In `record_application`, add `postcode` and `time` to the stored dict:

```python
def record_application(gig: Gig) -> bool:
    """Write a new 'applied' record. Returns False if URL already exists (idempotent)."""
    records = _read()
    if any(r["url"] == gig.link for r in records):
        return False
    now = _now_iso()
    records.append(
        {
            "url": gig.link,
            "header": gig.header or "",
            "organisation": gig.organisation or "",
            "date": gig.date or "",
            "time": gig.time or "",
            "fee": gig.fee or "",
            "email": gig.email or "",
            "postcode": gig.postcode or "",
            "status": "applied",
            "applied_at": now,
            "updated_at": now,
        }
    )
    _write(records)
    return True
```

Update `upsert_accepted` signature and body:

```python
def upsert_accepted(
    url: str | None,
    header: str,
    organisation: str,
    date: str,
    fee: str,
    email: str = "",
    *,
    postcode: str = "",
) -> None:
    """Create or update a record to 'accepted'.

    If url is given and matches an existing record, updates it in place.
    Otherwise creates a new 'accepted' record (url may be None for manual entries).
    """
    records = _read()
    now = _now_iso()
    if url is not None:
        for r in records:
            if r["url"] == url:
                r["status"] = "accepted"
                r["updated_at"] = now
                if postcode:
                    r["postcode"] = postcode
                _write(records)
                return
    records.append(
        {
            "url": url or "",
            "header": header,
            "organisation": organisation,
            "date": date,
            "time": "",
            "fee": fee,
            "email": email,
            "postcode": postcode,
            "status": "accepted",
            "applied_at": now,
            "updated_at": now,
        }
    )
    _write(records)
```

- [ ] **Step 4: Add `update_travel_buffer_ids` to `application_store.py`**

Add after `update_reply_message_id`:

```python
def update_travel_buffer_ids(url: str, before_id: str, after_id: str) -> bool:
    """Set travel_before_event_id and travel_after_event_id on the record with the given URL.

    Returns False if not found.
    """
    records = _read()
    for r in records:
        if r["url"] == url:
            r["travel_before_event_id"] = before_id
            r["travel_after_event_id"] = after_id
            r["updated_at"] = _now_iso()
            _write(records)
            return True
    return False
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_application_store.py -v
```

Expected: all tests PASS

- [ ] **Step 6: Commit**

```bash
git add organist_bot/application_store.py tests/test_application_store.py
git commit -m "feat: store postcode, time, and travel buffer IDs in application_store"
```

---

## Task 3: `add_travel_buffers` on `GoogleCalendarClient`

**Files:**
- Modify: `organist_bot/integrations/calendar_client.py`
- Modify: `tests/test_calendar_client.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_calendar_client.py`:

```python
import datetime as dt
# (already imported at top of file)


class TestAddTravelBuffers:
    def test_creates_two_buffer_events(self, client, mock_service):
        created_ids = iter(["before_evt_1", "after_evt_2"])

        def fake_insert(calendarId, body):
            mock = MagicMock()
            mock.execute.return_value = {"id": next(created_ids)}
            return mock

        mock_service.events.return_value.insert.side_effect = fake_insert

        start = dt.datetime(2026, 7, 15, 10, 0, tzinfo=dt.timezone.utc)
        end = dt.datetime(2026, 7, 15, 11, 0, tzinfo=dt.timezone.utc)
        before_id, after_id = client.add_travel_buffers(
            gig_summary="Wedding — St Mary's",
            start_dt=start,
            end_dt=end,
            travel_minutes=45,
        )

        assert before_id == "before_evt_1"
        assert after_id == "after_evt_2"
        assert mock_service.events.return_value.insert.call_count == 2

    def test_before_event_ends_at_gig_start(self, client, mock_service):
        inserted_bodies = []

        def fake_insert(calendarId, body):
            inserted_bodies.append(body)
            mock = MagicMock()
            mock.execute.return_value = {"id": f"evt_{len(inserted_bodies)}"}
            return mock

        mock_service.events.return_value.insert.side_effect = fake_insert

        start = dt.datetime(2026, 7, 15, 10, 0)
        end = dt.datetime(2026, 7, 15, 11, 0)
        client.add_travel_buffers("Test Gig", start, end, 30)

        before_body = inserted_bodies[0]
        after_body = inserted_bodies[1]

        # Before event: ends at gig start
        assert "Travel to Test Gig" in before_body["summary"]
        assert before_body["end"]["dateTime"] == start.isoformat()

        # After event: starts at gig end
        assert "Travel from Test Gig" in after_body["summary"]
        assert after_body["start"]["dateTime"] == end.isoformat()

    def test_events_tagged_with_extended_property(self, client, mock_service):
        inserted_bodies = []

        def fake_insert(calendarId, body):
            inserted_bodies.append(body)
            mock = MagicMock()
            mock.execute.return_value = {"id": "x"}
            return mock

        mock_service.events.return_value.insert.side_effect = fake_insert

        start = dt.datetime(2026, 7, 15, 10, 0)
        end = dt.datetime(2026, 7, 15, 11, 0)
        client.add_travel_buffers("Test Gig", start, end, 30)

        for body in inserted_bodies:
            assert body["extendedProperties"]["private"]["organist_bot_travel"] == "1"

    def test_raises_on_api_failure(self, client, mock_service):
        mock_service.events.return_value.insert.return_value.execute.side_effect = Exception(
            "API error"
        )
        start = dt.datetime(2026, 7, 15, 10, 0)
        end = dt.datetime(2026, 7, 15, 11, 0)
        with pytest.raises(Exception, match="API error"):
            client.add_travel_buffers("Test Gig", start, end, 30)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_calendar_client.py::TestAddTravelBuffers -v
```

Expected: FAIL — `add_travel_buffers` not defined.

- [ ] **Step 3: Implement `add_travel_buffers` in `calendar_client.py`**

Add after the `add_gig` method:

```python
def add_travel_buffers(
    self,
    gig_summary: str,
    start_dt: datetime.datetime,
    end_dt: datetime.datetime,
    travel_minutes: int,
) -> tuple[str, str]:
    """Create travel buffer events before and after a gig.

    Creates:
      - '🚗 Travel to {gig_summary}' ending at start_dt
      - '🚗 Travel from {gig_summary}' starting at end_dt

    Both events are tagged with extended property organist_bot_travel=1.
    Returns (before_event_id, after_event_id).
    Raises on API failure.
    """
    delta = datetime.timedelta(minutes=travel_minutes)

    before_event = {
        "summary": f"🚗 Travel to {gig_summary}",
        "start": {
            "dateTime": (start_dt - delta).isoformat(),
            "timeZone": "Europe/London",
        },
        "end": {
            "dateTime": start_dt.isoformat(),
            "timeZone": "Europe/London",
        },
        "extendedProperties": {"private": {"organist_bot_travel": "1"}},
    }

    after_event = {
        "summary": f"🚗 Travel from {gig_summary}",
        "start": {
            "dateTime": end_dt.isoformat(),
            "timeZone": "Europe/London",
        },
        "end": {
            "dateTime": (end_dt + delta).isoformat(),
            "timeZone": "Europe/London",
        },
        "extendedProperties": {"private": {"organist_bot_travel": "1"}},
    }

    t0 = time.perf_counter()
    before_created = (
        self._service.events().insert(calendarId=self.calendar_id, body=before_event).execute()
    )
    before_id = before_created["id"]

    after_created = (
        self._service.events().insert(calendarId=self.calendar_id, body=after_event).execute()
    )
    after_id = after_created["id"]

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    logger.info(
        "Travel buffers created",
        extra={
            "gig_summary": gig_summary,
            "travel_minutes": travel_minutes,
            "before_id": before_id,
            "after_id": after_id,
            "elapsed_ms": elapsed_ms,
        },
    )
    return before_id, after_id
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_calendar_client.py -v
```

Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add organist_bot/integrations/calendar_client.py tests/test_calendar_client.py
git commit -m "feat: add add_travel_buffers method to GoogleCalendarClient"
```

---

## Task 4: Reply monitor integration

**Files:**
- Modify: `organist_bot/reply_monitor.py`
- Modify: `tests/test_reply_monitor.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_reply_monitor.py` (after existing `TestCheckReplies` class):

```python
class TestCreateCalendarEventWithBuffers:
    """_create_calendar_event should call add_travel_buffers and store IDs."""

    def _make_record(self, postcode="CM1 1AA", time_str="10:00 AM"):
        return {
            "url": "http://a.com/1",
            "header": "Wedding Service",
            "organisation": "St Mary's",
            "date": "2026-07-15",
            "time": time_str,
            "fee": "£200",
            "email": "stmary@example.com",
            "status": "accepted",
            "postcode": postcode,
        }

    def test_creates_travel_buffers_when_postcode_available(self):
        record = self._make_record()
        with (
            patch("organist_bot.reply_monitor.settings") as mock_settings,
            patch("organist_bot.reply_monitor.GoogleCalendarClient") as mock_cal_cls,
            patch("organist_bot.reply_monitor.travel") as mock_travel,
            patch("organist_bot.reply_monitor.application_store") as mock_store,
        ):
            mock_settings.google_calendar_id = "cal@test.com"
            mock_settings.google_calendar_credentials_file = "creds.json"
            mock_settings.max_travel_minutes = 45
            mock_cal = mock_cal_cls.return_value
            mock_cal.add_gig.return_value = "event_123"
            mock_cal.add_travel_buffers.return_value = ("before_id", "after_id")
            mock_travel.get_travel_minutes.return_value = 35

            from organist_bot.reply_monitor import _create_calendar_event
            result = _create_calendar_event(record)

        assert result is True
        mock_travel.get_travel_minutes.assert_called_once_with("CM1 1AA")
        mock_cal.add_travel_buffers.assert_called_once()
        mock_store.update_travel_buffer_ids.assert_called_once_with(
            "http://a.com/1", "before_id", "after_id"
        )

    def test_falls_back_to_max_travel_minutes_when_maps_returns_none(self):
        record = self._make_record()
        with (
            patch("organist_bot.reply_monitor.settings") as mock_settings,
            patch("organist_bot.reply_monitor.GoogleCalendarClient") as mock_cal_cls,
            patch("organist_bot.reply_monitor.travel") as mock_travel,
            patch("organist_bot.reply_monitor.application_store"),
        ):
            mock_settings.google_calendar_id = "cal@test.com"
            mock_settings.google_calendar_credentials_file = "creds.json"
            mock_settings.max_travel_minutes = 45
            mock_cal = mock_cal_cls.return_value
            mock_cal.add_gig.return_value = "event_123"
            mock_cal.add_travel_buffers.return_value = ("b", "a")
            mock_travel.get_travel_minutes.return_value = None

            from organist_bot.reply_monitor import _create_calendar_event
            _create_calendar_event(record)

        call_args = mock_cal.add_travel_buffers.call_args
        assert call_args.kwargs.get("travel_minutes", call_args.args[3] if len(call_args.args) > 3 else None) == 45

    def test_skips_buffers_when_time_unparseable(self):
        record = self._make_record(time_str="")
        with (
            patch("organist_bot.reply_monitor.settings") as mock_settings,
            patch("organist_bot.reply_monitor.GoogleCalendarClient") as mock_cal_cls,
            patch("organist_bot.reply_monitor.travel") as mock_travel,
            patch("organist_bot.reply_monitor.application_store"),
        ):
            mock_settings.google_calendar_id = "cal@test.com"
            mock_settings.google_calendar_credentials_file = "creds.json"
            mock_settings.max_travel_minutes = 45
            mock_cal = mock_cal_cls.return_value
            mock_cal.add_gig.side_effect = ValueError("Cannot parse gig time")
            mock_travel.get_travel_minutes.return_value = 30

            from organist_bot.reply_monitor import _create_calendar_event
            result = _create_calendar_event(record)

        assert result is False
        mock_cal.add_travel_buffers.assert_not_called()


class TestCancellationDeletesBuffers:
    """Cancellation path should delete travel buffer events."""

    def _make_record(self, url, email):
        return {
            "url": url,
            "header": "Wedding",
            "organisation": "St John",
            "date": "2026-07-15",
            "time": "11:00 AM",
            "fee": "£150",
            "email": email,
            "status": "accepted",
            "applied_at": "2026-06-01T10:00:00Z",
            "updated_at": "2026-06-01T10:00:00Z",
            "reply_message_id": None,
            "travel_before_event_id": "before_abc",
            "travel_after_event_id": "after_def",
        }

    def test_cancellation_deletes_travel_buffer_events(self):
        records = [self._make_record("http://a.com/1", "church@example.com")]
        messages = [
            {
                "message_id": "msg1",
                "sender": "church@example.com",
                "recipient": "me@example.com",
                "body": "We need to cancel the booking.",
                "direction": "incoming",
            }
        ]
        with (
            patch("organist_bot.reply_monitor.settings") as mock_settings,
            patch(
                "organist_bot.reply_monitor.application_store.list_applications",
                return_value=records,
            ),
            patch("organist_bot.reply_monitor.application_store.update_reply_message_id"),
            patch("organist_bot.reply_monitor._make_gmail_client") as mock_gmail,
            patch("organist_bot.reply_monitor._classify_reply", return_value="cancellation"),
            patch("organist_bot.reply_monitor._send_telegram_notification"),
            patch("organist_bot.reply_monitor._make_calendar_client") as mock_cal_fn,
        ):
            mock_settings.gmail_credentials_file = "creds.json"
            mock_settings.gmail_token_file = "token.json"
            mock_settings.google_calendar_id = "cal@test.com"
            mock_settings.google_calendar_credentials_file = "creds.json"
            mock_settings.anthropic_api_key = "key"
            mock_gmail.return_value.fetch_reply_messages.return_value = messages
            mock_cal = MagicMock()
            mock_cal_fn.return_value = mock_cal

            check_replies()

        delete_calls = [str(c) for c in mock_cal.delete_event.call_args_list]
        deleted_ids = [c.args[0] for c in mock_cal.delete_event.call_args_list]
        assert "before_abc" in deleted_ids
        assert "after_def" in deleted_ids
```

Note: You'll need to add `from unittest.mock import MagicMock` at the top of `tests/test_reply_monitor.py` if not already present.

- [ ] **Step 2: Run tests to verify they fail**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_reply_monitor.py::TestCreateCalendarEventWithBuffers tests/test_reply_monitor.py::TestCancellationDeletesBuffers -v
```

Expected: FAIL — `travel` not imported, buffer logic not implemented.

- [ ] **Step 3: Update `_create_calendar_event` in `reply_monitor.py`**

Replace the existing `_create_calendar_event` function with:

```python
def _create_calendar_event(record: dict) -> bool:
    """Create a Google Calendar event and travel buffers for an accepted booking.

    Returns True if the gig event was created successfully (buffer failure is non-fatal).
    """
    if not settings.google_calendar_id or not settings.google_calendar_credentials_file:
        return False
    try:
        from organist_bot import travel
        from organist_bot.filters import normalize_to_yyyymmdd, parse_start_time
        from organist_bot.integrations.calendar_client import GoogleCalendarClient
        from organist_bot.models import Gig

        cal = GoogleCalendarClient(
            credentials_file=settings.google_calendar_credentials_file,
            calendar_id=settings.google_calendar_id,
        )
        gig = Gig(
            link=record.get("url", ""),
            header=record.get("header", record.get("organisation", "Gig")),
            organisation=record.get("organisation", ""),
            locality="",
            date=record.get("date", ""),
            time=record.get("time", ""),
            fee=record.get("fee", ""),
        )
        cal.add_gig(gig)

        # Travel buffers (non-fatal — gig event is already created)
        try:
            date_str = normalize_to_yyyymmdd(gig.date)
            start_time = parse_start_time(gig.time)
            if date_str and start_time:
                import datetime as _dt
                date = _dt.datetime.strptime(date_str, "%Y%m%d").date()
                start_dt = _dt.datetime.combine(date, start_time)
                end_dt = start_dt + _dt.timedelta(hours=1)
                postcode = record.get("postcode", "")
                travel_mins = travel.get_travel_minutes(postcode) or settings.max_travel_minutes
                before_id, after_id = cal.add_travel_buffers(
                    gig_summary=f"{gig.header} — {gig.organisation}",
                    start_dt=start_dt,
                    end_dt=end_dt,
                    travel_minutes=travel_mins,
                )
                url = record.get("url", "")
                if url:
                    application_store.update_travel_buffer_ids(url, before_id, after_id)
        except Exception as buf_exc:
            logger.warning("reply_monitor: travel buffer creation failed: %s", buf_exc)

        return True
    except Exception as exc:
        logger.warning("reply_monitor: calendar event creation failed: %s", exc)
        return False
```

- [ ] **Step 4: Add `_make_calendar_client` helper and update cancellation path**

Add helper function after `_create_calendar_event`:

```python
def _make_calendar_client():
    """Return a GoogleCalendarClient if configured, else None."""
    if not settings.google_calendar_id or not settings.google_calendar_credentials_file:
        return None
    from organist_bot.integrations.calendar_client import GoogleCalendarClient
    return GoogleCalendarClient(
        credentials_file=settings.google_calendar_credentials_file,
        calendar_id=settings.google_calendar_id,
    )
```

Then in `check_replies()`, find the cancellation branch and update it:

```python
            elif classification == "cancellation":
                _send_telegram_notification(
                    f"⚠️ Possible cancellation: {org} on {date}\n"
                    f"Reply from: {msg.get('sender', 'unknown')}\n"
                    f'"{msg.get("body", "")[:200]}"\n\n'
                    "Delete calendar event or ignore?"
                )
                # Delete travel buffer events automatically
                cal = _make_calendar_client()
                if cal:
                    for field in ("travel_before_event_id", "travel_after_event_id"):
                        evt_id = record.get(field)
                        if evt_id:
                            try:
                                cal.delete_event(evt_id)
                            except Exception as del_exc:
                                logger.warning(
                                    "reply_monitor: failed to delete travel buffer %s: %s",
                                    evt_id, del_exc,
                                )
```

- [ ] **Step 5: Run all reply_monitor tests**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_reply_monitor.py -v
```

Expected: all tests PASS

- [ ] **Step 6: Commit**

```bash
git add organist_bot/reply_monitor.py tests/test_reply_monitor.py
git commit -m "feat: create and delete travel buffers in reply_monitor"
```

---

## Task 5: Unified agent integration

**Files:**
- Modify: `organist_bot/integrations/unified_agent.py`
- Modify: `tests/test_unified_agent.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_unified_agent.py`:

```python
class TestAddGigTravelBuffers:
    """add_gig should create travel buffers after creating the calendar event."""

    async def _call_add_gig(self, extra_fields=None):
        fields = {
            "confirmed": True,
            "header": "Wedding",
            "organisation": "St Paul's",
            "locality": "Chelmsford",
            "date": "2026-07-15",
            "time": "11:00 AM",
            "fee": "£200",
        }
        if extra_fields:
            fields.update(extra_fields)
        agent = UnifiedAgent()
        return await agent._execute_tool("add_gig", fields, chat_id=1)

    async def test_creates_travel_buffers_when_postcode_provided(self):
        with (
            patch("organist_bot.integrations.unified_agent._make_calendar_client") as mock_cal_fn,
            patch("organist_bot.integrations.unified_agent.travel") as mock_travel,
            patch("organist_bot.integrations.unified_agent.application_store"),
            patch("organist_bot.integrations.unified_agent.filter_store"),
            patch("organist_bot.integrations.unified_agent.settings") as mock_settings,
        ):
            mock_settings.max_travel_minutes = 45
            mock_cal = MagicMock()
            mock_cal_fn.return_value = mock_cal
            mock_cal.add_gig.return_value = "event_123"
            mock_cal.add_travel_buffers.return_value = ("before_id", "after_id")
            mock_travel.get_travel_minutes.return_value = 40

            result = await self._call_add_gig({"postcode": "CM1 1AA"})

        import json
        data = json.loads(result)
        assert "event_123" in data["result"]
        mock_travel.get_travel_minutes.assert_called_once_with("CM1 1AA")
        mock_cal.add_travel_buffers.assert_called_once()

    async def test_add_gig_still_succeeds_when_buffer_fails(self):
        with (
            patch("organist_bot.integrations.unified_agent._make_calendar_client") as mock_cal_fn,
            patch("organist_bot.integrations.unified_agent.travel") as mock_travel,
            patch("organist_bot.integrations.unified_agent.application_store"),
            patch("organist_bot.integrations.unified_agent.filter_store"),
            patch("organist_bot.integrations.unified_agent.settings") as mock_settings,
        ):
            mock_settings.max_travel_minutes = 45
            mock_cal = MagicMock()
            mock_cal_fn.return_value = mock_cal
            mock_cal.add_gig.return_value = "event_123"
            mock_cal.add_travel_buffers.side_effect = Exception("Calendar API down")
            mock_travel.get_travel_minutes.return_value = 30

            result = await self._call_add_gig({"postcode": "CM1 1AA"})

        import json
        data = json.loads(result)
        assert "event_123" in data["result"]  # Still succeeded


class TestManageApplicationsDeclinedDeletesBuffers:
    async def test_declined_accepted_deletes_travel_buffers(self):
        record = {
            "url": "http://a.com/1",
            "header": "Wedding",
            "organisation": "St Mary's",
            "date": "2026-07-15",
            "status": "accepted",
            "travel_before_event_id": "before_abc",
            "travel_after_event_id": "after_def",
        }
        with (
            patch("organist_bot.integrations.unified_agent.application_store") as mock_store,
            patch("organist_bot.integrations.unified_agent._make_calendar_client") as mock_cal_fn,
        ):
            mock_store.list_applications.return_value = [record]
            mock_store.update_status.return_value = True
            mock_cal = MagicMock()
            mock_cal_fn.return_value = mock_cal

            agent = UnifiedAgent()
            agent._last_application_listing[1] = [record]
            await agent._execute_tool(
                "manage_applications",
                {"action": "update", "number": 1, "status": "declined"},
                chat_id=1,
            )

        deleted_ids = [c.args[0] for c in mock_cal.delete_event.call_args_list]
        assert "before_abc" in deleted_ids
        assert "after_def" in deleted_ids
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_unified_agent.py::TestAddGigTravelBuffers tests/test_unified_agent.py::TestManageApplicationsDeclinedDeletesBuffers -v
```

Expected: FAIL

- [ ] **Step 3: Add `travel` import to `unified_agent.py`**

Near the top of `organist_bot/integrations/unified_agent.py`, add to the imports from `organist_bot`:

```python
from organist_bot import analytics, application_store, filter_store, travel
```

- [ ] **Step 4: Add `postcode` field to `add_gig` tool definition**

Find the `add_gig` tool in the TOOLS list and add `postcode` to its properties:

```python
{
    "name": "add_gig",
    ...
    "properties": {
        ...
        "postcode": {
            "type": "string",
            "description": "Gig venue postcode for travel buffer calculation (e.g. CM1 1AA)",
        },
        ...
    }
}
```

- [ ] **Step 5: Update the `add_gig` handler in `_execute_tool`**

After `event_id = cal.add_gig(gig)` and before `yyyymmdd = normalize_to_yyyymmdd(...)`, add travel buffer creation:

```python
            event_id = cal.add_gig(gig)

            # Travel buffers (non-fatal)
            try:
                postcode = input_data.get("postcode", "")
                yyyymmdd_buf = normalize_to_yyyymmdd(fields["date"])
                start_time_buf = parse_start_time(fields["time"])
                if yyyymmdd_buf and start_time_buf:
                    buf_date = datetime.datetime.strptime(yyyymmdd_buf, "%Y%m%d").date()
                    buf_start = datetime.datetime.combine(buf_date, start_time_buf)
                    buf_end = buf_start + datetime.timedelta(hours=1)
                    travel_mins = travel.get_travel_minutes(postcode) or settings.max_travel_minutes
                    before_id, after_id = cal.add_travel_buffers(
                        gig_summary=f"{fields['header']} — {fields['organisation']}",
                        start_dt=buf_start,
                        end_dt=buf_end,
                        travel_minutes=travel_mins,
                    )
                    if url:
                        application_store.update_travel_buffer_ids(url, before_id, after_id)
            except Exception as buf_exc:
                logger.warning("add_gig: travel buffer creation failed: %s", buf_exc)
```

Also update the `upsert_accepted` call to pass postcode:

```python
                application_store.upsert_accepted(
                    url=url,
                    header=fields["header"],
                    organisation=fields.get("organisation", ""),
                    date=fields["date"],
                    fee=fields["fee"] if fields["fee"] != "not specified" else "",
                    postcode=input_data.get("postcode", ""),
                )
```

- [ ] **Step 6: Update `manage_applications` declined path to delete buffers**

Find the block around line 1281 where `original_status == "accepted" and status == "declined"` and add buffer deletion:

```python
                if original_status == "accepted" and status == "declined":
                    org = record.get("organisation") or record.get("header", "")
                    date = record.get("date", "")
                    # Delete travel buffer events
                    cal = _make_calendar_client()
                    if cal:
                        for field in ("travel_before_event_id", "travel_after_event_id"):
                            evt_id = record.get(field)
                            if evt_id:
                                try:
                                    cal.delete_event(evt_id)
                                except Exception as del_exc:
                                    logger.warning(
                                        "manage_applications: failed to delete travel buffer %s: %s",
                                        evt_id, del_exc,
                                    )
                    msg += (
                        f"\n\nThis was a confirmed booking ({org} on {date}). "
                        "Do you want to delete the calendar event?"
                    )
```

- [ ] **Step 7: Run all tests**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_unified_agent.py -v
```

Expected: all tests PASS

- [ ] **Step 8: Run full test suite**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest --tb=short -q
```

Expected: all tests PASS

- [ ] **Step 9: Commit**

```bash
git add organist_bot/integrations/unified_agent.py tests/test_unified_agent.py
git commit -m "feat: create and delete travel buffers in unified agent add_gig and manage_applications"
```
