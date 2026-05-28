"""Tests for invoice_generator schema changes — no browser required."""

import datetime

import pytest

from organist_bot.integrations.invoice_generator import (
    load_invoices,
    mark_invoice_emailed,
    mark_invoice_paid,
    save_invoice,
    save_invoice_field,
)


@pytest.fixture(autouse=True)
def tmp_invoices(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "organist_bot.integrations.invoice_generator.INVOICES_FILE",
        tmp_path / "invoices.json",
    )


def _base_invoice(number="INV-2026-001") -> dict:
    return {
        "invoice_number": number,
        "client_key": "stpauls",
        "client_name": "St Paul's",
        "client_email": "stpauls@example.com",
        "client_cc": [],
        "year": 2026,
        "date": "1 June 2026",
        "items": [],
        "total": 150.0,
        "currency": "£",
        "emailed": False,
        "created_at": "2026-06-01T10:00:00",
        "pdf_path": "/tmp/inv.pdf",
    }


class TestSaveInvoiceInitialisesNewFields:
    def test_save_invoice_sets_emailed_at_none(self):
        inv = _base_invoice()
        save_invoice(inv)
        stored = load_invoices()["INV-2026-001"]
        assert stored["emailed_at"] is None

    def test_save_invoice_sets_paid_at_none(self):
        inv = _base_invoice()
        save_invoice(inv)
        stored = load_invoices()["INV-2026-001"]
        assert stored["paid_at"] is None

    def test_save_invoice_sets_reminder_sent_false(self):
        inv = _base_invoice()
        save_invoice(inv)
        stored = load_invoices()["INV-2026-001"]
        assert stored["reminder_sent"] is False

    def test_save_invoice_sets_checked_reply_ids_empty_list(self):
        inv = _base_invoice()
        save_invoice(inv)
        stored = load_invoices()["INV-2026-001"]
        assert stored["checked_reply_ids"] == []


class TestMarkInvoiceEmailed:
    def test_sets_emailed_true_and_emailed_at_timestamp(self):
        save_invoice(_base_invoice())
        mark_invoice_emailed("INV-2026-001")
        stored = load_invoices()["INV-2026-001"]
        assert stored["emailed"] is True
        assert stored["emailed_at"] is not None
        # Should be a valid ISO timestamp
        datetime.datetime.fromisoformat(stored["emailed_at"].replace("Z", "+00:00"))

    def test_emailed_at_is_utc(self):
        save_invoice(_base_invoice())
        mark_invoice_emailed("INV-2026-001")
        stored = load_invoices()["INV-2026-001"]
        assert stored["emailed_at"].endswith("Z")


class TestMarkInvoicePaid:
    def test_sets_paid_at_timestamp(self):
        save_invoice(_base_invoice())
        result = mark_invoice_paid("INV-2026-001")
        assert result is True
        stored = load_invoices()["INV-2026-001"]
        assert stored["paid_at"] is not None
        datetime.datetime.fromisoformat(stored["paid_at"].replace("Z", "+00:00"))

    def test_returns_false_for_unknown_invoice(self):
        result = mark_invoice_paid("INV-9999-999")
        assert result is False

    def test_paid_at_is_utc(self):
        save_invoice(_base_invoice())
        mark_invoice_paid("INV-2026-001")
        stored = load_invoices()["INV-2026-001"]
        assert stored["paid_at"].endswith("Z")


class TestSaveInvoiceField:
    def test_updates_single_field(self):
        save_invoice(_base_invoice())
        save_invoice_field("INV-2026-001", "reminder_sent", True)
        stored = load_invoices()["INV-2026-001"]
        assert stored["reminder_sent"] is True

    def test_updates_list_field(self):
        save_invoice(_base_invoice())
        save_invoice_field("INV-2026-001", "checked_reply_ids", ["msg1", "msg2"])
        stored = load_invoices()["INV-2026-001"]
        assert stored["checked_reply_ids"] == ["msg1", "msg2"]

    def test_unknown_invoice_does_not_raise(self):
        # Should silently do nothing for unknown invoice
        save_invoice_field("INV-9999-999", "reminder_sent", True)
