"""Tests for organist_bot/weekly_summary.py."""

import datetime
import json
from unittest.mock import patch

from organist_bot import weekly_summary as ws

# ── should_send logic ─────────────────────────────────────────────────────────


class TestShouldSend:
    def _saturday(self, hour: int = 9, minute: int = 5) -> datetime.datetime:
        # Find the next Saturday from a fixed reference point
        # weekday() == 5 → Saturday
        d = datetime.date(2026, 5, 30)  # This is a Saturday
        return datetime.datetime(d.year, d.month, d.day, hour, minute)

    def _weekday(self) -> datetime.datetime:
        d = datetime.date(2026, 5, 29)  # Friday
        return datetime.datetime(d.year, d.month, d.day, 9, 5)

    def test_returns_true_on_saturday_after_time(self):
        assert ws.should_send(self._saturday(9, 5), None, "09:00") is True

    def test_returns_false_on_weekday(self):
        assert ws.should_send(self._weekday(), None, "09:00") is False

    def test_returns_false_before_send_time(self):
        assert ws.should_send(self._saturday(8, 59), None, "09:00") is False

    def test_returns_false_exactly_at_send_time(self):
        # time(9,0) is NOT < time(9,0), so it should fire at exactly 09:00
        assert ws.should_send(self._saturday(9, 0), None, "09:00") is True

    def test_returns_false_if_already_sent_today(self):
        sat = self._saturday()
        assert ws.should_send(sat, sat.date(), "09:00") is False

    def test_returns_true_if_sent_last_saturday(self):
        sat = self._saturday()
        last_week = sat.date() - datetime.timedelta(days=7)
        assert ws.should_send(sat, last_week, "09:00") is True

    def test_handles_invalid_time_string(self):
        """Falls back to 09:00 when time string is malformed."""
        assert ws.should_send(self._saturday(9, 5), None, "bad:time") is True

    def test_custom_send_time(self):
        assert ws.should_send(self._saturday(11, 0), None, "10:30") is True
        assert ws.should_send(self._saturday(10, 29), None, "10:30") is False


# ── last-sent persistence ─────────────────────────────────────────────────────


class TestLastSentPersistence:
    def test_load_returns_none_when_file_absent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ws, "_LAST_SENT_FILE", tmp_path / "weekly_summary_last.txt")
        assert ws.load_last_sent_date() is None

    def test_save_and_load_round_trip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ws, "_LAST_SENT_FILE", tmp_path / "weekly_summary_last.txt")
        d = datetime.date(2026, 5, 30)
        ws.save_last_sent_date(d)
        assert ws.load_last_sent_date() == d

    def test_load_handles_corrupt_file(self, tmp_path, monkeypatch):
        f = tmp_path / "weekly_summary_last.txt"
        f.write_text("not-a-date")
        monkeypatch.setattr(ws, "_LAST_SENT_FILE", f)
        assert ws.load_last_sent_date() is None


# ── build_message ─────────────────────────────────────────────────────────────


class TestBuildMessage:
    def test_message_contains_section_headers(self):
        with (
            patch.object(ws.application_store, "list_applications", return_value=[]),
            patch.object(ws, "_load_invoices", return_value=[]),
        ):
            msg = ws.build_message()

        assert "Upcoming gigs" in msg or "No accepted gigs" in msg
        assert "Pending applications" in msg or "No pending applications" in msg
        assert "Outstanding invoices" in msg or "No outstanding invoices" in msg

    def test_message_lists_upcoming_accepted_gig(self):
        today = datetime.date.today()
        accepted = {
            "header": "Sunday Service",
            "organisation": "St Mary's",
            "date": today.strftime("%A, %B %d, %Y"),
            "time": "10:00 AM",
            "fee": "£120",
            "status": "accepted",
            "applied_at": "2026-05-01T00:00:00Z",
        }
        with (
            patch.object(ws.application_store, "list_applications", return_value=[accepted]),
            patch.object(ws, "_load_invoices", return_value=[]),
        ):
            msg = ws.build_message()

        assert "Sunday Service" in msg
        assert "St Mary's" in msg

    def test_message_lists_pending_application(self):
        applied = {
            "header": "Wedding",
            "organisation": "Church",
            "date": "Sunday, June 1, 2026",
            "status": "applied",
            "applied_at": "2026-05-01T00:00:00Z",
        }
        with (
            patch.object(ws.application_store, "list_applications", return_value=[applied]),
            patch.object(ws, "_load_invoices", return_value=[]),
        ):
            msg = ws.build_message()

        assert "Wedding" in msg

    def test_message_lists_outstanding_invoice(self):
        invoice = {
            "invoice_number": "INV-2026-001",
            "client_name": "St Bartholomew",
            "total": 150.0,
        }
        with (
            patch.object(ws.application_store, "list_applications", return_value=[]),
            patch.object(ws, "_load_invoices", return_value=[invoice]),
        ):
            msg = ws.build_message()

        assert "INV-2026-001" in msg
        assert "St Bartholomew" in msg

    def test_paid_invoice_not_listed_as_outstanding(self):
        paid_invoice = {
            "invoice_number": "INV-2026-002",
            "client_name": "All Saints",
            "total": 200.0,
            "paid_at": "2026-05-15T00:00:00Z",
        }
        with (
            patch.object(ws.application_store, "list_applications", return_value=[]),
            patch.object(ws, "_load_invoices", return_value=[paid_invoice]),
        ):
            msg = ws.build_message()

        assert "INV-2026-002" not in msg
        assert "No outstanding invoices" in msg


# ── _load_invoices ────────────────────────────────────────────────────────────


class TestLoadInvoices:
    def test_returns_empty_list_when_file_absent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ws, "_INVOICES_FILE", tmp_path / "invoices.json")
        assert ws._load_invoices() == []

    def test_loads_dict_keyed_by_invoice_number(self, tmp_path, monkeypatch):
        data = {
            "INV-001": {"invoice_number": "INV-001", "total": 100.0},
            "INV-002": {"invoice_number": "INV-002", "total": 200.0},
        }
        f = tmp_path / "invoices.json"
        f.write_text(json.dumps(data))
        monkeypatch.setattr(ws, "_INVOICES_FILE", f)
        result = ws._load_invoices()
        assert len(result) == 2

    def test_returns_empty_list_on_bad_json(self, tmp_path, monkeypatch):
        f = tmp_path / "invoices.json"
        f.write_text("not json")
        monkeypatch.setattr(ws, "_INVOICES_FILE", f)
        assert ws._load_invoices() == []


# ── check_and_send ────────────────────────────────────────────────────────────


class TestCheckAndSend:
    def _saturday_now(self) -> datetime.datetime:
        return datetime.datetime(2026, 5, 30, 9, 5)  # Saturday

    def test_sends_alert_on_saturday(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ws, "_LAST_SENT_FILE", tmp_path / "last.txt")
        mock_settings = type("S", (), {"weekly_summary_time": "09:00"})()

        with (
            patch("organist_bot.weekly_summary.datetime") as mock_dt,
            patch.object(ws, "build_message", return_value="summary text"),
            patch.object(ws.alert, "send_alert") as mock_alert,
            patch("organist_bot.weekly_summary.settings", mock_settings),
        ):
            mock_dt.datetime.now.return_value = self._saturday_now()
            mock_dt.date.today.return_value = datetime.date.today()
            mock_dt.date.fromisoformat = datetime.date.fromisoformat
            mock_dt.time = datetime.time
            mock_dt.timedelta = datetime.timedelta
            ws.check_and_send()

        mock_alert.assert_called_once_with("summary text")

    def test_saves_last_sent_date_after_send(self, tmp_path, monkeypatch):
        last_file = tmp_path / "last.txt"
        monkeypatch.setattr(ws, "_LAST_SENT_FILE", last_file)
        mock_settings = type("S", (), {"weekly_summary_time": "09:00"})()

        with (
            patch("organist_bot.weekly_summary.datetime") as mock_dt,
            patch.object(ws, "build_message", return_value="summary"),
            patch.object(ws.alert, "send_alert"),
            patch("organist_bot.weekly_summary.settings", mock_settings),
        ):
            sat = self._saturday_now()
            mock_dt.datetime.now.return_value = sat
            mock_dt.date.today.return_value = datetime.date.today()
            mock_dt.date.fromisoformat = datetime.date.fromisoformat
            mock_dt.time = datetime.time
            mock_dt.timedelta = datetime.timedelta
            ws.check_and_send()

        assert last_file.exists()
        assert last_file.read_text().strip() == sat.date().isoformat()

    def test_does_not_send_on_weekday(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ws, "_LAST_SENT_FILE", tmp_path / "last.txt")
        mock_settings = type("S", (), {"weekly_summary_time": "09:00"})()
        friday = datetime.datetime(2026, 5, 29, 9, 5)  # Friday

        with (
            patch("organist_bot.weekly_summary.datetime") as mock_dt,
            patch.object(ws.alert, "send_alert") as mock_alert,
            patch("organist_bot.weekly_summary.settings", mock_settings),
        ):
            mock_dt.datetime.now.return_value = friday
            mock_dt.date.today.return_value = datetime.date.today()
            mock_dt.date.fromisoformat = datetime.date.fromisoformat
            mock_dt.time = datetime.time
            mock_dt.timedelta = datetime.timedelta
            ws.check_and_send()

        mock_alert.assert_not_called()

    def test_does_not_send_twice_on_same_saturday(self, tmp_path, monkeypatch):
        last_file = tmp_path / "last.txt"
        sat_date = datetime.date(2026, 5, 30)
        last_file.write_text(sat_date.isoformat())
        monkeypatch.setattr(ws, "_LAST_SENT_FILE", last_file)
        mock_settings = type("S", (), {"weekly_summary_time": "09:00"})()

        with (
            patch("organist_bot.weekly_summary.datetime") as mock_dt,
            patch.object(ws.alert, "send_alert") as mock_alert,
            patch("organist_bot.weekly_summary.settings", mock_settings),
        ):
            mock_dt.datetime.now.return_value = datetime.datetime(2026, 5, 30, 11, 0)
            mock_dt.date.today.return_value = datetime.date.today()
            mock_dt.date.fromisoformat = datetime.date.fromisoformat
            mock_dt.time = datetime.time
            mock_dt.timedelta = datetime.timedelta
            ws.check_and_send()

        mock_alert.assert_not_called()
