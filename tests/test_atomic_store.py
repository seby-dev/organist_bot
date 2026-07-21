import errno
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


def test_read_json_retries_transient_oserror_then_succeeds(tmp_path: Path, monkeypatch):
    """A cloud-sync-style transient OSError (e.g. a 'dataless' iCloud placeholder
    mid-download) must be retried once, not immediately reported as corrupt."""
    p = tmp_path / "d.json"
    p.write_text('{"a": 1}')
    calls = []
    monkeypatch.setattr(atomic_store.alert, "send_alert", lambda m: calls.append(m))
    monkeypatch.setattr(atomic_store.time, "sleep", lambda s: None)

    real_read_text = Path.read_text
    attempts = {"n": 0}

    def flaky_read_text(self, *args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise OSError(errno.EDEADLK, "Resource deadlock avoided")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read_text)

    assert atomic_store.read_json(p, {}) == {"a": 1}
    assert calls == []  # recovered on retry — no alert needed


def test_read_json_alerts_after_retry_still_failing(tmp_path: Path, monkeypatch):
    p = tmp_path / "d.json"
    p.write_text('{"a": 1}')
    calls = []
    monkeypatch.setattr(atomic_store.alert, "send_alert", lambda m: calls.append(m))
    monkeypatch.setattr(atomic_store.time, "sleep", lambda s: None)

    def always_fails(self, *args, **kwargs):
        raise OSError(errno.EDEADLK, "Resource deadlock avoided")

    monkeypatch.setattr(Path, "read_text", always_fails)

    assert atomic_store.read_json(p, {"default": True}) == {"default": True}
    assert len(calls) == 1
    assert "unreadable" in calls[0].lower()


def test_file_lock_retries_on_deadlock_errno(tmp_path: Path, monkeypatch):
    """EDEADLK is what macOS surfaces for lock contention on a cloud-synced
    (e.g. iCloud Drive) 'dataless' placeholder file in place of the normal
    EAGAIN/EWOULDBLOCK — it must be retried, not raised."""
    p = tmp_path / "x.json"
    real_flock = fcntl.flock
    attempts = {"n": 0}

    def flaky_flock(fd, op):
        if op & fcntl.LOCK_EX and op & fcntl.LOCK_NB:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise OSError(errno.EDEADLK, "Resource deadlock avoided")
        return real_flock(fd, op)

    monkeypatch.setattr(atomic_store.fcntl, "flock", flaky_flock)
    with atomic_store.file_lock(p):
        pass
    assert attempts["n"] >= 2  # retried past the simulated EDEADLK and acquired


def test_file_lock_reraises_non_retryable_oserror(tmp_path: Path, monkeypatch):
    p = tmp_path / "x.json"

    def broken_flock(fd, op):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(atomic_store.fcntl, "flock", broken_flock)
    with pytest.raises(OSError):
        with atomic_store.file_lock(p):
            pass
