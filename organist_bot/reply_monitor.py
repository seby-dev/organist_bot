"""Reply monitor — classifies Gmail replies to active applications and dispatches actions."""

from __future__ import annotations

import logging

import anthropic

import organist_bot.application_store as application_store
from organist_bot.config import settings
from organist_bot.integrations.gmail_client import GmailClient

logger = logging.getLogger(__name__)

_CLASSIFY_PROMPT = """\
You are classifying an email reply related to an organ performance job application.

The applicant applied for a gig at: {organisation}
Gig date: {date}
Reply from: {sender}
Reply body:
{body}

Classify the reply as one of:
- accepted: The church/organisation is confirming/booking the applicant.
- rejected: The church/organisation has moved on or filled the position with someone else.
- cancellation: Either party is signalling they want to cancel an existing booking.
- unclear: Anything else (questions, ambiguous requests, logistical queries, etc.).

Reply with ONLY the classification word, nothing else."""


def _make_gmail_client() -> GmailClient:
    return GmailClient(
        credentials_file=settings.gmail_credentials_file,
        token_file=settings.gmail_token_file,
    )


def _classify_reply(message: dict, record: dict) -> str:
    """Call Claude to classify a reply. Returns: accepted / rejected / cancellation / unclear."""
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    prompt = _CLASSIFY_PROMPT.format(
        organisation=record.get("organisation", ""),
        date=record.get("date", ""),
        sender=message.get("sender", ""),
        body=message.get("body", "")[:2000],
    )
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content": prompt}],
        )
        block = response.content[0]
        if not isinstance(block, anthropic.types.TextBlock):
            return "unclear"
        result = block.text.strip().lower()
        if result not in ("accepted", "rejected", "cancellation", "unclear"):
            logger.warning("Unexpected classification: %r — treating as unclear", result)
            return "unclear"
        return result
    except Exception as exc:
        logger.warning("reply_monitor: classification failed: %s", exc)
        return "unclear"


def _send_telegram_notification(text: str) -> None:
    """Fire-and-forget Telegram notification."""
    import requests

    token = settings.telegram_bot_token
    chat_id = settings.telegram_chat_id
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
    except Exception as exc:
        logger.warning("reply_monitor: telegram notification failed: %s", exc)


def _create_calendar_event(record: dict) -> None:
    """Create a Google Calendar event for an accepted booking."""
    if not settings.google_calendar_id or not settings.google_calendar_credentials_file:
        return
    try:
        from organist_bot.integrations.calendar_client import GoogleCalendarClient
        from organist_bot.models import Gig

        cal = GoogleCalendarClient(
            credentials_file=settings.google_calendar_credentials_file,
            calendar_id=settings.google_calendar_id,
        )
        gig = Gig(
            link=record.get("url", ""),
            header=record.get("header", record.get("organisation", "Gig")),
            organisation=record.get("organisation", ""),
            locality="",
            date=record.get("date", ""),
            time="",
            fee=record.get("fee", ""),
        )
        cal.add_gig(gig)
    except Exception as exc:
        logger.warning("reply_monitor: calendar event creation failed: %s", exc)


def _match_record(message: dict, records: list[dict]) -> dict | None:
    """Find the application record whose email matches the message sender or recipient."""
    msg_sender = message.get("sender", "").lower()
    msg_recipient = message.get("recipient", "").lower()
    for r in records:
        record_email = r.get("email", "").lower()
        if not record_email:
            continue
        if record_email in msg_sender or record_email in msg_recipient:
            return r
    return None


def check_replies() -> None:
    """Check Gmail for replies to active applications and dispatch actions."""
    if not settings.gmail_credentials_file:
        return
    try:
        records = application_store.list_applications(days=365)
        active = [r for r in records if r["status"] in ("applied", "accepted")]
        if not active:
            return

        applied_emails = [r["email"] for r in active if r["status"] == "applied" and r.get("email")]
        accepted_emails = [
            r["email"] for r in active if r["status"] == "accepted" and r.get("email")
        ]

        client = _make_gmail_client()
        messages = client.fetch_reply_messages(
            applied_emails=applied_emails,
            accepted_emails=accepted_emails,
        )

        for msg in messages:
            try:
                record = _match_record(msg, active)
                if record is None:
                    continue

                if record.get("reply_message_id"):
                    continue

                classification = _classify_reply(msg, record)
                org = record.get("organisation") or record.get("header", "")
                date = record.get("date", "")

                if classification == "accepted":
                    application_store.upsert_accepted(
                        url=record.get("url") or None,
                        header=record.get("header", ""),
                        organisation=org,
                        date=date,
                        fee=record.get("fee", ""),
                    )
                    _create_calendar_event(record)
                    _send_telegram_notification(
                        f"✅ Booking confirmed: {org} on {date}\n(via email reply)"
                    )

                elif classification == "rejected":
                    application_store.update_status(record["url"], "rejected")
                    _send_telegram_notification(
                        f"❌ Application rejected: {org} on {date}\n(via email reply)"
                    )

                elif classification == "cancellation":
                    _send_telegram_notification(
                        f"⚠️ Possible cancellation: {org} on {date}\n"
                        f"Reply from: {msg.get('sender', 'unknown')}\n"
                        f'"{msg.get("body", "")[:200]}"\n\n'
                        "Delete calendar event or ignore?"
                    )

                elif classification == "unclear":
                    _send_telegram_notification(
                        f'📧 Unclassified reply from {org} ({date}):\n"{msg.get("body", "")[:300]}"'
                    )

                application_store.update_reply_message_id(record.get("url", ""), msg["message_id"])

            except Exception as exc:
                logger.warning(
                    "reply_monitor: error processing message %s: %s",
                    msg.get("message_id"),
                    exc,
                )

    except Exception:
        logger.warning("reply_monitor: check_replies failed", exc_info=True)
