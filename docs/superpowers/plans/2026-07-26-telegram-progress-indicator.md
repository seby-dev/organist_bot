# Telegram Progress Indicator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show live, in-place progress in the Telegram bot while `unified_agent.process_message` runs its multi-step agent loop, so the user can tell the bot is working instead of wondering if it hung.

**Architecture:** `process_message` gains an optional `on_step` callback invoked with the cumulative step log each time a tool call starts/finishes. `telegram_bot.handle_message` sends a placeholder message, wires `on_step` to edit that message in place, and deletes it once the agent finishes (success or error) right before sending the real reply.

**Tech Stack:** Python, `python-telegram-bot`, `anthropic` SDK (Claude Sonnet), `pytest` + `pytest-asyncio`.

## Global Constraints

- `on_step` defaults to `None` on `process_message` — every existing caller/test that doesn't pass it is unaffected.
- No per-tool human-readable label mapping — raw tool names (e.g. `add_gig`) are shown as-is in the step log.
- `BadRequest: message is not modified` from `edit_message_text` is expected/harmless (two consecutive edits producing identical text) and must be silently swallowed, not logged as an error. Any other `BadRequest` from an edit or delete is logged at `debug` level and must never propagate out of `handle_message`.
- The placeholder message text is exactly `"🤔 Thinking…"`.
- The final chat output (text replies, file sends) is byte-for-byte unchanged from today — only the placeholder message's lifecycle is new.

---

### Task 0: Create the feature branch

Per this repo's `CLAUDE.md`, no change may be committed directly to `main`.

- [ ] **Step 1: Create and switch to the feature branch**

```bash
git checkout -b telegram-progress-indicator
```

Expected: `Switched to a new branch 'telegram-progress-indicator'`. All commits in Task 1 and Task 2 below happen on this branch.

---

### Task 1: `on_step` progress callback in `process_message`

**Files:**
- Modify: `organist_bot/integrations/unified_agent.py:2075-2149` (the `process_message` function)
- Test: `tests/test_unified_agent.py`

**Interfaces:**
- Consumes: nothing new — `Callable` and `Awaitable` are already imported at the top of `unified_agent.py` (`from collections.abc import Awaitable, Callable`).
- Produces: `process_message(chat_id: int, text: str, on_step: Callable[[str], Awaitable[None]] | None = None) -> list[AgentResponse]`. `on_step`, when given, is awaited with the full cumulative newline-joined step log (`"🔧 tool_name"` when a tool call starts, flipped to `"✅ tool_name"` once it returns) every time a tool call starts or finishes. Later tasks (Task 2) rely on this exact signature and step-string format.

- [ ] **Step 1: Write the failing test**

Add this to the end of `tests/test_unified_agent.py` (after the existing `neg_store` fixture block — append as a new top-level section):

```python
# ── process_message on_step progress reporting ──────────────────────────────


@pytest.mark.asyncio
async def test_process_message_reports_on_step_progress(tmp_path, monkeypatch):
    """process_message must report a 🔧 step when a tool call starts and flip
    it to ✅ once the tool call returns, via the on_step callback."""
    import sys
    from types import SimpleNamespace

    from organist_bot.integrations import agent_state, unified_agent

    monkeypatch.setattr(agent_state, "_PATH", tmp_path / "agent_state.json")
    cid = 314159
    unified_agent._hydrated.discard(cid)

    tool_use_block = SimpleNamespace(
        type="tool_use",
        name="add_gig",
        input={"url": "https://example.com/gig/1"},
        id="tool_1",
    )
    tool_use_response = SimpleNamespace(content=[tool_use_block], stop_reason="tool_use")

    text_block = SimpleNamespace(type="text", text="Added the gig.")
    end_turn_response = SimpleNamespace(content=[text_block], stop_reason="end_turn")

    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(side_effect=[tool_use_response, end_turn_response])
    fake_anthropic_module = SimpleNamespace(AsyncAnthropic=MagicMock(return_value=fake_client))
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic_module)

    monkeypatch.setattr(
        unified_agent, "_execute_tool", AsyncMock(return_value=json.dumps({"result": "ok"}))
    )

    steps: list[str] = []

    async def on_step(status_text: str) -> None:
        steps.append(status_text)

    try:
        responses = await unified_agent.process_message(cid, "add this gig", on_step=on_step)
    finally:
        unified_agent._histories.pop(cid, None)
        unified_agent._hydrated.discard(cid)

    assert steps == ["🔧 add_gig", "✅ add_gig"]
    assert responses == [unified_agent.AgentResponse(text="Added the gig.")]


@pytest.mark.asyncio
async def test_process_message_without_on_step_is_unaffected(tmp_path, monkeypatch):
    """Omitting on_step (the default) must not change existing behavior."""
    import sys
    from types import SimpleNamespace

    from organist_bot.integrations import agent_state, unified_agent

    monkeypatch.setattr(agent_state, "_PATH", tmp_path / "agent_state.json")
    cid = 271828
    unified_agent._hydrated.discard(cid)

    text_block = SimpleNamespace(type="text", text="All set.")
    end_turn_response = SimpleNamespace(content=[text_block], stop_reason="end_turn")

    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=end_turn_response)
    fake_anthropic_module = SimpleNamespace(AsyncAnthropic=MagicMock(return_value=fake_client))
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic_module)

    try:
        responses = await unified_agent.process_message(cid, "hello")
    finally:
        unified_agent._histories.pop(cid, None)
        unified_agent._hydrated.discard(cid)

    assert responses == [unified_agent.AgentResponse(text="All set.")]
```

Check the top of `tests/test_unified_agent.py` already imports `MagicMock` — it does (`from unittest.mock import MagicMock, patch`, line 5). Add `AsyncMock` to that import:

```python
from unittest.mock import AsyncMock, MagicMock, patch
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_unified_agent.py::test_process_message_reports_on_step_progress tests/test_unified_agent.py::test_process_message_without_on_step_is_unaffected -v`

Expected: `test_process_message_reports_on_step_progress` FAILs with `TypeError: process_message() got an unexpected keyword argument 'on_step'`. `test_process_message_without_on_step_is_unaffected` should already PASS (it doesn't use the new param) — that's fine, it's here to lock in the no-regression case before Step 3 touches the function.

- [ ] **Step 3: Implement `on_step` in `process_message`**

In `organist_bot/integrations/unified_agent.py`, replace the `process_message` function (currently lines 2075-2149) with:

```python
async def process_message(
    chat_id: int,
    text: str,
    on_step: Callable[[str], Awaitable[None]] | None = None,
) -> list[AgentResponse]:
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    _hydrate_chat(chat_id)

    if chat_id not in _histories:
        _histories[chat_id] = []

    _histories[chat_id].append({"role": "user", "content": text})

    responses: list[AgentResponse] = []
    steps: list[str] = []

    while True:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
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

        if response.stop_reason != "tool_use":
            responses.append(AgentResponse(text="(response truncated — please try again)"))
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            logger.info("Unified agent tool call: %s(%s)", block.name, json.dumps(block.input))

            steps.append(f"🔧 {block.name}")
            if on_step is not None:
                await on_step("\n".join(steps))

            try:
                result = await _execute_tool(block.name, block.input, chat_id)
            except Exception as e:
                logger.error("Tool execution failed: %s", e)
                result = json.dumps({"error": str(e)})

            steps[-1] = f"✅ {block.name}"
            if on_step is not None:
                await on_step("\n".join(steps))

            if block.name in _VERBATIM_RESPONSE_TOOLS:
                try:
                    data = json.loads(result)
                    if "result" in data:
                        responses.append(AgentResponse(text=data["result"]))
                        result = json.dumps({"result": "Listing sent to user."})
                except (json.JSONDecodeError, KeyError):
                    pass

            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

            if block.name in _PDF_RESPONSE_TOOLS and chat_id in _last_invoice:
                pdf_path = _last_invoice[chat_id].get("pdf_path")
                if pdf_path:
                    inv_num = _last_invoice[chat_id].get("invoice_number", "")
                    responses.append(
                        AgentResponse(file_path=str(pdf_path), file_caption=f"Invoice {inv_num}")
                    )

        if not tool_results:
            responses.append(
                AgentResponse(text="(unexpected empty tool response — please try again)")
            )
            break

        _histories[chat_id].append({"role": "user", "content": tool_results})

    _persist_chat(chat_id)
    return responses
```

The only changes from the original: the new `on_step` parameter, the `steps: list[str] = []` local, and the two `steps.append(...)` / `steps[-1] = ...` + `on_step` blocks around the existing `_execute_tool` call. Everything else is byte-identical to the current implementation.

- [ ] **Step 4: Run tests to verify they pass**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_unified_agent.py -v`

Expected: PASS (full file — this also confirms no existing `test_unified_agent.py` test regressed).

- [ ] **Step 5: Commit**

```bash
git add organist_bot/integrations/unified_agent.py tests/test_unified_agent.py
git commit -m "feat: add on_step progress callback to process_message"
```

---

### Task 2: Wire progress display into the Telegram handler

**Files:**
- Modify: `organist_bot/integrations/telegram_bot.py:87-112` (`handle_message`, plus a new `_delete_quietly` helper)
- Test: `tests/test_telegram_integration.py`

**Interfaces:**
- Consumes: `unified_agent.process_message(chat_id, text, on_step=...)` from Task 1.
- Produces: no new public interface — this is the top-level handler wired to the Telegram `MessageHandler`.

- [ ] **Step 1: Update test helpers and existing assertions (red)**

Replace the full contents of `tests/test_telegram_integration.py` with:

```python
"""Tests for the unified Telegram bot handlers."""

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import BadRequest

from organist_bot.integrations.telegram_bot import _is_authorised, handle_message
from organist_bot.integrations.unified_agent import AgentResponse

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_update(chat_id: int = 7973955362, text: str = "") -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.text = text
    update.message.reply_text = AsyncMock(return_value=MagicMock(message_id=111))
    return update


def _make_context() -> MagicMock:
    context = MagicMock()
    context.bot.edit_message_text = AsyncMock()
    context.bot.delete_message = AsyncMock()
    context.bot.send_document = AsyncMock()
    return context


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
        """Chat IDs from Telegram are ints; settings stores them as strings."""
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
        update.message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_sends_placeholder_then_text_response(self):
        update = _make_update(text="List my clients")
        context = _make_context()
        responses = [AgentResponse(text="You have 3 clients.")]
        with patch(
            "organist_bot.integrations.unified_agent.process_message",
            new=AsyncMock(return_value=responses),
        ):
            await handle_message(update, context)
        update.message.reply_text.assert_any_call("🤔 Thinking…")
        update.message.reply_text.assert_any_call(
            "You have 3 clients.", parse_mode="Markdown"
        )
        context.bot.delete_message.assert_called_once_with(
            chat_id=update.effective_chat.id, message_id=111
        )

    @pytest.mark.asyncio
    async def test_sends_file_response(self):
        update = _make_update(text="Generate invoice for holy-cross")
        context = _make_context()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            responses = [AgentResponse(file_path=tmp_path, file_caption="Invoice")]
            with patch(
                "organist_bot.integrations.unified_agent.process_message",
                new=AsyncMock(return_value=responses),
            ):
                await handle_message(update, context)
            context.bot.send_document.assert_called_once()
            _, kwargs = context.bot.send_document.call_args
            assert kwargs.get("caption") == "Invoice"
        finally:
            os.unlink(tmp_path)

    @pytest.mark.asyncio
    async def test_falls_back_to_plain_text_on_markdown_parse_error(self):
        """LLM output can contain stray underscores/asterisks (e.g. tool names
        like `manage_filter_suspensions`) that Telegram's legacy Markdown
        parser can't balance into valid entities. The reply must still be
        delivered, as plain text."""
        update = _make_update(text="what can you do across filter management")
        context = _make_context()
        update.message.reply_text = AsyncMock(
            side_effect=[
                MagicMock(message_id=111),  # placeholder
                BadRequest(
                    "Can't parse entities: can't find end of the entity at byte offset 658"
                ),
                None,
            ]
        )
        responses = [AgentResponse(text="Use manage_filter_suspensions to *pause* a filter.")]
        with patch(
            "organist_bot.integrations.unified_agent.process_message",
            new=AsyncMock(return_value=responses),
        ):
            await handle_message(update, context)
        assert update.message.reply_text.call_count == 3
        placeholder_call, first_call, second_call = update.message.reply_text.call_args_list
        assert placeholder_call.args == ("🤔 Thinking…",)
        assert first_call.kwargs.get("parse_mode") == "Markdown"
        assert second_call.args == ("Use manage_filter_suspensions to *pause* a filter.",)
        assert "parse_mode" not in second_call.kwargs

    @pytest.mark.asyncio
    async def test_reraises_non_markdown_bad_request(self):
        update = _make_update(text="hello")
        context = _make_context()
        # First call after the placeholder (the Markdown attempt) fails for an
        # unrelated reason and must propagate out of _reply; the last call is
        # handle_message's own error-reporting reply_text, which succeeds.
        update.message.reply_text = AsyncMock(
            side_effect=[
                MagicMock(message_id=111),  # placeholder
                BadRequest("Chat not found"),
                None,
            ]
        )
        responses = [AgentResponse(text="hi there")]
        with patch(
            "organist_bot.integrations.unified_agent.process_message",
            new=AsyncMock(return_value=responses),
        ):
            await handle_message(update, context)
        # handle_message's own try/except catches it and reports the error back
        assert update.message.reply_text.call_count == 3
        reply = update.message.reply_text.call_args[0][0]
        assert "❌" in reply

    @pytest.mark.asyncio
    async def test_handles_agent_error(self):
        update = _make_update(text="crash please")
        context = _make_context()
        with patch(
            "organist_bot.integrations.unified_agent.process_message",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            await handle_message(update, context)
        reply = update.message.reply_text.call_args[0][0]
        assert "❌" in reply or "error" in reply.lower()
        context.bot.delete_message.assert_called_once_with(
            chat_id=update.effective_chat.id, message_id=111
        )

    @pytest.mark.asyncio
    async def test_on_step_edits_placeholder_message(self):
        """The on_step callback passed into process_message must edit the
        placeholder message in place with each step's text."""
        update = _make_update(text="add a gig")
        context = _make_context()

        async def fake_process_message(chat_id, text, on_step=None):
            await on_step("🔧 add_gig")
            await on_step("✅ add_gig")
            return [AgentResponse(text="Added.")]

        with patch(
            "organist_bot.integrations.unified_agent.process_message",
            new=fake_process_message,
        ):
            await handle_message(update, context)

        assert context.bot.edit_message_text.call_count == 2
        first_kwargs = context.bot.edit_message_text.call_args_list[0].kwargs
        assert first_kwargs == {
            "chat_id": update.effective_chat.id,
            "message_id": 111,
            "text": "🔧 add_gig",
        }
        second_kwargs = context.bot.edit_message_text.call_args_list[1].kwargs
        assert second_kwargs["text"] == "✅ add_gig"

    @pytest.mark.asyncio
    async def test_on_step_swallows_not_modified_error(self):
        """Telegram raises BadRequest('message is not modified') when two
        consecutive edits produce identical text — this must not propagate."""
        update = _make_update(text="add a gig")
        context = _make_context()
        context.bot.edit_message_text = AsyncMock(
            side_effect=BadRequest("Bad Request: message is not modified")
        )

        async def fake_process_message(chat_id, text, on_step=None):
            await on_step("🔧 add_gig")  # must not raise
            return [AgentResponse(text="Added.")]

        with patch(
            "organist_bot.integrations.unified_agent.process_message",
            new=fake_process_message,
        ):
            await handle_message(update, context)  # must not raise

        update.message.reply_text.assert_any_call("Added.", parse_mode="Markdown")
```

- [ ] **Step 2: Run tests to verify the expected failures**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_telegram_integration.py -v`

Expected: FAIL on most `TestHandleMessage` tests — `handle_message` doesn't yet send a placeholder, call `context.bot.edit_message_text`/`delete_message`, or accept the new call pattern (e.g. `test_sends_placeholder_then_text_response` fails because `reply_text` was only called once; `test_on_step_edits_placeholder_message` fails because `edit_message_text` is never called). `TestIsAuthorised` and `test_rejects_unauthorised_chat` should still PASS.

- [ ] **Step 3: Implement the placeholder + progress + delete flow**

In `organist_bot/integrations/telegram_bot.py`, replace the `handle_message` function (currently lines 87-112) with:

```python
async def _delete_quietly(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int) -> None:
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except BadRequest as exc:
        logger.debug("Telegram: progress message delete failed: %s", exc)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update):
        _reject(update)
        return
    assert update.effective_chat is not None
    assert update.message is not None

    chat_id = update.effective_chat.id
    text = update.message.text or ""

    status_msg = await update.message.reply_text("🤔 Thinking…")

    async def on_step(status_text: str) -> None:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=status_msg.message_id, text=status_text
            )
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                logger.debug("Telegram: progress edit failed: %s", exc)

    try:
        responses = await unified_agent.process_message(chat_id, text, on_step=on_step)
    except Exception as exc:
        logger.exception("Telegram: unified agent error")
        await _delete_quietly(context, chat_id, status_msg.message_id)
        await update.message.reply_text(f"❌ Unexpected error: {exc}")
        return

    await _delete_quietly(context, chat_id, status_msg.message_id)

    for resp in responses:
        if resp.file_path:
            with open(resp.file_path, "rb") as f:
                await context.bot.send_document(
                    chat_id=chat_id,
                    document=f,
                    filename=os.path.basename(resp.file_path),
                    caption=resp.file_caption or "",
                )
        if resp.text:
            await _reply(update.message, resp.text)
```

This adds `_delete_quietly`, the placeholder send, the `on_step` closure, and the two `_delete_quietly` call sites (success path and exception path). The `for resp in responses:` block is unchanged from the current implementation.

- [ ] **Step 4: Run tests to verify they pass**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_telegram_integration.py -v`

Expected: PASS (all tests in the file).

- [ ] **Step 5: Run the full test suite**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest --tb=short -q`

Expected: PASS (no regressions elsewhere).

- [ ] **Step 6: Lint and type-check**

Run: `ruff check . && ruff format --check . && mypy organist_bot/`

Expected: no errors. If `ruff format --check` fails only on the two files just edited, run `ruff format organist_bot/integrations/telegram_bot.py organist_bot/integrations/unified_agent.py tests/test_telegram_integration.py tests/test_unified_agent.py` and re-check.

- [ ] **Step 7: Commit**

```bash
git add organist_bot/integrations/telegram_bot.py tests/test_telegram_integration.py
git commit -m "feat: show live progress indicator while the Telegram agent runs"
```

---

## Shipping

Both commits above were made on the `telegram-progress-indicator` branch created in Task 0, so no extra branch juggling is needed. Once Task 2 is committed, run:

```bash
make ship
```

`make ship` runs the full local quality gate (ruff lint, ruff format --check, mypy, bandit + semgrep, pytest), refuses to run on `main` (moot here — we're on the feature branch), then pushes the branch, opens a ready-for-review PR, and enables squash auto-merge — matching the existing project workflow documented in `CLAUDE.md`.
