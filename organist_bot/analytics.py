"""organist_bot/analytics.py
──────────────────────────────────────────────────
Pure analytics functions over the application store.

No I/O side-effects — all functions read from
application_store.list_applications() and return plain dicts.
"""

from __future__ import annotations

import datetime
import logging

import organist_bot.application_store as application_store

logger = logging.getLogger(__name__)

# Statuses treated as "rejected" for analytics (grouped together)
_REJECTED_STATUSES = frozenset({"rejected", "declined"})

_EMPTY_METRICS: dict = {
    "total": 0,
    "accepted": 0,
    "rejected": 0,
    "no_response": 0,
    "applied": 0,
    "acceptance_rate": 0.0,
    "response_rate": 0.0,
    "avg_response_days": None,
}


def get_success_metrics(days: int = 365) -> dict[str, object]:
    """Return application success metrics for the given lookback window in days.

    Returns a dict with keys:
      total, accepted, rejected, no_response, applied,
      acceptance_rate, response_rate, avg_response_days.

    - ``rejected`` includes both "rejected" and "declined" statuses.
    - ``applied`` (still-pending) records are excluded from rate denominators.
    - ``avg_response_days`` is None if there are no resolved (accepted/rejected) records.

    Returns the empty-metrics sentinel on any exception.
    """

    try:
        records = application_store.list_applications(days)

        n_accepted = sum(1 for r in records if r["status"] == "accepted")
        n_rejected = sum(1 for r in records if r["status"] in _REJECTED_STATUSES)
        n_no_response = sum(1 for r in records if r["status"] == "no_response")
        n_applied = sum(1 for r in records if r["status"] == "applied")
        total = len(records)

        resolved = n_accepted + n_rejected + n_no_response
        acceptance_rate = round(n_accepted / resolved * 100, 1) if resolved else 0.0
        response_rate = round((n_accepted + n_rejected) / resolved * 100, 1) if resolved else 0.0

        response_days: list[float] = []
        for r in records:
            if r["status"] not in ("accepted", *_REJECTED_STATUSES):
                continue
            try:
                applied_at = datetime.datetime.fromisoformat(r["applied_at"].replace("Z", "+00:00"))
                updated_at = datetime.datetime.fromisoformat(r["updated_at"].replace("Z", "+00:00"))
                response_days.append(float((updated_at - applied_at).days))
            except (KeyError, ValueError, TypeError):
                continue

        avg_response_days: float | None = (
            round(sum(response_days) / len(response_days), 1) if response_days else None
        )

        return {
            "total": total,
            "accepted": n_accepted,
            "rejected": n_rejected,
            "no_response": n_no_response,
            "applied": n_applied,
            "acceptance_rate": acceptance_rate,
            "response_rate": response_rate,
            "avg_response_days": avg_response_days,
        }
    except Exception:
        logger.exception("analytics.get_success_metrics failed")
        return dict(_EMPTY_METRICS)


# Keyword → label, checked in priority order (first match wins).
_GIG_TYPE_KEYWORDS: list[tuple[str, str]] = [
    ("carol", "Carol Service"),
    ("wedding", "Wedding"),
    ("funeral", "Funeral"),
    ("memorial", "Memorial"),
    ("requiem", "Requiem"),
    ("concert", "Concert"),
    ("recital", "Recital"),
    ("christmas", "Christmas"),
    ("easter", "Easter"),
    ("school", "School"),
    ("graduation", "Graduation"),
    ("service", "Service"),
]


def _classify_gig_type(header: str) -> str:
    """Return the gig type label for a header string (case-insensitive, first keyword match)."""
    h = header.lower()
    for keyword, label in _GIG_TYPE_KEYWORDS:
        if keyword in h:
            return label
    return "Other"


def get_gig_type_breakdown(days: int = 365) -> dict[str, dict[str, int | float]]:
    """Return breakdown of applications and acceptance rates by gig type.

    Classifies each record's ``header`` field using keyword matching.
    Returns a dict keyed by type label:
      {"Wedding": {"count": int, "accepted": int, "acceptance_rate": float}, ...}

    Returns {} on any exception.
    """
    try:
        records = application_store.list_applications(days)
        breakdown: dict[str, dict[str, int | float]] = {}
        for r in records:
            gig_type = _classify_gig_type(r.get("header", ""))
            if gig_type not in breakdown:
                breakdown[gig_type] = {"count": 0, "accepted": 0, "acceptance_rate": 0.0}
            breakdown[gig_type]["count"] = int(breakdown[gig_type]["count"]) + 1
            if r["status"] == "accepted":
                breakdown[gig_type]["accepted"] = int(breakdown[gig_type]["accepted"]) + 1

        for entry in breakdown.values():
            count = int(entry["count"])
            accepted = int(entry["accepted"])
            entry["acceptance_rate"] = round(accepted / count * 100, 1) if count else 0.0

        return breakdown
    except Exception:
        logger.exception("analytics.get_gig_type_breakdown failed")
        return {}
