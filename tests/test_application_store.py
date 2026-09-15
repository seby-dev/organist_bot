"""Tests for organist_bot.application_store."""

import datetime
import hashlib
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
        assert len(changed) == 1
        records = json.loads(store._PATH.read_text())
        assert records[0]["status"] == "no_response"

    def test_expire_past_applied_leaves_future_records(self):
        # 2099-12-31 is unambiguously in the future
        self._add_applied("https://organistsonline.org/gig/1", "Sunday, 31 December 2099")
        changed = store.expire_past_applied()
        assert changed == []
        records = json.loads(store._PATH.read_text())
        assert records[0]["status"] == "applied"

    def test_expire_past_applied_leaves_non_applied_records(self):
        self._add_applied("https://organistsonline.org/gig/1", "Sunday, 1 January 2020")
        store.update_status("https://organistsonline.org/gig/1", "accepted")
        changed = store.expire_past_applied()
        assert changed == []
        records = json.loads(store._PATH.read_text())
        assert records[0]["status"] == "accepted"

    def test_expire_returns_count_of_changed_records(self):
        self._add_applied("https://organistsonline.org/gig/1", "Sunday, 1 January 2020")
        self._add_applied("https://organistsonline.org/gig/2", "Sunday, 8 January 2020")
        self._add_applied(
            "https://organistsonline.org/gig/3", "Sunday, 31 December 2099"
        )  # future — unchanged
        changed = store.expire_past_applied()
        assert len(changed) == 2


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


# ── was_unclear_alerted / mark_unclear_alerted ───────────────────────────────


class TestUnclearAlertDedup:
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

    def test_not_alerted_before_marking(self, tmp_path, monkeypatch):
        import organist_bot.application_store as store

        monkeypatch.setattr(store, "_PATH", tmp_path / "applications.json")
        (tmp_path / "applications.json").write_text(
            json.dumps([self._make_record("http://a.com/1")])
        )
        assert store.was_unclear_alerted("http://a.com/1", "msg1") is False

    def test_mark_then_was_alerted_round_trip(self, tmp_path, monkeypatch):
        import organist_bot.application_store as store

        monkeypatch.setattr(store, "_PATH", tmp_path / "applications.json")
        (tmp_path / "applications.json").write_text(
            json.dumps([self._make_record("http://a.com/1")])
        )
        assert store.mark_unclear_alerted("http://a.com/1", "msg1") is True
        assert store.was_unclear_alerted("http://a.com/1", "msg1") is True
        # A different message_id on the same record is unaffected.
        assert store.was_unclear_alerted("http://a.com/1", "msg2") is False

    def test_mark_is_idempotent_and_does_not_duplicate_ids(self, tmp_path, monkeypatch):
        import organist_bot.application_store as store

        monkeypatch.setattr(store, "_PATH", tmp_path / "applications.json")
        (tmp_path / "applications.json").write_text(
            json.dumps([self._make_record("http://a.com/1")])
        )
        store.mark_unclear_alerted("http://a.com/1", "msg1")
        store.mark_unclear_alerted("http://a.com/1", "msg1")
        records = json.loads((tmp_path / "applications.json").read_text())
        assert records[0]["alerted_unclear_ids"] == ["msg1"]

    def test_mark_returns_false_when_url_not_found(self, tmp_path, monkeypatch):
        import organist_bot.application_store as store

        monkeypatch.setattr(store, "_PATH", tmp_path / "applications.json")
        (tmp_path / "applications.json").write_text(json.dumps([]))
        assert store.mark_unclear_alerted("http://notfound.com", "msg1") is False

    def test_was_alerted_returns_false_when_url_not_found(self, tmp_path, monkeypatch):
        import organist_bot.application_store as store

        monkeypatch.setattr(store, "_PATH", tmp_path / "applications.json")
        (tmp_path / "applications.json").write_text(json.dumps([]))
        assert store.was_unclear_alerted("http://notfound.com", "msg1") is False


# ── New fields: postcode, time, travel buffer IDs ────────────────────────────


class TestPostcodeStoredOnApplication:
    def test_record_application_stores_postcode(self):
        gig = _make_gig(postcode="CM1 1AA")
        store.record_application(gig)
        records = json.loads(store._PATH.read_text())
        assert records[0]["postcode"] == "CM1 1AA"

    def test_record_application_stores_time(self):
        gig = _make_gig(time="10:30 AM")
        store.record_application(gig)
        records = json.loads(store._PATH.read_text())
        assert records[0]["time"] == "10:30 AM"

    def test_record_application_stores_blank_postcode_when_none(self):
        gig = _make_gig()  # postcode not set → None
        store.record_application(gig)
        records = json.loads(store._PATH.read_text())
        assert records[0]["postcode"] == ""


class TestUpsertAcceptedPostcode:
    def test_upsert_accepted_stores_postcode(self):
        store.upsert_accepted(
            url="http://a.com/1",
            header="Wedding",
            organisation="St Mary's",
            date="2026-07-01",
            fee="£200",
            postcode="SW1A 1AA",
        )
        records = json.loads(store._PATH.read_text())
        assert records[0]["postcode"] == "SW1A 1AA"

    def test_upsert_accepted_postcode_defaults_to_empty(self):
        store.upsert_accepted(
            url="http://a.com/2",
            header="Funeral",
            organisation="St John's",
            date="2026-07-02",
            fee="£100",
        )
        records = json.loads(store._PATH.read_text())
        assert records[0]["postcode"] == ""

    def test_upsert_accepted_updates_postcode_on_existing_record(self):
        # First record the application
        gig = _make_gig()
        store.record_application(gig)
        # Then accept it with a postcode
        store.upsert_accepted(
            url="https://organistsonline.org/gig/123",
            header="Wedding",
            organisation="St Mary's",
            date="2026-07-01",
            fee="£200",
            postcode="CM1 1AA",
        )
        records = json.loads(store._PATH.read_text())
        assert records[0]["postcode"] == "CM1 1AA"

    def test_upsert_accepted_preserves_existing_postcode_when_blank(self):
        # Record application with a postcode
        gig = _make_gig(postcode="CM1 1AA")
        store.record_application(gig)
        # Accept without providing a postcode
        store.upsert_accepted(
            url="https://organistsonline.org/gig/123",
            header="Wedding",
            organisation="St Mary's",
            date="2026-07-01",
            fee="£200",
        )
        records = json.loads(store._PATH.read_text())
        # Original postcode should be preserved (not overwritten with "")
        assert records[0]["postcode"] == "CM1 1AA"


class TestUpdateTravelBufferIds:
    def test_sets_buffer_ids_on_existing_record(self):
        gig = _make_gig()
        store.record_application(gig)
        result = store.update_travel_buffer_ids(
            "https://organistsonline.org/gig/123", "before_id_123", "after_id_456"
        )
        assert result is True
        records = json.loads(store._PATH.read_text())
        assert records[0]["travel_before_event_id"] == "before_id_123"
        assert records[0]["travel_after_event_id"] == "after_id_456"

    def test_returns_false_for_unknown_url(self):
        result = store.update_travel_buffer_ids("http://not-found.com", "b", "a")
        assert result is False


# ── NEG-pending ───────────────────────────────────────────────────────────────


def _neg_gig(link="https://example.com/g/abc"):
    return Gig(
        header="Test Gig",
        organisation="Test Org",
        locality="London",
        date="Sunday, July 12, 2026",
        time="10:00 AM",
        fee="NEG",
        link=link,
        contact="Jane",
        email="jane@example.com",
    )


class TestHeldDrafts:
    def test_record_held_draft_writes_row(self):
        gig = _neg_gig()
        gig_id, created = store.record_held_draft(
            gig,
            status="neg_pending",
            draft_id="draft-abc",
            draft_subject="S",
            hold_reason="fee_negotiation",
            negotiable_fee=120,
        )
        expected = hashlib.sha256(gig.link.encode()).hexdigest()[:12]
        assert gig_id == expected
        assert created is True
        rows = store.list_held()
        assert len(rows) == 1
        r = rows[0]
        assert r["gig_id"] == expected
        assert r["status"] == "neg_pending"
        assert r["draft_id"] == "draft-abc"
        assert r["draft_subject"] == "S"
        assert r["hold_reason"] == "fee_negotiation"
        assert r["negotiable_fee"] == 120
        assert r["contact"] == gig.contact
        assert r["url"] == gig.link
        assert r["created_at"]
        assert r["decided_at"] is None
        assert r["decision"] is None
        assert "draft_body" not in r

    def test_record_held_draft_review_pending_has_no_negotiable_fee(self):
        gig = _neg_gig()
        gig_id, created = store.record_held_draft(
            gig,
            status="review_pending",
            draft_id="draft-xyz",
            draft_subject="S",
            hold_reason="multi_service",
        )
        assert created is True
        r = store.get_by_gig_id(gig_id)
        assert r["status"] == "review_pending"
        assert r["hold_reason"] == "multi_service"
        assert r["negotiable_fee"] is None

    def test_record_held_draft_is_idempotent_for_same_link_returns_created_false(self):
        gig = _neg_gig()
        id1, created1 = store.record_held_draft(
            gig,
            status="neg_pending",
            draft_id="draft-1",
            draft_subject="S",
            hold_reason="fee_negotiation",
            negotiable_fee=120,
        )
        id2, created2 = store.record_held_draft(
            gig,
            status="neg_pending",
            draft_id="draft-2",
            draft_subject="S2",
            hold_reason="fee_negotiation",
            negotiable_fee=130,
        )
        assert id1 == id2
        assert created1 is True
        assert created2 is False
        rows = store.list_held()
        assert len(rows) == 1
        # First write wins — the caller (main.py) is responsible for deleting
        # the now-orphaned second Gmail draft ("draft-2") since created=False.
        assert rows[0]["draft_id"] == "draft-1"

    def test_list_held_returns_only_neg_and_review_pending_rows(self):
        store.record_held_draft(
            _neg_gig("https://e.com/1"),
            status="neg_pending",
            draft_id="d1",
            draft_subject="S",
            hold_reason="fee_negotiation",
            negotiable_fee=120,
        )
        store.record_held_draft(
            _neg_gig("https://e.com/2"),
            status="review_pending",
            draft_id="d2",
            draft_subject="S",
            hold_reason="weekday",
        )
        store.record_application(_neg_gig("https://e.com/3"))  # status=applied
        rows = store.list_held()
        assert {r["url"] for r in rows} == {"https://e.com/1", "https://e.com/2"}

    def test_list_held_filters_by_status(self):
        store.record_held_draft(
            _neg_gig("https://e.com/1"),
            status="neg_pending",
            draft_id="d1",
            draft_subject="S",
            hold_reason="fee_negotiation",
            negotiable_fee=120,
        )
        store.record_held_draft(
            _neg_gig("https://e.com/2"),
            status="review_pending",
            draft_id="d2",
            draft_subject="S",
            hold_reason="weekday",
        )
        assert [r["url"] for r in store.list_held(status="neg_pending")] == ["https://e.com/1"]
        assert [r["url"] for r in store.list_held(status="review_pending")] == ["https://e.com/2"]

    def test_transition_held_to_applied_sets_applied_at(self):
        gig_id, _ = store.record_held_draft(
            _neg_gig(),
            status="neg_pending",
            draft_id="d1",
            draft_subject="S",
            hold_reason="fee_negotiation",
            negotiable_fee=120,
        )
        assert store.transition_held(gig_id, to="applied") is True
        r = store.get_by_gig_id(gig_id)
        assert r["status"] == "applied"
        assert r["decision"] == "applied"
        assert r["decided_at"]
        assert r["applied_at"]

    def test_transition_held_works_for_review_pending_too(self):
        gig_id, _ = store.record_held_draft(
            _neg_gig(),
            status="review_pending",
            draft_id="d1",
            draft_subject="S",
            hold_reason="weekday",
        )
        assert store.transition_held(gig_id, to="rejected") is True
        assert store.get_by_gig_id(gig_id)["status"] == "rejected"

    def test_transition_held_idempotent_second_call_returns_false(self):
        gig_id, _ = store.record_held_draft(
            _neg_gig(),
            status="neg_pending",
            draft_id="d1",
            draft_subject="S",
            hold_reason="fee_negotiation",
            negotiable_fee=120,
        )
        assert store.transition_held(gig_id, to="applied") is True
        assert store.transition_held(gig_id, to="rejected") is False
        assert store.get_by_gig_id(gig_id)["status"] == "applied"

    def test_transition_held_unknown_id_returns_false(self):
        assert store.transition_held("deadbeefcafe", to="applied") is False


class TestExpireHeldDrafts:
    def test_expire_past_neg_pending_flips_to_expired_and_is_returned(self):
        past = (datetime.date.today() - datetime.timedelta(days=5)).strftime("%A, %B %d, %Y")
        gig = _neg_gig()
        gig.date = past
        store.record_held_draft(
            gig,
            status="neg_pending",
            draft_id="draft-1",
            draft_subject="S",
            hold_reason="fee_negotiation",
            negotiable_fee=120,
        )
        expired_rows = store.expire_past_applied()
        assert len(expired_rows) == 1
        assert expired_rows[0]["draft_id"] == "draft-1"
        assert expired_rows[0]["status"] == "expired"
        r = store.get_by_gig_id(expired_rows[0]["gig_id"])
        assert r["status"] == "expired"
        assert r["decision"] == "expired"
        assert r["decided_at"]

    def test_expire_past_review_pending_flips_to_expired_and_is_returned(self):
        past = (datetime.date.today() - datetime.timedelta(days=5)).strftime("%A, %B %d, %Y")
        gig = _neg_gig()
        gig.date = past
        store.record_held_draft(
            gig,
            status="review_pending",
            draft_id="draft-2",
            draft_subject="S",
            hold_reason="weekday",
        )
        expired_rows = store.expire_past_applied()
        assert len(expired_rows) == 1
        assert expired_rows[0]["draft_id"] == "draft-2"
        assert store.get_by_gig_id(expired_rows[0]["gig_id"])["status"] == "expired"

    def test_expire_does_not_flip_future_held_rows(self):
        future = (datetime.date.today() + datetime.timedelta(days=30)).strftime("%A, %B %d, %Y")
        gig = _neg_gig()
        gig.date = future
        store.record_held_draft(
            gig,
            status="neg_pending",
            draft_id="d1",
            draft_subject="S",
            hold_reason="fee_negotiation",
            negotiable_fee=120,
        )
        assert store.expire_past_applied() == []
        assert store.get_by_gig_id(store.list_held()[0]["gig_id"])["status"] == "neg_pending"

    def test_expire_still_flips_past_applied_to_no_response_and_returns_it(self):
        past = (datetime.date.today() - datetime.timedelta(days=5)).strftime("%A, %B %d, %Y")
        gig = _neg_gig()
        gig.date = past
        store.record_application(gig)
        expired_rows = store.expire_past_applied()
        assert len(expired_rows) == 1
        assert "draft_id" not in expired_rows[0]
        assert expired_rows[0]["status"] == "no_response"
        assert store._read()[0]["status"] == "no_response"

    def test_expire_returns_both_kinds_of_row_in_one_call(self):
        past = (datetime.date.today() - datetime.timedelta(days=5)).strftime("%A, %B %d, %Y")
        applied_gig = _neg_gig("https://e.com/applied")
        applied_gig.date = past
        store.record_application(applied_gig)
        held_gig = _neg_gig("https://e.com/held")
        held_gig.date = past
        store.record_held_draft(
            held_gig,
            status="review_pending",
            draft_id="d1",
            draft_subject="S",
            hold_reason="weekday",
        )
        expired_rows = store.expire_past_applied()
        assert len(expired_rows) == 2
        statuses = {r["url"]: r["status"] for r in expired_rows}
        assert statuses == {"https://e.com/applied": "no_response", "https://e.com/held": "expired"}
