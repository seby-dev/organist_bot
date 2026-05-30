"""organist_bot/atomic_store.py
Atomic, lockable JSON/text persistence shared by the file-backed stores.

Generalizes the tempfile + os.replace pattern from application_store and adds
cross-process advisory locking (fcntl.flock) plus loud failure on corruption.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import organist_bot.alert as alert

logger = logging.getLogger(__name__)

_LOCK_TIMEOUT_S = 5.0


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Advisory exclusive lock on '<path>.lock'. Best-effort: on timeout, log
    and proceed unlocked (availability over strict consistency for a 2-min poll)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        deadline = time.monotonic() + _LOCK_TIMEOUT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    logger.warning("file_lock: timeout on %s — proceeding unlocked", lock_path)
                    break
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _atomic_replace(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(path))
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def write_json(path: Path, data: Any, *, lock: bool = True) -> None:
    payload = json.dumps(data, indent=2) + "\n"
    if lock:
        with file_lock(path):
            _atomic_replace(path, payload)
    else:
        _atomic_replace(path, payload)


def write_text_atomic(path: Path, text: str, *, lock: bool = True) -> None:
    if lock:
        with file_lock(path):
            _atomic_replace(path, text)
    else:
        _atomic_replace(path, text)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.exception("atomic_store: corrupt/unreadable %s", path)
        alert.send_alert(f"⚠️ Corrupt data file {path.name} — using default ({exc}).")
        return default
