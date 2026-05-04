"""organist_bot/filter_store.py
──────────────────────────────
File-backed store for runtime-editable filter values.

The store lives at data/filter_config.json and is read fresh on every
call — so the Telegram bot can mutate it and main.py picks up the changes
on the very next polling tick without a restart.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_PATH = Path("data/filter_config.json")

_KEYS = ("blacklist_emails", "unavailable_periods", "available_only_periods")


def _read() -> dict[str, list[str]]:
    if not _PATH.exists():
        return {k: [] for k in _KEYS}
    try:
        raw = json.loads(_PATH.read_text())
        return {k: list(raw.get(k, [])) for k in _KEYS}
    except Exception:
        logger.exception("filter_store: failed to read %s — using empty config", _PATH)
        return {k: [] for k in _KEYS}


def _write(data: dict[str, list[str]]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(data, indent=2) + "\n")


# ── Read helpers (fresh read each call) ───────────────────────────────────────


def blacklist_emails() -> list[str]:
    return _read()["blacklist_emails"]


def unavailable_periods() -> list[str]:
    return _read()["unavailable_periods"]


def available_only_periods() -> list[str]:
    return _read()["available_only_periods"]


# ── Blacklist mutations ───────────────────────────────────────────────────────


def add_blacklist_email(email: str) -> bool:
    """Add email (lowercased). Returns True if added, False if already present."""
    data = _read()
    normalized = email.lower().strip()
    if normalized in {e.lower() for e in data["blacklist_emails"]}:
        return False
    data["blacklist_emails"].append(normalized)
    _write(data)
    return True


def remove_blacklist_email(email: str) -> bool:
    """Remove email. Returns True if removed, False if not found."""
    data = _read()
    normalized = email.lower().strip()
    before = len(data["blacklist_emails"])
    data["blacklist_emails"] = [e for e in data["blacklist_emails"] if e.lower() != normalized]
    if len(data["blacklist_emails"]) == before:
        return False
    _write(data)
    return True


# ── Period mutations (shared by unavailable and available_only) ───────────────


def add_period(key: str, period: str) -> bool:
    """Add a period token. Returns True if added, False if already present."""
    data = _read()
    if period in data[key]:
        return False
    data[key].append(period)
    _write(data)
    return True


def remove_period(key: str, period: str) -> bool:
    """Remove a period token. Returns True if removed, False if not found."""
    data = _read()
    before = len(data[key])
    data[key] = [p for p in data[key] if p != period]
    if len(data[key]) == before:
        return False
    _write(data)
    return True
