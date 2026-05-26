# Competing Gig Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a scraped gig falls on a date already occupied by a confirmed calendar event (not an "Unavailable" block), reject the gig as before but also fire a Telegram alert containing the new gig's details and the conflicting event title(s).

**Architecture:** Add `get_events_on_date` to `GoogleCalendarClient` (returns `list[dict]` with id/summary instead of a bare bool), refactor `has_event_on_date` as a one-liner wrapper, then update `CalendarFilter` to call the new method and fire `alert.send_alert` when a real competing event is found.

**Tech Stack:** Python, `unittest.mock`, `pytest`, existing `organist_bot.alert.send_alert`.

---

### Task 1: Add `get_events_on_date` to `GoogleCalendarClient`

**Files:**
- Modify: `organist_bot/integrations/calendar_client.py` (lines 84–121 — `has_event_on_date`)
- Modify: `tests/test_calendar_client.py` (add `TestGetEventsOnDate` class; update `test_main.py` mock)
- Modify: `tests/test_main.py` (line 367 — swap `has_event_on_date` mock)

---

- [ ] **Step 1: Write the failing tests for `get_events_on_date`**

Add this class to `tests/test_calendar_client.py` directly after the `TestHasEventOnDate` class:

```python
# ── get_events_on_date ────────────────────────────────────────────────────────


class TestGetEventsOnDate:
    def test_returns_events_on_matching_date(self, client, mock_service):
        mock_service.events().list().execute.return_value = {
            "items": [{"id": "e1", "summary": "Evensong — St Mary's"}]
        }
        events = client.get_events_on_date("20260301")
        assert events == [{"id": "e1", "summary": "Evensong — St Mary's"}]

    def test_returns_multiple_events(self, client, mock_service):
        mock_service.events().list().execute.return_value = {
            "items": [
                {"id": "e1", "summary": "Matins"},
                {"id": "e2", "summary": "Evensong"},
            ]
        }
        events = client.get_events_on_date("20260301")
        assert len(events) == 2
        assert events[0] == {"id": "e1", "summary": "Matins"}
        assert events[1] == {"id": "e2", "summary": "Evensong"}

    def test_returns_empty_when_no_events(self, client, mock_service):
        mock_service.events().list().execute.return_value = {"items": []}
        assert client.get_events_on_date("20260301") == []

    def test_returns_empty_on_api_error(self, client, mock_service):
        mock_service.events().list().execute.side_effect = Exception("API down")
        assert client.get_events_on_date("20260301") == []

    def test_has_event_on_date_returns_true_when_events_exist(self, client, mock_service):
        mock_service.events().list().execute.return_value = {
            "items": [{"id": "e1", "summary": "Test Event"}]
        }
        assert client.has_event_on_date("20260301") is True

    def test_has_event_on_date_returns_false_when_no_events(self, client, mock_service):
        mock_service.events().list().execute.return_value = {"items": []}
        assert client.has_event_on_date("20260301") is False

    def test_summary_defaults_to_no_title_when_missing(self, client, mock_service):
        mock_service.events().list().execute.return_value = {
            "items": [{"id": "e1"}]  # no "summary" key
        }
        events = client.get_events_on_date("20260301")
        assert events == [{"id": "e1", "summary": "(No title)"}]
```

- [ ] **Step 2: Run the new tests to confirm they fail**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_calendar_client.py::TestGetEventsOnDate --tb=short -q
```

Expected: `AttributeError: 'GoogleCalendarClient' object has no attribute 'get_events_on_date'`

- [ ] **Step 3: Implement `get_events_on_date` and refactor `has_event_on_date`**

In `organist_bot/integrations/calendar_client.py`, replace the entire `has_event_on_date` method (lines 84–121) with:

```python
def get_events_on_date(self, date_str: str) -> list[dict]:
    """Return events on the given date (YYYYMMDD) as [{id, summary}] dicts.

    Returns [] on any API error (fail-open — don't silently drop gigs).
    """
    t0 = time.perf_counter()
    try:
        dt = datetime.datetime.strptime(date_str, "%Y%m%d").date()
        time_min = datetime.datetime.combine(dt, datetime.time.min).isoformat() + "Z"
        time_max = datetime.datetime.combine(dt, datetime.time.max).isoformat() + "Z"

        result = (
            self._service.events()
            .list(
                calendarId=self.calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
            )
            .execute()
        )

        events = [
            {"id": item.get("id", ""), "summary": item.get("summary", "(No title)")}
            for item in result.get("items", [])
        ]
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        logger.debug(
            "Calendar check complete",
            extra={"date": date_str, "event_count": len(events), "elapsed_ms": elapsed_ms},
        )
        return events

    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        logger.warning(
            "Calendar check failed — failing open",
            extra={"date": date_str, "error": str(exc), "elapsed_ms": elapsed_ms},
        )
        alert.send_alert(f"⚠️ Google Calendar API error (CalendarFilter query): {exc}")
        return []

def has_event_on_date(self, date_str: str) -> bool:
    """Return True if there is at least one event on the given date (YYYYMMDD).

    Fails open — returns False (don't block the gig) if the API call fails.
    """
    return bool(self.get_events_on_date(date_str))
```

- [ ] **Step 4: Update `test_main.py` to mock `get_events_on_date`**

In `tests/test_main.py`, find line 367:

```python
mock_cal_client.has_event_on_date.return_value = True  # date is booked
```

Replace it with (an "Unavailable" block — silent reject, no alert):

```python
mock_cal_client.get_events_on_date.return_value = [
    {"id": "b1", "summary": "Unavailable"}
]
```

- [ ] **Step 5: Run all tests to confirm everything passes**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_calendar_client.py tests/test_main.py --tb=short -q
```

Expected: all pass with no failures.

- [ ] **Step 6: Commit**

```bash
git add organist_bot/integrations/calendar_client.py \
        tests/test_calendar_client.py \
        tests/test_main.py
git commit -m "feat: add get_events_on_date to GoogleCalendarClient"
```

---

### Task 2: Update `CalendarFilter` to detect and alert on competing gigs

**Files:**
- Modify: `organist_bot/filters.py` (lines 447–461 — `CalendarFilter.__call__`)
- Modify: `tests/test_filters.py` (add `TestCalendarFilterCompeting` class)

---

- [ ] **Step 1: Write the failing tests**

Add this import to the top of `tests/test_filters.py` if not already present (check first):

```python
from unittest.mock import MagicMock, patch
```

Then add this class to `tests/test_filters.py`, after any existing `CalendarFilter`-related tests (search for `CalendarFilter` to find the right location, or add near the end of the filter tests):

```python
# ── CalendarFilter competing gig detection ────────────────────────────────────


class TestCalendarFilterCompeting:
    def _make_filter(self, events: list[dict]) -> CalendarFilter:
        client = MagicMock()
        client.get_events_on_date.return_value = events
        return CalendarFilter(client)

    def test_no_events_passes_gig(self):
        f = self._make_filter([])
        gig = make_gig(date="Sunday, 15 March 2026")
        with patch("organist_bot.filters.alert") as mock_alert:
            assert f(gig) is True
        mock_alert.send_alert.assert_not_called()

    def test_unavailable_only_silent_reject_no_alert(self):
        f = self._make_filter([{"id": "b1", "summary": "Unavailable"}])
        gig = make_gig(date="Sunday, 15 March 2026")
        with patch("organist_bot.filters.alert") as mock_alert:
            assert f(gig) is False
        mock_alert.send_alert.assert_not_called()

    def test_real_event_rejects_and_sends_alert(self):
        f = self._make_filter([{"id": "e1", "summary": "Evensong — St Mary's"}])
        gig = make_gig(
            date="Sunday, 15 March 2026",
            fee="£80",
            header="Sunday Service",
            organisation="All Saints Church",
            link="https://organistsonline.org/gig/99",
        )
        with patch("organist_bot.filters.alert") as mock_alert:
            assert f(gig) is False
        mock_alert.send_alert.assert_called_once()
        msg = mock_alert.send_alert.call_args.args[0]
        assert "Sunday Service" in msg
        assert "All Saints Church" in msg
        assert "£80" in msg
        assert "https://organistsonline.org/gig/99" in msg
        assert "Evensong — St Mary's" in msg
        assert "Unavailable" not in msg

    def test_mixed_events_alerts_only_real_events(self):
        events = [
            {"id": "b1", "summary": "Unavailable"},
            {"id": "e1", "summary": "Matins — St John's"},
        ]
        f = self._make_filter(events)
        gig = make_gig(date="Sunday, 15 March 2026")
        with patch("organist_bot.filters.alert") as mock_alert:
            assert f(gig) is False
        mock_alert.send_alert.assert_called_once()
        msg = mock_alert.send_alert.call_args.args[0]
        assert "Matins — St John's" in msg
        assert "Unavailable" not in msg
```

- [ ] **Step 2: Run the new tests to confirm they fail**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_filters.py::TestCalendarFilterCompeting --tb=short -q
```

Expected: failures — `CalendarFilter` still calls `has_event_on_date` and has no alert logic.

- [ ] **Step 3: Implement the updated `CalendarFilter`**

In `organist_bot/filters.py`, replace the `CalendarFilter` class (lines 432–461) with:

```python
class CalendarFilter:
    """Reject gigs whose date already has an event in Google Calendar.

    Distinguishes between 'Unavailable' blocking events (silent reject) and
    real confirmed gigs (reject + Telegram alert with competing gig details).

    Fails open in two cases:
      - The gig date cannot be parsed  → pass (can't judge)
      - The calendar API call fails    → pass (don't silently drop gigs)

    Args:
        client: A GoogleCalendarClient instance.
    """

    def __init__(self, client):
        self._client = client

    def __call__(self, gig: Gig) -> bool:
        normalized = normalize_to_yyyymmdd(gig.date)
        if normalized is None:
            return True  # Can't determine date — allow through

        events = self._client.get_events_on_date(normalized)
        if not events:
            return True

        competing = [e for e in events if e["summary"] != "Unavailable"]
        if competing:
            self._alert_competing(gig, competing)
        else:
            logger.debug(
                "CalendarFilter: date already busy — rejecting",
                extra={"header": gig.header, "date": gig.date},
            )
        return False

    def _alert_competing(self, gig: Gig, competing: list[dict]) -> None:
        org_part = f" — {gig.organisation}" if gig.organisation else ""
        fee_part = f"\nFee:      {gig.fee}" if gig.fee else ""
        conflicts = "\n".join(f"  • {e['summary']}" for e in competing)
        msg = (
            "⚠️ Competing gig — date already booked\n\n"
            f"New gig:  {gig.header}{org_part}\n"
            f"Date:     {gig.date}"
            f"{fee_part}\n"
            f"URL:      {gig.link}\n\n"
            f"Conflicts with:\n{conflicts}"
        )
        logger.info(
            "CalendarFilter: competing gig detected — alerting",
            extra={
                "header": gig.header,
                "date": gig.date,
                "competing": [e["summary"] for e in competing],
            },
        )
        alert.send_alert(msg)

    def __repr__(self):
        return "CalendarFilter()"
```

- [ ] **Step 4: Run the new tests to confirm they pass**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_filters.py::TestCalendarFilterCompeting --tb=short -q
```

Expected: 4 passed.

- [ ] **Step 5: Run the full test suite**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest --tb=short -q
```

Expected: all tests pass. If any existing test on `CalendarFilter` or `has_event_on_date` fails because it mocks the old interface, update it to use `get_events_on_date` the same way `test_main.py` was updated in Task 1, Step 4.

- [ ] **Step 6: Commit**

```bash
git add organist_bot/filters.py tests/test_filters.py
git commit -m "feat: alert on competing gigs in CalendarFilter"
```
