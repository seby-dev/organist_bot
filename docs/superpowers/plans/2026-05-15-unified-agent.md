# Unified Telegram Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace all Telegram slash commands and two specialised AI agents with a single unified Claude agent handling every intent through natural language, and fix the Playwright browser cold-start that slows invoice generation.

**Architecture:** A new `unified_agent.py` consolidates gig calendar, invoice, and filter management into one 19-tool agent with a shared conversation history per chat. `telegram_bot.py` becomes a thin dispatcher — one `MessageHandler` routes all free text through `unified_agent.process_message`. The Playwright browser is lifted to a module-level lazy singleton so Chromium only cold-starts once per bot process.

**Tech Stack:** Python 3.12, `python-telegram-bot` 20.x, `anthropic` SDK (AsyncAnthropic), Playwright async API, pytest + pytest-asyncio.

---

## File Map

| Action | Path |
|--------|------|
| New | `organist_bot/integrations/unified_agent.py` |
| New | `tests/test_unified_agent.py` |
| New | `tests/test_invoice_generator_browser.py` |
| Modified | `organist_bot/integrations/invoice_generator.py` |
| Modified | `organist_bot/integrations/telegram_bot.py` |
| Replaced | `tests/test_telegram_integration.py` |
| Deleted | `organist_bot/integrations/gig_agent.py` |
| Deleted | `organist_bot/integrations/invoice_agent.py` |
| Deleted | `tests/test_gig_agent.py` |

---

## Task 1: Fix Playwright browser cold-start

**Files:**
- Modify: `organist_bot/integrations/invoice_generator.py`

The current code does `async with async_playwright() as p: browser = await p.chromium.launch()` on every call. That cold-start costs ~3–5 s. Fix: keep a module-level browser singleton, launching it once and reusing the open process.

- [ ] **Step 1: Write a failing test for the singleton behaviour**

Add to a temporary test or directly to `tests/test_unified_agent.py` (create the file now, tests for the agent tool will be added in later tasks):

```python
# tests/test_unified_agent.py
"""Tests for unified_agent._execute_tool and supporting utilities."""
# (leave empty for now — add content in tasks 3–6)
```

Then add this test to `tests/test_telegram_integration.py` temporarily, or directly write it in a new scratch test. Actually, test it in `invoice_generator` directly. Add a new file `tests/test_invoice_generator_browser.py`:

```python
"""Test that generate_invoice reuses the browser singleton."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_browser_launched_once_across_two_calls():
    """The Chromium browser should be launched only once, not once per invoice."""
    mock_page = AsyncMock()
    mock_page.goto = AsyncMock()
    mock_page.pdf = AsyncMock()
    mock_browser = MagicMock()
    mock_browser.is_connected.return_value = True
    mock_browser.new_page = AsyncMock(return_value=mock_page)
    mock_browser.close = AsyncMock()
    mock_chromium = AsyncMock()
    mock_chromium.launch = AsyncMock(return_value=mock_browser)
    mock_pw = MagicMock()
    mock_pw.chromium = mock_chromium
    mock_pw.stop = AsyncMock()

    with (
        patch("organist_bot.integrations.invoice_generator.async_playwright") as mock_ap,
        patch("organist_bot.integrations.invoice_generator.load_clients", return_value={
            "test-client": {"name": "Test", "address": "1 Road", "email": "t@t.com", "cc": []}
        }),
        patch("organist_bot.integrations.invoice_generator.save_invoice"),
        patch("organist_bot.integrations.invoice_generator.get_next_invoice_number", return_value="INV-2026-001"),
        patch("organist_bot.integrations.invoice_generator.OUTPUT_DIR") as mock_dir,
    ):
        mock_dir.mkdir = MagicMock()
        mock_html_path = MagicMock()
        mock_html_path.resolve.return_value = "/tmp/test.html"
        mock_html_path.write_text = MagicMock()
        mock_html_path.unlink = MagicMock()
        mock_dir.__truediv__ = MagicMock(return_value=mock_html_path)

        mock_ap_instance = AsyncMock()
        mock_ap_instance.start = AsyncMock(return_value=mock_pw)
        mock_ap.return_value = mock_ap_instance

        # Reset the singleton before test
        import organist_bot.integrations.invoice_generator as ig
        ig._browser = None
        ig._pw_instance = None

        from organist_bot.integrations.invoice_generator import generate_invoice
        items = [{"description": "Service", "quantity": 1, "unit_price": 100}]
        await generate_invoice("test-client", items)
        await generate_invoice("test-client", items)

    assert mock_chromium.launch.call_count == 1, "Browser should be launched only once"
```

- [ ] **Step 2: Run the test — expect FAIL**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_invoice_generator_browser.py -v
```

Expected: FAIL (`call_count` will be 2, not 1).

- [ ] **Step 3: Implement the singleton in `invoice_generator.py`**

At the top of `invoice_generator.py`, add module-level globals and a helper:

```python
from playwright.async_api import async_playwright  # move import to top level

_pw_instance = None
_browser = None


async def _get_browser():
    global _pw_instance, _browser
    if _browser is None or not _browser.is_connected():
        _pw_instance = await async_playwright().start()
        _browser = await _pw_instance.chromium.launch()
    return _browser
```

Replace the `async with async_playwright() as p:` block inside `generate_invoice` with:

```python
    browser = await _get_browser()
    page = await browser.new_page()
    await page.goto(f"file://{html_path.resolve()}")
    await page.pdf(path=str(pdf_path), format="A4", print_background=True)
    await page.close()
```

Remove the `browser.close()` call — the browser stays alive. Also remove the `from playwright.async_api import async_playwright` lazy import inside the function (it moves to the top).

- [ ] **Step 4: Run the test — expect PASS**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_invoice_generator_browser.py -v
```

- [ ] **Step 5: Run full suite to check nothing regressed**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest --tb=short -q
```

- [ ] **Step 6: Commit**

```bash
git add organist_bot/integrations/invoice_generator.py tests/test_invoice_generator_browser.py
git commit -m "perf: reuse Playwright browser singleton across invoice generations"
```

---

## Task 2: Create `unified_agent.py` scaffold

**Files:**
- Create: `organist_bot/integrations/unified_agent.py`

Create the module with the full SYSTEM_PROMPT, TOOLS list, state dicts, `AgentResponse` dataclass, and stub implementations for `_execute_tool` and `process_message`. No tests yet — the scaffold just needs to import cleanly.

- [ ] **Step 1: Create `organist_bot/integrations/unified_agent.py`**

```python
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import cast

import anthropic

from organist_bot import filter_store
from organist_bot.config import settings
from organist_bot.integrations.email_sender import send_invoice_email
from organist_bot.integrations.invoice_generator import (
    add_client,
    delete_client,
    edit_client,
    generate_invoice,
    load_clients,
    load_invoices,
    mark_invoice_emailed,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are an assistant for an organist. You handle three areas:

## Gig calendar
- If the user provides a URL, call fetch_gig_details immediately.
- Gather any missing fields (header, organisation, locality, date, time, fee) one at a time.
- Always call add_gig(confirmed=false) first to show a summary; only call confirmed=true after explicit approval.
- "Show my gigs" / "list gigs" → call list_upcoming_gigs.
- "Delete gig 2" → call delete_gig(2). Tell the user to list gigs first if no listing is cached.

## Invoicing
- Confirm before calling generate_invoice, duplicate_invoice, send_invoice_email, resend_invoice, or delete_client. Present a clear summary and ask "Shall I go ahead?"
- If missing required info (client, description, quantity, or unit price), ask for the missing details.
- After generating an invoice, ask if the user wants to email it.
- Use list_clients to look up available client keys when the user mentions a client by name.
- Use list_invoices to look up past invoices when the user mentions a client or date.
- Use £ for money.

## Filter management
- "Add <email> to the blacklist" → manage_blacklist(action=add, email=<email>).
- "Remove <email> from the blacklist" → manage_blacklist(action=remove, email=<email>).
- "I'm unavailable in December" → manage_unavailable(action=add, period=2026-12).
- "I'm unavailable on 25 Dec" → manage_unavailable(action=add, period=2026-12-25).
- "Add an available-only period" → manage_available(action=add, period=<period>).
- Period formats: YYYY-MM-DD, YYYY-MM-DD:YYYY-MM-DD, YYYY-MM.

## Conversation
- If the user asks to start over, reset, or forget everything → call clear_conversation.

## General
- Keep responses concise — this is a chat interface.
- Use British English.
- Use £ for money.
"""

TOOLS: list[dict] = [
    # ── Gig — scraping & calendar add ──────────────────────────────────────
    {
        "name": "fetch_gig_details",
        "description": "Fetch gig details from an organistsonline.org URL.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "The full gig detail URL."}},
            "required": ["url"],
        },
    },
    {
        "name": "add_gig",
        "description": (
            "Two-phase gig calendar tool. "
            "Call with confirmed=false to generate a confirmation summary for the user. "
            "Call with confirmed=true only after the user has explicitly approved. "
            "When calling with confirmed=true, always include all fields shown in the last summary."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "confirmed": {"type": "boolean"},
                "header": {"type": "string"},
                "organisation": {"type": "string"},
                "locality": {"type": "string"},
                "date": {"type": "string", "description": "e.g. 'Sunday 1st June 2025'"},
                "time": {"type": "string", "description": "e.g. '10:30am'"},
                "fee": {"type": "string", "description": "e.g. '£150'"},
            },
            "required": ["confirmed", "header", "date", "time"],
        },
    },
    # ── Gig — calendar management ───────────────────────────────────────────
    {
        "name": "list_upcoming_gigs",
        "description": "List upcoming gigs from Google Calendar. Returns a numbered list.",
        "input_schema": {
            "type": "object",
            "properties": {
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of gigs to return (default 10).",
                }
            },
            "required": [],
        },
    },
    {
        "name": "delete_gig",
        "description": (
            "Delete a gig from Google Calendar by its 1-based position from the last list_upcoming_gigs call. "
            "Also removes the date from unavailable periods."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "number": {"type": "integer", "description": "1-based position from the last gig listing."}
            },
            "required": ["number"],
        },
    },
    # ── Invoice — client management ─────────────────────────────────────────
    {
        "name": "list_clients",
        "description": "List all saved clients with their keys, names, emails, and addresses.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_client",
        "description": "Get full details for a single client by their key.",
        "input_schema": {
            "type": "object",
            "properties": {
                "client_key": {"type": "string", "description": "The client key, e.g. 'holy-cross'"}
            },
            "required": ["client_key"],
        },
    },
    {
        "name": "add_client",
        "description": "Add a new client to the client database.",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "name": {"type": "string"},
                "address": {"type": "string", "description": "Full address (use <br> for line breaks)"},
                "email": {"type": "string"},
                "cc": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["key", "name", "address"],
        },
    },
    {
        "name": "edit_client",
        "description": "Update one or more fields of an existing client. Only provide the fields to change.",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "name": {"type": "string"},
                "address": {"type": "string"},
                "email": {"type": "string"},
                "cc": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["key"],
        },
    },
    {
        "name": "delete_client",
        "description": "Permanently delete a client from the database. Cannot be undone.",
        "input_schema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    },
    # ── Invoice — generation & email ────────────────────────────────────────
    {
        "name": "generate_invoice",
        "description": "Generate a PDF invoice for a client with one or more line items.",
        "input_schema": {
            "type": "object",
            "properties": {
                "client_key": {"type": "string"},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "description": {"type": "string"},
                            "quantity": {"type": "integer"},
                            "unit_price": {"type": "number"},
                        },
                        "required": ["description", "quantity", "unit_price"],
                    },
                },
            },
            "required": ["client_key", "items"],
        },
    },
    {
        "name": "duplicate_invoice",
        "description": "Create a new invoice identical to a previous one with today's date and a new number.",
        "input_schema": {
            "type": "object",
            "properties": {"invoice_number": {"type": "string"}},
            "required": ["invoice_number"],
        },
    },
    {
        "name": "send_invoice_email",
        "description": "Email the most recently generated invoice to the client.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "resend_invoice",
        "description": "Re-email a previously generated invoice by invoice number.",
        "input_schema": {
            "type": "object",
            "properties": {"invoice_number": {"type": "string"}},
            "required": ["invoice_number"],
        },
    },
    {
        "name": "list_invoices",
        "description": "List recent invoices.",
        "input_schema": {
            "type": "object",
            "properties": {
                "client_key": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": [],
        },
    },
    {
        "name": "get_invoice",
        "description": "Retrieve a specific invoice by number and send it as a PDF.",
        "input_schema": {
            "type": "object",
            "properties": {"invoice_number": {"type": "string"}},
            "required": ["invoice_number"],
        },
    },
    # ── Filter management ───────────────────────────────────────────────────
    {
        "name": "manage_blacklist",
        "description": "Manage the organist blacklist. action: list, add, or remove.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "add", "remove"]},
                "email": {"type": "string"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "manage_unavailable",
        "description": "Manage unavailable periods. action: list, add, or remove. period formats: YYYY-MM-DD, YYYY-MM-DD:YYYY-MM-DD, YYYY-MM.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "add", "remove"]},
                "period": {"type": "string"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "manage_available",
        "description": "Manage available-only periods. action: list, add, or remove.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "add", "remove"]},
                "period": {"type": "string"},
            },
            "required": ["action"],
        },
    },
    # ── Meta ────────────────────────────────────────────────────────────────
    {
        "name": "clear_conversation",
        "description": "Clear this chat's conversation history and all cached state.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]


@dataclass
class AgentResponse:
    text: str | None = None
    file_path: str | None = None
    file_caption: str | None = None


# Per-chat state
_histories: dict[int, list[dict]] = {}
_last_invoice: dict[int, dict] = {}
_last_gig_listing: dict[int, list[dict]] = {}

_PDF_RESPONSE_TOOLS = {"generate_invoice", "duplicate_invoice", "get_invoice"}


async def _execute_tool(name: str, input_data: dict, chat_id: int) -> str:
    return json.dumps({"error": f"Tool not implemented: {name}"})


async def process_message(chat_id: int, text: str) -> list[AgentResponse]:
    return [AgentResponse(text="(not implemented)")]


def reset_conversation(chat_id: int) -> None:
    _histories.pop(chat_id, None)
    _last_invoice.pop(chat_id, None)
    _last_gig_listing.pop(chat_id, None)
```

- [ ] **Step 2: Verify it imports cleanly**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  python -c "from organist_bot.integrations.unified_agent import process_message; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit the scaffold**

```bash
git add organist_bot/integrations/unified_agent.py
git commit -m "feat: unified_agent scaffold (stub _execute_tool, full TOOLS + SYSTEM_PROMPT)"
```

---

## Task 3: Implement gig tools + tests

**Files:**
- Modify: `organist_bot/integrations/unified_agent.py` (fill in gig tool handlers)
- Create/modify: `tests/test_unified_agent.py`

Implement `fetch_gig_details`, `add_gig`, `list_upcoming_gigs`, `delete_gig` in `_execute_tool`. Port the tests from `test_gig_agent.py` and add new ones for list/delete.

- [ ] **Step 1: Write failing tests for gig tools**

Create `tests/test_unified_agent.py`:

```python
"""Tests for unified_agent._execute_tool."""
import datetime
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from organist_bot.integrations.unified_agent import _execute_tool, reset_conversation, _last_gig_listing

CHAT_ID = 42

_GIG_INPUT_BASE = {
    "confirmed": False,
    "header": "Sunday Service",
    "organisation": "St Mary's",
    "locality": "Oxford",
    "date": "Sunday 1st June 2025",
    "time": "10:30am",
    "fee": "£150",
}


# ── add_gig (confirmed=false) ─────────────────────────────────────────────────

class TestAddGigPreview:
    @pytest.mark.asyncio
    async def test_returns_summary_with_all_fields(self):
        result = await _execute_tool("add_gig", _GIG_INPUT_BASE, CHAT_ID)
        for value in ["Sunday Service", "St Mary's", "Oxford", "Sunday 1st June 2025", "10:30am", "£150"]:
            assert value in result

    @pytest.mark.asyncio
    async def test_does_not_touch_calendar(self):
        with patch("organist_bot.integrations.unified_agent._make_calendar_client") as mock_factory:
            await _execute_tool("add_gig", _GIG_INPUT_BASE, CHAT_ID)
        mock_factory.assert_not_called()

    @pytest.mark.asyncio
    async def test_result_key_absent(self):
        result = await _execute_tool("add_gig", _GIG_INPUT_BASE, CHAT_ID)
        assert '"result"' not in result


# ── add_gig (confirmed=true) ──────────────────────────────────────────────────

class TestAddGigConfirmed:
    @pytest.mark.asyncio
    async def test_writes_to_calendar_and_returns_result(self):
        input_data = {**_GIG_INPUT_BASE, "confirmed": True}
        with patch("organist_bot.integrations.unified_agent._make_calendar_client") as mock_factory:
            mock_cal = MagicMock()
            mock_cal.add_gig.return_value = "evt_abc123"
            mock_factory.return_value = mock_cal
            result = await _execute_tool("add_gig", input_data, CHAT_ID)
        data = json.loads(result)
        assert "result" in data
        assert "evt_abc123" in data["result"]

    @pytest.mark.asyncio
    async def test_no_calendar_config_returns_error(self):
        input_data = {**_GIG_INPUT_BASE, "confirmed": True}
        with patch("organist_bot.integrations.unified_agent._make_calendar_client", return_value=None):
            result = await _execute_tool("add_gig", input_data, CHAT_ID)
        assert "error" in json.loads(result)

    @pytest.mark.asyncio
    async def test_calendar_exception_returns_error(self):
        input_data = {**_GIG_INPUT_BASE, "confirmed": True}
        with patch("organist_bot.integrations.unified_agent._make_calendar_client") as mock_factory:
            mock_cal = MagicMock()
            mock_cal.add_gig.side_effect = RuntimeError("calendar down")
            mock_factory.return_value = mock_cal
            result = await _execute_tool("add_gig", input_data, CHAT_ID)
        data = json.loads(result)
        assert "error" in data
        assert "calendar down" in data["error"]


# ── add_gig auto-unavailable ──────────────────────────────────────────────────

class TestAddGigAutoUnavailable:
    @pytest.mark.asyncio
    async def test_adds_date_to_unavailable_on_success(self):
        input_data = {**_GIG_INPUT_BASE, "confirmed": True}
        with (
            patch("organist_bot.integrations.unified_agent._make_calendar_client") as mock_factory,
            patch("organist_bot.integrations.unified_agent.filter_store") as mock_fs,
        ):
            mock_cal = MagicMock()
            mock_cal.add_gig.return_value = "evt_xyz"
            mock_factory.return_value = mock_cal
            await _execute_tool("add_gig", input_data, CHAT_ID)
        mock_fs.add_period.assert_called_once_with("unavailable_periods", "2025-06-01")

    @pytest.mark.asyncio
    async def test_calendar_failure_does_not_call_add_period(self):
        input_data = {**_GIG_INPUT_BASE, "confirmed": True}
        with (
            patch("organist_bot.integrations.unified_agent._make_calendar_client") as mock_factory,
            patch("organist_bot.integrations.unified_agent.filter_store") as mock_fs,
        ):
            mock_cal = MagicMock()
            mock_cal.add_gig.side_effect = RuntimeError("down")
            mock_factory.return_value = mock_cal
            await _execute_tool("add_gig", input_data, CHAT_ID)
        mock_fs.add_period.assert_not_called()

    @pytest.mark.asyncio
    async def test_unparseable_date_does_not_raise(self):
        input_data = {**_GIG_INPUT_BASE, "confirmed": True, "date": "sometime in June"}
        with (
            patch("organist_bot.integrations.unified_agent._make_calendar_client") as mock_factory,
            patch("organist_bot.integrations.unified_agent.filter_store") as mock_fs,
        ):
            mock_cal = MagicMock()
            mock_cal.add_gig.return_value = "evt_abc"
            mock_factory.return_value = mock_cal
            result = await _execute_tool("add_gig", input_data, CHAT_ID)
        assert "result" in json.loads(result)
        mock_fs.add_period.assert_not_called()


# ── list_upcoming_gigs ────────────────────────────────────────────────────────

def _make_event(n: int) -> dict:
    return {
        "id": f"evt{n}",
        "summary": f"Sunday Service {n}",
        "start_dt": datetime.datetime(2026, 6, n, 10, 30, tzinfo=datetime.UTC),
        "date_str": f"2026-06-0{n}",
    }


class TestListUpcomingGigs:
    @pytest.mark.asyncio
    async def test_returns_numbered_gig_list(self):
        events = [_make_event(1), _make_event(2)]
        with patch("organist_bot.integrations.unified_agent._make_calendar_client") as mock_factory:
            mock_cal = MagicMock()
            mock_cal.list_upcoming_events.return_value = events
            mock_factory.return_value = mock_cal
            result = await _execute_tool("list_upcoming_gigs", {}, CHAT_ID)
        assert "Sunday Service 1" in result
        assert "Sunday Service 2" in result

    @pytest.mark.asyncio
    async def test_stores_events_in_last_gig_listing(self):
        events = [_make_event(1)]
        _last_gig_listing.pop(CHAT_ID, None)
        with patch("organist_bot.integrations.unified_agent._make_calendar_client") as mock_factory:
            mock_cal = MagicMock()
            mock_cal.list_upcoming_events.return_value = events
            mock_factory.return_value = mock_cal
            await _execute_tool("list_upcoming_gigs", {}, CHAT_ID)
        assert _last_gig_listing[CHAT_ID] == events

    @pytest.mark.asyncio
    async def test_no_calendar_returns_error(self):
        with patch("organist_bot.integrations.unified_agent._make_calendar_client", return_value=None):
            result = await _execute_tool("list_upcoming_gigs", {}, CHAT_ID)
        assert "error" in result.lower() or "not configured" in result.lower()

    @pytest.mark.asyncio
    async def test_empty_calendar_says_no_gigs(self):
        with patch("organist_bot.integrations.unified_agent._make_calendar_client") as mock_factory:
            mock_cal = MagicMock()
            mock_cal.list_upcoming_events.return_value = []
            mock_factory.return_value = mock_cal
            result = await _execute_tool("list_upcoming_gigs", {}, CHAT_ID)
        assert "no" in result.lower() or "0" in result


# ── delete_gig ────────────────────────────────────────────────────────────────

class TestDeleteGig:
    @pytest.fixture(autouse=True)
    def seed_listing(self):
        _last_gig_listing[CHAT_ID] = [_make_event(1), _make_event(2)]
        yield
        _last_gig_listing.pop(CHAT_ID, None)

    @pytest.mark.asyncio
    async def test_deletes_event_and_returns_confirmation(self):
        with (
            patch("organist_bot.integrations.unified_agent._make_calendar_client") as mock_factory,
            patch("organist_bot.integrations.unified_agent.filter_store"),
        ):
            mock_cal = MagicMock()
            mock_factory.return_value = mock_cal
            result = await _execute_tool("delete_gig", {"number": 1}, CHAT_ID)
        mock_cal.delete_event.assert_called_once_with("evt1")
        assert "Sunday Service 1" in result or "result" in result.lower()

    @pytest.mark.asyncio
    async def test_removes_date_from_unavailable(self):
        with (
            patch("organist_bot.integrations.unified_agent._make_calendar_client") as mock_factory,
            patch("organist_bot.integrations.unified_agent.filter_store") as mock_fs,
        ):
            mock_cal = MagicMock()
            mock_factory.return_value = mock_cal
            await _execute_tool("delete_gig", {"number": 1}, CHAT_ID)
        mock_fs.remove_period.assert_called_once_with("unavailable_periods", "2026-06-01")

    @pytest.mark.asyncio
    async def test_no_listing_returns_error(self):
        _last_gig_listing.pop(CHAT_ID, None)
        result = await _execute_tool("delete_gig", {"number": 1}, CHAT_ID)
        data = json.loads(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_out_of_range_returns_error(self):
        result = await _execute_tool("delete_gig", {"number": 99}, CHAT_ID)
        data = json.loads(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_no_calendar_config_returns_error(self):
        with patch("organist_bot.integrations.unified_agent._make_calendar_client", return_value=None):
            result = await _execute_tool("delete_gig", {"number": 1}, CHAT_ID)
        data = json.loads(result)
        assert "error" in data
```

- [ ] **Step 2: Run tests — expect FAIL**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_unified_agent.py -v
```

Expected: all tests FAIL with "Tool not implemented".

- [ ] **Step 3: Implement gig tools in `_execute_tool`**

Replace the stub `_execute_tool` body with the gig tool handlers. Add the following imports at the top of `unified_agent.py`:

```python
import datetime

from organist_bot.filters import normalize_to_yyyymmdd
from organist_bot.integrations.calendar_client import GoogleCalendarClient
from organist_bot.models import Gig
from organist_bot.scraper import Scraper
```

Add a helper function:

```python
def _make_calendar_client() -> "GoogleCalendarClient | None":
    if settings.google_calendar_id and settings.google_calendar_credentials_file:
        return GoogleCalendarClient(
            credentials_file=settings.google_calendar_credentials_file,
            calendar_id=settings.google_calendar_id,
        )
    return None
```

Implement `_execute_tool` with the gig cases:

```python
async def _execute_tool(name: str, input_data: dict, chat_id: int) -> str:
    # ── fetch_gig_details ───────────────────────────────────────────────────
    if name == "fetch_gig_details":
        try:
            scraper = Scraper()
            html = scraper.fetch(input_data["url"])
            basic = scraper.extract_basic_from_detail(html, input_data["url"])
            full = scraper.extract_full_details(html)
            details = {**basic, **full}
            scraper.session.close()
            return json.dumps({k: v for k, v in details.items() if v is not None})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    # ── add_gig ─────────────────────────────────────────────────────────────
    if name == "add_gig":
        confirmed = input_data.get("confirmed", False)
        fields = {
            "header": input_data.get("header", ""),
            "organisation": input_data.get("organisation") or "",
            "locality": input_data.get("locality") or "",
            "date": input_data.get("date", ""),
            "time": input_data.get("time", ""),
            "fee": input_data.get("fee") or "not specified",
        }
        if not confirmed:
            return (
                "*Please confirm the following gig:*\n"
                f"• *Title:* {fields['header']}\n"
                f"• *Organisation:* {fields['organisation']}\n"
                f"• *Locality:* {fields['locality']}\n"
                f"• *Date:* {fields['date']}\n"
                f"• *Time:* {fields['time']}\n"
                f"• *Fee:* {fields['fee']}\n\n"
                "Reply *yes* to add to calendar, or tell me what to change."
            )
        try:
            cal = _make_calendar_client()
            if cal is None:
                return json.dumps({"error": "Google Calendar not configured."})
            gig = Gig(
                header=fields["header"],
                organisation=fields["organisation"],
                locality=fields["locality"],
                date=fields["date"],
                time=fields["time"],
                fee=fields["fee"] if fields["fee"] != "not specified" else None,
                link="",
            )
            event_id = cal.add_gig(gig)
            yyyymmdd = normalize_to_yyyymmdd(fields["date"])
            if yyyymmdd:
                try:
                    date_str = datetime.datetime.strptime(yyyymmdd, "%Y%m%d").strftime("%Y-%m-%d")
                    filter_store.add_period("unavailable_periods", date_str)
                except Exception:
                    logger.warning("Failed to add gig date to unavailable periods", extra={"date": fields["date"]})
            return json.dumps({"result": f"Added to calendar. Event ID: {event_id}"})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    # ── list_upcoming_gigs ──────────────────────────────────────────────────
    if name == "list_upcoming_gigs":
        cal = _make_calendar_client()
        if cal is None:
            return json.dumps({"error": "Google Calendar not configured."})
        max_results = input_data.get("max_results", 10)
        events = cal.list_upcoming_events(max_results=max_results)
        _last_gig_listing[chat_id] = events
        if not events:
            return json.dumps({"result": "No upcoming gigs found."})
        lines = []
        for i, ev in enumerate(events, start=1):
            start_dt = ev["start_dt"]
            time_str = start_dt.strftime("%I:%M%p").lstrip("0").lower()
            date_str = start_dt.strftime("%a %d %b %Y").replace(" 0", " ")
            lines.append(f"{i}. {ev['summary']} · {date_str} · {time_str}")
        return json.dumps({"result": "\n".join(lines)})

    # ── delete_gig ──────────────────────────────────────────────────────────
    if name == "delete_gig":
        n = input_data["number"]
        listing = _last_gig_listing.get(chat_id)
        if not listing:
            return json.dumps({"error": "No gig listing cached. Ask me to list your gigs first."})
        if n < 1 or n > len(listing):
            return json.dumps({"error": f"No gig number {n}. There are {len(listing)} gigs in the last listing."})
        cal = _make_calendar_client()
        if cal is None:
            return json.dumps({"error": "Google Calendar not configured."})
        event = listing[n - 1]
        try:
            cal.delete_event(event["id"])
        except Exception as exc:
            return json.dumps({"error": str(exc)})
        filter_store.remove_period("unavailable_periods", event["date_str"])
        _last_gig_listing[chat_id] = [e for i, e in enumerate(listing) if i != n - 1]
        return json.dumps({"result": f"Deleted {event['summary']}. Date removed from unavailable if present."})

    return json.dumps({"error": f"Tool not implemented: {name}"})
```

- [ ] **Step 4: Run gig tool tests — expect PASS**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_unified_agent.py -v
```

- [ ] **Step 5: Commit**

```bash
git add organist_bot/integrations/unified_agent.py tests/test_unified_agent.py
git commit -m "feat: gig tools in unified_agent (fetch, add_gig, list, delete)"
```

---

## Task 4: Implement invoice client tools + tests

**Files:**
- Modify: `organist_bot/integrations/unified_agent.py`
- Modify: `tests/test_unified_agent.py`

- [ ] **Step 1: Write failing tests for invoice client tools**

Append to `tests/test_unified_agent.py`:

```python
# ── Invoice client tools ──────────────────────────────────────────────────────

class TestInvoiceClientTools:
    @pytest.mark.asyncio
    async def test_list_clients_returns_all(self):
        clients = {"holy-cross": {"name": "The Secretary", "address": "1 Road", "email": "a@b.com", "cc": []}}
        with patch("organist_bot.integrations.unified_agent.load_clients", return_value=clients):
            result = await _execute_tool("list_clients", {}, CHAT_ID)
        assert "holy-cross" in result

    @pytest.mark.asyncio
    async def test_list_clients_empty_message(self):
        with patch("organist_bot.integrations.unified_agent.load_clients", return_value={}):
            result = await _execute_tool("list_clients", {}, CHAT_ID)
        assert "no clients" in result.lower()

    @pytest.mark.asyncio
    async def test_get_client_found(self):
        clients = {"st-marys": {"name": "St Mary's", "address": "1 Church St", "email": "c@d.com", "cc": []}}
        with patch("organist_bot.integrations.unified_agent.load_clients", return_value=clients):
            result = await _execute_tool("get_client", {"client_key": "st-marys"}, CHAT_ID)
        assert "St Mary's" in result

    @pytest.mark.asyncio
    async def test_get_client_not_found(self):
        with patch("organist_bot.integrations.unified_agent.load_clients", return_value={}):
            result = await _execute_tool("get_client", {"client_key": "missing"}, CHAT_ID)
        data = json.loads(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_add_client_calls_add_client(self):
        with patch("organist_bot.integrations.unified_agent.add_client") as mock_add:
            result = await _execute_tool(
                "add_client",
                {"key": "new-key", "name": "New Client", "address": "2 Road"},
                CHAT_ID,
            )
        mock_add.assert_called_once_with(key="new-key", name="New Client", address="2 Road", email="", cc=[])
        assert "added" in result.lower()

    @pytest.mark.asyncio
    async def test_edit_client_calls_edit_client(self):
        with patch("organist_bot.integrations.unified_agent.edit_client") as mock_edit:
            result = await _execute_tool("edit_client", {"key": "st-marys", "email": "new@email.com"}, CHAT_ID)
        mock_edit.assert_called_once_with(key="st-marys", name=None, address=None, email="new@email.com", cc=None)
        assert "updated" in result.lower()

    @pytest.mark.asyncio
    async def test_edit_client_not_found(self):
        with patch("organist_bot.integrations.unified_agent.edit_client", side_effect=ValueError("not found")):
            result = await _execute_tool("edit_client", {"key": "missing"}, CHAT_ID)
        data = json.loads(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_delete_client_calls_delete_client(self):
        with patch("organist_bot.integrations.unified_agent.delete_client") as mock_del:
            result = await _execute_tool("delete_client", {"key": "old-key"}, CHAT_ID)
        mock_del.assert_called_once_with("old-key")
        assert "deleted" in result.lower()

    @pytest.mark.asyncio
    async def test_delete_client_not_found(self):
        with patch("organist_bot.integrations.unified_agent.delete_client", side_effect=ValueError("not found")):
            result = await _execute_tool("delete_client", {"key": "missing"}, CHAT_ID)
        data = json.loads(result)
        assert "error" in data
```

- [ ] **Step 2: Run tests — expect FAIL on invoice client tests**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_unified_agent.py::TestInvoiceClientTools -v
```

- [ ] **Step 3: Implement invoice client tool handlers**

Add these cases to `_execute_tool` (before the final `return json.dumps({"error": ...})`):

```python
    # ── list_clients ────────────────────────────────────────────────────────
    if name == "list_clients":
        clients = load_clients()
        if not clients:
            return json.dumps({"result": "No clients found. Add one with a natural language request."})
        return json.dumps(clients, indent=2)

    if name == "get_client":
        clients = load_clients()
        key = input_data["client_key"]
        if key not in clients:
            return json.dumps({"error": f"Client '{key}' not found. Available: {', '.join(clients.keys())}"})
        return json.dumps({key: clients[key]}, indent=2)

    if name == "add_client":
        add_client(
            key=input_data["key"],
            name=input_data["name"],
            address=input_data["address"],
            email=input_data.get("email", ""),
            cc=input_data.get("cc", []),
        )
        return json.dumps({"result": f"Client '{input_data['key']}' added successfully."})

    if name == "edit_client":
        try:
            edit_client(
                key=input_data["key"],
                name=input_data.get("name"),
                address=input_data.get("address"),
                email=input_data.get("email"),
                cc=input_data.get("cc"),
            )
            return json.dumps({"result": f"Client '{input_data['key']}' updated successfully."})
        except ValueError as e:
            return json.dumps({"error": str(e)})

    if name == "delete_client":
        try:
            delete_client(input_data["key"])
            return json.dumps({"result": f"Client '{input_data['key']}' deleted."})
        except ValueError as e:
            return json.dumps({"error": str(e)})
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_unified_agent.py -v
```

- [ ] **Step 5: Commit**

```bash
git add organist_bot/integrations/unified_agent.py tests/test_unified_agent.py
git commit -m "feat: invoice client tools in unified_agent"
```

---

## Task 5: Implement invoice generation/email tools + tests

**Files:**
- Modify: `organist_bot/integrations/unified_agent.py`
- Modify: `tests/test_unified_agent.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_unified_agent.py`:

```python
# ── Invoice generation & email tools ─────────────────────────────────────────

class TestInvoiceGenerationTools:
    @pytest.fixture(autouse=True)
    def reset_state(self):
        from organist_bot.integrations.unified_agent import _last_invoice
        _last_invoice.pop(CHAT_ID, None)
        yield
        _last_invoice.pop(CHAT_ID, None)

    @pytest.mark.asyncio
    async def test_generate_invoice_stores_in_last_invoice(self):
        fake_result = {
            "pdf_path": "/tmp/inv.pdf", "client_key": "a", "client_name": "A",
            "client_email": "a@a.com", "client_cc": [], "invoice_number": "INV-2026-001",
            "year": 2026, "date": "1 Jan 2026", "items": [], "total": 100.0,
            "currency": "£", "emailed": False, "created_at": "2026-01-01T00:00:00",
        }
        with patch("organist_bot.integrations.unified_agent.generate_invoice", new=AsyncMock(return_value=fake_result)):
            result = await _execute_tool(
                "generate_invoice",
                {"client_key": "a", "items": [{"description": "S", "quantity": 1, "unit_price": 100}]},
                CHAT_ID,
            )
        data = json.loads(result)
        assert data["invoice_number"] == "INV-2026-001"
        from organist_bot.integrations.unified_agent import _last_invoice
        assert _last_invoice[CHAT_ID]["invoice_number"] == "INV-2026-001"

    @pytest.mark.asyncio
    async def test_send_invoice_email_no_invoice_returns_error(self):
        result = await _execute_tool("send_invoice_email", {}, CHAT_ID)
        data = json.loads(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_send_invoice_email_sends_and_marks_emailed(self):
        from organist_bot.integrations.unified_agent import _last_invoice
        _last_invoice[CHAT_ID] = {
            "invoice_number": "INV-2026-001", "client_email": "a@a.com",
            "client_cc": [], "pdf_path": "/tmp/inv.pdf",
        }
        with (
            patch("organist_bot.integrations.unified_agent.send_invoice_email", return_value={"success": True}) as mock_send,
            patch("organist_bot.integrations.unified_agent.mark_invoice_emailed") as mock_mark,
        ):
            result = await _execute_tool("send_invoice_email", {}, CHAT_ID)
        mock_send.assert_called_once()
        mock_mark.assert_called_once_with("INV-2026-001")
        assert "a@a.com" in result

    @pytest.mark.asyncio
    async def test_list_invoices_returns_summary(self):
        invoices = {
            "INV-2026-001": {
                "invoice_number": "INV-2026-001", "client_key": "a", "client_name": "A",
                "total": 100.0, "date": "1 Jan 2026", "currency": "£",
                "emailed": False, "created_at": "2026-01-01T00:00:00",
            }
        }
        with patch("organist_bot.integrations.unified_agent.load_invoices", return_value=invoices):
            result = await _execute_tool("list_invoices", {}, CHAT_ID)
        assert "INV-2026-001" in result
```

- [ ] **Step 2: Run tests — expect FAIL**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_unified_agent.py::TestInvoiceGenerationTools -v
```

- [ ] **Step 3: Implement invoice generation/email tool handlers**

Copy **only the tool handler `if/elif` blocks** for `generate_invoice`, `duplicate_invoice`, `send_invoice_email`, `resend_invoice`, `list_invoices`, and `get_invoice` from `invoice_agent.py:_execute_tool` into `unified_agent.py:_execute_tool`. Do NOT copy `process_message` or `reset_conversation` from `invoice_agent.py` — those are already implemented in `unified_agent.py` using `AsyncAnthropic`. The only difference from the original handlers: `chat_id` is already a parameter in the unified `_execute_tool` signature, so no changes to the handler bodies are needed.

- [ ] **Step 4: Run all tests — expect PASS**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_unified_agent.py -v
```

- [ ] **Step 5: Commit**

```bash
git add organist_bot/integrations/unified_agent.py tests/test_unified_agent.py
git commit -m "feat: invoice generation and email tools in unified_agent"
```

---

## Task 6: Implement filter tools, clear_conversation, and process_message loop

**Files:**
- Modify: `organist_bot/integrations/unified_agent.py`
- Modify: `tests/test_unified_agent.py`

- [ ] **Step 1: Write failing tests for filter tools and clear_conversation**

Append to `tests/test_unified_agent.py`:

```python
# ── Filter management tools ───────────────────────────────────────────────────

class TestFilterTools:
    @pytest.mark.asyncio
    async def test_manage_blacklist_list(self):
        with patch("organist_bot.integrations.unified_agent.filter_store") as mock_fs:
            mock_fs.blacklist_emails.return_value = ["bad@evil.com"]
            result = await _execute_tool("manage_blacklist", {"action": "list"}, CHAT_ID)
        assert "bad@evil.com" in result

    @pytest.mark.asyncio
    async def test_manage_blacklist_add(self):
        with patch("organist_bot.integrations.unified_agent.filter_store") as mock_fs:
            mock_fs.add_blacklist_email.return_value = True
            result = await _execute_tool("manage_blacklist", {"action": "add", "email": "x@y.com"}, CHAT_ID)
        mock_fs.add_blacklist_email.assert_called_once_with("x@y.com")
        assert "added" in result.lower()

    @pytest.mark.asyncio
    async def test_manage_blacklist_remove(self):
        with patch("organist_bot.integrations.unified_agent.filter_store") as mock_fs:
            mock_fs.remove_blacklist_email.return_value = True
            result = await _execute_tool("manage_blacklist", {"action": "remove", "email": "x@y.com"}, CHAT_ID)
        mock_fs.remove_blacklist_email.assert_called_once_with("x@y.com")
        assert "removed" in result.lower()

    @pytest.mark.asyncio
    async def test_manage_unavailable_add(self):
        with patch("organist_bot.integrations.unified_agent.filter_store") as mock_fs:
            mock_fs.add_period.return_value = True
            result = await _execute_tool("manage_unavailable", {"action": "add", "period": "2026-12"}, CHAT_ID)
        mock_fs.add_period.assert_called_once_with("unavailable_periods", "2026-12")
        assert "unavailable" in result.lower()

    @pytest.mark.asyncio
    async def test_manage_unavailable_remove(self):
        with patch("organist_bot.integrations.unified_agent.filter_store") as mock_fs:
            mock_fs.remove_period.return_value = True
            result = await _execute_tool("manage_unavailable", {"action": "remove", "period": "2026-12"}, CHAT_ID)
        mock_fs.remove_period.assert_called_once_with("unavailable_periods", "2026-12")

    @pytest.mark.asyncio
    async def test_manage_unavailable_list(self):
        with patch("organist_bot.integrations.unified_agent.filter_store") as mock_fs:
            mock_fs.unavailable_periods.return_value = ["2026-12", "2027-01-01"]
            result = await _execute_tool("manage_unavailable", {"action": "list"}, CHAT_ID)
        assert "2026-12" in result

    @pytest.mark.asyncio
    async def test_manage_available_add(self):
        with patch("organist_bot.integrations.unified_agent.filter_store") as mock_fs:
            mock_fs.add_period.return_value = True
            result = await _execute_tool("manage_available", {"action": "add", "period": "2026-08"}, CHAT_ID)
        mock_fs.add_period.assert_called_once_with("available_only_periods", "2026-08")


# ── clear_conversation ────────────────────────────────────────────────────────

class TestClearConversation:
    @pytest.mark.asyncio
    async def test_clears_all_three_dicts(self):
        from organist_bot.integrations.unified_agent import _histories, _last_invoice, _last_gig_listing
        _histories[CHAT_ID] = [{"role": "user", "content": "hello"}]
        _last_invoice[CHAT_ID] = {"invoice_number": "INV-2026-001"}
        _last_gig_listing[CHAT_ID] = [{"id": "evt1"}]

        result = await _execute_tool("clear_conversation", {}, CHAT_ID)

        assert CHAT_ID not in _histories
        assert CHAT_ID not in _last_invoice
        assert CHAT_ID not in _last_gig_listing
        assert "cleared" in result.lower()
```

- [ ] **Step 2: Run tests — expect FAIL**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_unified_agent.py::TestFilterTools tests/test_unified_agent.py::TestClearConversation -v
```

- [ ] **Step 3: Implement filter tools and clear_conversation in `_execute_tool`**

Add these cases:

```python
    # ── manage_blacklist ────────────────────────────────────────────────────
    if name == "manage_blacklist":
        action = input_data["action"]
        if action == "list":
            emails = filter_store.blacklist_emails()
            return json.dumps({"blacklist": emails}) if emails else json.dumps({"result": "Blacklist is empty."})
        email = input_data.get("email", "")
        if action == "add":
            added = filter_store.add_blacklist_email(email)
            msg = f"Added '{email}' to blacklist." if added else f"'{email}' is already in the blacklist."
            return json.dumps({"result": msg})
        if action == "remove":
            removed = filter_store.remove_blacklist_email(email)
            msg = f"Removed '{email}' from blacklist." if removed else f"'{email}' not found in blacklist."
            return json.dumps({"result": msg})
        return json.dumps({"error": f"Unknown action: {action}"})

    # ── manage_unavailable ──────────────────────────────────────────────────
    if name == "manage_unavailable":
        action = input_data["action"]
        if action == "list":
            periods = filter_store.unavailable_periods()
            return json.dumps({"unavailable_periods": periods}) if periods else json.dumps({"result": "No unavailable periods set."})
        period = input_data.get("period", "")
        if action == "add":
            added = filter_store.add_period("unavailable_periods", period)
            msg = f"Marked '{period}' as unavailable." if added else f"'{period}' already in unavailable list."
            return json.dumps({"result": msg})
        if action == "remove":
            removed = filter_store.remove_period("unavailable_periods", period)
            msg = f"Removed '{period}' from unavailable periods." if removed else f"'{period}' not found."
            return json.dumps({"result": msg})
        return json.dumps({"error": f"Unknown action: {action}"})

    # ── manage_available ────────────────────────────────────────────────────
    if name == "manage_available":
        action = input_data["action"]
        if action == "list":
            periods = filter_store.available_only_periods()
            return json.dumps({"available_only_periods": periods}) if periods else json.dumps({"result": "No available-only periods set."})
        period = input_data.get("period", "")
        if action == "add":
            added = filter_store.add_period("available_only_periods", period)
            msg = f"Added '{period}' to available-only periods." if added else f"'{period}' already present."
            return json.dumps({"result": msg})
        if action == "remove":
            removed = filter_store.remove_period("available_only_periods", period)
            msg = f"Removed '{period}' from available-only periods." if removed else f"'{period}' not found."
            return json.dumps({"result": msg})
        return json.dumps({"error": f"Unknown action: {action}"})

    # ── clear_conversation ──────────────────────────────────────────────────
    if name == "clear_conversation":
        reset_conversation(chat_id)
        return json.dumps({"result": "Conversation cleared."})
```

- [ ] **Step 4: Implement `process_message` loop**

Replace the stub `process_message` with the full agentic loop (mirrors `gig_agent.process_message` using `AsyncAnthropic`):

```python
async def process_message(chat_id: int, text: str) -> list[AgentResponse]:
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    if chat_id not in _histories:
        _histories[chat_id] = []

    _histories[chat_id].append({"role": "user", "content": text})

    responses: list[AgentResponse] = []

    while True:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=TOOLS,  # type: ignore[arg-type]
            messages=_histories[chat_id],  # type: ignore[arg-type]
        )

        _histories[chat_id].append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    responses.append(AgentResponse(text=block.text))
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            logger.info("Unified agent tool call: %s(%s)", block.name, json.dumps(block.input))
            try:
                result = await _execute_tool(block.name, block.input, chat_id)
            except Exception as e:
                logger.error("Tool execution failed: %s", e)
                result = json.dumps({"error": str(e)})

            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

            if block.name in _PDF_RESPONSE_TOOLS and chat_id in _last_invoice:
                pdf_path = str(_last_invoice[chat_id]["pdf_path"])
                inv_num = _last_invoice[chat_id].get("invoice_number", "")
                responses.append(AgentResponse(file_path=pdf_path, file_caption=f"Invoice {inv_num}"))

        _histories[chat_id].append({"role": "user", "content": tool_results})

    return responses
```

- [ ] **Step 5: Run all unified agent tests — expect PASS**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_unified_agent.py -v
```

- [ ] **Step 6: Commit**

```bash
git add organist_bot/integrations/unified_agent.py tests/test_unified_agent.py
git commit -m "feat: filter tools, clear_conversation, and process_message loop in unified_agent"
```

---

## Task 7: Rewrite `telegram_bot.py` and `test_telegram_integration.py`

**Files:**
- Replace: `organist_bot/integrations/telegram_bot.py`
- Replace: `tests/test_telegram_integration.py`

- [ ] **Step 1: Write failing integration tests for the new bot**

Overwrite `tests/test_telegram_integration.py`:

```python
"""Tests for the simplified unified Telegram bot dispatcher."""
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from organist_bot.integrations.telegram_bot import _is_authorised, handle_message
from organist_bot.integrations.unified_agent import AgentResponse


def _make_update(chat_id: int = 7973955362, text: str = "") -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


# ── _is_authorised ────────────────────────────────────────────────────────────

class TestIsAuthorised:
    def test_authorised_chat_id(self):
        update = _make_update(chat_id=7973955362)
        with patch("organist_bot.integrations.telegram_bot.settings") as mock_settings:
            mock_settings.telegram_chat_id = "7973955362"
            assert _is_authorised(update) is True

    def test_wrong_chat_id_rejected(self):
        update = _make_update(chat_id=9999999)
        with patch("organist_bot.integrations.telegram_bot.settings") as mock_settings:
            mock_settings.telegram_chat_id = "7973955362"
            assert _is_authorised(update) is False

    def test_string_vs_int_comparison(self):
        update = _make_update(chat_id=12345)
        with patch("organist_bot.integrations.telegram_bot.settings") as mock_settings:
            mock_settings.telegram_chat_id = "12345"
            assert _is_authorised(update) is True


# ── handle_message ────────────────────────────────────────────────────────────

class TestHandleMessage:
    @pytest.fixture(autouse=True)
    def patch_settings(self):
        with patch("organist_bot.integrations.telegram_bot.settings") as mock:
            mock.telegram_chat_id = "7973955362"
            yield mock

    @pytest.mark.asyncio
    async def test_rejects_unauthorised_chat(self):
        update = _make_update(chat_id=9999)
        with patch("organist_bot.integrations.unified_agent.process_message") as mock_pm:
            await handle_message(update, MagicMock())
        mock_pm.assert_not_called()

    @pytest.mark.asyncio
    async def test_sends_text_response(self):
        update = _make_update(text="List my clients")
        responses = [AgentResponse(text="You have 3 clients.")]
        with patch(
            "organist_bot.integrations.telegram_bot.unified_agent.process_message",
            new=AsyncMock(return_value=responses),
        ):
            await handle_message(update, MagicMock())
        update.message.reply_text.assert_called_once_with("You have 3 clients.", parse_mode="Markdown")

    @pytest.mark.asyncio
    async def test_sends_file_response(self):
        update = _make_update(text="Generate invoice")
        context = MagicMock()
        context.bot.send_document = AsyncMock()

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            responses = [AgentResponse(file_path=tmp_path, file_caption="Invoice INV-2026-001")]
            with patch(
                "organist_bot.integrations.telegram_bot.unified_agent.process_message",
                new=AsyncMock(return_value=responses),
            ):
                await handle_message(update, context)
            context.bot.send_document.assert_called_once()
            call_kwargs = context.bot.send_document.call_args[1]
            assert call_kwargs.get("caption") == "Invoice INV-2026-001"
        finally:
            os.unlink(tmp_path)

    @pytest.mark.asyncio
    async def test_handles_agent_error(self):
        update = _make_update(text="crash please")
        with patch(
            "organist_bot.integrations.telegram_bot.unified_agent.process_message",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            await handle_message(update, MagicMock())
        reply = update.message.reply_text.call_args[0][0]
        assert "❌" in reply or "error" in reply.lower()

    @pytest.mark.asyncio
    async def test_text_and_file_in_same_response(self):
        """Both text and file in one response are each sent."""
        update = _make_update(text="make invoice")
        context = MagicMock()
        context.bot.send_document = AsyncMock()

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            responses = [
                AgentResponse(file_path=tmp_path, file_caption="Invoice"),
                AgentResponse(text="Invoice generated!"),
            ]
            with patch(
                "organist_bot.integrations.telegram_bot.unified_agent.process_message",
                new=AsyncMock(return_value=responses),
            ):
                await handle_message(update, context)
            context.bot.send_document.assert_called_once()
            update.message.reply_text.assert_called_once_with("Invoice generated!", parse_mode="Markdown")
        finally:
            os.unlink(tmp_path)
```

- [ ] **Step 2: Run tests — expect FAIL (handle_message doesn't exist yet)**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_telegram_integration.py -v
```

- [ ] **Step 3: Rewrite `telegram_bot.py`**

Replace the entire file content:

```python
"""
organist_bot/integrations/telegram_bot.py
──────────────────────────────────────────
Unified Telegram bot dispatcher.

All free text is routed to the unified AI agent. There are no slash commands
for functionality — everything is handled through natural language.

Security: only messages from TELEGRAM_CHAT_ID are processed.
"""

import logging
import os

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler
from telegram.ext import filters as tg_filters

from organist_bot.config import settings
from organist_bot.integrations import unified_agent

logger = logging.getLogger(__name__)

_HELP = (
    "*Organist Bot*\n\n"
    "Just type what you need in plain English:\n\n"
    "• Add a gig: share a URL or describe it\n"
    "• List or delete gigs from your calendar\n"
    "• Generate and send invoices\n"
    "• Manage clients\n"
    "• Update blacklist or availability\n\n"
    "To start over, say \"reset\" or \"forget everything\"."
)


def _is_authorised(update: Update) -> bool:
    if update.effective_chat is None:
        return False
    return str(update.effective_chat.id) == str(settings.telegram_chat_id)


def _reject(update: Update) -> None:
    logger.warning(
        "Telegram: rejected unauthorised message",
        extra={"chat_id": update.effective_chat.id if update.effective_chat else None},
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update):
        _reject(update)
        return
    assert update.message is not None
    await update.message.reply_text(_HELP, parse_mode="Markdown")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update):
        _reject(update)
        return
    assert update.effective_chat is not None
    assert update.message is not None

    chat_id = update.effective_chat.id
    text = update.message.text or ""

    try:
        agent_responses = await unified_agent.process_message(chat_id, text)
        for resp in agent_responses:
            if resp.file_path:
                with open(resp.file_path, "rb") as f:
                    await context.bot.send_document(
                        chat_id=chat_id,
                        document=f,
                        filename=os.path.basename(resp.file_path),
                        caption=resp.file_caption or "",
                    )
            if resp.text:
                await update.message.reply_text(resp.text, parse_mode="Markdown")
    except Exception as exc:
        logger.exception("Telegram: agent error")
        await update.message.reply_text(f"❌ Unexpected error: {exc}")


def run(token: str) -> None:
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, handle_message))
    logger.info("Telegram bot polling", extra={"chat_id": settings.telegram_chat_id})
    app.run_polling()
```

- [ ] **Step 4: Run integration tests — expect PASS**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_telegram_integration.py -v
```

- [ ] **Step 5: Run full suite**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest --tb=short -q
```

Some tests from the old `test_telegram_integration.py` will now be gone (expected). All remaining tests should pass.

- [ ] **Step 6: Commit**

```bash
git add organist_bot/integrations/telegram_bot.py tests/test_telegram_integration.py
git commit -m "feat: unified Telegram dispatcher — single MessageHandler, no slash commands"
```

---

## Task 8: Delete old files and verify final suite

**Files:**
- Delete: `organist_bot/integrations/gig_agent.py`
- Delete: `organist_bot/integrations/invoice_agent.py`
- Delete: `tests/test_gig_agent.py`

- [ ] **Step 1: Delete old agent files and their tests**

```bash
git rm organist_bot/integrations/gig_agent.py
git rm organist_bot/integrations/invoice_agent.py
git rm tests/test_gig_agent.py
```

- [ ] **Step 2: Check nothing still imports the deleted modules**

```bash
grep -r "gig_agent\|invoice_agent" organist_bot/ tests/ --include="*.py"
```

Expected: no output. If any imports remain, fix them before proceeding.

- [ ] **Step 3: Run full test suite — all tests must pass**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest --tb=short -q
```

Expected: all tests pass, no errors.

- [ ] **Step 4: Run linter and type-checker**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com ruff check .
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com mypy organist_bot/
```

Fix any issues before committing.

- [ ] **Step 5: Final commit**

```bash
git add -A
git commit -m "chore: remove gig_agent, invoice_agent and their tests (replaced by unified_agent)"
```

---

## Done

At this point:
- All free text is routed through `unified_agent.process_message`
- Playwright cold-starts only on the first invoice per bot process
- `gig_agent.py` and `invoice_agent.py` are deleted
- All tests pass
