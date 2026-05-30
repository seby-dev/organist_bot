from __future__ import annotations

import logging
from pathlib import Path

from organist_bot import atomic_store

logger = logging.getLogger(__name__)

_PATH = Path("data/runtime_config.json")


def _read() -> dict[str, int]:
    return dict(atomic_store.read_json(_PATH, {}))


def _write(data: dict[str, int]) -> None:
    atomic_store.write_json(_PATH, data)


class RuntimeConfigStore:
    """File-backed store for runtime pipeline config overrides."""

    def get(self, key: str, default: int) -> int:
        """Return the stored override for key, or default if not set."""
        return _read().get(key, default)

    def set(self, key: str, value: int) -> None:
        """Write an override value for key."""
        with atomic_store.file_lock(_PATH):
            data = dict(atomic_store.read_json(_PATH, {}))
            data[key] = value
            atomic_store.write_json(_PATH, data, lock=False)

    def reset(self, key: str) -> bool:
        """Remove the override for key. Returns True if the key existed."""
        with atomic_store.file_lock(_PATH):
            data = dict(atomic_store.read_json(_PATH, {}))
            if key not in data:
                return False
            del data[key]
            atomic_store.write_json(_PATH, data, lock=False)
        return True

    def all(self) -> dict[str, int]:
        """Return all current overrides."""
        return _read()


runtime_config = RuntimeConfigStore()
