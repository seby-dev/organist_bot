"""Tests for gig_agent._execute_tool two-phase add_gig behaviour."""

import json
from unittest.mock import MagicMock, patch

import pytest

from organist_bot.integrations.gig_agent import _execute_tool

_FULL_INPUT = {
    "confirmed": False,
    "header": "Sunday Service",
    "organisation": "St Mary's",
    "locality": "Oxford",
    "date": "Sunday 1st June 2025",
    "time": "10:30am",
    "fee": "£150",
}


class TestExecuteToolAddGigPreview:
    @pytest.mark.asyncio
    async def test_returns_summary_containing_gig_fields(self):
        """confirmed=false returns a plain-text summary with all field values."""
        result = await _execute_tool("add_gig", _FULL_INPUT)
        assert "Sunday Service" in result
        assert "St Mary's" in result
        assert "Oxford" in result
        assert "Sunday 1st June 2025" in result
        assert "10:30am" in result
        assert "£150" in result

    @pytest.mark.asyncio
    async def test_does_not_write_to_calendar(self):
        """confirmed=false must never touch the calendar client."""
        with patch("organist_bot.integrations.gig_agent._make_calendar_client") as mock_factory:
            await _execute_tool("add_gig", _FULL_INPUT)
        mock_factory.assert_not_called()

    @pytest.mark.asyncio
    async def test_result_does_not_contain_result_key(self):
        """confirmed=false must not return a JSON 'result' key (that key signals done)."""
        result = await _execute_tool("add_gig", _FULL_INPUT)
        assert '"result"' not in result


class TestExecuteToolAddGigConfirmed:
    @pytest.mark.asyncio
    async def test_writes_to_calendar_and_returns_result(self):
        """confirmed=true calls calendar and returns JSON with 'result'."""
        input_data = {**_FULL_INPUT, "confirmed": True}
        with patch("organist_bot.integrations.gig_agent._make_calendar_client") as mock_factory:
            mock_cal = MagicMock()
            mock_cal.add_gig.return_value = "evt_abc123"
            mock_factory.return_value = mock_cal

            result = await _execute_tool("add_gig", input_data)

        data = json.loads(result)
        assert "result" in data
        assert "evt_abc123" in data["result"]
        mock_cal.add_gig.assert_called_once()
        gig_arg = mock_cal.add_gig.call_args[0][0]
        assert gig_arg.header == "Sunday Service"

    @pytest.mark.asyncio
    async def test_no_calendar_config_returns_error(self):
        """confirmed=true with no calendar configured returns error JSON."""
        input_data = {**_FULL_INPUT, "confirmed": True}
        with patch(
            "organist_bot.integrations.gig_agent._make_calendar_client",
            return_value=None,
        ):
            result = await _execute_tool("add_gig", input_data)

        data = json.loads(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_calendar_exception_returns_error(self):
        """confirmed=true when calendar.add_gig raises returns error JSON."""
        input_data = {**_FULL_INPUT, "confirmed": True}
        with patch("organist_bot.integrations.gig_agent._make_calendar_client") as mock_factory:
            mock_cal = MagicMock()
            mock_cal.add_gig.side_effect = RuntimeError("calendar down")
            mock_factory.return_value = mock_cal

            result = await _execute_tool("add_gig", input_data)

        data = json.loads(result)
        assert "error" in data
        assert "calendar down" in data["error"]


class TestExecuteToolAddGigAutoUnavailable:
    @pytest.mark.asyncio
    async def test_adds_date_to_unavailable_on_success(self):
        """confirmed=true after calendar write adds YYYY-MM-DD date to unavailable_periods."""
        input_data = {**_FULL_INPUT, "confirmed": True}
        # "Sunday 1st June 2025" should normalize to 20250601 → 2025-06-01
        with (
            patch("organist_bot.integrations.gig_agent._make_calendar_client") as mock_factory,
            patch("organist_bot.integrations.gig_agent.filter_store") as mock_fs,
        ):
            mock_cal = MagicMock()
            mock_cal.add_gig.return_value = "evt_xyz"
            mock_factory.return_value = mock_cal
            await _execute_tool("add_gig", input_data)
        mock_fs.add_period.assert_called_once_with("unavailable_periods", "2025-06-01")

    @pytest.mark.asyncio
    async def test_calendar_failure_does_not_call_add_period(self):
        """If calendar write fails, unavailable is not touched."""
        input_data = {**_FULL_INPUT, "confirmed": True}
        with (
            patch("organist_bot.integrations.gig_agent._make_calendar_client") as mock_factory,
            patch("organist_bot.integrations.gig_agent.filter_store") as mock_fs,
        ):
            mock_cal = MagicMock()
            mock_cal.add_gig.side_effect = RuntimeError("down")
            mock_factory.return_value = mock_cal
            await _execute_tool("add_gig", input_data)
        mock_fs.add_period.assert_not_called()

    @pytest.mark.asyncio
    async def test_unparseable_date_does_not_raise(self):
        """If date can't be parsed, calendar write still succeeds and no error is returned."""
        input_data = {**_FULL_INPUT, "confirmed": True, "date": "sometime in June"}
        with (
            patch("organist_bot.integrations.gig_agent._make_calendar_client") as mock_factory,
            patch("organist_bot.integrations.gig_agent.filter_store") as mock_fs,
        ):
            mock_cal = MagicMock()
            mock_cal.add_gig.return_value = "evt_abc"
            mock_factory.return_value = mock_cal
            result = await _execute_tool("add_gig", input_data)
        data = json.loads(result)
        assert "result" in data  # calendar write succeeded
        mock_fs.add_period.assert_not_called()  # but unavailable not touched
