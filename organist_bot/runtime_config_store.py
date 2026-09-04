from __future__ import annotations

import logging
from pathlib import Path
from typing import overload

from organist_bot import atomic_store

logger = logging.getLogger(__name__)

_PATH = Path("data/runtime_config.json")

RuntimeValue = int | str


def _read() -> dict[str, RuntimeValue]:
    return dict(atomic_store.read_json(_PATH, {}))


class RuntimeConfigStore:
    """File-backed store for runtime pipeline config overrides."""

    @overload
    def get(self, key: str, default: int) -> int: ...
    @overload
    def get(self, key: str, default: str) -> str: ...

    def get(self, key: str, default: RuntimeValue) -> RuntimeValue:
        """Return the stored override for key, or default if not set."""
        return _read().get(key, default)

    def set(self, key: str, value: RuntimeValue) -> None:
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

    def all(self) -> dict[str, RuntimeValue]:
        """Return all current overrides."""
        return _read()


runtime_config = RuntimeConfigStore()
