from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_PATH = Path("data/runtime_config.json")


def _read() -> dict[str, int]:
    if not _PATH.exists():
        return {}
    try:
        return dict(json.loads(_PATH.read_text()))
    except Exception:
        logger.exception("runtime_config_store: failed to read %s — using empty config", _PATH)
        return {}


def _write(data: dict[str, int]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(data, indent=2) + "\n")


class RuntimeConfigStore:
    """File-backed store for runtime pipeline config overrides."""

    def get(self, key: str, default: int) -> int:
        """Return the stored override for key, or default if not set."""
        return _read().get(key, default)

    def set(self, key: str, value: int) -> None:
        """Write an override value for key."""
        data = _read()
        data[key] = value
        _write(data)

    def reset(self, key: str) -> bool:
        """Remove the override for key. Returns True if the key existed."""
        data = _read()
        if key not in data:
            return False
        del data[key]
        _write(data)
        return True

    def all(self) -> dict[str, int]:
        """Return all current overrides."""
        return _read()


runtime_config = RuntimeConfigStore()
