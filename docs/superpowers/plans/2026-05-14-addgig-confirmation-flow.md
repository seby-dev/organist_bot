# /addgig Confirmation Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the fragile string-based `done` detection and soft confirmation convention in `/addgig` with a two-phase `add_gig` tool (`confirmed=false` previews, `confirmed=true` commits), enforce confirmation via system prompt, remove the URL regex gate, and fix the blocking sync Anthropic client.

**Architecture:** All changes are in two files — `gig_agent.py` (system prompt, tool schema, `_execute_tool` logic, async client) and `telegram_bot.py` (remove URL gate, simplify entry message). No new modules. Tests are added to a new `tests/test_gig_agent.py` and existing `tests/test_telegram_integration.py` is updated.

**Tech Stack:** Python 3.12, `anthropic` SDK (`AsyncAnthropic`), `python-telegram-bot`, `pytest-asyncio` (auto mode), `unittest.mock`.

---

## File map

| File | Action | What changes |
|---|---|---|
| `organist_bot/integrations/gig_agent.py` | Modify | System prompt, `add_gig` tool schema, `_execute_tool` split on `confirmed`, async client, `done` detection |
| `organist_bot/integrations/telegram_bot.py` | Modify | Remove `_GIG_URL_RE`, simplify `addgig_entry` |
| `tests/test_gig_agent.py` | Create | Tests for `_execute_tool` preview and commit paths |
| `tests/test_telegram_integration.py` | Modify | Update `test_invalid_url_rejected`, add `test_any_arg_forwarded_to_agent` |

---

## Task 1: Write failing tests for the new `_execute_tool` behaviour

**Files:**
- Create: `tests/test_gig_agent.py`

- [ ] **Step 1: Create the test file**

```python
"""Tests for gig_agent._execute_tool two-phase add_gig behaviour."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from organist_bot.integrations.gig_agent import _execute_tool

_FULL_INPUT = {
    "confirmed": False,
    "header": "Sunday Service",
    "organisation": "St Mary's",
    "locality": "Oxford",
    "date": "Sunday 1st June 2025",
    "time": "10:30am",
    "fee": "£150",
}


class TestExecuteToolAddGigPreview:
    async def test_returns_summary_containing_gig_fields(self):
        """confirmed=false returns a plain-text summary with all field values."""
        result = await _execute_tool("add_gig", _FULL_INPUT)
        assert "Sunday Service" in result
        assert "St Mary's" in result
        assert "Oxford" in result
        assert "Sunday 1st June 2025" in result
        assert "10:30am" in result
        assert "£150" in result

    async def test_does_not_write_to_calendar(self):
        """confirmed=false must never touch the calendar client."""
        with patch(
            "organist_bot.integrations.gig_agent._make_calendar_client"
        ) as mock_factory:
            await _execute_tool("add_gig", _FULL_INPUT)
        mock_factory.assert_not_called()

    async def test_result_does_not_contain_result_key(self):
        """confirmed=false must not return a JSON 'result' key (that key signals done)."""
        result = await _execute_tool("add_gig", _FULL_INPUT)
        assert '"result"' not in result


class TestExecuteToolAddGigConfirmed:
    async def test_writes_to_calendar_and_returns_result(self):
        """confirmed=true calls calendar and returns JSON with 'result'."""
        input_data = {**_FULL_INPUT, "confirmed": True}
        with patch(
            "organist_bot.integrations.gig_agent._make_calendar_client"
        ) as mock_factory:
            mock_cal = MagicMock()
            mock_cal.add_gig.return_value = "evt_abc123"
            mock_factory.return_value = mock_cal

            result = await _execute_tool("add_gig", input_data)

        data = json.loads(result)
        assert "result" in data
        assert "evt_abc123" in data["result"]
        mock_cal.add_gig.assert_called_once()

    async def test_no_calendar_config_returns_error(self):
        """confirmed=true with no calendar configured returns error JSON."""
        input_data = {
            "confirmed": True,
            "header": "Gig",
            "date": "2025-06-01",
            "time": "10am",
        }
        with patch(
            "organist_bot.integrations.gig_agent._make_calendar_client",
            return_value=None,
        ):
            result = await _execute_tool("add_gig", input_data)

        data = json.loads(result)
        assert "error" in data

    async def test_calendar_exception_returns_error(self):
        """confirmed=true when calendar.add_gig raises returns error JSON."""
        input_data = {**_FULL_INPUT, "confirmed": True}
        with patch(
            "organist_bot.integrations.gig_agent._make_calendar_client"
        ) as mock_factory:
            mock_cal = MagicMock()
            mock_cal.add_gig.side_effect = RuntimeError("calendar down")
            mock_factory.return_value = mock_cal

            result = await _execute_tool("add_gig", input_data)

        data = json.loads(result)
        assert "error" in data
        assert "calendar down" in data["error"]
```

- [ ] **Step 2: Run the tests to confirm they fail**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_gig_agent.py -v
```

Expected: all tests fail — `_execute_tool` doesn't yet split on `confirmed`.

---

## Task 2: Implement the two-phase `add_gig` in `gig_agent.py`

**Files:**
- Modify: `organist_bot/integrations/gig_agent.py`

- [ ] **Step 1: Replace `SYSTEM_PROMPT`**

Replace the `SYSTEM_PROMPT` constant (currently lines 16–27) with:

```python
SYSTEM_PROMPT = """\
You are a gig calendar assistant for an organist. Your job is to add gig bookings to Google Calendar.

## Gathering details
- If the user provides a URL, call fetch_gig_details immediately. Use the returned fields to pre-fill as much as possible.
- If fetch_gig_details returns an error, tell the user plainly and ask them to enter the details manually.
- If any of header, organisation, locality, date, time, or fee are missing after fetching (or no URL was given), ask for them one at a time in a natural conversational way.
- Do not ask for a field you already have.

## Confirmation (always required)
- Once you have all fields, call add_gig with confirmed=false. This returns a formatted summary — relay it to the user verbatim.
- Wait for the user's reply before doing anything else.
- If the user approves (e.g. "yes", "looks good", "confirm"), call add_gig with confirmed=true. Always include all fields from the last summary in the confirmed=true call.
- If the user requests a change (e.g. "change the fee to £200"), update the relevant field(s) and call add_gig with confirmed=false again with the full corrected fields. Repeat until confirmed.
- If the user's reply is ambiguous (a question, "maybe", etc.), treat it as a change request and re-present the summary with a prompt to confirm or edit.

## Rules
- Never call add_gig with confirmed=true unless the user has explicitly approved the summary in this conversation.
- Keep responses concise — this is a chat interface.
- Use British English and £ for fees.
- Once add_gig(confirmed=true) succeeds, your job is done.
"""
```

- [ ] **Step 2: Replace the `add_gig` entry in `TOOLS`**

Replace the `add_gig` dict in `TOOLS` (currently the second element) with:

```python
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
```

- [ ] **Step 3: Replace the `add_gig` branch in `_execute_tool`**

Replace the existing `if name == "add_gig":` block with:

```python
    if name == "add_gig":
        confirmed = input_data.get("confirmed", False)
        fields = {
            "header":       input_data.get("header", ""),
            "organisation": input_data.get("organisation") or "",
            "locality":     input_data.get("locality") or "",
            "date":         input_data.get("date", ""),
            "time":         input_data.get("time", ""),
            "fee":          input_data.get("fee") or "not specified",
        }

        if not confirmed:
            summary = (
                "*Please confirm the following gig:*\n"
                f"• *Title:* {fields['header']}\n"
                f"• *Organisation:* {fields['organisation']}\n"
                f"• *Locality:* {fields['locality']}\n"
                f"• *Date:* {fields['date']}\n"
                f"• *Time:* {fields['time']}\n"
                f"• *Fee:* {fields['fee']}\n\n"
                "Reply *yes* to add to calendar, or tell me what to change."
            )
            return summary

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
            return json.dumps({"result": f"Added to calendar. Event ID: {event_id}"})
        except Exception as exc:
            return json.dumps({"error": str(exc)})
```

- [ ] **Step 4: Fix the async client**

In `process_message`, replace:

```python
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
```

with:

```python
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
```

And replace:

```python
        response = client.messages.create(
```

with:

```python
        response = await client.messages.create(
```

- [ ] **Step 5: Remove the text-based `done` detection from the `end_turn` block**

Replace:

```python
        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    final_text = block.text
            # Agent signals completion by including "done=true" or "DONE" in its reply,
            # or when it has successfully added a gig.
            done = "done=true" in final_text.lower() or "added to calendar" in final_text.lower()
            break
```

with:

```python
        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    final_text = block.text
            break
```

(`done` is now only set to `True` in the tool-result loop below when `add_gig(confirmed=true)` succeeds — the existing `'"result"' in result` check works correctly because `confirmed=false` returns a plain markdown string, not JSON.)

- [ ] **Step 6: Run the new tests**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_gig_agent.py -v
```

Expected: all 6 tests pass.

- [ ] **Step 7: Run the full test suite**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest --tb=short -q
```

Expected: all tests pass except the one test in `test_telegram_integration.py` that asserts the URL gate rejects non-organistsonline URLs — that test will fail, which is expected and fixed in Task 3.

- [ ] **Step 8: Commit**

```bash
git add organist_bot/integrations/gig_agent.py tests/test_gig_agent.py
git commit -m "feat: two-phase add_gig tool with confirmation flow and async client fix"
```

---

## Task 3: Update `telegram_bot.py` — remove URL gate

**Files:**
- Modify: `organist_bot/integrations/telegram_bot.py`
- Modify: `tests/test_telegram_integration.py`

- [ ] **Step 1: Update the existing test for the URL gate**

In `tests/test_telegram_integration.py`, replace the `test_invalid_url_rejected` test:

```python
    async def test_any_arg_forwarded_to_agent(self):
        """Any argument — including a non-organistsonline URL — is forwarded to the agent."""
        update = _make_update()
        context = MagicMock()
        context.args = ["https://example.com/some-gig"]
        from organist_bot.integrations.gig_agent import GigAgentResponse
        from organist_bot.integrations.telegram_bot import CHATTING

        agent_resp = GigAgentResponse(text="What's the organisation?", done=False)
        with patch(
            "organist_bot.integrations.gig_agent.process_message",
            new=AsyncMock(return_value=agent_resp),
        ) as mock_pm:
            result = await addgig_entry(update, context)

        mock_pm.assert_called_once()
        call_args = mock_pm.call_args
        assert "https://example.com/some-gig" in call_args[0][1]
        assert result == CHATTING
```

- [ ] **Step 2: Run the new test to confirm it fails**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_telegram_integration.py::TestAddGigEntry::test_any_arg_forwarded_to_agent -v
```

Expected: FAIL — the URL gate still rejects the URL before reaching the agent.

- [ ] **Step 3: Remove the URL regex and gate from `telegram_bot.py`**

In `organist_bot/integrations/telegram_bot.py`:

Delete the module-level constant:
```python
_GIG_URL_RE = re.compile(r"https?://organistsonline\.org/\S+")
```

Replace the `addgig_entry` body from the `if context.args:` block onwards. Replace:

```python
    if context.args:
        url = context.args[0]
        if not _GIG_URL_RE.match(url):
            await update.message.reply_text(
                "That doesn't look like an organistsonline.org URL. "
                "Use `/addgig` without arguments to enter a gig in conversation.",
                parse_mode="Markdown",
            )
            return ConversationHandler.END
        initial = f"Add this gig to my calendar: {url}"
    else:
        initial = "I'd like to add a gig to my calendar."
```

with:

```python
    initial = context.args[0] if context.args else ""
```

Also remove `import re` if `_GIG_URL_RE` was the only use of `re` in the file. Check with:

```bash
grep -n "re\." organist_bot/integrations/telegram_bot.py
```

If no remaining uses, remove the `import re` line.

- [ ] **Step 4: Run the updated test to confirm it passes**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_telegram_integration.py::TestAddGigEntry::test_any_arg_forwarded_to_agent -v
```

Expected: PASS.

- [ ] **Step 5: Run the full test suite**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest --tb=short -q
```

Expected: all tests pass.

- [ ] **Step 6: Type-check and lint**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com mypy organist_bot/
ruff check .
ruff format .
```

Expected: no errors. Re-run `ruff format .` if formatter made changes, then verify tests still pass.

- [ ] **Step 7: Commit**

```bash
git add organist_bot/integrations/telegram_bot.py tests/test_telegram_integration.py
git commit -m "feat: remove URL gate from addgig — forward all input to gig agent"
```
