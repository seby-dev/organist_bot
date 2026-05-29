"""Tests for dry-run mode in main.py."""

import logging
from unittest.mock import MagicMock, patch

import main as main_module


def _make_settings(**overrides):
    s = MagicMock()
    s.target_url = "https://organistsonline.org/required/"
    s.min_fee = 100
    s.poll_minutes = 2
    s.enable_seen_filter = False
    s.enable_fee_filter = False
    s.enable_sunday_time_filter = False
    s.enable_blacklist_filter = False
    s.enable_postcode_filter = False
    s.enable_calendar_filter = False
    s.enable_availability_filter = False
    s.email_password = "pass"
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _make_scraper_with_gig(link="https://organistsonline.org/required/gig1"):
    scraper = MagicMock()
    scraper.fetch.return_value = "<html></html>"
    scraper.parse_gig_listings.return_value = [MagicMock()]
    scraper.extract_basic_details.return_value = {
        "header": "Sunday Service",
        "organisation": "St Mary's",
        "locality": "London",
        "date": "Sunday, March 1, 2026",
        "time": "10:00 AM",
        "fee": "£120",
        "link": link,
    }
    scraper.extract_full_details.return_value = {}
    return scraper


class TestDryRunNoWrites:
    def test_dry_run_does_not_write_listings_hash(self):
        """save_listings_hash must NOT be called in dry-run mode."""
        scraper = _make_scraper_with_gig()
        with (
            patch("main.settings", _make_settings()),
            patch("main.load_seen_gigs", return_value=set()),
            patch("main.load_listings_hash", return_value="old"),
            patch("main.save_listings_hash") as mock_save_hash,
            patch("main.save_seen_gigs"),
            patch("main.set_run_id"),
            patch("main.filter_store"),
            patch("main.Notifier"),
            patch("main.SMTPTransport"),
        ):
            main_module.main(scraper, dry_run=True)

        mock_save_hash.assert_not_called()

    def test_dry_run_does_not_write_seen_gigs(self):
        """save_seen_gigs must NOT be called in dry-run mode even when valid gigs exist."""
        scraper = _make_scraper_with_gig()
        with (
            patch("main.settings", _make_settings()),
            patch("main.load_seen_gigs", return_value=set()),
            patch("main.load_listings_hash", return_value="old"),
            patch("main.save_listings_hash"),
            patch("main.save_seen_gigs") as mock_save_seen,
            patch("main.set_run_id"),
            patch("main.filter_store"),
            patch("main.Notifier"),
            patch("main.SMTPTransport"),
        ):
            main_module.main(scraper, dry_run=True)

        mock_save_seen.assert_not_called()

    def test_dry_run_does_not_send_email(self):
        """Notifier must NOT be instantiated or called in dry-run mode."""
        scraper = _make_scraper_with_gig()
        with (
            patch("main.settings", _make_settings()),
            patch("main.load_seen_gigs", return_value=set()),
            patch("main.load_listings_hash", return_value="old"),
            patch("main.save_listings_hash"),
            patch("main.save_seen_gigs"),
            patch("main.set_run_id"),
            patch("main.filter_store"),
            patch("main.Notifier") as MockNotifier,
            patch("main.SMTPTransport"),
        ):
            main_module.main(scraper, dry_run=True)

        MockNotifier.assert_not_called()

    def test_dry_run_does_not_drain_sheets(self):
        """sheets_logger.drain must NOT be called in dry-run mode."""
        scraper = _make_scraper_with_gig()
        mock_sheets = MagicMock()
        with (
            patch("main.settings", _make_settings()),
            patch("main.load_seen_gigs", return_value=set()),
            patch("main.load_listings_hash", return_value="old"),
            patch("main.save_listings_hash"),
            patch("main.save_seen_gigs"),
            patch("main.set_run_id"),
            patch("main.filter_store"),
            patch("main.Notifier"),
            patch("main.SMTPTransport"),
        ):
            main_module.main(scraper, sheets_logger=mock_sheets, dry_run=True)

        mock_sheets.drain.assert_not_called()


class TestDryRunLogs:
    def test_dry_run_logs_banner(self, caplog):
        """Dry-run mode must log a banner at start."""
        scraper = _make_scraper_with_gig()
        with (
            patch("main.settings", _make_settings()),
            patch("main.load_seen_gigs", return_value=set()),
            patch("main.load_listings_hash", return_value="old"),
            patch("main.save_listings_hash"),
            patch("main.save_seen_gigs"),
            patch("main.set_run_id"),
            patch("main.filter_store"),
            patch("main.Notifier"),
            patch("main.SMTPTransport"),
            caplog.at_level(logging.INFO),
        ):
            main_module.main(scraper, dry_run=True)

        messages = " ".join(r.message for r in caplog.records)
        assert "DRY-RUN" in messages.upper()

    def test_dry_run_logs_would_notify(self, caplog):
        """Dry-run mode must log 'Would notify' for each gig that passes filters."""
        scraper = _make_scraper_with_gig()
        with (
            patch("main.settings", _make_settings()),
            patch("main.load_seen_gigs", return_value=set()),
            patch("main.load_listings_hash", return_value="old"),
            patch("main.save_listings_hash"),
            patch("main.save_seen_gigs"),
            patch("main.set_run_id"),
            patch("main.filter_store"),
            patch("main.Notifier"),
            patch("main.SMTPTransport"),
            caplog.at_level(logging.INFO),
        ):
            main_module.main(scraper, dry_run=True)

        messages = " ".join(r.message for r in caplog.records)
        assert "Would notify" in messages


class TestNormalRunStillWrites:
    def test_normal_run_writes_listings_hash(self):
        """In normal mode, save_listings_hash IS called."""
        scraper = _make_scraper_with_gig()
        with (
            patch("main.settings", _make_settings()),
            patch("main.load_seen_gigs", return_value=set()),
            patch("main.load_listings_hash", return_value="old"),
            patch("main.save_listings_hash") as mock_save_hash,
            patch("main.save_seen_gigs"),
            patch("main.set_run_id"),
            patch("main.filter_store"),
            patch("main.Notifier"),
            patch("main.SMTPTransport"),
        ):
            main_module.main(scraper, dry_run=False)

        mock_save_hash.assert_called_once()
