"""organist_bot/filter_suspension_store.py
──────────────────────────────────────────
File-backed store for runtime filter suspensions — temporarily exempting
gigs whose OWN date falls within a period from a named filter (or "all"
filters except "seen"). The store lives at data/filter_suspensions.json
and is read fresh on every call, mirroring filter_store.py, so the
Telegram bot can mutate it and main.py picks up the changes on the very
next polling tick without a restart.
"""

from __future__ import annotations

import calendar
import datetime
import logging
import re
from pathlib import Path

from organist_bot import atomic_store

logger = logging.getLogger(__name__)

_PATH = Path("data/filter_suspensions.json")

# "seen" is deliberately excluded: suspending it wouldn't exempt a category
# of gig, it would just re-send the same application every poll tick.
FILTER_KEYS = ("fee", "sunday_time", "blacklist", "postcode", "calendar", "availability", "all")


def _read() -> dict[str, list[dict[str, str]]]:
    raw = atomic_store.read_json(_PATH, {})
    return {"suspensions": list(raw.get("suspensions", []))}


def _parse_period_token(token: str) -> tuple[datetime.date, datetime.date] | None:
    """Parse a period token into an inclusive (start, end) date range.

    Accepted formats:
      "2026-12-25"            – single day
      "2026-12-15:2027-01-05" – inclusive date range
      "2026-12"               – full calendar month
      "2026-08-01:"           – from that date onward (open-ended end)
      ":2026-08-01"           – up to and including that date (open-ended start)
    Returns None if the token cannot be parsed.
    """
    token = token.strip()
    try:
        if token.startswith(":"):
            end = datetime.date.fromisoformat(token[1:].strip())
            return (datetime.date.min, end)
        if token.endswith(":") and token.count(":") == 1:
            start = datetime.date.fromisoformat(token[:-1].strip())
            return (start, datetime.date.max)
        if ":" in token:
            start_str, end_str = token.split(":", 1)
            return (
                datetime.date.fromisoformat(start_str.strip()),
                datetime.date.fromisoformat(end_str.strip()),
            )
        if re.fullmatch(r"\d{4}-\d{2}", token):
            year, month = int(token[:4]), int(token[5:])
            last_day = calendar.monthrange(year, month)[1]
            return (datetime.date(year, month, 1), datetime.date(year, month, last_day))
        d = datetime.date.fromisoformat(token)
        return (d, d)
    except (ValueError, AttributeError):
        return None


# ── Read helpers (fresh read each call) ───────────────────────────────────────


def list_suspensions() -> list[dict[str, str]]:
    return _read()["suspensions"]


# ── Mutations ──────────────────────────────────────────────────────────────────


def add_suspension(filter_name: str, period: str) -> bool:
    """Add a suspension. Returns True if added, False if an identical
    (filter, period) pair already exists.

    Raises ValueError if filter_name is not in FILTER_KEYS, or period cannot
    be parsed.
    """
    if filter_name not in FILTER_KEYS:
        raise ValueError(f"Unknown filter {filter_name!r}; must be one of {FILTER_KEYS}")
    if _parse_period_token(period) is None:
        raise ValueError(f"Could not parse period {period!r}")
    with atomic_store.file_lock(_PATH):
        data = _read()
        for entry in data["suspensions"]:
            if entry["filter"] == filter_name and entry["period"] == period:
                return False
        data["suspensions"].append({"filter": filter_name, "period": period})
        atomic_store.write_json(_PATH, data, lock=False)
    return True


def remove_suspension(filter_name: str, period: str) -> bool:
    """Remove a suspension by exact (filter, period) match. Returns True if removed."""
    with atomic_store.file_lock(_PATH):
        data = _read()
        before = len(data["suspensions"])
        data["suspensions"] = [
            e
            for e in data["suspensions"]
            if not (e["filter"] == filter_name and e["period"] == period)
        ]
        if len(data["suspensions"]) == before:
            return False
        atomic_store.write_json(_PATH, data, lock=False)
    return True


def purge_past_suspensions() -> int:
    """Remove suspensions whose parsed end date is in the past.

    Open-ended "from X" entries (end == date.max) are never purged — they're
    meant to be indefinite until manually removed. Unparseable entries are
    left alone (they're surfaced via load_active's warning instead).
    Returns the count removed.
    """
    today = datetime.date.today()
    with atomic_store.file_lock(_PATH):
        data = _read()
        before = len(data["suspensions"])
        kept = []
        for entry in data["suspensions"]:
            parsed = _parse_period_token(entry.get("period", ""))
            if parsed is None:
                kept.append(entry)
                continue
            _, end = parsed
            if end == datetime.date.max or end >= today:
                kept.append(entry)
        removed = before - len(kept)
        if removed:
            data["suspensions"] = kept
            atomic_store.write_json(_PATH, data, lock=False)
        return removed


def load_active() -> list[tuple[str, datetime.date, datetime.date]]:
    """Parse all current suspensions into (filter_name, start, end) tuples for
    a single per-tick snapshot. Unparseable entries are skipped with a warning."""
    snapshot = []
    for entry in list_suspensions():
        parsed = _parse_period_token(entry.get("period", ""))
        if parsed is None:
            logger.warning("load_active: could not parse period %r — skipping", entry.get("period"))
            continue
        start, end = parsed
        snapshot.append((entry.get("filter", ""), start, end))
    return snapshot


def is_suspended(
    snapshot: list[tuple[str, datetime.date, datetime.date]],
    filter_name: str,
    gig_date: datetime.date,
) -> bool:
    """Pure check: True if snapshot has an entry for filter_name (or "all")
    covering gig_date. No I/O — snapshot must come from load_active()."""
    return any(
        (name == filter_name or name == "all") and start <= gig_date <= end
        for name, start, end in snapshot
    )
