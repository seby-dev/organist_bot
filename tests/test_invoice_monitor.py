"""Tests for organist_bot.invoice_monitor."""

import datetime
from unittest.mock import MagicMock, patch

import organist_bot.invoice_monitor as monitor


def _make_invoice(
    number="INV-2026-001",
    emailed=True,
    emailed_at=None,
    paid_at=None,
    reminder_sent=False,
    checked_reply_ids=None,
    client_email="client@example.com",
    client_name="St Paul's",
    total=150.0,
) -> dict:
    if emailed_at is None and emailed:
        # Default: 6 days ago (overdue)
        emailed_at = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=6)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    return {
        "invoice_number": number,
        "client_name": client_name,
        "client_email": client_email,
        "total": total,
        "emailed": emailed,
        "emailed_at": emailed_at,
        "paid_at": paid_at,
        "reminder_sent": reminder_sent,
        "checked_reply_ids": checked_reply_ids or [],
    }


def _recent_emailed_at() -> str:
    return (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=2)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


class TestOverdueReminder:
    def test_sends_reminder_for_overdue_invoice(self):
        invoices = {"INV-2026-001": _make_invoice()}
        with (
            patch("organist_bot.invoice_monitor.load_invoices", return_value=invoices),
            patch("organist_bot.invoice_monitor._make_gmail_client", return_value=None),
            patch("organist_bot.invoice_monitor.save_invoice_field"),
            patch("organist_bot.invoice_monitor.alert.send_alert") as mock_alert,
        ):
            monitor.check_invoice_reminders_and_replies()

        mock_alert.assert_called_once()
        alert_text = mock_alert.call_args.args[0]
        assert "INV-2026-001" in alert_text
        assert "St Paul's" in alert_text
        assert "£150" in alert_text

    def test_sets_reminder_sent_true_after_sending(self):
        invoices = {"INV-2026-001": _make_invoice()}
        with (
            patch("organist_bot.invoice_monitor.load_invoices", return_value=invoices),
            patch("organist_bot.invoice_monitor._make_gmail_client", return_value=None),
            patch("organist_bot.invoice_monitor.save_invoice_field") as mock_save,
            patch("organist_bot.invoice_monitor.alert.send_alert"),
        ):
            monitor.check_invoice_reminders_and_replies()

        mock_save.assert_any_call("INV-2026-001", "reminder_sent", True)

    def test_does_not_send_when_not_overdue(self):
        invoices = {"INV-2026-001": _make_invoice(emailed_at=_recent_emailed_at())}
        with (
            patch("organist_bot.invoice_monitor.load_invoices", return_value=invoices),
            patch("organist_bot.invoice_monitor._make_gmail_client", return_value=None),
            patch("organist_bot.invoice_monitor.save_invoice_field"),
            patch("organist_bot.invoice_monitor.alert.send_alert") as mock_alert,
        ):
            monitor.check_invoice_reminders_and_replies()

        mock_alert.assert_not_called()

    def test_does_not_send_when_reminder_already_sent(self):
        invoices = {"INV-2026-001": _make_invoice(reminder_sent=True)}
        with (
            patch("organist_bot.invoice_monitor.load_invoices", return_value=invoices),
            patch("organist_bot.invoice_monitor._make_gmail_client", return_value=None),
            patch("organist_bot.invoice_monitor.save_invoice_field"),
            patch("organist_bot.invoice_monitor.alert.send_alert") as mock_alert,
        ):
            monitor.check_invoice_reminders_and_replies()

        mock_alert.assert_not_called()

    def test_does_not_send_when_already_paid(self):
        invoices = {"INV-2026-001": _make_invoice(paid_at="2026-06-05T10:00:00Z")}
        with (
            patch("organist_bot.invoice_monitor.load_invoices", return_value=invoices),
            patch("organist_bot.invoice_monitor._make_gmail_client", return_value=None),
            patch("organist_bot.invoice_monitor.save_invoice_field"),
            patch("organist_bot.invoice_monitor.alert.send_alert") as mock_alert,
        ):
            monitor.check_invoice_reminders_and_replies()

        mock_alert.assert_not_called()

    def test_does_not_send_when_not_emailed(self):
        invoices = {"INV-2026-001": _make_invoice(emailed=False, emailed_at=None)}
        with (
            patch("organist_bot.invoice_monitor.load_invoices", return_value=invoices),
            patch("organist_bot.invoice_monitor._make_gmail_client", return_value=None),
            patch("organist_bot.invoice_monitor.save_invoice_field"),
            patch("organist_bot.invoice_monitor.alert.send_alert") as mock_alert,
        ):
            monitor.check_invoice_reminders_and_replies()

        mock_alert.assert_not_called()

    def test_does_not_send_reminder_when_no_telegram_alert_but_does_not_set_reminder_sent(self):
        """If alert.send_alert fails, reminder_sent should NOT be set to True."""
        invoices = {"INV-2026-001": _make_invoice()}
        with (
            patch("organist_bot.invoice_monitor.load_invoices", return_value=invoices),
            patch("organist_bot.invoice_monitor._make_gmail_client", return_value=None),
            patch("organist_bot.invoice_monitor.save_invoice_field") as mock_save,
            patch(
                "organist_bot.invoice_monitor.alert.send_alert",
                side_effect=Exception("Telegram down"),
            ),
        ):
            monitor.check_invoice_reminders_and_replies()

        # reminder_sent should NOT be set to True since alert failed
        for c in mock_save.call_args_list:
            if c.args[1] == "reminder_sent":
                assert c.args[2] is not True


class TestReplyMonitoring:
    def test_marks_invoice_paid_on_paid_reply(self):
        invoices = {"INV-2026-001": _make_invoice()}
        reply = {
            "message_id": "msg1",
            "sender": "client@example.com",
            "body": "Hi, I have transferred the payment. Thank you.",
            "direction": "incoming",
        }
        mock_gmail = MagicMock()
        mock_gmail.fetch_invoice_replies.return_value = [reply]

        with (
            patch("organist_bot.invoice_monitor.load_invoices", return_value=invoices),
            patch("organist_bot.invoice_monitor._make_gmail_client", return_value=mock_gmail),
            patch("organist_bot.invoice_monitor._classify_payment_reply", return_value="paid"),
            patch("organist_bot.invoice_monitor.mark_invoice_paid") as mock_paid,
            patch("organist_bot.invoice_monitor.save_invoice_field"),
            patch("organist_bot.invoice_monitor.alert.send_alert") as mock_alert,
        ):
            monitor.check_invoice_reminders_and_replies()

        mock_paid.assert_called_once_with("INV-2026-001")
        mock_alert.assert_called_once()
        assert "paid" in mock_alert.call_args.args[0].lower()

    def test_skips_already_seen_message_ids(self):
        invoices = {"INV-2026-001": _make_invoice(checked_reply_ids=["msg1"])}
        reply = {
            "message_id": "msg1",
            "sender": "client@example.com",
            "body": "Payment sent.",
            "direction": "incoming",
        }
        mock_gmail = MagicMock()
        mock_gmail.fetch_invoice_replies.return_value = [reply]

        with (
            patch("organist_bot.invoice_monitor.load_invoices", return_value=invoices),
            patch("organist_bot.invoice_monitor._make_gmail_client", return_value=mock_gmail),
            patch("organist_bot.invoice_monitor._classify_payment_reply") as mock_classify,
            patch("organist_bot.invoice_monitor.mark_invoice_paid") as mock_paid,
            patch("organist_bot.invoice_monitor.save_invoice_field"),
            patch("organist_bot.invoice_monitor.alert.send_alert"),
        ):
            monitor.check_invoice_reminders_and_replies()

        mock_classify.assert_not_called()
        mock_paid.assert_not_called()

    def test_does_not_mark_paid_on_unclear_reply(self):
        invoices = {"INV-2026-001": _make_invoice()}
        reply = {
            "message_id": "msg2",
            "sender": "client@example.com",
            "body": "Can you resend the invoice?",
            "direction": "incoming",
        }
        mock_gmail = MagicMock()
        mock_gmail.fetch_invoice_replies.return_value = [reply]

        with (
            patch("organist_bot.invoice_monitor.load_invoices", return_value=invoices),
            patch("organist_bot.invoice_monitor._make_gmail_client", return_value=mock_gmail),
            patch("organist_bot.invoice_monitor._classify_payment_reply", return_value="unclear"),
            patch("organist_bot.invoice_monitor.mark_invoice_paid") as mock_paid,
            patch("organist_bot.invoice_monitor.save_invoice_field"),
            patch("organist_bot.invoice_monitor.alert.send_alert"),
        ):
            monitor.check_invoice_reminders_and_replies()

        mock_paid.assert_not_called()

    def test_appends_new_message_id_to_checked_reply_ids(self):
        invoices = {"INV-2026-001": _make_invoice()}
        reply = {
            "message_id": "msg3",
            "sender": "client@example.com",
            "body": "Thanks, I'll pay next week.",
            "direction": "incoming",
        }
        mock_gmail = MagicMock()
        mock_gmail.fetch_invoice_replies.return_value = [reply]

        saved_fields = {}

        def capture_save(inv_num, field, value):
            saved_fields[(inv_num, field)] = value

        with (
            patch("organist_bot.invoice_monitor.load_invoices", return_value=invoices),
            patch("organist_bot.invoice_monitor._make_gmail_client", return_value=mock_gmail),
            patch("organist_bot.invoice_monitor._classify_payment_reply", return_value="unclear"),
            patch("organist_bot.invoice_monitor.mark_invoice_paid"),
            patch("organist_bot.invoice_monitor.save_invoice_field", side_effect=capture_save),
            patch("organist_bot.invoice_monitor.alert.send_alert"),
        ):
            monitor.check_invoice_reminders_and_replies()

        assert ("INV-2026-001", "checked_reply_ids") in saved_fields
        assert "msg3" in saved_fields[("INV-2026-001", "checked_reply_ids")]

    def test_gmail_failure_does_not_crash(self):
        invoices = {"INV-2026-001": _make_invoice(reminder_sent=True)}
        mock_gmail = MagicMock()
        mock_gmail.fetch_invoice_replies.side_effect = Exception("Gmail down")

        with (
            patch("organist_bot.invoice_monitor.load_invoices", return_value=invoices),
            patch("organist_bot.invoice_monitor._make_gmail_client", return_value=mock_gmail),
            patch("organist_bot.invoice_monitor.save_invoice_field"),
            patch("organist_bot.invoice_monitor.alert.send_alert"),
        ):
            # Should not raise
            monitor.check_invoice_reminders_and_replies()
