"""Tests for organist_bot.llm_usage_store."""

from __future__ import annotations

import datetime

import pytest

import organist_bot.llm_usage_store as store


@pytest.fixture(autouse=True)
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_PATH", tmp_path / "llm_usage.json")


class TestRecordCall:
    def test_record_call_appends_a_record(self):
        store.record_call("anthropic", "anthropic/claude-sonnet-4-6", 100, 50)
        records = store._read()
        assert len(records) == 1
        r = records[0]
        assert r["provider"] == "anthropic"
        assert r["model"] == "anthropic/claude-sonnet-4-6"
        assert r["prompt_tokens"] == 100
        assert r["completion_tokens"] == 50
        assert r["total_tokens"] == 150
        assert "timestamp" in r

    def test_multiple_calls_accumulate(self):
        store.record_call("anthropic", "anthropic/claude-sonnet-4-6", 100, 50)
        store.record_call("openai", "openai/gpt-5.6-luna", 10, 5)
        assert len(store._read()) == 2


class TestSummary:
    def test_summary_with_no_records_is_empty(self):
        assert store.summary() == {}

    def test_summary_groups_by_provider(self):
        store.record_call("anthropic", "anthropic/claude-sonnet-4-6", 100, 50)
        store.record_call("anthropic", "anthropic/claude-opus-4-6", 10, 5)
        store.record_call("openai", "openai/gpt-5.6-luna", 1, 1)

        result = store.summary()

        assert result["anthropic"]["call_count"] == 2
        assert result["anthropic"]["prompt_tokens"] == 110
        assert result["anthropic"]["completion_tokens"] == 55
        assert result["anthropic"]["total_tokens"] == 165
        assert result["openai"]["call_count"] == 1
        assert result["openai"]["total_tokens"] == 2

    def test_summary_since_excludes_older_records(self):
        old = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=1)
        store.record_call("anthropic", "anthropic/claude-sonnet-4-6", 100, 50)
        records = store._read()
        records[0]["timestamp"] = old.strftime("%Y-%m-%dT%H:%M:%SZ")
        store.atomic_store.write_json(store._PATH, records, lock=False)

        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=1)
        assert store.summary(since=cutoff) == {}
        assert store.summary()["anthropic"]["call_count"] == 1

    def test_summary_since_includes_records_at_or_after_cutoff(self):
        store.record_call("anthropic", "anthropic/claude-sonnet-4-6", 100, 50)
        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=1)
        assert store.summary(since=cutoff)["anthropic"]["call_count"] == 1

    def test_summary_since_skips_record_with_bad_timestamp(self):
        store.record_call("anthropic", "anthropic/claude-sonnet-4-6", 100, 50)
        records = store._read()
        records[0]["timestamp"] = "not-a-timestamp"
        store.atomic_store.write_json(store._PATH, records, lock=False)

        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=1)
        assert store.summary(since=cutoff) == {}
        # Unrestricted summary still counts it, keyed by provider.
        assert store.summary()["anthropic"]["call_count"] == 1
