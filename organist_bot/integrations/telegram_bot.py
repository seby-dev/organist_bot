"""
organist_bot/integrations/telegram_bot.py
──────────────────────────────────────────
Unified Telegram bot for the organist toolkit.

All free text is routed to the unified AI agent.

Security: only messages from TELEGRAM_CHAT_ID are processed.
"""

import logging
import os

from telegram import Update
from telegram.ext import (
    Application,
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


# ── /start ────────────────────────────────────────────────────────────────────


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update):
        _reject(update)
        return
    assert update.message is not None
    await update.message.reply_text(_HELP, parse_mode="Markdown")


# ── Free-text handler ─────────────────────────────────────────────────────────


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update):
        _reject(update)
        return
    assert update.effective_chat is not None
    assert update.message is not None

    chat_id = update.effective_chat.id
    text = update.message.text or ""

    try:
        responses = await unified_agent.process_message(chat_id, text)
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
                await update.message.reply_text(resp.text, parse_mode="Markdown")
    except Exception as exc:
        logger.exception("Telegram: unified agent error")
        await update.message.reply_text(f"❌ Unexpected error: {exc}")


# ── Bot setup ─────────────────────────────────────────────────────────────────


def run(token: str) -> None:
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, handle_message))

    cal = unified_agent._make_calendar_client()
    if cal:
        unified_agent.sync_calendar_blocks(cal)

    logger.info("Telegram bot polling", extra={"chat_id": settings.telegram_chat_id})
    alert.send_alert("🤖 Telegram bot started")
    app.run_polling()
