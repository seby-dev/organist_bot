"""organist_bot/weekly_summary.py
──────────────────────────────────────────────────
Saturday morning Telegram digest: upcoming gigs, pending applications,
outstanding invoices.  Idempotent — stores last-sent date in
data/weekly_summary_last.txt so the summary fires at most once per Saturday.
"""

from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path

import organist_bot.alert as alert
import organist_bot.application_store as application_store
from organist_bot.config import settings

logger = logging.getLogger(__name__)

_LAST_SENT_FILE = Path("data/weekly_summary_last.txt")
_INVOICES_FILE = Path("invoices.json")


# ── persistence ───────────────────────────────────────────────────────────────


def load_last_sent_date() -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(_LAST_SENT_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def save_last_sent_date(d: datetime.date) -> None:
    _LAST_SENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    _LAST_SENT_FILE.write_text(d.isoformat())


# ── trigger logic ─────────────────────────────────────────────────────────────


def should_send(
    now: datetime.datetime,
    last_sent: datetime.date | None,
    summary_time_str: str = "09:00",
) -> bool:
    """Return True iff it is Saturday, past send time, and not already sent today."""
    if now.weekday() != 5:  # Saturday = 5
        return False
    try:
        h, m = (int(x) for x in summary_time_str.split(":"))
    except (ValueError, AttributeError):
        h, m = 9, 0
    if now.time() < datetime.time(h, m):
        return False
    if last_sent == now.date():
        return False
    return True


# ── message builder ───────────────────────────────────────────────────────────


def _load_invoices() -> list[dict]:
    try:
        data = json.loads(_INVOICES_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    # invoices.json is a dict keyed by invoice_number
    if isinstance(data, dict):
        return list(data.values())
    return data


def _parse_app_date(date_str: str) -> datetime.date:
    from organist_bot.filters import normalize_to_yyyymmdd

    normalized = normalize_to_yyyymmdd(date_str)
    if normalized:
        try:
            return datetime.datetime.strptime(normalized, "%Y%m%d").date()
        except ValueError:
            pass
    return datetime.date.max


def build_message() -> str:
    today = datetime.date.today()
    week_end = today + datetime.timedelta(days=6)

    apps = application_store.list_applications(days=90)

    upcoming = [
        a
        for a in apps
        if a.get("status") == "accepted" and today <= _parse_app_date(a.get("date", "")) <= week_end
    ]

    pending = [a for a in apps if a.get("status") == "applied"]

    invoices = _load_invoices()
    outstanding = [inv for inv in invoices if not inv.get("paid_at")]

    lines = [f"📅 Weekly Summary — {today.strftime('%A %-d %B %Y')}", ""]

    if upcoming:
        lines.append(f"🎹 Upcoming gigs this week ({len(upcoming)}):")
        for a in upcoming:
            org = a.get("organisation", "")
            lines.append(f"  • {a.get('header', 'Gig')} — {org} ({a.get('date', '')})")
    else:
        lines.append("🎹 No accepted gigs this week.")

    lines.append("")

    if pending:
        lines.append(f"📨 Pending applications ({len(pending)}):")
        for a in pending[:5]:
            org = a.get("organisation", "")
            lines.append(f"  • {a.get('header', 'Gig')} — {org} ({a.get('date', '')})")
        if len(pending) > 5:
            lines.append(f"  … and {len(pending) - 5} more")
    else:
        lines.append("📨 No pending applications.")

    lines.append("")

    if outstanding:
        lines.append(f"💷 Outstanding invoices ({len(outstanding)}):")
        for inv in outstanding[:5]:
            num = inv.get("invoice_number", "?")
            client = inv.get("client_name", inv.get("client", "?"))
            total = inv.get("total", "")
            total_str = f"£{total:.2f}" if isinstance(total, (int, float)) else str(total)
            lines.append(f"  • #{num} — {client} ({total_str})")
        if len(outstanding) > 5:
            lines.append(f"  … and {len(outstanding) - 5} more")
    else:
        lines.append("💷 No outstanding invoices.")

    return "\n".join(lines)


# ── entry point ───────────────────────────────────────────────────────────────


def check_and_send() -> None:
    """Fire the weekly summary if conditions are met; no-op otherwise."""
    now = datetime.datetime.now()
    last_sent = load_last_sent_date()
    if not should_send(now, last_sent, settings.weekly_summary_time):
        return
    try:
        msg = build_message()
        alert.send_alert(msg)
        save_last_sent_date(now.date())
        logger.info("Weekly summary sent")
    except Exception:
        logger.warning("weekly_summary: failed to send", exc_info=True)
