"""organist_bot/integrations/agent_state.py
Disk-backed per-chat agent context so a bot restart doesn't drop the "last
invoice" / "last listing" the user refers to ("email that invoice", "delete
gig 2 from the list"). Conversation history is intentionally NOT persisted —
only the small reference-context fields below.

Backed by data/agent_state.json: {"<chat_id>": {"last_invoice": ..., ...}}.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from organist_bot import atomic_store

_PATH = Path("data/agent_state.json")
_KEYS = ("last_invoice", "last_gig_listing", "last_application_listing")


def load_chat(chat_id: int) -> dict[str, Any]:
    """Return the persisted reference-context for chat_id (missing fields → None)."""
    data = atomic_store.read_json(_PATH, {})
    entry = data.get(str(chat_id), {})
    return {k: entry.get(k) for k in _KEYS}


def save_chat(chat_id: int, state: dict[str, Any]) -> None:
    """Persist the reference-context for chat_id (only _KEYS are stored)."""
    with atomic_store.file_lock(_PATH):
        data = atomic_store.read_json(_PATH, {})
        data[str(chat_id)] = {k: state.get(k) for k in _KEYS}
        atomic_store.write_json(_PATH, data, lock=False)
