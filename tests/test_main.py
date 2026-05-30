# tests/test_main.py
"""Tests for main.py — scheduler orchestration and helper functions."""

import hashlib
import logging
from unittest.mock import MagicMock, patch

import main as main_module

# ── overlapping run protection ────────────────────────────────────────────────


class TestRunLock:
    def test_skips_when_lock_held(self, caplog):
        """main() returns early without calling _run when the lock is already held."""
        with patch("fcntl.flock", side_effect=BlockingIOError):
            with caplog.at_level(logging.WARNING, logger="__main__"):
                main_module.main(MagicMock(), None)

        assert any("skipping this tick" in r.message for r in caplog.records)

    def test_runs_when_lock_free(self):
        """main() calls _run when no lock is held."""
        with patch("main._run") as mock_run:
            main_module.main(MagicMock(), None)

        mock_run.assert_called_once()


# ── parse error alert ─────────────────────────────────────────────────────────


class TestParseErrorAlert:
    """Tests for the gig parse error rate alert in main()."""

    def _make_minimal_settings(self):
        s = MagicMock()
        s.target_url = "https://organistsonline.org/required/"
        s.min_fee = 100
        s.poll_minutes = 2
        s.enable_seen_filter = False
        s.enable_fee_filter = False
        s.enable_sunday_time_filter = False
        s.enable_blacklist_filter = False
        s.enable_booked_date_filter = False
        s.enable_postcode_filter = False
        s.enable_calendar_filter = False
        s.enable_availability_filter = False
        s.email_password = "pass"
        return s

    def test_alert_sent_when_gig_errors_ge_2(self):
        """send_alert is called when gig_errors >= 2 after scraping."""
        mock_settings = self._make_minimal_settings()
        mock_scraper = MagicMock()
        mock_scraper.fetch.return_value = "<html/>"
        mock_scraper.parse_gig_listings.return_value = [MagicMock()] * 3

        call_count = 0

        def fake_extract(el):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise Exception("parse failure")
            return {
                "header": "Test",
                "organisation": "Church",
                "locality": "London",
                "date": "Sunday 1st June 2025",
                "time": "10:00 AM",
                "link": "https://example.com/1",
                "fee": "£100",
            }

        mock_scraper.extract_basic_details.side_effect = fake_extract
        mock_scraper.extract_full_details.return_value = {}

        with (
            patch("main.alert") as mock_alert,
            patch("main.settings", mock_settings),
            patch("main.load_seen_gigs", return_value=set()),
            patch("main.load_listings_hash", return_value="old_hash"),
            patch("main.save_listings_hash"),
            patch("main.save_seen_gigs"),
            patch("main.filter_store"),
            patch("main.Notifier"),
            patch("main.SMTPTransport"),
            patch("main.set_run_id"),
        ):
            main_module.main(mock_scraper)

        mock_alert.send_alert.assert_called_once()
        alert_msg = mock_alert.send_alert.call_args.args[0]
        assert "⚠️" in alert_msg
        assert "2" in alert_msg

    def test_no_alert_when_gig_errors_lt_2(self):
        """send_alert is NOT called when gig_errors < 2."""
        mock_settings = self._make_minimal_settings()
        mock_scraper = MagicMock()
        mock_scraper.fetch.return_value = "<html/>"
        mock_scraper.parse_gig_listings.return_value = [MagicMock(), MagicMock()]

        call_count = 0

        def fake_extract(el):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("one error")
            return {
                "header": "Test",
                "organisation": "Church",
                "locality": "London",
                "date": "Sunday 1st June 2025",
                "time": "10:00 AM",
                "link": "https://example.com/1",
                "fee": "£100",
            }

        mock_scraper.extract_basic_details.side_effect = fake_extract
        mock_scraper.extract_full_details.return_value = {}

        with (
            patch("main.alert") as mock_alert,
            patch("main.settings", mock_settings),
            patch("main.load_seen_gigs", return_value=set()),
            patch("main.load_listings_hash", return_value="old_hash"),
            patch("main.save_listings_hash"),
            patch("main.save_seen_gigs"),
            patch("main.filter_store"),
            patch("main.Notifier"),
            patch("main.SMTPTransport"),
            patch("main.set_run_id"),
        ):
            main_module.main(mock_scraper)

        mock_alert.send_alert.assert_not_called()


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
        s.enable_availability_filter = False
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
        mock_cal_client.get_events_on_date.return_value = [{"id": "b1", "summary": "Unavailable"}]

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


# ── expire_past_applied called each tick ──────────────────────────────────────


class TestPhase2RejectedGigsSeen:
    """Phase-2-only rejections (BlacklistFilter) must be recorded as seen so they
    are not re-fetched on the next listings change."""

    def _make_minimal_settings(self):
        s = MagicMock()
        s.target_url = "https://organistsonline.org/required/"
        s.min_fee = 100
        s.poll_minutes = 2
        # All pre-filter (Phase-1) filters disabled so the gig reaches Phase 2.
        s.enable_seen_filter = False
        s.enable_fee_filter = False
        s.enable_sunday_time_filter = False
        s.enable_calendar_filter = False
        s.enable_availability_filter = False
        s.enable_booked_date_filter = False
        # BlacklistFilter enabled — this is the Phase-2-only rejection we're testing.
        s.enable_blacklist_filter = True
        s.enable_postcode_filter = False
        s.email_password = "pass"
        return s

    def test_blacklist_rejected_gig_is_recorded_as_seen(self):
        """A gig rejected by BlacklistFilter at Phase 2 must have its URL saved
        to the seen set so it is not re-fetched on every subsequent listings change."""
        blacklisted_email = "organist@blacklisted.org"
        gig_link = "https://organistsonline.org/required/blacklisted-gig"

        mock_settings = self._make_minimal_settings()
        mock_scraper = MagicMock()
        mock_scraper.fetch.return_value = "<html></html>"
        mock_scraper.parse_gig_listings.return_value = [MagicMock()]
        mock_scraper.extract_basic_details.return_value = dict(
            header="Blacklisted Org Gig",
            organisation="Blacklisted Church",
            locality="London",
            date="Sunday, March 1, 2026",
            time="10:00 AM",
            fee="£120",
            link=gig_link,
        )
        # Detail page yields the blacklisted contact email.
        mock_scraper.extract_full_details.return_value = {"email": blacklisted_email}

        with (
            patch("main.settings", mock_settings),
            patch("main.Notifier"),
            patch("main.SMTPTransport"),
            patch("main.load_seen_gigs", return_value=set()),
            patch("main.save_seen_gigs") as mock_save,
            patch("main.load_listings_hash", return_value=None),
            patch("main.save_listings_hash"),
            patch("main.set_run_id"),
            patch("main.filter_store") as mock_filter_store,
            patch("main.application_store") as mock_store,
            patch("organist_bot.reply_monitor.check_replies"),
            patch("organist_bot.invoice_monitor.check_invoice_reminders_and_replies"),
        ):
            # Inject the blacklisted email so BlacklistFilter rejects the gig.
            mock_filter_store.blacklist_emails.return_value = [blacklisted_email]
            mock_filter_store.unavailable_periods.return_value = []
            mock_filter_store.available_only_periods.return_value = []
            mock_store.expire_past_applied.return_value = 0
            main_module._run(mock_scraper, dry_run=False)

        # The gig was rejected at Phase 2 — but its URL must still be saved.
        mock_save.assert_called_once()
        saved_seen = mock_save.call_args[1]["seen"]
        assert (
            gig_link in saved_seen
        ), f"Expected {gig_link!r} in saved seen set, got {saved_seen!r}"


class TestExpirePastApplied:
    def _make_minimal_settings(self):
        s = MagicMock()
        s.target_url = "https://organistsonline.org/required/"
        s.min_fee = 100
        s.poll_minutes = 2
        s.enable_seen_filter = False
        s.enable_fee_filter = False
        s.enable_sunday_time_filter = False
        s.enable_blacklist_filter = False
        s.enable_booked_date_filter = False
        s.enable_postcode_filter = False
        s.enable_calendar_filter = False
        s.enable_availability_filter = False
        s.email_password = "pass"
        return s

    def test_expire_past_applied_called_each_tick(self):
        """expire_past_applied must be called once per _run, even when no gigs are found."""
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
            patch("main.application_store") as mock_store,
        ):
            mock_store.expire_past_applied.return_value = 0
            main_module.main(mock_scraper)

        mock_store.expire_past_applied.assert_called_once()
