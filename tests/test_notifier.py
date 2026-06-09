# tests/test_notifier.py
"""Tests for Notifier._dispatch() — success log and elapsed_ms."""

import logging
from unittest.mock import MagicMock, patch

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


# ── apply_to_gig records application ─────────────────────────────────────────


class TestApplyToGigRecordsApplication:
    def test_apply_to_gig_records_application(self):
        """apply_to_gig must call record_application once with the gig."""
        settings = _make_settings()
        transport = FakeTransport()
        notifier = Notifier(settings, transport)
        gig = _make_gig(email="test@church.com")
        with patch("organist_bot.notifier.application_store") as mock_store:
            mock_store.record_application.return_value = True
            notifier.apply_to_gig(gig)
        mock_store.record_application.assert_called_once_with(gig)


# ── negotiation.html.j2 template ──────────────────────────────────────────────


def _render_negotiation(**ctx):
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    from organist_bot.notifier import TEMPLATES_DIR

    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html", "j2"]),
    )
    defaults = dict(
        gig=_make_gig(fee="NEG", contact="Jane Smith", email="jane@stmarys.org"),
        applicant_name="Alex Organist",
        applicant_mobile="07700 900000",
        applicant_video_1="",
        applicant_video_2="",
        negotiable_fee=120,
    )
    defaults.update(ctx)
    return env.get_template("negotiation.html.j2").render(**defaults)


class TestNegotiationTemplate:
    def test_includes_negotiable_fee(self):
        rendered = _render_negotiation(
            applicant_video_1="https://yt/v1",
            applicant_video_2="https://yt/v2",
        )
        assert "£120" in rendered
        assert "Jane Smith" in rendered
        assert "Sunday, March 1, 2026" in rendered
        assert "Alex Organist" in rendered
        assert "https://yt/v1" in rendered

    def test_uses_provided_fee_value(self):
        rendered = _render_negotiation(negotiable_fee=150)
        assert "£150" in rendered
        assert "£120" not in rendered

    def test_falls_back_to_sir_madam_when_no_contact(self):
        rendered = _render_negotiation(
            gig=_make_gig(fee="NEG", contact=None, email="jane@stmarys.org")
        )
        assert "Sir/Madam" in rendered

    def test_omits_videos_section_when_empty(self):
        rendered = _render_negotiation()
        assert "Video 1" not in rendered
        assert "Video 2" not in rendered
