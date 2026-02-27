import datetime
import logging
import smtplib
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

from jinja2 import Environment, FileSystemLoader, select_autoescape

from organist_bot.config import Settings
from organist_bot.models import Gig

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"


# ── Transport layer ───────────────────────────────────────────────────────────

@runtime_checkable
class Transport(Protocol):
    """Anything that can deliver a raw MIME message."""

    def send(self, sender: str, recipients: list[str], raw_message: str) -> None: ...


class SMTPTransport:
    """Sends email via Gmail SMTP SSL."""

    def __init__(self, password: str) -> None:
        self._password = password

    def send(self, sender: str, recipients: list[str], raw_message: str) -> None:
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
                smtp.login(sender, self._password)
                smtp.sendmail(sender, recipients, raw_message)
        except smtplib.SMTPException:
            logger.exception(
                "SMTP send failed",
                extra={"sender": sender, "recipients": recipients},
            )
            raise


class FakeTransport:
    """Records outgoing messages without touching the network. Use in tests."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, sender: str, recipients: list[str], raw_message: str) -> None:
        self.sent.append(
            {"sender": sender, "recipients": recipients, "message": raw_message}
        )

    def reset(self) -> None:
        self.sent.clear()


# ── Notifier ──────────────────────────────────────────────────────────────────

class Notifier:
    """Renders Jinja2 templates and dispatches email via an injected Transport.

    Usage (production):
        transport = SMTPTransport(password=settings.email_password)
        notifier  = Notifier(settings, transport)
        notifier.send_summary(valid_gigs)
        notifier.apply_to_gig(gig)

    Usage (tests):
        transport = FakeTransport()
        notifier  = Notifier(settings, transport)
        notifier.send_summary([gig])
        assert "New Organ Gigs" in transport.sent[0]["message"]
    """

    def __init__(self, settings: Settings, transport: Transport) -> None:
        self._settings = settings
        self._transport = transport
        self._env = Environment(
            loader=FileSystemLoader(TEMPLATES_DIR),
            autoescape=select_autoescape(["html", "j2"]),
        )

    # ── private helpers ───────────────────────────────────────────────────────

    def _render(self, template_name: str, **context) -> str:
        logger.debug("Rendering template", extra={"template": template_name})
        return self._env.get_template(template_name).render(**context)

    def _build_message(
        self,
        subject: str,
        body: str,
        recipient: str,
        cc: Optional[list[str]] = None,
    ) -> tuple[MIMEText, list[str]]:
        msg = MIMEText(body, "html")
        msg["Subject"] = subject
        msg["From"] = self._settings.email_sender
        msg["To"] = recipient
        if cc:
            msg["Cc"] = ", ".join(cc)
        recipients = [recipient] + (cc or [])
        return msg, recipients

    def _dispatch(
        self,
        subject: str,
        body: str,
        recipient: str,
        cc: Optional[list[str]] = None,
    ) -> None:
        msg, recipients = self._build_message(subject, body, recipient, cc)
        logger.debug(
            "Dispatching email",
            extra={
                "subject":    subject,
                "recipient":  recipient,
                "cc":         cc or [],
                "body_bytes": len(body.encode()),
            },
        )
        try:
            self._transport.send(self._settings.email_sender, recipients, msg.as_string())
        except Exception:
            logger.exception(
                "Email dispatch failed",
                extra={"subject": subject, "recipient": recipient},
            )
            raise

    # ── public API ────────────────────────────────────────────────────────────

    def send_summary(self, gigs: list[Gig]) -> None:
        """Send a digest of all newly-matched gigs to the bot owner."""
        if not gigs:
            logger.info("No new gigs — summary email skipped")
            return

        logger.info("Sending summary email", extra={"gig_count": len(gigs)})
        body = self._render(
            "summary.html.j2",
            gigs=gigs,
            base_url=self._settings.base_url,
        )
        cc = []
        self._dispatch(
            subject=f"New Organ Gigs ({len(gigs)} found)",
            body=body,
            recipient=self._settings.email_sender,
            cc=cc,
        )

    def apply_to_gig(self, gig: Gig) -> None:
        """Send an application email directly to the gig's contact."""
        if not gig.email:
            logger.warning(
                "Application skipped — no contact email",
                extra={"header": gig.header, "date": gig.date, "org": gig.organisation},
            )
            return

        body = self._render(
            "application.html.j2",
            gig=gig,
            applicant_name=self._settings.applicant_name,
            applicant_mobile=self._settings.applicant_mobile,
            applicant_video_1=self._settings.applicant_video_1,
            applicant_video_2=self._settings.applicant_video_2,
        )
        cc = [self._settings.cc_email] if self._settings.cc_email else None
        self._dispatch(
            subject=f"Application for Organist Position \u2013 {gig.date}",
            body=body,
            recipient="kojodakey@gmail.com",
            cc=cc,
        )
