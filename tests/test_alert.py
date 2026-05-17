# tests/test_alert.py
"""Tests for organist_bot.alert.send_alert."""

from unittest.mock import MagicMock, patch

from organist_bot.alert import send_alert


class TestSendAlert:
    def test_posts_to_telegram_when_configured(self):
        """Sends a POST to the Telegram Bot API with the correct payload."""
        mock_post = MagicMock()
        with (
            patch("organist_bot.alert.settings") as mock_settings,
            patch("organist_bot.alert._requests.post", mock_post),
        ):
            mock_settings.telegram_bot_token = "TOKEN123"
            mock_settings.telegram_chat_id = 42
            send_alert("test message")

        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        assert "TOKEN123" in call_kwargs.args[0]
        assert call_kwargs.kwargs["json"]["text"] == "test message"
        assert call_kwargs.kwargs["json"]["chat_id"] == 42
        assert call_kwargs.kwargs["timeout"] == 10

    def test_no_op_when_token_missing(self):
        """Does nothing (no POST) when telegram_bot_token is not set."""
        mock_post = MagicMock()
        with (
            patch("organist_bot.alert.settings") as mock_settings,
            patch("organist_bot.alert._requests.post", mock_post),
        ):
            mock_settings.telegram_bot_token = ""
            mock_settings.telegram_chat_id = 42
            send_alert("test message")

        mock_post.assert_not_called()

    def test_no_op_when_chat_id_missing(self):
        """Does nothing when telegram_chat_id is not set."""
        mock_post = MagicMock()
        with (
            patch("organist_bot.alert.settings") as mock_settings,
            patch("organist_bot.alert._requests.post", mock_post),
        ):
            mock_settings.telegram_bot_token = "TOKEN"
            mock_settings.telegram_chat_id = None
            send_alert("test message")

        mock_post.assert_not_called()

    def test_network_failure_is_swallowed(self):
        """A network error during POST does not propagate and is logged at WARNING."""
        mock_post = MagicMock(side_effect=ConnectionError("timeout"))
        with (
            patch("organist_bot.alert.settings") as mock_settings,
            patch("organist_bot.alert._requests.post", mock_post),
            patch("organist_bot.alert.logger") as mock_logger,
        ):
            mock_settings.telegram_bot_token = "TOKEN"
            mock_settings.telegram_chat_id = 42
            send_alert("test message")  # must not raise

        mock_logger.warning.assert_called_once()
