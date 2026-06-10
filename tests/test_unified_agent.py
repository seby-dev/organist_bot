"""Tests for unified_agent._execute_tool and supporting utilities."""

import datetime
import json
from unittest.mock import MagicMock, patch

import pytest

from organist_bot.integrations.unified_agent import (
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
        from unittest.mock import AsyncMock

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


# ── get_gig_stats ─────────────────────────────────────────────────────────────


class TestGetGigStats:
    @pytest.mark.asyncio
    async def test_returns_formatted_stats(self):
        """Successful Sheets query returns formatted pipeline summary."""
        runs = [
            {
                "run_id": "abc",
                "timestamp": "2026-05-17T10:00:00.000Z",
                "listed": 20,
                "pre_filter_passed": 3,
                "valid": 1,
                "gig_errors": 0,
                "elapsed_ms": 800,
                "filter_breakdown": {"SeenFilter": 12, "FeeFilter": 4},
            },
            {
                "run_id": "def",
                "timestamp": "2026-05-16T10:00:00.000Z",
                "listed": 18,
                "pre_filter_passed": 2,
                "valid": 0,
                "gig_errors": 0,
                "elapsed_ms": 600,
                "filter_breakdown": {"SeenFilter": 10},
            },
        ]
        mock_sl = MagicMock()
        mock_sl.query_run_stats.return_value = runs
        with patch(
            "organist_bot.integrations.unified_agent._make_sheets_logger",
            return_value=mock_sl,
        ):
            result = await _execute_tool("get_gig_stats", {"days": 7}, CHAT_ID)
        data = json.loads(result)
        assert "result" in data
        text = data["result"]
        assert "Runs:* 2" in text
        assert "Listed:" in text
        assert "Pre-filter:" in text
        assert "Valid:" in text
        assert "SeenFilter" in text
        assert "Recent runs" in text

    @pytest.mark.asyncio
    async def test_sheets_not_configured_returns_message(self):
        """Returns a clear message when Sheets is not configured."""
        with patch(
            "organist_bot.integrations.unified_agent._make_sheets_logger",
            return_value=None,
        ):
            result = await _execute_tool("get_gig_stats", {}, CHAT_ID)
        data = json.loads(result)
        assert "result" in data
        assert "not configured" in data["result"].lower()

    @pytest.mark.asyncio
    async def test_sheets_api_error_returns_message(self):
        """Returns a clear message when the Sheets API call fails."""
        mock_sl = MagicMock()
        mock_sl.query_run_stats.side_effect = RuntimeError("network failure")
        with patch(
            "organist_bot.integrations.unified_agent._make_sheets_logger",
            return_value=mock_sl,
        ):
            result = await _execute_tool("get_gig_stats", {"days": 7}, CHAT_ID)
        data = json.loads(result)
        assert "result" in data
        assert "Google Sheets" in data["result"]

    @pytest.mark.asyncio
    async def test_no_data_in_window_returns_message(self):
        """Returns a clear message when there are no runs in the requested window."""
        mock_sl = MagicMock()
        mock_sl.query_run_stats.return_value = []
        with patch(
            "organist_bot.integrations.unified_agent._make_sheets_logger",
            return_value=mock_sl,
        ):
            result = await _execute_tool("get_gig_stats", {"days": 7}, CHAT_ID)
        data = json.loads(result)
        assert "result" in data
        assert "No pipeline runs" in data["result"]

    @pytest.mark.asyncio
    async def test_days_clamped_to_range(self):
        """days is clamped to 1–90."""
        mock_sl = MagicMock()
        mock_sl.query_run_stats.return_value = []
        with patch(
            "organist_bot.integrations.unified_agent._make_sheets_logger",
            return_value=mock_sl,
        ):
            await _execute_tool("get_gig_stats", {"days": 999}, CHAT_ID)
        mock_sl.query_run_stats.assert_called_once_with(90)


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

    async def test_approve_without_confirmed_returns_preview(self, neg_store):
        gig_id = _seed_neg_pending()
        out = json.loads(await _TOOL_HANDLERS["approve_neg_application"]({"gig_id": gig_id}, 1))
        assert "confirm" in out["result"].lower()
        assert application_store._read()[0]["status"] == "neg_pending"

    async def test_approve_confirmed_sends_and_transitions(self, neg_store):
        gig_id = _seed_neg_pending()
        with patch("organist_bot.integrations.unified_agent.send_application_email") as mock_send:
            out = json.loads(
                await _TOOL_HANDLERS["approve_neg_application"](
                    {"gig_id": gig_id, "confirmed": True}, 1
                )
            )
        assert "sent" in out["result"].lower()
        mock_send.assert_called_once()
        assert mock_send.call_args.kwargs["recipient"] == "jane@example.com"
        assert application_store._read()[0]["status"] == "applied"

    async def test_approve_unknown_gig_id_returns_error(self, neg_store):
        out = json.loads(
            await _TOOL_HANDLERS["approve_neg_application"](
                {"gig_id": "deadbeefcafe", "confirmed": True}, 1
            )
        )
        assert "no draft found" in out["result"].lower()

    async def test_approve_already_applied_returns_already(self, neg_store):
        gig_id = _seed_neg_pending()
        with patch("organist_bot.integrations.unified_agent.send_application_email"):
            await _TOOL_HANDLERS["approve_neg_application"](
                {"gig_id": gig_id, "confirmed": True}, 1
            )
            out = json.loads(
                await _TOOL_HANDLERS["approve_neg_application"](
                    {"gig_id": gig_id, "confirmed": True}, 1
                )
            )
        assert "already" in out["result"].lower()

    async def test_approve_send_failure_keeps_row_pending(self, neg_store):
        gig_id = _seed_neg_pending()
        with patch(
            "organist_bot.integrations.unified_agent.send_application_email",
            side_effect=RuntimeError("smtp down"),
        ):
            out = json.loads(
                await _TOOL_HANDLERS["approve_neg_application"](
                    {"gig_id": gig_id, "confirmed": True}, 1
                )
            )
        assert "failed" in out["result"].lower()
        assert application_store._read()[0]["status"] == "neg_pending"

    async def test_edit_with_new_body_confirmed_sends_edited(self, neg_store):
        gig_id = _seed_neg_pending()
        with patch("organist_bot.integrations.unified_agent.send_application_email") as mock_send:
            await _TOOL_HANDLERS["edit_neg_application"](
                {"gig_id": gig_id, "new_body": "<p>EDITED</p>", "confirmed": True}, 1
            )
        assert "EDITED" in mock_send.call_args.kwargs["body"]
        r = application_store._read()[0]
        assert r["status"] == "applied"
        assert "EDITED" in r["draft_body"]

    async def test_edit_requires_new_body_or_new_fee(self, neg_store):
        gig_id = _seed_neg_pending()
        out = json.loads(await _TOOL_HANDLERS["edit_neg_application"]({"gig_id": gig_id}, 1))
        assert "new_body or new_fee" in out["result"]

    async def test_edit_with_new_fee_rerenders(self, neg_store):
        gig_id = _seed_neg_pending()
        with patch("organist_bot.integrations.unified_agent.send_application_email") as mock_send:
            await _TOOL_HANDLERS["edit_neg_application"](
                {"gig_id": gig_id, "new_fee": 150, "confirmed": True}, 1
            )
        assert "£150" in mock_send.call_args.kwargs["body"]

    async def test_reject_confirmed_skips_send(self, neg_store):
        gig_id = _seed_neg_pending()
        with patch("organist_bot.integrations.unified_agent.send_application_email") as mock_send:
            out = json.loads(
                await _TOOL_HANDLERS["reject_neg_application"](
                    {"gig_id": gig_id, "confirmed": True}, 1
                )
            )
        assert "rejected" in out["result"].lower()
        mock_send.assert_not_called()
        assert application_store._read()[0]["status"] == "rejected"
