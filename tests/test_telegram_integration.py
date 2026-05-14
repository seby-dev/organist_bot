"""Tests for the unified Telegram bot handlers."""

import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from organist_bot.integrations.invoice_agent import AgentResponse
from organist_bot.integrations.telegram_bot import (
    _gig_listing,
    _is_authorised,
    addgig_entry,
    cmd_deletegig,
    cmd_gigs,
    handle_invoice,
)
from organist_bot.models import Gig

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_update(chat_id: int = 7973955362, text: str = "") -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def _make_gig(**overrides) -> Gig:
    defaults = dict(
        header="Sunday Service",
        organisation="St Paul's",
        locality="London",
        date="Sunday, March 1, 2026",
        time="10:00 AM",
        fee="£120",
        link="https://organistsonline.org/required/test",
    )
    defaults.update(overrides)
    return Gig(**defaults)


# ── _is_authorised ────────────────────────────────────────────────────────────


class TestIsAuthorised:
    def test_authorised_chat_id(self):
        update = _make_update(chat_id=7973955362)
        with patch("organist_bot.integrations.telegram_bot.settings") as mock_settings:
            mock_settings.telegram_chat_id = "7973955362"
            assert _is_authorised(update) is True

    def test_wrong_chat_id_rejected(self):
        update = _make_update(chat_id=9999999)
        with patch("organist_bot.integrations.telegram_bot.settings") as mock_settings:
            mock_settings.telegram_chat_id = "7973955362"
            assert _is_authorised(update) is False

    def test_string_vs_int_comparison(self):
        """Chat IDs from Telegram are ints; settings stores them as strings."""
        update = _make_update(chat_id=12345)
        with patch("organist_bot.integrations.telegram_bot.settings") as mock_settings:
            mock_settings.telegram_chat_id = "12345"
            assert _is_authorised(update) is True


# ── handle_invoice ────────────────────────────────────────────────────────────


class TestHandleInvoice:
    @pytest.fixture(autouse=True)
    def patch_settings(self):
        with patch("organist_bot.integrations.telegram_bot.settings") as mock:
            mock.telegram_chat_id = "7973955362"
            yield mock

    @pytest.mark.asyncio
    async def test_rejects_unauthorised_chat(self):
        update = _make_update(chat_id=9999)
        with patch("organist_bot.integrations.invoice_agent.process_message") as mock_pm:
            await handle_invoice(update, MagicMock())
        mock_pm.assert_not_called()
        update.message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_sends_text_response(self):
        update = _make_update(text="List my clients")
        responses = [AgentResponse(text="You have 3 clients.")]
        with patch(
            "organist_bot.integrations.invoice_agent.process_message",
            new=AsyncMock(return_value=responses),
        ):
            await handle_invoice(update, MagicMock())
        update.message.reply_text.assert_called_once_with(
            "You have 3 clients.", parse_mode="Markdown"
        )

    @pytest.mark.asyncio
    async def test_sends_file_response(self):
        update = _make_update(text="Generate invoice for holy-cross")
        context = MagicMock()
        context.bot.send_document = AsyncMock()

        import os
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            responses = [AgentResponse(file_path=tmp_path, file_caption="Invoice")]
            with patch(
                "organist_bot.integrations.invoice_agent.process_message",
                new=AsyncMock(return_value=responses),
            ):
                await handle_invoice(update, context)
            context.bot.send_document.assert_called_once()
            _, kwargs = context.bot.send_document.call_args
            assert (
                kwargs.get("caption") == "Invoice"
                or context.bot.send_document.call_args[1].get("caption") == "Invoice"
            )
        finally:
            os.unlink(tmp_path)

    @pytest.mark.asyncio
    async def test_handles_agent_error(self):
        update = _make_update(text="crash please")
        with patch(
            "organist_bot.integrations.invoice_agent.process_message",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            await handle_invoice(update, MagicMock())
        reply = update.message.reply_text.call_args[0][0]
        assert "❌" in reply or "error" in reply.lower()


# ── addgig_entry ──────────────────────────────────────────────────────────────


class TestAddGigEntry:
    @pytest.fixture(autouse=True)
    def patch_settings(self):
        with patch("organist_bot.integrations.telegram_bot.settings") as mock:
            mock.telegram_chat_id = "7973955362"
            mock.google_calendar_credentials_file = "fake.json"
            mock.google_calendar_id = "cal@test.com"
            yield mock

    @pytest.mark.asyncio
    async def test_rejects_unauthorised(self):
        update = _make_update(chat_id=9999)
        context = MagicMock()
        context.args = []
        from telegram.ext import ConversationHandler

        result = await addgig_entry(update, context)
        assert result == ConversationHandler.END

    @pytest.mark.asyncio
    async def test_any_arg_forwarded_to_agent(self):
        """Any argument — including a non-organistsonline URL — is forwarded to the agent."""
        update = _make_update()
        context = MagicMock()
        context.args = ["https://example.com/some-gig"]
        from organist_bot.integrations.gig_agent import GigAgentResponse
        from organist_bot.integrations.telegram_bot import CHATTING

        agent_resp = GigAgentResponse(text="What's the organisation?", done=False)
        with patch(
            "organist_bot.integrations.gig_agent.process_message",
            new=AsyncMock(return_value=agent_resp),
        ) as mock_pm:
            result = await addgig_entry(update, context)

        mock_pm.assert_called_once()
        call_args = mock_pm.call_args
        assert "https://example.com/some-gig" in call_args[0][1]
        assert result == CHATTING

    @pytest.mark.asyncio
    async def test_no_args_starts_agent_conversation(self):
        update = _make_update()
        context = MagicMock()
        context.args = []
        from organist_bot.integrations.gig_agent import GigAgentResponse
        from organist_bot.integrations.telegram_bot import CHATTING

        agent_resp = GigAgentResponse(text="What's the gig title?", done=False)
        with patch(
            "organist_bot.integrations.gig_agent.process_message",
            new=AsyncMock(return_value=agent_resp),
        ):
            result = await addgig_entry(update, context)

        assert result == CHATTING
        update.message.reply_text.assert_called_with("What's the gig title?", parse_mode="Markdown")

    @pytest.mark.asyncio
    async def test_url_arg_starts_agent_and_ends_when_done(self):
        update = _make_update()
        context = MagicMock()
        context.args = ["https://organistsonline.org/required/123/"]
        from telegram.ext import ConversationHandler

        from organist_bot.integrations.gig_agent import GigAgentResponse

        agent_resp = GigAgentResponse(text="✓ Added to calendar!", done=True)
        with patch(
            "organist_bot.integrations.gig_agent.process_message",
            new=AsyncMock(return_value=agent_resp),
        ):
            result = await addgig_entry(update, context)

        assert result == ConversationHandler.END


# ── cmd_gigs ──────────────────────────────────────────────────────────────────


def _make_event(n: int = 1) -> dict:
    return {
        "id": f"evt{n}",
        "summary": f"Sunday Service {n}",
        "start_dt": datetime.datetime(2026, 6, n, 10, 30, tzinfo=datetime.UTC),
        "date_str": f"2026-06-0{n}",
    }


class TestCmdGigs:
    @pytest.fixture(autouse=True)
    def patch_settings(self):
        with patch("organist_bot.integrations.telegram_bot.settings") as mock:
            mock.telegram_chat_id = "7973955362"
            mock.google_calendar_id = "cal@test.com"
            mock.google_calendar_credentials_file = "fake.json"
            yield mock

    @pytest.mark.asyncio
    async def test_lists_events_as_numbered_reply(self):
        update = _make_update()
        events = [_make_event(1), _make_event(2)]
        with patch("organist_bot.integrations.telegram_bot._make_calendar_client") as mock_factory:
            mock_cal = MagicMock()
            mock_cal.list_upcoming_events.return_value = events
            mock_factory.return_value = mock_cal
            await cmd_gigs(update, MagicMock())
        update.message.reply_text.assert_called_once()
        reply_text = update.message.reply_text.call_args[0][0]
        assert "1" in reply_text
        assert "Sunday Service 1" in reply_text
        assert "2" in reply_text

    @pytest.mark.asyncio
    async def test_stores_events_in_listing_cache(self):
        update = _make_update(chat_id=7973955362)
        events = [_make_event(1)]
        _gig_listing.clear()
        with patch("organist_bot.integrations.telegram_bot._make_calendar_client") as mock_factory:
            mock_cal = MagicMock()
            mock_cal.list_upcoming_events.return_value = events
            mock_factory.return_value = mock_cal
            await cmd_gigs(update, MagicMock())
        assert _gig_listing.get(7973955362) == events

    @pytest.mark.asyncio
    async def test_replies_no_upcoming_gigs_when_empty(self):
        update = _make_update()
        with patch("organist_bot.integrations.telegram_bot._make_calendar_client") as mock_factory:
            mock_cal = MagicMock()
            mock_cal.list_upcoming_events.return_value = []
            mock_factory.return_value = mock_cal
            await cmd_gigs(update, MagicMock())
        reply = update.message.reply_text.call_args[0][0]
        assert "No upcoming gigs" in reply

    @pytest.mark.asyncio
    async def test_rejects_unauthorised_chat(self):
        update = _make_update(chat_id=9999)
        with patch("organist_bot.integrations.telegram_bot._make_calendar_client") as mock_factory:
            await cmd_gigs(update, MagicMock())
        mock_factory.assert_not_called()

    @pytest.mark.asyncio
    async def test_replies_not_configured_when_no_calendar(self):
        update = _make_update()
        with patch(
            "organist_bot.integrations.telegram_bot._make_calendar_client", return_value=None
        ):
            await cmd_gigs(update, MagicMock())
        reply = update.message.reply_text.call_args[0][0]
        assert "not configured" in reply.lower()


# ── cmd_deletegig ─────────────────────────────────────────────────────────────


class TestCmdDeletegig:
    @pytest.fixture(autouse=True)
    def patch_settings(self):
        with patch("organist_bot.integrations.telegram_bot.settings") as mock:
            mock.telegram_chat_id = "7973955362"
            mock.google_calendar_id = "cal@test.com"
            mock.google_calendar_credentials_file = "fake.json"
            yield mock

    @pytest.fixture(autouse=True)
    def seed_cache(self):
        _gig_listing[7973955362] = [_make_event(1), _make_event(2)]
        yield
        _gig_listing.pop(7973955362, None)

    @pytest.mark.asyncio
    async def test_deletes_event_and_replies_confirmation(self):
        update = _make_update()
        context = MagicMock()
        context.args = ["1"]
        with (
            patch("organist_bot.integrations.telegram_bot._make_calendar_client") as mock_factory,
            patch("organist_bot.integrations.telegram_bot.filter_store"),
        ):
            mock_cal = MagicMock()
            mock_factory.return_value = mock_cal
            await cmd_deletegig(update, context)
        mock_cal.delete_event.assert_called_once_with("evt1")
        reply = update.message.reply_text.call_args[0][0]
        assert "Sunday Service 1" in reply

    @pytest.mark.asyncio
    async def test_removes_date_from_unavailable(self):
        update = _make_update()
        context = MagicMock()
        context.args = ["1"]
        with (
            patch("organist_bot.integrations.telegram_bot._make_calendar_client") as mock_factory,
            patch("organist_bot.integrations.telegram_bot.filter_store") as mock_fs,
        ):
            mock_cal = MagicMock()
            mock_factory.return_value = mock_cal
            await cmd_deletegig(update, context)
        mock_fs.remove_period.assert_called_once_with("unavailable_periods", "2026-06-01")

    @pytest.mark.asyncio
    async def test_updates_cache_after_delete(self):
        update = _make_update(chat_id=7973955362)
        context = MagicMock()
        context.args = ["1"]
        with (
            patch("organist_bot.integrations.telegram_bot._make_calendar_client") as mock_factory,
            patch("organist_bot.integrations.telegram_bot.filter_store"),
        ):
            mock_factory.return_value = MagicMock()
            await cmd_deletegig(update, context)
        assert len(_gig_listing[7973955362]) == 1
        assert _gig_listing[7973955362][0]["id"] == "evt2"

    @pytest.mark.asyncio
    async def test_no_args_replies_usage_hint(self):
        update = _make_update()
        context = MagicMock()
        context.args = []
        await cmd_deletegig(update, context)
        reply = update.message.reply_text.call_args[0][0]
        assert "/deletegig" in reply

    @pytest.mark.asyncio
    async def test_out_of_range_replies_error(self):
        update = _make_update()
        context = MagicMock()
        context.args = ["99"]
        await cmd_deletegig(update, context)
        reply = update.message.reply_text.call_args[0][0]
        assert "99" in reply

    @pytest.mark.asyncio
    async def test_empty_cache_prompts_run_gigs(self):
        _gig_listing.pop(7973955362, None)
        update = _make_update(chat_id=7973955362)
        context = MagicMock()
        context.args = ["1"]
        await cmd_deletegig(update, context)
        reply = update.message.reply_text.call_args[0][0]
        assert "/gigs" in reply

    @pytest.mark.asyncio
    async def test_delete_failure_replies_error(self):
        update = _make_update()
        context = MagicMock()
        context.args = ["1"]
        with (
            patch("organist_bot.integrations.telegram_bot._make_calendar_client") as mock_factory,
            patch("organist_bot.integrations.telegram_bot.filter_store"),
        ):
            mock_cal = MagicMock()
            mock_cal.delete_event.side_effect = Exception("API error")
            mock_factory.return_value = mock_cal
            await cmd_deletegig(update, context)
        reply = update.message.reply_text.call_args[0][0]
        assert "API error" in reply or "error" in reply.lower()

    @pytest.mark.asyncio
    async def test_rejects_unauthorised_chat(self):
        update = _make_update(chat_id=9999)
        context = MagicMock()
        context.args = ["1"]
        with patch("organist_bot.integrations.telegram_bot._make_calendar_client") as mock_factory:
            await cmd_deletegig(update, context)
        mock_factory.assert_not_called()
