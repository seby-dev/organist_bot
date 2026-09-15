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


def record_held_draft(
    gig: Gig,
    *,
    status: Literal["neg_pending", "review_pending"],
    draft_id: str,
    draft_subject: str,
    hold_reason: str,
    negotiable_fee: int | None = None,
) -> tuple[str, bool]:
    """Write a new held-draft record ('neg_pending' or 'review_pending').

    Returns (gig_id, created). Idempotent by URL: if a row for this gig URL
    already exists in ANY status, nothing is written and created=False — the
    caller (main.py) must then delete the just-created Gmail draft (draft_id)
    since it's now orphaned; the existing row's own draft_id is untouched.
    """
    gig_id = _gig_id(gig.link)
    with atomic_store.file_lock(_PATH):
        records = _read()
        for r in records:
            if r.get("url") == gig.link:
                return gig_id, False
        now = _now_iso()
        records.append(
            {
                "gig_id": gig_id,
                "url": gig.link,
                "header": gig.header or "",
                "organisation": gig.organisation or "",
                "contact": gig.contact or "",
                "date": gig.date or "",
                "time": gig.time or "",
                "fee": gig.fee or "",
                "email": gig.email or "",
                "postcode": gig.postcode or "",
                "status": status,
                "draft_id": draft_id,
                "draft_subject": draft_subject,
                "negotiable_fee": negotiable_fee,
                "hold_reason": hold_reason,
                "created_at": now,
                "updated_at": now,
                "decided_at": None,
                "decision": None,
            }
        )
        _write(records)
    return gig_id, True


def list_held(status: str | None = None) -> list[dict]:
    """Return held-draft rows (status in {'neg_pending', 'review_pending'}),
    optionally filtered to just one of those statuses."""
    statuses = {status} if status else {"neg_pending", "review_pending"}
    return [r for r in _read() if r.get("status") in statuses]


def get_by_gig_id(gig_id: str) -> dict | None:
    """Return the record with this gig_id regardless of status, or None."""
    for r in _read():
        if r.get("gig_id") == gig_id:
            return r
    return None


def transition_held(gig_id: str, *, to: Literal["applied", "rejected", "expired"]) -> bool:
    """Transition a held-draft row (neg_pending or review_pending) to
    applied/rejected/expired.

    Returns False if no held row with this gig_id exists (already
    transitioned, never existed, or in a different state) — caller should
    treat False as "already decided" and not double-send/double-delete.

    On to='applied' the standard 'applied_at' field is set so downstream
    tools (get_income_forecast, manage_applications) see this like any other
    application.
    """
    with atomic_store.file_lock(_PATH):
        records = _read()
        for r in records:
            if r.get("gig_id") != gig_id:
                continue
            if r.get("status") not in ("neg_pending", "review_pending"):
                return False
            now = _now_iso()
            r["status"] = to
            r["decision"] = to
            r["decided_at"] = now
            r["updated_at"] = now
            if to == "applied":
                r["applied_at"] = now
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


def was_unclear_alerted(url: str, message_id: str) -> bool:
    """True if this exact message_id already triggered an 'unclear reply' alert.

    An 'unclear' classification deliberately does not stamp reply_message_id
    (see reply_monitor.check_replies) so a later, clearer reply to the same
    application is still picked up. Without a separate dedup key, the same
    unclear message gets reclassified and re-alerted every poll tick forever.
    """
    for r in _read():
        if r.get("url") == url:
            return message_id in (r.get("alerted_unclear_ids") or [])
    return False


def mark_unclear_alerted(url: str, message_id: str) -> bool:
    """Record that message_id has already triggered an 'unclear reply' alert
    for the record with this URL. Returns False if not found."""
    with atomic_store.file_lock(_PATH):
        records = _read()
        for r in records:
            if r.get("url") == url:
                ids = r.get("alerted_unclear_ids") or []
                if message_id not in ids:
                    ids.append(message_id)
                r["alerted_unclear_ids"] = ids
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


def expire_past_applied() -> list[dict]:
    """Mark past-date 'applied' rows as 'no_response' and past-date held-draft
    rows ('neg_pending'/'review_pending') as 'expired'.

    Returns every row whose status changed, each a shallow copy — not just a
    count — so the caller (main.py) can act on rows that carry a draft_id
    (only the expired held-draft rows have one; 'applied'->'no_response' rows
    don't) to delete the now-orphaned Gmail draft. Check row.get("draft_id")
    rather than the row's prior status to tell the two kinds apart.
    """
    from organist_bot.filters import normalize_to_yyyymmdd

    today = datetime.date.today()
    expired_rows: list[dict] = []
    with atomic_store.file_lock(_PATH):
        records = _read()
        changed = False
        now = _now_iso()
        for r in records:
            status = r.get("status")
            if status not in ("applied", "neg_pending", "review_pending"):
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
                else:  # neg_pending or review_pending
                    r["status"] = "expired"
                    r["decision"] = "expired"
                    r["decided_at"] = now
                r["updated_at"] = now
                changed = True
                expired_rows.append(dict(r))
        if changed:
            _write(records)
    return expired_rows


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
