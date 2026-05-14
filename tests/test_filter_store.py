"""Tests for filter_store, focusing on purge_past_periods and auto-purge integration."""

import datetime
import json

import pytest

import organist_bot.filter_store as fs


@pytest.fixture(autouse=True)
def use_tmp_dir(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)


def _write_config(data: dict) -> None:
    path = fs._PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) + "\n")


def _read_config() -> dict:
    return json.loads(fs._PATH.read_text())


class TestPurgePastPeriods:
    def test_removes_past_single_day(self):
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        _write_config(
            {
                "unavailable_periods": [yesterday],
                "blacklist_emails": [],
                "available_only_periods": [],
            }
        )
        removed = fs.purge_past_periods()
        assert removed == 1
        assert _read_config()["unavailable_periods"] == []

    def test_keeps_today(self):
        today = datetime.date.today().isoformat()
        _write_config(
            {"unavailable_periods": [today], "blacklist_emails": [], "available_only_periods": []}
        )
        removed = fs.purge_past_periods()
        assert removed == 0
        assert today in _read_config()["unavailable_periods"]

    def test_keeps_future_single_day(self):
        future = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
        _write_config(
            {"unavailable_periods": [future], "blacklist_emails": [], "available_only_periods": []}
        )
        removed = fs.purge_past_periods()
        assert removed == 0

    def test_removes_past_range_by_end_date(self):
        start = (datetime.date.today() - datetime.timedelta(days=10)).isoformat()
        end = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        token = f"{start}:{end}"
        _write_config(
            {"unavailable_periods": [token], "blacklist_emails": [], "available_only_periods": []}
        )
        removed = fs.purge_past_periods()
        assert removed == 1

    def test_keeps_range_ending_today(self):
        start = (datetime.date.today() - datetime.timedelta(days=5)).isoformat()
        end = datetime.date.today().isoformat()
        token = f"{start}:{end}"
        _write_config(
            {"unavailable_periods": [token], "blacklist_emails": [], "available_only_periods": []}
        )
        removed = fs.purge_past_periods()
        assert removed == 0

    def test_removes_past_month(self):
        today = datetime.date.today()
        if today.month == 1:
            past_month = f"{today.year - 1}-12"
        else:
            past_month = f"{today.year}-{today.month - 1:02d}"
        _write_config(
            {
                "unavailable_periods": [past_month],
                "blacklist_emails": [],
                "available_only_periods": [],
            }
        )
        removed = fs.purge_past_periods()
        assert removed == 1

    def test_leaves_unparseable_tokens(self):
        _write_config(
            {
                "unavailable_periods": ["not-a-date"],
                "blacklist_emails": [],
                "available_only_periods": [],
            }
        )
        removed = fs.purge_past_periods()
        assert removed == 0
        assert "not-a-date" in _read_config()["unavailable_periods"]

    def test_does_not_touch_other_keys(self):
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        future = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        _write_config(
            {
                "unavailable_periods": [yesterday],
                "blacklist_emails": ["a@b.com"],
                "available_only_periods": [future],
            }
        )
        fs.purge_past_periods()
        data = _read_config()
        assert data["blacklist_emails"] == ["a@b.com"]
        assert future in data["available_only_periods"]

    def test_returns_zero_when_no_file(self):
        removed = fs.purge_past_periods()
        assert removed == 0

    def test_mixed_keeps_future_removes_past(self):
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        future = (datetime.date.today() + datetime.timedelta(days=10)).isoformat()
        _write_config(
            {
                "unavailable_periods": [yesterday, future],
                "blacklist_emails": [],
                "available_only_periods": [],
            }
        )
        removed = fs.purge_past_periods()
        assert removed == 1
        data = _read_config()
        assert future in data["unavailable_periods"]
        assert yesterday not in data["unavailable_periods"]


class TestAutoPurgeOnUnavailableOperations:
    def test_unavailable_periods_getter_purges_stale(self):
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        future = (datetime.date.today() + datetime.timedelta(days=5)).isoformat()
        _write_config(
            {
                "unavailable_periods": [yesterday, future],
                "blacklist_emails": [],
                "available_only_periods": [],
            }
        )
        result = fs.unavailable_periods()
        assert yesterday not in result
        assert future in result

    def test_add_period_unavailable_purges_first(self):
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        future = (datetime.date.today() + datetime.timedelta(days=5)).isoformat()
        _write_config(
            {
                "unavailable_periods": [yesterday],
                "blacklist_emails": [],
                "available_only_periods": [],
            }
        )
        fs.add_period("unavailable_periods", future)
        data = _read_config()
        assert yesterday not in data["unavailable_periods"]
        assert future in data["unavailable_periods"]

    def test_remove_period_unavailable_purges_first(self):
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        future = (datetime.date.today() + datetime.timedelta(days=5)).isoformat()
        _write_config(
            {
                "unavailable_periods": [yesterday, future],
                "blacklist_emails": [],
                "available_only_periods": [],
            }
        )
        fs.remove_period("unavailable_periods", future)
        data = _read_config()
        assert yesterday not in data["unavailable_periods"]
        assert future not in data["unavailable_periods"]

    def test_add_period_blacklist_does_not_purge_unavailable(self):
        """Only unavailable_periods operations trigger purge — not blacklist operations."""
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        _write_config(
            {
                "unavailable_periods": [yesterday],
                "blacklist_emails": [],
                "available_only_periods": [],
            }
        )
        fs.add_blacklist_email("x@y.com")
        data = _read_config()
        assert yesterday in data["unavailable_periods"]
