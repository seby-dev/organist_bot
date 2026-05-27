"""organist_bot/application_store.py
──────────────────────────────────────────────────
Track every gig application through its lifecycle.
Backed by data/applications.json — a flat JSON array, one object per application.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import tempfile
from pathlib import Path

from organist_bot.models import Gig

logger = logging.getLogger(__name__)

_PATH = Path("data/applications.json")


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read() -> list[dict]:
    if not _PATH.exists():
        return []
    try:
        return json.loads(_PATH.read_text())
    except Exception:
        logger.exception("application_store: failed to read %s", _PATH)
        return []


def _write(records: list[dict]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(_PATH.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(records, f, indent=2)
            f.write("\n")
        os.replace(tmp, str(_PATH))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def record_application(gig: Gig) -> bool:
    """Write a new 'applied' record. Returns False if URL already exists (idempotent)."""
    records = _read()
    if any(r["url"] == gig.link for r in records):
        return False
    now = _now_iso()
    records.append(
        {
            "url": gig.link,
            "header": gig.header or "",
            "organisation": gig.organisation or "",
            "date": gig.date or "",
            "time": gig.time or "",
            "fee": gig.fee or "",
            "email": gig.email or "",
            "postcode": gig.postcode or "",
            "status": "applied",
            "applied_at": now,
            "updated_at": now,
        }
    )
    _write(records)
    return True


def update_status(url: str, status: str) -> bool:
    """Update status and updated_at for the record with the given URL. Returns False if not found."""
    records = _read()
    for r in records:
        if r["url"] == url:
            r["status"] = status
            r["updated_at"] = _now_iso()
            _write(records)
            return True
    return False


def update_reply_message_id(url: str, message_id: str) -> bool:
    """Set reply_message_id on the record with the given URL. Returns False if not found."""
    records = _read()
    for r in records:
        if r["url"] == url:
            r["reply_message_id"] = message_id
            r["updated_at"] = _now_iso()
            _write(records)
            return True
    return False


def upsert_accepted(
    url: str | None,
    header: str,
    organisation: str,
    date: str,
    fee: str,
    email: str = "",
    *,
    postcode: str = "",
) -> None:
    """Create or update a record to 'accepted'.

    If url is given and matches an existing record, updates it in place.
    Otherwise creates a new 'accepted' record (url may be None for manual entries).
    """
    records = _read()
    now = _now_iso()
    if url is not None:
        for r in records:
            if r["url"] == url:
                r["status"] = "accepted"
                r["updated_at"] = now
                if postcode:
                    r["postcode"] = postcode
                _write(records)
                return
    records.append(
        {
            "url": url or "",
            "header": header,
            "organisation": organisation,
            "date": date,
            "time": "",
            "fee": fee,
            "email": email,
            "postcode": postcode,
            "status": "accepted",
            "applied_at": now,
            "updated_at": now,
        }
    )
    _write(records)


def update_travel_buffer_ids(url: str, before_id: str, after_id: str) -> bool:
    """Set travel_before_event_id and travel_after_event_id on the record with the given URL.

    Returns False if not found.
    """
    records = _read()
    for r in records:
        if r["url"] == url:
            r["travel_before_event_id"] = before_id
            r["travel_after_event_id"] = after_id
            r["updated_at"] = _now_iso()
            _write(records)
            return True
    return False


def expire_past_applied() -> int:
    """Mark all 'applied' records whose date < today as 'no_response'. Returns count changed."""
    from organist_bot.filters import normalize_to_yyyymmdd

    today = datetime.date.today()
    records = _read()
    changed = 0
    now = _now_iso()
    for r in records:
        if r["status"] != "applied":
            continue
        normalized = normalize_to_yyyymmdd(r["date"])
        if normalized is None:
            continue
        try:
            gig_date = datetime.datetime.strptime(normalized, "%Y%m%d").date()
        except ValueError:
            continue
        if gig_date < today:
            r["status"] = "no_response"
            r["updated_at"] = now
            changed += 1
    if changed:
        _write(records)
    return changed


def _parse_fee(fee_str: str) -> float | None:
    """Extract first numeric value from a fee string. Returns None if empty or no number found."""
    import re

    if not fee_str or not fee_str.strip():
        return None
    m = re.search(r"[\d,]+(?:\.\d+)?", fee_str.replace("£", "").replace("$", ""))
    if not m:
        return None
    try:
        return float(m.group().replace(",", ""))
    except ValueError:
        return None


def get_income(from_date: str, to_date: str) -> dict:
    """Return income summary for accepted records where gig date falls in [from_date, to_date] inclusive."""
    _empty: dict = {"total": 0.0, "count": 0, "no_fee_count": 0, "records": []}
    try:
        start = datetime.date.fromisoformat(from_date)
        end = datetime.date.fromisoformat(to_date)
        records = _read()
        matched = []
        for r in records:
            if r.get("status") != "accepted":
                continue
            try:
                gig_date = datetime.date.fromisoformat(r.get("date", ""))
            except ValueError:
                continue
            if start <= gig_date <= end:
                matched.append(r)
        matched.sort(key=lambda r: r.get("date", ""))
        total = 0.0
        no_fee_count = 0
        for r in matched:
            fee = _parse_fee(r.get("fee", ""))
            if fee is None:
                no_fee_count += 1
            else:
                total += fee
        return {
            "total": total,
            "count": len(matched),
            "no_fee_count": no_fee_count,
            "records": matched,
        }
    except Exception:
        logger.exception("application_store: get_income failed")
        return _empty


def list_applications(days: int = 30) -> list[dict]:
    """Return all records with applied_at within the last N days, newest first."""
    records = _read()
    cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days)
    result = []
    for r in records:
        try:
            applied_at = datetime.datetime.fromisoformat(r["applied_at"].replace("Z", "+00:00"))
        except Exception:
            continue
        if applied_at >= cutoff:
            result.append(r)
    result.sort(key=lambda r: r["applied_at"], reverse=True)
    return result
