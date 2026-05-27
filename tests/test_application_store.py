"""Tests for organist_bot.application_store."""

import datetime
import json

import pytest

import organist_bot.application_store as store
from organist_bot.models import Gig


def _make_gig(**overrides) -> Gig:
    defaults = dict(
        header="Sunday Service",
        organisation="St Paul's",
        locality="London",
        date="Sunday, 15 June 2026",
        time="10:00 AM",
        fee="£80",
        link="https://organistsonline.org/gig/123",
        email="contact@stpauls.com",
    )
    defaults.update(overrides)
    return Gig(**defaults)


@pytest.fixture(autouse=True)
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_PATH", tmp_path / "applications.json")


# ── record_application ────────────────────────────────────────────────────────


class TestRecordApplication:
    def test_record_application_writes_applied_record(self):
        gig = _make_gig()
        result = store.record_application(gig)
        assert result is True
        records = json.loads(store._PATH.read_text())
        assert len(records) == 1
        assert records[0]["url"] == "https://organistsonline.org/gig/123"
        assert records[0]["status"] == "applied"
        assert records[0]["header"] == "Sunday Service"
        assert records[0]["organisation"] == "St Paul's"
        assert records[0]["fee"] == "£80"
        assert records[0]["email"] == "contact@stpauls.com"

    def test_record_application_idempotent(self):
        gig = _make_gig()
        store.record_application(gig)
        result = store.record_application(gig)
        assert result is False
        records = json.loads(store._PATH.read_text())
        assert len(records) == 1


# ── update_status ─────────────────────────────────────────────────────────────


class TestUpdateStatus:
    def test_update_status_changes_status_and_updated_at(self):
        gig = _make_gig()
        store.record_application(gig)
        before = json.loads(store._PATH.read_text())[0]["updated_at"]
        result = store.update_status("https://organistsonline.org/gig/123", "declined")
        assert result is True
        records = json.loads(store._PATH.read_text())
        assert records[0]["status"] == "declined"
        assert records[0]["updated_at"] >= before

    def test_update_status_returns_false_when_not_found(self):
        result = store.update_status("https://unknown.com/gig/999", "declined")
        assert result is False
        assert not store._PATH.exists()


# ── upsert_accepted ───────────────────────────────────────────────────────────


class TestUpsertAccepted:
    def test_upsert_accepted_updates_existing_record(self):
        gig = _make_gig()
        store.record_application(gig)
        store.upsert_accepted(
            url="https://organistsonline.org/gig/123",
            header="Sunday Service",
            organisation="St Paul's",
            date="Sunday, 15 June 2026",
            fee="£80",
        )
        records = json.loads(store._PATH.read_text())
        assert len(records) == 1
        assert records[0]["status"] == "accepted"

    def test_upsert_accepted_creates_new_when_no_match(self):
        store.upsert_accepted(
            url="https://organistsonline.org/gig/456",
            header="Evensong",
            organisation="All Saints",
            date="2026-06-22",
            fee="£100",
        )
        records = json.loads(store._PATH.read_text())
        assert len(records) == 1
        assert records[0]["status"] == "accepted"
        assert records[0]["url"] == "https://organistsonline.org/gig/456"

    def test_upsert_accepted_creates_new_when_url_none(self):
        store.upsert_accepted(
            url=None,
            header="Manual Gig",
            organisation="St John's",
            date="2026-07-01",
            fee="£90",
        )
        records = json.loads(store._PATH.read_text())
        assert len(records) == 1
        assert records[0]["status"] == "accepted"
        assert records[0]["url"] == ""


# ── expire_past_applied ───────────────────────────────────────────────────────


class TestExpirePastApplied:
    def _add_applied(self, url: str, date: str) -> None:
        store.record_application(_make_gig(link=url, date=date))

    def test_expire_past_applied_marks_old_records(self):
        # 2020-01-01 is unambiguously in the past
        self._add_applied("https://organistsonline.org/gig/1", "Sunday, 1 January 2020")
        changed = store.expire_past_applied()
        assert changed == 1
        records = json.loads(store._PATH.read_text())
        assert records[0]["status"] == "no_response"

    def test_expire_past_applied_leaves_future_records(self):
        # 2099-12-31 is unambiguously in the future
        self._add_applied("https://organistsonline.org/gig/1", "Sunday, 31 December 2099")
        changed = store.expire_past_applied()
        assert changed == 0
        records = json.loads(store._PATH.read_text())
        assert records[0]["status"] == "applied"

    def test_expire_past_applied_leaves_non_applied_records(self):
        self._add_applied("https://organistsonline.org/gig/1", "Sunday, 1 January 2020")
        store.update_status("https://organistsonline.org/gig/1", "accepted")
        changed = store.expire_past_applied()
        assert changed == 0
        records = json.loads(store._PATH.read_text())
        assert records[0]["status"] == "accepted"

    def test_expire_returns_count_of_changed_records(self):
        self._add_applied("https://organistsonline.org/gig/1", "Sunday, 1 January 2020")
        self._add_applied("https://organistsonline.org/gig/2", "Sunday, 8 January 2020")
        self._add_applied(
            "https://organistsonline.org/gig/3", "Sunday, 31 December 2099"
        )  # future — unchanged
        changed = store.expire_past_applied()
        assert changed == 2


# ── list_applications ─────────────────────────────────────────────────────────


class TestListApplications:
    def test_list_applications_newest_first(self):
        store.record_application(_make_gig(link="https://organistsonline.org/gig/old"))
        store.record_application(_make_gig(link="https://organistsonline.org/gig/new"))
        # Back-date the first record to make it older
        data = json.loads(store._PATH.read_text())
        data[0]["applied_at"] = "2026-01-01T10:00:00Z"
        data[1]["applied_at"] = "2026-06-01T10:00:00Z"
        store._PATH.write_text(json.dumps(data, indent=2) + "\n")
        result = store.list_applications(days=365)
        assert len(result) == 2
        assert result[0]["url"] == "https://organistsonline.org/gig/new"
        assert result[1]["url"] == "https://organistsonline.org/gig/old"

    def test_list_applications_filters_by_days(self):
        gig = _make_gig()
        store.record_application(gig)
        # Back-date applied_at to 60 days ago so it falls outside a 30-day window
        data = json.loads(store._PATH.read_text())
        old_ts = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=60)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        data[0]["applied_at"] = old_ts
        store._PATH.write_text(json.dumps(data, indent=2) + "\n")

        assert store.list_applications(days=30) == []
        assert len(store.list_applications(days=61)) == 1


# ── get_income ────────────────────────────────────────────────────────────────


class TestGetIncome:
    def _make_accepted(self, date, fee, url="http://example.com/1"):
        return {
            "url": url,
            "header": "Test",
            "organisation": "St John",
            "date": date,
            "fee": fee,
            "email": "",
            "status": "accepted",
            "applied_at": "2026-06-01T10:00:00Z",
            "updated_at": "2026-06-01T10:00:00Z",
        }

    def test_sums_accepted_fees_in_range(self, tmp_path, monkeypatch):
        import organist_bot.application_store as store

        monkeypatch.setattr(store, "_PATH", tmp_path / "applications.json")
        records = [
            self._make_accepted("2026-06-10", "£140.00", "http://a.com/1"),
            self._make_accepted("2026-06-15", "£150.00", "http://a.com/2"),
        ]
        (tmp_path / "applications.json").write_text(json.dumps(records))
        result = store.get_income("2026-06-01", "2026-06-30")
        assert result["total"] == pytest.approx(290.0)
        assert result["count"] == 2
        assert result["no_fee_count"] == 0

    def test_excludes_non_accepted_statuses(self, tmp_path, monkeypatch):
        import organist_bot.application_store as store

        monkeypatch.setattr(store, "_PATH", tmp_path / "applications.json")
        records = [
            {
                **self._make_accepted("2026-06-10", "£100.00"),
                "status": "applied",
                "url": "http://a.com/1",
            },
            {
                **self._make_accepted("2026-06-10", "£100.00"),
                "status": "rejected",
                "url": "http://a.com/2",
            },
            {
                **self._make_accepted("2026-06-10", "£100.00"),
                "status": "declined",
                "url": "http://a.com/3",
            },
        ]
        (tmp_path / "applications.json").write_text(json.dumps(records))
        result = store.get_income("2026-06-01", "2026-06-30")
        assert result["total"] == 0.0
        assert result["count"] == 0

    def test_excludes_records_outside_date_range(self, tmp_path, monkeypatch):
        import organist_bot.application_store as store

        monkeypatch.setattr(store, "_PATH", tmp_path / "applications.json")
        records = [
            self._make_accepted("2026-05-31", "£100.00", "http://a.com/1"),
            self._make_accepted("2026-06-15", "£140.00", "http://a.com/2"),
            self._make_accepted("2026-07-01", "£100.00", "http://a.com/3"),
        ]
        (tmp_path / "applications.json").write_text(json.dumps(records))
        result = store.get_income("2026-06-01", "2026-06-30")
        assert result["count"] == 1
        assert result["total"] == pytest.approx(140.0)

    def test_empty_fee_counted_as_no_fee(self, tmp_path, monkeypatch):
        import organist_bot.application_store as store

        monkeypatch.setattr(store, "_PATH", tmp_path / "applications.json")
        records = [self._make_accepted("2026-06-10", "")]
        (tmp_path / "applications.json").write_text(json.dumps(records))
        result = store.get_income("2026-06-01", "2026-06-30")
        assert result["count"] == 1
        assert result["no_fee_count"] == 1
        assert result["total"] == 0.0

    def test_parses_pound_and_dollar(self, tmp_path, monkeypatch):
        import organist_bot.application_store as store

        monkeypatch.setattr(store, "_PATH", tmp_path / "applications.json")
        records = [
            self._make_accepted("2026-06-10", "£140.00", "http://a.com/1"),
            self._make_accepted("2026-06-15", "$500.00", "http://a.com/2"),
        ]
        (tmp_path / "applications.json").write_text(json.dumps(records))
        result = store.get_income("2026-06-01", "2026-06-30")
        assert result["total"] == pytest.approx(640.0)

    def test_fails_open_on_corrupt_json(self, tmp_path, monkeypatch):
        import organist_bot.application_store as store

        monkeypatch.setattr(store, "_PATH", tmp_path / "applications.json")
        (tmp_path / "applications.json").write_text("not json")
        result = store.get_income("2026-06-01", "2026-06-30")
        assert result == {"total": 0.0, "count": 0, "no_fee_count": 0, "records": []}


# ── update_reply_message_id ───────────────────────────────────────────────────


class TestUpdateReplyMessageId:
    def _make_record(self, url, email="church@example.com"):
        return {
            "url": url,
            "header": "Test",
            "organisation": "St John",
            "date": "2026-06-10",
            "fee": "£100",
            "email": email,
            "status": "applied",
            "applied_at": "2026-06-01T10:00:00Z",
            "updated_at": "2026-06-01T10:00:00Z",
        }

    def test_sets_reply_message_id_on_existing_record(self, tmp_path, monkeypatch):
        import organist_bot.application_store as store

        monkeypatch.setattr(store, "_PATH", tmp_path / "applications.json")
        (tmp_path / "applications.json").write_text(
            json.dumps([self._make_record("http://a.com/1")])
        )
        result = store.update_reply_message_id("http://a.com/1", "msg123")
        assert result is True
        records = json.loads((tmp_path / "applications.json").read_text())
        assert records[0]["reply_message_id"] == "msg123"

    def test_returns_false_when_url_not_found(self, tmp_path, monkeypatch):
        import organist_bot.application_store as store

        monkeypatch.setattr(store, "_PATH", tmp_path / "applications.json")
        (tmp_path / "applications.json").write_text(json.dumps([]))
        assert store.update_reply_message_id("http://notfound.com", "msg123") is False
