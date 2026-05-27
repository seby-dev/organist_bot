from unittest.mock import patch

import organist_bot.reply_monitor  # noqa: F401 — ensures module is resolvable by patch()
from organist_bot.reply_monitor import check_replies


def _make_record(url, email, status, reply_message_id=None):
    return {
        "url": url,
        "header": "Evening Service",
        "organisation": "St John",
        "date": "2026-06-15",
        "fee": "£100",
        "email": email,
        "status": status,
        "applied_at": "2026-06-01T10:00:00Z",
        "updated_at": "2026-06-01T10:00:00Z",
        "reply_message_id": reply_message_id,
    }


def _make_message(msg_id, sender, direction="incoming"):
    return {
        "message_id": msg_id,
        "sender": sender,
        "recipient": "me@example.com",
        "body": "Thank you for your application, we'd love to book you.",
        "direction": direction,
    }


class TestCheckReplies:
    def _patch_settings(self, mock_settings):
        mock_settings.gmail_credentials_file = "creds.json"
        mock_settings.gmail_token_file = "token.json"
        mock_settings.telegram_bot_token = ""
        mock_settings.telegram_chat_id = ""
        mock_settings.anthropic_api_key = ""
        mock_settings.google_calendar_id = ""
        mock_settings.google_calendar_credentials_file = ""

    def test_accepted_reply_calls_upsert_and_notifies(self):
        records = [_make_record("http://a.com/1", "church@example.com", "applied")]
        messages = [_make_message("msg1", "church@example.com")]
        with (
            patch("organist_bot.reply_monitor.settings") as mock_settings,
            patch(
                "organist_bot.reply_monitor.application_store.list_applications",
                return_value=records,
            ),
            patch("organist_bot.reply_monitor.application_store.upsert_accepted") as mock_upsert,
            patch(
                "organist_bot.reply_monitor.application_store.update_reply_message_id"
            ) as mock_rid,
            patch("organist_bot.reply_monitor._make_gmail_client") as mock_gmail,
            patch("organist_bot.reply_monitor._classify_reply", return_value="accepted"),
            patch("organist_bot.reply_monitor._send_telegram_notification") as mock_notify,
            patch("organist_bot.reply_monitor._create_calendar_event") as mock_cal,
        ):
            self._patch_settings(mock_settings)
            mock_gmail.return_value.fetch_reply_messages.return_value = messages

            check_replies()
        mock_upsert.assert_called_once()
        mock_cal.assert_called_once()
        mock_notify.assert_called_once()
        mock_rid.assert_called_once_with("http://a.com/1", "msg1")

    def test_rejected_reply_updates_status_and_notifies(self):
        records = [_make_record("http://a.com/1", "church@example.com", "applied")]
        messages = [_make_message("msg1", "church@example.com")]
        with (
            patch("organist_bot.reply_monitor.settings") as mock_settings,
            patch(
                "organist_bot.reply_monitor.application_store.list_applications",
                return_value=records,
            ),
            patch("organist_bot.reply_monitor.application_store.update_status") as mock_update,
            patch(
                "organist_bot.reply_monitor.application_store.update_reply_message_id"
            ) as mock_rid,
            patch("organist_bot.reply_monitor._make_gmail_client") as mock_gmail,
            patch("organist_bot.reply_monitor._classify_reply", return_value="rejected"),
            patch("organist_bot.reply_monitor._send_telegram_notification") as mock_notify,
        ):
            self._patch_settings(mock_settings)
            mock_gmail.return_value.fetch_reply_messages.return_value = messages

            check_replies()
        mock_update.assert_called_once_with("http://a.com/1", "rejected")
        mock_notify.assert_called_once()
        mock_rid.assert_called_once_with("http://a.com/1", "msg1")

    def test_cancellation_sends_telegram_prompt_no_status_change(self):
        records = [_make_record("http://a.com/1", "church@example.com", "accepted")]
        messages = [_make_message("msg1", "church@example.com")]
        with (
            patch("organist_bot.reply_monitor.settings") as mock_settings,
            patch(
                "organist_bot.reply_monitor.application_store.list_applications",
                return_value=records,
            ),
            patch("organist_bot.reply_monitor.application_store.update_status") as mock_update,
            patch("organist_bot.reply_monitor.application_store.update_reply_message_id"),
            patch("organist_bot.reply_monitor._make_gmail_client") as mock_gmail,
            patch("organist_bot.reply_monitor._classify_reply", return_value="cancellation"),
            patch("organist_bot.reply_monitor._send_telegram_notification") as mock_notify,
        ):
            self._patch_settings(mock_settings)
            mock_gmail.return_value.fetch_reply_messages.return_value = messages

            check_replies()
        mock_update.assert_not_called()
        mock_notify.assert_called_once()
        call_text = mock_notify.call_args[0][0]
        assert "cancel" in call_text.lower() or "Cancel" in call_text

    def test_unclear_sends_notification_no_status_change(self):
        records = [_make_record("http://a.com/1", "church@example.com", "applied")]
        messages = [_make_message("msg1", "church@example.com")]
        with (
            patch("organist_bot.reply_monitor.settings") as mock_settings,
            patch(
                "organist_bot.reply_monitor.application_store.list_applications",
                return_value=records,
            ),
            patch("organist_bot.reply_monitor.application_store.update_status") as mock_update,
            patch("organist_bot.reply_monitor.application_store.update_reply_message_id"),
            patch("organist_bot.reply_monitor._make_gmail_client") as mock_gmail,
            patch("organist_bot.reply_monitor._classify_reply", return_value="unclear"),
            patch("organist_bot.reply_monitor._send_telegram_notification") as mock_notify,
        ):
            self._patch_settings(mock_settings)
            mock_gmail.return_value.fetch_reply_messages.return_value = messages

            check_replies()
        mock_update.assert_not_called()
        mock_notify.assert_called_once()

    def test_dedup_skips_message_when_reply_message_id_already_set(self):
        records = [
            _make_record("http://a.com/1", "church@example.com", "applied", reply_message_id="msg1")
        ]
        messages = [_make_message("msg1", "church@example.com")]
        with (
            patch("organist_bot.reply_monitor.settings") as mock_settings,
            patch(
                "organist_bot.reply_monitor.application_store.list_applications",
                return_value=records,
            ),
            patch("organist_bot.reply_monitor.application_store.update_status") as mock_update,
            patch("organist_bot.reply_monitor._make_gmail_client") as mock_gmail,
            patch("organist_bot.reply_monitor._classify_reply") as mock_classify,
        ):
            self._patch_settings(mock_settings)
            mock_gmail.return_value.fetch_reply_messages.return_value = messages

            check_replies()
        mock_classify.assert_not_called()
        mock_update.assert_not_called()

    def test_message_from_unknown_email_is_skipped(self):
        records = [_make_record("http://a.com/1", "known@example.com", "applied")]
        messages = [_make_message("msg1", "unknown@other.com")]
        with (
            patch("organist_bot.reply_monitor.settings") as mock_settings,
            patch(
                "organist_bot.reply_monitor.application_store.list_applications",
                return_value=records,
            ),
            patch("organist_bot.reply_monitor._make_gmail_client") as mock_gmail,
            patch("organist_bot.reply_monitor._classify_reply") as mock_classify,
        ):
            self._patch_settings(mock_settings)
            mock_gmail.return_value.fetch_reply_messages.return_value = messages

            check_replies()
        mock_classify.assert_not_called()

    def test_disabled_when_credentials_file_empty(self):
        with (
            patch("organist_bot.reply_monitor.settings") as mock_settings,
            patch("organist_bot.reply_monitor._make_gmail_client") as mock_gmail,
        ):
            mock_settings.gmail_credentials_file = ""

            check_replies()
        mock_gmail.assert_not_called()

    def test_fails_open_on_list_applications_error(self):
        with (
            patch("organist_bot.reply_monitor.settings") as mock_settings,
            patch(
                "organist_bot.reply_monitor.application_store.list_applications",
                side_effect=Exception("db error"),
            ),
        ):
            mock_settings.gmail_credentials_file = "creds.json"

            check_replies()  # must not raise
