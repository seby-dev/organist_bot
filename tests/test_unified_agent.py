"""Tests for unified_agent._execute_tool and supporting utilities."""

import datetime
import json
from unittest.mock import MagicMock, patch

import pytest

from organist_bot.integrations.unified_agent import (
    _execute_tool,
    _last_gig_listing,
)

CHAT_ID = 42

_GIG_INPUT_BASE = {
    "confirmed": False,
    "header": "Sunday Service",
    "organisation": "St Mary's",
    "locality": "Oxford",
    "date": "Sunday 1st June 2025",
    "time": "10:30am",
    "fee": "£150",
}


# ── add_gig (confirmed=false) ─────────────────────────────────────────────────


class TestAddGigPreview:
    @pytest.mark.asyncio
    async def test_returns_summary_with_all_fields(self):
        result = await _execute_tool("add_gig", _GIG_INPUT_BASE, CHAT_ID)
        for value in [
            "Sunday Service",
            "St Mary's",
            "Oxford",
            "Sunday 1st June 2025",
            "10:30am",
            "£150",
        ]:
            assert value in result

    @pytest.mark.asyncio
    async def test_does_not_touch_calendar(self):
        with patch("organist_bot.integrations.unified_agent._make_calendar_client") as mock_factory:
            await _execute_tool("add_gig", _GIG_INPUT_BASE, CHAT_ID)
        mock_factory.assert_not_called()

    @pytest.mark.asyncio
    async def test_result_key_absent(self):
        result = await _execute_tool("add_gig", _GIG_INPUT_BASE, CHAT_ID)
        assert '"result"' not in result


# ── add_gig (confirmed=true) ──────────────────────────────────────────────────


class TestAddGigConfirmed:
    @pytest.mark.asyncio
    async def test_writes_to_calendar_and_returns_result(self):
        input_data = {**_GIG_INPUT_BASE, "confirmed": True}
        with patch("organist_bot.integrations.unified_agent._make_calendar_client") as mock_factory:
            mock_cal = MagicMock()
            mock_cal.add_gig.return_value = "evt_abc123"
            mock_factory.return_value = mock_cal
            result = await _execute_tool("add_gig", input_data, CHAT_ID)
        data = json.loads(result)
        assert "result" in data
        assert "evt_abc123" in data["result"]

    @pytest.mark.asyncio
    async def test_no_calendar_config_returns_error(self):
        input_data = {**_GIG_INPUT_BASE, "confirmed": True}
        with patch(
            "organist_bot.integrations.unified_agent._make_calendar_client", return_value=None
        ):
            result = await _execute_tool("add_gig", input_data, CHAT_ID)
        assert "error" in json.loads(result)

    @pytest.mark.asyncio
    async def test_calendar_exception_returns_error(self):
        input_data = {**_GIG_INPUT_BASE, "confirmed": True}
        with patch("organist_bot.integrations.unified_agent._make_calendar_client") as mock_factory:
            mock_cal = MagicMock()
            mock_cal.add_gig.side_effect = RuntimeError("calendar down")
            mock_factory.return_value = mock_cal
            result = await _execute_tool("add_gig", input_data, CHAT_ID)
        data = json.loads(result)
        assert "error" in data
        assert "calendar down" in data["error"]


# ── add_gig auto-unavailable ──────────────────────────────────────────────────


class TestAddGigAutoUnavailable:
    @pytest.mark.asyncio
    async def test_adds_date_to_unavailable_on_success(self):
        input_data = {**_GIG_INPUT_BASE, "confirmed": True}
        with (
            patch("organist_bot.integrations.unified_agent._make_calendar_client") as mock_factory,
            patch("organist_bot.integrations.unified_agent.filter_store") as mock_fs,
        ):
            mock_cal = MagicMock()
            mock_cal.add_gig.return_value = "evt_xyz"
            mock_factory.return_value = mock_cal
            await _execute_tool("add_gig", input_data, CHAT_ID)
        mock_fs.add_period.assert_called_once_with("unavailable_periods", "2025-06-01")

    @pytest.mark.asyncio
    async def test_calendar_failure_does_not_call_add_period(self):
        input_data = {**_GIG_INPUT_BASE, "confirmed": True}
        with (
            patch("organist_bot.integrations.unified_agent._make_calendar_client") as mock_factory,
            patch("organist_bot.integrations.unified_agent.filter_store") as mock_fs,
        ):
            mock_cal = MagicMock()
            mock_cal.add_gig.side_effect = RuntimeError("down")
            mock_factory.return_value = mock_cal
            await _execute_tool("add_gig", input_data, CHAT_ID)
        mock_fs.add_period.assert_not_called()

    @pytest.mark.asyncio
    async def test_unparseable_date_does_not_raise(self):
        input_data = {**_GIG_INPUT_BASE, "confirmed": True, "date": "sometime in June"}
        with (
            patch("organist_bot.integrations.unified_agent._make_calendar_client") as mock_factory,
            patch("organist_bot.integrations.unified_agent.filter_store") as mock_fs,
        ):
            mock_cal = MagicMock()
            mock_cal.add_gig.return_value = "evt_abc"
            mock_factory.return_value = mock_cal
            result = await _execute_tool("add_gig", input_data, CHAT_ID)
        assert "result" in json.loads(result)
        mock_fs.add_period.assert_not_called()


# ── list_upcoming_gigs ────────────────────────────────────────────────────────


def _make_event(n: int) -> dict:
    return {
        "id": f"evt{n}",
        "summary": f"Sunday Service {n}",
        "start_dt": datetime.datetime(2026, 6, n, 10, 30, tzinfo=datetime.UTC),
        "date_str": f"2026-06-0{n}",
    }


class TestListUpcomingGigs:
    @pytest.mark.asyncio
    async def test_returns_numbered_gig_list(self):
        events = [_make_event(1), _make_event(2)]
        with patch("organist_bot.integrations.unified_agent._make_calendar_client") as mock_factory:
            mock_cal = MagicMock()
            mock_cal.list_upcoming_events.return_value = events
            mock_factory.return_value = mock_cal
            result = await _execute_tool("list_upcoming_gigs", {}, CHAT_ID)
        assert "Sunday Service 1" in result
        assert "Sunday Service 2" in result

    @pytest.mark.asyncio
    async def test_stores_events_in_last_gig_listing(self):
        events = [_make_event(1)]
        _last_gig_listing.pop(CHAT_ID, None)
        with patch("organist_bot.integrations.unified_agent._make_calendar_client") as mock_factory:
            mock_cal = MagicMock()
            mock_cal.list_upcoming_events.return_value = events
            mock_factory.return_value = mock_cal
            await _execute_tool("list_upcoming_gigs", {}, CHAT_ID)
        assert _last_gig_listing[CHAT_ID] == events

    @pytest.mark.asyncio
    async def test_no_calendar_returns_error(self):
        with patch(
            "organist_bot.integrations.unified_agent._make_calendar_client", return_value=None
        ):
            result = await _execute_tool("list_upcoming_gigs", {}, CHAT_ID)
        assert "error" in result.lower() or "not configured" in result.lower()

    @pytest.mark.asyncio
    async def test_empty_calendar_says_no_gigs(self):
        with patch("organist_bot.integrations.unified_agent._make_calendar_client") as mock_factory:
            mock_cal = MagicMock()
            mock_cal.list_upcoming_events.return_value = []
            mock_factory.return_value = mock_cal
            result = await _execute_tool("list_upcoming_gigs", {}, CHAT_ID)
        assert "no" in result.lower() or "0" in result


# ── delete_gig ────────────────────────────────────────────────────────────────


class TestDeleteGig:
    @pytest.fixture(autouse=True)
    def seed_listing(self):
        _last_gig_listing[CHAT_ID] = [_make_event(1), _make_event(2)]
        yield
        _last_gig_listing.pop(CHAT_ID, None)

    @pytest.mark.asyncio
    async def test_deletes_event_and_returns_confirmation(self):
        with (
            patch("organist_bot.integrations.unified_agent._make_calendar_client") as mock_factory,
            patch("organist_bot.integrations.unified_agent.filter_store"),
        ):
            mock_cal = MagicMock()
            mock_factory.return_value = mock_cal
            result = await _execute_tool("delete_gig", {"number": 1}, CHAT_ID)
        mock_cal.delete_event.assert_called_once_with("evt1")
        assert "Sunday Service 1" in result or "result" in result.lower()

    @pytest.mark.asyncio
    async def test_removes_date_from_unavailable(self):
        with (
            patch("organist_bot.integrations.unified_agent._make_calendar_client") as mock_factory,
            patch("organist_bot.integrations.unified_agent.filter_store") as mock_fs,
        ):
            mock_cal = MagicMock()
            mock_factory.return_value = mock_cal
            await _execute_tool("delete_gig", {"number": 1}, CHAT_ID)
        mock_fs.remove_period.assert_called_once_with("unavailable_periods", "2026-06-01")

    @pytest.mark.asyncio
    async def test_no_listing_returns_error(self):
        _last_gig_listing.pop(CHAT_ID, None)
        result = await _execute_tool("delete_gig", {"number": 1}, CHAT_ID)
        data = json.loads(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_out_of_range_returns_error(self):
        result = await _execute_tool("delete_gig", {"number": 99}, CHAT_ID)
        data = json.loads(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_no_calendar_config_returns_error(self):
        with patch(
            "organist_bot.integrations.unified_agent._make_calendar_client", return_value=None
        ):
            result = await _execute_tool("delete_gig", {"number": 1}, CHAT_ID)
        data = json.loads(result)
        assert "error" in data
