"""Gmail API OAuth2 client for monitoring application reply emails."""

from __future__ import annotations

import base64
import logging
import os
import tempfile
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

    def _build_service(self):
        """Build authenticated Gmail API service. Refreshes token if expired."""
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        scopes = ["https://www.googleapis.com/auth/gmail.readonly"]
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
            service = self._build_service()
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
