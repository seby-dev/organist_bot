"""Reply monitor — classifies Gmail replies to active applications and dispatches actions."""

from __future__ import annotations

import datetime as _dt
import logging
from pathlib import Path

import anthropic

import organist_bot.application_store as application_store
from organist_bot import travel
from organist_bot.config import settings
from organist_bot.integrations.calendar_client import (
    GoogleCalendarClient,
)
from organist_bot.integrations.calendar_client import (
    make_calendar_client as _make_calendar_client,
)
from organist_bot.integrations.gmail_client import GmailClient

logger = logging.getLogger(__name__)

# The earliest date reply_monitor will ever search Gmail for, persisted so it
# never drifts backward. Set to "today" the first time this runs; from then
# on, replies to applications made before this floor are never retroactively
# surfaced, no matter how far back an application's applied_at goes.
_SINCE_FLOOR_PATH = Path("data/reply_monitor_since_floor.txt")

_CLASSIFY_PROMPT = """\
You are classifying an email reply related to an organ performance job application.

The applicant applied for a gig at: {organisation}
Gig date: {date}
Reply from: {sender}

<email>
{body}
</email>

Classify the reply as one of:
- accepted: The church/organisation is confirming/booking the applicant.
- rejected: The church/organisation has moved on or filled the position with someone else.
- cancellation: Either party is signalling they want to cancel an existing booking.
- unclear: Anything else (questions, ambiguous requests, logistical queries, etc.).

Reply with ONLY the classification word, nothing else."""

_TERMINAL_STATUSES = frozenset({"rejected", "declined", "no_response"})


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
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        if not resp.ok:
            logger.warning(
                "reply_monitor: Telegram notification returned HTTP %d: %s",
                resp.status_code,
                resp.text[:200],
            )
    except Exception as exc:
        logger.warning("reply_monitor: telegram notification failed: %s", exc)


def _create_calendar_event(record: dict) -> bool:
    """Create a Google Calendar event and travel buffers for an accepted booking.

    Returns True if the gig event was created successfully (buffer failure is non-fatal).
    """
    if not settings.google_calendar_id or not settings.google_calendar_credentials_file:
        return False
    try:
        from organist_bot.filters import normalize_to_yyyymmdd, parse_start_time
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
            time=record.get("time", ""),
            fee=record.get("fee", ""),
        )
        cal.add_gig(gig)

        # Travel buffers (non-fatal — gig event is already created)
        try:
            date_str = normalize_to_yyyymmdd(gig.date)
            start_time = parse_start_time(gig.time)
            if date_str and start_time:
                date = _dt.datetime.strptime(date_str, "%Y%m%d").date()
                start_dt = _dt.datetime.combine(date, start_time)
                end_dt = start_dt + _dt.timedelta(hours=1)
                postcode = record.get("postcode", "")
                travel_mins = travel.get_travel_minutes(postcode) or settings.max_travel_minutes
                before_id, after_id = cal.add_travel_buffers(
                    gig_summary=f"{gig.header} — {gig.organisation}",
                    start_dt=start_dt,
                    end_dt=end_dt,
                    travel_minutes=travel_mins,
                )
                url = record.get("url", "")
                if url:
                    application_store.update_travel_buffer_ids(url, before_id, after_id)
        except Exception as buf_exc:
            logger.warning("reply_monitor: travel buffer creation failed: %s", buf_exc)

        return True
    except Exception as exc:
        logger.warning("reply_monitor: calendar event creation failed: %s", exc)
        return False


def _extract_email_address(header: str) -> str:
    """Extract bare email address from a header value like 'Name <email@example.com>'."""
    import re

    m = re.search(r"<([^>]+)>", header)
    return m.group(1).lower() if m else header.lower()


def _match_record(message: dict, records: list[dict]) -> dict | None:
    """Find the application record whose email exactly matches the message sender or recipient."""
    msg_sender = _extract_email_address(message.get("sender", ""))
    msg_recipient = _extract_email_address(message.get("recipient", ""))
    for r in records:
        record_email = r.get("email", "").lower()
        if not record_email:
            continue
        if record_email in (msg_sender, msg_recipient):
            return r
    return None


def _since_floor(today: _dt.date) -> _dt.date:
    """Read the persisted floor date, creating it (as today) on first use."""
    if _SINCE_FLOOR_PATH.exists():
        try:
            return _dt.date.fromisoformat(_SINCE_FLOOR_PATH.read_text().strip())
        except ValueError:
            logger.warning(
                "reply_monitor: could not parse %s — resetting to today", _SINCE_FLOOR_PATH
            )
    _SINCE_FLOOR_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SINCE_FLOOR_PATH.write_text(today.isoformat() + "\n")
    return today


def check_replies() -> None:
    """Check Gmail for replies to active applications and dispatch actions.

    Fails open — infrastructure errors (DB, Gmail auth) are caught, logged,
    and a Telegram alert is sent so the operator is notified. Per-message
    errors are caught individually so one bad message does not abort the run.
    """
    if not settings.gmail_credentials_file or not settings.gmail_token_file:
        return

    try:
        records = application_store.list_applications(days=365)
    except Exception as exc:
        logger.warning("reply_monitor: could not load applications: %s", exc)
        _send_telegram_notification(f"⚠️ reply_monitor: failed to load applications — {exc}")
        return

    active = [r for r in records if r["status"] in ("applied", "accepted")]
    if not active:
        return

    applied_emails = [r["email"] for r in active if r["status"] == "applied" and r.get("email")]
    accepted_emails = [r["email"] for r in active if r["status"] == "accepted" and r.get("email")]

    # Bound Gmail search to the oldest applied_at to avoid full-inbox scans every tick.
    since = _dt.date.today() - _dt.timedelta(days=365)
    for r in active:
        try:
            applied = _dt.date.fromisoformat(r.get("applied_at", "")[:10])
            if applied < since:
                since = applied
        except (ValueError, TypeError):
            pass

    # Never look earlier than the persisted floor, so replies to applications
    # made long before Gmail monitoring came online aren't retroactively surfaced.
    floor = _since_floor(_dt.date.today())
    if since < floor:
        since = floor

    since_str = since.strftime("%Y/%m/%d")

    try:
        client = _make_gmail_client()
    except Exception as exc:
        logger.warning("reply_monitor: Gmail client init failed: %s", exc)
        _send_telegram_notification(f"⚠️ reply_monitor: Gmail auth failed — {exc}")
        return

    messages = client.fetch_reply_messages(
        applied_emails=applied_emails,
        accepted_emails=accepted_emails,
        since_date=since_str,
    )

    seen_msg_ids: set[str] = set()
    for msg in messages:
        try:
            msg_id = msg["message_id"]
            if msg_id in seen_msg_ids:
                continue

            record = _match_record(msg, active)
            if record is None:
                continue

            if record.get("reply_message_id"):
                continue

            classification = _classify_reply(msg, record)
            org = record.get("organisation") or record.get("header", "")
            date = record.get("date", "")
            url = record.get("url") or ""

            # Stamp reply_message_id BEFORE side-effects so partial failures
            # (calendar API down, Telegram down) don't cause duplicate processing.
            # For "unclear" we skip stamping so the message is re-evaluated next tick.
            if classification != "unclear":
                if url:
                    wrote = application_store.update_reply_message_id(url, msg_id)
                    if not wrote:
                        logger.warning(
                            "reply_monitor: could not persist reply_message_id for url=%r — "
                            "this reply may be reprocessed on the next tick",
                            url,
                        )
                seen_msg_ids.add(msg_id)

            if classification == "accepted":
                if record.get("status") not in _TERMINAL_STATUSES:
                    application_store.upsert_accepted(
                        url=url or None,
                        header=record.get("header", ""),
                        organisation=org,
                        date=date,
                        fee=record.get("fee", ""),
                        email=record.get("email", ""),
                    )
                calendar_ok = _create_calendar_event(record)
                note = "" if calendar_ok else "\n\n⚠️ Calendar event could not be created."
                _send_telegram_notification(
                    f"✅ Booking confirmed: {org} on {date}\n(via email reply){note}"
                )

            elif classification == "rejected":
                if url:
                    application_store.update_status(url, "rejected")
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
                # Delete travel buffer events automatically
                cal = _make_calendar_client()
                if cal:
                    for field in ("travel_before_event_id", "travel_after_event_id"):
                        evt_id = record.get(field)
                        if evt_id:
                            try:
                                cal.delete_event(evt_id)
                            except Exception as del_exc:
                                logger.warning(
                                    "reply_monitor: failed to delete travel buffer %s: %s",
                                    evt_id,
                                    del_exc,
                                )

            elif classification == "unclear":
                _send_telegram_notification(
                    f'📧 Unclassified reply from {org} ({date}):\n"{msg.get("body", "")[:300]}"'
                )

        except Exception as exc:
            logger.warning(
                "reply_monitor: error processing message %s: %s",
                msg.get("message_id"),
                exc,
            )
