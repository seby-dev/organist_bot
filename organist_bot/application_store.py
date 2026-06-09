"""organist_bot/application_store.py
──────────────────────────────────────────────────
Track every gig application through its lifecycle.
Backed by data/applications.json — a flat JSON array, one object per application.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
from pathlib import Path
from typing import Literal

from organist_bot import atomic_store
from organist_bot.models import Gig

logger = logging.getLogger(__name__)

_PATH = Path("data/applications.json")


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read() -> list[dict]:
    return atomic_store.read_json(_PATH, [])


def _write(records: list[dict]) -> None:
    """Atomically write records. Caller MUST hold file_lock(_PATH)."""
    atomic_store.write_json(_PATH, records, lock=False)


def record_application(gig: Gig) -> bool:
    """Write a new 'applied' record. Returns False if URL already exists (idempotent)."""
    with atomic_store.file_lock(_PATH):
        records = _read()
        if any(r.get("url") == gig.link for r in records):
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


def _gig_id(link: str) -> str:
    """Deterministic short id derived from the gig URL."""
    return hashlib.sha256(link.encode()).hexdigest()[:12]


def record_neg_pending(
    gig: Gig,
    *,
    draft_subject: str,
    draft_body: str,
    negotiable_fee: int,
) -> str:
    """Write a new 'neg_pending' record. Returns the gig_id.

    Idempotent: if a row for this gig URL already exists in any state
    (neg_pending or otherwise), returns the existing gig_id without modifying
    the row — the original draft the user is reviewing is preserved.
    """
    gig_id = _gig_id(gig.link)
    with atomic_store.file_lock(_PATH):
        records = _read()
        for r in records:
            if r.get("url") == gig.link:
                return gig_id
        now = _now_iso()
        records.append(
            {
                "gig_id": gig_id,
                "url": gig.link,
                "header": gig.header or "",
                "organisation": gig.organisation or "",
                "date": gig.date or "",
                "time": gig.time or "",
                "fee": gig.fee or "",
                "email": gig.email or "",
                "postcode": gig.postcode or "",
                "status": "neg_pending",
                "draft_subject": draft_subject,
                "draft_body": draft_body,
                "negotiable_fee": negotiable_fee,
                "created_at": now,
                "updated_at": now,
                "decided_at": None,
                "decision": None,
            }
        )
        _write(records)
    return gig_id


def list_neg_pending() -> list[dict]:
    """Return all records with status == 'neg_pending'."""
    return [r for r in _read() if r.get("status") == "neg_pending"]


def get_by_gig_id(gig_id: str) -> dict | None:
    """Return the record with this gig_id regardless of status, or None."""
    for r in _read():
        if r.get("gig_id") == gig_id:
            return r
    return None


def transition_neg_pending(
    gig_id: str,
    *,
    to: Literal["applied", "rejected", "expired"],
    sent_body: str | None = None,
) -> bool:
    """Transition a neg_pending row to applied/rejected/expired.

    Returns False if no neg_pending row with this gig_id exists (already
    transitioned, never existed, or in a different state) — caller should
    treat False as "already decided" and not double-send.

    On to='applied' the standard 'applied_at' field is set so downstream
    tools (get_income_forecast, manage_applications) see this like any
    other application. If sent_body is provided, draft_body is overwritten
    (for the edit case).
    """
    with atomic_store.file_lock(_PATH):
        records = _read()
        for r in records:
            if r.get("gig_id") != gig_id:
                continue
            if r.get("status") != "neg_pending":
                return False
            now = _now_iso()
            r["status"] = to
            r["decision"] = to
            r["decided_at"] = now
            r["updated_at"] = now
            if to == "applied":
                r["applied_at"] = now
                if sent_body is not None:
                    r["draft_body"] = sent_body
            _write(records)
            return True
    return False


def update_status(url: str, status: str) -> bool:
    """Update status and updated_at for the record with the given URL. Returns False if not found."""
    with atomic_store.file_lock(_PATH):
        records = _read()
        for r in records:
            if r.get("url") == url:
                r["status"] = status
                r["updated_at"] = _now_iso()
                _write(records)
                return True
    return False


def update_reply_message_id(url: str, message_id: str) -> bool:
    """Set reply_message_id on the record with the given URL. Returns False if not found."""
    with atomic_store.file_lock(_PATH):
        records = _read()
        for r in records:
            if r.get("url") == url:
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
    time: str = "",
) -> None:
    """Create or update a record to 'accepted'.

    If url is given and matches an existing record, updates it in place.
    Otherwise creates a new 'accepted' record (url may be None for manual entries).
    """
    with atomic_store.file_lock(_PATH):
        records = _read()
        now = _now_iso()
        if url is not None:
            for r in records:
                if r.get("url") == url:
                    r["status"] = "accepted"
                    r["updated_at"] = now
                    if postcode:
                        r["postcode"] = postcode
                    if time:
                        r["time"] = time
                    _write(records)
                    return
        records.append(
            {
                "url": url or "",
                "header": header,
                "organisation": organisation,
                "date": date,
                "time": time,
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
    with atomic_store.file_lock(_PATH):
        records = _read()
        for r in records:
            if r.get("url") == url:
                r["travel_before_event_id"] = before_id
                r["travel_after_event_id"] = after_id
                r["updated_at"] = _now_iso()
                _write(records)
                return True
    return False


def expire_past_applied() -> int:
    """Mark past-date 'applied' rows as 'no_response' and past-date 'neg_pending'
    rows as 'expired'. Returns total count changed.
    """
    from organist_bot.filters import normalize_to_yyyymmdd

    today = datetime.date.today()
    with atomic_store.file_lock(_PATH):
        records = _read()
        changed = 0
        now = _now_iso()
        for r in records:
            status = r.get("status")
            if status not in ("applied", "neg_pending"):
                continue
            normalized = normalize_to_yyyymmdd(r.get("date", ""))
            if normalized is None:
                continue
            try:
                gig_date = datetime.datetime.strptime(normalized, "%Y%m%d").date()
            except ValueError:
                continue
            if gig_date < today:
                if status == "applied":
                    r["status"] = "no_response"
                else:  # neg_pending
                    r["status"] = "expired"
                    r["decision"] = "expired"
                    r["decided_at"] = now
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
        applied_at_raw = r.get("applied_at")
        if not applied_at_raw:
            continue
        try:
            applied_at = datetime.datetime.fromisoformat(applied_at_raw.replace("Z", "+00:00"))
        except Exception:
            continue
        if applied_at >= cutoff:
            result.append(r)
    result.sort(key=lambda r: r.get("applied_at", ""), reverse=True)
    return result
