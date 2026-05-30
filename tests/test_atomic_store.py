import fcntl
import os
import time
from pathlib import Path

import pytest

from organist_bot import atomic_store


def test_file_lock_can_be_reacquired_after_release(tmp_path: Path):
    p = tmp_path / "x.json"
    with atomic_store.file_lock(p):
        pass
    with atomic_store.file_lock(p):
        assert (tmp_path / "x.json.lock").exists()


def test_file_lock_times_out_and_proceeds_unlocked(tmp_path, monkeypatch):
    monkeypatch.setattr(atomic_store, "_LOCK_TIMEOUT_S", 0.2)
    p = tmp_path / "x.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    lock_path = p.with_name(p.name + ".lock")
    holder = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)  # another holder keeps the lock
    try:
        start = time.monotonic()
        with atomic_store.file_lock(p):  # must NOT block forever, must NOT raise
            atomic_store.write_json(p, {"ok": 1}, lock=False)
        assert time.monotonic() - start >= 0.2  # it waited out the timeout
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)
    assert atomic_store.read_json(p, {}) == {"ok": 1}


def test_write_json_roundtrip(tmp_path: Path):
    p = tmp_path / "d.json"
    atomic_store.write_json(p, {"a": 1})
    assert atomic_store.read_json(p, {}) == {"a": 1}


def test_failed_replace_leaves_original_intact(tmp_path: Path, monkeypatch):
    p = tmp_path / "d.json"
    atomic_store.write_json(p, {"ok": True})
    monkeypatch.setattr(
        atomic_store.os,
        "replace",
        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")),
    )
    with pytest.raises(OSError):
        atomic_store.write_json(p, {"ok": False})
    assert atomic_store.read_json(p, {}) == {"ok": True}
    assert list(p.parent.glob("tmp*")) == []


def test_corrupt_file_returns_default_and_alerts(tmp_path: Path, monkeypatch):
    p = tmp_path / "d.json"
    p.write_text("{not valid json")
    calls = []
    monkeypatch.setattr(atomic_store.alert, "send_alert", lambda m: calls.append(m))
    assert atomic_store.read_json(p, {"default": True}) == {"default": True}
    assert len(calls) == 1
