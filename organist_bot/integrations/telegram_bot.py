"""
organist_bot/integrations/telegram_bot.py
──────────────────────────────────────────
Unified Telegram bot for the organist toolkit.

Commands
--------
  /start          — Show help
  /addgig <url>   — Scrape a gig URL and add it to Google Calendar
  /addgig         — Manually enter gig details step-by-step
  /cancel         — Cancel an in-progress manual gig entry
  /reset          — Clear invoice conversation history

Everything else (free text) is routed to the invoice AI agent.

Security: only messages from TELEGRAM_CHAT_ID are processed.
"""

import logging
import os
import re

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

from organist_bot.config import settings
from organist_bot.filters import normalize_to_yyyymmdd
from organist_bot.models import Gig

logger = logging.getLogger(__name__)

_GIG_URL_RE = re.compile(r"https?://organistsonline\.org/\S+")

# ConversationHandler states for manual gig entry
TITLE, ORG, LOCALITY, DATE, TIME, FEE, CONFIRM = range(7)

_HELP = (
    "*Organist Bot*\n\n"
    "*Gig calendar*\n"
    "  /addgig \\<url\\> — Add a gig by URL\n"
    "  /addgig         — Add a gig manually\n"
    "  /cancel         — Cancel manual entry\n\n"
    "*Invoicing*\n"
    "  Just type your request in plain English, e.g.:\n"
    '  "Send an invoice to Holy Cross for March Masses, £240"\n'
    '  "List my clients"\n'
    "  /reset — Clear invoice conversation history"
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


# ── /addgig — entry point (URL or start manual flow) ─────────────────────────


async def addgig_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_authorised(update):
        _reject(update)
        return ConversationHandler.END
    assert update.message is not None
    assert context.user_data is not None

    if context.args:
        url = context.args[0]
        if not _GIG_URL_RE.match(url):
            await update.message.reply_text(
                "That doesn't look like an organistsonline.org URL. "
                "Use `/addgig` without arguments to enter a gig manually.",
                parse_mode="Markdown",
            )
            return ConversationHandler.END
        await _add_gig_from_url(update, url)
        return ConversationHandler.END

    context.user_data["gig"] = {}
    await update.message.reply_text(
        '*Add Gig Manually*\n\nStep 1/6 — Gig title / header:\n_(e.g. "Sunday Morning Eucharist")_',
        parse_mode="Markdown",
    )
    return TITLE


async def _add_gig_from_url(update: Update, url: str) -> None:
    assert update.message is not None
    from organist_bot.scraper import Scraper

    await update.message.reply_text("⏳ Fetching gig details…")
    try:
        with Scraper() as scraper:
            html = scraper.fetch(url)
            basic = scraper.extract_basic_from_detail(html, link=url)
            extra = scraper.extract_full_details(html)

        gig = Gig(**{**basic, **extra})
        logger.info(
            "Telegram: gig scraped", extra={"header": gig.header, "date": gig.date, "url": url}
        )
        await _book_gig(update, gig)

    except ValueError as exc:
        logger.warning("Telegram: could not parse gig", extra={"error": str(exc), "url": url})
        await update.message.reply_text(f"⚠️ Couldn't parse gig details: {exc}")
    except Exception:
        logger.exception("Telegram: unexpected error scraping gig", extra={"url": url})
        await update.message.reply_text("❌ Unexpected error fetching the gig.")


# ── Manual gig entry — step handlers ─────────────────────────────────────────


async def got_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None and context.user_data is not None
    context.user_data["gig"]["header"] = (update.message.text or "").strip()
    await update.message.reply_text(
        'Step 2/6 — Organisation / church name:\n_(e.g. "St Mary\'s, Battersea")_',
        parse_mode="Markdown",
    )
    return ORG


async def got_org(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None and context.user_data is not None
    context.user_data["gig"]["organisation"] = (update.message.text or "").strip()
    await update.message.reply_text(
        'Step 3/6 — Locality / town:\n_(e.g. "Battersea, London")_',
        parse_mode="Markdown",
    )
    return LOCALITY


async def got_locality(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None and context.user_data is not None
    context.user_data["gig"]["locality"] = (update.message.text or "").strip()
    await update.message.reply_text(
        'Step 4/6 — Date:\n_(e.g. "15 June 2026" or "15/06/2026")_',
        parse_mode="Markdown",
    )
    return DATE


async def got_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None and context.user_data is not None
    date_text = (update.message.text or "").strip()
    if not normalize_to_yyyymmdd(date_text):
        await update.message.reply_text(
            '⚠️ Couldn\'t parse that date. Try a format like "15 June 2026" or "15/06/2026".'
        )
        return DATE
    context.user_data["gig"]["date"] = date_text
    await update.message.reply_text(
        'Step 5/6 — Start time:\n_(e.g. "10:30am" or "10:30")_',
        parse_mode="Markdown",
    )
    return TIME


async def got_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None and context.user_data is not None
    context.user_data["gig"]["time"] = (update.message.text or "").strip()
    await update.message.reply_text(
        'Step 6/6 — Fee:\n_(e.g. "£150" — or type *skip* if unknown)_',
        parse_mode="Markdown",
    )
    return FEE


async def got_fee(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None and context.user_data is not None
    raw = (update.message.text or "").strip()
    context.user_data["gig"]["fee"] = None if raw.lower() == "skip" else raw

    data = context.user_data["gig"]
    summary = (
        f"*Confirm gig details*\n\n"
        f"Title:        {data['header']}\n"
        f"Organisation: {data['organisation']}\n"
        f"Locality:     {data['locality']}\n"
        f"Date:         {data['date']}\n"
        f"Time:         {data['time']}\n"
        f"Fee:          {data['fee'] or '—'}\n\n"
        "Add to calendar? Reply *yes* to confirm or *no* to cancel."
    )
    await update.message.reply_text(summary, parse_mode="Markdown")
    return CONFIRM


async def got_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None and context.user_data is not None
    answer = (update.message.text or "").strip().lower()
    if answer not in ("yes", "y"):
        await update.message.reply_text("Cancelled — gig not added.")
        context.user_data.pop("gig", None)
        return ConversationHandler.END

    data = context.user_data.pop("gig")
    gig = Gig(
        header=data["header"],
        organisation=data["organisation"],
        locality=data["locality"],
        date=data["date"],
        time=data["time"],
        fee=data.get("fee"),
        link="",
    )
    await _book_gig(update, gig)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_authorised(update):
        _reject(update)
        return ConversationHandler.END
    assert update.message is not None and context.user_data is not None
    context.user_data.pop("gig", None)
    await update.message.reply_text("Manual gig entry cancelled.")
    return ConversationHandler.END


# ── Calendar booking (shared by URL and manual flows) ─────────────────────────


async def _book_gig(update: Update, gig: Gig) -> None:
    assert update.message is not None
    from organist_bot.integrations.calendar_client import GoogleCalendarClient

    cal = GoogleCalendarClient(
        credentials_file=settings.google_calendar_credentials_file,
        calendar_id=settings.google_calendar_id,
    )

    date_str = normalize_to_yyyymmdd(gig.date)
    if date_str and cal.has_event_on_date(date_str):
        await update.message.reply_text(
            f"✗ Already have an event on *{gig.date}* — not adding.",
            parse_mode="Markdown",
        )
        return

    try:
        event_id = cal.add_gig(gig)
    except ValueError as exc:
        await update.message.reply_text(f"⚠️ Couldn't add to calendar: {exc}")
        return

    logger.info(
        "Telegram: gig added to calendar", extra={"header": gig.header, "event_id": event_id}
    )
    await update.message.reply_text(
        f"✓ *{gig.header} — {gig.organisation}*\n"
        f"📅 {gig.date}\n"
        f"🕐 {gig.time}\n"
        f"💷 {gig.fee or '—'}\n"
        f"🆔 `{event_id}`",
        parse_mode="Markdown",
    )


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


# ── Bot setup ─────────────────────────────────────────────────────────────────


def run(token: str) -> None:
    app = Application.builder().token(token).build()

    # ConversationHandler registered first: captures /addgig and all manual-entry
    # replies before the invoice free-text handler can see them.
    manual_gig_conv = ConversationHandler(
        entry_points=[CommandHandler("addgig", addgig_entry)],
        states={
            TITLE: [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, got_title)],
            ORG: [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, got_org)],
            LOCALITY: [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, got_locality)],
            DATE: [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, got_date)],
            TIME: [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, got_time)],
            FEE: [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, got_fee)],
            CONFIRM: [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, got_confirm)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(manual_gig_conv)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, handle_invoice))

    logger.info("Telegram bot polling", extra={"chat_id": settings.telegram_chat_id})
    app.run_polling()
