from pathlib import Path

import pytest

from organist_bot import atomic_store


def test_file_lock_is_reentrant_across_sequential_calls(tmp_path: Path):
    p = tmp_path / "x.json"
    with atomic_store.file_lock(p):
        pass
    with atomic_store.file_lock(p):
        assert (tmp_path / "x.json.lock").exists()


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
