"""Gmail API OAuth2 client for monitoring application reply emails."""

from __future__ import annotations

import base64
import logging
import os
import tempfile
from email.mime.text import MIMEText
from pathlib import Path

logger = logging.getLogger(__name__)


def _write_token_secure(path: Path, content: str) -> None:
    """Write token content atomically with mode 0o600 (owner read/write only)."""
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.chmod(tmp, 0o600)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class GmailClient:
    def __init__(self, credentials_file: str, token_file: str) -> None:
        self._credentials_file = credentials_file
        self._token_file = token_file
        self._service = None

    def _get_service(self):
        """Return the Gmail service, building it once per instance.

        A fresh GmailClient is created per check_replies tick, so the OAuth token
        is still refreshed once per tick (in _build_service) — caching here just
        avoids rebuilding the service for each method call within the same tick.
        """
        if self._service is None:
            self._service = self._build_service()
        return self._service

    def _build_service(self):
        """Build authenticated Gmail API service. Refreshes token if expired."""
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        scopes = [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.compose",
        ]
        creds = None
        token_path = Path(self._token_file)

        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), scopes)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    _write_token_secure(token_path, creds.to_json())
                except Exception as exc:
                    # False positive: this logs the exception object, not a credential — the
                    # message text merely mentions "token" in the word "refresh failed".
                    # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
                    logger.warning("Gmail: token refresh failed: %s", exc)
                    raise
            else:
                raise RuntimeError(
                    "Gmail token missing or invalid. Run scripts/setup_gmail_auth.py."
                )

        return build("gmail", "v1", credentials=creds)

    def _search_messages(self, service, query: str) -> list[dict]:
        """Search messages matching query string. Returns list of {id: ...} dicts."""
        try:
            result = service.users().messages().list(userId="me", q=query).execute()
            return result.get("messages", [])
        except Exception as exc:
            logger.warning("Gmail: message search failed (query=%r): %s", query, exc)
            return []

    def _get_message_details(self, service, msg_id: str, direction: str) -> dict | None:
        """Fetch full message and extract key fields."""
        try:
            msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
            headers = {
                h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])
            }
            body = _extract_body(msg.get("payload", {}))
            return {
                "message_id": msg_id,
                "sender": headers.get("from", ""),
                "recipient": headers.get("to", ""),
                "body": body,
                "direction": direction,
            }
        except Exception as exc:
            logger.warning("Gmail: failed to fetch message %s: %s", msg_id, exc)
            return None

    def fetch_reply_messages(
        self,
        applied_emails: list[str],
        accepted_emails: list[str],
        since_date: str | None = None,
    ) -> list[dict]:
        """
        Search inbox for messages FROM church emails (applied + accepted records).
        Search sent folder for messages TO church emails (accepted records only).
        since_date: optional YYYY/MM/DD bound to limit search (avoids full-inbox scan).
        Returns list of dicts: message_id, sender, recipient, body, direction ('incoming'|'outgoing').
        Deduplicates by message_id. Fails open — returns [] on API errors.
        """
        try:
            service = self._get_service()
        except Exception as exc:
            logger.warning("Gmail: could not build service: %s", exc)
            return []

        seen_ids: set[str] = set()
        results: list[dict] = []
        date_suffix = f" after:{since_date}" if since_date else ""
        all_emails = list(set(applied_emails + accepted_emails))

        # Inbox: messages FROM any church email (applied and accepted)
        for email in all_emails:
            msgs = self._search_messages(service, f"from:{email} in:inbox{date_suffix}")
            for m in msgs:
                if m["id"] in seen_ids:
                    continue
                details = self._get_message_details(service, m["id"], "incoming")
                if details:
                    seen_ids.add(m["id"])
                    results.append(details)

        # Sent: messages TO accepted-record emails only (outgoing cancellations)
        for email in accepted_emails:
            msgs = self._search_messages(service, f"to:{email} in:sent{date_suffix}")
            for m in msgs:
                if m["id"] in seen_ids:
                    continue
                details = self._get_message_details(service, m["id"], "outgoing")
                if details:
                    seen_ids.add(m["id"])
                    results.append(details)

        return results

    def fetch_invoice_replies(
        self,
        invoice_number: str,
        client_email: str,
        since_date: str | None = None,
    ) -> list[dict]:
        """Search inbox for replies to a sent invoice.

        Searches for messages from client_email with the invoice number in the subject.
        since_date: optional YYYY/MM/DD bound to avoid full-inbox scan.
        Returns list of {message_id, sender, body, ...} dicts.
        Fails open — returns [] on any error.
        """
        try:
            service = self._get_service()
        except Exception as exc:
            logger.warning("Gmail: could not build service for invoice replies: %s", exc)
            return []

        date_suffix = f" after:{since_date}" if since_date else ""
        query = f"from:{client_email} subject:{invoice_number} in:inbox{date_suffix}"

        seen_ids: set[str] = set()
        results: list[dict] = []

        msgs = self._search_messages(service, query)
        for m in msgs:
            if m["id"] in seen_ids:
                continue
            details = self._get_message_details(service, m["id"], "incoming")
            if details:
                seen_ids.add(m["id"])
                results.append(details)

        return results

    def create_draft(
        self,
        *,
        sender: str,
        recipient: str,
        cc: list[str] | None,
        subject: str,
        body_html: str,
    ) -> str:
        """Create a Gmail draft addressed to recipient. Returns the new draft id.

        Raises on any API failure — callers decide whether to catch (see
        main.py's NEG/review-drafts blocks, which log+alert and skip the gig
        rather than let one gig's failure drop the rest of the tick).
        """
        msg = MIMEText(body_html, "html")
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = recipient
        if cc:
            msg["Cc"] = ", ".join(cc)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        service = self._get_service()
        draft = (
            service.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
        )
        return draft["id"]

    def send_draft(self, draft_id: str) -> None:
        """Send exactly what's currently in the draft — drafts().send. Raises
        on failure, including a 404 if the draft no longer exists (callers
        use is_not_found_error to distinguish that case — see §7 of the spec)."""
        service = self._get_service()
        service.users().drafts().send(userId="me", body={"id": draft_id}).execute()

    def delete_draft(self, draft_id: str) -> None:
        """Delete a draft. Raises on failure, including a 404 if it's already
        gone (callers use is_not_found_error to treat that as success)."""
        service = self._get_service()
        service.users().drafts().delete(userId="me", id=draft_id).execute()

    def has_compose_access(self) -> bool:
        """True if this token can create AND delete a draft.

        A mere drafts().list() call would succeed even under the OLD
        gmail.readonly-only scope (listing drafts is a read operation), so
        it can't distinguish "has compose" from "read-only" — only a real
        create (+ immediate cleanup delete) genuinely exercises write access.
        Never raises — used only by the startup smoke-check in main.py,
        which alerts on False rather than crashing the scheduler.
        """
        try:
            service = self._get_service()
            msg = MIMEText("")
            msg["Subject"] = "OrganistBot scope check (safe to ignore/delete)"
            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            draft = (
                service.users()
                .drafts()
                .create(userId="me", body={"message": {"raw": raw}})
                .execute()
            )
            service.users().drafts().delete(userId="me", id=draft["id"]).execute()
            return True
        except Exception as exc:
            logger.warning("Gmail: compose-access smoke check failed: %s", exc)
            return False


def _extract_body(payload: dict) -> str:
    """Recursively extract plain-text body from a Gmail message payload."""
    mime_type = payload.get("mimeType", "")
    if mime_type == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
        return ""
    for part in payload.get("parts", []):
        body = _extract_body(part)
        if body:
            return body
    return ""


class GmailNotFoundError(Exception):
    """Test-double stand-in for a 404 googleapiclient.errors.HttpError —
    carries status_code so is_not_found_error() recognizes it without
    needing the real HttpError's httplib2.Response machinery."""

    status_code = 404


def is_not_found_error(exc: Exception) -> bool:
    """True if exc represents an HTTP 404 (the draft no longer exists).

    Checks both the real googleapiclient HttpError shape and a generic
    status_code attribute so test doubles (FakeGmailClient, GmailNotFoundError)
    can signal the same condition without needing the real Gmail SDK's
    exception type.
    """
    from googleapiclient.errors import HttpError

    if isinstance(exc, HttpError):
        return exc.resp.status == 404
    return getattr(exc, "status_code", None) == 404


class FakeGmailClient:
    """Records create_draft/send_draft/delete_draft/has_compose_access calls
    without touching the network. Use in tests — mirrors notifier.FakeTransport.
    """

    def __init__(self, *, compose_access: bool = True) -> None:
        self.created: list[dict] = []
        self.sent: list[str] = []
        self.deleted: list[str] = []
        self._compose_access = compose_access
        self._next_id = 0
        self._not_found_ids: set[str] = set()

    def create_draft(
        self,
        *,
        sender: str,
        recipient: str,
        cc: list[str] | None,
        subject: str,
        body_html: str,
    ) -> str:
        self._next_id += 1
        draft_id = f"fake-draft-{self._next_id}"
        self.created.append(
            {
                "draft_id": draft_id,
                "sender": sender,
                "recipient": recipient,
                "cc": cc,
                "subject": subject,
                "body_html": body_html,
            }
        )
        return draft_id

    def send_draft(self, draft_id: str) -> None:
        if draft_id in self._not_found_ids:
            raise GmailNotFoundError(f"draft {draft_id} not found")
        self.sent.append(draft_id)

    def delete_draft(self, draft_id: str) -> None:
        if draft_id in self._not_found_ids:
            raise GmailNotFoundError(f"draft {draft_id} not found")
        self.deleted.append(draft_id)

    def has_compose_access(self) -> bool:
        return self._compose_access

    def simulate_not_found(self, draft_id: str) -> None:
        """Test helper: make a later send_draft/delete_draft(draft_id) raise
        a 404-shaped error instead of succeeding."""
        self._not_found_ids.add(draft_id)
