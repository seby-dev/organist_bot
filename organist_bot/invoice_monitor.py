"""organist_bot/invoice_monitor.py
───────────────────────────────────
Periodic invoice monitoring: overdue reminders and payment reply detection.

check_invoice_reminders_and_replies()
    Called every scheduler tick (main.py post-pipeline).
    Checks all emailed-but-unpaid invoices for:
    1. New Gmail replies → classify with Haiku → mark paid if confirmed
    2. Overdue (5+ days since emailed) → send one-time Telegram reminder
"""

from __future__ import annotations

import datetime
import logging

import anthropic

from organist_bot import alert
from organist_bot.config import settings
from organist_bot.integrations.invoice_generator import (
    load_invoices,
    mark_invoice_paid,
    save_invoice_field,
)

logger = logging.getLogger(__name__)

_OVERDUE_DAYS = 5

_CLASSIFY_PROMPT = """\
Does this email indicate that invoice {invoice_number} has been paid?

<email>
{body}
</email>

Reply with exactly one word: paid / unclear
"""


def _classify_payment_reply(invoice_number: str, body: str) -> str:
    """Classify a reply email as 'paid' or 'unclear' using Claude Haiku."""
    try:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        prompt = _CLASSIFY_PROMPT.format(invoice_number=invoice_number, body=body[:2000])
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content": prompt}],
        )
        block = response.content[0]
        if not isinstance(block, anthropic.types.TextBlock):
            return "unclear"
        result = block.text.strip().lower()
        return result if result == "paid" else "unclear"
    except Exception as exc:
        logger.warning("invoice_monitor: classification failed: %s", exc)
        return "unclear"


def _make_gmail_client():
    """Return a GmailClient if configured, else None."""
    if not settings.gmail_credentials_file:
        return None
    try:
        from organist_bot.integrations.gmail_client import GmailClient

        return GmailClient(settings.gmail_credentials_file, settings.gmail_token_file)
    except Exception as exc:
        logger.warning("invoice_monitor: could not build Gmail client: %s", exc)
        return None


def check_invoice_reminders_and_replies() -> None:
    """Check all emailed-but-unpaid invoices for payment replies and overdue status.

    Called in main.py post-pipeline steps on every scheduler tick.
    Fails open — logs warnings on any per-invoice error and continues.
    """
    invoices = load_invoices()
    now = datetime.datetime.now(datetime.UTC)
    gmail = _make_gmail_client()

    candidates = [inv for inv in invoices.values() if inv.get("emailed") and not inv.get("paid_at")]

    for inv in candidates:
        inv_num = inv["invoice_number"]
        try:
            _process_invoice(inv, now, gmail)
        except Exception as exc:
            logger.warning("invoice_monitor: error processing %s: %s", inv_num, exc)


def _process_invoice(
    inv: dict,
    now: datetime.datetime,
    gmail,
) -> None:
    """Process a single emailed-but-unpaid invoice."""
    inv_num = inv["invoice_number"]
    client_email = inv.get("client_email", "")
    client_name = inv.get("client_name", inv_num)
    total = inv.get("total", 0.0)
    emailed_at_str = inv.get("emailed_at")
    checked_ids: list[str] = list(inv.get("checked_reply_ids") or [])
    just_paid = False

    # ── Reply check ───────────────────────────────────────────────────────────
    if gmail and client_email:
        since_date = emailed_at_str[:10].replace("-", "/") if emailed_at_str else None
        try:
            replies = gmail.fetch_invoice_replies(
                invoice_number=inv_num,
                client_email=client_email,
                since_date=since_date,
            )
        except Exception as exc:
            logger.warning("invoice_monitor: Gmail fetch failed for %s: %s", inv_num, exc)
            replies = []

        new_ids: list[str] = []
        for msg in replies:
            msg_id = msg.get("message_id", "")
            if msg_id in checked_ids:
                continue
            new_ids.append(msg_id)
            classification = _classify_payment_reply(inv_num, msg.get("body", ""))
            if classification == "paid":
                mark_invoice_paid(inv_num)
                just_paid = True
                try:
                    alert.send_alert(
                        f"✅ Invoice {inv_num} ({client_name}, £{total:.2f})"
                        " marked as paid — reply received."
                    )
                except Exception as exc:
                    logger.warning("invoice_monitor: Telegram alert failed: %s", exc)
                break  # No need to process more replies

        if new_ids:
            save_invoice_field(inv_num, "checked_reply_ids", checked_ids + new_ids)

    if just_paid:
        return

    # ── Overdue check ─────────────────────────────────────────────────────────
    if inv.get("reminder_sent"):
        return
    if not emailed_at_str:
        return

    try:
        emailed_at = datetime.datetime.fromisoformat(emailed_at_str.replace("Z", "+00:00"))
    except ValueError:
        return

    days_since = (now - emailed_at).days
    if days_since < _OVERDUE_DAYS:
        return

    try:
        alert.send_alert(
            f"⏰ Invoice {inv_num} ({client_name}, £{total:.2f})"
            f" was sent {days_since} day{'s' if days_since != 1 else ''} ago"
            " and hasn't been paid."
        )
        save_invoice_field(inv_num, "reminder_sent", True)
    except Exception as exc:
        logger.warning("invoice_monitor: failed to send overdue reminder for %s: %s", inv_num, exc)
        # Do NOT set reminder_sent=True — retry on next tick
