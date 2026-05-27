# Email Reply Monitoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Monitor Gmail inbox and sent folder for replies to active applications, classify them with Claude, and automatically transition statuses with calendar and Telegram actions.

**Architecture:** `GmailClient` wraps the Gmail API with OAuth2 token auth. `ReplyMonitor.check_replies()` loads active records, fetches reply messages, calls Claude for classification, and dispatches actions (status update, calendar event, Telegram notification). Called from `_run()` in `main.py` after the notify phase, wrapped in try/except so failures never affect the scheduler. Disabled when `GMAIL_CREDENTIALS_FILE` is empty.

**Tech Stack:** Python 3.13, `google-api-python-client`, `google-auth-oauthlib`, `google-auth`, `anthropic`, `pytest`, `unittest.mock`.

---

## File Structure

| File | Change |
|------|--------|
| `organist_bot/integrations/gmail_client.py` | New — OAuth2 Gmail API wrapper |
| `organist_bot/reply_monitor.py` | New — reply classification and dispatch |
| `organist_bot/application_store.py` | Add `update_reply_message_id`; add `rejected` to recognised statuses |
| `organist_bot/integrations/unified_agent.py` | Enhance `manage_applications` update action for accepted→declined follow-up |
| `organist_bot/config.py` | Add `GMAIL_CREDENTIALS_FILE`, `GMAIL_TOKEN_FILE` |
| `main.py` | Import and call `reply_monitor.check_replies()` after notify phase |
| `scripts/setup_gmail_auth.py` | New — one-time OAuth2 setup script |
| `pyproject.toml` | Add `google-auth-oauthlib` dependency |
| `tests/test_gmail_client.py` | New |
| `tests/test_reply_monitor.py` | New |
| `tests/test_application_store.py` | Add `update_reply_message_id` tests |

---

### Task 1: Config fields + `application_store` additions

**Files:**
- Modify: `organist_bot/config.py`
- Modify: `organist_bot/application_store.py`
- Test: `tests/test_application_store.py`

- [ ] **Step 1: Add `GMAIL_CREDENTIALS_FILE` and `GMAIL_TOKEN_FILE` to `organist_bot/config.py`**

```python
# ── Gmail (reply monitoring) ───────────────────────────────────────────────
gmail_credentials_file: str = ""
gmail_token_file: str = "data/gmail_token.json"
```

Add these two lines in `Settings` class after the Telegram section.

- [ ] **Step 2: Write failing tests for `update_reply_message_id`**

Add to `tests/test_application_store.py`:

```python
class TestUpdateReplyMessageId:
    def _write(self, tmp_path, records):
        (tmp_path / "applications.json").write_text(json.dumps(records))

    def test_sets_reply_message_id_on_existing_record(self, tmp_path, monkeypatch):
        import organist_bot.application_store as store
        monkeypatch.setattr(store, "_PATH", tmp_path / "applications.json")
        self._write(tmp_path, [{
            "url": "http://a.com/1", "header": "T", "organisation": "St John",
            "date": "2026-06-10", "fee": "£100", "email": "org@example.com",
            "status": "applied", "applied_at": "2026-06-01T10:00:00Z",
            "updated_at": "2026-06-01T10:00:00Z",
        }])
        result = store.update_reply_message_id("http://a.com/1", "msg123")
        assert result is True
        records = json.loads((tmp_path / "applications.json").read_text())
        assert records[0]["reply_message_id"] == "msg123"

    def test_returns_false_when_url_not_found(self, tmp_path, monkeypatch):
        import organist_bot.application_store as store
        monkeypatch.setattr(store, "_PATH", tmp_path / "applications.json")
        self._write(tmp_path, [])
        assert store.update_reply_message_id("http://notfound.com", "msg123") is False
```

- [ ] **Step 3: Run tests to confirm they fail**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_application_store.py::TestUpdateReplyMessageId -v
```
Expected: AttributeError (function not defined)

- [ ] **Step 4: Implement `update_reply_message_id` in `organist_bot/application_store.py`**

Add after `update_status`:

```python
def update_reply_message_id(url: str, message_id: str) -> bool:
    """Set reply_message_id on the record with the given URL. Returns False if not found."""
    records = _read()
    for r in records:
        if r["url"] == url:
            r["reply_message_id"] = message_id
            r["updated_at"] = _now_iso()
            _write(records)
            return True
    return False
```

- [ ] **Step 5: Run tests to confirm they pass**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_application_store.py::TestUpdateReplyMessageId -v
```
Expected: 2 PASSED

- [ ] **Step 6: Run full suite**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest --tb=short -q
```

- [ ] **Step 7: Commit**

```bash
git add organist_bot/config.py organist_bot/application_store.py tests/test_application_store.py
git commit -m "feat: add Gmail config fields and update_reply_message_id to application_store"
```

---

### Task 2: `GmailClient` — `organist_bot/integrations/gmail_client.py`

**Files:**
- Create: `organist_bot/integrations/gmail_client.py`
- Test: `tests/test_gmail_client.py`

- [ ] **Step 1: Add `google-auth-oauthlib` to `pyproject.toml`**

```toml
"google-auth-oauthlib>=1.0",
```
Add to the `dependencies` list in `pyproject.toml`, then run:
```bash
uv sync
```

- [ ] **Step 2: Write failing tests for `GmailClient`**

Create `tests/test_gmail_client.py`:

```python
import pytest
from unittest.mock import MagicMock, patch


def _make_message(msg_id, sender, recipient, body, direction):
    return {
        "message_id": msg_id,
        "sender": sender,
        "recipient": recipient,
        "body": body,
        "direction": direction,
    }


class TestFetchReplyMessages:
    def _make_client(self, tmp_path):
        creds_file = tmp_path / "credentials.json"
        creds_file.write_text("{}")
        token_file = tmp_path / "token.json"
        from organist_bot.integrations.gmail_client import GmailClient
        return GmailClient(str(creds_file), str(token_file))

    def test_returns_inbox_messages_from_church_emails(self, tmp_path):
        client = self._make_client(tmp_path)
        with patch.object(client, "_search_messages") as mock_search, \
             patch.object(client, "_get_message_details") as mock_details:
            mock_search.return_value = [{"id": "msg1"}]
            mock_details.return_value = _make_message(
                "msg1", "church@example.com", "me@example.com",
                "We'd love to have you", "incoming"
            )
            result = client.fetch_reply_messages(
                applied_emails=["church@example.com"],
                accepted_emails=[],
            )
        assert len(result) == 1
        assert result[0]["message_id"] == "msg1"
        assert result[0]["direction"] == "incoming"

    def test_returns_sent_messages_to_accepted_record_emails(self, tmp_path):
        client = self._make_client(tmp_path)
        with patch.object(client, "_search_messages") as mock_search, \
             patch.object(client, "_get_message_details") as mock_details:
            mock_search.return_value = [{"id": "msg2"}]
            mock_details.return_value = _make_message(
                "msg2", "me@example.com", "accepted_church@example.com",
                "I need to cancel", "outgoing"
            )
            result = client.fetch_reply_messages(
                applied_emails=[],
                accepted_emails=["accepted_church@example.com"],
            )
        assert len(result) == 1
        assert result[0]["direction"] == "outgoing"

    def test_does_not_return_sent_for_applied_only_emails(self, tmp_path):
        client = self._make_client(tmp_path)
        with patch.object(client, "_search_messages") as mock_search:
            mock_search.return_value = []
            result = client.fetch_reply_messages(
                applied_emails=["applied_only@example.com"],
                accepted_emails=[],
            )
        # sent folder not searched for applied-only emails
        assert result == []

    def test_fails_open_on_api_error(self, tmp_path):
        client = self._make_client(tmp_path)
        with patch.object(client, "_build_service", side_effect=Exception("API down")):
            result = client.fetch_reply_messages(
                applied_emails=["church@example.com"],
                accepted_emails=[],
            )
        assert result == []
```

- [ ] **Step 3: Run tests to confirm they fail**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_gmail_client.py -v
```
Expected: ImportError (module not yet created)

- [ ] **Step 4: Implement `organist_bot/integrations/gmail_client.py`**

```python
"""Gmail API OAuth2 client for monitoring application reply emails."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class GmailClient:
    def __init__(self, credentials_file: str, token_file: str) -> None:
        self._credentials_file = credentials_file
        self._token_file = token_file

    def _build_service(self):
        """Build authenticated Gmail API service. Refreshes token if needed."""
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
                    token_path.write_text(creds.to_json())
                except Exception as exc:
                    logger.warning("Gmail: token refresh failed: %s", exc)
                    raise
            else:
                raise RuntimeError("Gmail token missing or invalid. Run scripts/setup_gmail_auth.py.")

        return build("gmail", "v1", credentials=creds)

    def _search_messages(self, service, query: str) -> list[dict]:
        """Search messages matching query. Returns list of {id: ...} dicts."""
        try:
            result = service.users().messages().list(userId="me", q=query).execute()
            return result.get("messages", [])
        except Exception as exc:
            logger.warning("Gmail: message search failed: %s", exc)
            return []

    def _get_message_details(self, service, msg_id: str, direction: str) -> dict | None:
        """Fetch full message and extract relevant fields."""
        try:
            msg = service.users().messages().get(
                userId="me", id=msg_id, format="full"
            ).execute()
            headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
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
    ) -> list[dict]:
        """
        Search inbox for messages FROM church emails (applied + accepted records).
        Search sent folder for messages TO church emails (accepted records only).
        Returns list of dicts: message_id, sender, recipient, body, direction.
        Fails open — returns [] on API errors.
        """
        try:
            service = self._build_service()
        except Exception as exc:
            logger.warning("Gmail: could not build service: %s", exc)
            return []

        results: list[dict] = []
        all_emails = list(set(applied_emails + accepted_emails))

        # Inbox: messages FROM any church email
        for email in all_emails:
            msgs = self._search_messages(service, f"from:{email} in:inbox")
            for m in msgs:
                details = self._get_message_details(service, m["id"], "incoming")
                if details:
                    results.append(details)

        # Sent: messages TO accepted-record emails only (for outgoing cancellations)
        for email in accepted_emails:
            msgs = self._search_messages(service, f"to:{email} in:sent")
            for m in msgs:
                details = self._get_message_details(service, m["id"], "outgoing")
                if details:
                    results.append(details)

        return results


def _extract_body(payload: dict) -> str:
    """Recursively extract plain-text body from a Gmail message payload."""
    import base64
    mime_type = payload.get("mimeType", "")
    if mime_type == "text/plain":
        data = payload.get("body", {}).get("data", "")
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace") if data else ""
    for part in payload.get("parts", []):
        body = _extract_body(part)
        if body:
            return body
    return ""
```

- [ ] **Step 5: Run tests to confirm they pass**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_gmail_client.py -v
```
Expected: 4 PASSED

- [ ] **Step 6: Run full suite**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest --tb=short -q
```

- [ ] **Step 7: Commit**

```bash
git add organist_bot/integrations/gmail_client.py tests/test_gmail_client.py pyproject.toml
git commit -m "feat: add GmailClient OAuth2 wrapper for reply monitoring"
```

---

### Task 3: `ReplyMonitor` — `organist_bot/reply_monitor.py`

**Files:**
- Create: `organist_bot/reply_monitor.py`
- Test: `tests/test_reply_monitor.py`

- [ ] **Step 1: Write failing tests for `check_replies`**

Create `tests/test_reply_monitor.py`:

```python
import pytest
from unittest.mock import MagicMock, patch, call


def _make_record(url, email, status, reply_message_id=None):
    return {
        "url": url, "header": "Evening Service", "organisation": "St John",
        "date": "2026-06-15", "fee": "£100", "email": email,
        "status": status,
        "applied_at": "2026-06-01T10:00:00Z",
        "updated_at": "2026-06-01T10:00:00Z",
        "reply_message_id": reply_message_id,
    }


def _make_message(msg_id, sender, direction="incoming"):
    return {
        "message_id": msg_id,
        "sender": sender,
        "recipient": "me@example.com",
        "body": "Thank you for your application, we'd like to book you.",
        "direction": direction,
    }


class TestCheckReplies:
    def _patch_all(self, records, messages, classification):
        """Helper to set up all the mocks needed for check_replies."""
        return [
            patch("organist_bot.reply_monitor.application_store.list_applications", return_value=records),
            patch("organist_bot.reply_monitor.application_store.update_status"),
            patch("organist_bot.reply_monitor.application_store.upsert_accepted"),
            patch("organist_bot.reply_monitor.application_store.update_reply_message_id"),
            patch("organist_bot.reply_monitor.GmailClient") ,
            patch("organist_bot.reply_monitor._classify_reply", return_value=classification),
            patch("organist_bot.reply_monitor._send_telegram_notification"),
            patch("organist_bot.reply_monitor.settings"),
        ]

    def test_accepted_reply_updates_status_and_notifies(self):
        records = [_make_record("http://a.com/1", "church@example.com", "applied")]
        messages = [_make_message("msg1", "church@example.com")]
        with patch("organist_bot.reply_monitor.application_store.list_applications", return_value=records), \
             patch("organist_bot.reply_monitor.application_store.upsert_accepted") as mock_upsert, \
             patch("organist_bot.reply_monitor.application_store.update_reply_message_id") as mock_rid, \
             patch("organist_bot.reply_monitor._make_gmail_client") as mock_gmail, \
             patch("organist_bot.reply_monitor._classify_reply", return_value="accepted"), \
             patch("organist_bot.reply_monitor._send_telegram_notification") as mock_notify, \
             patch("organist_bot.reply_monitor._create_calendar_event") as mock_cal, \
             patch("organist_bot.reply_monitor.settings") as mock_settings:
            mock_settings.gmail_credentials_file = "creds.json"
            mock_settings.gmail_token_file = "token.json"
            mock_gmail.return_value.fetch_reply_messages.return_value = messages
            from organist_bot.reply_monitor import check_replies
            check_replies()
        mock_upsert.assert_called_once()
        mock_cal.assert_called_once()
        mock_notify.assert_called_once()
        mock_rid.assert_called_once_with("http://a.com/1", "msg1")

    def test_rejected_reply_updates_status_and_notifies(self):
        records = [_make_record("http://a.com/1", "church@example.com", "applied")]
        messages = [_make_message("msg1", "church@example.com")]
        with patch("organist_bot.reply_monitor.application_store.list_applications", return_value=records), \
             patch("organist_bot.reply_monitor.application_store.update_status") as mock_update, \
             patch("organist_bot.reply_monitor.application_store.update_reply_message_id") as mock_rid, \
             patch("organist_bot.reply_monitor._make_gmail_client") as mock_gmail, \
             patch("organist_bot.reply_monitor._classify_reply", return_value="rejected"), \
             patch("organist_bot.reply_monitor._send_telegram_notification") as mock_notify, \
             patch("organist_bot.reply_monitor.settings") as mock_settings:
            mock_settings.gmail_credentials_file = "creds.json"
            mock_settings.gmail_token_file = "token.json"
            mock_gmail.return_value.fetch_reply_messages.return_value = messages
            from organist_bot.reply_monitor import check_replies
            check_replies()
        mock_update.assert_called_once_with("http://a.com/1", "rejected")
        mock_notify.assert_called_once()
        mock_rid.assert_called_once_with("http://a.com/1", "msg1")

    def test_cancellation_sends_telegram_prompt_no_status_change(self):
        records = [_make_record("http://a.com/1", "church@example.com", "accepted")]
        messages = [_make_message("msg1", "church@example.com")]
        with patch("organist_bot.reply_monitor.application_store.list_applications", return_value=records), \
             patch("organist_bot.reply_monitor.application_store.update_status") as mock_update, \
             patch("organist_bot.reply_monitor.application_store.update_reply_message_id") as mock_rid, \
             patch("organist_bot.reply_monitor._make_gmail_client") as mock_gmail, \
             patch("organist_bot.reply_monitor._classify_reply", return_value="cancellation"), \
             patch("organist_bot.reply_monitor._send_telegram_notification") as mock_notify, \
             patch("organist_bot.reply_monitor.settings") as mock_settings:
            mock_settings.gmail_credentials_file = "creds.json"
            mock_settings.gmail_token_file = "token.json"
            mock_gmail.return_value.fetch_reply_messages.return_value = messages
            from organist_bot.reply_monitor import check_replies
            check_replies()
        mock_update.assert_not_called()
        mock_notify.assert_called_once()
        # "cancel" should appear in the notification text
        call_args = mock_notify.call_args[0][0]
        assert "cancel" in call_args.lower() or "Cancel" in call_args

    def test_unclear_sends_telegram_excerpt_no_status_change(self):
        records = [_make_record("http://a.com/1", "church@example.com", "applied")]
        messages = [_make_message("msg1", "church@example.com")]
        with patch("organist_bot.reply_monitor.application_store.list_applications", return_value=records), \
             patch("organist_bot.reply_monitor.application_store.update_status") as mock_update, \
             patch("organist_bot.reply_monitor.application_store.update_reply_message_id") as mock_rid, \
             patch("organist_bot.reply_monitor._make_gmail_client") as mock_gmail, \
             patch("organist_bot.reply_monitor._classify_reply", return_value="unclear"), \
             patch("organist_bot.reply_monitor._send_telegram_notification") as mock_notify, \
             patch("organist_bot.reply_monitor.settings") as mock_settings:
            mock_settings.gmail_credentials_file = "creds.json"
            mock_settings.gmail_token_file = "token.json"
            mock_gmail.return_value.fetch_reply_messages.return_value = messages
            from organist_bot.reply_monitor import check_replies
            check_replies()
        mock_update.assert_not_called()
        mock_notify.assert_called_once()

    def test_dedup_skips_message_when_reply_message_id_set(self):
        records = [_make_record("http://a.com/1", "church@example.com", "applied", reply_message_id="msg1")]
        messages = [_make_message("msg1", "church@example.com")]
        with patch("organist_bot.reply_monitor.application_store.list_applications", return_value=records), \
             patch("organist_bot.reply_monitor.application_store.update_status") as mock_update, \
             patch("organist_bot.reply_monitor._make_gmail_client") as mock_gmail, \
             patch("organist_bot.reply_monitor._classify_reply") as mock_classify, \
             patch("organist_bot.reply_monitor.settings") as mock_settings:
            mock_settings.gmail_credentials_file = "creds.json"
            mock_settings.gmail_token_file = "token.json"
            mock_gmail.return_value.fetch_reply_messages.return_value = messages
            from organist_bot.reply_monitor import check_replies
            check_replies()
        mock_classify.assert_not_called()
        mock_update.assert_not_called()

    def test_message_from_unknown_email_skipped(self):
        records = [_make_record("http://a.com/1", "known@example.com", "applied")]
        messages = [_make_message("msg1", "unknown@other.com")]
        with patch("organist_bot.reply_monitor.application_store.list_applications", return_value=records), \
             patch("organist_bot.reply_monitor._make_gmail_client") as mock_gmail, \
             patch("organist_bot.reply_monitor._classify_reply") as mock_classify, \
             patch("organist_bot.reply_monitor.settings") as mock_settings:
            mock_settings.gmail_credentials_file = "creds.json"
            mock_settings.gmail_token_file = "token.json"
            mock_gmail.return_value.fetch_reply_messages.return_value = messages
            from organist_bot.reply_monitor import check_replies
            check_replies()
        mock_classify.assert_not_called()

    def test_disabled_when_credentials_file_empty(self):
        with patch("organist_bot.reply_monitor.settings") as mock_settings, \
             patch("organist_bot.reply_monitor._make_gmail_client") as mock_gmail:
            mock_settings.gmail_credentials_file = ""
            from organist_bot.reply_monitor import check_replies
            check_replies()
        mock_gmail.assert_not_called()

    def test_fails_open_on_api_error(self):
        with patch("organist_bot.reply_monitor.settings") as mock_settings, \
             patch("organist_bot.reply_monitor.application_store.list_applications", side_effect=Exception("db error")):
            mock_settings.gmail_credentials_file = "creds.json"
            from organist_bot.reply_monitor import check_replies
            # Must not raise
            check_replies()
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_reply_monitor.py -v
```
Expected: ImportError (module not yet created)

- [ ] **Step 3: Implement `organist_bot/reply_monitor.py`**

```python
"""Reply monitor — classifies Gmail replies to active applications and dispatches actions."""

from __future__ import annotations

import logging

import anthropic

import organist_bot.application_store as application_store
from organist_bot.config import settings
from organist_bot.integrations.gmail_client import GmailClient

logger = logging.getLogger(__name__)

_CLASSIFY_PROMPT = """\
You are classifying an email reply related to an organ performance job application.

The applicant applied for a gig at: {organisation}
Gig date: {date}
Reply from: {sender}
Reply body:
{body}

Classify the reply as one of:
- accepted: The church/organisation is confirming/booking the applicant.
- rejected: The church/organisation has moved on or filled the position with someone else.
- cancellation: Either party is signalling they want to cancel an existing booking.
- unclear: Anything else (questions, ambiguous requests, logistical queries, etc.).

Reply with ONLY the classification word, nothing else."""


def _make_gmail_client() -> GmailClient:
    return GmailClient(
        credentials_file=settings.gmail_credentials_file,
        token_file=settings.gmail_token_file,
    )


def _classify_reply(message: dict, record: dict) -> str:
    """Call Claude to classify a reply. Returns one of: accepted/rejected/cancellation/unclear."""
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    prompt = _CLASSIFY_PROMPT.format(
        organisation=record.get("organisation", ""),
        date=record.get("date", ""),
        sender=message.get("sender", ""),
        body=message.get("body", "")[:2000],
    )
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content": prompt}],
        )
        result = response.content[0].text.strip().lower()
        if result not in ("accepted", "rejected", "cancellation", "unclear"):
            logger.warning("Unexpected classification: %r — treating as unclear", result)
            return "unclear"
        return result
    except Exception as exc:
        logger.warning("reply_monitor: classification failed: %s", exc)
        return "unclear"


def _send_telegram_notification(text: str) -> None:
    """Fire-and-forget Telegram notification."""
    import requests
    token = settings.telegram_bot_token
    chat_id = settings.telegram_chat_id
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
    except Exception as exc:
        logger.warning("reply_monitor: telegram notification failed: %s", exc)


def _create_calendar_event(record: dict) -> None:
    """Create a Google Calendar event for an accepted booking."""
    if not settings.google_calendar_id or not settings.google_calendar_credentials_file:
        return
    try:
        from organist_bot.integrations.calendar_client import GoogleCalendarClient
        cal = GoogleCalendarClient(
            credentials_file=settings.google_calendar_credentials_file,
            calendar_id=settings.google_calendar_id,
        )
        cal.add_gig(
            title=record.get("header", record.get("organisation", "Gig")),
            organisation=record.get("organisation", ""),
            locality="",
            date=record.get("date", ""),
            time="",
            fee=record.get("fee", ""),
        )
    except Exception as exc:
        logger.warning("reply_monitor: calendar event creation failed: %s", exc)


def _match_record(message: dict, records: list[dict]) -> dict | None:
    """Find the application record whose email matches the message sender or recipient."""
    msg_sender = message.get("sender", "").lower()
    msg_recipient = message.get("recipient", "").lower()
    for r in records:
        record_email = r.get("email", "").lower()
        if not record_email:
            continue
        if record_email in msg_sender or record_email in msg_recipient:
            return r
    return None


def check_replies() -> None:
    """Check Gmail for replies to active applications and dispatch actions."""
    if not settings.gmail_credentials_file:
        return
    try:
        records = application_store.list_applications(days=365)
        active = [r for r in records if r["status"] in ("applied", "accepted")]
        if not active:
            return

        applied_emails = [r["email"] for r in active if r["status"] == "applied" and r.get("email")]
        accepted_emails = [r["email"] for r in active if r["status"] == "accepted" and r.get("email")]

        client = _make_gmail_client()
        messages = client.fetch_reply_messages(
            applied_emails=applied_emails,
            accepted_emails=accepted_emails,
        )

        for msg in messages:
            try:
                record = _match_record(msg, active)
                if record is None:
                    continue

                # Dedup: skip if already processed
                if record.get("reply_message_id"):
                    continue

                classification = _classify_reply(msg, record)
                org = record.get("organisation") or record.get("header", "")
                date = record.get("date", "")

                if classification == "accepted":
                    application_store.upsert_accepted(
                        url=record.get("url") or None,
                        header=record.get("header", ""),
                        organisation=org,
                        date=date,
                        fee=record.get("fee", ""),
                    )
                    _create_calendar_event(record)
                    _send_telegram_notification(
                        f"✅ Booking confirmed: {org} on {date}\n(via email reply)"
                    )

                elif classification == "rejected":
                    application_store.update_status(record["url"], "rejected")
                    _send_telegram_notification(
                        f"❌ Application rejected: {org} on {date}\n(via email reply)"
                    )

                elif classification == "cancellation":
                    _send_telegram_notification(
                        f"⚠️ Possible cancellation: {org} on {date}\n"
                        f"Reply from: {msg.get('sender', 'unknown')}\n"
                        f'"{msg.get("body", "")[:200]}"\n\n'
                        f"Delete calendar event or ignore?"
                    )

                elif classification == "unclear":
                    _send_telegram_notification(
                        f"📧 Unclassified reply from {org} ({date}):\n"
                        f'"{msg.get("body", "")[:300]}"'
                    )

                application_store.update_reply_message_id(
                    record.get("url", ""), msg["message_id"]
                )

            except Exception as exc:
                logger.warning("reply_monitor: error processing message %s: %s", msg.get("message_id"), exc)

    except Exception:
        logger.warning("reply_monitor: check_replies failed", exc_info=True)
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_reply_monitor.py -v
```
Expected: 8 PASSED

- [ ] **Step 5: Run full suite**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest --tb=short -q
```

- [ ] **Step 6: Commit**

```bash
git add organist_bot/reply_monitor.py tests/test_reply_monitor.py
git commit -m "feat: add reply_monitor — classify Gmail replies and update application statuses"
```

---

### Task 4: Wire `check_replies` into `main.py` + `setup_gmail_auth.py`

**Files:**
- Modify: `main.py`
- Create: `scripts/setup_gmail_auth.py`

- [ ] **Step 1: Add `reply_monitor` import to `main.py`**

```python
import organist_bot.reply_monitor as reply_monitor
```

- [ ] **Step 2: Call `check_replies()` after `expire_past_applied` in `_run()`**

After the `expire_past_applied` try/except block (around line 280), add:

```python
try:
    reply_monitor.check_replies()
except Exception:
    logger.warning("reply_monitor: check_replies failed", exc_info=True)
    alert.send_alert("⚠️ reply_monitor: check_replies raised an unexpected exception. Check logs.")
```

- [ ] **Step 3: Create `scripts/setup_gmail_auth.py`**

```python
#!/usr/bin/env python3
"""One-time OAuth2 setup for Gmail API access.

Run this locally (requires a browser). Copy the resulting data/gmail_token.json
to the server for headless operation.

Prerequisites:
- Download OAuth2 credentials.json from Google Cloud Console
- Set GMAIL_CREDENTIALS_FILE in .env to point to credentials.json
"""
import sys
from pathlib import Path

# Allow running from project root without installing
sys.path.insert(0, str(Path(__file__).parent.parent))

from organist_bot.config import settings

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def main():
    if not settings.gmail_credentials_file:
        print("Error: GMAIL_CREDENTIALS_FILE not set in .env")
        sys.exit(1)

    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(settings.gmail_credentials_file, SCOPES)
    creds = flow.run_local_server(port=0)

    token_path = Path(settings.gmail_token_file)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    print(f"Token saved to {token_path}")
    print("Copy this file to the server and set GMAIL_TOKEN_FILE in .env if needed.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run full suite to confirm no regressions**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest --tb=short -q
```
Expected: all passing

- [ ] **Step 5: Commit**

```bash
git add main.py scripts/setup_gmail_auth.py
git commit -m "feat: wire reply_monitor.check_replies into main scheduler tick"
```

---

### Task 5: `manage_applications` update action — accepted→declined calendar follow-up

**Files:**
- Modify: `organist_bot/integrations/unified_agent.py`
- Test: `tests/test_unified_agent.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_unified_agent.py`:

```python
class TestManageApplicationsUpdateDeclined:
    @pytest.mark.asyncio
    async def test_accepted_to_declined_prompts_calendar_delete(self):
        """When update transitions accepted→declined, result should mention calendar event deletion."""
        records = [
            {
                "url": "http://a.com/1", "header": "Evening Service",
                "organisation": "St John", "date": "2026-06-15",
                "fee": "£100", "email": "", "status": "accepted",
                "applied_at": "2026-06-01T10:00:00Z", "updated_at": "2026-06-01T10:00:00Z",
            }
        ]
        # First list to populate cache
        with patch("organist_bot.integrations.unified_agent.application_store.list_applications", return_value=records):
            await _execute_tool("manage_applications", {"action": "list"}, CHAT_ID)

        with patch("organist_bot.integrations.unified_agent.application_store.update_status", return_value=True):
            result = await _execute_tool(
                "manage_applications",
                {"action": "update", "number": 1, "status": "declined"},
                CHAT_ID,
            )
        # Should mention calendar event
        assert "calendar" in result.lower() or "event" in result.lower()
```

- [ ] **Step 2: Run to confirm it fails**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest "tests/test_unified_agent.py::TestManageApplicationsUpdateDeclined" -v
```

- [ ] **Step 3: Update `manage_applications` update action in `unified_agent.py`**

After `update_status` is called successfully (around line 1110), check if transitioning from accepted to declined and append a follow-up prompt:

```python
ok = application_store.update_status(url, status)
if ok:
    listing[n - 1]["status"] = status
    result_msg = f"Updated application {n} to '{status}'."
    # If transitioning accepted → declined, offer calendar event deletion
    if status == "declined" and record.get("status") == "accepted":
        org = record.get("organisation") or record.get("header", "")
        date = record.get("date", "")
        result_msg += (
            f"\n\nThis was an accepted booking. "
            f"Do you want to delete the calendar event for {org} on {date}?"
        )
    return json.dumps({"result": result_msg})
```

Note: `record` is already available as `listing[n - 1]` before the update. Capture the original status before calling `update_status`:

```python
record = listing[n - 1]
original_status = record.get("status", "")
url = record.get("url", "")
# ... existing validation ...
ok = application_store.update_status(url, status)
if ok:
    listing[n - 1]["status"] = status
    result_msg = f"Updated application {n} to '{status}'."
    if status == "declined" and original_status == "accepted":
        org = record.get("organisation") or record.get("header", "")
        date = record.get("date", "")
        result_msg += (
            f"\n\nThis was an accepted booking. "
            f"Do you want to delete the calendar event for {org} on {date}?"
        )
    return json.dumps({"result": result_msg})
```

Also add `rejected` to the `_status_emoji` dict in the list action:
```python
_status_emoji = {
    "accepted": "✅",
    "applied": "⏳",
    "no_response": "🔕",
    "declined": "❌",
    "rejected": "🚫",
}
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest "tests/test_unified_agent.py::TestManageApplicationsUpdateDeclined" -v
```
Expected: PASSED

- [ ] **Step 5: Run full suite**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest --tb=short -q
```

- [ ] **Step 6: Commit**

```bash
git add organist_bot/integrations/unified_agent.py tests/test_unified_agent.py
git commit -m "feat: prompt calendar event deletion when accepted application is declined"
```
