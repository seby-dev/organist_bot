# tests/test_calendar_client.py
"""Tests for GoogleCalendarClient."""

import datetime as dt
from unittest.mock import MagicMock, patch

import pytest

from organist_bot.integrations.calendar_client import GoogleCalendarClient, _parse_period_dates
from organist_bot.models import Gig

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_service():
    return MagicMock()


@pytest.fixture
def client(mock_service):
    """GoogleCalendarClient with all Google API calls mocked out."""
    with (
        patch(
            "organist_bot.integrations.calendar_client.service_account.Credentials.from_service_account_file"
        ),
        patch("organist_bot.integrations.calendar_client.build", return_value=mock_service),
    ):
        return GoogleCalendarClient(credentials_file="fake.json", calendar_id="cal@test.com")


def _make_gig(**overrides) -> Gig:
    defaults = dict(
        header="Sunday Service",
        organisation="St. Paul's Church",
        locality="London",
        date="Sunday, March 1, 2026",
        time="10:00 AM",
        fee="£120",
        link="https://organistsonline.org/required/test",
    )
    defaults.update(overrides)
    return Gig(**defaults)


# ── has_event_on_date ─────────────────────────────────────────────────────────


class TestHasEventOnDate:
    def test_returns_true_when_event_exists(self, client, mock_service):
        mock_service.events().list().execute.return_value = {
            "items": [{"id": "event1", "summary": "Existing Event"}]
        }
        assert client.has_event_on_date("20260301") is True

    def test_returns_false_when_no_events(self, client, mock_service):
        mock_service.events().list().execute.return_value = {"items": []}
        assert client.has_event_on_date("20260301") is False

    def test_returns_false_when_items_key_missing(self, client, mock_service):
        mock_service.events().list().execute.return_value = {}
        assert client.has_event_on_date("20260301") is False

    def test_returns_false_on_api_exception(self, client, mock_service):
        """Fail-open: API errors must not block the gig."""
        mock_service.events().list().execute.side_effect = Exception("API unavailable")
        assert client.has_event_on_date("20260301") is False

    def test_returns_false_on_invalid_date_string(self, client, mock_service):
        """Fail-open: unparseable date should not raise."""
        mock_service.events().list().execute.side_effect = ValueError("bad date")
        assert client.has_event_on_date("not-a-date") is False

    def test_multiple_events_still_returns_true(self, client, mock_service):
        mock_service.events().list().execute.return_value = {
            "items": [{"id": "e1"}, {"id": "e2"}, {"id": "e3"}]
        }
        assert client.has_event_on_date("20260301") is True


# ── add_gig ───────────────────────────────────────────────────────────────────


class TestAddGig:
    def test_creates_event_and_returns_id(self, client, mock_service):
        mock_service.events().insert().execute.return_value = {"id": "new_event_123"}
        gig = _make_gig()
        result = client.add_gig(gig)
        assert result == "new_event_123"

    def test_event_has_correct_summary(self, client, mock_service):
        mock_service.events().insert().execute.return_value = {"id": "x"}
        gig = _make_gig(header="Sunday Service", organisation="St Paul's")
        client.add_gig(gig)
        call_kwargs = mock_service.events().insert.call_args
        body = call_kwargs[1]["body"]
        assert "Sunday Service" in body["summary"]
        assert "St Paul's" in body["summary"]

    def test_event_timezone_is_london(self, client, mock_service):
        mock_service.events().insert().execute.return_value = {"id": "x"}
        client.add_gig(_make_gig())
        body = mock_service.events().insert.call_args[1]["body"]
        assert body["start"]["timeZone"] == "Europe/London"
        assert body["end"]["timeZone"] == "Europe/London"

    def test_event_end_is_one_hour_after_start(self, client, mock_service):
        mock_service.events().insert().execute.return_value = {"id": "x"}
        from datetime import datetime

        client.add_gig(_make_gig(time="10:00 AM", date="Sunday, March 1, 2026"))
        body = mock_service.events().insert.call_args[1]["body"]
        start = datetime.fromisoformat(body["start"]["dateTime"])
        end = datetime.fromisoformat(body["end"]["dateTime"])
        assert (end - start).total_seconds() == 3600

    def test_raises_value_error_for_unparseable_date(self, client, mock_service):
        gig = _make_gig(date="not a date at all")
        with pytest.raises(ValueError, match="Cannot parse gig date"):
            client.add_gig(gig)

    def test_raises_value_error_for_unparseable_time(self, client, mock_service):
        gig = _make_gig(time="whenever")
        with pytest.raises(ValueError, match="Cannot parse gig time"):
            client.add_gig(gig)

    def test_raises_when_api_insert_fails(self, client, mock_service):
        mock_service.events().insert().execute.side_effect = Exception("Quota exceeded")
        with pytest.raises(Exception, match="Quota exceeded"):
            client.add_gig(_make_gig())

    def test_event_description_contains_gig_details(self, client, mock_service):
        mock_service.events().insert().execute.return_value = {"id": "x"}
        gig = _make_gig(fee="£120", contact="John Smith", email="john@church.org")
        client.add_gig(gig)
        body = mock_service.events().insert.call_args[1]["body"]
        assert "£120" in body["description"]
        assert "John Smith" in body["description"]
        assert "john@church.org" in body["description"]

    def test_calendar_id_passed_to_insert(self, client, mock_service):
        mock_service.events().insert().execute.return_value = {"id": "x"}
        client.add_gig(_make_gig())
        call_kwargs = mock_service.events().insert.call_args
        assert call_kwargs[1]["calendarId"] == "cal@test.com"


# ── elapsed_ms logging ────────────────────────────────────────────────────────


class TestElapsedMsLogging:
    """Verify that both Calendar API methods emit elapsed_ms in their log records."""

    def test_has_event_on_date_logs_elapsed_ms_on_success(self, client, mock_service, caplog):
        """has_event_on_date() must include elapsed_ms in the 'Calendar check complete' DEBUG record."""
        import logging

        mock_service.events().list().execute.return_value = {"items": []}
        with caplog.at_level(logging.DEBUG, logger="organist_bot.integrations.calendar_client"):
            client.has_event_on_date("20260301")

        record = next(
            (r for r in caplog.records if r.message == "Calendar check complete"),
            None,
        )
        assert record is not None, "Expected 'Calendar check complete' log record"
        assert isinstance(record.elapsed_ms, int)
        assert record.elapsed_ms >= 0

    def test_has_event_on_date_logs_elapsed_ms_on_failure(self, client, mock_service, caplog):
        """has_event_on_date() must include elapsed_ms in the WARNING record when the API fails."""
        import logging

        mock_service.events().list().execute.side_effect = Exception("quota exceeded")
        with caplog.at_level(logging.WARNING, logger="organist_bot.integrations.calendar_client"):
            client.has_event_on_date("20260301")

        record = next(
            (r for r in caplog.records if "Calendar check failed" in r.message),
            None,
        )
        assert record is not None, "Expected 'Calendar check failed' log record"
        assert isinstance(record.elapsed_ms, int)
        assert record.elapsed_ms >= 0

    def test_add_gig_logs_elapsed_ms_on_success(self, client, mock_service, caplog):
        """add_gig() must include elapsed_ms in the 'Gig added to Google Calendar' INFO record."""
        import logging

        mock_service.events().insert().execute.return_value = {"id": "ev123"}
        with caplog.at_level(logging.INFO, logger="organist_bot.integrations.calendar_client"):
            client.add_gig(_make_gig())

        record = next(
            (r for r in caplog.records if r.message == "Gig added to Google Calendar"),
            None,
        )
        assert record is not None, "Expected 'Gig added to Google Calendar' log record"
        assert isinstance(record.elapsed_ms, int)
        assert record.elapsed_ms >= 0

    def test_list_upcoming_events_logs_elapsed_ms_on_success(self, client, mock_service, caplog):
        import logging

        mock_service.events().list().execute.return_value = {"items": []}
        with caplog.at_level(logging.DEBUG, logger="organist_bot.integrations.calendar_client"):
            client.list_upcoming_events()
        record = next(
            (r for r in caplog.records if r.message == "list_upcoming_events complete"),
            None,
        )
        assert record is not None, "Expected 'list_upcoming_events complete' log record"
        assert isinstance(record.elapsed_ms, int)
        assert record.elapsed_ms >= 0

    def test_list_upcoming_events_logs_elapsed_ms_on_failure(self, client, mock_service, caplog):
        import logging

        mock_service.events().list().execute.side_effect = Exception("API error")
        with caplog.at_level(logging.WARNING, logger="organist_bot.integrations.calendar_client"):
            client.list_upcoming_events()
        record = next(
            (r for r in caplog.records if "list_upcoming_events failed" in r.message),
            None,
        )
        assert record is not None
        assert isinstance(record.elapsed_ms, int)

    def test_delete_event_logs_elapsed_ms_on_success(self, client, mock_service, caplog):
        import logging

        mock_service.events().delete().execute.return_value = None
        with caplog.at_level(logging.INFO, logger="organist_bot.integrations.calendar_client"):
            client.delete_event("evt_abc")
        record = next(
            (r for r in caplog.records if r.message == "Calendar event deleted"),
            None,
        )
        assert record is not None
        assert isinstance(record.elapsed_ms, int)


# ── list_upcoming_events ──────────────────────────────────────────────────────


class TestListUpcomingEvents:
    def test_returns_list_of_event_dicts(self, client, mock_service):
        mock_service.events().list().execute.return_value = {
            "items": [
                {
                    "id": "evt1",
                    "summary": "Sunday Service",
                    "start": {"dateTime": "2026-06-01T10:30:00+01:00"},
                },
                {
                    "id": "evt2",
                    "summary": "Evensong",
                    "start": {"dateTime": "2026-06-14T18:00:00+01:00"},
                },
            ]
        }
        events = client.list_upcoming_events()
        assert len(events) == 2
        assert events[0]["id"] == "evt1"
        assert events[0]["summary"] == "Sunday Service"
        assert events[0]["date_str"] == "2026-06-01"
        assert isinstance(events[0]["start_dt"], dt.datetime)

    def test_returns_empty_list_when_no_events(self, client, mock_service):
        mock_service.events().list().execute.return_value = {"items": []}
        assert client.list_upcoming_events() == []

    def test_returns_empty_list_on_api_error(self, client, mock_service):
        mock_service.events().list().execute.side_effect = Exception("API down")
        assert client.list_upcoming_events() == []

    def test_respects_max_results(self, client, mock_service):
        mock_service.events().list().execute.return_value = {"items": []}
        client.list_upcoming_events(max_results=5)
        call_kwargs = mock_service.events().list.call_args[1]
        assert call_kwargs["maxResults"] == 5

    def test_handles_all_day_event(self, client, mock_service):
        mock_service.events().list().execute.return_value = {
            "items": [{"id": "a1", "summary": "Holiday", "start": {"date": "2026-12-25"}}]
        }
        events = client.list_upcoming_events()
        assert events[0]["date_str"] == "2026-12-25"
        assert isinstance(events[0]["start_dt"], dt.datetime)

    def test_handles_utc_z_suffix_datetime(self, client, mock_service):
        """Google Calendar commonly returns UTC timestamps with Z suffix."""
        mock_service.events().list().execute.return_value = {
            "items": [
                {"id": "u1", "summary": "Evensong", "start": {"dateTime": "2026-07-01T09:00:00Z"}}
            ]
        }
        events = client.list_upcoming_events()
        assert events[0]["date_str"] == "2026-07-01"
        assert events[0]["start_dt"].tzinfo is not None

    def test_events_missing_summary_use_no_title(self, client, mock_service):
        mock_service.events().list().execute.return_value = {
            "items": [{"id": "x1", "start": {"dateTime": "2026-07-01T10:00:00Z"}}]
        }
        events = client.list_upcoming_events()
        assert events[0]["summary"] == "(No title)"


# ── delete_event ──────────────────────────────────────────────────────────────


class TestDeleteEvent:
    def test_calls_delete_with_correct_args(self, client, mock_service):
        mock_service.events().delete().execute.return_value = None
        client.delete_event("evt_123")
        call_kwargs = mock_service.events().delete.call_args[1]
        assert call_kwargs["calendarId"] == "cal@test.com"
        assert call_kwargs["eventId"] == "evt_123"

    def test_raises_on_api_error(self, client, mock_service):
        mock_service.events().delete().execute.side_effect = Exception("Not found")
        with pytest.raises(Exception, match="Not found"):
            client.delete_event("nonexistent_id")


# ── update_event ──────────────────────────────────────────────────────────────


class TestUpdateEvent:
    def test_patches_summary(self, client, mock_service):
        mock_service.events().patch().execute.return_value = {}
        client.update_event("evt_1", summary="New Title")
        kwargs = mock_service.events().patch.call_args[1]
        assert kwargs["eventId"] == "evt_1"
        assert kwargs["body"]["summary"] == "New Title"

    def test_patches_start_and_end_time(self, client, mock_service):
        mock_service.events().patch().execute.return_value = {}
        import datetime as dt_mod

        start = dt_mod.datetime(2026, 6, 1, 11, 0, tzinfo=dt_mod.UTC)
        client.update_event("evt_1", start_dt=start)
        body = mock_service.events().patch.call_args[1]["body"]
        assert "start" in body
        assert "end" in body
        from datetime import datetime

        end_dt = datetime.fromisoformat(body["end"]["dateTime"])
        start_dt = datetime.fromisoformat(body["start"]["dateTime"])
        assert (end_dt - start_dt).total_seconds() == 3600

    def test_no_op_when_nothing_provided(self, client, mock_service):
        client.update_event("evt_1")
        mock_service.events().patch.assert_not_called()

    def test_raises_on_api_error(self, client, mock_service):
        mock_service.events().patch().execute.side_effect = Exception("Forbidden")
        with pytest.raises(Exception, match="Forbidden"):
            client.update_event("evt_1", summary="X")


# ── _parse_period_dates ───────────────────────────────────────────────────────


class TestParsePeriodDates:
    def test_single_date(self):
        result = _parse_period_dates("2026-12-25")
        assert result == (dt.date(2026, 12, 25), dt.date(2026, 12, 25))

    def test_range(self):
        result = _parse_period_dates("2026-12-01:2026-12-31")
        assert result == (dt.date(2026, 12, 1), dt.date(2026, 12, 31))

    def test_month_december(self):
        result = _parse_period_dates("2026-12")
        assert result == (dt.date(2026, 12, 1), dt.date(2026, 12, 31))

    def test_month_february_non_leap(self):
        result = _parse_period_dates("2026-02")
        assert result == (dt.date(2026, 2, 1), dt.date(2026, 2, 28))

    def test_invalid_returns_none(self):
        assert _parse_period_dates("not-a-date") is None

    def test_empty_returns_none(self):
        assert _parse_period_dates("") is None
