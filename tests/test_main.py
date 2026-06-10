# tests/test_main.py
"""Tests for main.py — scheduler orchestration and helper functions."""

import datetime as _dt
import hashlib
import logging
from unittest.mock import MagicMock, patch

import main as main_module
import organist_bot.application_store as application_store

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
        assert gig_link in saved_seen, (
            f"Expected {gig_link!r} in saved seen set, got {saved_seen!r}"
        )


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


# ── Sheets drain path ─────────────────────────────────────────────────────────


class TestSheetsDrain:
    """Tests for the SheetsLogger.drain() call at the end of _run."""

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
        s.dry_run = False
        return s

    def _run_with_sheets(
        self, mock_scraper, mock_settings, mock_sheets, dry_run=False, extra_patches=None
    ):
        """Run _run() with all live-I/O patched out, returning after completion."""
        mock_store = MagicMock()
        mock_store.expire_past_applied.return_value = 0
        patches = [
            patch("main.settings", mock_settings),
            patch("main.Notifier"),
            patch("main.SMTPTransport"),
            patch("main.load_seen_gigs", return_value=set()),
            patch("main.save_seen_gigs"),
            patch("main.load_listings_hash", return_value=None),
            patch("main.save_listings_hash"),
            patch("main.set_run_id"),
            patch("main.filter_store"),
            patch("main.application_store", mock_store),
            patch("organist_bot.reply_monitor.check_replies"),
            patch("organist_bot.invoice_monitor.check_invoice_reminders_and_replies"),
        ]
        if extra_patches:
            patches.extend(extra_patches)
        from contextlib import ExitStack

        with ExitStack() as stack:
            mocks = {
                p.attribute if hasattr(p, "attribute") else str(i): stack.enter_context(p)
                for i, p in enumerate(patches)
            }
            main_module._run(mock_scraper, sheets_logger=mock_sheets, dry_run=dry_run)
        return mocks

    def test_drain_called_once_on_successful_non_dry_run(self):
        """sheets_logger.drain() is called exactly once on a successful non-dry-run _run."""
        mock_settings = self._make_minimal_settings()
        mock_scraper = MagicMock()
        mock_scraper.fetch.return_value = "<html></html>"
        mock_scraper.parse_gig_listings.return_value = []

        mock_sheets = MagicMock()
        mock_sheets.drain.return_value = 0

        self._run_with_sheets(mock_scraper, mock_settings, mock_sheets, dry_run=False)

        mock_sheets.drain.assert_called_once()

    def test_drain_not_called_in_dry_run(self):
        """sheets_logger.drain() is NOT called when dry_run=True."""
        mock_settings = self._make_minimal_settings()
        mock_scraper = MagicMock()
        mock_scraper.fetch.return_value = "<html></html>"
        mock_scraper.parse_gig_listings.return_value = []

        mock_sheets = MagicMock()

        self._run_with_sheets(mock_scraper, mock_settings, mock_sheets, dry_run=True)

        mock_sheets.drain.assert_not_called()

    def test_drain_failure_does_not_raise_and_sends_alert(self):
        """When sheets_logger.drain() raises, _run does NOT propagate the error
        and alert.send_alert is called with the failure message."""
        mock_settings = self._make_minimal_settings()
        mock_scraper = MagicMock()
        mock_scraper.fetch.return_value = "<html></html>"
        mock_scraper.parse_gig_listings.return_value = []

        mock_sheets = MagicMock()
        mock_sheets.drain.side_effect = RuntimeError("spreadsheet quota exceeded")

        mock_alert = MagicMock()
        extra = [patch("main.alert", mock_alert)]

        # Must not raise despite drain() throwing
        self._run_with_sheets(
            mock_scraper, mock_settings, mock_sheets, dry_run=False, extra_patches=extra
        )

        # Alert must have been sent with the exception message
        mock_alert.send_alert.assert_called_once()
        alert_msg = mock_alert.send_alert.call_args.args[0]
        assert "Sheets flush failed" in alert_msg
        assert "spreadsheet quota exceeded" in alert_msg


# ── NEG-fee draft & approval pipeline branch ─────────────────────────────────


class TestNegDrafts:
    """Tests for the NEG-fee draft & approval pipeline branch."""

    def _settings(self, **overrides):
        s = MagicMock()
        s.target_url = "https://organistsonline.org/required/"
        s.min_fee = 100
        s.negotiable_fee = 120
        s.enable_neg_drafts = True
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
        s.applicant_name = "Alex"
        s.applicant_mobile = "07700 900000"
        s.applicant_video_1 = ""
        s.applicant_video_2 = ""
        s.enable_fee_filter = True
        s.enable_sunday_time_filter = False
        s.enable_blacklist_filter = False
        s.enable_booked_date_filter = False
        s.enable_seen_filter = False
        s.enable_postcode_filter = False
        s.enable_calendar_filter = False
        s.enable_availability_filter = False
        s.dry_run = False
        for k, v in overrides.items():
            setattr(s, k, v)
        return s

    def _future_date(self) -> str:
        d = _dt.date.today() + _dt.timedelta(days=21)
        return d.strftime("%A, %B %d, %Y")

    def _mock_scraper_with_one_gig(self, fee: str, link: str = "https://e.com/abc"):
        scraper = MagicMock()
        scraper.fetch.return_value = "<html/>"
        scraper.parse_gig_listings.return_value = [MagicMock()]
        scraper.extract_basic_details.return_value = {
            "header": "St Mary's Sunday Service",
            "organisation": "St Mary's",
            "locality": "London",
            "date": self._future_date(),
            "time": "10:00 AM",
            "link": link,
            "fee": fee,
        }
        scraper.extract_full_details.return_value = {
            "phone": "020 1234 5678",
            "contact": "Jane Smith",
            "email": "jane@stmarys.org",
            "address": "1 High St",
            "postcode": "SW1A 1AA",
        }
        return scraper

    def _run(self, mock_settings, scraper, tmp_path, monkeypatch):
        monkeypatch.setattr(application_store, "_PATH", tmp_path / "applications.json")
        with (
            patch("main.alert") as mock_alert,
            patch("main.settings", mock_settings),
            patch("organist_bot.notifier.application_store"),
            patch("main.load_seen_gigs", return_value=set()),
            patch("main.load_listings_hash", return_value="old_hash"),
            patch("main.save_listings_hash"),
            patch("main.save_seen_gigs"),
            patch("main.filter_store"),
            patch("main.SMTPTransport"),
            patch("main.set_run_id"),
            patch("main.runtime_config") as mock_rc,
        ):
            mock_rc.get.side_effect = lambda k, d: d
            main_module.main(scraper)
        return mock_alert

    def test_neg_gig_is_recorded_as_pending_and_alerts_telegram(self, tmp_path, monkeypatch):
        mock_alert = self._run(
            self._settings(), self._mock_scraper_with_one_gig(fee="NEG"), tmp_path, monkeypatch
        )
        rows = application_store.list_neg_pending()
        assert len(rows) == 1
        assert rows[0]["status"] == "neg_pending"
        assert "£120" in rows[0]["draft_body"]
        neg_calls = [
            c for c in mock_alert.send_alert.call_args_list if "NEG draft pending" in c.args[0]
        ]
        assert len(neg_calls) == 1
        assert rows[0]["gig_id"] in neg_calls[0].args[0]

    def test_below_min_fee_gig_is_not_drafted(self, tmp_path, monkeypatch):
        self._run(
            self._settings(), self._mock_scraper_with_one_gig(fee="£50"), tmp_path, monkeypatch
        )
        assert application_store.list_neg_pending() == []

    def test_expenses_only_gig_is_not_drafted(self, tmp_path, monkeypatch):
        mock_alert = self._run(
            self._settings(),
            self._mock_scraper_with_one_gig(fee="Expenses only"),
            tmp_path,
            monkeypatch,
        )
        assert application_store.list_neg_pending() == []
        for c in mock_alert.send_alert.call_args_list:
            assert "NEG draft pending" not in c.args[0]

    def test_enable_neg_drafts_false_rejects_neg(self, tmp_path, monkeypatch):
        self._run(
            self._settings(enable_neg_drafts=False),
            self._mock_scraper_with_one_gig(fee="NEG"),
            tmp_path,
            monkeypatch,
        )
        assert application_store.list_neg_pending() == []

    def test_normal_gig_above_min_fee_still_notified(self, tmp_path, monkeypatch):
        """Regression: partition must not break the normal Phase-3 path."""
        with patch("main.Notifier") as mock_notifier_cls:
            mock_alert = self._run(
                self._settings(),
                self._mock_scraper_with_one_gig(fee="£150"),
                tmp_path,
                monkeypatch,
            )
        assert application_store.list_neg_pending() == []
        # No NEG-draft alert should have been sent
        for c in mock_alert.send_alert.call_args_list:
            assert "NEG draft pending" not in c.args[0]
        # Notifier must have been instantiated (Phase 3 ran)
        mock_notifier_cls.assert_called()


# ── Gmail monitoring config warning ──────────────────────────────────────────


class TestGmailMonitoringConfigWarning:
    """Tests for warn_if_gmail_monitoring_unconfigured() — the startup guard
    that makes a silently-disabled Gmail reply/payment monitor loud."""

    def _settings(self, credentials_file, token_file):
        s = MagicMock()
        s.gmail_credentials_file = credentials_file
        s.gmail_token_file = token_file
        return s

    def test_alerts_when_credentials_unset(self, caplog):
        """No GMAIL_CREDENTIALS_FILE → one alert naming the missing env var."""
        with (
            patch("main.settings", self._settings("", "data/gmail_token.json")),
            patch("main.alert") as mock_alert,
            caplog.at_level(logging.WARNING),
        ):
            main_module.warn_if_gmail_monitoring_unconfigured()

        mock_alert.send_alert.assert_called_once()
        msg = mock_alert.send_alert.call_args.args[0]
        assert "GMAIL_CREDENTIALS_FILE" in msg
        assert "disabled" in msg.lower()
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1

    def test_alerts_when_token_file_missing(self, tmp_path, caplog):
        """Credentials set but no minted token → one alert naming the setup script."""
        creds = tmp_path / "gmail_credentials.json"
        creds.write_text("{}")
        missing_token = tmp_path / "gmail_token.json"

        with (
            patch("main.settings", self._settings(str(creds), str(missing_token))),
            patch("main.alert") as mock_alert,
            caplog.at_level(logging.WARNING),
        ):
            main_module.warn_if_gmail_monitoring_unconfigured()

        mock_alert.send_alert.assert_called_once()
        msg = mock_alert.send_alert.call_args.args[0]
        assert "setup_gmail_auth" in msg
        assert "disabled" in msg.lower()
        # Log message must be a stable string (Sheets dashboard groups by message);
        # the variable token path belongs in `extra`, not the message.
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert str(missing_token) not in warnings[0].message
        assert warnings[0].token_file == str(missing_token)

    def test_silent_when_fully_configured(self, tmp_path):
        """Credentials and token both present → no alert, no warning."""
        creds = tmp_path / "gmail_credentials.json"
        creds.write_text("{}")
        token = tmp_path / "gmail_token.json"
        token.write_text("{}")

        with (
            patch("main.settings", self._settings(str(creds), str(token))),
            patch("main.alert") as mock_alert,
        ):
            main_module.warn_if_gmail_monitoring_unconfigured()

        mock_alert.send_alert.assert_not_called()
