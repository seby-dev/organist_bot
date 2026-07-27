"""Tests for the unified Telegram bot handlers."""

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import BadRequest

from organist_bot.integrations.telegram_bot import (
    _is_authorised,
    handle_message,
    handle_neg_callback,
)
from organist_bot.integrations.unified_agent import AgentResponse

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_update(chat_id: int = 7973955362, text: str = "") -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.text = text
    update.message.reply_text = AsyncMock(return_value=MagicMock(message_id=111))
    return update


def _make_context() -> MagicMock:
    context = MagicMock()
    context.bot.edit_message_text = AsyncMock()
    context.bot.edit_message_reply_markup = AsyncMock()
    context.bot.delete_message = AsyncMock()
    context.bot.send_document = AsyncMock()
    context.bot.send_message = AsyncMock()
    return context


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
    async def test_sends_placeholder_then_text_response(self):
        update = _make_update(text="List my clients")
        context = _make_context()
        responses = [AgentResponse(text="You have 3 clients.")]
        with patch(
            "organist_bot.integrations.unified_agent.process_message",
            new=AsyncMock(return_value=responses),
        ):
            await handle_message(update, context)
        update.message.reply_text.assert_any_call("🤔 Thinking…")
        update.message.reply_text.assert_any_call(
            "You have 3 clients.", parse_mode="Markdown", reply_markup=None
        )
        context.bot.delete_message.assert_called_once_with(
            chat_id=update.effective_chat.id, message_id=111
        )

    @pytest.mark.asyncio
    async def test_sends_file_response(self):
        update = _make_update(text="Generate invoice for holy-cross")
        context = _make_context()
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
        context = _make_context()
        update.message.reply_text = AsyncMock(
            side_effect=[
                MagicMock(message_id=111),  # placeholder
                BadRequest("Can't parse entities: can't find end of the entity at byte offset 658"),
                None,
            ]
        )
        responses = [AgentResponse(text="Use manage_filter_suspensions to *pause* a filter.")]
        with patch(
            "organist_bot.integrations.unified_agent.process_message",
            new=AsyncMock(return_value=responses),
        ):
            await handle_message(update, context)
        assert update.message.reply_text.call_count == 3
        placeholder_call, first_call, second_call = update.message.reply_text.call_args_list
        assert placeholder_call.args == ("🤔 Thinking…",)
        assert first_call.kwargs.get("parse_mode") == "Markdown"
        assert second_call.args == ("Use manage_filter_suspensions to *pause* a filter.",)
        assert "parse_mode" not in second_call.kwargs

    @pytest.mark.asyncio
    async def test_reraises_non_markdown_bad_request(self):
        update = _make_update(text="hello")
        context = _make_context()
        # First call after the placeholder (the Markdown attempt) fails for an
        # unrelated reason and must propagate out of _reply; the last call is
        # handle_message's own error-reporting reply_text, which succeeds.
        update.message.reply_text = AsyncMock(
            side_effect=[
                MagicMock(message_id=111),  # placeholder
                BadRequest("Chat not found"),
                None,
            ]
        )
        responses = [AgentResponse(text="hi there")]
        with patch(
            "organist_bot.integrations.unified_agent.process_message",
            new=AsyncMock(return_value=responses),
        ):
            await handle_message(update, context)
        # handle_message's own try/except catches it and reports the error back
        assert update.message.reply_text.call_count == 3
        reply = update.message.reply_text.call_args[0][0]
        assert "❌" in reply

    @pytest.mark.asyncio
    async def test_handles_agent_error(self):
        update = _make_update(text="crash please")
        context = _make_context()
        with patch(
            "organist_bot.integrations.unified_agent.process_message",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            await handle_message(update, context)
        reply = update.message.reply_text.call_args[0][0]
        assert "❌" in reply or "error" in reply.lower()
        context.bot.delete_message.assert_called_once_with(
            chat_id=update.effective_chat.id, message_id=111
        )

    @pytest.mark.asyncio
    async def test_on_step_edits_placeholder_message(self):
        """The on_step callback passed into process_message must edit the
        placeholder message in place with each step's text."""
        update = _make_update(text="add a gig")
        context = _make_context()

        async def fake_process_message(chat_id, text, on_step=None):
            await on_step("🔧 add_gig")
            await on_step("✅ add_gig")
            return [AgentResponse(text="Added.")]

        with patch(
            "organist_bot.integrations.unified_agent.process_message",
            new=fake_process_message,
        ):
            await handle_message(update, context)

        assert context.bot.edit_message_text.call_count == 2
        first_kwargs = context.bot.edit_message_text.call_args_list[0].kwargs
        assert first_kwargs == {
            "chat_id": update.effective_chat.id,
            "message_id": 111,
            "text": "🔧 add_gig",
        }
        second_kwargs = context.bot.edit_message_text.call_args_list[1].kwargs
        assert second_kwargs["text"] == "✅ add_gig"

    @pytest.mark.asyncio
    async def test_on_step_swallows_not_modified_error(self):
        """Telegram raises BadRequest('message is not modified') when two
        consecutive edits produce identical text — this must not propagate."""
        update = _make_update(text="add a gig")
        context = _make_context()
        context.bot.edit_message_text = AsyncMock(
            side_effect=BadRequest("Bad Request: message is not modified")
        )

        async def fake_process_message(chat_id, text, on_step=None):
            await on_step("🔧 add_gig")  # must not raise
            return [AgentResponse(text="Added.")]

        with patch(
            "organist_bot.integrations.unified_agent.process_message",
            new=fake_process_message,
        ):
            await handle_message(update, context)  # must not raise

        update.message.reply_text.assert_any_call(
            "Added.", parse_mode="Markdown", reply_markup=None
        )

    @pytest.mark.asyncio
    async def test_sends_buttons_when_response_has_them(self):
        update = _make_update(text="approve abc123")
        context = _make_context()
        responses = [
            AgentResponse(
                text="Will send this draft.",
                buttons=[[{"text": "Confirm", "callback_data": "neg:confirm_send:abc123"}]],
            )
        ]
        with patch(
            "organist_bot.integrations.unified_agent.process_message",
            new=AsyncMock(return_value=responses),
        ):
            await handle_message(update, context)
        last_call = update.message.reply_text.call_args_list[-1]
        markup = last_call.kwargs["reply_markup"]
        assert markup.inline_keyboard[0][0].text == "Confirm"
        assert markup.inline_keyboard[0][0].callback_data == "neg:confirm_send:abc123"


# ── NEG callback handler ─────────────────────────────────────────────────────


def _make_callback_update(chat_id: int = 7973955362, data: str = "", message_id: int = 55):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.message = MagicMock()
    update.callback_query.message.message_id = message_id
    return update


class TestHandleNegCallback:
    @pytest.fixture(autouse=True)
    def patch_settings(self):
        with patch("organist_bot.integrations.telegram_bot.settings") as mock:
            mock.telegram_chat_id = "7973955362"
            yield mock

    @pytest.mark.asyncio
    async def test_rejects_unauthorised_chat(self):
        update = _make_callback_update(chat_id=9999, data="neg:accept:abc123")
        context = _make_context()
        with patch("organist_bot.integrations.unified_agent.neg_confirm_buttons") as mock_fn:
            await handle_neg_callback(update, context)
        mock_fn.assert_not_called()
        update.callback_query.answer.assert_called_once()

    @pytest.mark.asyncio
    async def test_ignores_non_neg_callback_data(self):
        update = _make_callback_update(data="something:else")
        context = _make_context()
        await handle_neg_callback(update, context)
        context.bot.edit_message_reply_markup.assert_not_called()
        context.bot.edit_message_text.assert_not_called()
        update.callback_query.answer.assert_called_once()

    @pytest.mark.asyncio
    async def test_accept_swaps_to_confirm_send_buttons(self):
        update = _make_callback_update(data="neg:accept:abc123")
        context = _make_context()
        with patch(
            "organist_bot.integrations.unified_agent.neg_confirm_buttons",
            return_value=[[{"text": "Confirm", "callback_data": "neg:confirm_send:abc123"}]],
        ):
            await handle_neg_callback(update, context)
        context.bot.edit_message_reply_markup.assert_called_once()
        kwargs = context.bot.edit_message_reply_markup.call_args.kwargs
        assert kwargs["chat_id"] == 7973955362
        assert kwargs["message_id"] == 55
        markup = kwargs["reply_markup"]
        assert markup.inline_keyboard[0][0].text == "Confirm"
        assert markup.inline_keyboard[0][0].callback_data == "neg:confirm_send:abc123"

    @pytest.mark.asyncio
    async def test_reject_swaps_to_confirm_reject_buttons(self):
        update = _make_callback_update(data="neg:reject:abc123")
        context = _make_context()
        with patch("organist_bot.integrations.unified_agent.neg_confirm_buttons") as mock_buttons:
            mock_buttons.return_value = [
                [{"text": "Confirm", "callback_data": "neg:confirm_reject:abc123"}]
            ]
            await handle_neg_callback(update, context)
        mock_buttons.assert_called_once_with("abc123", send=False)
        kwargs = context.bot.edit_message_reply_markup.call_args.kwargs
        markup = kwargs["reply_markup"]
        assert markup.inline_keyboard[0][0].callback_data == "neg:confirm_reject:abc123"

    @pytest.mark.asyncio
    async def test_edit_sets_active_draft_and_prompts(self):
        update = _make_callback_update(data="neg:edit:abc123")
        context = _make_context()
        with (
            patch("organist_bot.integrations.unified_agent.set_active_neg_draft") as mock_set,
            patch("organist_bot.integrations.unified_agent._persist_chat") as mock_persist,
        ):
            await handle_neg_callback(update, context)
        mock_set.assert_called_once_with(7973955362, "abc123")
        # Persisted immediately (not deferred to the next process_message call)
        # so a restart between this tap and the user's reply doesn't lose it.
        mock_persist.assert_called_once_with(7973955362)
        context.bot.edit_message_text.assert_called_once()
        assert (
            "what would you like to change"
            in context.bot.edit_message_text.call_args.kwargs["text"].lower()
        )

    @pytest.mark.asyncio
    async def test_confirm_send_success_shows_sent(self):
        update = _make_callback_update(data="neg:confirm_send:abc123")
        context = _make_context()
        with patch(
            "organist_bot.integrations.unified_agent.neg_confirm_send",
            new=AsyncMock(return_value=(True, "Sent to jane@example.com.")),
        ):
            await handle_neg_callback(update, context)
        text = context.bot.edit_message_text.call_args.kwargs["text"]
        assert "✅" in text
        assert "Sent to jane@example.com." in text

    @pytest.mark.asyncio
    async def test_confirm_send_failure_shows_failure(self):
        update = _make_callback_update(data="neg:confirm_send:abc123")
        context = _make_context()
        with patch(
            "organist_bot.integrations.unified_agent.neg_confirm_send",
            new=AsyncMock(return_value=(False, "Send failed: smtp down")),
        ):
            await handle_neg_callback(update, context)
        text = context.bot.edit_message_text.call_args.kwargs["text"]
        assert "❌" in text

    @pytest.mark.asyncio
    async def test_confirm_reject_success_shows_rejected(self):
        update = _make_callback_update(data="neg:confirm_reject:abc123")
        context = _make_context()
        with patch(
            "organist_bot.integrations.unified_agent.neg_confirm_reject",
            return_value=(True, "Draft rejected — no email sent."),
        ):
            await handle_neg_callback(update, context)
        text = context.bot.edit_message_text.call_args.kwargs["text"]
        assert "✅" in text

    @pytest.mark.asyncio
    async def test_cancel_restores_draft_view(self):
        update = _make_callback_update(data="neg:cancel:abc123")
        context = _make_context()
        view_text = "Draft email — id: abc123"
        view_buttons = [[{"text": "Accept", "callback_data": "neg:accept:abc123"}]]
        with patch(
            "organist_bot.integrations.unified_agent.neg_draft_view",
            return_value=(view_text, view_buttons),
        ):
            await handle_neg_callback(update, context)
        kwargs = context.bot.edit_message_text.call_args.kwargs
        assert kwargs["text"] == view_text
        markup = kwargs["reply_markup"]
        assert markup.inline_keyboard[0][0].text == "Accept"
        assert markup.inline_keyboard[0][0].callback_data == "neg:accept:abc123"

    @pytest.mark.asyncio
    async def test_cancel_when_draft_gone(self):
        update = _make_callback_update(data="neg:cancel:abc123")
        context = _make_context()
        with patch("organist_bot.integrations.unified_agent.neg_draft_view", return_value=None):
            await handle_neg_callback(update, context)
        text = context.bot.edit_message_text.call_args.kwargs["text"]
        assert "no longer available" in text.lower()

    @pytest.mark.asyncio
    async def test_pick_replays_instruction_through_agent(self):
        update = _make_callback_update(data="neg:pick:abc123")
        context = _make_context()
        with (
            patch("organist_bot.integrations.unified_agent.set_active_neg_draft") as mock_set,
            patch(
                "organist_bot.integrations.unified_agent.pop_pending_neg_instruction",
                return_value="raise the fee to 180",
            ),
            patch(
                "organist_bot.integrations.unified_agent.process_message",
                new=AsyncMock(return_value=[AgentResponse(text="Revised draft.", buttons=None)]),
            ) as mock_pm,
        ):
            await handle_neg_callback(update, context)
        mock_set.assert_called_once_with(7973955362, "abc123")
        mock_pm.assert_called_once_with(7973955362, "For gig abc123: raise the fee to 180")
        context.bot.send_message.assert_called_once()
        assert context.bot.send_message.call_args.kwargs["text"] == "Revised draft."

    @pytest.mark.asyncio
    async def test_pick_with_no_pending_instruction_asks_to_repeat(self):
        update = _make_callback_update(data="neg:pick:abc123")
        context = _make_context()
        with (
            patch("organist_bot.integrations.unified_agent.set_active_neg_draft"),
            patch(
                "organist_bot.integrations.unified_agent.pop_pending_neg_instruction",
                return_value=None,
            ),
        ):
            await handle_neg_callback(update, context)
        text = context.bot.send_message.call_args.kwargs["text"]
        assert "lost track" in text.lower()

    @pytest.mark.asyncio
    async def test_edit_message_badrequest_is_swallowed(self):
        update = _make_callback_update(data="neg:cancel:abc123")
        context = _make_context()
        context.bot.edit_message_text = AsyncMock(side_effect=BadRequest("message is not found"))
        with patch(
            "organist_bot.integrations.unified_agent.neg_draft_view",
            return_value=("text", []),
        ):
            await handle_neg_callback(update, context)  # must not raise
