"""Tests for the unified Telegram bot handlers."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from organist_bot.integrations.invoice_agent import AgentResponse
from organist_bot.integrations.telegram_bot import (
    _is_authorised,
    addgig_entry,
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
