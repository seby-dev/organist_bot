"""Tests for the unified Telegram bot handlers."""

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import BadRequest

from organist_bot.integrations.telegram_bot import _is_authorised, handle_message
from organist_bot.integrations.unified_agent import AgentResponse

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_update(chat_id: int = 7973955362, text: str = "") -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


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


# ── handle_message ────────────────────────────────────────────────────────────


class TestHandleMessage:
    @pytest.fixture(autouse=True)
    def patch_settings(self):
        with patch("organist_bot.integrations.telegram_bot.settings") as mock:
            mock.telegram_chat_id = "7973955362"
            yield mock

    @pytest.mark.asyncio
    async def test_rejects_unauthorised_chat(self):
        update = _make_update(chat_id=9999)
        with patch("organist_bot.integrations.unified_agent.process_message") as mock_pm:
            await handle_message(update, MagicMock())
        mock_pm.assert_not_called()
        update.message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_sends_text_response(self):
        update = _make_update(text="List my clients")
        responses = [AgentResponse(text="You have 3 clients.")]
        with patch(
            "organist_bot.integrations.unified_agent.process_message",
            new=AsyncMock(return_value=responses),
        ):
            await handle_message(update, MagicMock())
        update.message.reply_text.assert_called_once_with(
            "You have 3 clients.", parse_mode="Markdown"
        )

    @pytest.mark.asyncio
    async def test_sends_file_response(self):
        update = _make_update(text="Generate invoice for holy-cross")
        context = MagicMock()
        context.bot.send_document = AsyncMock()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            responses = [AgentResponse(file_path=tmp_path, file_caption="Invoice")]
            with patch(
                "organist_bot.integrations.unified_agent.process_message",
                new=AsyncMock(return_value=responses),
            ):
                await handle_message(update, context)
            context.bot.send_document.assert_called_once()
            _, kwargs = context.bot.send_document.call_args
            assert kwargs.get("caption") == "Invoice"
        finally:
            os.unlink(tmp_path)

    @pytest.mark.asyncio
    async def test_falls_back_to_plain_text_on_markdown_parse_error(self):
        """LLM output can contain stray underscores/asterisks (e.g. tool names
        like `manage_filter_suspensions`) that Telegram's legacy Markdown
        parser can't balance into valid entities. The reply must still be
        delivered, as plain text."""
        update = _make_update(text="what can you do across filter management")
        update.message.reply_text = AsyncMock(
            side_effect=[
                BadRequest("Can't parse entities: can't find end of the entity at byte offset 658"),
                None,
            ]
        )
        responses = [AgentResponse(text="Use manage_filter_suspensions to *pause* a filter.")]
        with patch(
            "organist_bot.integrations.unified_agent.process_message",
            new=AsyncMock(return_value=responses),
        ):
            await handle_message(update, MagicMock())
        assert update.message.reply_text.call_count == 2
        first_call, second_call = update.message.reply_text.call_args_list
        assert first_call.kwargs.get("parse_mode") == "Markdown"
        assert second_call.args == ("Use manage_filter_suspensions to *pause* a filter.",)
        assert "parse_mode" not in second_call.kwargs

    @pytest.mark.asyncio
    async def test_reraises_non_markdown_bad_request(self):
        update = _make_update(text="hello")
        # First call (the Markdown attempt) fails for an unrelated reason and
        # must propagate out of _reply; second call is handle_message's own
        # error-reporting reply_text, which should succeed normally.
        update.message.reply_text = AsyncMock(side_effect=[BadRequest("Chat not found"), None])
        responses = [AgentResponse(text="hi there")]
        with patch(
            "organist_bot.integrations.unified_agent.process_message",
            new=AsyncMock(return_value=responses),
        ):
            await handle_message(update, MagicMock())
        # handle_message's own try/except catches it and reports the error back
        assert update.message.reply_text.call_count == 2
        reply = update.message.reply_text.call_args[0][0]
        assert "❌" in reply

    @pytest.mark.asyncio
    async def test_handles_agent_error(self):
        update = _make_update(text="crash please")
        with patch(
            "organist_bot.integrations.unified_agent.process_message",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            await handle_message(update, MagicMock())
        reply = update.message.reply_text.call_args[0][0]
        assert "❌" in reply or "error" in reply.lower()
