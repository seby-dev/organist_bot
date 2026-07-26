"""
organist_bot/integrations/telegram_bot.py
──────────────────────────────────────────
Unified Telegram bot for the organist toolkit.

All free text is routed to the unified AI agent.

Security: only messages from TELEGRAM_CHAT_ID are processed.
"""

import logging
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
)
from telegram.ext import (
    filters as tg_filters,
)

import organist_bot.alert as alert
from organist_bot.config import settings
from organist_bot.integrations import unified_agent

logger = logging.getLogger(__name__)

_HELP = (
    "I can help you manage gigs, generate invoices, and manage your availability filters. "
    "Just tell me what you need in plain English."
)


# ── Auth ──────────────────────────────────────────────────────────────────────


def _is_authorised(update: Update) -> bool:
    if update.effective_chat is None:
        return False
    return str(update.effective_chat.id) == str(settings.telegram_chat_id)


def _reject(update: Update) -> None:
    logger.warning(
        "Telegram: rejected unauthorised message",
        extra={"chat_id": update.effective_chat.id if update.effective_chat else None},
    )


# ── Reply helper ──────────────────────────────────────────────────────────────


def _build_reply_markup(buttons: list[list[dict]] | None) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(**cell) for cell in row] for row in buttons])


async def _reply(
    message: Message, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    """Send with Markdown formatting; fall back to plain text if the LLM's
    output contains characters (stray `_`, `*`, backticks) that Telegram's
    legacy Markdown parser can't balance into valid entities."""
    try:
        await message.reply_text(text, parse_mode="Markdown", reply_markup=reply_markup)
    except BadRequest as exc:
        if "can't parse entities" not in str(exc).lower():
            raise
        logger.warning(
            "Telegram: Markdown parse failed, resending as plain text",
            extra={"error": str(exc)},
        )
        await message.reply_text(text, reply_markup=reply_markup)


# ── /start ────────────────────────────────────────────────────────────────────


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update):
        _reject(update)
        return
    assert update.message is not None
    await _reply(update.message, _HELP)


# ── Free-text handler ─────────────────────────────────────────────────────────


async def _delete_quietly(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int
) -> None:
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except BadRequest as exc:
        logger.debug("Telegram: progress message delete failed: %s", exc)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update):
        _reject(update)
        return
    assert update.effective_chat is not None
    assert update.message is not None

    chat_id = update.effective_chat.id
    text = update.message.text or ""

    status_msg = await update.message.reply_text("🤔 Thinking…")

    async def on_step(status_text: str) -> None:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=status_msg.message_id, text=status_text
            )
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                logger.debug("Telegram: progress edit failed: %s", exc)

    try:
        responses = await unified_agent.process_message(chat_id, text, on_step=on_step)
        await _delete_quietly(context, chat_id, status_msg.message_id)
        for resp in responses:
            if resp.file_path:
                with open(resp.file_path, "rb") as f:
                    await context.bot.send_document(
                        chat_id=chat_id,
                        document=f,
                        filename=os.path.basename(resp.file_path),
                        caption=resp.file_caption or "",
                    )
            if resp.text:
                await _reply(
                    update.message, resp.text, reply_markup=_build_reply_markup(resp.buttons)
                )
    except Exception as exc:
        logger.exception("Telegram: unified agent error")
        await _delete_quietly(context, chat_id, status_msg.message_id)
        await update.message.reply_text(f"❌ Unexpected error: {exc}")


# ── NEG draft callback handler ──────────────────────────────────────────────


async def _edit_buttons_quietly(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int,
    buttons: list[list[dict]] | None,
) -> None:
    try:
        await context.bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id, reply_markup=_build_reply_markup(buttons)
        )
    except BadRequest as exc:
        logger.debug("Telegram: NEG button edit failed: %s", exc)


async def _edit_text_quietly(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int,
    text: str,
    buttons: list[list[dict]] | None = None,
) -> None:
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=_build_reply_markup(buttons),
        )
    except BadRequest as exc:
        logger.debug("Telegram: NEG message edit failed: %s", exc)


async def handle_neg_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.callback_query is not None
    await update.callback_query.answer()

    if not _is_authorised(update):
        _reject(update)
        return

    assert update.effective_chat is not None
    assert update.callback_query.message is not None
    chat_id = update.effective_chat.id
    message_id = update.callback_query.message.message_id

    data = update.callback_query.data or ""
    parts = data.split(":", 2)
    if len(parts) != 3 or parts[0] != "neg":
        return
    _, action, gig_id = parts

    if action == "accept":
        await _edit_buttons_quietly(
            context, chat_id, message_id, unified_agent.neg_confirm_buttons(gig_id, send=True)
        )
    elif action == "reject":
        await _edit_buttons_quietly(
            context, chat_id, message_id, unified_agent.neg_confirm_buttons(gig_id, send=False)
        )
    elif action == "edit":
        unified_agent.set_active_neg_draft(chat_id, gig_id)
        await _edit_text_quietly(
            context, chat_id, message_id, "✏️ What would you like to change about this draft?"
        )
    elif action == "confirm_send":
        ok, result = await unified_agent.neg_confirm_send(gig_id)
        await _edit_text_quietly(context, chat_id, message_id, f"{'✅' if ok else '❌'} {result}")
    elif action == "confirm_reject":
        ok, result = unified_agent.neg_confirm_reject(gig_id)
        await _edit_text_quietly(context, chat_id, message_id, f"{'✅' if ok else '❌'} {result}")
    elif action == "cancel":
        view = unified_agent.neg_draft_view(gig_id)
        if view is None:
            await _edit_text_quietly(
                context, chat_id, message_id, "This draft is no longer available."
            )
        else:
            text, buttons = view
            await _edit_text_quietly(context, chat_id, message_id, text, buttons)
    elif action == "pick":
        unified_agent.set_active_neg_draft(chat_id, gig_id)
        instruction = unified_agent.pop_pending_neg_instruction(chat_id)
        if instruction is None:
            await context.bot.send_message(
                chat_id=chat_id,
                text="I lost track of what you asked — please repeat it for this draft.",
            )
            return
        responses = await unified_agent.process_message(chat_id, f"For gig {gig_id}: {instruction}")
        for resp in responses:
            if resp.text:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=resp.text,
                    reply_markup=_build_reply_markup(resp.buttons),
                )


# ── Bot setup ─────────────────────────────────────────────────────────────────


def run(token: str) -> None:
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_neg_callback, pattern=r"^neg:"))

    cal = unified_agent._make_calendar_client()
    if cal:
        unified_agent.sync_calendar_blocks(cal)

    logger.info("Telegram bot polling", extra={"chat_id": settings.telegram_chat_id})
    alert.send_alert("🤖 Telegram bot started")
    app.run_polling()
