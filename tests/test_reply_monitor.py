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


class TestCreateCalendarEventWithBuffers:
    """_create_calendar_event should call add_travel_buffers and store IDs."""

    def _make_record(self, postcode="CM1 1AA", time_str="10:00 AM"):
        return {
            "url": "http://a.com/1",
            "header": "Wedding Service",
            "organisation": "St Mary's",
            "date": "2026-07-15",
            "time": time_str,
            "fee": "£200",
            "email": "stmary@example.com",
            "status": "accepted",
            "postcode": postcode,
        }

    def test_creates_travel_buffers_when_time_is_parseable(self):
        record = self._make_record()
        with (
            patch("organist_bot.reply_monitor.settings") as mock_settings,
            patch("organist_bot.reply_monitor.GoogleCalendarClient") as mock_cal_cls,
            patch("organist_bot.reply_monitor.travel") as mock_travel,
            patch("organist_bot.reply_monitor.application_store") as mock_store,
        ):
            mock_settings.google_calendar_id = "cal@test.com"
            mock_settings.google_calendar_credentials_file = "creds.json"
            mock_settings.max_travel_minutes = 45
            mock_cal = mock_cal_cls.return_value
            mock_cal.add_gig.return_value = "event_123"
            mock_cal.add_travel_buffers.return_value = ("before_id", "after_id")
            mock_travel.get_travel_minutes.return_value = 35

            from organist_bot.reply_monitor import _create_calendar_event

            result = _create_calendar_event(record)

        assert result is True
        mock_travel.get_travel_minutes.assert_called_once_with("CM1 1AA")
        mock_cal.add_travel_buffers.assert_called_once()
        mock_store.update_travel_buffer_ids.assert_called_once_with(
            "http://a.com/1", "before_id", "after_id"
        )

    def test_falls_back_to_max_travel_minutes_when_maps_returns_none(self):
        record = self._make_record()
        with (
            patch("organist_bot.reply_monitor.settings") as mock_settings,
            patch("organist_bot.reply_monitor.GoogleCalendarClient") as mock_cal_cls,
            patch("organist_bot.reply_monitor.travel") as mock_travel,
            patch("organist_bot.reply_monitor.application_store"),
        ):
            mock_settings.google_calendar_id = "cal@test.com"
            mock_settings.google_calendar_credentials_file = "creds.json"
            mock_settings.max_travel_minutes = 45
            mock_cal = mock_cal_cls.return_value
            mock_cal.add_gig.return_value = "event_123"
            mock_cal.add_travel_buffers.return_value = ("b", "a")
            mock_travel.get_travel_minutes.return_value = None

            from organist_bot.reply_monitor import _create_calendar_event

            result = _create_calendar_event(record)

        assert result is True
        # travel_minutes should be the fallback value
        call_args = mock_cal.add_travel_buffers.call_args
        travel_minutes_used = call_args.kwargs.get("travel_minutes") or call_args.args[3]
        assert travel_minutes_used == 45

    def test_skips_buffers_when_time_is_blank(self):
        record = self._make_record(time_str="")
        with (
            patch("organist_bot.reply_monitor.settings") as mock_settings,
            patch("organist_bot.reply_monitor.GoogleCalendarClient") as mock_cal_cls,
            patch("organist_bot.reply_monitor.travel") as mock_travel,
            patch("organist_bot.reply_monitor.application_store"),
        ):
            mock_settings.google_calendar_id = "cal@test.com"
            mock_settings.google_calendar_credentials_file = "creds.json"
            mock_settings.max_travel_minutes = 45
            mock_cal = mock_cal_cls.return_value
            mock_cal.add_gig.side_effect = ValueError("Cannot parse gig time: ''")
            mock_travel.get_travel_minutes.return_value = 30

            from organist_bot.reply_monitor import _create_calendar_event

            result = _create_calendar_event(record)

        assert result is False
        mock_cal.add_travel_buffers.assert_not_called()


class TestCancellationDeletesBuffers:
    def _make_record(self, url, email):
        return {
            "url": url,
            "header": "Wedding",
            "organisation": "St John",
            "date": "2026-07-15",
            "time": "11:00 AM",
            "fee": "£150",
            "email": email,
            "status": "accepted",
            "applied_at": "2026-06-01T10:00:00Z",
            "updated_at": "2026-06-01T10:00:00Z",
            "reply_message_id": None,
            "travel_before_event_id": "before_abc",
            "travel_after_event_id": "after_def",
        }

    def test_cancellation_deletes_travel_buffer_events(self):
        from unittest.mock import MagicMock

        records = [self._make_record("http://a.com/1", "church@example.com")]
        messages = [
            {
                "message_id": "msg1",
                "sender": "church@example.com",
                "recipient": "me@example.com",
                "body": "We need to cancel the booking.",
                "direction": "incoming",
            }
        ]
        with (
            patch("organist_bot.reply_monitor.settings") as mock_settings,
            patch(
                "organist_bot.reply_monitor.application_store.list_applications",
                return_value=records,
            ),
            patch("organist_bot.reply_monitor.application_store.update_reply_message_id"),
            patch("organist_bot.reply_monitor._make_gmail_client") as mock_gmail,
            patch("organist_bot.reply_monitor._classify_reply", return_value="cancellation"),
            patch("organist_bot.reply_monitor._send_telegram_notification"),
            patch("organist_bot.reply_monitor._make_calendar_client") as mock_cal_fn,
        ):
            mock_settings.gmail_credentials_file = "creds.json"
            mock_settings.gmail_token_file = "token.json"
            mock_settings.google_calendar_id = "cal@test.com"
            mock_settings.google_calendar_credentials_file = "creds.json"
            mock_settings.anthropic_api_key = "key"
            mock_gmail.return_value.fetch_reply_messages.return_value = messages
            mock_cal = MagicMock()
            mock_cal_fn.return_value = mock_cal

            check_replies()

        deleted_ids = [c.args[0] for c in mock_cal.delete_event.call_args_list]
        assert "before_abc" in deleted_ids
        assert "after_def" in deleted_ids
