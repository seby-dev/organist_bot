"""
organist_bot/telegram_bot.py
─────────────────────────────
Telegram bot for adding confirmed gig bookings to Google Calendar.

Workflow:
  1. You secure a gig by replying to the notification email manually.
  2. Send the gig URL (from organistsonline.org) to this bot in Telegram.
  3. The bot scrapes the detail page, checks your calendar for a clash,
     and either adds a timed event or tells you the date is already taken.

Security:
  Only messages from TELEGRAM_CHAT_ID are processed; all others are silently
  ignored.  Set this to your personal chat ID to ensure only you can trigger
  calendar writes.

Run alongside the main scheduler:
  python telegram_bot.py
"""

import logging
import re

from telegram import Update
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    filters as tg_filters,
)

from organist_bot.integrations.calendar_client import GoogleCalendarClient
from organist_bot.config import settings
from organist_bot.filters import normalize_to_yyyymmdd
from organist_bot.models import Gig
from organist_bot.scraper import Scraper

logger = logging.getLogger(__name__)

_GIG_URL_RE = re.compile(r"https?://organistsonline\.org/\S+")


def _is_authorised(update: Update) -> bool:
    """Return True only if the message comes from the configured chat ID."""
    return str(update.effective_chat.id) == str(settings.telegram_chat_id)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update):
        logger.warning(
            "Telegram: rejected unauthorised message",
            extra={"chat_id": update.effective_chat.id},
        )
        return

    text  = update.message.text or ""
    match = _GIG_URL_RE.search(text)

    if not match:
        await update.message.reply_text(
            "Send me a gig URL from organistsonline.org and I'll add it to your calendar."
        )
        return

    url = match.group(0)
    await update.message.reply_text(f"⏳ Fetching gig details…")

    try:
        # ── Scrape ────────────────────────────────────────────────────────────
        with Scraper() as scraper:
            html  = scraper.fetch(url)
            basic = scraper.extract_basic_from_detail(html, link=url)
            extra = scraper.extract_full_details(html)

        gig = Gig(**{**basic, **extra})
        logger.info(
            "Telegram: gig scraped",
            extra={"header": gig.header, "date": gig.date, "url": url},
        )

        # ── Calendar check ────────────────────────────────────────────────────
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

        # ── Add to calendar ───────────────────────────────────────────────────
        event_id = cal.add_gig(gig)
        logger.info(
            "Telegram: gig added to calendar",
            extra={"header": gig.header, "event_id": event_id},
        )

        await update.message.reply_text(
            f"✓ *{gig.header} — {gig.organisation}*\n"
            f"📅 {gig.date}\n"
            f"🕐 {gig.time}\n"
            f"💷 {gig.fee}\n"
            f"🆔 `{event_id}`",
            parse_mode="Markdown",
        )

    except ValueError as exc:
        # Raised by add_gig when date/time can't be parsed
        logger.warning("Telegram: could not parse gig", extra={"error": str(exc), "url": url})
        await update.message.reply_text(f"⚠️ Couldn't parse gig details: {exc}")

    except Exception as exc:
        logger.exception("Telegram: unexpected error", extra={"url": url})
        await update.message.reply_text(f"❌ Unexpected error: {exc}")


def run(token: str) -> None:
    """Build and start the bot (blocking)."""
    app = Application.builder().token(token).build()
    app.add_handler(MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, handle_message))

    logger.info("Telegram bot polling", extra={"chat_id": settings.telegram_chat_id})
    app.run_polling()
