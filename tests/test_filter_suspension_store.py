"""Tests for filter_suspension_store: open-ended period parsing, CRUD, purge, snapshot."""

import datetime
import json

import pytest

import organist_bot.filter_suspension_store as fss


@pytest.fixture(autouse=True)
def use_tmp_dir(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)


def _write(data: dict) -> None:
    path = fss._PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) + "\n")


def _read() -> dict:
    return json.loads(fss._PATH.read_text())


class TestParsePeriodToken:
    def test_single_day(self):
        assert fss._parse_period_token("2026-12-25") == (
            datetime.date(2026, 12, 25),
            datetime.date(2026, 12, 25),
        )

    def test_closed_range(self):
        assert fss._parse_period_token("2026-12-15:2027-01-05") == (
            datetime.date(2026, 12, 15),
            datetime.date(2027, 1, 5),
        )

    def test_whole_month(self):
        assert fss._parse_period_token("2026-04") == (
            datetime.date(2026, 4, 1),
            datetime.date(2026, 4, 30),
        )

    def test_open_ended_from(self):
        start, end = fss._parse_period_token("2026-08-01:")
        assert start == datetime.date(2026, 8, 1)
        assert end == datetime.date.max

    def test_open_ended_until(self):
        start, end = fss._parse_period_token(":2026-08-01")
        assert start == datetime.date.min
        assert end == datetime.date(2026, 8, 1)

    def test_unparseable_returns_none(self):
        assert fss._parse_period_token("not-a-date") is None

    def test_empty_string_returns_none(self):
        assert fss._parse_period_token("") is None

    def test_bare_colon_returns_none(self):
        assert fss._parse_period_token(":") is None


class TestAddSuspension:
    def test_adds_new_entry(self):
        added = fss.add_suspension("postcode", "2026-12")
        assert added is True
        assert _read()["suspensions"] == [{"filter": "postcode", "period": "2026-12"}]

    def test_duplicate_returns_false(self):
        fss.add_suspension("postcode", "2026-12")
        added = fss.add_suspension("postcode", "2026-12")
        assert added is False
        assert len(_read()["suspensions"]) == 1

    def test_same_period_different_filter_is_not_duplicate(self):
        fss.add_suspension("postcode", "2026-12")
        added = fss.add_suspension("fee", "2026-12")
        assert added is True
        assert len(_read()["suspensions"]) == 2

    def test_invalid_filter_name_raises(self):
        with pytest.raises(ValueError):
            fss.add_suspension("seen", "2026-12")

    def test_unparseable_period_raises(self):
        with pytest.raises(ValueError):
            fss.add_suspension("postcode", "not-a-date")

    def test_all_is_a_valid_filter_name(self):
        assert fss.add_suspension("all", "2026-12") is True


class TestRemoveSuspension:
    def test_removes_exact_match(self):
        fss.add_suspension("postcode", "2026-12")
        removed = fss.remove_suspension("postcode", "2026-12")
        assert removed is True
        assert _read()["suspensions"] == []

    def test_no_match_returns_false(self):
        fss.add_suspension("postcode", "2026-12")
        removed = fss.remove_suspension("fee", "2026-12")
        assert removed is False
        assert len(_read()["suspensions"]) == 1


class TestPurgePastSuspensions:
    def test_removes_expired_closed_range(self):
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        _write({"suspensions": [{"filter": "fee", "period": f"2020-01-01:{yesterday}"}]})
        removed = fss.purge_past_suspensions()
        assert removed == 1
        assert _read()["suspensions"] == []

    def test_keeps_range_ending_today(self):
        today = datetime.date.today().isoformat()
        _write({"suspensions": [{"filter": "fee", "period": f"2020-01-01:{today}"}]})
        removed = fss.purge_past_suspensions()
        assert removed == 0

    def test_never_purges_open_ended_from(self):
        _write({"suspensions": [{"filter": "fee", "period": "2020-01-01:"}]})
        removed = fss.purge_past_suspensions()
        assert removed == 0
        assert len(_read()["suspensions"]) == 1

    def test_purges_expired_open_ended_until(self):
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        _write({"suspensions": [{"filter": "fee", "period": f":{yesterday}"}]})
        removed = fss.purge_past_suspensions()
        assert removed == 1

    def test_leaves_unparseable_entries(self):
        _write({"suspensions": [{"filter": "fee", "period": "garbage"}]})
        removed = fss.purge_past_suspensions()
        assert removed == 0
        assert len(_read()["suspensions"]) == 1

    def test_returns_zero_when_no_file(self):
        assert fss.purge_past_suspensions() == 0


class TestLoadActive:
    def test_parses_all_entries(self):
        fss.add_suspension("postcode", "2026-12")
        fss.add_suspension("fee", "2026-08-01:")
        snapshot = fss.load_active()
        assert ("postcode", datetime.date(2026, 12, 1), datetime.date(2026, 12, 31)) in snapshot
        assert ("fee", datetime.date(2026, 8, 1), datetime.date.max) in snapshot

    def test_skips_unparseable_entries(self):
        _write({"suspensions": [{"filter": "fee", "period": "garbage"}]})
        assert fss.load_active() == []

    def test_empty_store_returns_empty_list(self):
        assert fss.load_active() == []


class TestIsSuspended:
    def test_matches_named_filter_within_range(self):
        snapshot = [("postcode", datetime.date(2026, 12, 1), datetime.date(2026, 12, 31))]
        assert fss.is_suspended(snapshot, "postcode", datetime.date(2026, 12, 15)) is True

    def test_does_not_match_outside_range(self):
        snapshot = [("postcode", datetime.date(2026, 12, 1), datetime.date(2026, 12, 31))]
        assert fss.is_suspended(snapshot, "postcode", datetime.date(2027, 1, 1)) is False

    def test_does_not_match_different_filter(self):
        snapshot = [("postcode", datetime.date(2026, 12, 1), datetime.date(2026, 12, 31))]
        assert fss.is_suspended(snapshot, "fee", datetime.date(2026, 12, 15)) is False

    def test_all_matches_any_filter_name(self):
        snapshot = [("all", datetime.date(2026, 12, 1), datetime.date(2026, 12, 31))]
        assert fss.is_suspended(snapshot, "fee", datetime.date(2026, 12, 15)) is True
        assert fss.is_suspended(snapshot, "postcode", datetime.date(2026, 12, 15)) is True

    def test_empty_snapshot_matches_nothing(self):
        assert fss.is_suspended([], "fee", datetime.date(2026, 12, 15)) is False
