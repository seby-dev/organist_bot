# tests/test_runtime_config_store.py
"""Tests for RuntimeConfigStore."""

import json


class TestRuntimeConfigStore:
    def test_get_returns_default_when_key_absent(self, tmp_path, monkeypatch):
        from organist_bot.runtime_config_store import RuntimeConfigStore

        monkeypatch.chdir(tmp_path)
        store = RuntimeConfigStore()
        assert store.get("min_fee", 100) == 100

    def test_get_returns_override_when_set(self, tmp_path, monkeypatch):
        from organist_bot.runtime_config_store import RuntimeConfigStore

        monkeypatch.chdir(tmp_path)
        store = RuntimeConfigStore()
        store.set("min_fee", 150)
        assert store.get("min_fee", 100) == 150

    def test_set_persists_to_file(self, tmp_path, monkeypatch):
        from organist_bot.runtime_config_store import RuntimeConfigStore

        monkeypatch.chdir(tmp_path)
        store = RuntimeConfigStore()
        store.set("poll_minutes", 5)
        raw = json.loads((tmp_path / "data" / "runtime_config.json").read_text())
        assert raw["poll_minutes"] == 5

    def test_reset_removes_override(self, tmp_path, monkeypatch):
        from organist_bot.runtime_config_store import RuntimeConfigStore

        monkeypatch.chdir(tmp_path)
        store = RuntimeConfigStore()
        store.set("max_travel_minutes", 60)
        assert store.reset("max_travel_minutes") is True
        assert store.get("max_travel_minutes", 45) == 45

    def test_reset_returns_false_when_key_absent(self, tmp_path, monkeypatch):
        from organist_bot.runtime_config_store import RuntimeConfigStore

        monkeypatch.chdir(tmp_path)
        store = RuntimeConfigStore()
        assert store.reset("min_fee") is False

    def test_all_returns_current_overrides(self, tmp_path, monkeypatch):
        from organist_bot.runtime_config_store import RuntimeConfigStore

        monkeypatch.chdir(tmp_path)
        store = RuntimeConfigStore()
        store.set("min_fee", 120)
        store.set("poll_minutes", 3)
        result = store.all()
        assert result == {"min_fee": 120, "poll_minutes": 3}

    def test_missing_file_treated_as_empty(self, tmp_path, monkeypatch):
        from organist_bot.runtime_config_store import RuntimeConfigStore

        monkeypatch.chdir(tmp_path)
        store = RuntimeConfigStore()
        assert store.all() == {}

    def test_malformed_json_treated_as_empty(self, tmp_path, monkeypatch):
        from organist_bot.runtime_config_store import RuntimeConfigStore

        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "runtime_config.json").write_text("not json")
        store = RuntimeConfigStore()
        assert store.get("min_fee", 100) == 100

    def test_multiple_keys_are_independent(self, tmp_path, monkeypatch):
        from organist_bot.runtime_config_store import RuntimeConfigStore

        monkeypatch.chdir(tmp_path)
        store = RuntimeConfigStore()
        store.set("min_fee", 200)
        store.set("max_travel_minutes", 60)
        store.reset("min_fee")
        assert store.get("min_fee", 100) == 100
        assert store.get("max_travel_minutes", 45) == 60
