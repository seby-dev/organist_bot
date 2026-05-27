# Overdue Invoice Reminders Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Send a one-time Telegram reminder when an emailed invoice hasn't been paid after 5 days, auto-detect payment via Gmail reply monitoring, and expose a `mark_invoice_paid` tool in the unified agent.

**Architecture:** `invoice_generator.py` gains `emailed_at`, `paid_at`, `reminder_sent`, and `checked_reply_ids` fields plus `mark_invoice_paid()` and `save_invoice_field()` functions. A new `invoice_monitor.py` module checks emailed-but-unpaid invoices every scheduler tick. `gmail_client.py` gets a `fetch_invoice_replies()` method. `main.py` calls the monitor in its post-pipeline block. `unified_agent.py` gets a `mark_invoice_paid` tool and an updated `list_invoices` display.

**Tech Stack:** Python 3.12, `anthropic` (claude-haiku-4-5-20251001 for classification), `gmail_client` (OAuth2), `alert.send_alert`, `pytest`, `unittest.mock`.

---

## File Map

| File | Change |
|---|---|
| `organist_bot/integrations/invoice_generator.py` | Add schema fields, `mark_invoice_paid()`, `save_invoice_field()`, update `mark_invoice_emailed()` and `save_invoice()` |
| `organist_bot/integrations/gmail_client.py` | Add `fetch_invoice_replies()` method |
| `organist_bot/invoice_monitor.py` | **CREATE** — `check_invoice_reminders_and_replies()` |
| `main.py` | Call `invoice_monitor.check_invoice_reminders_and_replies()` in post-pipeline |
| `organist_bot/integrations/unified_agent.py` | Add `mark_invoice_paid` tool + update `list_invoices` display |
| `tests/test_invoice_generator_schema.py` | **CREATE** — schema/persistence tests (separate from browser tests) |
| `tests/test_gmail_client.py` | Extend |
| `tests/test_invoice_monitor.py` | **CREATE** |
| `tests/test_unified_agent.py` | Extend |

---

## Task 1: Invoice schema changes

**Files:**
- Modify: `organist_bot/integrations/invoice_generator.py`
- Create: `tests/test_invoice_generator_schema.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_invoice_generator_schema.py`:

```python
"""Tests for invoice_generator schema changes — no browser required."""

import json
import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from organist_bot.integrations.invoice_generator import (
    INVOICES_FILE,
    load_invoices,
    mark_invoice_emailed,
    mark_invoice_paid,
    save_invoice,
    save_invoice_field,
)


@pytest.fixture(autouse=True)
def tmp_invoices(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "organist_bot.integrations.invoice_generator.INVOICES_FILE",
        tmp_path / "invoices.json",
    )


def _base_invoice(number="INV-2026-001") -> dict:
    return {
        "invoice_number": number,
        "client_key": "stpauls",
        "client_name": "St Paul's",
        "client_email": "stpauls@example.com",
        "client_cc": [],
        "year": 2026,
        "date": "1 June 2026",
        "items": [],
        "total": 150.0,
        "currency": "£",
        "emailed": False,
        "created_at": "2026-06-01T10:00:00",
        "pdf_path": "/tmp/inv.pdf",
    }


class TestSaveInvoiceInitialisesNewFields:
    def test_save_invoice_sets_emailed_at_none(self):
        inv = _base_invoice()
        save_invoice(inv)
        stored = load_invoices()["INV-2026-001"]
        assert stored["emailed_at"] is None

    def test_save_invoice_sets_paid_at_none(self):
        inv = _base_invoice()
        save_invoice(inv)
        stored = load_invoices()["INV-2026-001"]
        assert stored["paid_at"] is None

    def test_save_invoice_sets_reminder_sent_false(self):
        inv = _base_invoice()
        save_invoice(inv)
        stored = load_invoices()["INV-2026-001"]
        assert stored["reminder_sent"] is False

    def test_save_invoice_sets_checked_reply_ids_empty_list(self):
        inv = _base_invoice()
        save_invoice(inv)
        stored = load_invoices()["INV-2026-001"]
        assert stored["checked_reply_ids"] == []


class TestMarkInvoiceEmailed:
    def test_sets_emailed_true_and_emailed_at_timestamp(self):
        save_invoice(_base_invoice())
        mark_invoice_emailed("INV-2026-001")
        stored = load_invoices()["INV-2026-001"]
        assert stored["emailed"] is True
        assert stored["emailed_at"] is not None
        # Should be a valid ISO timestamp
        datetime.datetime.fromisoformat(stored["emailed_at"].replace("Z", "+00:00"))

    def test_emailed_at_is_utc(self):
        save_invoice(_base_invoice())
        mark_invoice_emailed("INV-2026-001")
        stored = load_invoices()["INV-2026-001"]
        assert stored["emailed_at"].endswith("Z")


class TestMarkInvoicePaid:
    def test_sets_paid_at_timestamp(self):
        save_invoice(_base_invoice())
        result = mark_invoice_paid("INV-2026-001")
        assert result is True
        stored = load_invoices()["INV-2026-001"]
        assert stored["paid_at"] is not None
        datetime.datetime.fromisoformat(stored["paid_at"].replace("Z", "+00:00"))

    def test_returns_false_for_unknown_invoice(self):
        result = mark_invoice_paid("INV-9999-999")
        assert result is False

    def test_paid_at_is_utc(self):
        save_invoice(_base_invoice())
        mark_invoice_paid("INV-2026-001")
        stored = load_invoices()["INV-2026-001"]
        assert stored["paid_at"].endswith("Z")


class TestSaveInvoiceField:
    def test_updates_single_field(self):
        save_invoice(_base_invoice())
        save_invoice_field("INV-2026-001", "reminder_sent", True)
        stored = load_invoices()["INV-2026-001"]
        assert stored["reminder_sent"] is True

    def test_updates_list_field(self):
        save_invoice(_base_invoice())
        save_invoice_field("INV-2026-001", "checked_reply_ids", ["msg1", "msg2"])
        stored = load_invoices()["INV-2026-001"]
        assert stored["checked_reply_ids"] == ["msg1", "msg2"]

    def test_unknown_invoice_does_not_raise(self):
        # Should silently do nothing for unknown invoice
        save_invoice_field("INV-9999-999", "reminder_sent", True)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_invoice_generator_schema.py -v
```

Expected: FAIL — `mark_invoice_paid`, `save_invoice_field` not defined; `save_invoice` doesn't set new fields.

- [ ] **Step 3: Add `_now_iso` helper and update `save_invoice` in `invoice_generator.py`**

At the top of `invoice_generator.py`, after the imports, add:

```python
def _now_iso() -> str:
    """Return current UTC time as ISO-8601 string ending in Z."""
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
```

Update `save_invoice` to initialise the four new fields:

```python
def save_invoice(invoice_data: dict) -> None:
    invoices = load_invoices()
    record = {
        **invoice_data,
        "pdf_path": str(invoice_data["pdf_path"]),
        "emailed_at": invoice_data.get("emailed_at", None),
        "paid_at": invoice_data.get("paid_at", None),
        "reminder_sent": invoice_data.get("reminder_sent", False),
        "checked_reply_ids": invoice_data.get("checked_reply_ids", []),
    }
    invoices[invoice_data["invoice_number"]] = record
    with open(INVOICES_FILE, "w") as f:
        json.dump(invoices, f, indent=2)
```

- [ ] **Step 4: Update `mark_invoice_emailed` and add `mark_invoice_paid` and `save_invoice_field`**

Replace `mark_invoice_emailed` and add the two new functions:

```python
def mark_invoice_emailed(invoice_number: str) -> None:
    invoices = load_invoices()
    if invoice_number in invoices:
        invoices[invoice_number]["emailed"] = True
        invoices[invoice_number]["emailed_at"] = _now_iso()
        with open(INVOICES_FILE, "w") as f:
            json.dump(invoices, f, indent=2)


def mark_invoice_paid(invoice_number: str) -> bool:
    """Set paid_at on the matching invoice record. Returns False if not found."""
    invoices = load_invoices()
    if invoice_number not in invoices:
        return False
    invoices[invoice_number]["paid_at"] = _now_iso()
    with open(INVOICES_FILE, "w") as f:
        json.dump(invoices, f, indent=2)
    return True


def save_invoice_field(invoice_number: str, field: str, value: object) -> None:
    """Update a single field on an invoice record. Silently ignores unknown invoice numbers."""
    invoices = load_invoices()
    if invoice_number not in invoices:
        return
    invoices[invoice_number][field] = value
    with open(INVOICES_FILE, "w") as f:
        json.dump(invoices, f, indent=2)
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_invoice_generator_schema.py -v
```

Expected: all tests PASS

- [ ] **Step 6: Commit**

```bash
git add organist_bot/integrations/invoice_generator.py tests/test_invoice_generator_schema.py
git commit -m "feat: add emailed_at, paid_at, reminder_sent fields and mark_invoice_paid to invoice_generator"
```

---

## Task 2: `fetch_invoice_replies` on `GmailClient`

**Files:**
- Modify: `organist_bot/integrations/gmail_client.py`
- Modify: `tests/test_gmail_client.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_gmail_client.py`:

```python
class TestFetchInvoiceReplies:
    def _make_client(self, tmp_path):
        creds_file = tmp_path / "credentials.json"
        creds_file.write_text('{"installed": {}}')
        token_file = tmp_path / "token.json"
        from organist_bot.integrations.gmail_client import GmailClient
        return GmailClient(str(creds_file), str(token_file))

    def test_returns_inbox_replies_from_client_email(self, tmp_path):
        client = self._make_client(tmp_path)
        expected = _make_message_dict(
            "msg1", "client@example.com", "me@example.com",
            "Thank you, payment has been sent.", "incoming"
        )
        with (
            patch.object(client, "_build_service"),
            patch.object(client, "_search_messages", return_value=[{"id": "msg1"}]),
            patch.object(client, "_get_message_details", return_value=expected),
        ):
            result = client.fetch_invoice_replies(
                invoice_number="INV-2026-001",
                client_email="client@example.com",
            )
        assert len(result) == 1
        assert result[0]["message_id"] == "msg1"

    def test_search_query_includes_invoice_number_and_client_email(self, tmp_path):
        client = self._make_client(tmp_path)
        captured_queries = []

        def capture_search(service, query):
            captured_queries.append(query)
            return []

        with (
            patch.object(client, "_build_service"),
            patch.object(client, "_search_messages", side_effect=capture_search),
        ):
            client.fetch_invoice_replies(
                invoice_number="INV-2026-001",
                client_email="client@example.com",
            )

        assert any("INV-2026-001" in q for q in captured_queries)
        assert any("client@example.com" in q for q in captured_queries)

    def test_since_date_appended_to_query(self, tmp_path):
        client = self._make_client(tmp_path)
        captured_queries = []

        def capture_search(service, query):
            captured_queries.append(query)
            return []

        with (
            patch.object(client, "_build_service"),
            patch.object(client, "_search_messages", side_effect=capture_search),
        ):
            client.fetch_invoice_replies(
                invoice_number="INV-2026-001",
                client_email="client@example.com",
                since_date="2026/06/01",
            )

        assert any("2026/06/01" in q for q in captured_queries)

    def test_returns_empty_list_on_api_error(self, tmp_path):
        client = self._make_client(tmp_path)
        with (
            patch.object(client, "_build_service", side_effect=Exception("auth error")),
        ):
            result = client.fetch_invoice_replies("INV-2026-001", "client@example.com")
        assert result == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_gmail_client.py::TestFetchInvoiceReplies -v
```

Expected: FAIL — `fetch_invoice_replies` not defined.

- [ ] **Step 3: Add `fetch_invoice_replies` to `GmailClient`**

Add after `fetch_reply_messages` in `organist_bot/integrations/gmail_client.py`:

```python
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
        service = self._build_service()
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
```

- [ ] **Step 4: Run all gmail client tests**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_gmail_client.py -v
```

Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add organist_bot/integrations/gmail_client.py tests/test_gmail_client.py
git commit -m "feat: add fetch_invoice_replies to GmailClient"
```

---

## Task 3: `invoice_monitor` module

**Files:**
- Create: `organist_bot/invoice_monitor.py`
- Create: `tests/test_invoice_monitor.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_invoice_monitor.py`:

```python
"""Tests for organist_bot.invoice_monitor."""

import datetime
from unittest.mock import MagicMock, call, patch

import pytest

import organist_bot.invoice_monitor as monitor


def _make_invoice(
    number="INV-2026-001",
    emailed=True,
    emailed_at=None,
    paid_at=None,
    reminder_sent=False,
    checked_reply_ids=None,
    client_email="client@example.com",
    client_name="St Paul's",
    total=150.0,
) -> dict:
    if emailed_at is None and emailed:
        # Default: 6 days ago (overdue)
        emailed_at = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=6)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "invoice_number": number,
        "client_name": client_name,
        "client_email": client_email,
        "total": total,
        "emailed": emailed,
        "emailed_at": emailed_at,
        "paid_at": paid_at,
        "reminder_sent": reminder_sent,
        "checked_reply_ids": checked_reply_ids or [],
    }


def _recent_emailed_at() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=2)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestOverdueReminder:
    def test_sends_reminder_for_overdue_invoice(self):
        invoices = {"INV-2026-001": _make_invoice()}
        with (
            patch("organist_bot.invoice_monitor.load_invoices", return_value=invoices),
            patch("organist_bot.invoice_monitor._make_gmail_client", return_value=None),
            patch("organist_bot.invoice_monitor.save_invoice_field") as mock_save,
            patch("organist_bot.invoice_monitor.alert.send_alert") as mock_alert,
        ):
            monitor.check_invoice_reminders_and_replies()

        mock_alert.assert_called_once()
        alert_text = mock_alert.call_args.args[0]
        assert "INV-2026-001" in alert_text
        assert "St Paul's" in alert_text
        assert "£150" in alert_text

    def test_sets_reminder_sent_true_after_sending(self):
        invoices = {"INV-2026-001": _make_invoice()}
        with (
            patch("organist_bot.invoice_monitor.load_invoices", return_value=invoices),
            patch("organist_bot.invoice_monitor._make_gmail_client", return_value=None),
            patch("organist_bot.invoice_monitor.save_invoice_field") as mock_save,
            patch("organist_bot.invoice_monitor.alert.send_alert"),
        ):
            monitor.check_invoice_reminders_and_replies()

        mock_save.assert_any_call("INV-2026-001", "reminder_sent", True)

    def test_does_not_send_when_not_overdue(self):
        invoices = {"INV-2026-001": _make_invoice(emailed_at=_recent_emailed_at())}
        with (
            patch("organist_bot.invoice_monitor.load_invoices", return_value=invoices),
            patch("organist_bot.invoice_monitor._make_gmail_client", return_value=None),
            patch("organist_bot.invoice_monitor.save_invoice_field"),
            patch("organist_bot.invoice_monitor.alert.send_alert") as mock_alert,
        ):
            monitor.check_invoice_reminders_and_replies()

        mock_alert.assert_not_called()

    def test_does_not_send_when_reminder_already_sent(self):
        invoices = {"INV-2026-001": _make_invoice(reminder_sent=True)}
        with (
            patch("organist_bot.invoice_monitor.load_invoices", return_value=invoices),
            patch("organist_bot.invoice_monitor._make_gmail_client", return_value=None),
            patch("organist_bot.invoice_monitor.save_invoice_field"),
            patch("organist_bot.invoice_monitor.alert.send_alert") as mock_alert,
        ):
            monitor.check_invoice_reminders_and_replies()

        mock_alert.assert_not_called()

    def test_does_not_send_when_already_paid(self):
        invoices = {
            "INV-2026-001": _make_invoice(paid_at="2026-06-05T10:00:00Z")
        }
        with (
            patch("organist_bot.invoice_monitor.load_invoices", return_value=invoices),
            patch("organist_bot.invoice_monitor._make_gmail_client", return_value=None),
            patch("organist_bot.invoice_monitor.save_invoice_field"),
            patch("organist_bot.invoice_monitor.alert.send_alert") as mock_alert,
        ):
            monitor.check_invoice_reminders_and_replies()

        mock_alert.assert_not_called()

    def test_does_not_send_when_not_emailed(self):
        invoices = {"INV-2026-001": _make_invoice(emailed=False, emailed_at=None)}
        with (
            patch("organist_bot.invoice_monitor.load_invoices", return_value=invoices),
            patch("organist_bot.invoice_monitor._make_gmail_client", return_value=None),
            patch("organist_bot.invoice_monitor.save_invoice_field"),
            patch("organist_bot.invoice_monitor.alert.send_alert") as mock_alert,
        ):
            monitor.check_invoice_reminders_and_replies()

        mock_alert.assert_not_called()

    def test_does_not_send_reminder_when_no_telegram_alert_but_does_not_set_reminder_sent(self):
        """If alert.send_alert fails, reminder_sent should NOT be set to True."""
        invoices = {"INV-2026-001": _make_invoice()}
        with (
            patch("organist_bot.invoice_monitor.load_invoices", return_value=invoices),
            patch("organist_bot.invoice_monitor._make_gmail_client", return_value=None),
            patch("organist_bot.invoice_monitor.save_invoice_field") as mock_save,
            patch(
                "organist_bot.invoice_monitor.alert.send_alert",
                side_effect=Exception("Telegram down"),
            ),
        ):
            monitor.check_invoice_reminders_and_replies()

        # reminder_sent should NOT be set to True since alert failed
        for c in mock_save.call_args_list:
            if c.args[1] == "reminder_sent":
                assert c.args[2] is not True


class TestReplyMonitoring:
    def test_marks_invoice_paid_on_paid_reply(self):
        invoices = {"INV-2026-001": _make_invoice()}
        reply = {
            "message_id": "msg1",
            "sender": "client@example.com",
            "body": "Hi, I have transferred the payment. Thank you.",
            "direction": "incoming",
        }
        mock_gmail = MagicMock()
        mock_gmail.fetch_invoice_replies.return_value = [reply]

        with (
            patch("organist_bot.invoice_monitor.load_invoices", return_value=invoices),
            patch("organist_bot.invoice_monitor._make_gmail_client", return_value=mock_gmail),
            patch(
                "organist_bot.invoice_monitor._classify_payment_reply", return_value="paid"
            ),
            patch("organist_bot.invoice_monitor.mark_invoice_paid") as mock_paid,
            patch("organist_bot.invoice_monitor.save_invoice_field"),
            patch("organist_bot.invoice_monitor.alert.send_alert") as mock_alert,
        ):
            monitor.check_invoice_reminders_and_replies()

        mock_paid.assert_called_once_with("INV-2026-001")
        mock_alert.assert_called_once()
        assert "paid" in mock_alert.call_args.args[0].lower()

    def test_skips_already_seen_message_ids(self):
        invoices = {
            "INV-2026-001": _make_invoice(checked_reply_ids=["msg1"])
        }
        reply = {
            "message_id": "msg1",
            "sender": "client@example.com",
            "body": "Payment sent.",
            "direction": "incoming",
        }
        mock_gmail = MagicMock()
        mock_gmail.fetch_invoice_replies.return_value = [reply]

        with (
            patch("organist_bot.invoice_monitor.load_invoices", return_value=invoices),
            patch("organist_bot.invoice_monitor._make_gmail_client", return_value=mock_gmail),
            patch("organist_bot.invoice_monitor._classify_payment_reply") as mock_classify,
            patch("organist_bot.invoice_monitor.mark_invoice_paid") as mock_paid,
            patch("organist_bot.invoice_monitor.save_invoice_field"),
            patch("organist_bot.invoice_monitor.alert.send_alert"),
        ):
            monitor.check_invoice_reminders_and_replies()

        mock_classify.assert_not_called()
        mock_paid.assert_not_called()

    def test_does_not_mark_paid_on_unclear_reply(self):
        invoices = {"INV-2026-001": _make_invoice()}
        reply = {
            "message_id": "msg2",
            "sender": "client@example.com",
            "body": "Can you resend the invoice?",
            "direction": "incoming",
        }
        mock_gmail = MagicMock()
        mock_gmail.fetch_invoice_replies.return_value = [reply]

        with (
            patch("organist_bot.invoice_monitor.load_invoices", return_value=invoices),
            patch("organist_bot.invoice_monitor._make_gmail_client", return_value=mock_gmail),
            patch(
                "organist_bot.invoice_monitor._classify_payment_reply", return_value="unclear"
            ),
            patch("organist_bot.invoice_monitor.mark_invoice_paid") as mock_paid,
            patch("organist_bot.invoice_monitor.save_invoice_field"),
            patch("organist_bot.invoice_monitor.alert.send_alert"),
        ):
            monitor.check_invoice_reminders_and_replies()

        mock_paid.assert_not_called()

    def test_appends_new_message_id_to_checked_reply_ids(self):
        invoices = {"INV-2026-001": _make_invoice()}
        reply = {
            "message_id": "msg3",
            "sender": "client@example.com",
            "body": "Thanks, I'll pay next week.",
            "direction": "incoming",
        }
        mock_gmail = MagicMock()
        mock_gmail.fetch_invoice_replies.return_value = [reply]

        saved_fields = {}

        def capture_save(inv_num, field, value):
            saved_fields[(inv_num, field)] = value

        with (
            patch("organist_bot.invoice_monitor.load_invoices", return_value=invoices),
            patch("organist_bot.invoice_monitor._make_gmail_client", return_value=mock_gmail),
            patch(
                "organist_bot.invoice_monitor._classify_payment_reply", return_value="unclear"
            ),
            patch("organist_bot.invoice_monitor.mark_invoice_paid"),
            patch("organist_bot.invoice_monitor.save_invoice_field", side_effect=capture_save),
            patch("organist_bot.invoice_monitor.alert.send_alert"),
        ):
            monitor.check_invoice_reminders_and_replies()

        assert ("INV-2026-001", "checked_reply_ids") in saved_fields
        assert "msg3" in saved_fields[("INV-2026-001", "checked_reply_ids")]

    def test_gmail_failure_does_not_crash(self):
        invoices = {"INV-2026-001": _make_invoice(reminder_sent=True)}
        mock_gmail = MagicMock()
        mock_gmail.fetch_invoice_replies.side_effect = Exception("Gmail down")

        with (
            patch("organist_bot.invoice_monitor.load_invoices", return_value=invoices),
            patch("organist_bot.invoice_monitor._make_gmail_client", return_value=mock_gmail),
            patch("organist_bot.invoice_monitor.save_invoice_field"),
            patch("organist_bot.invoice_monitor.alert.send_alert"),
        ):
            # Should not raise
            monitor.check_invoice_reminders_and_replies()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_invoice_monitor.py -v
```

Expected: `ModuleNotFoundError: No module named 'organist_bot.invoice_monitor'`

- [ ] **Step 3: Create `organist_bot/invoice_monitor.py`**

```python
"""organist_bot/invoice_monitor.py
───────────────────────────────────
Periodic invoice monitoring: overdue reminders and payment reply detection.

check_invoice_reminders_and_replies()
    Called every scheduler tick (main.py post-pipeline).
    Checks all emailed-but-unpaid invoices for:
    1. New Gmail replies → classify with Haiku → mark paid if confirmed
    2. Overdue (5+ days since emailed) → send one-time Telegram reminder
"""

from __future__ import annotations

import datetime
import logging

import anthropic

from organist_bot import alert
from organist_bot.config import settings
from organist_bot.integrations.invoice_generator import (
    load_invoices,
    mark_invoice_paid,
    save_invoice_field,
)

logger = logging.getLogger(__name__)

_OVERDUE_DAYS = 5

_CLASSIFY_PROMPT = """\
Does this email indicate that invoice {invoice_number} has been paid?

<email>
{body}
</email>

Reply with exactly one word: paid / unclear
"""


def _classify_payment_reply(invoice_number: str, body: str) -> str:
    """Classify a reply email as 'paid' or 'unclear' using Claude Haiku."""
    try:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        prompt = _CLASSIFY_PROMPT.format(invoice_number=invoice_number, body=body[:2000])
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content": prompt}],
        )
        block = response.content[0]
        if not isinstance(block, anthropic.types.TextBlock):
            return "unclear"
        result = block.text.strip().lower()
        return result if result == "paid" else "unclear"
    except Exception as exc:
        logger.warning("invoice_monitor: classification failed: %s", exc)
        return "unclear"


def _make_gmail_client():
    """Return a GmailClient if configured, else None."""
    if not settings.gmail_credentials_file:
        return None
    try:
        from organist_bot.integrations.gmail_client import GmailClient
        return GmailClient(settings.gmail_credentials_file, settings.gmail_token_file)
    except Exception as exc:
        logger.warning("invoice_monitor: could not build Gmail client: %s", exc)
        return None


def check_invoice_reminders_and_replies() -> None:
    """Check all emailed-but-unpaid invoices for payment replies and overdue status.

    Called in main.py post-pipeline steps on every scheduler tick.
    Fails open — logs warnings on any per-invoice error and continues.
    """
    invoices = load_invoices()
    now = datetime.datetime.now(datetime.timezone.utc)
    gmail = _make_gmail_client()

    candidates = [
        inv for inv in invoices.values()
        if inv.get("emailed") and not inv.get("paid_at")
    ]

    for inv in candidates:
        inv_num = inv["invoice_number"]
        try:
            _process_invoice(inv, now, gmail)
        except Exception as exc:
            logger.warning("invoice_monitor: error processing %s: %s", inv_num, exc)


def _process_invoice(
    inv: dict,
    now: datetime.datetime,
    gmail,
) -> None:
    """Process a single emailed-but-unpaid invoice."""
    inv_num = inv["invoice_number"]
    client_email = inv.get("client_email", "")
    client_name = inv.get("client_name", inv_num)
    total = inv.get("total", 0.0)
    emailed_at_str = inv.get("emailed_at")
    checked_ids: list[str] = list(inv.get("checked_reply_ids") or [])
    just_paid = False

    # ── Reply check ───────────────────────────────────────────────────────────
    if gmail and client_email:
        since_date = emailed_at_str[:10].replace("-", "/") if emailed_at_str else None
        try:
            replies = gmail.fetch_invoice_replies(
                invoice_number=inv_num,
                client_email=client_email,
                since_date=since_date,
            )
        except Exception as exc:
            logger.warning("invoice_monitor: Gmail fetch failed for %s: %s", inv_num, exc)
            replies = []

        new_ids: list[str] = []
        for msg in replies:
            msg_id = msg.get("message_id", "")
            if msg_id in checked_ids:
                continue
            new_ids.append(msg_id)
            classification = _classify_payment_reply(inv_num, msg.get("body", ""))
            if classification == "paid":
                mark_invoice_paid(inv_num)
                just_paid = True
                try:
                    alert.send_alert(
                        f"✅ Invoice {inv_num} ({client_name}, £{total:.2f})"
                        " marked as paid — reply received."
                    )
                except Exception as exc:
                    logger.warning("invoice_monitor: Telegram alert failed: %s", exc)
                break  # No need to process more replies

        if new_ids:
            save_invoice_field(inv_num, "checked_reply_ids", checked_ids + new_ids)

    if just_paid:
        return

    # ── Overdue check ─────────────────────────────────────────────────────────
    if inv.get("reminder_sent"):
        return
    if not emailed_at_str:
        return

    try:
        emailed_at = datetime.datetime.fromisoformat(emailed_at_str.replace("Z", "+00:00"))
    except ValueError:
        return

    days_since = (now - emailed_at).days
    if days_since < _OVERDUE_DAYS:
        return

    try:
        alert.send_alert(
            f"⏰ Invoice {inv_num} ({client_name}, £{total:.2f})"
            f" was sent {days_since} day{'s' if days_since != 1 else ''} ago"
            " and hasn't been paid."
        )
        save_invoice_field(inv_num, "reminder_sent", True)
    except Exception as exc:
        logger.warning(
            "invoice_monitor: failed to send overdue reminder for %s: %s", inv_num, exc
        )
        # Do NOT set reminder_sent=True — retry on next tick
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_invoice_monitor.py -v
```

Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add organist_bot/invoice_monitor.py tests/test_invoice_monitor.py
git commit -m "feat: add invoice_monitor with overdue reminders and payment reply detection"
```

---

## Task 4: Wire into `main.py`

**Files:**
- Modify: `main.py`

- [ ] **Step 1: Add `invoice_monitor` call after `reply_monitor` in `main.py`**

Find the block that calls `reply_monitor.check_replies()` (around line 285) and add immediately after it:

```python
    try:
        import organist_bot.invoice_monitor as invoice_monitor

        invoice_monitor.check_invoice_reminders_and_replies()
    except Exception as exc:
        alert.send_alert(f"⚠️ invoice_monitor: check failed — {exc}")
        logger.warning("invoice_monitor: check_invoice_reminders_and_replies failed", exc_info=True)
```

- [ ] **Step 2: Run the full test suite to confirm nothing broke**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest --tb=short -q
```

Expected: all tests PASS

- [ ] **Step 3: Commit**

```bash
git add main.py
git commit -m "feat: call invoice_monitor in main.py post-pipeline steps"
```

---

## Task 5: Unified agent `mark_invoice_paid` tool and updated `list_invoices`

**Files:**
- Modify: `organist_bot/integrations/unified_agent.py`
- Modify: `tests/test_unified_agent.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_unified_agent.py`:

```python
class TestMarkInvoicePaidTool:
    async def test_marks_invoice_paid_and_returns_success(self):
        with patch(
            "organist_bot.integrations.unified_agent.mark_invoice_paid", return_value=True
        ) as mock_paid:
            agent = UnifiedAgent()
            result = await agent._execute_tool(
                "mark_invoice_paid", {"invoice_number": "INV-2026-001"}, chat_id=1
            )
        import json
        data = json.loads(result)
        assert "INV-2026-001" in data["result"]
        assert "paid" in data["result"].lower()
        mock_paid.assert_called_once_with("INV-2026-001")

    async def test_returns_error_for_unknown_invoice(self):
        with patch(
            "organist_bot.integrations.unified_agent.mark_invoice_paid", return_value=False
        ):
            agent = UnifiedAgent()
            result = await agent._execute_tool(
                "mark_invoice_paid", {"invoice_number": "INV-9999-999"}, chat_id=1
            )
        import json
        data = json.loads(result)
        assert "error" in data


class TestListInvoicesPaymentStatus:
    async def test_shows_paid_status(self):
        import datetime
        invoices = {
            "INV-2026-001": {
                "invoice_number": "INV-2026-001",
                "client_name": "St Paul's",
                "total": 150.0,
                "currency": "£",
                "date": "1 June 2026",
                "emailed": True,
                "emailed_at": "2026-06-01T10:00:00Z",
                "paid_at": "2026-06-03T10:00:00Z",
                "reminder_sent": False,
            }
        }
        with patch("organist_bot.integrations.unified_agent.load_invoices", return_value=invoices):
            agent = UnifiedAgent()
            result = await agent._execute_tool("list_invoices", {}, chat_id=1)
        import json
        data = json.loads(result)
        assert "paid" in data["result"].lower()

    async def test_shows_overdue_status(self):
        overdue_at = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=7)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        invoices = {
            "INV-2026-001": {
                "invoice_number": "INV-2026-001",
                "client_name": "St Mary's",
                "total": 200.0,
                "currency": "£",
                "date": "1 May 2026",
                "emailed": True,
                "emailed_at": overdue_at,
                "paid_at": None,
                "reminder_sent": False,
            }
        }
        with patch("organist_bot.integrations.unified_agent.load_invoices", return_value=invoices):
            agent = UnifiedAgent()
            result = await agent._execute_tool("list_invoices", {}, chat_id=1)
        import json
        data = json.loads(result)
        assert "overdue" in data["result"].lower()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_unified_agent.py::TestMarkInvoicePaidTool tests/test_unified_agent.py::TestListInvoicesPaymentStatus -v
```

Expected: FAIL

- [ ] **Step 3: Add `mark_invoice_paid` to imports in `unified_agent.py`**

Update the import from `invoice_generator`:

```python
from organist_bot.integrations.invoice_generator import (
    generate_invoice,
    load_invoices,
    mark_invoice_emailed,
    mark_invoice_paid,
    save_invoice,
)
```

- [ ] **Step 4: Add `mark_invoice_paid` to the TOOLS list**

Add after the `list_invoices` tool definition:

```python
        {
            "name": "mark_invoice_paid",
            "description": "Mark an invoice as paid. Use when the user says an invoice has been paid or confirms payment.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "invoice_number": {
                        "type": "string",
                        "description": "The invoice number, e.g. INV-2026-001",
                    }
                },
                "required": ["invoice_number"],
            },
        },
```

- [ ] **Step 5: Add system prompt bullet for `mark_invoice_paid`**

In the system prompt string, after the invoice section, add:

```python
"- \"Mark INV-2026-001 as paid\" / \"invoice has been paid\" → mark_invoice_paid.\n"
```

- [ ] **Step 6: Add the `mark_invoice_paid` handler in `_execute_tool`**

Add before the `list_invoices` handler:

```python
    if name == "mark_invoice_paid":
        inv_num = input_data["invoice_number"]
        ok = mark_invoice_paid(inv_num)
        if not ok:
            return json.dumps({"error": f"Invoice {inv_num} not found."})
        return json.dumps({"result": f"✅ Invoice {inv_num} marked as paid."})
```

- [ ] **Step 7: Update `list_invoices` handler to show richer payment status**

Find the `list_invoices` handler (around line 965). Replace the section that builds the invoice line for each record with:

```python
    if name == "list_invoices":
        invoices = load_invoices()
        if not invoices:
            return json.dumps({"result": "No invoices found."})

        import datetime as _dt
        now = _dt.datetime.now(_dt.timezone.utc)

        def _payment_status(r: dict) -> str:
            if r.get("paid_at"):
                return "✅ paid"
            emailed_at_str = r.get("emailed_at")
            if not r.get("emailed") or not emailed_at_str:
                return "not sent"
            try:
                emailed_at = _dt.datetime.fromisoformat(emailed_at_str.replace("Z", "+00:00"))
                days = (now - emailed_at).days
                if days >= 5:
                    return f"⏰ overdue ({days}d)"
                return f"emailed {days}d ago"
            except ValueError:
                return "emailed"

        records = list(invoices.values())
        records.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        lines = ["📄 Invoices (most recent first)", ""]
        for r in records[:20]:
            status = _payment_status(r)
            lines.append(
                f"{r['invoice_number']}  {r.get('client_name', '?'):<20}"
                f"  £{r.get('total', 0):.2f}  {r.get('date', '?')}  {status}"
            )
        return json.dumps({"result": "\n".join(lines)})
```

- [ ] **Step 8: Run all unified agent tests**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_unified_agent.py -v
```

Expected: all tests PASS

- [ ] **Step 9: Run full test suite**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest --tb=short -q
```

Expected: all tests PASS

- [ ] **Step 10: Commit**

```bash
git add organist_bot/integrations/unified_agent.py tests/test_unified_agent.py
git commit -m "feat: add mark_invoice_paid tool and rich payment status to list_invoices"
```
