# tests/test_main.py
"""Tests for main.py — scheduler orchestration and helper functions."""

import hashlib
import logging
from unittest.mock import MagicMock, patch

import main as main_module
from main import _send_telegram_alert

# ── _send_telegram_alert ──────────────────────────────────────────────────────


class TestSendTelegramAlert:
    def test_sends_message_when_token_and_chat_id_set(self):
        mock_settings = MagicMock()
        mock_settings.telegram_bot_token = "fake_token"
        mock_settings.telegram_chat_id = "12345"

        with patch("main.settings", mock_settings), patch("main._requests.post") as mock_post:
            _send_telegram_alert("Test crash message")
            mock_post.assert_called_once()
            call_kwargs = mock_post.call_args
            assert "fake_token" in call_kwargs[0][0]
            assert call_kwargs[1]["json"]["text"] == "Test crash message"
            assert call_kwargs[1]["json"]["chat_id"] == "12345"

    def test_does_nothing_when_token_missing(self):
        mock_settings = MagicMock()
        mock_settings.telegram_bot_token = ""
        mock_settings.telegram_chat_id = "12345"

        with patch("main.settings", mock_settings), patch("main._requests.post") as mock_post:
            _send_telegram_alert("crash")
            mock_post.assert_not_called()

    def test_does_nothing_when_chat_id_missing(self):
        mock_settings = MagicMock()
        mock_settings.telegram_bot_token = "token"
        mock_settings.telegram_chat_id = ""

        with patch("main.settings", mock_settings), patch("main._requests.post") as mock_post:
            _send_telegram_alert("crash")
            mock_post.assert_not_called()

    def test_silently_swallows_network_error(self):
        """Alert failures must never propagate — the scheduler depends on this."""
        mock_settings = MagicMock()
        mock_settings.telegram_bot_token = "token"
        mock_settings.telegram_chat_id = "12345"

        with (
            patch("main.settings", mock_settings),
            patch("main._requests.post", side_effect=ConnectionError("unreachable")),
        ):
            # Should not raise
            _send_telegram_alert("crash")

    def test_logs_info_on_successful_send(self, caplog):
        """A successful Telegram POST must emit an INFO 'Telegram alert sent' record."""

        mock_settings = MagicMock()
        mock_settings.telegram_bot_token = "token"
        mock_settings.telegram_chat_id = "12345"

        # When main.py is imported as a module its __name__ is "main", not "__main__".
        with (
            patch("main.settings", mock_settings),
            patch("main._requests.post"),
            caplog.at_level(logging.INFO, logger="main"),
        ):
            _send_telegram_alert("crash message")

        record = next(
            (r for r in caplog.records if r.message == "Telegram alert sent"),
            None,
        )
        assert record is not None, "Expected 'Telegram alert sent' INFO log record"
        assert isinstance(record.elapsed_ms, int)
        assert record.elapsed_ms >= 0

    def test_logs_warning_on_network_error(self, caplog):
        """A failing Telegram POST must emit a WARNING 'Telegram alert failed' record."""

        mock_settings = MagicMock()
        mock_settings.telegram_bot_token = "token"
        mock_settings.telegram_chat_id = "12345"

        with (
            patch("main.settings", mock_settings),
            patch("main._requests.post", side_effect=ConnectionError("unreachable")),
            caplog.at_level(logging.WARNING, logger="main"),
        ):
            _send_telegram_alert("crash message")

        record = next(
            (r for r in caplog.records if r.message == "Telegram alert failed"),
            None,
        )
        assert record is not None, "Expected 'Telegram alert failed' WARNING log record"
        assert isinstance(record.elapsed_ms, int)
        assert record.elapsed_ms >= 0


# ── main() orchestration ──────────────────────────────────────────────────────


class TestMain:
    """Tests for the main() scheduler function."""

    def _make_minimal_settings(self):
        s = MagicMock()
        s.target_url = "https://organistsonline.org/required/"
        s.min_fee = 100
        s.poll_minutes = 2
        s.booked_dates = []

        s.home_postcode = ""
        s.google_maps_api_key = ""
        s.google_calendar_id = ""
        s.google_calendar_credentials_file = ""
        s.google_sheets_id = ""
        s.google_sheets_credentials_file = ""
        s.telegram_bot_token = "token"
        s.telegram_chat_id = "12345"
        s.email_password = "pass"
        s.email_sender = "bot@test.com"
        s.cc_email = ""
        s.enable_fee_filter = False
        s.enable_sunday_time_filter = False
        s.enable_blacklist_filter = False
        s.enable_booked_date_filter = False
        s.enable_seen_filter = False
        s.enable_postcode_filter = False
        s.enable_calendar_filter = False
        return s

    def test_main_runs_with_no_gigs(self):
        """main() should complete without error when the listing page is empty."""
        mock_settings = self._make_minimal_settings()
        mock_scraper = MagicMock()
        mock_scraper.fetch.return_value = "<html></html>"
        mock_scraper.parse_gig_listings.return_value = []

        with (
            patch("main.settings", mock_settings),
            patch("main.Notifier"),
            patch("main.SMTPTransport"),
            patch("main.load_seen_gigs", return_value=set()),
            patch("main.save_seen_gigs"),
            patch("main.load_listings_hash", return_value=None),
            patch("main.save_listings_hash"),
            patch("main.set_run_id"),
        ):
            main_module.main(mock_scraper)  # should not raise

    def test_per_gig_error_is_isolated(self):
        """A scraping error on one gig must not abort the rest of the run."""
        mock_settings = self._make_minimal_settings()

        good_basic = dict(
            header="Good Gig",
            organisation="Church",
            locality="London",
            date="Sunday, March 1, 2026",
            time="10:00 AM",
            fee="£120",
            link="https://organistsonline.org/required/good",
        )

        mock_scraper = MagicMock()
        mock_scraper.fetch.return_value = "<html></html>"
        mock_scraper.parse_gig_listings.return_value = [MagicMock(), MagicMock()]
        mock_scraper.extract_basic_details.side_effect = [
            RuntimeError("bad page"),  # first gig fails
            good_basic,  # second gig succeeds
        ]
        mock_scraper.extract_full_details.return_value = {}

        with (
            patch("main.settings", mock_settings),
            patch("main.Notifier"),
            patch("main.SMTPTransport"),
            patch("main.load_seen_gigs", return_value=set()),
            patch("main.save_seen_gigs"),
            patch("main.load_listings_hash", return_value=None),
            patch("main.save_listings_hash"),
            patch("main.set_run_id"),
        ):
            main_module.main(mock_scraper)  # should not raise

            # extract_basic_details was called for both elements
            assert mock_scraper.extract_basic_details.call_count == 2

    def test_all_filters_disabled_passes_all_gigs(self):
        """With all filters off, every scraped gig should reach the notify phase."""
        mock_settings = self._make_minimal_settings()

        basic = dict(
            header="Test Gig",
            organisation="Church",
            locality="London",
            date="Sunday, March 1, 2026",
            time="10:00 AM",
            fee="£120",
            link="https://organistsonline.org/required/test",
        )

        mock_scraper = MagicMock()
        mock_scraper.fetch.return_value = "<html></html>"
        mock_scraper.parse_gig_listings.return_value = [MagicMock()]
        mock_scraper.extract_basic_details.return_value = basic
        mock_scraper.extract_full_details.return_value = {}

        with (
            patch("main.settings", mock_settings),
            patch("main.Notifier") as MockNotifier,
            patch("main.SMTPTransport"),
            patch("main.load_seen_gigs", return_value=set()),
            patch("main.save_seen_gigs"),
            patch("main.load_listings_hash", return_value=None),
            patch("main.save_listings_hash"),
            patch("main.set_run_id"),
        ):
            notifier_inst = MockNotifier.return_value
            main_module.main(mock_scraper)

            notifier_inst.send_summary.assert_called_once()
            assert len(notifier_inst.send_summary.call_args[0][0]) == 1

    def test_save_seen_gigs_merges_with_previously_seen(self):
        """Previously-seen links must be preserved when new valid gigs are saved."""
        mock_settings = self._make_minimal_settings()
        mock_settings.enable_seen_filter = True

        basic = dict(
            header="New Gig",
            organisation="Church",
            locality="London",
            date="Sunday, March 1, 2026",
            time="10:00 AM",
            fee="£120",
            link="https://organistsonline.org/required/new",
        )
        mock_scraper = MagicMock()
        mock_scraper.fetch.return_value = "<html></html>"
        mock_scraper.parse_gig_listings.return_value = [MagicMock()]
        mock_scraper.extract_basic_details.return_value = basic
        mock_scraper.extract_full_details.return_value = {}

        with (
            patch("main.settings", mock_settings),
            patch("main.Notifier"),
            patch("main.SMTPTransport"),
            patch(
                "main.load_seen_gigs",
                return_value={"https://organistsonline.org/required/old"},
            ),
            patch("main.save_seen_gigs") as mock_save,
            patch("main.load_listings_hash", return_value=None),
            patch("main.save_listings_hash"),
            patch("main.set_run_id"),
        ):
            main_module.main(mock_scraper)

        saved = mock_save.call_args[1]["seen"]
        assert "https://organistsonline.org/required/old" in saved  # previously seen — preserved
        assert "https://organistsonline.org/required/new" in saved  # newly emailed — added

    def test_seen_gig_skips_detail_page_fetch(self):
        """A gig whose link is already in seen_gigs must not trigger a detail-page fetch."""
        mock_settings = self._make_minimal_settings()
        mock_settings.enable_seen_filter = True

        seen_link = "https://organistsonline.org/required/already-seen"
        basic = dict(
            header="Old Gig",
            organisation="Church",
            locality="London",
            date="Sunday, March 1, 2026",
            time="10:00 AM",
            fee="£120",
            link=seen_link,
        )
        mock_scraper = MagicMock()
        mock_scraper.fetch.return_value = "<html></html>"
        mock_scraper.parse_gig_listings.return_value = [MagicMock()]
        mock_scraper.extract_basic_details.return_value = basic
        mock_scraper.extract_full_details.return_value = {}

        with (
            patch("main.settings", mock_settings),
            patch("main.Notifier"),
            patch("main.SMTPTransport"),
            patch("main.load_seen_gigs", return_value={seen_link}),
            patch("main.save_seen_gigs"),
            patch("main.load_listings_hash", return_value=None),
            patch("main.save_listings_hash"),
            patch("main.set_run_id"),
        ):
            main_module.main(mock_scraper)

        # SeenFilter in pre-filter → rejected before detail-page fetch
        mock_scraper.extract_full_details.assert_not_called()

    def test_calendar_filter_in_prefilter_skips_detail_page_fetch(self):
        """A gig on a calendar-booked date must not trigger a detail-page fetch."""
        mock_settings = self._make_minimal_settings()
        mock_settings.enable_calendar_filter = True
        mock_settings.google_calendar_id = "cal@test.com"
        mock_settings.google_calendar_credentials_file = "fake_creds.json"

        basic = dict(
            header="Booked Gig",
            organisation="Church",
            locality="London",
            date="Sunday, March 1, 2026",
            time="10:00 AM",
            fee="£120",
            link="https://organistsonline.org/required/booked",
        )
        mock_scraper = MagicMock()
        mock_scraper.fetch.return_value = "<html></html>"
        mock_scraper.parse_gig_listings.return_value = [MagicMock()]
        mock_scraper.extract_basic_details.return_value = basic
        mock_scraper.extract_full_details.return_value = {}

        mock_cal_client = MagicMock()
        mock_cal_client.has_event_on_date.return_value = True  # date is booked

        with (
            patch("main.settings", mock_settings),
            patch("main.Notifier") as MockNotifier,
            patch("main.SMTPTransport"),
            patch("main.load_seen_gigs", return_value=set()),
            patch("main.save_seen_gigs"),
            patch("main.load_listings_hash", return_value=None),
            patch("main.save_listings_hash"),
            patch("main.set_run_id"),
            patch("main.GoogleCalendarClient", return_value=mock_cal_client),
        ):
            notifier_inst = MockNotifier.return_value
            main_module.main(mock_scraper)

        # CalendarFilter in pre-filter → rejected before detail-page fetch
        mock_scraper.extract_full_details.assert_not_called()
        notifier_inst.send_summary.assert_not_called()


class TestHashChangeDetection:
    def test_skips_pipeline_when_hash_unchanged(self, caplog):
        """When the gig-elements hash matches the stored hash, main() returns
        early without entering the per-gig loop or notifying."""
        # Hash is derived from serialised gig elements, not the full HTML.
        gig_elements = []  # parse_gig_listings returns empty list
        stored_hash = hashlib.sha256("".join(str(el) for el in gig_elements).encode()).hexdigest()

        mock_scraper = MagicMock()
        mock_scraper.fetch.return_value = "<html>unchanged</html>"
        mock_scraper.parse_gig_listings.return_value = gig_elements

        with (
            patch("main.load_listings_hash", return_value=stored_hash),
            patch("main.save_listings_hash") as mock_save,
            patch("main.set_run_id"),
            patch("main.settings"),
            patch("main.filter_store"),
            patch("main.GoogleCalendarClient"),
            caplog.at_level(logging.INFO),
        ):
            main_module.main(mock_scraper)

        mock_scraper.extract_basic_details.assert_not_called()
        mock_save.assert_not_called()
        assert any("unchanged" in r.message.lower() for r in caplog.records)

    def test_runs_pipeline_when_hash_changes(self):
        """When stored hash differs from the current gig-elements hash, the
        pipeline proceeds and the new hash is saved."""
        old_hash = "stale_hash_value"
        # Hash of serialised empty gig list
        new_hash = hashlib.sha256(b"").hexdigest()

        mock_scraper = MagicMock()
        mock_scraper.fetch.return_value = "<html>new content</html>"
        mock_scraper.parse_gig_listings.return_value = []

        with (
            patch("main.load_listings_hash", return_value=old_hash),
            patch("main.save_listings_hash") as mock_save,
            patch("main.set_run_id"),
            patch("main.load_seen_gigs", return_value=set()),
            patch("main.save_seen_gigs"),
            patch("main.filter_store"),
            patch("main.settings"),
            patch("main.GoogleCalendarClient"),
        ):
            main_module.main(mock_scraper)

        mock_scraper.parse_gig_listings.assert_called_once()
        mock_save.assert_called_once_with(new_hash)

    def test_runs_pipeline_when_no_stored_hash(self):
        """First run (no hash file yet) proceeds normally and saves the hash."""
        new_hash = hashlib.sha256(b"").hexdigest()

        mock_scraper = MagicMock()
        mock_scraper.fetch.return_value = "<html>first run</html>"
        mock_scraper.parse_gig_listings.return_value = []

        with (
            patch("main.load_listings_hash", return_value=None),
            patch("main.save_listings_hash") as mock_save,
            patch("main.set_run_id"),
            patch("main.load_seen_gigs", return_value=set()),
            patch("main.save_seen_gigs"),
            patch("main.filter_store"),
            patch("main.settings"),
            patch("main.GoogleCalendarClient"),
        ):
            main_module.main(mock_scraper)

        mock_scraper.parse_gig_listings.assert_called_once()
        mock_save.assert_called_once_with(new_hash)
