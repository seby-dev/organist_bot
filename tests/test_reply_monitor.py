from unittest.mock import MagicMock, patch

import anthropic  # used to construct real TextBlock instances in classify tests

import organist_bot.reply_monitor  # noqa: F401 — ensures module is resolvable by patch()
from organist_bot.reply_monitor import _classify_reply, check_replies


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


# ── _classify_reply unit tests ────────────────────────────────────────────────


def _make_text_block(text: str) -> anthropic.types.TextBlock:
    """Return a real anthropic.types.TextBlock with the given text."""
    return anthropic.types.TextBlock(type="text", text=text)


def _make_anthropic_response(text: str) -> MagicMock:
    """Return a mock whose .content[0] is a real TextBlock."""
    resp = MagicMock()
    resp.content = [_make_text_block(text)]
    return resp


class TestClassifyReply:
    """Unit tests for _classify_reply — patches anthropic.Anthropic at the module level."""

    _MSG = {"sender": "church@example.com", "body": "We'd love to book you."}
    _REC = {"organisation": "St John", "date": "2026-06-15"}

    def _patch_client(self, response):
        """Patch anthropic.Anthropic in reply_monitor so .messages.create returns *response*."""
        mock_client = MagicMock()
        mock_client.messages.create.return_value = response
        mock_anthropic_cls = MagicMock(return_value=mock_client)
        return patch(
            "organist_bot.reply_monitor.anthropic.Anthropic", mock_anthropic_cls
        ), mock_client

    # (a) valid label "accepted" is returned as-is
    def test_accepted_label_returned(self):
        ctx, mock_client = self._patch_client(_make_anthropic_response("accepted"))
        with (
            ctx,
            patch("organist_bot.reply_monitor.settings") as s,
        ):
            s.anthropic_api_key = "key"
            result = _classify_reply(self._MSG, self._REC)
        assert result == "accepted"
        mock_client.messages.create.assert_called_once()

    # (a) second valid label: "rejected"
    def test_rejected_label_returned(self):
        ctx, _mock_client = self._patch_client(_make_anthropic_response("rejected"))
        with (
            ctx,
            patch("organist_bot.reply_monitor.settings") as s,
        ):
            s.anthropic_api_key = "key"
            result = _classify_reply(self._MSG, self._REC)
        assert result == "rejected"

    # (b) an unexpected label from the model normalises to "unclear"
    def test_unexpected_label_normalises_to_unclear(self):
        ctx, _mock_client = self._patch_client(_make_anthropic_response("DEFINITELY_NOT_A_LABEL"))
        with (
            ctx,
            patch("organist_bot.reply_monitor.settings") as s,
        ):
            s.anthropic_api_key = "key"
            result = _classify_reply(self._MSG, self._REC)
        # Must be exactly "unclear", NOT the raw unexpected label
        assert result == "unclear"

    # (b) whitespace / mixed-case in unexpected label still normalises to "unclear"
    def test_unexpected_label_with_extra_whitespace_normalises_to_unclear(self):
        ctx, _mock_client = self._patch_client(_make_anthropic_response("  MAYBE  "))
        with (
            ctx,
            patch("organist_bot.reply_monitor.settings") as s,
        ):
            s.anthropic_api_key = "key"
            result = _classify_reply(self._MSG, self._REC)
        assert result == "unclear"

    # (c) API exception does not propagate and returns "unclear"
    def test_api_exception_returns_unclear_without_raising(self):
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = Exception("network timeout")
        mock_anthropic_cls = MagicMock(return_value=mock_client)
        with (
            patch("organist_bot.reply_monitor.anthropic.Anthropic", mock_anthropic_cls),
            patch("organist_bot.reply_monitor.settings") as s,
        ):
            s.anthropic_api_key = "key"
            result = _classify_reply(self._MSG, self._REC)
        # Must not raise; must fall back to "unclear"
        assert result == "unclear"
