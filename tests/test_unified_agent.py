"""Tests for unified_agent._execute_tool and supporting utilities."""

import datetime
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from organist_bot.integrations.unified_agent import (
    TOOLS,
    UnifiedAgent,
    _execute_tool,
    _last_gig_listing,
    sync_calendar_blocks,
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


# ── fetch_gig_details ─────────────────────────────────────────────────────────


class TestFetchGigDetails:
    @pytest.mark.asyncio
    async def test_returns_merged_gig_fields(self):
        """Successful scrape returns merged basic + full details as JSON."""
        basic = {
            "header": "Sunday Service",
            "date": "2025-06-01",
            "link": "https://example.com/gig/1",
        }
        full = {"organisation": "St Mary's", "locality": "Oxford", "fee": "£150", "postcode": None}
        mock_scraper = MagicMock()
        mock_scraper.fetch.return_value = "<html/>"
        mock_scraper.extract_basic_from_detail.return_value = basic
        mock_scraper.extract_full_details.return_value = full
        mock_scraper.session.close = MagicMock()

        with patch("organist_bot.integrations.unified_agent.Scraper", return_value=mock_scraper):
            result = await _execute_tool(
                "fetch_gig_details", {"url": "https://example.com/gig/1"}, CHAT_ID
            )

        data = json.loads(result)
        assert data["header"] == "Sunday Service"
        assert data["organisation"] == "St Mary's"
        assert "postcode" not in data  # None values excluded

    @pytest.mark.asyncio
    async def test_scrape_exception_returns_error(self):
        """If scraping raises, returns JSON error without crashing."""
        mock_scraper = MagicMock()
        mock_scraper.fetch.side_effect = RuntimeError("network error")

        with patch("organist_bot.integrations.unified_agent.Scraper", return_value=mock_scraper):
            result = await _execute_tool(
                "fetch_gig_details", {"url": "https://example.com/bad"}, CHAT_ID
            )

        data = json.loads(result)
        assert "error" in data
        assert "network error" in data["error"]


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


# ── add_gig → application_store.upsert_accepted ───────────────────────────────


class TestAddGigApplicationStore:
    @pytest.mark.asyncio
    async def test_add_gig_url_match_updates_to_accepted(self):
        """When url is provided, upsert_accepted is called with that url."""
        input_data = {
            **_GIG_INPUT_BASE,
            "confirmed": True,
            "url": "https://organistsonline.org/gig/1",
        }
        with (
            patch("organist_bot.integrations.unified_agent._make_calendar_client") as mock_factory,
            patch("organist_bot.integrations.unified_agent.application_store") as mock_store,
        ):
            mock_cal = MagicMock()
            mock_cal.add_gig.return_value = "evt_abc"
            mock_factory.return_value = mock_cal
            await _execute_tool("add_gig", input_data, CHAT_ID)
        mock_store.upsert_accepted.assert_called_once_with(
            url="https://organistsonline.org/gig/1",
            header="Sunday Service",
            organisation="St Mary's",
            date="Sunday 1st June 2025",
            fee="£150",
            postcode="",
            time="10:30am",
        )

    @pytest.mark.asyncio
    async def test_add_gig_url_no_match_creates_accepted(self):
        """upsert_accepted is called with url even when no prior record exists."""
        input_data = {
            **_GIG_INPUT_BASE,
            "confirmed": True,
            "url": "https://organistsonline.org/gig/99",
        }
        with (
            patch("organist_bot.integrations.unified_agent._make_calendar_client") as mock_factory,
            patch("organist_bot.integrations.unified_agent.application_store") as mock_store,
        ):
            mock_cal = MagicMock()
            mock_cal.add_gig.return_value = "evt_abc"
            mock_factory.return_value = mock_cal
            await _execute_tool("add_gig", input_data, CHAT_ID)
        mock_store.upsert_accepted.assert_called_once_with(
            url="https://organistsonline.org/gig/99",
            header="Sunday Service",
            organisation="St Mary's",
            date="Sunday 1st June 2025",
            fee="£150",
            postcode="",
            time="10:30am",
        )

    @pytest.mark.asyncio
    async def test_add_gig_manual_entry_creates_accepted(self):
        """When no url is provided (manual entry), upsert_accepted is called with url=None."""
        input_data = {**_GIG_INPUT_BASE, "confirmed": True}  # no "url" key
        with (
            patch("organist_bot.integrations.unified_agent._make_calendar_client") as mock_factory,
            patch("organist_bot.integrations.unified_agent.application_store") as mock_store,
        ):
            mock_cal = MagicMock()
            mock_cal.add_gig.return_value = "evt_abc"
            mock_factory.return_value = mock_cal
            await _execute_tool("add_gig", input_data, CHAT_ID)
        mock_store.upsert_accepted.assert_called_once_with(
            url=None,
            header="Sunday Service",
            organisation="St Mary's",
            date="Sunday 1st June 2025",
            fee="£150",
            postcode="",
            time="10:30am",
        )


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

    @pytest.mark.asyncio
    async def test_events_sorted_earliest_first(self):
        late = _make_event(3)  # 3 Jun
        early = _make_event(1)  # 1 Jun
        mid = _make_event(2)  # 2 Jun
        events = [late, mid, early]  # intentionally out of order
        with patch("organist_bot.integrations.unified_agent._make_calendar_client") as mock_factory:
            mock_cal = MagicMock()
            mock_cal.list_upcoming_events.return_value = events
            mock_factory.return_value = mock_cal
            result = await _execute_tool("list_upcoming_gigs", {}, CHAT_ID)
        pos1 = result.index("Sunday Service 1")
        pos2 = result.index("Sunday Service 2")
        pos3 = result.index("Sunday Service 3")
        assert pos1 < pos2 < pos3

    @pytest.mark.asyncio
    async def test_unavailable_blocks_excluded(self):
        unavailable = {
            "id": "block1",
            "summary": "Unavailable",
            "start_dt": datetime.datetime(2026, 6, 2, 0, 0, tzinfo=datetime.UTC),
            "date_str": "2026-06-02",
        }
        events = [_make_event(1), unavailable, _make_event(3)]
        with patch("organist_bot.integrations.unified_agent._make_calendar_client") as mock_factory:
            mock_cal = MagicMock()
            mock_cal.list_upcoming_events.return_value = events
            mock_factory.return_value = mock_cal
            result = await _execute_tool("list_upcoming_gigs", {}, CHAT_ID)
        assert "Unavailable" not in result
        assert "Sunday Service 1" in result
        assert "Sunday Service 3" in result

    @pytest.mark.asyncio
    async def test_travel_buffers_excluded(self):
        travel_to = {
            "id": "tb1",
            "summary": "🚗 Travel to Sunday Service 2",
            "start_dt": datetime.datetime(2026, 6, 2, 9, 45, tzinfo=datetime.UTC),
            "date_str": "2026-06-02",
        }
        travel_from = {
            "id": "tb2",
            "summary": "🚗 Travel from Sunday Service 2",
            "start_dt": datetime.datetime(2026, 6, 2, 11, 30, tzinfo=datetime.UTC),
            "date_str": "2026-06-02",
        }
        events = [travel_to, _make_event(2), travel_from]
        with patch("organist_bot.integrations.unified_agent._make_calendar_client") as mock_factory:
            mock_cal = MagicMock()
            mock_cal.list_upcoming_events.return_value = events
            mock_factory.return_value = mock_cal
            result = await _execute_tool("list_upcoming_gigs", {}, CHAT_ID)
        assert "🚗" not in result
        assert "Travel to" not in result
        assert "Travel from" not in result
        assert "Sunday Service 2" in result

    @pytest.mark.asyncio
    async def test_overfetches_then_truncates_to_max_results(self):
        # 5 real gigs interleaved with travel buffers — caller asks for 3,
        # should still get exactly 3 real gigs (not 3 minus the buffers).
        events = []
        for n in (1, 2, 3, 4, 5):
            events.append(
                {
                    "id": f"tb-before-{n}",
                    "summary": f"🚗 Travel to Sunday Service {n}",
                    "start_dt": datetime.datetime(2026, 6, n, 9, 45, tzinfo=datetime.UTC),
                    "date_str": f"2026-06-0{n}",
                }
            )
            events.append(_make_event(n))
            events.append(
                {
                    "id": f"tb-after-{n}",
                    "summary": f"🚗 Travel from Sunday Service {n}",
                    "start_dt": datetime.datetime(2026, 6, n, 11, 30, tzinfo=datetime.UTC),
                    "date_str": f"2026-06-0{n}",
                }
            )
        with patch("organist_bot.integrations.unified_agent._make_calendar_client") as mock_factory:
            mock_cal = MagicMock()
            mock_cal.list_upcoming_events.return_value = events
            mock_factory.return_value = mock_cal
            result = await _execute_tool("list_upcoming_gigs", {"max_results": 3}, CHAT_ID)
        # Over-fetch: API call should request more than max_results
        called_count = mock_cal.list_upcoming_events.call_args.kwargs["max_results"]
        assert called_count > 3, f"expected over-fetch, got max_results={called_count}"
        # Truncate: only 3 gigs should appear in the rendered output and cache
        assert "Sunday Service 1" in result
        assert "Sunday Service 2" in result
        assert "Sunday Service 3" in result
        assert "Sunday Service 4" not in result
        assert "Sunday Service 5" not in result
        assert len(_last_gig_listing[CHAT_ID]) == 3

    @pytest.mark.asyncio
    async def test_gig_listing_includes_markdown_formatting(self):
        events = [_make_event(1)]
        with patch("organist_bot.integrations.unified_agent._make_calendar_client") as mock_factory:
            mock_cal = MagicMock()
            mock_cal.list_upcoming_events.return_value = events
            mock_factory.return_value = mock_cal
            result = await _execute_tool("list_upcoming_gigs", {}, CHAT_ID)
        data = json.loads(result)
        text = data["result"]
        assert "🎵" in text
        assert "*Sunday Service 1*" in text
        assert "Jun 2026" in text


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
        data = json.loads(result)
        assert "Sunday Service 1" in data["result"]

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
    async def test_listing_shrinks_after_delete(self):
        with (
            patch("organist_bot.integrations.unified_agent._make_calendar_client") as mock_factory,
            patch("organist_bot.integrations.unified_agent.filter_store"),
        ):
            mock_cal = MagicMock()
            mock_factory.return_value = mock_cal
            await _execute_tool("delete_gig", {"number": 1}, CHAT_ID)
        assert len(_last_gig_listing[CHAT_ID]) == 1
        assert _last_gig_listing[CHAT_ID][0]["id"] == "evt2"

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


# ── edit_gig ──────────────────────────────────────────────────────────────────


class TestEditGig:
    @pytest.fixture(autouse=True)
    def seed_listing(self):
        from organist_bot.integrations.unified_agent import _last_gig_listing

        _last_gig_listing[CHAT_ID] = [_make_event(1), _make_event(2)]
        yield
        _last_gig_listing.pop(CHAT_ID, None)

    @pytest.mark.asyncio
    async def test_edit_summary(self):
        with patch("organist_bot.integrations.unified_agent._make_calendar_client") as mock_factory:
            mock_cal = MagicMock()
            mock_factory.return_value = mock_cal
            result = await _execute_tool("edit_gig", {"number": 1, "summary": "New Title"}, CHAT_ID)
        mock_cal.update_event.assert_called_once()
        _, kwargs = mock_cal.update_event.call_args
        assert kwargs["summary"] == "New Title"
        assert kwargs["start_dt"] is None
        assert "result" in json.loads(result)

    @pytest.mark.asyncio
    async def test_edit_time(self):
        with patch("organist_bot.integrations.unified_agent._make_calendar_client") as mock_factory:
            mock_cal = MagicMock()
            mock_factory.return_value = mock_cal
            result = await _execute_tool("edit_gig", {"number": 1, "time": "11:00am"}, CHAT_ID)
        _, kwargs = mock_cal.update_event.call_args
        assert kwargs["start_dt"] is not None
        assert kwargs["start_dt"].hour == 11
        assert "result" in json.loads(result)

    @pytest.mark.asyncio
    async def test_edit_date_updates_unavailable(self):
        with (
            patch("organist_bot.integrations.unified_agent._make_calendar_client") as mock_factory,
            patch("organist_bot.integrations.unified_agent.filter_store") as mock_fs,
        ):
            mock_factory.return_value = MagicMock()
            await _execute_tool("edit_gig", {"number": 1, "date": "Sunday 7th June 2026"}, CHAT_ID)
        mock_fs.remove_period.assert_called_once_with("unavailable_periods", "2026-06-01")
        mock_fs.add_period.assert_called_once_with("unavailable_periods", "2026-06-07")

    @pytest.mark.asyncio
    async def test_no_listing_returns_error(self):
        from organist_bot.integrations.unified_agent import _last_gig_listing

        _last_gig_listing.pop(CHAT_ID, None)
        result = await _execute_tool("edit_gig", {"number": 1, "summary": "X"}, CHAT_ID)
        assert "error" in json.loads(result)

    @pytest.mark.asyncio
    async def test_out_of_range_returns_error(self):
        result = await _execute_tool("edit_gig", {"number": 99}, CHAT_ID)
        assert "error" in json.loads(result)

    @pytest.mark.asyncio
    async def test_updates_cached_listing(self):
        from organist_bot.integrations.unified_agent import _last_gig_listing

        with patch("organist_bot.integrations.unified_agent._make_calendar_client") as mock_factory:
            mock_factory.return_value = MagicMock()
            await _execute_tool("edit_gig", {"number": 1, "summary": "Updated"}, CHAT_ID)
        assert _last_gig_listing[CHAT_ID][0]["summary"] == "Updated"


# ── Invoice client tools ──────────────────────────────────────────────────────


class TestInvoiceClientTools:
    @pytest.mark.asyncio
    async def test_list_clients_returns_all(self):
        clients = {
            "holy-cross": {
                "name": "The Secretary",
                "address": "1 Road",
                "email": "a@b.com",
                "cc": [],
            }
        }
        with patch("organist_bot.integrations.unified_agent.load_clients", return_value=clients):
            result = await _execute_tool("list_clients", {}, CHAT_ID)
        assert "holy-cross" in result

    @pytest.mark.asyncio
    async def test_list_clients_empty_message(self):
        with patch("organist_bot.integrations.unified_agent.load_clients", return_value={}):
            result = await _execute_tool("list_clients", {}, CHAT_ID)
        assert "no clients" in result.lower()

    @pytest.mark.asyncio
    async def test_get_client_found(self):
        clients = {
            "st-marys": {
                "name": "St Mary's",
                "address": "1 Church St",
                "email": "c@d.com",
                "cc": [],
            }
        }
        with patch("organist_bot.integrations.unified_agent.load_clients", return_value=clients):
            result = await _execute_tool("get_client", {"client_key": "st-marys"}, CHAT_ID)
        assert "St Mary's" in result

    @pytest.mark.asyncio
    async def test_get_client_not_found(self):
        with patch("organist_bot.integrations.unified_agent.load_clients", return_value={}):
            result = await _execute_tool("get_client", {"client_key": "missing"}, CHAT_ID)
        data = json.loads(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_add_client_calls_add_client(self):
        with patch("organist_bot.integrations.unified_agent.add_client") as mock_add:
            result = await _execute_tool(
                "add_client",
                {"key": "new-key", "name": "New Client", "address": "2 Road"},
                CHAT_ID,
            )
        mock_add.assert_called_once_with(
            key="new-key", name="New Client", address="2 Road", email="", cc=[]
        )
        assert "added" in result.lower()

    @pytest.mark.asyncio
    async def test_edit_client_calls_edit_client(self):
        with patch("organist_bot.integrations.unified_agent.edit_client") as mock_edit:
            result = await _execute_tool(
                "edit_client", {"key": "st-marys", "email": "new@email.com"}, CHAT_ID
            )
        mock_edit.assert_called_once_with(
            key="st-marys", name=None, address=None, email="new@email.com", cc=None
        )
        assert "updated" in result.lower()

    @pytest.mark.asyncio
    async def test_edit_client_not_found(self):
        with patch(
            "organist_bot.integrations.unified_agent.edit_client",
            side_effect=ValueError("not found"),
        ):
            result = await _execute_tool("edit_client", {"key": "missing"}, CHAT_ID)
        data = json.loads(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_delete_client_calls_delete_client(self):
        with patch("organist_bot.integrations.unified_agent.delete_client") as mock_del:
            result = await _execute_tool("delete_client", {"key": "old-key"}, CHAT_ID)
        mock_del.assert_called_once_with("old-key")
        assert "deleted" in result.lower()

    @pytest.mark.asyncio
    async def test_delete_client_not_found(self):
        with patch(
            "organist_bot.integrations.unified_agent.delete_client",
            side_effect=ValueError("not found"),
        ):
            result = await _execute_tool("delete_client", {"key": "missing"}, CHAT_ID)
        data = json.loads(result)
        assert "error" in data


# ── Invoice generation & email tools ─────────────────────────────────────────


class TestInvoiceGenerationTools:
    @pytest.fixture(autouse=True)
    def reset_state(self):
        from organist_bot.integrations.unified_agent import _last_invoice

        _last_invoice.pop(CHAT_ID, None)
        yield
        _last_invoice.pop(CHAT_ID, None)

    @pytest.mark.asyncio
    async def test_generate_invoice_stores_in_last_invoice(self):
        fake_result = {
            "pdf_path": "/tmp/inv.pdf",
            "client_key": "a",
            "client_name": "A",
            "client_email": "a@a.com",
            "client_cc": [],
            "invoice_number": "INV-2026-001",
            "year": 2026,
            "date": "1 Jan 2026",
            "items": [],
            "total": 100.0,
            "currency": "£",
            "emailed": False,
            "created_at": "2026-01-01T00:00:00",
        }
        with patch(
            "organist_bot.integrations.unified_agent.generate_invoice",
            new=AsyncMock(return_value=fake_result),
        ):
            result = await _execute_tool(
                "generate_invoice",
                {
                    "client_key": "a",
                    "items": [{"description": "S", "quantity": 1, "unit_price": 100}],
                },
                CHAT_ID,
            )
        data = json.loads(result)
        assert data["invoice_number"] == "INV-2026-001"
        from organist_bot.integrations.unified_agent import _last_invoice

        assert _last_invoice[CHAT_ID]["invoice_number"] == "INV-2026-001"

    @pytest.mark.asyncio
    async def test_send_invoice_email_no_invoice_returns_error(self):
        result = await _execute_tool("send_invoice_email", {}, CHAT_ID)
        data = json.loads(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_send_invoice_email_sends_and_marks_emailed(self):
        from organist_bot.integrations.unified_agent import _last_invoice

        _last_invoice[CHAT_ID] = {
            "invoice_number": "INV-2026-001",
            "client_email": "a@a.com",
            "client_cc": [],
            "pdf_path": "/tmp/inv.pdf",
        }
        with (
            patch(
                "organist_bot.integrations.unified_agent.send_invoice_email",
                return_value={"success": True},
            ) as mock_send,
            patch("organist_bot.integrations.unified_agent.mark_invoice_emailed") as mock_mark,
        ):
            result = await _execute_tool("send_invoice_email", {}, CHAT_ID)
        mock_send.assert_called_once()
        mock_mark.assert_called_once_with("INV-2026-001")
        assert "a@a.com" in result

    @pytest.mark.asyncio
    async def test_list_invoices_returns_summary(self):
        invoices = {
            "INV-2026-001": {
                "invoice_number": "INV-2026-001",
                "client_key": "a",
                "client_name": "A",
                "total": 100.0,
                "date": "1 Jan 2026",
                "currency": "£",
                "emailed": False,
                "created_at": "2026-01-01T00:00:00",
            }
        }
        with patch("organist_bot.integrations.unified_agent.load_invoices", return_value=invoices):
            result = await _execute_tool("list_invoices", {}, CHAT_ID)
        assert "INV-2026-001" in result


# ── Filter management tools ───────────────────────────────────────────────────


class TestFilterTools:
    @pytest.mark.asyncio
    async def test_manage_blacklist_list(self):
        with patch("organist_bot.integrations.unified_agent.filter_store") as mock_fs:
            mock_fs.blacklist_emails.return_value = ["bad@evil.com"]
            result = await _execute_tool("manage_blacklist", {"action": "list"}, CHAT_ID)
        assert "bad@evil.com" in result

    @pytest.mark.asyncio
    async def test_manage_blacklist_add(self):
        with patch("organist_bot.integrations.unified_agent.filter_store") as mock_fs:
            mock_fs.add_blacklist_email.return_value = True
            result = await _execute_tool(
                "manage_blacklist", {"action": "add", "email": "x@y.com"}, CHAT_ID
            )
        mock_fs.add_blacklist_email.assert_called_once_with("x@y.com")
        assert "added" in result.lower()

    @pytest.mark.asyncio
    async def test_manage_blacklist_remove(self):
        with patch("organist_bot.integrations.unified_agent.filter_store") as mock_fs:
            mock_fs.remove_blacklist_email.return_value = True
            result = await _execute_tool(
                "manage_blacklist", {"action": "remove", "email": "x@y.com"}, CHAT_ID
            )
        mock_fs.remove_blacklist_email.assert_called_once_with("x@y.com")
        assert "removed" in result.lower()

    @pytest.mark.asyncio
    async def test_manage_unavailable_add(self):
        with patch("organist_bot.integrations.unified_agent.filter_store") as mock_fs:
            mock_fs.add_period.return_value = True
            result = await _execute_tool(
                "manage_unavailable", {"action": "add", "period": "2026-12"}, CHAT_ID
            )
        mock_fs.add_period.assert_called_once_with("unavailable_periods", "2026-12")
        assert "unavailable" in result.lower()

    @pytest.mark.asyncio
    async def test_manage_unavailable_remove(self):
        with patch("organist_bot.integrations.unified_agent.filter_store") as mock_fs:
            mock_fs.remove_period.return_value = True
            await _execute_tool(
                "manage_unavailable", {"action": "remove", "period": "2026-12"}, CHAT_ID
            )
        mock_fs.remove_period.assert_called_once_with("unavailable_periods", "2026-12")

    @pytest.mark.asyncio
    async def test_manage_unavailable_list(self):
        with patch("organist_bot.integrations.unified_agent.filter_store") as mock_fs:
            mock_fs.unavailable_periods.return_value = ["2026-12", "2027-01-01"]
            result = await _execute_tool("manage_unavailable", {"action": "list"}, CHAT_ID)
        assert "01 Dec 2026" in result
        assert "01 Jan 2027" in result

    @pytest.mark.asyncio
    async def test_manage_available_add(self):
        with patch("organist_bot.integrations.unified_agent.filter_store") as mock_fs:
            mock_fs.add_period.return_value = True
            await _execute_tool("manage_available", {"action": "add", "period": "2026-08"}, CHAT_ID)
        mock_fs.add_period.assert_called_once_with("available_only_periods", "2026-08")

    @pytest.mark.asyncio
    async def test_manage_available_list(self):
        with patch("organist_bot.integrations.unified_agent.filter_store") as mock_fs:
            mock_fs.available_only_periods.return_value = ["2026-08-01"]
            result = await _execute_tool("manage_available", {"action": "list"}, CHAT_ID)
        assert "01 Aug 2026" in result
        assert "Available-only periods" in result

    @pytest.mark.asyncio
    async def test_manage_available_remove(self):
        with patch("organist_bot.integrations.unified_agent.filter_store") as mock_fs:
            mock_fs.remove_period.return_value = True
            result = await _execute_tool(
                "manage_available", {"action": "remove", "period": "2026-08"}, CHAT_ID
            )
        mock_fs.remove_period.assert_called_once_with("available_only_periods", "2026-08")
        data = json.loads(result)
        assert "2026-08" in data.get("result", "")

    @pytest.mark.asyncio
    async def test_manage_unavailable_add_blocks_calendar(self):
        mock_cal = MagicMock()
        with (
            patch("organist_bot.integrations.unified_agent.filter_store") as mock_fs,
            patch(
                "organist_bot.integrations.unified_agent._make_calendar_client",
                return_value=mock_cal,
            ),
        ):
            mock_fs.add_period.return_value = True
            await _execute_tool(
                "manage_unavailable", {"action": "add", "period": "2026-12"}, CHAT_ID
            )
        mock_cal.block_period.assert_called_once_with("2026-12")

    @pytest.mark.asyncio
    async def test_manage_unavailable_add_calendar_failure_does_not_raise(self):
        mock_cal = MagicMock()
        mock_cal.block_period.side_effect = Exception("API down")
        with (
            patch("organist_bot.integrations.unified_agent.filter_store") as mock_fs,
            patch(
                "organist_bot.integrations.unified_agent._make_calendar_client",
                return_value=mock_cal,
            ),
        ):
            mock_fs.add_period.return_value = True
            result = await _execute_tool(
                "manage_unavailable", {"action": "add", "period": "2026-12"}, CHAT_ID
            )
        data = json.loads(result)
        assert "result" in data

    @pytest.mark.asyncio
    async def test_manage_unavailable_add_skips_calendar_when_none(self):
        with (
            patch("organist_bot.integrations.unified_agent.filter_store") as mock_fs,
            patch(
                "organist_bot.integrations.unified_agent._make_calendar_client",
                return_value=None,
            ),
        ):
            mock_fs.add_period.return_value = True
            result = await _execute_tool(
                "manage_unavailable", {"action": "add", "period": "2026-12"}, CHAT_ID
            )
        data = json.loads(result)
        assert "result" in data

    @pytest.mark.asyncio
    async def test_manage_unavailable_remove_unblocks_calendar(self):
        mock_cal = MagicMock()
        with (
            patch("organist_bot.integrations.unified_agent.filter_store") as mock_fs,
            patch(
                "organist_bot.integrations.unified_agent._make_calendar_client",
                return_value=mock_cal,
            ),
        ):
            mock_fs.remove_period.return_value = True
            await _execute_tool(
                "manage_unavailable", {"action": "remove", "period": "2026-12"}, CHAT_ID
            )
        mock_cal.unblock_period.assert_called_once_with("2026-12")

    @pytest.mark.asyncio
    async def test_manage_unavailable_remove_calendar_failure_does_not_raise(self):
        mock_cal = MagicMock()
        mock_cal.unblock_period.side_effect = Exception("API down")
        with (
            patch("organist_bot.integrations.unified_agent.filter_store") as mock_fs,
            patch(
                "organist_bot.integrations.unified_agent._make_calendar_client",
                return_value=mock_cal,
            ),
        ):
            mock_fs.remove_period.return_value = True
            result = await _execute_tool(
                "manage_unavailable", {"action": "remove", "period": "2026-12"}, CHAT_ID
            )
        data = json.loads(result)
        assert "result" in data

    @pytest.mark.asyncio
    async def test_manage_filter_suspensions_list_empty(self):
        with patch("organist_bot.integrations.unified_agent.filter_suspension_store") as mock_fss:
            mock_fss.list_suspensions.return_value = []
            result = await _execute_tool("manage_filter_suspensions", {"action": "list"}, CHAT_ID)
        assert "no filter suspensions" in result.lower()

    @pytest.mark.asyncio
    async def test_manage_filter_suspensions_list_shows_filter_and_period(self):
        with patch("organist_bot.integrations.unified_agent.filter_suspension_store") as mock_fss:
            mock_fss.list_suspensions.return_value = [{"filter": "postcode", "period": "2026-12"}]
            result = await _execute_tool("manage_filter_suspensions", {"action": "list"}, CHAT_ID)
        assert "postcode" in result
        assert "01 Dec 2026" in result

    @pytest.mark.asyncio
    async def test_manage_filter_suspensions_list_shows_open_ended_from(self):
        with patch("organist_bot.integrations.unified_agent.filter_suspension_store") as mock_fss:
            mock_fss.list_suspensions.return_value = [{"filter": "fee", "period": "2026-08-01:"}]
            result = await _execute_tool("manage_filter_suspensions", {"action": "list"}, CHAT_ID)
        assert "from" in result.lower()
        assert "onward" in result.lower()

    @pytest.mark.asyncio
    async def test_manage_filter_suspensions_list_shows_open_ended_until(self):
        with patch("organist_bot.integrations.unified_agent.filter_suspension_store") as mock_fss:
            mock_fss.list_suspensions.return_value = [{"filter": "all", "period": ":2026-01-05"}]
            result = await _execute_tool("manage_filter_suspensions", {"action": "list"}, CHAT_ID)
        assert "through" in result.lower()

    @pytest.mark.asyncio
    async def test_manage_filter_suspensions_add_success(self):
        with patch("organist_bot.integrations.unified_agent.filter_suspension_store") as mock_fss:
            mock_fss.add_suspension.return_value = True
            result = await _execute_tool(
                "manage_filter_suspensions",
                {"action": "add", "filter": "postcode", "period": "2026-12"},
                CHAT_ID,
            )
        mock_fss.add_suspension.assert_called_once_with("postcode", "2026-12")
        data = json.loads(result)
        assert "suspended" in data["result"].lower()

    @pytest.mark.asyncio
    async def test_manage_filter_suspensions_add_duplicate(self):
        with patch("organist_bot.integrations.unified_agent.filter_suspension_store") as mock_fss:
            mock_fss.add_suspension.return_value = False
            result = await _execute_tool(
                "manage_filter_suspensions",
                {"action": "add", "filter": "postcode", "period": "2026-12"},
                CHAT_ID,
            )
        data = json.loads(result)
        assert "already" in data["result"].lower()

    @pytest.mark.asyncio
    async def test_manage_filter_suspensions_add_invalid_returns_error(self):
        with patch("organist_bot.integrations.unified_agent.filter_suspension_store") as mock_fss:
            mock_fss.add_suspension.side_effect = ValueError("Could not parse period 'nonsense'")
            result = await _execute_tool(
                "manage_filter_suspensions",
                {"action": "add", "filter": "fee", "period": "nonsense"},
                CHAT_ID,
            )
        data = json.loads(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_manage_filter_suspensions_add_resolves_relative_period(self):
        with patch("organist_bot.integrations.unified_agent.filter_suspension_store") as mock_fss:
            mock_fss.add_suspension.return_value = True
            await _execute_tool(
                "manage_filter_suspensions",
                {"action": "add", "filter": "fee", "period": "next month"},
                CHAT_ID,
            )
        called_period = mock_fss.add_suspension.call_args[0][1]
        assert called_period != "next month"  # resolved to a real token

    @pytest.mark.asyncio
    async def test_manage_filter_suspensions_remove_success(self):
        with patch("organist_bot.integrations.unified_agent.filter_suspension_store") as mock_fss:
            mock_fss.remove_suspension.return_value = True
            result = await _execute_tool(
                "manage_filter_suspensions",
                {"action": "remove", "filter": "postcode", "period": "2026-12"},
                CHAT_ID,
            )
        mock_fss.remove_suspension.assert_called_once_with("postcode", "2026-12")
        data = json.loads(result)
        assert "resumed" in data["result"].lower()

    @pytest.mark.asyncio
    async def test_manage_filter_suspensions_remove_not_found(self):
        with patch("organist_bot.integrations.unified_agent.filter_suspension_store") as mock_fss:
            mock_fss.remove_suspension.return_value = False
            result = await _execute_tool(
                "manage_filter_suspensions",
                {"action": "remove", "filter": "postcode", "period": "2026-12"},
                CHAT_ID,
            )
        data = json.loads(result)
        assert "no matching" in data["result"].lower()

    def test_seen_not_in_manage_filter_suspensions_enum(self):
        tool_def = next(t for t in TOOLS if t["function"]["name"] == "manage_filter_suspensions")
        assert "seen" not in tool_def["function"]["parameters"]["properties"]["filter"]["enum"]


# ── clear_conversation ────────────────────────────────────────────────────────


class TestClearConversation:
    @pytest.mark.asyncio
    async def test_clears_all_three_dicts(self):
        from organist_bot.integrations.unified_agent import (
            _histories,
            _last_gig_listing,
            _last_invoice,
        )

        _histories[CHAT_ID] = [{"role": "user", "content": "hello"}]
        _last_invoice[CHAT_ID] = {"invoice_number": "INV-2026-001"}
        _last_gig_listing[CHAT_ID] = [{"id": "evt1"}]

        result = await _execute_tool("clear_conversation", {}, CHAT_ID)

        assert CHAT_ID not in _histories
        assert CHAT_ID not in _last_invoice
        assert CHAT_ID not in _last_gig_listing
        assert "cleared" in result.lower()


# ── sync_calendar_blocks ──────────────────────────────────────────────────────


class TestSyncCalendarBlocks:
    def test_calls_block_period_for_each_unavailable_period(self):
        mock_cal = MagicMock()
        with patch("organist_bot.integrations.unified_agent.filter_store") as mock_fs:
            mock_fs.unavailable_periods.return_value = ["2026-12", "2027-01-15"]
            sync_calendar_blocks(mock_cal)
        assert mock_cal.block_period.call_count == 2
        mock_cal.block_period.assert_any_call("2026-12")
        mock_cal.block_period.assert_any_call("2027-01-15")

    def test_no_periods_makes_no_calls(self):
        mock_cal = MagicMock()
        with patch("organist_bot.integrations.unified_agent.filter_store") as mock_fs:
            mock_fs.unavailable_periods.return_value = []
            sync_calendar_blocks(mock_cal)
        mock_cal.block_period.assert_not_called()

    def test_api_failure_on_one_period_does_not_abort_others(self):
        mock_cal = MagicMock()
        mock_cal.block_period.side_effect = [Exception("API error"), "evt_ok"]
        with patch("organist_bot.integrations.unified_agent.filter_store") as mock_fs:
            mock_fs.unavailable_periods.return_value = ["2026-12", "2027-01"]
            sync_calendar_blocks(mock_cal)  # must not raise
        assert mock_cal.block_period.call_count == 2


# ── _resolve_period ───────────────────────────────────────────────────────────


class TestResolvePeriod:
    def test_today(self):
        import datetime

        from organist_bot.integrations.unified_agent import _resolve_period

        today = datetime.date.today().isoformat()
        assert _resolve_period("today") == today
        assert _resolve_period("Today") == today

    def test_tomorrow(self):
        import datetime

        from organist_bot.integrations.unified_agent import _resolve_period

        tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        assert _resolve_period("tomorrow") == tomorrow

    def test_this_month(self):
        import datetime

        from organist_bot.integrations.unified_agent import _resolve_period

        expected = datetime.date.today().strftime("%Y-%m")
        assert _resolve_period("this month") == expected

    def test_next_month(self):
        import datetime

        from organist_bot.integrations.unified_agent import _resolve_period

        today = datetime.date.today()
        if today.month == 12:
            expected = f"{today.year + 1}-01"
        else:
            expected = f"{today.year}-{today.month + 1:02d}"
        assert _resolve_period("next month") == expected

    def test_next_week_is_monday_to_sunday(self):
        import datetime

        from organist_bot.integrations.unified_agent import _resolve_period

        result = _resolve_period("next week")
        assert ":" in result
        start_str, end_str = result.split(":")
        start = datetime.date.fromisoformat(start_str)
        end = datetime.date.fromisoformat(end_str)
        assert start.weekday() == 0  # Monday
        assert end.weekday() == 6  # Sunday
        assert (end - start).days == 6

    def test_this_weekend_is_sat_and_sun(self):
        import datetime

        from organist_bot.integrations.unified_agent import _resolve_period

        result = _resolve_period("this weekend")
        today = datetime.date.today()
        if today.weekday() == 6:
            assert result == today.isoformat()
        elif today.weekday() == 5:
            assert (
                result == f"{today.isoformat()}:{(today + datetime.timedelta(days=1)).isoformat()}"
            )
        else:
            assert ":" in result
            start, end = result.split(":")
            start_d = datetime.date.fromisoformat(start)
            end_d = datetime.date.fromisoformat(end)
            assert start_d.weekday() == 5  # Saturday
            assert end_d.weekday() == 6  # Sunday

    def test_this_weekday(self):
        import datetime

        from organist_bot.integrations.unified_agent import _resolve_period

        result = _resolve_period("this Sunday")
        d = datetime.date.fromisoformat(result)
        assert d.weekday() == 6  # Sunday
        assert d > datetime.date.today()  # always in the future

    def test_next_weekday(self):
        import datetime

        from organist_bot.integrations.unified_agent import _resolve_period

        result = _resolve_period("next Monday")
        d = datetime.date.fromisoformat(result)
        assert d.weekday() == 0  # Monday

    def test_this_weekend_on_saturday(self):
        """When today is Saturday, 'this weekend' returns today and tomorrow."""
        import datetime
        from unittest.mock import patch as _patch

        from organist_bot.integrations.unified_agent import _resolve_period

        saturday = datetime.date(2026, 5, 16)  # a Saturday
        with _patch("datetime.date") as mock_date:
            mock_date.today.return_value = saturday
            mock_date.side_effect = lambda *a, **kw: datetime.date(*a, **kw)
            result = _resolve_period("this weekend")
        assert result == "2026-05-16:2026-05-17"

    def test_this_weekend_on_sunday(self):
        """When today is Sunday, 'this weekend' returns today only."""
        import datetime
        from unittest.mock import patch as _patch

        from organist_bot.integrations.unified_agent import _resolve_period

        sunday = datetime.date(2026, 5, 17)  # a Sunday
        with _patch("datetime.date") as mock_date:
            mock_date.today.return_value = sunday
            mock_date.side_effect = lambda *a, **kw: datetime.date(*a, **kw)
            result = _resolve_period("this weekend")
        assert result == "2026-05-17"

    def test_unknown_expression_passthrough(self):
        from organist_bot.integrations.unified_agent import _resolve_period

        assert _resolve_period("2026-12-25") == "2026-12-25"
        assert _resolve_period("gibberish") == "gibberish"
        assert _resolve_period("2026-12") == "2026-12"


# ── manage_config ─────────────────────────────────────────────────────────────


class TestManageConfig:
    @pytest.mark.asyncio
    async def test_get_shows_all_three_keys(self):
        mock_store = MagicMock()
        mock_store.all.return_value = {"min_fee": 150}
        with patch("organist_bot.integrations.unified_agent.runtime_config", mock_store):
            result = await _execute_tool("manage_config", {"action": "get"}, CHAT_ID)
        data = json.loads(result)
        assert "result" in data
        assert "min_fee" in data["result"]
        assert "max_travel_minutes" in data["result"]
        assert "poll_minutes" in data["result"]

    @pytest.mark.asyncio
    async def test_set_valid_value(self):
        mock_store = MagicMock()
        with patch("organist_bot.integrations.unified_agent.runtime_config", mock_store):
            result = await _execute_tool(
                "manage_config", {"action": "set", "key": "min_fee", "value": 150}, CHAT_ID
            )
        mock_store.set.assert_called_once_with("min_fee", 150)
        data = json.loads(result)
        assert "result" in data
        assert "150" in data["result"]

    @pytest.mark.asyncio
    async def test_set_invalid_range_returns_error(self):
        mock_store = MagicMock()
        with patch("organist_bot.integrations.unified_agent.runtime_config", mock_store):
            result = await _execute_tool(
                "manage_config",
                {"action": "set", "key": "poll_minutes", "value": 999},
                CHAT_ID,
            )
        mock_store.set.assert_not_called()
        data = json.loads(result)
        assert "error" in data or (
            "result" in data
            and ("invalid" in data["result"].lower() or "range" in data["result"].lower())
        )

    @pytest.mark.asyncio
    async def test_reset_calls_store_reset(self):
        mock_store = MagicMock()
        mock_store.reset.return_value = True
        with patch("organist_bot.integrations.unified_agent.runtime_config", mock_store):
            result = await _execute_tool(
                "manage_config", {"action": "reset", "key": "min_fee"}, CHAT_ID
            )
        mock_store.reset.assert_called_once_with("min_fee")
        data = json.loads(result)
        assert "result" in data

    @pytest.mark.asyncio
    async def test_reset_not_set_returns_message(self):
        mock_store = MagicMock()
        mock_store.reset.return_value = False
        with patch("organist_bot.integrations.unified_agent.runtime_config", mock_store):
            result = await _execute_tool(
                "manage_config", {"action": "reset", "key": "min_fee"}, CHAT_ID
            )
        data = json.loads(result)
        assert "result" in data


# ── manage_applications ───────────────────────────────────────────────────────


def _make_app_record(**overrides) -> dict:
    defaults = {
        "url": "https://organistsonline.org/gig/1",
        "header": "Sunday Service",
        "organisation": "St Mary's",
        "date": "Sunday, 15 June 2026",
        "fee": "£80",
        "status": "applied",
        "applied_at": "2026-05-01T10:00:00Z",
        "updated_at": "2026-05-01T10:00:00Z",
    }
    defaults.update(overrides)
    return defaults


class TestManageApplications:
    @pytest.mark.asyncio
    async def test_summary_returns_status_counts(self):
        records = [
            _make_app_record(status="accepted"),
            _make_app_record(url="u2", status="applied"),
            _make_app_record(url="u3", status="no_response"),
        ]
        income = {"total": 80.0, "count": 1, "no_fee_count": 0, "records": []}
        with patch("organist_bot.integrations.unified_agent.application_store") as mock_store:
            mock_store.list_applications.return_value = records
            mock_store.get_income.return_value = income
            result = await _execute_tool("manage_applications", {"action": "summary"}, CHAT_ID)
        assert "Accepted" in result
        assert "Pending" in result
        assert "No response" in result

    @pytest.mark.asyncio
    async def test_list_returns_numbered_entries_with_emoji(self):
        records = [_make_app_record(status="accepted")]
        with patch("organist_bot.integrations.unified_agent.application_store") as mock_store:
            mock_store.list_applications.return_value = records
            result = await _execute_tool("manage_applications", {"action": "list"}, CHAT_ID)
        assert "Sunday Service" in result
        assert "St Mary's" in result
        assert "✅" in result
        assert "1." in result

    @pytest.mark.asyncio
    async def test_list_empty_returns_no_applications_message(self):
        with patch("organist_bot.integrations.unified_agent.application_store") as mock_store:
            mock_store.list_applications.return_value = []
            result = await _execute_tool("manage_applications", {"action": "list"}, CHAT_ID)
        data = json.loads(result)
        assert "result" in data

    @pytest.mark.asyncio
    async def test_update_changes_status_via_cached_listing(self):
        records = [_make_app_record(status="applied")]
        with patch("organist_bot.integrations.unified_agent.application_store") as mock_store:
            mock_store.list_applications.return_value = records
            mock_store.update_status.return_value = True
            # populate the listing cache first
            await _execute_tool("manage_applications", {"action": "list"}, CHAT_ID)
            result = await _execute_tool(
                "manage_applications",
                {"action": "update", "number": 1, "status": "declined"},
                CHAT_ID,
            )
        mock_store.update_status.assert_called_once_with(
            "https://organistsonline.org/gig/1", "declined"
        )
        data = json.loads(result)
        assert "result" in data

    @pytest.mark.asyncio
    async def test_update_no_listing_cached_returns_error(self):
        from organist_bot.integrations.unified_agent import _last_application_listing

        _last_application_listing.pop(CHAT_ID, None)
        with patch("organist_bot.integrations.unified_agent.application_store") as mock_store:
            mock_store.list_applications.return_value = []
            result = await _execute_tool(
                "manage_applications",
                {"action": "update", "number": 1, "status": "declined"},
                CHAT_ID,
            )
        data = json.loads(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_summary_populates_listing_cache(self):
        """summary must populate _last_application_listing so detail works afterwards."""
        from organist_bot.integrations.unified_agent import _last_application_listing

        _last_application_listing.pop(CHAT_ID, None)
        records = [_make_app_record(status="accepted")]
        income = {"total": 80.0, "count": 1, "no_fee_count": 0, "records": []}
        with patch("organist_bot.integrations.unified_agent.application_store") as mock_store:
            mock_store.list_applications.return_value = records
            mock_store.get_income.return_value = income
            await _execute_tool("manage_applications", {"action": "summary"}, CHAT_ID)
        assert _last_application_listing.get(CHAT_ID) == records

    @pytest.mark.asyncio
    async def test_detail_returns_all_fields(self):
        """detail returns every stored field for the requested record."""
        records = [
            _make_app_record(
                status="accepted",
                email="vicar@stmarys.org",
                applied_at="2026-05-01T10:00:00Z",
                updated_at="2026-05-02T11:00:00Z",
            )
        ]
        with patch("organist_bot.integrations.unified_agent.application_store") as mock_store:
            mock_store.list_applications.return_value = records
            await _execute_tool("manage_applications", {"action": "list"}, CHAT_ID)
            result = await _execute_tool(
                "manage_applications", {"action": "detail", "number": 1}, CHAT_ID
            )
        data = json.loads(result)
        text = data["result"]
        assert "Sunday Service" in text
        assert "St Mary's" in text
        assert "vicar@stmarys.org" in text
        assert "https://organistsonline.org/gig/1" in text
        assert "2026-05-01T10:00:00Z" in text
        assert "accepted" in text

    @pytest.mark.asyncio
    async def test_detail_after_summary_works(self):
        """detail must work after summary (not just after list)."""
        records = [_make_app_record(status="no_response")]
        income = {"total": 0.0, "count": 0, "no_fee_count": 0, "records": []}
        with patch("organist_bot.integrations.unified_agent.application_store") as mock_store:
            mock_store.list_applications.return_value = records
            mock_store.get_income.return_value = income
            await _execute_tool("manage_applications", {"action": "summary"}, CHAT_ID)
            result = await _execute_tool(
                "manage_applications", {"action": "detail", "number": 1}, CHAT_ID
            )
        data = json.loads(result)
        assert "result" in data
        assert "Sunday Service" in data["result"]

    @pytest.mark.asyncio
    async def test_detail_no_cache_returns_error(self):
        """detail with no prior list/summary returns a clear error."""
        from organist_bot.integrations.unified_agent import _last_application_listing

        _last_application_listing.pop(CHAT_ID, None)
        result = await _execute_tool(
            "manage_applications", {"action": "detail", "number": 1}, CHAT_ID
        )
        data = json.loads(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_detail_out_of_range_returns_error(self):
        """detail with a number beyond the listing length returns a clear error."""
        records = [_make_app_record(status="applied")]
        with patch("organist_bot.integrations.unified_agent.application_store") as mock_store:
            mock_store.list_applications.return_value = records
            await _execute_tool("manage_applications", {"action": "list"}, CHAT_ID)
            result = await _execute_tool(
                "manage_applications", {"action": "detail", "number": 99}, CHAT_ID
            )
        data = json.loads(result)
        assert "error" in data


# ── get_income_forecast ───────────────────────────────────────────────────────


class TestGetIncomeForecast:
    @pytest.mark.asyncio
    async def test_formats_output_with_records(self):
        summary = {
            "total": 290.0,
            "count": 2,
            "no_fee_count": 0,
            "records": [
                {"organisation": "St John", "date": "2026-06-10", "fee": "£140.00"},
                {"organisation": "St Leonard's", "date": "2026-06-22", "fee": "£150.00"},
            ],
        }
        with patch(
            "organist_bot.integrations.unified_agent.application_store.get_income",
            return_value=summary,
        ):
            result = await _execute_tool(
                "get_income_forecast",
                {"from_date": "2026-06-01", "to_date": "2026-06-30"},
                CHAT_ID,
            )
        data = json.loads(result)["result"]
        assert "💰" in data
        assert "£290.00" in data
        assert "St John" in data
        assert "St Leonard" in data

    @pytest.mark.asyncio
    async def test_no_gigs_message(self):
        summary = {"total": 0.0, "count": 0, "no_fee_count": 0, "records": []}
        with patch(
            "organist_bot.integrations.unified_agent.application_store.get_income",
            return_value=summary,
        ):
            result = await _execute_tool(
                "get_income_forecast",
                {"from_date": "2026-06-01", "to_date": "2026-06-30"},
                CHAT_ID,
            )
        assert "No accepted gigs" in result

    @pytest.mark.asyncio
    async def test_shows_no_fee_note(self):
        summary = {
            "total": 140.0,
            "count": 2,
            "no_fee_count": 1,
            "records": [
                {"organisation": "St John", "date": "2026-06-10", "fee": "£140.00"},
                {"organisation": "All Saints", "date": "2026-06-15", "fee": ""},
            ],
        }
        with patch(
            "organist_bot.integrations.unified_agent.application_store.get_income",
            return_value=summary,
        ):
            result = await _execute_tool(
                "get_income_forecast",
                {"from_date": "2026-06-01", "to_date": "2026-06-30"},
                CHAT_ID,
            )
        assert "no fee" in result.lower() or "(no fee)" in result.lower()


# ── manage_applications summary income ───────────────────────────────────────


class TestManageApplicationsSummaryIncome:
    @pytest.mark.asyncio
    async def test_summary_includes_income_line(self):
        records = [
            {
                "url": "http://a.com/1",
                "header": "Service",
                "organisation": "St John",
                "date": "2026-06-10",
                "fee": "£140.00",
                "email": "",
                "status": "accepted",
                "applied_at": "2026-06-01T10:00:00Z",
                "updated_at": "2026-06-01T10:00:00Z",
            }
        ]
        income = {"total": 140.0, "count": 1, "no_fee_count": 0, "records": records}
        with (
            patch(
                "organist_bot.integrations.unified_agent.application_store.list_applications",
                return_value=records,
            ),
            patch(
                "organist_bot.integrations.unified_agent.application_store.get_income",
                return_value=income,
            ),
        ):
            result = await _execute_tool("manage_applications", {"action": "summary"}, CHAT_ID)
        text = json.loads(result)["result"]
        assert "Income" in text
        assert "£140.00" in text


# ── get_application_analytics ─────────────────────────────────────────────────


class TestGetApplicationAnalytics:
    @pytest.mark.asyncio
    async def test_returns_formatted_metrics(self):
        mock_metrics = {
            "total": 10,
            "accepted": 3,
            "rejected": 2,
            "no_response": 4,
            "applied": 1,
            "acceptance_rate": 33.3,
            "response_rate": 55.6,
            "avg_response_days": 4.5,
        }
        with patch("organist_bot.integrations.unified_agent.analytics") as mock_analytics:
            mock_analytics.get_success_metrics.return_value = mock_metrics
            result = await _execute_tool("get_application_analytics", {}, CHAT_ID)
        data = json.loads(result)
        assert "result" in data
        assert "33.3%" in data["result"]
        assert "4.5 days" in data["result"]

    @pytest.mark.asyncio
    async def test_no_avg_response_shows_not_enough_data(self):
        mock_metrics = {
            "total": 5,
            "accepted": 0,
            "rejected": 0,
            "no_response": 5,
            "applied": 0,
            "acceptance_rate": 0.0,
            "response_rate": 0.0,
            "avg_response_days": None,
        }
        with patch("organist_bot.integrations.unified_agent.analytics") as mock_analytics:
            mock_analytics.get_success_metrics.return_value = mock_metrics
            result = await _execute_tool("get_application_analytics", {}, CHAT_ID)
        data = json.loads(result)
        assert "not enough data" in data["result"]

    @pytest.mark.asyncio
    async def test_custom_days_passed_to_analytics(self):
        mock_metrics = {
            "total": 0,
            "accepted": 0,
            "rejected": 0,
            "no_response": 0,
            "applied": 0,
            "acceptance_rate": 0.0,
            "response_rate": 0.0,
            "avg_response_days": None,
        }
        with patch("organist_bot.integrations.unified_agent.analytics") as mock_analytics:
            mock_analytics.get_success_metrics.return_value = mock_metrics
            await _execute_tool("get_application_analytics", {"days": 90}, CHAT_ID)
        mock_analytics.get_success_metrics.assert_called_once_with(90)


# ── get_gig_breakdown ─────────────────────────────────────────────────────────


class TestGetGigBreakdown:
    @pytest.mark.asyncio
    async def test_returns_sorted_breakdown(self):
        mock_breakdown = {
            "Wedding": {"count": 10, "accepted": 3, "acceptance_rate": 30.0},
            "Funeral": {"count": 5, "accepted": 1, "acceptance_rate": 20.0},
        }
        with patch("organist_bot.integrations.unified_agent.analytics") as mock_analytics:
            mock_analytics.get_gig_type_breakdown.return_value = mock_breakdown
            result = await _execute_tool("get_gig_breakdown", {}, CHAT_ID)
        data = json.loads(result)
        assert "result" in data
        assert "Wedding" in data["result"]
        assert "Funeral" in data["result"]
        lines = data["result"].split("\n")
        wedding_idx = next(i for i, line in enumerate(lines) if "Wedding" in line)
        funeral_idx = next(i for i, line in enumerate(lines) if "Funeral" in line)
        # Wedding (10 applied) must appear before Funeral (5 applied)
        assert wedding_idx < funeral_idx

    @pytest.mark.asyncio
    async def test_empty_breakdown_returns_no_applications_message(self):
        with patch("organist_bot.integrations.unified_agent.analytics") as mock_analytics:
            mock_analytics.get_gig_type_breakdown.return_value = {}
            result = await _execute_tool("get_gig_breakdown", {}, CHAT_ID)
        data = json.loads(result)
        assert "No applications" in data["result"]

    @pytest.mark.asyncio
    async def test_custom_days_passed_to_analytics(self):
        with patch("organist_bot.integrations.unified_agent.analytics") as mock_analytics:
            mock_analytics.get_gig_type_breakdown.return_value = {}
            await _execute_tool("get_gig_breakdown", {"days": 180}, CHAT_ID)
        mock_analytics.get_gig_type_breakdown.assert_called_once_with(180)


# ── add_gig travel buffers ────────────────────────────────────────────────────


class TestAddGigTravelBuffers:
    """add_gig should create travel buffers after creating the calendar event."""

    @pytest.mark.asyncio
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

            result = await _execute_tool(
                "add_gig",
                {
                    "confirmed": True,
                    "header": "Wedding",
                    "organisation": "St Paul's",
                    "locality": "Chelmsford",
                    "date": "2026-07-15",
                    "time": "11:00 AM",
                    "fee": "£200",
                    "postcode": "CM1 1AA",
                },
                CHAT_ID,
            )

        data = json.loads(result)
        assert "event_123" in data["result"]
        mock_travel.get_travel_minutes.assert_called_once_with("CM1 1AA")
        mock_cal.add_travel_buffers.assert_called_once()

    @pytest.mark.asyncio
    async def test_add_gig_still_succeeds_when_buffer_creation_fails(self):
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

            result = await _execute_tool(
                "add_gig",
                {
                    "confirmed": True,
                    "header": "Wedding",
                    "organisation": "St Paul's",
                    "locality": "Chelmsford",
                    "date": "2026-07-15",
                    "time": "11:00 AM",
                    "fee": "£200",
                    "postcode": "CM1 1AA",
                },
                CHAT_ID,
            )

        data = json.loads(result)
        assert "event_123" in data["result"]  # Still succeeded despite buffer failure


# ── mark_invoice_paid ─────────────────────────────────────────────────────────


class TestMarkInvoicePaidTool:
    async def test_marks_invoice_paid_and_returns_success(self):
        with patch(
            "organist_bot.integrations.unified_agent.mark_invoice_paid", return_value=True
        ) as mock_paid:
            agent = UnifiedAgent()
            result = await agent._execute_tool(
                "mark_invoice_paid", {"invoice_number": "INV-2026-001"}, chat_id=1
            )
        import json

        data = json.loads(result)
        assert "INV-2026-001" in data["result"]
        assert "paid" in data["result"].lower()
        mock_paid.assert_called_once_with("INV-2026-001")

    async def test_returns_error_for_unknown_invoice(self):
        with patch("organist_bot.integrations.unified_agent.mark_invoice_paid", return_value=False):
            agent = UnifiedAgent()
            result = await agent._execute_tool(
                "mark_invoice_paid", {"invoice_number": "INV-9999-999"}, chat_id=1
            )
        import json

        data = json.loads(result)
        assert "error" in data


class TestUnmarkInvoicePaidTool:
    async def test_unmarks_invoice_and_returns_success(self):
        with patch(
            "organist_bot.integrations.unified_agent.unmark_invoice_paid", return_value=True
        ) as mock_unmark:
            agent = UnifiedAgent()
            result = await agent._execute_tool(
                "unmark_invoice_paid", {"invoice_number": "INV-2026-001"}, chat_id=1
            )
        import json

        data = json.loads(result)
        assert "INV-2026-001" in data["result"]
        assert "no longer" in data["result"].lower()
        mock_unmark.assert_called_once_with("INV-2026-001")

    async def test_returns_error_for_unknown_invoice(self):
        with patch(
            "organist_bot.integrations.unified_agent.unmark_invoice_paid", return_value=False
        ):
            agent = UnifiedAgent()
            result = await agent._execute_tool(
                "unmark_invoice_paid", {"invoice_number": "INV-9999-999"}, chat_id=1
            )
        import json

        data = json.loads(result)
        assert "error" in data


class TestDeleteInvoiceTool:
    async def test_deletes_invoice_and_returns_success(self):
        with patch(
            "organist_bot.integrations.unified_agent.delete_invoice", return_value=True
        ) as mock_delete:
            agent = UnifiedAgent()
            result = await agent._execute_tool(
                "delete_invoice", {"invoice_number": "INV-2026-001"}, chat_id=1
            )
        import json

        data = json.loads(result)
        assert "INV-2026-001" in data["result"]
        assert "deleted" in data["result"].lower()
        mock_delete.assert_called_once_with("INV-2026-001")

    async def test_returns_error_for_unknown_invoice(self):
        with patch("organist_bot.integrations.unified_agent.delete_invoice", return_value=False):
            agent = UnifiedAgent()
            result = await agent._execute_tool(
                "delete_invoice", {"invoice_number": "INV-9999-999"}, chat_id=1
            )
        import json

        data = json.loads(result)
        assert "error" in data

    async def test_clears_last_invoice_cache_when_matching(self):
        from organist_bot.integrations.unified_agent import _last_invoice

        _last_invoice[42] = {"invoice_number": "INV-2026-001", "client_name": "St Paul's"}
        with patch("organist_bot.integrations.unified_agent.delete_invoice", return_value=True):
            agent = UnifiedAgent()
            await agent._execute_tool(
                "delete_invoice", {"invoice_number": "INV-2026-001"}, chat_id=42
            )
        assert 42 not in _last_invoice

    async def test_preserves_last_invoice_cache_when_different(self):
        from organist_bot.integrations.unified_agent import _last_invoice

        _last_invoice[42] = {"invoice_number": "INV-2026-002", "client_name": "St Mary's"}
        with patch("organist_bot.integrations.unified_agent.delete_invoice", return_value=True):
            agent = UnifiedAgent()
            await agent._execute_tool(
                "delete_invoice", {"invoice_number": "INV-2026-001"}, chat_id=42
            )
        assert _last_invoice.get(42, {}).get("invoice_number") == "INV-2026-002"
        _last_invoice.pop(42, None)


class TestListInvoicesPaymentStatus:
    async def test_shows_paid_status(self):
        invoices = {
            "INV-2026-001": {
                "invoice_number": "INV-2026-001",
                "client_name": "St Paul's",
                "total": 150.0,
                "currency": "£",
                "date": "1 June 2026",
                "emailed": True,
                "emailed_at": "2026-06-01T10:00:00Z",
                "paid_at": "2026-06-03T10:00:00Z",
                "reminder_sent": False,
            }
        }
        with patch("organist_bot.integrations.unified_agent.load_invoices", return_value=invoices):
            agent = UnifiedAgent()
            result = await agent._execute_tool("list_invoices", {}, chat_id=1)
        assert "paid" in result.lower()

    async def test_shows_overdue_status(self):
        import datetime

        overdue_at = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=7)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        invoices = {
            "INV-2026-001": {
                "invoice_number": "INV-2026-001",
                "client_name": "St Mary's",
                "total": 200.0,
                "currency": "£",
                "date": "1 May 2026",
                "emailed": True,
                "emailed_at": overdue_at,
                "paid_at": None,
                "reminder_sent": False,
            }
        }
        with patch("organist_bot.integrations.unified_agent.load_invoices", return_value=invoices):
            agent = UnifiedAgent()
            result = await agent._execute_tool("list_invoices", {}, chat_id=1)
        assert "overdue" in result.lower()

    async def test_empty_invoices_plain_text(self):
        with patch("organist_bot.integrations.unified_agent.load_invoices", return_value={}):
            agent = UnifiedAgent()
            result = await agent._execute_tool("list_invoices", {}, chat_id=1)
        assert result == "No invoices found."


# ── manage_applications declined → delete travel buffers ─────────────────────


class TestManageApplicationsDeclinedDeletesBuffers:
    @pytest.mark.asyncio
    async def test_declined_accepted_gig_deletes_travel_buffers(self):
        from organist_bot.integrations.unified_agent import _last_application_listing

        record = {
            "url": "http://a.com/1",
            "header": "Wedding",
            "organisation": "St Mary's",
            "date": "2026-07-15",
            "status": "accepted",
            "travel_before_event_id": "before_abc",
            "travel_after_event_id": "after_def",
        }
        _last_application_listing[CHAT_ID] = [record]
        try:
            with (
                patch("organist_bot.integrations.unified_agent.application_store") as mock_store,
                patch(
                    "organist_bot.integrations.unified_agent._make_calendar_client"
                ) as mock_cal_fn,
            ):
                mock_store.list_applications.return_value = [record]
                mock_store.update_status.return_value = True
                mock_cal = MagicMock()
                mock_cal_fn.return_value = mock_cal

                await _execute_tool(
                    "manage_applications",
                    {"action": "update", "number": 1, "status": "declined"},
                    CHAT_ID,
                )

            deleted_ids = [c.args[0] for c in mock_cal.delete_event.call_args_list]
            assert "before_abc" in deleted_ids
            assert "after_def" in deleted_ids
        finally:
            _last_application_listing.pop(CHAT_ID, None)


# ── Per-chat state persistence (survives bot restart) ───────────────────────────


class TestAgentStatePersistence:
    """_hydrate_chat / _persist_chat round-trip the last_* reference context to disk."""

    def test_persist_then_hydrate_restores_last_invoice(self, tmp_path, monkeypatch):
        from organist_bot.integrations import agent_state, unified_agent

        monkeypatch.setattr(agent_state, "_PATH", tmp_path / "agent_state.json")
        cid = 778899
        try:
            unified_agent._last_invoice[cid] = {"invoice_number": "INV-9", "pdf_path": "/x.pdf"}
            unified_agent._persist_chat(cid)
            # Simulate a restart: in-memory state and hydration marker are gone.
            unified_agent._last_invoice.pop(cid, None)
            unified_agent._hydrated.discard(cid)

            unified_agent._hydrate_chat(cid)
            assert unified_agent._last_invoice[cid] == {
                "invoice_number": "INV-9",
                "pdf_path": "/x.pdf",
            }
        finally:
            unified_agent._last_invoice.pop(cid, None)
            unified_agent._hydrated.discard(cid)

    def test_hydrate_does_not_clobber_live_state(self, tmp_path, monkeypatch):
        from organist_bot.integrations import agent_state, unified_agent

        monkeypatch.setattr(agent_state, "_PATH", tmp_path / "agent_state.json")
        cid = 887766
        try:
            agent_state.save_chat(cid, {"last_invoice": {"invoice_number": "OLD"}})
            unified_agent._hydrated.discard(cid)
            unified_agent._last_invoice[cid] = {"invoice_number": "LIVE"}

            unified_agent._hydrate_chat(cid)  # must not overwrite live in-memory state
            assert unified_agent._last_invoice[cid] == {"invoice_number": "LIVE"}
        finally:
            unified_agent._last_invoice.pop(cid, None)
            unified_agent._hydrated.discard(cid)

    def test_hydrate_is_idempotent(self, tmp_path, monkeypatch):
        from organist_bot.integrations import agent_state, unified_agent

        monkeypatch.setattr(agent_state, "_PATH", tmp_path / "agent_state.json")
        cid = 665544
        try:
            agent_state.save_chat(cid, {"last_gig_listing": [{"id": "evt1"}]})
            unified_agent._hydrated.discard(cid)
            unified_agent._last_gig_listing.pop(cid, None)

            unified_agent._hydrate_chat(cid)
            assert unified_agent._last_gig_listing[cid] == [{"id": "evt1"}]
            # Second hydrate is a no-op (chat already marked hydrated).
            unified_agent._last_gig_listing[cid] = [{"id": "changed"}]
            unified_agent._hydrate_chat(cid)
            assert unified_agent._last_gig_listing[cid] == [{"id": "changed"}]
        finally:
            unified_agent._last_gig_listing.pop(cid, None)
            unified_agent._hydrated.discard(cid)

    def test_persist_failure_is_swallowed(self, monkeypatch):
        """A disk error during persistence must never propagate to the user's reply."""
        from organist_bot.integrations import unified_agent

        def _boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr("organist_bot.integrations.unified_agent.agent_state.save_chat", _boom)
        unified_agent._persist_chat(123456)  # must not raise

    def test_hydrate_failure_is_swallowed(self, monkeypatch):
        """A failure loading persisted context must not break message handling."""
        from organist_bot.integrations import unified_agent

        def _boom(*a, **k):
            raise OSError("unreadable")

        monkeypatch.setattr("organist_bot.integrations.unified_agent.agent_state.load_chat", _boom)
        cid = 543210
        try:
            unified_agent._hydrated.discard(cid)
            unified_agent._hydrate_chat(cid)  # must not raise
        finally:
            unified_agent._hydrated.discard(cid)


# ── manage_config — negotiable_fee ────────────────────────────────────────────


import organist_bot.application_store as application_store  # noqa: E402
import organist_bot.runtime_config_store as rcs  # noqa: E402
from organist_bot.integrations.unified_agent import _TOOL_HANDLERS  # noqa: E402


@pytest.mark.asyncio
async def test_manage_config_accepts_negotiable_fee_set(tmp_path, monkeypatch):
    monkeypatch.setattr(rcs, "_PATH", tmp_path / "runtime_config.json")
    handler = _TOOL_HANDLERS["manage_config"]
    out = json.loads(await handler({"action": "set", "key": "negotiable_fee", "value": 150}, 1))
    assert "negotiable_fee set to 150" in out["result"]


@pytest.mark.asyncio
async def test_manage_config_get_shows_negotiable_fee(tmp_path, monkeypatch):
    monkeypatch.setattr(rcs, "_PATH", tmp_path / "runtime_config.json")
    handler = _TOOL_HANDLERS["manage_config"]
    out = json.loads(await handler({"action": "get"}, 1))
    assert "negotiable_fee" in out["result"]


@pytest.mark.asyncio
async def test_manage_config_rejects_negotiable_fee_out_of_range(tmp_path, monkeypatch):
    monkeypatch.setattr(rcs, "_PATH", tmp_path / "runtime_config.json")
    handler = _TOOL_HANDLERS["manage_config"]
    out = json.loads(await handler({"action": "set", "key": "negotiable_fee", "value": -5}, 1))
    assert "Invalid value" in out["result"]


# ── NEG-application agent tools ───────────────────────────────────────────────


def _seed_neg_pending(link="https://e.com/a"):
    from organist_bot.models import Gig

    gig = Gig(
        header="Test",
        organisation="Org",
        locality="London",
        date="Sunday, July 12, 2026",
        time="10:00 AM",
        fee="NEG",
        link=link,
        contact="Jane",
        email="jane@example.com",
    )
    return application_store.record_neg_pending(
        gig,
        draft_subject="Subject",
        draft_body="<p>Body</p>",
        negotiable_fee=120,
    )


@pytest.fixture
def neg_store(tmp_path, monkeypatch):
    unified_agent._active_neg_draft.pop(1, None)
    monkeypatch.setattr(application_store, "_PATH", tmp_path / "applications.json")


class TestNegTools:
    async def test_list_neg_pending_returns_pending_rows(self, neg_store):
        gig_id = _seed_neg_pending()
        out = json.loads(await _TOOL_HANDLERS["list_neg_pending"]({}, 1))
        assert gig_id in out["result"]
        assert "Test" in out["result"]

    async def test_list_neg_pending_empty(self, neg_store):
        out = json.loads(await _TOOL_HANDLERS["list_neg_pending"]({}, 1))
        assert "No NEG drafts pending" in out["result"]

    async def test_approve_returns_confirm_buttons(self, neg_store):
        gig_id = _seed_neg_pending()
        out = json.loads(await _TOOL_HANDLERS["approve_neg_application"]({"gig_id": gig_id}, 1))
        assert "confirm" in out["result"].lower() or "will send" in out["result"].lower()
        assert out["buttons"] == [
            [
                {"text": "Confirm", "callback_data": f"neg:confirm_send:{gig_id}"},
                {"text": "Cancel", "callback_data": f"neg:cancel:{gig_id}"},
            ]
        ]
        assert application_store._read()[0]["status"] == "neg_pending"

    async def test_approve_unknown_gig_id_returns_error(self, neg_store):
        out = json.loads(
            await _TOOL_HANDLERS["approve_neg_application"]({"gig_id": "deadbeefcafe"}, 1)
        )
        assert "no draft found" in out["result"].lower()
        assert "buttons" not in out

    async def test_approve_unknown_gig_id_does_not_poison_active_draft(self, neg_store):
        await _TOOL_HANDLERS["approve_neg_application"]({"gig_id": "deadbeefcafe"}, 1)
        assert unified_agent.get_active_neg_draft(1) is None

    async def test_approve_already_applied_returns_already(self, neg_store):
        gig_id = _seed_neg_pending()
        application_store.transition_neg_pending(gig_id, to="applied")
        out = json.loads(await _TOOL_HANDLERS["approve_neg_application"]({"gig_id": gig_id}, 1))
        assert "already" in out["result"].lower()

    async def test_approve_omitted_gig_id_resolves_single_pending(self, neg_store):
        gig_id = _seed_neg_pending()
        out = json.loads(await _TOOL_HANDLERS["approve_neg_application"]({}, 1))
        assert out["buttons"][0][0]["callback_data"] == f"neg:confirm_send:{gig_id}"

    async def test_approve_omitted_gig_id_multiple_pending_needs_pick(self, neg_store):
        id_a = _seed_neg_pending(link="https://e.com/a")
        id_b = _seed_neg_pending(link="https://e.com/b")
        out = json.loads(await _TOOL_HANDLERS["approve_neg_application"]({}, 1))
        assert out.get("needs_pick") is True
        picked_ids = {row[0]["callback_data"] for row in out["buttons"]}
        assert picked_ids == {f"neg:pick:{id_a}", f"neg:pick:{id_b}"}

    async def test_approve_omitted_gig_id_uses_active_draft(self, neg_store):
        id_a = _seed_neg_pending(link="https://e.com/a")
        _seed_neg_pending(link="https://e.com/b")
        unified_agent.set_active_neg_draft(1, id_a)
        try:
            out = json.loads(await _TOOL_HANDLERS["approve_neg_application"]({}, 1))
        finally:
            unified_agent._active_neg_draft.pop(1, None)
        assert out["buttons"][0][0]["callback_data"] == f"neg:confirm_send:{id_a}"

    async def test_edit_with_new_body_persists_and_returns_draft_buttons(self, neg_store):
        gig_id = _seed_neg_pending()
        out = json.loads(
            await _TOOL_HANDLERS["edit_neg_application"](
                {"gig_id": gig_id, "new_body": "<p>EDITED</p>"}, 1
            )
        )
        assert "EDITED" in out["result"]
        assert out["buttons"] == [
            [
                {"text": "✅ Accept", "callback_data": f"neg:accept:{gig_id}"},
                {"text": "✏️ Edit", "callback_data": f"neg:edit:{gig_id}"},
                {"text": "❌ Reject", "callback_data": f"neg:reject:{gig_id}"},
            ]
        ]
        r = application_store._read()[0]
        assert r["status"] == "neg_pending"
        assert "EDITED" in r["draft_body"]

    async def test_edit_requires_new_body_or_new_fee(self, neg_store):
        gig_id = _seed_neg_pending()
        out = json.loads(await _TOOL_HANDLERS["edit_neg_application"]({"gig_id": gig_id}, 1))
        assert "new_body or new_fee" in out["result"]
        assert "buttons" not in out

    async def test_edit_with_new_fee_rerenders_and_persists(self, neg_store):
        gig_id = _seed_neg_pending()
        out = json.loads(
            await _TOOL_HANDLERS["edit_neg_application"]({"gig_id": gig_id, "new_fee": 150}, 1)
        )
        assert "£150" in out["result"]
        r = application_store._read()[0]
        assert "£150" in r["draft_body"]
        assert r["negotiable_fee"] == 150
        assert r["status"] == "neg_pending"

    async def test_edit_sets_active_draft(self, neg_store):
        gig_id = _seed_neg_pending()
        await _TOOL_HANDLERS["edit_neg_application"]({"gig_id": gig_id, "new_fee": 150}, 42)
        try:
            assert unified_agent.get_active_neg_draft(42) == gig_id
        finally:
            unified_agent._active_neg_draft.pop(42, None)

    async def test_reject_returns_confirm_buttons(self, neg_store):
        gig_id = _seed_neg_pending()
        out = json.loads(await _TOOL_HANDLERS["reject_neg_application"]({"gig_id": gig_id}, 1))
        assert out["buttons"] == [
            [
                {"text": "Confirm", "callback_data": f"neg:confirm_reject:{gig_id}"},
                {"text": "Cancel", "callback_data": f"neg:cancel:{gig_id}"},
            ]
        ]
        assert application_store._read()[0]["status"] == "neg_pending"

    async def test_reject_omitted_gig_id_no_pending_returns_error(self, neg_store):
        out = json.loads(await _TOOL_HANDLERS["reject_neg_application"]({}, 1))
        assert "no draft found" in out["result"].lower() or "no neg drafts" in out["result"].lower()


# ── NEG active-draft state, buttons, and deterministic actions ──────────────

from organist_bot.integrations import unified_agent  # noqa: E402


class TestNegActiveDraftState:
    def test_set_and_get_active_neg_draft(self):
        unified_agent.set_active_neg_draft(999, "abc123")
        try:
            assert unified_agent.get_active_neg_draft(999) == "abc123"
        finally:
            unified_agent._active_neg_draft.pop(999, None)

    def test_get_active_neg_draft_defaults_to_none(self):
        assert unified_agent.get_active_neg_draft(88888) is None

    def test_stash_and_pop_pending_neg_instruction(self):
        unified_agent.stash_pending_neg_instruction(999, "raise the fee to 180")
        assert unified_agent.pop_pending_neg_instruction(999) == "raise the fee to 180"
        # pop is destructive — a second pop finds nothing.
        assert unified_agent.pop_pending_neg_instruction(999) is None


# ── _trim_history ─────────────────────────────────────────────────────────────


def _turn_with_tool_call(n: int) -> list[dict]:
    """One user(str) turn followed by an assistant tool_calls turn and a flat
    role="tool" result message — the shape litellm.acompletion's response actually
    produces (see process_message() in unified_agent.py)."""
    return [
        {"role": "user", "content": f"do thing {n}"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"tool_{n}",
                    "type": "function",
                    "function": {"name": "noop", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": f"tool_{n}", "name": "noop", "content": "ok"},
    ]


class TestTrimHistory:
    CHAT_ID = 777

    def teardown_method(self):
        unified_agent._histories.pop(self.CHAT_ID, None)

    def test_under_cap_is_untouched(self):
        history = _turn_with_tool_call(1) + _turn_with_tool_call(2)
        unified_agent._histories[self.CHAT_ID] = list(history)
        unified_agent._trim_history(self.CHAT_ID)
        assert unified_agent._histories[self.CHAT_ID] == history

    def test_over_cap_trims_to_a_user_text_boundary(self):
        # 30 turns * 3 entries = 90 messages, well past _MAX_HISTORY_MESSAGES (60).
        history: list[dict] = []
        for i in range(30):
            history.extend(_turn_with_tool_call(i))
        unified_agent._histories[self.CHAT_ID] = list(history)

        unified_agent._trim_history(self.CHAT_ID)
        trimmed = unified_agent._histories[self.CHAT_ID]

        assert len(trimmed) <= unified_agent._MAX_HISTORY_MESSAGES
        assert len(trimmed) < len(history)
        # Must start on a real user-text turn, never inside a tool_use/tool_result pair.
        assert trimmed[0]["role"] == "user"
        assert isinstance(trimmed[0]["content"], str)
        # No assistant tool_calls entry should be left without its matching
        # role="tool" result message.
        pending_tool_call_ids: set[str] = set()
        for msg in trimmed:
            if msg["role"] == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    pending_tool_call_ids.add(tc["id"])
            elif msg["role"] == "tool":
                pending_tool_call_ids.discard(msg["tool_call_id"])
        assert pending_tool_call_ids == set()

    def test_missing_chat_id_is_a_noop(self):
        unified_agent._trim_history(self.CHAT_ID)  # no entry for this chat_id at all
        assert self.CHAT_ID not in unified_agent._histories


class TestNegConfirmButtons:
    def test_send_buttons_use_confirm_send_callback(self):
        buttons = unified_agent.neg_confirm_buttons("abc123", send=True)
        assert buttons == [
            [
                {"text": "Confirm", "callback_data": "neg:confirm_send:abc123"},
                {"text": "Cancel", "callback_data": "neg:cancel:abc123"},
            ]
        ]

    def test_reject_buttons_use_confirm_reject_callback(self):
        buttons = unified_agent.neg_confirm_buttons("abc123", send=False)
        assert buttons == [
            [
                {"text": "Confirm", "callback_data": "neg:confirm_reject:abc123"},
                {"text": "Cancel", "callback_data": "neg:cancel:abc123"},
            ]
        ]


class TestManageLlmProvider:
    def teardown_method(self):
        from organist_bot.runtime_config_store import runtime_config

        runtime_config.reset("llm_provider")
        runtime_config.reset("llm_model")
        unified_agent._pending_llm_switch.pop(CHAT_ID, None)

    async def test_get_returns_default_before_any_switch(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = await _execute_tool("manage_llm_provider", {"action": "get"}, CHAT_ID)
        data = json.loads(result)
        assert "anthropic" in data["result"]
        assert "claude-sonnet-4-6" in data["result"]

    async def test_set_missing_provider_returns_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = await _execute_tool("manage_llm_provider", {"action": "set"}, CHAT_ID)
        data = json.loads(result)
        assert "provider is required" in data["result"].lower()

    async def test_set_unknown_provider_returns_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = await _execute_tool(
            "manage_llm_provider", {"action": "set", "provider": "cohere"}, CHAT_ID
        )
        data = json.loads(result)
        assert "unknown provider" in data["result"].lower()

    async def test_set_without_configured_key_refuses(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(unified_agent.settings, "openai_api_key", "")
        result = await _execute_tool(
            "manage_llm_provider", {"action": "set", "provider": "openai"}, CHAT_ID
        )
        data = json.loads(result)
        assert "OPENAI_API_KEY" in data["result"]
        from organist_bot.runtime_config_store import runtime_config

        assert runtime_config.get("llm_provider", "anthropic") == "anthropic"

    async def test_set_without_model_lists_options_and_does_not_switch(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(unified_agent.settings, "openai_api_key", "sk-test")
        result = await _execute_tool(
            "manage_llm_provider", {"action": "set", "provider": "openai"}, CHAT_ID
        )
        data = json.loads(result)
        assert "gpt-6-astra" in data["result"]
        assert "gpt-5.6-luna" in data["result"]
        from organist_bot.runtime_config_store import runtime_config

        assert runtime_config.get("llm_provider", "anthropic") == "anthropic"

    async def test_set_unknown_model_returns_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(unified_agent.settings, "openai_api_key", "sk-test")
        result = await _execute_tool(
            "manage_llm_provider",
            {"action": "set", "provider": "openai", "model": "gpt-9-fictional"},
            CHAT_ID,
        )
        data = json.loads(result)
        assert "unknown model" in data["result"].lower()

    async def test_set_with_valid_provider_and_model_returns_confirm_buttons(
        self, tmp_path, monkeypatch
    ):
        """set no longer switches immediately -- it stashes the target and
        returns a Confirm/Cancel prompt; the actual switch only happens via
        llm_confirm_switch (called by the deterministic Telegram button
        handler, tested separately below)."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(unified_agent.settings, "openai_api_key", "sk-test")
        result = await _execute_tool(
            "manage_llm_provider",
            {"action": "set", "provider": "openai", "model": "gpt-5.6-luna"},
            CHAT_ID,
        )
        data = json.loads(result)
        assert "openai/gpt-5.6-luna" in data["result"]
        assert data["buttons"] == [
            [
                {"text": "Confirm", "callback_data": "llm:confirm:openai/gpt-5.6-luna"},
                {"text": "Cancel", "callback_data": "llm:cancel:openai/gpt-5.6-luna"},
            ]
        ]
        from organist_bot.runtime_config_store import runtime_config

        assert runtime_config.get("llm_provider", "anthropic") == "anthropic"
        assert unified_agent._pending_llm_switch[CHAT_ID] == ("openai", "gpt-5.6-luna")

    async def test_get_reflects_switch_only_after_confirm(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(unified_agent.settings, "openai_api_key", "sk-test")
        await _execute_tool(
            "manage_llm_provider",
            {"action": "set", "provider": "openai", "model": "gpt-5.6-luna"},
            CHAT_ID,
        )

        before = await _execute_tool("manage_llm_provider", {"action": "get"}, CHAT_ID)
        assert "anthropic" in json.loads(before)["result"]

        ok, _ = unified_agent.llm_confirm_switch(CHAT_ID, "openai", "gpt-5.6-luna")
        assert ok is True

        after = await _execute_tool("manage_llm_provider", {"action": "get"}, CHAT_ID)
        data = json.loads(after)
        assert "openai" in data["result"]
        assert "gpt-5.6-luna" in data["result"] or "openai/gpt-5.6-luna" in data["result"]

    async def test_reset_restores_default(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(unified_agent.settings, "openai_api_key", "sk-test")
        await _execute_tool(
            "manage_llm_provider",
            {"action": "set", "provider": "openai", "model": "gpt-5.6-luna"},
            CHAT_ID,
        )
        unified_agent.llm_confirm_switch(CHAT_ID, "openai", "gpt-5.6-luna")

        result = await _execute_tool("manage_llm_provider", {"action": "reset"}, CHAT_ID)
        data = json.loads(result)
        assert "reset" in data["result"].lower() or "default" in data["result"].lower()
        from organist_bot.runtime_config_store import runtime_config

        assert runtime_config.get("llm_provider", "anthropic") == "anthropic"


class TestLlmConfirmAndCancelSwitch:
    def teardown_method(self):
        from organist_bot.runtime_config_store import runtime_config

        runtime_config.reset("llm_provider")
        runtime_config.reset("llm_model")
        unified_agent._pending_llm_switch.pop(CHAT_ID, None)

    def test_confirm_applies_the_pending_switch(self):
        unified_agent._pending_llm_switch[CHAT_ID] = ("openai", "gpt-5.6-luna")
        ok, message = unified_agent.llm_confirm_switch(CHAT_ID, "openai", "gpt-5.6-luna")
        assert ok is True
        assert "openai/gpt-5.6-luna" in message
        from organist_bot.runtime_config_store import runtime_config

        assert runtime_config.get("llm_provider", "") == "openai"
        assert runtime_config.get("llm_model", "") == "openai/gpt-5.6-luna"
        assert CHAT_ID not in unified_agent._pending_llm_switch

    def test_confirm_with_no_pending_switch_is_a_noop(self):
        ok, message = unified_agent.llm_confirm_switch(CHAT_ID, "openai", "gpt-5.6-luna")
        assert ok is False
        assert "no longer pending" in message.lower()
        from organist_bot.runtime_config_store import runtime_config

        assert runtime_config.get("llm_provider", "anthropic") == "anthropic"

    def test_confirm_with_mismatched_target_does_not_apply_a_different_pending_switch(self):
        """A stale button for an earlier request must not apply a switch the
        user has since overwritten with a newer one."""
        unified_agent._pending_llm_switch[CHAT_ID] = ("gemini", "gemini-pro")
        ok, message = unified_agent.llm_confirm_switch(CHAT_ID, "openai", "gpt-5.6-luna")
        assert ok is False
        assert "no longer pending" in message.lower()
        from organist_bot.runtime_config_store import runtime_config

        assert runtime_config.get("llm_provider", "anthropic") == "anthropic"
        # The still-pending gemini switch is untouched by the stale attempt.
        assert unified_agent._pending_llm_switch[CHAT_ID] == ("gemini", "gemini-pro")

    def test_cancel_discards_the_pending_switch_without_applying_it(self):
        unified_agent._pending_llm_switch[CHAT_ID] = ("openai", "gpt-5.6-luna")
        ok, message = unified_agent.llm_cancel_switch(CHAT_ID, "openai", "gpt-5.6-luna")
        assert ok is True
        assert "cancelled" in message.lower()
        from organist_bot.runtime_config_store import runtime_config

        assert runtime_config.get("llm_provider", "anthropic") == "anthropic"
        assert CHAT_ID not in unified_agent._pending_llm_switch

    def test_cancel_with_mismatched_target_is_a_noop(self):
        unified_agent._pending_llm_switch[CHAT_ID] = ("gemini", "gemini-pro")
        ok, message = unified_agent.llm_cancel_switch(CHAT_ID, "openai", "gpt-5.6-luna")
        assert ok is False
        assert unified_agent._pending_llm_switch[CHAT_ID] == ("gemini", "gemini-pro")


class TestNegDeterministicActions:
    async def test_neg_confirm_send_success(self, neg_store):
        gig_id = _seed_neg_pending()
        with patch("organist_bot.integrations.unified_agent.send_application_email") as mock_send:
            ok, msg = await unified_agent.neg_confirm_send(gig_id)
        assert ok is True
        assert "sent" in msg.lower()
        mock_send.assert_called_once()
        assert application_store._read()[0]["status"] == "applied"

    async def test_neg_confirm_send_unknown_id(self, neg_store):
        ok, msg = await unified_agent.neg_confirm_send("deadbeefcafe")
        assert ok is False
        assert "no draft found" in msg.lower()

    async def test_neg_confirm_send_already_decided(self, neg_store):
        gig_id = _seed_neg_pending()
        application_store.transition_neg_pending(gig_id, to="rejected")
        ok, msg = await unified_agent.neg_confirm_send(gig_id)
        assert ok is False
        assert "already" in msg.lower()

    async def test_neg_confirm_send_failure_keeps_row_pending(self, neg_store):
        gig_id = _seed_neg_pending()
        with patch(
            "organist_bot.integrations.unified_agent.send_application_email",
            side_effect=RuntimeError("smtp down"),
        ):
            ok, msg = await unified_agent.neg_confirm_send(gig_id)
        assert ok is False
        assert "failed" in msg.lower()
        assert application_store._read()[0]["status"] == "neg_pending"

    async def test_neg_confirm_send_reports_sent_when_transition_fails(self, neg_store):
        """If the email send succeeds but recording the transition fails (a
        losing race, or a disk write error), the message must say the email
        was sent — not leave the user thinking it wasn't."""
        gig_id = _seed_neg_pending()
        with (
            patch("organist_bot.integrations.unified_agent.send_application_email"),
            patch(
                "organist_bot.integrations.unified_agent.application_store.transition_neg_pending",
                return_value=False,
            ),
        ):
            ok, msg = await unified_agent.neg_confirm_send(gig_id)
        assert ok is False
        assert "sent to" in msg.lower()
        assert "failed to record" in msg.lower()

    def test_neg_confirm_reject_success(self, neg_store):
        gig_id = _seed_neg_pending()
        ok, msg = unified_agent.neg_confirm_reject(gig_id)
        assert ok is True
        assert "rejected" in msg.lower()
        assert application_store._read()[0]["status"] == "rejected"

    def test_neg_confirm_reject_already_decided(self, neg_store):
        gig_id = _seed_neg_pending()
        application_store.transition_neg_pending(gig_id, to="applied")
        ok, msg = unified_agent.neg_confirm_reject(gig_id)
        assert ok is False

    def test_neg_draft_view_returns_text_and_buttons(self, neg_store):
        gig_id = _seed_neg_pending()
        view = unified_agent.neg_draft_view(gig_id)
        assert view is not None
        text, buttons = view
        assert gig_id in text
        assert buttons == [
            [
                {"text": "✅ Accept", "callback_data": f"neg:accept:{gig_id}"},
                {"text": "✏️ Edit", "callback_data": f"neg:edit:{gig_id}"},
                {"text": "❌ Reject", "callback_data": f"neg:reject:{gig_id}"},
            ]
        ]

    def test_neg_draft_view_none_when_not_pending(self, neg_store):
        assert unified_agent.neg_draft_view("deadbeefcafe") is None


def test_agent_response_buttons_defaults_to_none():
    from organist_bot.integrations.unified_agent import AgentResponse

    assert AgentResponse(text="hi").buttons is None
    assert AgentResponse(text="hi", buttons=[[{"text": "A", "callback_data": "x"}]]).buttons == [
        [{"text": "A", "callback_data": "x"}]
    ]


def test_every_tool_uses_openai_function_calling_shape():
    from organist_bot.integrations.unified_agent import TOOLS

    assert len(TOOLS) > 0
    for tool in TOOLS:
        assert tool["type"] == "function"
        fn = tool["function"]
        assert isinstance(fn["name"], str) and fn["name"]
        assert isinstance(fn["description"], str) and fn["description"]
        assert isinstance(fn["parameters"], dict)
        assert "input_schema" not in tool
        assert "name" not in tool  # top-level — only under "function"


def _fake_tool_call(call_id: str, name: str, arguments: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def _fake_litellm_response(
    content: str | None = None,
    tool_calls: list | None = None,
    usage: SimpleNamespace | None = None,
) -> SimpleNamespace:
    dumped = {
        "role": "assistant",
        "content": content,
        "tool_calls": (
            [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in tool_calls
            ]
            if tool_calls
            else None
        ),
    }
    # Intentionally takes NO kwargs: if process_message() ever calls
    # msg.model_dump(exclude_none=True), this raises TypeError instead of silently
    # dropping the `content: None` key — pinning the history round-trip bug fixed
    # during spec review (see Global Constraints in the plan/spec).
    message = SimpleNamespace(content=content, tool_calls=tool_calls, model_dump=lambda: dumped)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


# ── Responses API support (gpt-6-astra) ─────────────────────────────────────


class TestResponsesToolsShape:
    def test_every_entry_is_flat_function_tool_shape(self):
        for tool in unified_agent.RESPONSES_TOOLS:
            assert tool["type"] == "function"
            assert isinstance(tool["name"], str)
            assert isinstance(tool["description"], str)
            assert isinstance(tool["parameters"], dict)
            assert "function" not in tool

    def test_same_names_and_order_as_TOOLS(self):
        chat_names = [t["function"]["name"] for t in unified_agent.TOOLS]
        responses_names = [t["name"] for t in unified_agent.RESPONSES_TOOLS]
        assert responses_names == chat_names


def test_gpt_6_astra_is_in_responses_api_models():
    assert "openai/gpt-6-astra" in unified_agent._RESPONSES_API_MODELS
    assert "openai/gpt-5.6-luna" not in unified_agent._RESPONSES_API_MODELS


def _fake_responses_function_call(call_id: str, name: str, arguments: dict) -> SimpleNamespace:
    """A Responses API output item of type "function_call" -- mirrors the real
    field names (call_id, name, arguments, type) confirmed against the
    installed openai/litellm packages during spec research."""
    return SimpleNamespace(
        type="function_call", call_id=call_id, name=name, arguments=json.dumps(arguments)
    )


def _fake_responses_message(text: str) -> SimpleNamespace:
    """A Responses API output item of type "message" with one output_text
    content part."""
    return SimpleNamespace(
        type="message",
        content=[SimpleNamespace(type="output_text", text=text)],
    )


def _fake_responses_api_response(
    output: list,
    *,
    response_id: str = "resp_1",
    status: str = "completed",
    error: object = None,
    usage: SimpleNamespace | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(id=response_id, output=output, status=status, error=error, usage=usage)


class TestChatMessageFromResponsesOutput:
    def test_message_only_sets_content_no_tool_calls(self):
        msg = unified_agent._chat_message_from_responses_output(
            [_fake_responses_message("Hello there")]
        )
        assert msg.content == "Hello there"
        assert msg.tool_calls is None
        assert msg.model_dump() == {
            "role": "assistant",
            "content": "Hello there",
            "tool_calls": None,
        }

    def test_function_call_sets_tool_calls_content_none(self):
        msg = unified_agent._chat_message_from_responses_output(
            [_fake_responses_function_call("call_abc123", "add_gig", {"url": "https://x"})]
        )
        assert msg.content is None
        assert len(msg.tool_calls) == 1
        tc = msg.tool_calls[0]
        # Critical: .id must be the item's call_id (call_...), not its id (fc_...)
        # -- call_id is what a matching function_call_output must reference.
        assert tc.id == "call_abc123"
        assert tc.function.name == "add_gig"
        assert json.loads(tc.function.arguments) == {"url": "https://x"}
        assert msg.model_dump() == {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_abc123",
                    "type": "function",
                    "function": {
                        "name": "add_gig",
                        "arguments": json.dumps({"url": "https://x"}),
                    },
                }
            ],
        }

    def test_multiple_function_calls_all_captured(self):
        msg = unified_agent._chat_message_from_responses_output(
            [
                _fake_responses_function_call("call_1", "tool_a", {}),
                _fake_responses_function_call("call_2", "tool_b", {}),
            ]
        )
        assert [tc.id for tc in msg.tool_calls] == ["call_1", "call_2"]


class TestChatResponseFromResponsesApi:
    def test_usage_translated_to_prompt_and_completion_tokens(self):
        response = _fake_responses_api_response(
            output=[_fake_responses_message("hi")],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )
        chat_response = unified_agent._chat_response_from_responses_api(response)
        assert chat_response.usage.prompt_tokens == 10
        assert chat_response.usage.completion_tokens == 5

    def test_none_usage_stays_none(self):
        response = _fake_responses_api_response(output=[_fake_responses_message("hi")], usage=None)
        chat_response = unified_agent._chat_response_from_responses_api(response)
        assert chat_response.usage is None

    def test_message_reachable_via_choices_zero(self):
        response = _fake_responses_api_response(output=[_fake_responses_message("hi")])
        chat_response = unified_agent._chat_response_from_responses_api(response)
        assert chat_response.choices[0].message.content == "hi"


class TestResponsesInputFromMessages:
    def test_user_text_becomes_user_message_item(self):
        items = unified_agent._responses_input_from_messages([{"role": "user", "content": "hello"}])
        assert items == [{"type": "message", "role": "user", "content": "hello"}]

    def test_assistant_text_only_becomes_assistant_message_item(self):
        items = unified_agent._responses_input_from_messages(
            [{"role": "assistant", "content": "sure thing", "tool_calls": None}]
        )
        assert items == [{"type": "message", "role": "assistant", "content": "sure thing"}]

    def test_tool_call_round_trip_is_dropped(self):
        messages = [
            {"role": "user", "content": "add this gig"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1"}]},
            {"role": "tool", "tool_call_id": "call_1", "name": "add_gig", "content": "{}"},
        ]
        items = unified_agent._responses_input_from_messages(messages)
        assert items == [{"type": "message", "role": "user", "content": "add this gig"}]

    def test_assistant_message_with_both_content_and_tool_calls_keeps_the_text(self):
        messages = [
            {
                "role": "assistant",
                "content": "Let me check that.",
                "tool_calls": [{"id": "call_1"}],
            },
        ]
        items = unified_agent._responses_input_from_messages(messages)
        assert items == [{"type": "message", "role": "assistant", "content": "Let me check that."}]

    def test_dangling_unresolved_tool_calls_message_is_tolerated(self):
        """Simulates a crash mid-turn on a previous call leaving an assistant
        tool_calls message with no matching tool result at all -- the
        translator must not assume turns are always cleanly resolved."""
        messages = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1"}]},
            {"role": "user", "content": "second"},
        ]
        items = unified_agent._responses_input_from_messages(messages)
        assert items == [
            {"type": "message", "role": "user", "content": "first"},
            {"type": "message", "role": "user", "content": "second"},
        ]


class TestResponsesToolOutputsFromMessages:
    def test_translates_tool_messages_to_function_call_output_items(self):
        new_messages = [
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1"}]},
            {"role": "tool", "tool_call_id": "call_1", "name": "add_gig", "content": '{"ok":true}'},
        ]
        items = unified_agent._responses_tool_outputs_from_messages(new_messages)
        assert items == [
            {"type": "function_call_output", "call_id": "call_1", "output": '{"ok":true}'}
        ]

    def test_multiple_tool_results_all_translated(self):
        new_messages = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_1"}, {"id": "call_2"}],
            },
            {"role": "tool", "tool_call_id": "call_1", "name": "a", "content": "1"},
            {"role": "tool", "tool_call_id": "call_2", "name": "b", "content": "2"},
        ]
        items = unified_agent._responses_tool_outputs_from_messages(new_messages)
        assert items == [
            {"type": "function_call_output", "call_id": "call_1", "output": "1"},
            {"type": "function_call_output", "call_id": "call_2", "output": "2"},
        ]


class TestCallOpenaiResponsesApi:
    async def test_first_call_bootstraps_from_history_without_system_message(self, monkeypatch):
        import litellm

        messages = [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "hello"},
        ]
        fake_response = _fake_responses_api_response(
            output=[_fake_responses_message("hi there")], response_id="resp_A"
        )
        mock_aresponses = AsyncMock(return_value=fake_response)
        monkeypatch.setattr(litellm, "aresponses", mock_aresponses)

        session: dict = {}
        result = await unified_agent._call_openai_responses_api(
            model="openai/gpt-6-astra",
            api_key="sk-test",
            messages=messages,
            responses_session=session,
        )

        assert result.choices[0].message.content == "hi there"
        call_kwargs = mock_aresponses.call_args.kwargs
        assert call_kwargs["previous_response_id"] is None
        assert call_kwargs["store"] is True
        assert call_kwargs["input"] == [{"type": "message", "role": "user", "content": "hello"}]
        assert call_kwargs["instructions"] == unified_agent.SYSTEM_PROMPT
        assert call_kwargs["tools"] == unified_agent.RESPONSES_TOOLS
        assert session["previous_response_id"] == "resp_A"
        assert session["synced_len"] == len(messages)

    async def test_second_call_chains_via_previous_response_id_with_only_new_tool_outputs(
        self, monkeypatch
    ):
        import litellm

        first_messages = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "hi"}]
        session: dict = {"previous_response_id": "resp_A", "synced_len": len(first_messages)}
        second_messages = [
            *first_messages,
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1"}]},
            {"role": "tool", "tool_call_id": "call_1", "name": "add_gig", "content": '{"ok":1}'},
        ]
        fake_response = _fake_responses_api_response(
            output=[_fake_responses_message("done")], response_id="resp_B"
        )
        mock_aresponses = AsyncMock(return_value=fake_response)
        monkeypatch.setattr(litellm, "aresponses", mock_aresponses)

        await unified_agent._call_openai_responses_api(
            model="openai/gpt-6-astra",
            api_key="sk-test",
            messages=second_messages,
            responses_session=session,
        )

        call_kwargs = mock_aresponses.call_args.kwargs
        assert call_kwargs["previous_response_id"] == "resp_A"
        assert call_kwargs["input"] == [
            {"type": "function_call_output", "call_id": "call_1", "output": '{"ok":1}'}
        ]
        assert session["previous_response_id"] == "resp_B"
        assert session["synced_len"] == len(second_messages)

    async def test_incomplete_status_raises_and_does_not_mutate_session(self, monkeypatch):
        import litellm

        fake_response = _fake_responses_api_response(
            output=[], response_id="resp_C", status="incomplete"
        )
        monkeypatch.setattr(litellm, "aresponses", AsyncMock(return_value=fake_response))

        session: dict = {}
        with pytest.raises(unified_agent.ResponsesApiError):
            await unified_agent._call_openai_responses_api(
                model="openai/gpt-6-astra",
                api_key="sk-test",
                messages=[{"role": "system", "content": "SYS"}, {"role": "user", "content": "hi"}],
                responses_session=session,
            )
        assert session == {}

    async def test_populated_error_field_raises_even_if_status_completed(self, monkeypatch):
        import litellm

        fake_response = _fake_responses_api_response(
            output=[], response_id="resp_D", status="completed", error={"message": "boom"}
        )
        monkeypatch.setattr(litellm, "aresponses", AsyncMock(return_value=fake_response))

        session: dict = {}
        with pytest.raises(unified_agent.ResponsesApiError):
            await unified_agent._call_openai_responses_api(
                model="openai/gpt-6-astra",
                api_key="sk-test",
                messages=[{"role": "system", "content": "SYS"}, {"role": "user", "content": "hi"}],
                responses_session=session,
            )
        assert session == {}


# ── LLM provider failover cascade ────────────────────────────────────────────


class TestCallLlmWithFailover:
    def teardown_method(self):
        from organist_bot.runtime_config_store import runtime_config

        runtime_config.reset("llm_provider")
        runtime_config.reset("llm_model")

    async def test_success_on_first_try_does_not_touch_runtime_config(self, tmp_path, monkeypatch):
        import litellm

        monkeypatch.chdir(tmp_path)
        response = _fake_litellm_response(content="ok")
        monkeypatch.setattr(litellm, "acompletion", AsyncMock(return_value=response))

        result, provider, model = await unified_agent._call_llm_with_failover(
            "anthropic", "anthropic/claude-sonnet-4-6", messages=[], tools=[]
        )

        assert result is response
        assert provider == "anthropic"
        assert model == "anthropic/claude-sonnet-4-6"
        from organist_bot.runtime_config_store import runtime_config

        assert runtime_config.get("llm_provider", "unset") == "unset"

    async def test_uses_max_completion_tokens_not_max_tokens(self, tmp_path, monkeypatch):
        """max_tokens breaks on some providers' newer models (e.g. OpenAI's
        reasoning-style models reject it and require max_completion_tokens);
        litellm accepts the latter as a generic name and translates it for
        every provider here, so it must be the one actually sent."""
        import litellm

        monkeypatch.chdir(tmp_path)
        mock_acompletion = AsyncMock(return_value=_fake_litellm_response(content="ok"))
        monkeypatch.setattr(litellm, "acompletion", mock_acompletion)

        await unified_agent._call_llm_with_failover(
            "anthropic", "anthropic/claude-sonnet-4-6", messages=[], tools=[]
        )

        call_kwargs = mock_acompletion.call_args.kwargs
        assert call_kwargs["max_completion_tokens"] == 4096
        assert "max_tokens" not in call_kwargs

    async def test_failure_then_success_promotes_the_working_provider(self, tmp_path, monkeypatch):
        import litellm

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(unified_agent.settings, "openai_api_key", "sk-test")
        monkeypatch.setattr(unified_agent.settings, "gemini_api_key", "")

        good_response = _fake_litellm_response(content="ok")
        monkeypatch.setattr(
            litellm,
            "acompletion",
            AsyncMock(side_effect=[RuntimeError("anthropic is down"), good_response]),
        )
        alerts: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            unified_agent.alert, "send_alert", lambda msg, **kw: alerts.append((msg, kw))
        )

        result, provider, model = await unified_agent._call_llm_with_failover(
            "anthropic", "anthropic/claude-sonnet-4-6", messages=[], tools=[]
        )

        assert result is good_response
        assert provider == "openai"
        assert model == "openai/gpt-5.6-luna"
        from organist_bot.runtime_config_store import runtime_config

        assert runtime_config.get("llm_provider", "") == "openai"
        assert runtime_config.get("llm_model", "") == "openai/gpt-5.6-luna"
        assert len(alerts) == 1
        message, kwargs = alerts[0]
        assert message == (
            "🔀 *AI provider auto\\-switched*\nanthropic → openai\n\nReason: anthropic is down"
        )
        assert kwargs == {"parse_mode": "MarkdownV2"}

    async def test_skips_providers_without_a_configured_api_key(self, tmp_path, monkeypatch):
        import litellm

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(unified_agent.settings, "openai_api_key", "")
        monkeypatch.setattr(unified_agent.settings, "gemini_api_key", "sk-test")

        good_response = _fake_litellm_response(content="ok")
        calls: list[str] = []

        async def fake_acompletion(*, model, **kwargs):
            calls.append(model)
            if model == "anthropic/claude-sonnet-4-6":
                raise RuntimeError("down")
            return good_response

        monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

        result, provider, model = await unified_agent._call_llm_with_failover(
            "anthropic", "anthropic/claude-sonnet-4-6", messages=[], tools=[]
        )

        assert provider == "gemini"
        # openai was skipped entirely -- no key configured, never attempted.
        assert calls == ["anthropic/claude-sonnet-4-6", "gemini/gemini-3.1-pro-preview"]

    async def test_all_providers_failing_raises_a_summary_and_alerts(self, tmp_path, monkeypatch):
        """When every configured provider fails, the raised error must
        summarize every attempt (not just re-raise whichever failed last,
        which would hide that failover was even tried) and an alert must go
        out immediately, since there's no later success to alert about."""
        import litellm

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(unified_agent.settings, "openai_api_key", "sk-test")
        monkeypatch.setattr(unified_agent.settings, "gemini_api_key", "")

        monkeypatch.setattr(
            litellm,
            "acompletion",
            AsyncMock(
                side_effect=[
                    RuntimeError("anthropic is down"),
                    RuntimeError("openai is down"),
                ]
            ),
        )
        alerts: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            unified_agent.alert, "send_alert", lambda msg, **kw: alerts.append((msg, kw))
        )

        with pytest.raises(RuntimeError) as exc_info:
            await unified_agent._call_llm_with_failover(
                "anthropic", "anthropic/claude-sonnet-4-6", messages=[], tools=[]
            )

        assert "All configured providers failed" in str(exc_info.value)
        assert "anthropic: anthropic is down" in str(exc_info.value)
        assert "openai: openai is down" in str(exc_info.value)
        assert isinstance(exc_info.value.__cause__, RuntimeError)
        assert str(exc_info.value.__cause__) == "openai is down"

        from organist_bot.runtime_config_store import runtime_config

        assert runtime_config.get("llm_provider", "unset") == "unset"
        assert len(alerts) == 1
        message, kwargs = alerts[0]
        assert "All configured AI providers failed" in message
        assert "anthropic: anthropic is down" in message
        assert "openai: openai is down" in message
        assert kwargs == {"parse_mode": "MarkdownV2"}

    async def test_all_providers_failing_truncates_long_reasons(self, tmp_path, monkeypatch):
        import litellm

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(unified_agent.settings, "openai_api_key", "")
        monkeypatch.setattr(unified_agent.settings, "gemini_api_key", "")

        monkeypatch.setattr(litellm, "acompletion", AsyncMock(side_effect=RuntimeError("x" * 1000)))
        alerts: list[str] = []
        monkeypatch.setattr(unified_agent.alert, "send_alert", lambda msg, **kw: alerts.append(msg))

        with pytest.raises(RuntimeError) as exc_info:
            await unified_agent._call_llm_with_failover(
                "anthropic", "anthropic/claude-sonnet-4-6", messages=[], tools=[]
            )

        assert len(str(exc_info.value)) < 400
        assert str(exc_info.value).endswith("…")
        assert len(alerts[0]) < 400

    async def test_records_usage_on_success(self, tmp_path, monkeypatch):
        import litellm

        monkeypatch.chdir(tmp_path)
        response = _fake_litellm_response(
            content="ok", usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5)
        )
        monkeypatch.setattr(litellm, "acompletion", AsyncMock(return_value=response))

        await unified_agent._call_llm_with_failover(
            "anthropic", "anthropic/claude-sonnet-4-6", messages=[], tools=[]
        )

        from organist_bot import llm_usage_store

        summary = llm_usage_store.summary()
        assert summary["anthropic"]["call_count"] == 1
        assert summary["anthropic"]["prompt_tokens"] == 10
        assert summary["anthropic"]["completion_tokens"] == 5


class TestGetLlmUsageSummary:
    async def test_no_usage_recorded_yet(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = await _execute_tool("get_llm_usage_summary", {}, CHAT_ID)
        data = json.loads(result)
        assert "no llm usage" in data["result"].lower()

    async def test_summarizes_recorded_usage_by_provider(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from organist_bot import llm_usage_store

        llm_usage_store.record_call("anthropic", "anthropic/claude-sonnet-4-6", 100, 50)
        llm_usage_store.record_call("openai", "openai/gpt-5.6-luna", 10, 5)

        result = await _execute_tool("get_llm_usage_summary", {}, CHAT_ID)
        data = json.loads(result)
        assert "anthropic" in data["result"]
        assert "openai" in data["result"]
        assert "150" in data["result"]  # anthropic total tokens
        assert "15" in data["result"]  # openai total tokens


# ── process_message on_step progress reporting ──────────────────────────────


@pytest.mark.asyncio
async def test_process_message_reports_on_step_progress(tmp_path, monkeypatch):
    """process_message must report a 🔧 step when a tool call starts and flip
    it to ✅ once the tool call returns, via the on_step callback."""
    import litellm

    from organist_bot.integrations import agent_state, unified_agent

    monkeypatch.setattr(agent_state, "_PATH", tmp_path / "agent_state.json")
    cid = 314159
    unified_agent._hydrated.discard(cid)

    tool_use_response = _fake_litellm_response(
        tool_calls=[_fake_tool_call("tool_1", "add_gig", {"url": "https://example.com/gig/1"})]
    )
    end_turn_response = _fake_litellm_response(content="Added the gig.")

    monkeypatch.setattr(
        litellm, "acompletion", AsyncMock(side_effect=[tool_use_response, end_turn_response])
    )
    monkeypatch.setattr(
        unified_agent, "_execute_tool", AsyncMock(return_value=json.dumps({"result": "ok"}))
    )

    steps: list[str] = []

    async def on_step(status_text: str) -> None:
        steps.append(status_text)

    try:
        responses = await unified_agent.process_message(cid, "add this gig", on_step=on_step)
        # Confirms the history round-trip: the assistant's tool-call turn keeps an
        # explicit content: None (not a dropped key), and the tool result landed as
        # a flat role="tool" message — both required for the Anthropic backend to
        # accept the next turn.
        history = unified_agent._histories[cid]
        assistant_turn = next(m for m in history if m["role"] == "assistant")
        assert assistant_turn["content"] is None
        assert "tool_calls" in assistant_turn
        tool_turn = next(m for m in history if m["role"] == "tool")
        assert tool_turn["tool_call_id"] == "tool_1"
    finally:
        unified_agent._histories.pop(cid, None)
        unified_agent._hydrated.discard(cid)

    assert steps == ["🔧 add_gig", "✅ add_gig"]
    assert responses == [unified_agent.AgentResponse(text="Added the gig.")]


@pytest.mark.asyncio
async def test_process_message_without_on_step_is_unaffected(tmp_path, monkeypatch):
    """Omitting on_step (the default) must not change existing behavior."""
    import litellm

    from organist_bot.integrations import agent_state, unified_agent

    monkeypatch.setattr(agent_state, "_PATH", tmp_path / "agent_state.json")
    cid = 271828
    unified_agent._hydrated.discard(cid)

    end_turn_response = _fake_litellm_response(content="All set.")
    monkeypatch.setattr(litellm, "acompletion", AsyncMock(return_value=end_turn_response))

    try:
        responses = await unified_agent.process_message(cid, "hello")
    finally:
        unified_agent._histories.pop(cid, None)
        unified_agent._hydrated.discard(cid)

    assert responses == [unified_agent.AgentResponse(text="All set.")]


@pytest.mark.asyncio
async def test_process_message_stale_provider_resets_to_default(tmp_path, monkeypatch):
    """A stale/invalid llm_provider in runtime_config must reset to the default
    instead of crashing — the guard at the top of process_message() handles this."""
    import litellm

    from organist_bot.integrations import agent_state, unified_agent
    from organist_bot.runtime_config_store import runtime_config

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agent_state, "_PATH", tmp_path / "agent_state.json")
    cid = 161803
    unified_agent._hydrated.discard(cid)

    # Plant a provider name that is not in _PROVIDER_MODELS.
    runtime_config.set("llm_provider", "cohere")

    end_turn_response = _fake_litellm_response(content="ok")
    monkeypatch.setattr(litellm, "acompletion", AsyncMock(return_value=end_turn_response))

    try:
        responses = await unified_agent.process_message(cid, "hello")
    finally:
        unified_agent._histories.pop(cid, None)
        unified_agent._hydrated.discard(cid)
        runtime_config.reset("llm_provider")
        runtime_config.reset("llm_model")

    assert responses == [unified_agent.AgentResponse(text="ok")]
    assert runtime_config.get("llm_provider", "anthropic") == "anthropic"


# ── process_message NEG buttons/picker plumbing ─────────────────────────────


@pytest.mark.asyncio
async def test_process_message_passes_through_tool_buttons(tmp_path, monkeypatch):
    import litellm

    from organist_bot.integrations import agent_state, unified_agent

    monkeypatch.setattr(agent_state, "_PATH", tmp_path / "agent_state.json")
    cid = 424242
    unified_agent._hydrated.discard(cid)

    tool_use_response = _fake_litellm_response(
        tool_calls=[_fake_tool_call("t1", "approve_neg_application", {"gig_id": "abc123"})]
    )
    end_turn_response = _fake_litellm_response(content="ok")

    monkeypatch.setattr(
        litellm, "acompletion", AsyncMock(side_effect=[tool_use_response, end_turn_response])
    )

    buttons = [[{"text": "Confirm", "callback_data": "neg:confirm_send:abc123"}]]
    monkeypatch.setattr(
        unified_agent,
        "_execute_tool",
        AsyncMock(return_value=json.dumps({"result": "Will send.", "buttons": buttons})),
    )

    try:
        responses = await unified_agent.process_message(cid, "approve abc123")
    finally:
        unified_agent._histories.pop(cid, None)
        unified_agent._hydrated.discard(cid)

    assert responses[0].buttons == buttons


@pytest.mark.asyncio
async def test_process_message_stashes_instruction_on_needs_pick(tmp_path, monkeypatch):
    import litellm

    from organist_bot.integrations import agent_state, unified_agent

    monkeypatch.setattr(agent_state, "_PATH", tmp_path / "agent_state.json")
    cid = 535353
    unified_agent._hydrated.discard(cid)
    unified_agent._pending_neg_instruction.pop(cid, None)

    tool_use_response = _fake_litellm_response(
        tool_calls=[_fake_tool_call("t1", "approve_neg_application", {})]
    )
    end_turn_response = _fake_litellm_response(content="ok")

    monkeypatch.setattr(
        litellm, "acompletion", AsyncMock(side_effect=[tool_use_response, end_turn_response])
    )

    picker_buttons = [[{"text": "A", "callback_data": "neg:pick:aaa"}]]
    monkeypatch.setattr(
        unified_agent,
        "_execute_tool",
        AsyncMock(
            return_value=json.dumps(
                {"result": "Which draft?", "buttons": picker_buttons, "needs_pick": True}
            )
        ),
    )

    try:
        responses = await unified_agent.process_message(cid, "approve it")
        assert unified_agent.pop_pending_neg_instruction(cid) == "approve it"
    finally:
        unified_agent._histories.pop(cid, None)
        unified_agent._hydrated.discard(cid)
        unified_agent._pending_neg_instruction.pop(cid, None)

    assert responses[0].buttons == picker_buttons


def test_settings_has_openai_and_gemini_api_key_fields(monkeypatch):
    """Verifies the fields default to "" when unset. Needs BOTH _env_file=None
    (skip re-reading this machine's real .env) AND delenv of the two vars:
    litellm.__init__ calls load_dotenv() on import as a side effect, which
    (once any test in this session has imported litellm) has already copied
    this project's real .env into the process's actual os.environ -- and
    pydantic-settings reads os.environ regardless of _env_file."""
    from organist_bot.config import Settings

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    s = Settings(_env_file=None, email_sender="a@b.com", email_password="x", cc_email="a@b.com")
    assert s.openai_api_key == ""
    assert s.gemini_api_key == ""
