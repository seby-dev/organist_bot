"""Tests for gig_agent._execute_tool two-phase add_gig behaviour."""

import json
from unittest.mock import MagicMock, patch

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
    async def test_returns_summary_containing_gig_fields(self):
        """confirmed=false returns a plain-text summary with all field values."""
        result = await _execute_tool("add_gig", _FULL_INPUT)
        assert "Sunday Service" in result
        assert "St Mary's" in result
        assert "Oxford" in result
        assert "Sunday 1st June 2025" in result
        assert "10:30am" in result
        assert "£150" in result

    async def test_does_not_write_to_calendar(self):
        """confirmed=false must never touch the calendar client."""
        with patch("organist_bot.integrations.gig_agent._make_calendar_client") as mock_factory:
            await _execute_tool("add_gig", _FULL_INPUT)
        mock_factory.assert_not_called()

    async def test_result_does_not_contain_result_key(self):
        """confirmed=false must not return a JSON 'result' key (that key signals done)."""
        result = await _execute_tool("add_gig", _FULL_INPUT)
        assert '"result"' not in result


class TestExecuteToolAddGigConfirmed:
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

    async def test_no_calendar_config_returns_error(self):
        """confirmed=true with no calendar configured returns error JSON."""
        input_data = {
            "confirmed": True,
            "header": "Gig",
            "date": "2025-06-01",
            "time": "10am",
        }
        with patch(
            "organist_bot.integrations.gig_agent._make_calendar_client",
            return_value=None,
        ):
            result = await _execute_tool("add_gig", input_data)

        data = json.loads(result)
        assert "error" in data

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
