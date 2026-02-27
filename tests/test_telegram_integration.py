# tests/test_telegram_integration.py
"""Tests for the Telegram bot message handler."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from organist_bot.integrations.telegram_bot import _is_authorised, handle_message
from organist_bot.models import Gig

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_update(chat_id: int = 7973955362, text: str = "") -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def _make_gig(**overrides) -> Gig:
    defaults = dict(
        header="Sunday Service",
        organisation="St Paul's",
        locality="London",
        date="Sunday, March 1, 2026",
        time="10:00 AM",
        fee="£120",
        link="https://organistsonline.org/required/test",
    )
    defaults.update(overrides)
    return Gig(**defaults)


# ── _is_authorised ────────────────────────────────────────────────────────────


class TestIsAuthorised:
    def test_authorised_chat_id(self):
        update = _make_update(chat_id=7973955362)
        with patch("organist_bot.integrations.telegram_bot.settings") as mock_settings:
            mock_settings.telegram_chat_id = "7973955362"
            assert _is_authorised(update) is True

    def test_wrong_chat_id_rejected(self):
        update = _make_update(chat_id=9999999)
        with patch("organist_bot.integrations.telegram_bot.settings") as mock_settings:
            mock_settings.telegram_chat_id = "7973955362"
            assert _is_authorised(update) is False

    def test_string_vs_int_comparison(self):
        """Chat IDs from Telegram are ints; settings stores them as strings."""
        update = _make_update(chat_id=12345)
        with patch("organist_bot.integrations.telegram_bot.settings") as mock_settings:
            mock_settings.telegram_chat_id = "12345"
            assert _is_authorised(update) is True


# ── handle_message ────────────────────────────────────────────────────────────


class TestHandleMessage:
    @pytest.fixture(autouse=True)
    def patch_settings(self):
        with patch("organist_bot.integrations.telegram_bot.settings") as mock:
            mock.telegram_chat_id = "7973955362"
            mock.google_calendar_credentials_file = "fake.json"
            mock.google_calendar_id = "cal@test.com"
            yield mock

    async def test_rejects_unauthorised_chat(self):
        update = _make_update(chat_id=9999)
        await handle_message(update, MagicMock())
        update.message.reply_text.assert_not_called()

    async def test_prompts_when_no_url_in_message(self):
        update = _make_update(text="hello there")
        await handle_message(update, MagicMock())
        update.message.reply_text.assert_called_once()
        reply = update.message.reply_text.call_args[0][0]
        assert "organistsonline.org" in reply

    async def test_success_path_adds_gig_and_replies(self):
        url = "https://organistsonline.org/required/some-gig"
        update = _make_update(text=f"Please add this: {url}")
        gig = _make_gig()

        with (
            patch("organist_bot.integrations.telegram_bot.Scraper") as MockScraper,
            patch("organist_bot.integrations.telegram_bot.GoogleCalendarClient") as MockCal,
            patch(
                "organist_bot.integrations.telegram_bot.normalize_to_yyyymmdd",
                return_value="20260301",
            ),
        ):
            scraper_inst = MockScraper.return_value.__enter__.return_value
            scraper_inst.fetch.return_value = "<html></html>"
            scraper_inst.extract_basic_from_detail.return_value = {
                "header": gig.header,
                "organisation": gig.organisation,
                "locality": gig.locality,
                "date": gig.date,
                "time": gig.time,
                "fee": gig.fee,
                "link": gig.link,
            }
            scraper_inst.extract_full_details.return_value = {}

            cal_inst = MockCal.return_value
            cal_inst.has_event_on_date.return_value = False
            cal_inst.add_gig.return_value = "event_abc123"

            await handle_message(update, MagicMock())

        cal_inst.add_gig.assert_called_once()
        final_reply = update.message.reply_text.call_args_list[-1][0][0]
        assert "event_abc123" in final_reply

    async def test_replies_when_date_already_busy(self):
        url = "https://organistsonline.org/required/some-gig"
        update = _make_update(text=url)
        gig = _make_gig()

        with (
            patch("organist_bot.integrations.telegram_bot.Scraper") as MockScraper,
            patch("organist_bot.integrations.telegram_bot.GoogleCalendarClient") as MockCal,
            patch(
                "organist_bot.integrations.telegram_bot.normalize_to_yyyymmdd",
                return_value="20260301",
            ),
        ):
            scraper_inst = MockScraper.return_value.__enter__.return_value
            scraper_inst.fetch.return_value = "<html></html>"
            scraper_inst.extract_basic_from_detail.return_value = {
                "header": gig.header,
                "organisation": gig.organisation,
                "locality": gig.locality,
                "date": gig.date,
                "time": gig.time,
                "fee": gig.fee,
                "link": gig.link,
            }
            scraper_inst.extract_full_details.return_value = {}

            cal_inst = MockCal.return_value
            cal_inst.has_event_on_date.return_value = True

            await handle_message(update, MagicMock())

        cal_inst.add_gig.assert_not_called()
        reply = update.message.reply_text.call_args_list[-1][0][0]
        assert "Already have" in reply or "not adding" in reply

    async def test_handles_value_error_from_add_gig(self):
        url = "https://organistsonline.org/required/some-gig"
        update = _make_update(text=url)
        gig = _make_gig(date="unparseable date", time="??")

        with (
            patch("organist_bot.integrations.telegram_bot.Scraper") as MockScraper,
            patch("organist_bot.integrations.telegram_bot.GoogleCalendarClient") as MockCal,
            patch(
                "organist_bot.integrations.telegram_bot.normalize_to_yyyymmdd",
                return_value="20260301",
            ),
        ):
            scraper_inst = MockScraper.return_value.__enter__.return_value
            scraper_inst.fetch.return_value = "<html></html>"
            scraper_inst.extract_basic_from_detail.return_value = {
                "header": gig.header,
                "organisation": gig.organisation,
                "locality": gig.locality,
                "date": gig.date,
                "time": gig.time,
                "fee": gig.fee,
                "link": gig.link,
            }
            scraper_inst.extract_full_details.return_value = {}

            cal_inst = MockCal.return_value
            cal_inst.has_event_on_date.return_value = False
            cal_inst.add_gig.side_effect = ValueError("Cannot parse gig time")

            await handle_message(update, MagicMock())

        reply = update.message.reply_text.call_args_list[-1][0][0]
        assert "parse" in reply.lower() or "⚠️" in reply

    async def test_handles_unexpected_exception(self):
        url = "https://organistsonline.org/required/some-gig"
        update = _make_update(text=url)

        with patch("organist_bot.integrations.telegram_bot.Scraper") as MockScraper:
            MockScraper.return_value.__enter__.return_value.fetch.side_effect = RuntimeError(
                "network down"
            )
            await handle_message(update, MagicMock())

        reply = update.message.reply_text.call_args_list[-1][0][0]
        assert "❌" in reply or "error" in reply.lower()
