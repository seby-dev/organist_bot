"""Tests for organist_bot/integrations/agent_state.py — disk-backed per-chat context."""

from organist_bot.integrations import agent_state


def _redirect(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_state, "_PATH", tmp_path / "agent_state.json")


def test_save_and_load_roundtrip(tmp_path, monkeypatch):
    _redirect(monkeypatch, tmp_path)
    agent_state.save_chat(7, {"last_invoice": {"invoice_number": "INV-1"}})
    loaded = agent_state.load_chat(7)
    assert loaded["last_invoice"] == {"invoice_number": "INV-1"}
    assert loaded["last_gig_listing"] is None
    assert loaded["last_application_listing"] is None


def test_load_missing_chat_returns_none_fields(tmp_path, monkeypatch):
    _redirect(monkeypatch, tmp_path)
    assert agent_state.load_chat(999) == {
        "last_invoice": None,
        "last_gig_listing": None,
        "last_application_listing": None,
    }


def test_only_known_keys_are_persisted(tmp_path, monkeypatch):
    """history (and any other field) must NOT be persisted — only the _KEYS."""
    _redirect(monkeypatch, tmp_path)
    agent_state.save_chat(1, {"last_gig_listing": [{"id": "x"}], "history": [1, 2, 3]})
    loaded = agent_state.load_chat(1)
    assert loaded["last_gig_listing"] == [{"id": "x"}]
    assert "history" not in loaded


def test_chats_are_isolated(tmp_path, monkeypatch):
    _redirect(monkeypatch, tmp_path)
    agent_state.save_chat(1, {"last_invoice": {"n": "A"}})
    agent_state.save_chat(2, {"last_invoice": {"n": "B"}})
    assert agent_state.load_chat(1)["last_invoice"] == {"n": "A"}
    assert agent_state.load_chat(2)["last_invoice"] == {"n": "B"}


def test_save_overwrites_previous(tmp_path, monkeypatch):
    _redirect(monkeypatch, tmp_path)
    agent_state.save_chat(5, {"last_invoice": {"n": "old"}})
    agent_state.save_chat(5, {"last_invoice": {"n": "new"}})
    assert agent_state.load_chat(5)["last_invoice"] == {"n": "new"}
