# tests/test_calendar_client.py
"""Tests for GoogleCalendarClient."""

from unittest.mock import MagicMock, patch

import pytest

from organist_bot.integrations.calendar_client import GoogleCalendarClient
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
