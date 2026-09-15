# tests/test_main.py
"""Tests for main.py — scheduler orchestration and helper functions."""

import datetime as _dt
import hashlib
import logging
from unittest.mock import MagicMock, patch

import pytest

import main as main_module
import organist_bot.application_store as application_store
from organist_bot import gig_classifier
from organist_bot.models import Gig

# ── overlapping run protection ────────────────────────────────────────────────


class TestRunLock:
    """The suite-wide _isolate_scheduler_lock_file fixture (conftest.py) points
    main._LOCK_FILE at a tmp_path for every test — real production behavior
    (the module-level default) is unaffected."""

    def test_skips_when_lock_held(self, caplog):
        """main() returns early without calling _run when the lock is already held."""
        with patch("fcntl.flock", side_effect=BlockingIOError):
            with caplog.at_level(logging.WARNING, logger="__main__"):
                main_module.main(MagicMock())

        assert any("skipping this tick" in r.message for r in caplog.records)

    def test_runs_when_lock_free(self):
        """main() calls _run when no lock is held."""
        with patch("main._run") as mock_run:
            main_module.main(MagicMock())

        mock_run.assert_called_once()


# ── scheduler crash alert dedup ────────────────────────────────────────────────


class TestRunPendingWithCrashAlert:
    """A run of consecutive tick failures only alerts once the failure streak
    reaches _CONSECUTIVE_FAILURES_BEFORE_ALERT — a single failed tick is
    usually a transient blip in the scraped site (confirmed repeatedly:
    organistsonline.org going unreachable for a few minutes at a time, e.g.
    2026-09-11 and 2026-09-14) that self-heals before it's worth paging
    about. Once the threshold is hit, later ticks in the same streak must
    not re-alert — same alert-once-until-recovered shape as
    auto_deploy.py's alert-once-per-SHA."""

    def test_failure_below_threshold_does_not_alert(self):
        with (
            patch("main.schedule") as mock_schedule,
            patch("main.alert") as mock_alert,
        ):
            mock_schedule.run_pending.side_effect = RuntimeError("boom")
            consecutive_failures = main_module._run_pending_with_crash_alert(0)

        assert consecutive_failures == 1
        assert consecutive_failures < main_module._CONSECUTIVE_FAILURES_BEFORE_ALERT
        mock_alert.send_alert.assert_not_called()

    def test_failure_reaching_threshold_alerts_once(self):
        threshold = main_module._CONSECUTIVE_FAILURES_BEFORE_ALERT
        with (
            patch("main.schedule") as mock_schedule,
            patch("main.alert") as mock_alert,
        ):
            mock_schedule.run_pending.side_effect = RuntimeError("boom")
            consecutive_failures = main_module._run_pending_with_crash_alert(threshold - 1)

        assert consecutive_failures == threshold
        mock_alert.send_alert.assert_called_once()

    def test_failure_past_threshold_does_not_alert_again(self):
        threshold = main_module._CONSECUTIVE_FAILURES_BEFORE_ALERT
        with (
            patch("main.schedule") as mock_schedule,
            patch("main.alert") as mock_alert,
        ):
            mock_schedule.run_pending.side_effect = RuntimeError("boom")
            consecutive_failures = main_module._run_pending_with_crash_alert(threshold)

        assert consecutive_failures == threshold + 1
        mock_alert.send_alert.assert_not_called()

    def test_success_resets_the_failure_count(self):
        with patch("main.schedule") as mock_schedule:
            mock_schedule.run_pending.return_value = None
            consecutive_failures = main_module._run_pending_with_crash_alert(
                main_module._CONSECUTIVE_FAILURES_BEFORE_ALERT
            )

        assert consecutive_failures == 0

    def test_failure_streak_after_recovery_alerts_again_at_threshold(self):
        threshold = main_module._CONSECUTIVE_FAILURES_BEFORE_ALERT
        with (
            patch("main.schedule") as mock_schedule,
            patch("main.alert") as mock_alert,
        ):
            mock_schedule.run_pending.side_effect = RuntimeError("boom again")
            consecutive_failures = 0
            for _ in range(threshold):
                consecutive_failures = main_module._run_pending_with_crash_alert(
                    consecutive_failures
                )

        assert consecutive_failures == threshold
        mock_alert.send_alert.assert_called_once()


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
            patch("main.GmailClient"),
            patch(
                "main.classify_gig",
                return_value=gig_classifier.Classification(
                    decision="auto_send", reason="auto_eligible"
                ),
            ),
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
            patch("main.GmailClient"),
            patch(
                "main.classify_gig",
                return_value=gig_classifier.Classification(
                    decision="auto_send", reason="auto_eligible"
                ),
            ),
        ):
            main_module.main(mock_scraper)

        mock_alert.send_alert.assert_not_called()


# ── main() orchestration ──────────────────────────────────────────────────────


class TestMain:
    """Tests for the main() scheduler function."""

    @pytest.fixture(autouse=True)
    def _classifier_and_gmail_defaults(self):
        """Every gig built in this class is a non-NEG Sunday gig expected to
        reach Phase 3 unheld — patch the classifier to always say so, and
        stub GmailClient so nothing here attempts real Gmail/Anthropic I/O.
        A test that wants different classifier behavior can still override
        with its own nested `patch("main.classify_gig", ...)`."""
        with (
            patch(
                "main.classify_gig",
                return_value=gig_classifier.Classification(
                    decision="auto_send", reason="auto_eligible"
                ),
            ),
            patch("main.GmailClient"),
        ):
            yield

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

    def test_suspended_blacklist_filter_lets_gig_through(self):
        """A blacklist suspension covering the gig's date must let a
        blacklisted email through the Phase 2 chain."""
        mock_settings = self._make_minimal_settings()
        mock_settings.enable_blacklist_filter = True

        basic = dict(
            header="Test Gig",
            organisation="Church",
            locality="London",
            date="Sunday, March 1, 2026",
            time="10:00 AM",
            fee="£120",
            link="https://organistsonline.org/required/test",
        )
        full = {"email": "blacklisted@example.com"}

        mock_scraper = MagicMock()
        mock_scraper.fetch.return_value = "<html></html>"
        mock_scraper.parse_gig_listings.return_value = [MagicMock()]
        mock_scraper.extract_basic_details.return_value = basic
        mock_scraper.extract_full_details.return_value = full

        with (
            patch("main.settings", mock_settings),
            patch("main.Notifier") as MockNotifier,
            patch("main.SMTPTransport"),
            patch("main.load_seen_gigs", return_value=set()),
            patch("main.save_seen_gigs"),
            patch("main.load_listings_hash", return_value=None),
            patch("main.save_listings_hash"),
            patch("main.set_run_id"),
            patch("main.filter_store") as mock_filter_store,
            patch("main.filter_suspension_store") as mock_fss,
        ):
            mock_filter_store.blacklist_emails.return_value = ["blacklisted@example.com"]
            mock_fss.load_active.return_value = [
                ("blacklist", _dt.date(2026, 1, 1), _dt.date(2026, 12, 31))
            ]
            mock_fss.purge_past_suspensions.return_value = 0
            notifier_inst = MockNotifier.return_value
            main_module.main(mock_scraper)

        notifier_inst.send_summary.assert_called_once()
        assert len(notifier_inst.send_summary.call_args[0][0]) == 1

    def test_purge_past_suspensions_called_each_tick(self):
        """purge_past_suspensions() must run once per tick, alongside expire_past_applied()."""
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
            patch("main.filter_suspension_store") as mock_fss,
        ):
            mock_fss.load_active.return_value = []
            mock_fss.purge_past_suspensions.return_value = 0
            main_module.main(mock_scraper)

        mock_fss.purge_past_suspensions.assert_called_once()


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

    def test_dry_run_never_calls_expire_past_applied(self):
        """A dry-run tick must never call expire_past_applied — it writes
        directly to the live applications.json regardless of dry_run, so
        calling it during a dry-run would flip past-date neg_pending/
        review_pending rows to "expired" in the REAL store while the
        Gmail-draft cleanup for those same rows stays skipped (already
        dry_run-gated), permanently orphaning the Gmail draft since no
        future real tick will ever revisit an already-expired row. See
        Finding 1 of the whole-branch review of gmail-draft-review-flow."""
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
            patch("main.GmailClient") as mock_gmail_cls,
        ):
            main_module.main(mock_scraper, dry_run=True)

        mock_store.expire_past_applied.assert_not_called()
        mock_gmail_cls.assert_not_called()
        mock_gmail_cls.return_value.delete_draft.assert_not_called()


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

    def _date_on_weekday(self, weekday: int) -> str:
        """weekday: Monday=0 ... Sunday=6 (matches filters.parse_weekday).
        Returns a date >=21 days out on the given weekday, same format
        _future_date used ("%A, %B %d, %Y") — far enough out to avoid any
        date-adjacent filter edge case, but deterministic instead of
        whatever weekday today+21 happens to land on."""
        d = _dt.date.today() + _dt.timedelta(days=21)
        while d.weekday() != weekday:
            d += _dt.timedelta(days=1)
        return d.strftime("%A, %B %d, %Y")

    def _mock_scraper_with_one_gig(
        self, fee: str, link: str = "https://e.com/abc", date: str | None = None
    ):
        scraper = MagicMock()
        scraper.fetch.return_value = "<html/>"
        scraper.parse_gig_listings.return_value = [MagicMock()]
        scraper.extract_basic_details.return_value = {
            "header": "St Mary's Sunday Service",
            "organisation": "St Mary's",
            "locality": "London",
            "date": date or self._date_on_weekday(6),  # default: Sunday
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
            patch("main.GmailClient") as mock_gmail_cls,
            patch(
                "main.classify_gig",
                return_value=gig_classifier.Classification(
                    decision="auto_send", reason="auto_eligible"
                ),
            ),
        ):
            mock_rc.get.side_effect = lambda k, d: d
            mock_gmail_cls.return_value.create_draft.return_value = "fake-draft-id"
            main_module.main(scraper)
        return mock_alert

    def test_neg_gig_is_recorded_as_pending_and_alerts_telegram(self, tmp_path, monkeypatch):
        mock_alert = self._run(
            self._settings(), self._mock_scraper_with_one_gig(fee="NEG"), tmp_path, monkeypatch
        )
        rows = application_store.list_held(status="neg_pending")
        assert len(rows) == 1
        assert rows[0]["status"] == "neg_pending"
        assert rows[0]["draft_id"] == "fake-draft-id"
        assert rows[0]["negotiable_fee"] == 120
        gig_id = rows[0]["gig_id"]
        # A single Telegram message per NEG draft now (the draft itself lives
        # in Gmail, not in a second Telegram message) — see _send_review_alert.
        assert mock_alert.send_alert.call_count == 1
        call = mock_alert.send_alert.call_args_list[0]
        assert "NEG gig" in call.args[0]
        assert "£120" in call.args[0]  # "Proposed: £120" line
        buttons = call.kwargs["reply_markup"]["inline_keyboard"][0]
        callback_data = {b["callback_data"] for b in buttons}
        assert callback_data == {f"review:accept:{gig_id}", f"review:decline:{gig_id}"}

    def test_neg_draft_creates_real_gmail_draft(self, tmp_path, monkeypatch):
        with patch("main.GmailClient") as mock_gmail_cls:
            mock_gmail_cls.return_value.create_draft.return_value = "fake-draft-id"
            with (
                patch("main.alert"),
                patch("main.settings", self._settings()),
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
                monkeypatch.setattr(application_store, "_PATH", tmp_path / "applications.json")
                mock_rc.get.side_effect = lambda k, d: d
                main_module.main(self._mock_scraper_with_one_gig(fee="NEG"))
        create_call = mock_gmail_cls.return_value.create_draft
        create_call.assert_called_once()
        assert create_call.call_args.kwargs["recipient"] == "jane@stmarys.org"
        assert "£120" in create_call.call_args.kwargs["body_html"]

    def test_neg_draft_create_failure_excludes_link_from_seen(self, tmp_path, monkeypatch):
        """When gmail_client.create_draft raises, the gig's link must be
        excluded from newly_seen (main.py's draft_failed_links set) so it's
        retried next tick instead of being permanently marked seen and lost.
        With only this one gig scraped this tick, newly_seen ends up empty
        and save_seen_gigs must not be called at all (the "if newly_seen:"
        guard)."""
        monkeypatch.setattr(application_store, "_PATH", tmp_path / "applications.json")
        with (
            patch("main.alert"),
            patch("main.settings", self._settings()),
            patch("organist_bot.notifier.application_store"),
            patch("main.load_seen_gigs", return_value=set()),
            patch("main.load_listings_hash", return_value="old_hash"),
            patch("main.save_listings_hash"),
            patch("main.save_seen_gigs") as mock_save_seen,
            patch("main.filter_store"),
            patch("main.SMTPTransport"),
            patch("main.set_run_id"),
            patch("main.runtime_config") as mock_rc,
            patch("main.GmailClient") as mock_gmail_cls,
        ):
            mock_rc.get.side_effect = lambda k, d: d
            mock_gmail_cls.return_value.create_draft.side_effect = RuntimeError("Gmail API down")
            main_module.main(
                self._mock_scraper_with_one_gig(fee="NEG", link="https://e.com/create-fails")
            )
        mock_save_seen.assert_not_called()
        assert application_store.list_held(status="neg_pending") == []

    def test_neg_draft_duplicate_url_deletes_new_orphaned_draft(self, tmp_path, monkeypatch):
        """record_held_draft returning created=False (a row for this gig's
        URL already exists in any state) must delete the just-created Gmail
        draft — the NEW draft id returned by this tick's create_draft call,
        not the pre-existing row's original draft id."""
        monkeypatch.setattr(application_store, "_PATH", tmp_path / "applications.json")
        link = "https://e.com/dup-neg"
        existing = Gig(
            header="St Mary's Sunday Service",
            organisation="St Mary's",
            locality="London",
            date="Monday, January 01, 2030",
            time="10:00 AM",
            fee="NEG",
            link=link,
            email="jane@stmarys.org",
        )
        application_store.record_held_draft(
            existing,
            status="neg_pending",
            draft_id="old-neg-draft-id",
            draft_subject="Old Subject",
            hold_reason="fee_negotiation",
            negotiable_fee=100,
        )
        with (
            patch("main.alert"),
            patch("main.settings", self._settings()),
            patch("organist_bot.notifier.application_store"),
            patch("main.load_seen_gigs", return_value=set()),
            patch("main.load_listings_hash", return_value="old_hash"),
            patch("main.save_listings_hash"),
            patch("main.save_seen_gigs"),
            patch("main.filter_store"),
            patch("main.SMTPTransport"),
            patch("main.set_run_id"),
            patch("main.runtime_config") as mock_rc,
            patch("main.GmailClient") as mock_gmail_cls,
        ):
            mock_rc.get.side_effect = lambda k, d: d
            mock_gmail_cls.return_value.create_draft.return_value = "new-neg-draft-id"
            main_module.main(self._mock_scraper_with_one_gig(fee="NEG", link=link))
        mock_gmail_cls.return_value.delete_draft.assert_called_once_with("new-neg-draft-id")
        expected_gig_id = hashlib.sha256(link.encode()).hexdigest()[:12]
        assert application_store.get_by_gig_id(expected_gig_id)["draft_id"] == "old-neg-draft-id"

    def test_record_held_draft_exception_deletes_orphaned_draft(self, tmp_path, monkeypatch):
        """If create_draft succeeds but record_held_draft itself RAISES (a
        genuine failure — disk write error, lock timeout, corrupt JSON — not
        the already-handled created=False duplicate-URL case), the
        just-created Gmail draft must be deleted before the exception
        propagates. Without this, the gig's link is excluded from
        newly_seen (draft_failed_links) so it's retried every tick, and each
        retry calls create_draft again — leaking a new orphaned draft every
        time the underlying failure persists. See Finding 3 of the
        whole-branch review of gmail-draft-review-flow."""
        monkeypatch.setattr(application_store, "_PATH", tmp_path / "applications.json")

        def _boom(*args, **kwargs):
            raise RuntimeError("disk write failed")

        monkeypatch.setattr(application_store, "record_held_draft", _boom)

        with (
            patch("main.alert"),
            patch("main.settings", self._settings()),
            patch("organist_bot.notifier.application_store"),
            patch("main.load_seen_gigs", return_value=set()),
            patch("main.load_listings_hash", return_value="old_hash"),
            patch("main.save_listings_hash"),
            patch("main.save_seen_gigs") as mock_save_seen,
            patch("main.filter_store"),
            patch("main.SMTPTransport"),
            patch("main.set_run_id"),
            patch("main.runtime_config") as mock_rc,
            patch("main.GmailClient") as mock_gmail_cls,
        ):
            mock_rc.get.side_effect = lambda k, d: d
            mock_gmail_cls.return_value.create_draft.return_value = "leaked-neg-draft-id"
            main_module.main(
                self._mock_scraper_with_one_gig(fee="NEG", link="https://e.com/record-raises")
            )

        mock_gmail_cls.return_value.delete_draft.assert_called_once_with("leaked-neg-draft-id")
        # The failure must still be tracked like any other draft failure —
        # the gig's link is excluded from newly_seen so it's retried next
        # tick instead of being silently lost.
        mock_save_seen.assert_not_called()

    def test_below_min_fee_gig_is_not_drafted(self, tmp_path, monkeypatch):
        self._run(
            self._settings(), self._mock_scraper_with_one_gig(fee="£50"), tmp_path, monkeypatch
        )
        assert application_store.list_held(status="neg_pending") == []

    def test_expenses_only_gig_is_not_drafted(self, tmp_path, monkeypatch):
        mock_alert = self._run(
            self._settings(),
            self._mock_scraper_with_one_gig(fee="Expenses only"),
            tmp_path,
            monkeypatch,
        )
        assert application_store.list_held(status="neg_pending") == []
        for c in mock_alert.send_alert.call_args_list:
            assert "NEG gig" not in c.args[0]

    def test_enable_neg_drafts_false_rejects_neg(self, tmp_path, monkeypatch):
        self._run(
            self._settings(enable_neg_drafts=False),
            self._mock_scraper_with_one_gig(fee="NEG"),
            tmp_path,
            monkeypatch,
        )
        assert application_store.list_held(status="neg_pending") == []

    def test_normal_gig_above_min_fee_still_notified(self, tmp_path, monkeypatch):
        """Regression: partition must not break the normal Phase-3 path."""
        with patch("main.Notifier") as mock_notifier_cls:
            mock_alert = self._run(
                self._settings(),
                self._mock_scraper_with_one_gig(fee="£150"),  # date defaults to Sunday
                tmp_path,
                monkeypatch,
            )
        assert application_store.list_held(status="neg_pending") == []
        for c in mock_alert.send_alert.call_args_list:
            assert "NEG gig" not in c.args[0]
        mock_notifier_cls.assert_called()

    def test_suspended_fee_filter_bypasses_neg_partition(self, tmp_path, monkeypatch):
        """A fee suspension covering the gig's date must let a below-threshold,
        non-negotiable-worded gig through as normal — not partitioned to
        neg_pending or dropped. This proves the suspension applies at the
        direct _fee_filter(gig) call site inside the NEG partition, not just
        inside GigFilterChain."""
        monkeypatch.setattr(application_store, "_PATH", tmp_path / "applications.json")
        mock_settings = (
            self._settings()
        )  # enable_neg_drafts=True, enable_fee_filter=True, min_fee=100
        scraper = self._mock_scraper_with_one_gig(fee="£50")  # below min_fee, not "NEG"-worded

        with (
            patch("main.alert"),
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
            patch("main.filter_suspension_store") as mock_fss,
            patch("main.Notifier") as mock_notifier_cls,
            patch("main.GmailClient"),
            patch(
                "main.classify_gig",
                return_value=gig_classifier.Classification(
                    decision="auto_send", reason="auto_eligible"
                ),
            ),
        ):
            mock_rc.get.side_effect = lambda k, d: d
            mock_fss.load_active.return_value = [("fee", _dt.date.min, _dt.date.max)]
            mock_fss.purge_past_suspensions.return_value = 0
            main_module.main(scraper)

        assert application_store.list_held(status="neg_pending") == []
        mock_notifier_cls.return_value.send_summary.assert_called_once()
        assert len(mock_notifier_cls.return_value.send_summary.call_args[0][0]) == 1


class TestClassifierPartition:
    """Tests for the new non-NEG hold-classifier partition — main.py's logic
    deciding auto_send_gigs vs review_gigs for gigs that already passed the
    filter chain and aren't NEG."""

    def _settings(self, **overrides):
        s = MagicMock()
        s.target_url = "https://organistsonline.org/required/"
        s.min_fee = 100
        s.negotiable_fee = 120
        s.enable_neg_drafts = True
        s.enable_fee_filter = True
        s.enable_sunday_time_filter = False
        s.enable_blacklist_filter = False
        s.enable_seen_filter = False
        s.enable_postcode_filter = False
        s.enable_calendar_filter = False
        s.enable_availability_filter = False
        s.dry_run = False
        s.email_password = "pass"
        s.email_sender = "bot@test.com"
        s.cc_email = ""
        s.applicant_name = "Alex"
        s.applicant_mobile = ""
        s.applicant_video_1 = ""
        s.applicant_video_2 = ""
        for k, v in overrides.items():
            setattr(s, k, v)
        return s

    def _date_on_weekday(self, weekday: int) -> str:
        d = _dt.date.today() + _dt.timedelta(days=21)
        while d.weekday() != weekday:
            d += _dt.timedelta(days=1)
        return d.strftime("%A, %B %d, %Y")

    def _scraper(self, fee: str, date: str, link="https://e.com/abc"):
        scraper = MagicMock()
        scraper.fetch.return_value = "<html/>"
        scraper.parse_gig_listings.return_value = [MagicMock()]
        scraper.extract_basic_details.return_value = {
            "header": "Sunday Service",
            "organisation": "St Mary's",
            "locality": "London",
            "date": date,
            "time": "10:00 AM",
            "link": link,
            "fee": fee,
        }
        scraper.extract_full_details.return_value = {
            "contact": "Jane Smith",
            "email": "jane@stmarys.org",
            "postcode": "SW1A 1AA",
        }
        return scraper

    def _run(self, mock_settings, scraper, tmp_path, monkeypatch, classify_return=None):
        monkeypatch.setattr(application_store, "_PATH", tmp_path / "applications.json")
        with (
            patch("main.alert"),
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
            patch("main.GmailClient") as mock_gmail_cls,
            patch("main.classify_gig") as mock_classify,
            patch("main.Notifier") as mock_notifier_cls,
        ):
            mock_rc.get.side_effect = lambda k, d: d
            mock_gmail_cls.return_value.create_draft.return_value = "fake-draft-id"
            # main.Notifier is a mocked class here (to assert apply_to_gig /
            # send_summary calls) — but the held-drafts blocks also call
            # notifier.draft_application(...)/draft_negotiation(...) and
            # unpack the result as `subject, body = ...`. Without an explicit
            # return_value, MagicMock() is not iterable and that unpacking
            # raises ValueError, which the surrounding `except Exception:`
            # swallows — silently skipping record_held_draft and making
            # every "was it held?" assertion below fail for the wrong reason.
            mock_notifier_cls.return_value.draft_application.return_value = (
                "Subject",
                "<p>Body</p>",
            )
            mock_notifier_cls.return_value.draft_negotiation.return_value = (
                "Subject",
                "<p>Body</p>",
            )
            if classify_return is not None:
                mock_classify.return_value = classify_return
            main_module.main(scraper)
        return mock_classify, mock_notifier_cls

    def test_monday_gig_held_without_calling_classifier(self, tmp_path, monkeypatch):
        mock_classify, mock_notifier_cls = self._run(
            self._settings(),
            self._scraper(fee="£150", date=self._date_on_weekday(0)),  # Monday
            tmp_path,
            monkeypatch,
        )
        mock_classify.assert_not_called()
        rows = application_store.list_held(status="review_pending")
        assert len(rows) == 1
        assert rows[0]["hold_reason"] == "weekday"

    def test_saturday_auto_eligible_auto_sends(self, tmp_path, monkeypatch):
        mock_classify, mock_notifier_cls = self._run(
            self._settings(),
            self._scraper(fee="£150", date=self._date_on_weekday(5)),  # Saturday
            tmp_path,
            monkeypatch,
            classify_return=gig_classifier.Classification(
                decision="auto_send", reason="auto_eligible"
            ),
        )
        mock_classify.assert_called_once()
        assert application_store.list_held(status="review_pending") == []
        mock_notifier_cls.return_value.apply_to_gig.assert_called_once()

    def test_sunday_multi_service_is_held(self, tmp_path, monkeypatch):
        mock_classify, mock_notifier_cls = self._run(
            self._settings(),
            self._scraper(fee="£150", date=self._date_on_weekday(6)),  # Sunday
            tmp_path,
            monkeypatch,
            classify_return=gig_classifier.Classification(
                decision="hold_for_review", reason="multi_service"
            ),
        )
        rows = application_store.list_held(status="review_pending")
        assert len(rows) == 1
        assert rows[0]["hold_reason"] == "multi_service"
        mock_notifier_cls.return_value.apply_to_gig.assert_not_called()

    def test_neg_gig_never_reaches_classifier(self, tmp_path, monkeypatch):
        mock_classify, mock_notifier_cls = self._run(
            self._settings(),
            self._scraper(
                fee="NEG", date=self._date_on_weekday(0)
            ),  # Monday — irrelevant, NEG short-circuits
            tmp_path,
            monkeypatch,
        )
        mock_classify.assert_not_called()

    def test_enable_fee_filter_false_neg_gig_auto_sends_without_classifier(
        self, tmp_path, monkeypatch
    ):
        """The ENABLE_FEE_FILTER=false edge case (spec §4) — the fee
        partition never runs, so a NEG gig reaches the classifier partition
        directly; is_negotiable(gig.fee) must still route it straight to
        auto-send, bypassing the classifier, unchanged from today's
        behavior in that config."""
        mock_classify, mock_notifier_cls = self._run(
            self._settings(enable_fee_filter=False),
            self._scraper(fee="NEG", date=self._date_on_weekday(0)),  # Monday
            tmp_path,
            monkeypatch,
        )
        mock_classify.assert_not_called()
        assert application_store.list_held(status="review_pending") == []
        assert application_store.list_held(status="neg_pending") == []
        mock_notifier_cls.return_value.apply_to_gig.assert_called_once()


class TestReviewDrafts:
    def _settings(self, **overrides):
        s = MagicMock()
        s.target_url = "https://organistsonline.org/required/"
        s.min_fee = 100
        s.enable_neg_drafts = True
        s.enable_fee_filter = True
        s.enable_sunday_time_filter = False
        s.enable_blacklist_filter = False
        s.enable_seen_filter = False
        s.enable_postcode_filter = False
        s.enable_calendar_filter = False
        s.enable_availability_filter = False
        s.dry_run = False
        s.email_password = "pass"
        s.email_sender = "bot@test.com"
        s.cc_email = ""
        s.applicant_name = "Alex"
        s.applicant_mobile = ""
        s.applicant_video_1 = ""
        s.applicant_video_2 = ""
        for k, v in overrides.items():
            setattr(s, k, v)
        return s

    def _date_on_weekday(self, weekday: int) -> str:
        d = _dt.date.today() + _dt.timedelta(days=21)
        while d.weekday() != weekday:
            d += _dt.timedelta(days=1)
        return d.strftime("%A, %B %d, %Y")

    def _scraper(self, date: str):
        scraper = MagicMock()
        scraper.fetch.return_value = "<html/>"
        scraper.parse_gig_listings.return_value = [MagicMock()]
        scraper.extract_basic_details.return_value = {
            "header": "Evensong",
            "organisation": "St Mary's",
            "locality": "London",
            "date": date,
            "time": "6:00 PM",
            "link": "https://e.com/evensong",
            "fee": "£100",
        }
        scraper.extract_full_details.return_value = {
            "contact": "Jane Smith",
            "email": "jane@stmarys.org",
            "postcode": "SW1A 1AA",
        }
        return scraper

    def test_review_gig_creates_gmail_draft_and_alerts(self, tmp_path, monkeypatch):
        monkeypatch.setattr(application_store, "_PATH", tmp_path / "applications.json")
        with (
            patch("main.alert") as mock_alert,
            patch("main.settings", self._settings()),
            patch("organist_bot.notifier.application_store"),
            patch("main.load_seen_gigs", return_value=set()),
            patch("main.load_listings_hash", return_value="old_hash"),
            patch("main.save_listings_hash"),
            patch("main.save_seen_gigs"),
            patch("main.filter_store"),
            patch("main.SMTPTransport"),
            patch("main.set_run_id"),
            patch("main.runtime_config") as mock_rc,
            patch("main.GmailClient") as mock_gmail_cls,
            patch(
                "main.classify_gig",
                return_value=gig_classifier.Classification(
                    decision="hold_for_review", reason="other_service_type"
                ),
            ),
        ):
            mock_rc.get.side_effect = lambda k, d: d
            mock_gmail_cls.return_value.create_draft.return_value = "fake-draft-id"
            main_module.main(self._scraper(date=self._date_on_weekday(6)))
        rows = application_store.list_held(status="review_pending")
        assert len(rows) == 1
        assert rows[0]["draft_id"] == "fake-draft-id"
        assert rows[0]["hold_reason"] == "other_service_type"
        create_call = mock_gmail_cls.return_value.create_draft
        assert create_call.call_args.kwargs["recipient"] == "jane@stmarys.org"
        alert_call = mock_alert.send_alert.call_args_list[-1]
        assert "Review needed" in alert_call.args[0]
        assert "other_service_type" in alert_call.args[0]

    def test_expiry_deletes_orphaned_draft(self, tmp_path, monkeypatch):
        import hashlib

        past = (_dt.date.today() - _dt.timedelta(days=5)).strftime("%A, %B %d, %Y")
        monkeypatch.setattr(application_store, "_PATH", tmp_path / "applications.json")
        gig = Gig(
            header="Evensong",
            organisation="St Mary's",
            locality="London",
            date=past,
            time="6:00 PM",
            fee="£100",
            link="https://e.com/past-evensong",
            email="jane@stmarys.org",
        )
        application_store.record_held_draft(
            gig,
            status="review_pending",
            draft_id="stale-draft-id",
            draft_subject="S",
            hold_reason="weekday",
        )
        empty_scraper = MagicMock()
        empty_scraper.fetch.return_value = "<html/>"
        empty_scraper.parse_gig_listings.return_value = []
        with (
            patch("main.alert"),
            patch("main.settings", self._settings()),
            patch("organist_bot.notifier.application_store"),
            patch("main.load_seen_gigs", return_value=set()),
            patch("main.load_listings_hash", return_value="different_hash"),
            patch("main.save_listings_hash"),
            patch("main.save_seen_gigs"),
            patch("main.filter_store"),
            patch("main.SMTPTransport"),
            patch("main.set_run_id"),
            patch("main.runtime_config") as mock_rc,
            patch("main.GmailClient") as mock_gmail_cls,
        ):
            mock_rc.get.side_effect = lambda k, d: d
            main_module.main(empty_scraper)
        mock_gmail_cls.return_value.delete_draft.assert_called_once_with("stale-draft-id")
        expected_gig_id = hashlib.sha256(gig.link.encode()).hexdigest()[:12]
        assert application_store.get_by_gig_id(expected_gig_id)["status"] == "expired"

    def test_review_draft_duplicate_url_deletes_new_orphaned_draft(self, tmp_path, monkeypatch):
        """record_held_draft returning created=False (a row for this gig's
        URL already exists in any state) must delete the just-created Gmail
        draft — the NEW draft id returned by this tick's create_draft call,
        not the pre-existing row's original draft id."""
        monkeypatch.setattr(application_store, "_PATH", tmp_path / "applications.json")
        existing = Gig(
            header="Evensong",
            organisation="St Mary's",
            locality="London",
            date="Monday, January 01, 2030",
            time="6:00 PM",
            fee="£100",
            link="https://e.com/evensong",
            email="jane@stmarys.org",
        )
        application_store.record_held_draft(
            existing,
            status="review_pending",
            draft_id="old-review-draft-id",
            draft_subject="Old Subject",
            hold_reason="weekday",
        )
        with (
            patch("main.alert"),
            patch("main.settings", self._settings()),
            patch("organist_bot.notifier.application_store"),
            patch("main.load_seen_gigs", return_value=set()),
            patch("main.load_listings_hash", return_value="old_hash"),
            patch("main.save_listings_hash"),
            patch("main.save_seen_gigs"),
            patch("main.filter_store"),
            patch("main.SMTPTransport"),
            patch("main.set_run_id"),
            patch("main.runtime_config") as mock_rc,
            patch("main.GmailClient") as mock_gmail_cls,
            patch(
                "main.classify_gig",
                return_value=gig_classifier.Classification(
                    decision="hold_for_review", reason="other_service_type"
                ),
            ),
        ):
            mock_rc.get.side_effect = lambda k, d: d
            mock_gmail_cls.return_value.create_draft.return_value = "new-review-draft-id"
            main_module.main(self._scraper(date=self._date_on_weekday(6)))
        mock_gmail_cls.return_value.delete_draft.assert_called_once_with("new-review-draft-id")
        expected_gig_id = hashlib.sha256(b"https://e.com/evensong").hexdigest()[:12]
        row = application_store.get_by_gig_id(expected_gig_id)
        assert row["draft_id"] == "old-review-draft-id"

    def test_record_held_draft_exception_deletes_orphaned_draft(self, tmp_path, monkeypatch):
        """Same mechanism as the NEG-drafts block's identically-named test:
        create_draft succeeds but record_held_draft itself RAISES, so the
        just-created Gmail draft must be deleted before the exception
        propagates, or a retry every tick leaks a fresh orphaned draft on
        top of the last one. See Finding 3 of the whole-branch review."""
        monkeypatch.setattr(application_store, "_PATH", tmp_path / "applications.json")

        def _boom(*args, **kwargs):
            raise RuntimeError("disk write failed")

        monkeypatch.setattr(application_store, "record_held_draft", _boom)

        with (
            patch("main.alert"),
            patch("main.settings", self._settings()),
            patch("organist_bot.notifier.application_store"),
            patch("main.load_seen_gigs", return_value=set()),
            patch("main.load_listings_hash", return_value="old_hash"),
            patch("main.save_listings_hash"),
            patch("main.save_seen_gigs") as mock_save_seen,
            patch("main.filter_store"),
            patch("main.SMTPTransport"),
            patch("main.set_run_id"),
            patch("main.runtime_config") as mock_rc,
            patch("main.GmailClient") as mock_gmail_cls,
            patch(
                "main.classify_gig",
                return_value=gig_classifier.Classification(
                    decision="hold_for_review", reason="other_service_type"
                ),
            ),
        ):
            mock_rc.get.side_effect = lambda k, d: d
            mock_gmail_cls.return_value.create_draft.return_value = "leaked-review-draft-id"
            main_module.main(self._scraper(date=self._date_on_weekday(6)))

        mock_gmail_cls.return_value.delete_draft.assert_called_once_with("leaked-review-draft-id")
        mock_save_seen.assert_not_called()

    def test_expiry_delete_404_is_silently_ignored(self, tmp_path, monkeypatch, caplog):
        """A 404 deleting the expired row's Gmail draft (already gone) must
        be treated as already-cleaned-up — no warning logged, nothing raised."""
        from organist_bot.integrations.gmail_client import GmailNotFoundError

        past = (_dt.date.today() - _dt.timedelta(days=5)).strftime("%A, %B %d, %Y")
        monkeypatch.setattr(application_store, "_PATH", tmp_path / "applications.json")
        gig = Gig(
            header="Evensong",
            organisation="St Mary's",
            locality="London",
            date=past,
            time="6:00 PM",
            fee="£100",
            link="https://e.com/past-evensong-404",
            email="jane@stmarys.org",
        )
        application_store.record_held_draft(
            gig,
            status="review_pending",
            draft_id="already-gone-draft-id",
            draft_subject="S",
            hold_reason="weekday",
        )
        empty_scraper = MagicMock()
        empty_scraper.fetch.return_value = "<html/>"
        empty_scraper.parse_gig_listings.return_value = []
        with (
            patch("main.alert"),
            patch("main.settings", self._settings()),
            patch("organist_bot.notifier.application_store"),
            patch("main.load_seen_gigs", return_value=set()),
            patch("main.load_listings_hash", return_value="different_hash"),
            patch("main.save_listings_hash"),
            patch("main.save_seen_gigs"),
            patch("main.filter_store"),
            patch("main.SMTPTransport"),
            patch("main.set_run_id"),
            patch("main.runtime_config") as mock_rc,
            patch("main.GmailClient") as mock_gmail_cls,
            caplog.at_level(logging.WARNING),
        ):
            mock_rc.get.side_effect = lambda k, d: d
            mock_gmail_cls.return_value.delete_draft.side_effect = GmailNotFoundError()
            main_module.main(empty_scraper)  # must not raise
        mock_gmail_cls.return_value.delete_draft.assert_called_once_with("already-gone-draft-id")
        assert not any("Could not delete Gmail draft" in r.message for r in caplog.records)
        expected_gig_id = hashlib.sha256(gig.link.encode()).hexdigest()[:12]
        assert application_store.get_by_gig_id(expected_gig_id)["status"] == "expired"

    def test_expiry_delete_non_404_error_logs_warning(self, tmp_path, monkeypatch, caplog):
        """A non-404 error deleting the expired row's Gmail draft must be
        logged as a warning, not silently swallowed or raised — and must not
        block the row's own expiry transition."""
        past = (_dt.date.today() - _dt.timedelta(days=5)).strftime("%A, %B %d, %Y")
        monkeypatch.setattr(application_store, "_PATH", tmp_path / "applications.json")
        gig = Gig(
            header="Evensong",
            organisation="St Mary's",
            locality="London",
            date=past,
            time="6:00 PM",
            fee="£100",
            link="https://e.com/past-evensong-error",
            email="jane@stmarys.org",
        )
        application_store.record_held_draft(
            gig,
            status="review_pending",
            draft_id="undeletable-draft-id",
            draft_subject="S",
            hold_reason="weekday",
        )
        empty_scraper = MagicMock()
        empty_scraper.fetch.return_value = "<html/>"
        empty_scraper.parse_gig_listings.return_value = []
        with (
            patch("main.alert"),
            patch("main.settings", self._settings()),
            patch("organist_bot.notifier.application_store"),
            patch("main.load_seen_gigs", return_value=set()),
            patch("main.load_listings_hash", return_value="different_hash"),
            patch("main.save_listings_hash"),
            patch("main.save_seen_gigs"),
            patch("main.filter_store"),
            patch("main.SMTPTransport"),
            patch("main.set_run_id"),
            patch("main.runtime_config") as mock_rc,
            patch("main.GmailClient") as mock_gmail_cls,
            caplog.at_level(logging.WARNING),
        ):
            mock_rc.get.side_effect = lambda k, d: d
            mock_gmail_cls.return_value.delete_draft.side_effect = RuntimeError("quota exceeded")
            main_module.main(empty_scraper)  # must not raise
        mock_gmail_cls.return_value.delete_draft.assert_called_once_with("undeletable-draft-id")
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("Could not delete Gmail draft" in r.message for r in warnings)
        expected_gig_id = hashlib.sha256(gig.link.encode()).hexdigest()[:12]
        assert application_store.get_by_gig_id(expected_gig_id)["status"] == "expired"


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
        # Log message must be a stable string for downstream log aggregation;
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


class TestGmailWriteScopeWarning:
    """Tests for warn_if_gmail_write_scope_missing()."""

    def _settings(self, credentials_file, token_file):
        s = MagicMock()
        s.gmail_credentials_file = credentials_file
        s.gmail_token_file = token_file
        return s

    def test_silent_when_credentials_unset(self, tmp_path):
        """No credentials configured — warn_if_gmail_monitoring_unconfigured
        already covers this case, so this check must no-op rather than
        double-alert."""
        with (
            patch("main.settings", self._settings("", str(tmp_path / "token.json"))),
            patch("main.alert") as mock_alert,
            patch("main.GmailClient") as mock_gmail_cls,
        ):
            main_module.warn_if_gmail_write_scope_missing()
        mock_alert.send_alert.assert_not_called()
        mock_gmail_cls.assert_not_called()

    def test_silent_when_token_file_missing(self, tmp_path):
        creds = tmp_path / "gmail_credentials.json"
        creds.write_text("{}")
        with (
            patch("main.settings", self._settings(str(creds), str(tmp_path / "missing.json"))),
            patch("main.alert") as mock_alert,
            patch("main.GmailClient") as mock_gmail_cls,
        ):
            main_module.warn_if_gmail_write_scope_missing()
        mock_alert.send_alert.assert_not_called()
        mock_gmail_cls.assert_not_called()

    def test_alerts_when_compose_access_false(self, tmp_path):
        creds = tmp_path / "gmail_credentials.json"
        creds.write_text("{}")
        token = tmp_path / "token.json"
        token.write_text("{}")
        with (
            patch("main.settings", self._settings(str(creds), str(token))),
            patch("main.alert") as mock_alert,
            patch("main.GmailClient") as mock_gmail_cls,
        ):
            mock_gmail_cls.return_value.has_compose_access.return_value = False
            main_module.warn_if_gmail_write_scope_missing()
        mock_alert.send_alert.assert_called_once()
        msg = mock_alert.send_alert.call_args.args[0]
        assert "setup_gmail_auth" in msg

    def test_silent_when_compose_access_true(self, tmp_path):
        creds = tmp_path / "gmail_credentials.json"
        creds.write_text("{}")
        token = tmp_path / "token.json"
        token.write_text("{}")
        with (
            patch("main.settings", self._settings(str(creds), str(token))),
            patch("main.alert") as mock_alert,
            patch("main.GmailClient") as mock_gmail_cls,
        ):
            mock_gmail_cls.return_value.has_compose_access.return_value = True
            main_module.warn_if_gmail_write_scope_missing()
        mock_alert.send_alert.assert_not_called()

    def test_does_not_raise_when_check_itself_errors(self, tmp_path):
        creds = tmp_path / "gmail_credentials.json"
        creds.write_text("{}")
        token = tmp_path / "token.json"
        token.write_text("{}")
        with (
            patch("main.settings", self._settings(str(creds), str(token))),
            patch("main.alert") as mock_alert,
            patch("main.GmailClient", side_effect=RuntimeError("boom")),
        ):
            main_module.warn_if_gmail_write_scope_missing()  # must not raise
        mock_alert.send_alert.assert_not_called()


class TestGigClassifierConfigWarning:
    """Tests for warn_if_gig_classifier_unconfigured()."""

    def _settings(self, anthropic_api_key):
        s = MagicMock()
        s.anthropic_api_key = anthropic_api_key
        return s

    def test_alerts_when_api_key_unset(self, caplog):
        with (
            patch("main.settings", self._settings("")),
            patch("main.alert") as mock_alert,
            caplog.at_level(logging.WARNING),
        ):
            main_module.warn_if_gig_classifier_unconfigured()
        mock_alert.send_alert.assert_called_once()
        msg = mock_alert.send_alert.call_args.args[0]
        assert "ANTHROPIC_API_KEY" in msg
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1

    def test_silent_when_api_key_set(self):
        with (
            patch("main.settings", self._settings("sk-ant-fake-key")),
            patch("main.alert") as mock_alert,
        ):
            main_module.warn_if_gig_classifier_unconfigured()
        mock_alert.send_alert.assert_not_called()
