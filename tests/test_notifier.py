# tests/test_notifier.py
"""Tests for Notifier._dispatch() — success log and elapsed_ms."""

import logging
from unittest.mock import MagicMock

import pytest

from organist_bot.models import Gig
from organist_bot.notifier import FakeTransport, Notifier

# ── Fixtures ──────────────────────────────────────────────────────────────────


def _make_settings(**overrides):
    settings = MagicMock()
    settings.email_sender = "bot@example.com"
    settings.applicant_name = "Test Applicant"
    settings.applicant_mobile = "07700 000000"
    settings.applicant_video_1 = ""
    settings.applicant_video_2 = ""
    settings.cc_email = ""
    settings.base_url = "https://organistsonline.org/required/"
    for k, v in overrides.items():
        setattr(settings, k, v)
    return settings


def _make_gig(**overrides) -> Gig:
    defaults = dict(
        header="Sunday Service",
        organisation="St Paul's Church",
        locality="London",
        date="Sunday, March 1, 2026",
        time="10:00 AM",
        fee="£120",
        link="https://organistsonline.org/required/test",
    )
    defaults.update(overrides)
    return Gig(**defaults)


# ── _dispatch success log ─────────────────────────────────────────────────────


class TestDispatchLogging:
    def test_logs_info_on_successful_send(self, caplog):
        """A successful email send must emit an INFO 'Email dispatched' record."""
        settings = _make_settings()
        transport = FakeTransport()
        notifier = Notifier(settings, transport)

        with caplog.at_level(logging.INFO, logger="organist_bot.notifier"):
            notifier._dispatch(
                subject="Test Subject",
                body="<p>Hello</p>",
                recipient="recipient@example.com",
            )

        record = next(
            (r for r in caplog.records if r.message == "Email dispatched"),
            None,
        )
        assert record is not None, "Expected 'Email dispatched' INFO log record"
        assert record.levelno == logging.INFO

    def test_dispatch_log_contains_subject_and_recipient(self, caplog):
        """The 'Email dispatched' record must carry subject and recipient fields."""
        settings = _make_settings()
        transport = FakeTransport()
        notifier = Notifier(settings, transport)

        with caplog.at_level(logging.INFO, logger="organist_bot.notifier"):
            notifier._dispatch(
                subject="My Subject",
                body="<p>body</p>",
                recipient="dest@example.com",
            )

        record = next(
            (r for r in caplog.records if r.message == "Email dispatched"),
            None,
        )
        assert record is not None
        assert record.subject == "My Subject"
        assert record.recipient == "dest@example.com"

    def test_dispatch_log_contains_elapsed_ms(self, caplog):
        """The 'Email dispatched' record must carry a non-negative elapsed_ms field."""
        settings = _make_settings()
        transport = FakeTransport()
        notifier = Notifier(settings, transport)

        with caplog.at_level(logging.INFO, logger="organist_bot.notifier"):
            notifier._dispatch(
                subject="Timing Test",
                body="<p>body</p>",
                recipient="dest@example.com",
            )

        record = next(
            (r for r in caplog.records if r.message == "Email dispatched"),
            None,
        )
        assert record is not None
        assert isinstance(record.elapsed_ms, int)
        assert record.elapsed_ms >= 0

    def test_no_success_log_when_send_raises(self, caplog):
        """If transport.send() raises, 'Email dispatched' must NOT be logged."""
        settings = _make_settings()
        transport = MagicMock()
        transport.send.side_effect = Exception("SMTP error")
        notifier = Notifier(settings, transport)

        with caplog.at_level(logging.INFO, logger="organist_bot.notifier"):
            with pytest.raises(Exception, match="SMTP error"):
                notifier._dispatch(
                    subject="Fail Subject",
                    body="<p>body</p>",
                    recipient="dest@example.com",
                )

        success_records = [r for r in caplog.records if r.message == "Email dispatched"]
        assert success_records == [], "Should not log 'Email dispatched' on failure"

    def test_failure_log_contains_elapsed_ms(self, caplog):
        """The 'Email dispatch failed' exception record must include elapsed_ms."""
        settings = _make_settings()
        transport = MagicMock()
        transport.send.side_effect = Exception("SMTP error")
        notifier = Notifier(settings, transport)

        with caplog.at_level(logging.ERROR, logger="organist_bot.notifier"):
            with pytest.raises(Exception, match="SMTP error"):
                notifier._dispatch(
                    subject="Fail Subject",
                    body="<p>body</p>",
                    recipient="dest@example.com",
                )

        record = next(
            (r for r in caplog.records if "Email dispatch failed" in r.message),
            None,
        )
        assert record is not None, "Expected 'Email dispatch failed' log record"
        assert isinstance(record.elapsed_ms, int)
        assert record.elapsed_ms >= 0
