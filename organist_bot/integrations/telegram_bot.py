"""
organist_bot/integrations/telegram_bot.py
──────────────────────────────────────────
Unified Telegram bot for the organist toolkit.

Commands
--------
  /start          — Show help
  /addgig <url>   — Scrape a gig URL and add it to Google Calendar (agentic)
  /addgig         — Add a gig via natural-language conversation (agentic)
  /cancel         — Cancel an in-progress gig entry
  /reset          — Clear invoice conversation history

Everything else (free text) is routed to the invoice AI agent.

Security: only messages from TELEGRAM_CHAT_ID are processed.
"""

import logging
import os

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
)
from telegram.ext import (
    filters as tg_filters,
)

from organist_bot import filter_store
from organist_bot.config import settings

logger = logging.getLogger(__name__)

# Single state for the agentic gig conversation
CHATTING = 0

_HELP = (
    "*Organist Bot*\n\n"
    "*Gig calendar*\n"
    "  /gigs           — View upcoming gigs\n"
    "  /deletegig \\<n\\> — Delete gig by number\n"
    "  /addgig \\<url\\> — Add a gig by URL\n"
    "  /addgig         — Add a gig via conversation\n"
    "  /cancel         — Cancel gig entry\n\n"
    "*Filters*\n"
    "  /blacklist \\[add \\<email\\>|rm \\<email\\>|list\\]\n"
    "  /unavailable \\[add \\<period\\>|rm \\<period\\>|list\\]\n"
    "  /available \\[add \\<period\\>|rm \\<period\\>|list\\]\n"
    "  Period formats: `2026-12-25` · `2026-12-20:2027-01-05` · `2026-12`\n\n"
    "*Invoicing*\n"
    "  Just type your request in plain English, e.g.:\n"
    '  "Send an invoice to Holy Cross for March Masses, £240"\n'
    '  "List my clients"\n'
    "  /reset — Clear invoice conversation history"
)

# Per-chat cache of the last /gigs listing, keyed by chat_id.
_gig_listing: dict[int, list[dict]] = {}


def _make_calendar_client():
    if settings.google_calendar_id and settings.google_calendar_credentials_file:
        from organist_bot.integrations.calendar_client import GoogleCalendarClient

        return GoogleCalendarClient(
            credentials_file=settings.google_calendar_credentials_file,
            calendar_id=settings.google_calendar_id,
        )
    return None


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


# ── /reset ────────────────────────────────────────────────────────────────────


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update):
        _reject(update)
        return
    assert update.effective_chat is not None
    assert update.message is not None
    from organist_bot.integrations.invoice_agent import reset_conversation

    reset_conversation(update.effective_chat.id)
    await update.message.reply_text("Invoice conversation cleared.")


# ── /addgig — agentic entry point ────────────────────────────────────────────


async def addgig_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_authorised(update):
        _reject(update)
        return ConversationHandler.END
    assert update.message is not None
    assert update.effective_chat is not None

    from organist_bot.integrations import gig_agent

    chat_id = update.effective_chat.id
    gig_agent.reset_gig_conversation(chat_id)  # fresh start on every /addgig

    initial = context.args[0] if context.args else "I'd like to add a gig to my calendar."

    await update.message.reply_text("⏳ One moment…")
    try:
        response = await gig_agent.process_message(chat_id, initial)
    except Exception as exc:
        logger.exception("Gig agent error on entry")
        await update.message.reply_text(f"❌ Unexpected error: {exc}")
        return ConversationHandler.END

    if response.text:
        await update.message.reply_text(response.text, parse_mode="Markdown")

    if response.done:
        gig_agent.reset_gig_conversation(chat_id)
        return ConversationHandler.END

    return CHATTING


# ── Gig agent — in-conversation handler ──────────────────────────────────────


async def gig_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None
    assert update.effective_chat is not None

    if not _is_authorised(update):
        _reject(update)
        return ConversationHandler.END

    from organist_bot.integrations import gig_agent

    chat_id = update.effective_chat.id
    text = update.message.text or ""

    try:
        response = await gig_agent.process_message(chat_id, text)
    except Exception as exc:
        logger.exception("Gig agent error")
        await update.message.reply_text(f"❌ Unexpected error: {exc}")
        gig_agent.reset_gig_conversation(chat_id)
        return ConversationHandler.END

    if response.text:
        await update.message.reply_text(response.text, parse_mode="Markdown")

    if response.done:
        gig_agent.reset_gig_conversation(chat_id)
        return ConversationHandler.END

    return CHATTING


# ── /cancel ───────────────────────────────────────────────────────────────────


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_authorised(update):
        _reject(update)
        return ConversationHandler.END
    assert update.message is not None
    assert update.effective_chat is not None

    from organist_bot.integrations import gig_agent

    gig_agent.reset_gig_conversation(update.effective_chat.id)
    await update.message.reply_text("Gig entry cancelled.")
    return ConversationHandler.END


# ── Invoice agent — free-text handler ────────────────────────────────────────


async def handle_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update):
        _reject(update)
        return
    assert update.effective_chat is not None
    assert update.message is not None
    from organist_bot.integrations.invoice_agent import process_message

    chat_id = update.effective_chat.id
    text = update.message.text or ""

    try:
        agent_responses = await process_message(chat_id, text)
        for resp in agent_responses:
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
        logger.exception("Telegram: invoice agent error")
        await update.message.reply_text(f"❌ Unexpected error: {exc}")


# ── Filter management commands ───────────────────────────────────────────────


def _fmt_list(items: list[str], label: str) -> str:
    body = "\n".join(f"• `{item}`" for item in items) if items else "_empty_"
    return f"*{label}*\n{body}"


async def cmd_blacklist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update):
        _reject(update)
        return
    assert update.message is not None

    args = context.args or []
    if not args or args[0] == "list":
        text = _fmt_list(filter_store.blacklist_emails(), "Blacklist")
    elif args[0] == "add" and len(args) > 1:
        email = args[1]
        text = (
            f"✓ Added `{email}` to blacklist."
            if filter_store.add_blacklist_email(email)
            else f"`{email}` is already in the blacklist."
        )
    elif args[0] == "rm" and len(args) > 1:
        email = args[1]
        text = (
            f"✓ Removed `{email}` from blacklist."
            if filter_store.remove_blacklist_email(email)
            else f"`{email}` not found in blacklist."
        )
    else:
        text = "Usage: `/blacklist [list | add <email> | rm <email>]`"
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_unavailable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update):
        _reject(update)
        return
    assert update.message is not None

    args = context.args or []
    key = "unavailable_periods"
    if not args or args[0] == "list":
        text = _fmt_list(filter_store.unavailable_periods(), "Unavailable periods")
    elif args[0] == "add" and len(args) > 1:
        period = args[1]
        text = (
            f"✓ Marked `{period}` as unavailable."
            if filter_store.add_period(key, period)
            else f"`{period}` is already in the unavailable list."
        )
    elif args[0] == "rm" and len(args) > 1:
        period = args[1]
        text = (
            f"✓ Removed `{period}` from unavailable periods."
            if filter_store.remove_period(key, period)
            else f"`{period}` not found in unavailable periods."
        )
    else:
        text = "Usage: `/unavailable [list | add <period> | rm <period>]`"
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_available(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update):
        _reject(update)
        return
    assert update.message is not None

    args = context.args or []
    key = "available_only_periods"
    if not args or args[0] == "list":
        text = _fmt_list(filter_store.available_only_periods(), "Available-only periods")
    elif args[0] == "add" and len(args) > 1:
        period = args[1]
        text = (
            f"✓ Added `{period}` to available-only periods."
            if filter_store.add_period(key, period)
            else f"`{period}` is already in the available-only list."
        )
    elif args[0] == "rm" and len(args) > 1:
        period = args[1]
        text = (
            f"✓ Removed `{period}` from available-only periods."
            if filter_store.remove_period(key, period)
            else f"`{period}` not found in available-only periods."
        )
    else:
        text = "Usage: `/available [list | add <period> | rm <period>]`"
    await update.message.reply_text(text, parse_mode="Markdown")


# ── /gigs and /deletegig ──────────────────────────────────────────────────────


async def cmd_gigs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    assert update.effective_chat is not None
    if not _is_authorised(update):
        _reject(update)
        return
    cal = _make_calendar_client()
    if cal is None:
        await update.message.reply_text("Google Calendar not configured.")
        return
    events = cal.list_upcoming_events(max_results=10)
    chat_id = update.effective_chat.id
    _gig_listing[chat_id] = events
    if not events:
        await update.message.reply_text("No upcoming gigs found.")
        return

    import datetime as _dt

    now_str = _dt.datetime.now().strftime("%H:%M")
    lines = [f"*Upcoming gigs* \\(fetched at {now_str}\\)"]
    for i, ev in enumerate(events, start=1):
        start_dt = ev["start_dt"]
        time_str = start_dt.strftime("%I:%M%p").lstrip("0").lower()
        date_str = start_dt.strftime("%a %d %b %Y").replace(" 0", " ")
        summary = (
            ev["summary"]
            .replace("\\", "\\\\")
            .replace(".", "\\.")
            .replace("-", "\\-")
            .replace("(", "\\(")
            .replace(")", "\\)")
            .replace("!", "\\!")
            .replace("|", "\\|")
            .replace("_", "\\_")
            .replace("*", "\\*")
            .replace("[", "\\[")
            .replace("]", "\\]")
        )
        lines.append(f"{i}\\. {summary} · {date_str} · {time_str}")
    lines.append("\nUse /deletegig \\<number\\> to remove one\\.")
    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")


async def cmd_deletegig(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    assert update.effective_chat is not None
    if not _is_authorised(update):
        _reject(update)
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: /deletegig <number>  — run /gigs first to see the list."
        )
        return
    try:
        n = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Usage: /deletegig <number>  — e.g. /deletegig 2")
        return

    chat_id = update.effective_chat.id
    listing = _gig_listing.get(chat_id)
    if not listing:
        await update.message.reply_text("Run /gigs first to see your upcoming gigs.")
        return
    if n < 1 or n > len(listing):
        await update.message.reply_text(f"No gig number {n}. Run /gigs to see the list.")
        return

    event = listing[n - 1]
    cal = _make_calendar_client()
    if cal is None:
        await update.message.reply_text("Google Calendar not configured.")
        return
    try:
        cal.delete_event(event["id"])
    except Exception as exc:
        await update.message.reply_text(f"Failed to delete: {exc}")
        return

    filter_store.remove_period("unavailable_periods", event["date_str"])
    _gig_listing[chat_id] = [e for i, e in enumerate(listing) if i != n - 1]
    await update.message.reply_text(
        f"✓ Deleted {event['summary']}. Date removed from unavailable if it was there."
    )


# ── Bot setup ─────────────────────────────────────────────────────────────────


def run(token: str) -> None:
    app = Application.builder().token(token).build()

    # The gig ConversationHandler must be registered first so its CHATTING
    # state intercepts free-text replies before the invoice catch-all sees them.
    gig_conv = ConversationHandler(
        entry_points=[CommandHandler("addgig", addgig_entry)],
        states={
            CHATTING: [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, gig_chat)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(gig_conv)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("gigs", cmd_gigs))
    app.add_handler(CommandHandler("deletegig", cmd_deletegig))
    app.add_handler(CommandHandler("blacklist", cmd_blacklist))
    app.add_handler(CommandHandler("unavailable", cmd_unavailable))
    app.add_handler(CommandHandler("available", cmd_available))
    app.add_handler(MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, handle_invoice))

    logger.info("Telegram bot polling", extra={"chat_id": settings.telegram_chat_id})
    app.run_polling()
