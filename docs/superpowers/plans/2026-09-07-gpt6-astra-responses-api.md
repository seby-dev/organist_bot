# gpt-6-astra Responses API Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `gpt-6-astra` actually work when a user selects it via `manage_llm_provider`, by routing that one model through OpenAI's Responses API (`litellm.aresponses()`) instead of the Chat Completions endpoint it currently — and permanently — fails on.

**Architecture:** Add a self-contained adapter (`_call_openai_responses_api`) plus four small translation helpers to `organist_bot/integrations/unified_agent.py`, and branch to it from inside `_call_llm_with_failover`'s existing per-candidate loop whenever the candidate model is `openai/gpt-6-astra`. The adapter returns an object shaped exactly like a `litellm.acompletion()` response, so `process_message()`, `_record_llm_usage`, and all history bookkeeping need zero branching and work identically regardless of which endpoint actually served the call. Reasoning-item continuity for a multi-round tool-call loop is handled by OpenAI's own `previous_response_id` chaining (scoped to one `process_message()` turn via a local, non-persisted session dict) rather than by manually replaying reasoning items — a well-documented, error-prone pattern this design avoids entirely.

**Tech Stack:** Python 3.13, `litellm` (`litellm.aresponses()` — new usage; `litellm.acompletion()` — existing, unchanged), `pytest` + `pytest-asyncio` + `unittest.mock.AsyncMock` (existing test conventions in `tests/test_unified_agent.py`).

**Spec:** `docs/superpowers/specs/2026-09-07-gpt6-astra-responses-api-design.md`

## Global Constraints

- Every other model/provider (`anthropic/*`, `gemini/*`, `openai/gpt-5.6-luna`) must keep using `litellm.acompletion()` completely unchanged — the Responses API branch is scoped to exactly one model string, `"openai/gpt-6-astra"`.
- No manual replay of OpenAI Responses API `reasoning` items, ever — use `previous_response_id` chaining instead (see spec §"Why this needs care").
- The Responses API's flat tool schema (`{"type": "function", "name", "description", "parameters"}`) is a *different* shape from this file's existing `TOOLS` (nested `{"type": "function", "function": {...}}`) — do not conflate them; both are derived from the same `_TOOLS_SCHEMA` source list so they can never drift apart.
- A response with `status` other than `"completed"`, or a non-`None` `error` field, must raise (`ResponsesApiError`) rather than silently returning an empty turn — this feeds the existing failover machinery in `_call_llm_with_failover`.
- `responses_session` is a plain `dict`, created fresh once per `process_message()` call (i.e. per incoming user text message) — never persisted across turns, chats, or a bot restart.
- Line length 100 (ruff), mypy is configured `strict = false` (existing untyped-helper patterns like `SimpleNamespace`-returning functions are acceptable, matching the rest of this file).
- Run `ruff format .`, `ruff check .`, and the relevant `pytest` file after every task, before committing.
- All commands in this plan assume the working directory is the isolated worktree: `/Users/sebby/Developer/organist_bot/.worktrees/fix-gpt6-astra-responses-api`. Because the Bash tool's cwd resets between calls, chain `cd` with the rest of each command (`cd <worktree> && <command>`), or invoke `.venv/bin/<tool>` — do not rely on a prior `cd` persisting.

---

## Task 1: Flat Responses-API tool schema + model registry constant

**Files:**
- Modify: `organist_bot/integrations/unified_agent.py:1-8` (imports), `:85-89` (near `_DEFAULT_MODEL_KEY_PER_PROVIDER`), `:893-907` (near `_to_function_tool`/`TOOLS`)
- Test: `tests/test_unified_agent.py`

**Interfaces:**
- Produces: `unified_agent._RESPONSES_API_MODELS: set[str]`, `unified_agent._to_responses_tool(tool: dict) -> dict`, `unified_agent.RESPONSES_TOOLS: list[dict]`. Later tasks consume `_RESPONSES_API_MODELS` (Task 5's routing branch) and `RESPONSES_TOOLS` (Task 4's adapter).

- [ ] **Step 1: Write the failing tests**

Open `tests/test_unified_agent.py` and find the comment line `# ── LLM provider failover cascade ────...` (search for it — it sits right after the `_fake_litellm_response` helper and right before `class TestCallLlmWithFailover:`, roughly line 2657). Insert this new section **immediately above** that comment line, so it reads as its own section before the failover-cascade one:

```python
# ── Responses API support (gpt-6-astra) ─────────────────────────────────────


class TestResponsesToolsShape:
    def test_every_entry_is_flat_function_tool_shape(self):
        for tool in unified_agent.RESPONSES_TOOLS:
            assert tool["type"] == "function"
            assert isinstance(tool["name"], str)
            assert isinstance(tool["description"], str)
            assert isinstance(tool["parameters"], dict)
            assert "function" not in tool

    def test_same_names_and_order_as_TOOLS(self):
        chat_names = [t["function"]["name"] for t in unified_agent.TOOLS]
        responses_names = [t["name"] for t in unified_agent.RESPONSES_TOOLS]
        assert responses_names == chat_names


def test_gpt_6_astra_is_in_responses_api_models():
    assert "openai/gpt-6-astra" in unified_agent._RESPONSES_API_MODELS
    assert "openai/gpt-5.6-luna" not in unified_agent._RESPONSES_API_MODELS
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd /Users/sebby/Developer/organist_bot/.worktrees/fix-gpt6-astra-responses-api && EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com .venv/bin/pytest tests/test_unified_agent.py -k "ResponsesToolsShape or gpt_6_astra_is_in_responses_api_models" -v
```

Expected: FAIL — `AttributeError: module 'organist_bot.integrations.unified_agent' has no attribute 'RESPONSES_TOOLS'` (and similarly for `_RESPONSES_API_MODELS`).

- [ ] **Step 3: Add the `SimpleNamespace` import**

In `organist_bot/integrations/unified_agent.py`, the top of the file currently reads:

```python
from __future__ import annotations

import datetime
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import cast
```

Add one import line so it reads:

```python
from __future__ import annotations

import datetime
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import cast
```

- [ ] **Step 4: Add `_RESPONSES_API_MODELS`**

Find `_DEFAULT_MODEL_KEY_PER_PROVIDER = {` (currently around line 85) — it ends with a closing `}` a few lines later. Immediately after that closing `}`, before the two blank lines and `def _default_model_string():`, add:

```python
# Models that reject function tools on /v1/chat/completions and must go through
# /v1/responses instead (litellm.aresponses()) -- see
# docs/superpowers/specs/2026-09-07-gpt6-astra-responses-api-design.md. Currently
# just gpt-6-astra; every other curated model keeps using acompletion().
_RESPONSES_API_MODELS = {"openai/gpt-6-astra"}
```

- [ ] **Step 5: Add `_to_responses_tool` and `RESPONSES_TOOLS`**

Find this existing code (currently around lines 893-907):

```python
def _to_function_tool(tool: dict) -> dict:
    """Wrap one Anthropic-shaped tool schema into OpenAI's function-calling shape —
    LiteLLM's canonical `tools=` input format regardless of which backend provider
    actually handles the request."""
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["input_schema"],
        },
    }


TOOLS: list[dict] = [_to_function_tool(t) for t in _TOOLS_SCHEMA]
```

Add immediately after the `TOOLS: list[dict] = ...` line:

```python


def _to_responses_tool(tool: dict) -> dict:
    """Flat function-tool shape required by the Responses API (no nested
    "function" key, unlike Chat Completions' TOOLS)."""
    return {
        "type": "function",
        "name": tool["name"],
        "description": tool["description"],
        "parameters": tool["input_schema"],
    }


RESPONSES_TOOLS: list[dict] = [_to_responses_tool(t) for t in _TOOLS_SCHEMA]
```

- [ ] **Step 6: Run the tests to verify they pass**

```bash
cd /Users/sebby/Developer/organist_bot/.worktrees/fix-gpt6-astra-responses-api && EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com .venv/bin/pytest tests/test_unified_agent.py -k "ResponsesToolsShape or gpt_6_astra_is_in_responses_api_models" -v
```

Expected: PASS (3 tests).

- [ ] **Step 7: Lint, format, full test file**

```bash
cd /Users/sebby/Developer/organist_bot/.worktrees/fix-gpt6-astra-responses-api && .venv/bin/ruff format organist_bot/integrations/unified_agent.py tests/test_unified_agent.py && .venv/bin/ruff check organist_bot/integrations/unified_agent.py tests/test_unified_agent.py && EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com .venv/bin/pytest tests/test_unified_agent.py -q
```

Expected: ruff clean, full file's existing tests (924+3) all still pass.

- [ ] **Step 8: Commit**

```bash
cd /Users/sebby/Developer/organist_bot/.worktrees/fix-gpt6-astra-responses-api && git add organist_bot/integrations/unified_agent.py tests/test_unified_agent.py && git commit -m "feat: add flat Responses-API tool schema and model registry constant

Part 1/7 of gpt-6-astra Responses API support -- see
docs/superpowers/specs/2026-09-07-gpt6-astra-responses-api-design.md.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 2: Response output → chat-completions translation

**Files:**
- Modify: `organist_bot/integrations/unified_agent.py` — insert after `_truncate_reason` (currently ends ~line 126), before `async def _call_llm_with_failover` (currently line 129)
- Test: `tests/test_unified_agent.py`

**Interfaces:**
- Consumes: nothing new (pure functions).
- Produces: `unified_agent._item_get(item, key, default=None)`, `unified_agent._chat_message_from_responses_output(output_items) -> SimpleNamespace`, `unified_agent._chat_response_from_responses_api(response) -> SimpleNamespace` (has `.choices[0].message` with `.content`/`.tool_calls`/`.model_dump()`, and `.usage` with `.prompt_tokens`/`.completion_tokens` or `None`). Task 4's adapter consumes `_chat_response_from_responses_api`.

- [ ] **Step 1: Write the failing tests**

Add this test helper and test class in `tests/test_unified_agent.py`, directly below the `test_gpt_6_astra_is_in_responses_api_models` function added in Task 1:

```python


def _fake_responses_function_call(call_id: str, name: str, arguments: dict) -> SimpleNamespace:
    """A Responses API output item of type "function_call" -- mirrors the real
    field names (call_id, name, arguments, type) confirmed against the
    installed openai/litellm packages during spec research."""
    return SimpleNamespace(
        type="function_call", call_id=call_id, name=name, arguments=json.dumps(arguments)
    )


def _fake_responses_message(text: str) -> SimpleNamespace:
    """A Responses API output item of type "message" with one output_text
    content part."""
    return SimpleNamespace(
        type="message",
        content=[SimpleNamespace(type="output_text", text=text)],
    )


def _fake_responses_api_response(
    output: list,
    *,
    response_id: str = "resp_1",
    status: str = "completed",
    error: object = None,
    usage: SimpleNamespace | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=response_id, output=output, status=status, error=error, usage=usage
    )


class TestChatMessageFromResponsesOutput:
    def test_message_only_sets_content_no_tool_calls(self):
        msg = unified_agent._chat_message_from_responses_output(
            [_fake_responses_message("Hello there")]
        )
        assert msg.content == "Hello there"
        assert msg.tool_calls is None
        assert msg.model_dump() == {
            "role": "assistant",
            "content": "Hello there",
            "tool_calls": None,
        }

    def test_function_call_sets_tool_calls_content_none(self):
        msg = unified_agent._chat_message_from_responses_output(
            [_fake_responses_function_call("call_abc123", "add_gig", {"url": "https://x"})]
        )
        assert msg.content is None
        assert len(msg.tool_calls) == 1
        tc = msg.tool_calls[0]
        # Critical: .id must be the item's call_id (call_...), not its id (fc_...)
        # -- call_id is what a matching function_call_output must reference.
        assert tc.id == "call_abc123"
        assert tc.function.name == "add_gig"
        assert json.loads(tc.function.arguments) == {"url": "https://x"}
        assert msg.model_dump() == {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_abc123",
                    "type": "function",
                    "function": {
                        "name": "add_gig",
                        "arguments": json.dumps({"url": "https://x"}),
                    },
                }
            ],
        }

    def test_multiple_function_calls_all_captured(self):
        msg = unified_agent._chat_message_from_responses_output(
            [
                _fake_responses_function_call("call_1", "tool_a", {}),
                _fake_responses_function_call("call_2", "tool_b", {}),
            ]
        )
        assert [tc.id for tc in msg.tool_calls] == ["call_1", "call_2"]


class TestChatResponseFromResponsesApi:
    def test_usage_translated_to_prompt_and_completion_tokens(self):
        response = _fake_responses_api_response(
            output=[_fake_responses_message("hi")],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )
        chat_response = unified_agent._chat_response_from_responses_api(response)
        assert chat_response.usage.prompt_tokens == 10
        assert chat_response.usage.completion_tokens == 5

    def test_none_usage_stays_none(self):
        response = _fake_responses_api_response(output=[_fake_responses_message("hi")], usage=None)
        chat_response = unified_agent._chat_response_from_responses_api(response)
        assert chat_response.usage is None

    def test_message_reachable_via_choices_zero(self):
        response = _fake_responses_api_response(output=[_fake_responses_message("hi")])
        chat_response = unified_agent._chat_response_from_responses_api(response)
        assert chat_response.choices[0].message.content == "hi"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd /Users/sebby/Developer/organist_bot/.worktrees/fix-gpt6-astra-responses-api && EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com .venv/bin/pytest tests/test_unified_agent.py -k "ChatMessageFromResponsesOutput or ChatResponseFromResponsesApi" -v
```

Expected: FAIL — `AttributeError: module 'organist_bot.integrations.unified_agent' has no attribute '_chat_message_from_responses_output'`.

- [ ] **Step 3: Implement**

In `organist_bot/integrations/unified_agent.py`, find `_truncate_reason` (currently ends around line 126, right before `async def _call_llm_with_failover`):

```python
def _truncate_reason(exc: Exception) -> str:
    """Cap a provider's exception text so a verbose provider error (looking
    at you, Gemini's multi-KB quota-violation JSON) can't blow past Telegram's
    4096-char message limit once several are joined into one alert/message."""
    text = str(exc)
    return text if len(text) <= _REASON_TRUNCATE_LEN else text[: _REASON_TRUNCATE_LEN - 1] + "…"
```

Insert this new block immediately after it, before `async def _call_llm_with_failover(`:

```python


def _item_get(item, key, default=None):
    """Accessor tolerant of both a pydantic response-item object (attribute
    access) and a plain dict -- litellm's Responses API objects are pydantic
    models, but this keeps the translators trivially unit-testable with dicts."""
    return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)


def _chat_message_from_responses_output(output_items) -> SimpleNamespace:
    """Build an object exposing .content / .tool_calls / .model_dump() shaped
    exactly like litellm.acompletion()'s response.choices[0].message, from a
    Responses API response's `.output` list."""
    text_parts: list[str] = []
    tool_calls: list[SimpleNamespace] = []
    for item in output_items:
        item_type = _item_get(item, "type")
        if item_type == "message":
            for content_item in _item_get(item, "content", []) or []:
                if _item_get(content_item, "type") == "output_text":
                    text_parts.append(_item_get(content_item, "text", "") or "")
        elif item_type == "function_call":
            tool_calls.append(
                SimpleNamespace(
                    id=_item_get(item, "call_id"),
                    function=SimpleNamespace(
                        name=_item_get(item, "name"),
                        arguments=_item_get(item, "arguments", "{}"),
                    ),
                )
            )
        # "reasoning" items (and any other type) are intentionally not surfaced
        # here -- they're not replayed manually; see previous_response_id
        # chaining in _call_openai_responses_api.

    tool_calls_out = tool_calls or None
    content = ("".join(text_parts) or None) if not tool_calls_out else None

    def _model_dump() -> dict:
        return {
            "role": "assistant",
            "content": content,
            "tool_calls": (
                [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls_out
                ]
                if tool_calls_out
                else None
            ),
        }

    return SimpleNamespace(content=content, tool_calls=tool_calls_out, model_dump=_model_dump)


def _chat_response_from_responses_api(response) -> SimpleNamespace:
    """Wrap a Responses API result as a chat-completions-shaped response object
    (`.choices[0].message`, `.usage.prompt_tokens/.completion_tokens`) -- same
    field names _record_llm_usage() already reads, translated from the
    Responses API's own usage.input_tokens/output_tokens."""
    message = _chat_message_from_responses_output(response.output)
    usage = _item_get(response, "usage")
    chat_usage = (
        SimpleNamespace(
            prompt_tokens=_item_get(usage, "input_tokens", 0) or 0,
            completion_tokens=_item_get(usage, "output_tokens", 0) or 0,
        )
        if usage is not None
        else None
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=chat_usage)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd /Users/sebby/Developer/organist_bot/.worktrees/fix-gpt6-astra-responses-api && EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com .venv/bin/pytest tests/test_unified_agent.py -k "ChatMessageFromResponsesOutput or ChatResponseFromResponsesApi" -v
```

Expected: PASS (6 tests).

- [ ] **Step 5: Lint, format, full test file**

```bash
cd /Users/sebby/Developer/organist_bot/.worktrees/fix-gpt6-astra-responses-api && .venv/bin/ruff format organist_bot/integrations/unified_agent.py tests/test_unified_agent.py && .venv/bin/ruff check organist_bot/integrations/unified_agent.py tests/test_unified_agent.py && EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com .venv/bin/pytest tests/test_unified_agent.py -q
```

- [ ] **Step 6: Commit**

```bash
cd /Users/sebby/Developer/organist_bot/.worktrees/fix-gpt6-astra-responses-api && git add organist_bot/integrations/unified_agent.py tests/test_unified_agent.py && git commit -m "feat: translate Responses API output into chat-completions shape

Part 2/7 -- _chat_message_from_responses_output / _chat_response_from_responses_api,
so process_message()/_record_llm_usage() need no branching regardless of which
endpoint served the call.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 3: History → Responses input translation

**Files:**
- Modify: `organist_bot/integrations/unified_agent.py` — insert directly after the block added in Task 2, still before `async def _call_llm_with_failover`
- Test: `tests/test_unified_agent.py`

**Interfaces:**
- Produces: `unified_agent._responses_input_from_messages(messages: list[dict]) -> list[dict]`, `unified_agent._responses_tool_outputs_from_messages(new_messages: list[dict]) -> list[dict]`. Task 4's adapter consumes both.

- [ ] **Step 1: Write the failing tests**

Add below `TestChatResponseFromResponsesApi` in `tests/test_unified_agent.py`:

```python


class TestResponsesInputFromMessages:
    def test_user_text_becomes_user_message_item(self):
        items = unified_agent._responses_input_from_messages(
            [{"role": "user", "content": "hello"}]
        )
        assert items == [{"type": "message", "role": "user", "content": "hello"}]

    def test_assistant_text_only_becomes_assistant_message_item(self):
        items = unified_agent._responses_input_from_messages(
            [{"role": "assistant", "content": "sure thing", "tool_calls": None}]
        )
        assert items == [{"type": "message", "role": "assistant", "content": "sure thing"}]

    def test_tool_call_round_trip_is_dropped(self):
        messages = [
            {"role": "user", "content": "add this gig"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1"}]},
            {"role": "tool", "tool_call_id": "call_1", "name": "add_gig", "content": "{}"},
        ]
        items = unified_agent._responses_input_from_messages(messages)
        assert items == [{"type": "message", "role": "user", "content": "add this gig"}]

    def test_assistant_message_with_both_content_and_tool_calls_keeps_the_text(self):
        messages = [
            {
                "role": "assistant",
                "content": "Let me check that.",
                "tool_calls": [{"id": "call_1"}],
            },
        ]
        items = unified_agent._responses_input_from_messages(messages)
        assert items == [
            {"type": "message", "role": "assistant", "content": "Let me check that."}
        ]

    def test_dangling_unresolved_tool_calls_message_is_tolerated(self):
        """Simulates a crash mid-turn on a previous call leaving an assistant
        tool_calls message with no matching tool result at all -- the
        translator must not assume turns are always cleanly resolved."""
        messages = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1"}]},
            {"role": "user", "content": "second"},
        ]
        items = unified_agent._responses_input_from_messages(messages)
        assert items == [
            {"type": "message", "role": "user", "content": "first"},
            {"type": "message", "role": "user", "content": "second"},
        ]


class TestResponsesToolOutputsFromMessages:
    def test_translates_tool_messages_to_function_call_output_items(self):
        new_messages = [
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1"}]},
            {"role": "tool", "tool_call_id": "call_1", "name": "add_gig", "content": '{"ok":true}'},
        ]
        items = unified_agent._responses_tool_outputs_from_messages(new_messages)
        assert items == [
            {"type": "function_call_output", "call_id": "call_1", "output": '{"ok":true}'}
        ]

    def test_multiple_tool_results_all_translated(self):
        new_messages = [
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1"}, {"id": "call_2"}]},
            {"role": "tool", "tool_call_id": "call_1", "name": "a", "content": "1"},
            {"role": "tool", "tool_call_id": "call_2", "name": "b", "content": "2"},
        ]
        items = unified_agent._responses_tool_outputs_from_messages(new_messages)
        assert items == [
            {"type": "function_call_output", "call_id": "call_1", "output": "1"},
            {"type": "function_call_output", "call_id": "call_2", "output": "2"},
        ]
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd /Users/sebby/Developer/organist_bot/.worktrees/fix-gpt6-astra-responses-api && EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com .venv/bin/pytest tests/test_unified_agent.py -k "ResponsesInputFromMessages or ResponsesToolOutputsFromMessages" -v
```

Expected: FAIL — `AttributeError: ... has no attribute '_responses_input_from_messages'`.

- [ ] **Step 3: Implement**

Insert this immediately after the block added in Task 2 (after `_chat_response_from_responses_api`'s closing line, still before `async def _call_llm_with_failover`):

```python


def _responses_input_from_messages(messages: list[dict]) -> list[dict]:
    """Bootstrap translation for the first Responses API call of a turn.
    Expects `messages` WITHOUT the leading system message -- the caller passes
    `messages[1:]`; the system prompt goes via `instructions=` instead.

    Keeps any text a user or assistant message carries (an assistant turn can
    have both `content` and `tool_calls` -- e.g. "Let me check that." before a
    tool call -- and its text is kept even though the tool_calls themselves are
    dropped). Drops the tool-call machinery of completed round trips from
    earlier turns entirely: neither a bare function_call item (an id from a
    different provider, or one with no reasoning item behind it) nor its
    function_call_output is replayed -- see
    docs/superpowers/specs/2026-09-07-gpt6-astra-responses-api-design.md.

    Tolerant by construction of a dangling, never-resolved tool_calls message
    (e.g. left behind by a crash mid-turn on a previous call) -- it's dropped
    the same as any other assistant-with-tool_calls message, whether or not a
    matching tool result ever arrived."""
    items: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "user" and isinstance(m.get("content"), str):
            items.append({"type": "message", "role": "user", "content": m["content"]})
        elif role == "assistant" and m.get("content"):
            items.append({"type": "message", "role": "assistant", "content": m["content"]})
        # assistant-with-tool_calls-and-no-content, and "tool" messages, are
        # intentionally dropped here.
    return items


def _responses_tool_outputs_from_messages(new_messages: list[dict]) -> list[dict]:
    """Translate a turn's new tool-result messages into function_call_output
    items for a previous_response_id-chained follow-up call. `new_messages` is
    the slice of `messages` appended since the last Responses API call in this
    turn -- exactly one assistant tool_calls message plus its tool messages."""
    return [
        {"type": "function_call_output", "call_id": m["tool_call_id"], "output": m["content"]}
        for m in new_messages
        if m.get("role") == "tool"
    ]
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd /Users/sebby/Developer/organist_bot/.worktrees/fix-gpt6-astra-responses-api && EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com .venv/bin/pytest tests/test_unified_agent.py -k "ResponsesInputFromMessages or ResponsesToolOutputsFromMessages" -v
```

Expected: PASS (7 tests).

- [ ] **Step 5: Lint, format, full test file**

```bash
cd /Users/sebby/Developer/organist_bot/.worktrees/fix-gpt6-astra-responses-api && .venv/bin/ruff format organist_bot/integrations/unified_agent.py tests/test_unified_agent.py && .venv/bin/ruff check organist_bot/integrations/unified_agent.py tests/test_unified_agent.py && EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com .venv/bin/pytest tests/test_unified_agent.py -q
```

- [ ] **Step 6: Commit**

```bash
cd /Users/sebby/Developer/organist_bot/.worktrees/fix-gpt6-astra-responses-api && git add organist_bot/integrations/unified_agent.py tests/test_unified_agent.py && git commit -m "feat: translate chat history into Responses API input items

Part 3/7 -- _responses_input_from_messages (bootstrap, drops historical
tool-call round trips) and _responses_tool_outputs_from_messages (delta,
for previous_response_id-chained follow-ups).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 4: `ResponsesApiError` + the `_call_openai_responses_api` adapter

**Files:**
- Modify: `organist_bot/integrations/unified_agent.py` — insert directly after the block added in Task 3, still before `async def _call_llm_with_failover`
- Test: `tests/test_unified_agent.py`

**Interfaces:**
- Consumes: `_responses_input_from_messages`, `_responses_tool_outputs_from_messages` (Task 3), `_chat_response_from_responses_api` (Task 2), `RESPONSES_TOOLS` (Task 1), `SYSTEM_PROMPT` (existing module global).
- Produces: `unified_agent.ResponsesApiError(RuntimeError)`, `unified_agent._call_openai_responses_api(model: str, api_key: str, messages: list[dict], responses_session: dict) -> SimpleNamespace` (async). Task 5 consumes `_call_openai_responses_api` and `ResponsesApiError`.

- [ ] **Step 1: Write the failing tests**

Add below `TestResponsesToolOutputsFromMessages` in `tests/test_unified_agent.py`:

```python


class TestCallOpenaiResponsesApi:
    async def test_first_call_bootstraps_from_history_without_system_message(self, monkeypatch):
        import litellm

        messages = [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "hello"},
        ]
        fake_response = _fake_responses_api_response(
            output=[_fake_responses_message("hi there")], response_id="resp_A"
        )
        mock_aresponses = AsyncMock(return_value=fake_response)
        monkeypatch.setattr(litellm, "aresponses", mock_aresponses)

        session: dict = {}
        result = await unified_agent._call_openai_responses_api(
            model="openai/gpt-6-astra",
            api_key="sk-test",
            messages=messages,
            responses_session=session,
        )

        assert result.choices[0].message.content == "hi there"
        call_kwargs = mock_aresponses.call_args.kwargs
        assert call_kwargs["previous_response_id"] is None
        assert call_kwargs["store"] is True
        assert call_kwargs["input"] == [{"type": "message", "role": "user", "content": "hello"}]
        assert call_kwargs["instructions"] == unified_agent.SYSTEM_PROMPT
        assert call_kwargs["tools"] == unified_agent.RESPONSES_TOOLS
        assert session["previous_response_id"] == "resp_A"
        assert session["synced_len"] == len(messages)

    async def test_second_call_chains_via_previous_response_id_with_only_new_tool_outputs(
        self, monkeypatch
    ):
        import litellm

        first_messages = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "hi"}]
        session: dict = {"previous_response_id": "resp_A", "synced_len": len(first_messages)}
        second_messages = [
            *first_messages,
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1"}]},
            {"role": "tool", "tool_call_id": "call_1", "name": "add_gig", "content": '{"ok":1}'},
        ]
        fake_response = _fake_responses_api_response(
            output=[_fake_responses_message("done")], response_id="resp_B"
        )
        mock_aresponses = AsyncMock(return_value=fake_response)
        monkeypatch.setattr(litellm, "aresponses", mock_aresponses)

        await unified_agent._call_openai_responses_api(
            model="openai/gpt-6-astra",
            api_key="sk-test",
            messages=second_messages,
            responses_session=session,
        )

        call_kwargs = mock_aresponses.call_args.kwargs
        assert call_kwargs["previous_response_id"] == "resp_A"
        assert call_kwargs["input"] == [
            {"type": "function_call_output", "call_id": "call_1", "output": '{"ok":1}'}
        ]
        assert session["previous_response_id"] == "resp_B"
        assert session["synced_len"] == len(second_messages)

    async def test_incomplete_status_raises_and_does_not_mutate_session(self, monkeypatch):
        import litellm

        fake_response = _fake_responses_api_response(
            output=[], response_id="resp_C", status="incomplete"
        )
        monkeypatch.setattr(litellm, "aresponses", AsyncMock(return_value=fake_response))

        session: dict = {}
        with pytest.raises(unified_agent.ResponsesApiError):
            await unified_agent._call_openai_responses_api(
                model="openai/gpt-6-astra",
                api_key="sk-test",
                messages=[{"role": "system", "content": "SYS"}, {"role": "user", "content": "hi"}],
                responses_session=session,
            )
        assert session == {}

    async def test_populated_error_field_raises_even_if_status_completed(self, monkeypatch):
        import litellm

        fake_response = _fake_responses_api_response(
            output=[], response_id="resp_D", status="completed", error={"message": "boom"}
        )
        monkeypatch.setattr(litellm, "aresponses", AsyncMock(return_value=fake_response))

        with pytest.raises(unified_agent.ResponsesApiError):
            await unified_agent._call_openai_responses_api(
                model="openai/gpt-6-astra",
                api_key="sk-test",
                messages=[{"role": "system", "content": "SYS"}, {"role": "user", "content": "hi"}],
                responses_session={},
            )
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd /Users/sebby/Developer/organist_bot/.worktrees/fix-gpt6-astra-responses-api && EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com .venv/bin/pytest tests/test_unified_agent.py -k "CallOpenaiResponsesApi" -v
```

Expected: FAIL — `AttributeError: ... has no attribute '_call_openai_responses_api'`.

- [ ] **Step 3: Implement**

Insert this immediately after the block added in Task 3 (after `_responses_tool_outputs_from_messages`'s closing line, still before `async def _call_llm_with_failover`):

```python


class ResponsesApiError(RuntimeError):
    """Raised when OpenAI's /v1/responses returns a non-"completed" response
    (status "failed"/"incomplete", or a populated `error` field) instead of
    raising an HTTP-level exception. litellm.aresponses() does not itself raise
    for these -- they're a normal 200 response with a status field describing
    what went wrong -- so _call_openai_responses_api raises explicitly to fold
    them into _call_llm_with_failover's existing except/failover path instead
    of silently returning an empty turn -- see
    docs/superpowers/specs/2026-09-07-gpt6-astra-responses-api-design.md."""


async def _call_openai_responses_api(
    model: str,
    api_key: str,
    messages: list[dict],
    responses_session: dict,
) -> SimpleNamespace:
    """Call OpenAI's /v1/responses endpoint (via litellm.aresponses) for models
    that reject function tools on /v1/chat/completions (currently just
    gpt-6-astra -- see _RESPONSES_API_MODELS). Returns a chat-completions-shaped
    response so process_message()/_record_llm_usage()/history bookkeeping work
    unchanged regardless of which endpoint actually served the call.

    `responses_session` is a plain dict, one per process_message() turn (created
    fresh in process_message, NOT persisted across turns or chats), used to
    chain calls within the same tool-calling loop via previous_response_id --
    this replaces manually replaying reasoning items (see spec). That chaining
    requires the referenced response to be stored server-side, hence
    `store=True` below (litellm's/OpenAI's default already, but pinned
    explicitly since correctness depends on it).

    No `max_output_tokens` is set deliberately: this is a reasoning model, and
    reasoning tokens count against that cap -- an aggressive limit (e.g. the
    4096 the acompletion() branch uses) risks routinely truncating mid-reasoning
    into an "incomplete" status, which now raises (see ResponsesApiError) and
    would push every such turn into failover instead of just being slow.
    """
    import litellm

    previous_response_id = responses_session.get("previous_response_id")
    if previous_response_id is None:
        input_items = _responses_input_from_messages(messages[1:])  # [0] is system
    else:
        input_items = _responses_tool_outputs_from_messages(
            messages[responses_session["synced_len"] :]
        )

    response = await litellm.aresponses(
        model=model,
        input=input_items,
        instructions=SYSTEM_PROMPT,
        tools=RESPONSES_TOOLS,
        api_key=api_key,
        previous_response_id=previous_response_id,
        store=True,
    )

    if response.status != "completed" or response.error is not None:
        raise ResponsesApiError(
            f"OpenAI Responses API returned status={response.status!r}, "
            f"error={response.error!r}"
        )

    responses_session["previous_response_id"] = response.id
    responses_session["synced_len"] = len(messages)

    return _chat_response_from_responses_api(response)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd /Users/sebby/Developer/organist_bot/.worktrees/fix-gpt6-astra-responses-api && EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com .venv/bin/pytest tests/test_unified_agent.py -k "CallOpenaiResponsesApi" -v
```

Expected: PASS (4 tests).

- [ ] **Step 5: Lint, format, full test file**

```bash
cd /Users/sebby/Developer/organist_bot/.worktrees/fix-gpt6-astra-responses-api && .venv/bin/ruff format organist_bot/integrations/unified_agent.py tests/test_unified_agent.py && .venv/bin/ruff check organist_bot/integrations/unified_agent.py tests/test_unified_agent.py && EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com .venv/bin/pytest tests/test_unified_agent.py -q
```

- [ ] **Step 6: Type-check**

```bash
cd /Users/sebby/Developer/organist_bot/.worktrees/fix-gpt6-astra-responses-api && .venv/bin/mypy organist_bot/
```

Expected: no new errors introduced by this task's changes (pre-existing errors elsewhere, if any, are not this task's concern — but there should be none, since `mypy organist_bot/` is part of `make pre-push` and the repo is expected to be clean before this branch started).

- [ ] **Step 7: Commit**

```bash
cd /Users/sebby/Developer/organist_bot/.worktrees/fix-gpt6-astra-responses-api && git add organist_bot/integrations/unified_agent.py tests/test_unified_agent.py && git commit -m "feat: add the OpenAI Responses API adapter for gpt-6-astra

Part 4/7 -- ResponsesApiError + _call_openai_responses_api, ties together
the translation helpers from parts 2-3. Not yet wired into
_call_llm_with_failover or process_message (parts 5-6).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 5: Wire the adapter into `_call_llm_with_failover`

**Files:**
- Modify: `organist_bot/integrations/unified_agent.py` — the `_DEFAULT_MODEL_KEY_PER_PROVIDER` comment (search for it, ~line 76) and `_call_llm_with_failover` (search for `async def _call_llm_with_failover`, ~line 129 at plan-writing time but shifted by ~130 lines after Tasks 2-4's insertions — use the quoted anchor text below, not the line number)
- Test: `tests/test_unified_agent.py` (extends the existing `TestCallLlmWithFailover` class)

**Interfaces:**
- Consumes: `_call_openai_responses_api`, `ResponsesApiError` (Task 4), `_RESPONSES_API_MODELS` (Task 1).
- Produces: `_call_llm_with_failover(provider, model, messages, tools, responses_session=None)` — new optional 5th parameter. Task 6 consumes this new parameter.

- [ ] **Step 1: Write the failing tests**

Add these two tests inside the existing `class TestCallLlmWithFailover:` in `tests/test_unified_agent.py` (find that class — it already has `teardown_method` and several `async def test_...` methods; add these as new methods in the same class, anywhere after `teardown_method`):

```python
    async def test_responses_api_model_routes_to_the_adapter_not_acompletion(
        self, tmp_path, monkeypatch
    ):
        import litellm

        # No runtime_config write is expected on this path (first-try success,
        # i == 0 -- see _call_llm_with_failover's promotion logic), but chdir
        # anyway to match this class's other tests and guard against any
        # incidental write touching the real repo's data/ directory.
        monkeypatch.chdir(tmp_path)
        fake_response = _fake_responses_api_response(output=[_fake_responses_message("ok")])
        mock_aresponses = AsyncMock(return_value=fake_response)
        mock_acompletion = AsyncMock(side_effect=AssertionError("acompletion must not be called"))
        monkeypatch.setattr(litellm, "aresponses", mock_aresponses)
        monkeypatch.setattr(litellm, "acompletion", mock_acompletion)
        # _call_llm_with_failover's except-and-retry swallows an AssertionError
        # from the mock above like any other provider failure -- if routing
        # ever regresses, every candidate gets tried and the real
        # alert.send_alert would fire (a real Telegram alert from a test run,
        # if this machine's .env has it configured). Stub it so a regression
        # fails on the assertions below instead of sending anything real.
        monkeypatch.setattr(unified_agent.alert, "send_alert", lambda *a, **k: None)

        result, provider, model = await unified_agent._call_llm_with_failover(
            "openai", "openai/gpt-6-astra", messages=[{"role": "system", "content": "S"}], tools=[]
        )

        assert provider == "openai"
        assert model == "openai/gpt-6-astra"
        assert result.choices[0].message.content == "ok"
        mock_aresponses.assert_awaited_once()
        mock_acompletion.assert_not_awaited()

    async def test_responses_api_failure_falls_over_to_next_provider(self, tmp_path, monkeypatch):
        import litellm

        monkeypatch.chdir(tmp_path)
        # Pin all three key fields explicitly so candidate ordering is
        # deterministic regardless of this machine's real .env (litellm's
        # own dotenv side effect can otherwise leak a real ANTHROPIC_API_KEY
        # into settings -- see the test file's existing
        # test_settings_has_openai_and_gemini_api_key_fields docstring).
        monkeypatch.setattr(unified_agent.settings, "anthropic_api_key", "sk-anthropic-test")
        monkeypatch.setattr(unified_agent.settings, "openai_api_key", "sk-test")
        monkeypatch.setattr(unified_agent.settings, "gemini_api_key", "")

        good_response = _fake_litellm_response(content="ok")
        monkeypatch.setattr(
            litellm,
            "aresponses",
            AsyncMock(side_effect=unified_agent.ResponsesApiError("incomplete")),
        )
        monkeypatch.setattr(litellm, "acompletion", AsyncMock(return_value=good_response))
        monkeypatch.setattr(unified_agent.alert, "send_alert", lambda *a, **k: None)

        result, provider, model = await unified_agent._call_llm_with_failover(
            "openai",
            "openai/gpt-6-astra",
            messages=[{"role": "system", "content": "S"}],
            tools=[],
        )

        # candidates = [("openai", "openai/gpt-6-astra")] first, then
        # _FAILOVER_ORDER = ["anthropic", "openai", "gemini"] filtered to
        # configured keys minus the starting provider ("openai" skipped as
        # itself, "gemini" skipped as unconfigured) -- so "anthropic" is the
        # only, deterministic fallback candidate.
        assert result is good_response
        assert provider == "anthropic"
        assert model == "anthropic/claude-sonnet-4-6"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd /Users/sebby/Developer/organist_bot/.worktrees/fix-gpt6-astra-responses-api && EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com .venv/bin/pytest tests/test_unified_agent.py -k "test_responses_api_model_routes_to_the_adapter_not_acompletion or test_responses_api_failure_falls_over_to_next_provider" -v
```

Expected: FAIL — the first test fails because `_call_llm_with_failover` still calls `litellm.acompletion` unconditionally (hits the `AssertionError` mock), not because of a missing attribute.

- [ ] **Step 3: Implement**

In `organist_bot/integrations/unified_agent.py`, find the existing `_DEFAULT_MODEL_KEY_PER_PROVIDER` block and its comment (currently lines ~70-89):

```python
# Which curated model a provider fails over TO, since the currently-active
# model key may not exist for that provider (e.g. "opus" has no OpenAI
# equivalent key). Deliberately NOT each provider's flagship: OpenAI's
# flagship "gpt-6-astra" is a reasoning model that OpenAI's own API refuses
# to run with function tools at all on the chat-completions endpoint this
# codebase uses (needs /v1/responses instead, and separately rejects
# reasoning_effort="none" as a workaround) -- confirmed live against the real
# API. Every conversation here always sends tools, so gpt-6-astra can never
# actually serve a failover call; "gpt-5.6-luna" is openai's other curated
# option and works fine with tools.
_DEFAULT_MODEL_KEY_PER_PROVIDER = {
    "anthropic": "sonnet",
    "openai": "gpt-5.6-luna",
    "gemini": "gemini-pro",
}
```

Replace the comment (keep the dict itself unchanged) with:

```python
# Which curated model a provider fails over TO, since the currently-active
# model key may not exist for that provider (e.g. "opus" has no OpenAI
# equivalent key). Deliberately NOT each provider's flagship: gpt-6-astra now
# works standalone via the Responses API adapter (see _RESPONSES_API_MODELS /
# _call_openai_responses_api), but the failover default stays "gpt-5.6-luna"
# on purpose -- an outage-driven failover should land on the plain, fast
# acompletion() path, not the added Responses API round-trip machinery.
_DEFAULT_MODEL_KEY_PER_PROVIDER = {
    "anthropic": "sonnet",
    "openai": "gpt-5.6-luna",
    "gemini": "gemini-pro",
}
```

Then find `async def _call_llm_with_failover`:

```python
async def _call_llm_with_failover(
    provider: str, model: str, messages: list[dict], tools: list[dict]
):
```

Change the signature to:

```python
async def _call_llm_with_failover(
    provider: str,
    model: str,
    messages: list[dict],
    tools: list[dict],
    responses_session: dict | None = None,
):
```

Leave the docstring's body text unchanged (its first line, "Call litellm.acompletion against `provider`/`model`...", becomes slightly imprecise now that one candidate can go through the Responses API instead — acceptable, out of scope to rewrite the whole docstring for one branch). Immediately after the docstring's closing `"""`, find:

```python
    import litellm

    candidates = [(provider, model)]
```

Change to:

```python
    import litellm

    if responses_session is None:
        responses_session = {}

    candidates = [(provider, model)]
```

Then find the per-candidate call inside the `for i, (p, m) in enumerate(candidates):` loop:

```python
    for i, (p, m) in enumerate(candidates):
        try:
            response = await litellm.acompletion(
                model=m,
                # Not max_tokens: some providers' newer models (e.g. OpenAI's
                # reasoning-style models) reject it outright and require
                # max_completion_tokens instead. litellm's per-provider
                # transformation layer accepts this generic name for every
                # provider here and maps it to that provider's native param
                # (Anthropic/Gemini included), so this one name is safe
                # everywhere -- unlike max_tokens, which isn't for all of them.
                max_completion_tokens=4096,
                api_key=getattr(settings, _PROVIDER_API_KEY_FIELD[p]),
                messages=messages,
                tools=tools,
            )
        except Exception as exc:
```

Change to:

```python
    for i, (p, m) in enumerate(candidates):
        try:
            if m in _RESPONSES_API_MODELS:
                response = await _call_openai_responses_api(
                    model=m,
                    api_key=getattr(settings, _PROVIDER_API_KEY_FIELD[p]),
                    messages=messages,
                    responses_session=responses_session,
                )
            else:
                response = await litellm.acompletion(
                    model=m,
                    # Not max_tokens: some providers' newer models (e.g. OpenAI's
                    # reasoning-style models) reject it outright and require
                    # max_completion_tokens instead. litellm's per-provider
                    # transformation layer accepts this generic name for every
                    # provider here and maps it to that provider's native param
                    # (Anthropic/Gemini included), so this one name is safe
                    # everywhere -- unlike max_tokens, which isn't for all of them.
                    max_completion_tokens=4096,
                    api_key=getattr(settings, _PROVIDER_API_KEY_FIELD[p]),
                    messages=messages,
                    tools=tools,
                )
        except Exception as exc:
```

Everything below `except Exception as exc:` in this function is unchanged.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd /Users/sebby/Developer/organist_bot/.worktrees/fix-gpt6-astra-responses-api && EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com .venv/bin/pytest tests/test_unified_agent.py -k "TestCallLlmWithFailover" -v
```

Expected: PASS — all existing `TestCallLlmWithFailover` tests plus the two new ones (the existing ones prove the `responses_session=None` default keeps every non-`gpt-6-astra` candidate on the unchanged `acompletion` branch).

- [ ] **Step 5: Lint, format, full test file**

```bash
cd /Users/sebby/Developer/organist_bot/.worktrees/fix-gpt6-astra-responses-api && .venv/bin/ruff format organist_bot/integrations/unified_agent.py tests/test_unified_agent.py && .venv/bin/ruff check organist_bot/integrations/unified_agent.py tests/test_unified_agent.py && EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com .venv/bin/pytest tests/test_unified_agent.py -q
```

Expected: full suite green (all prior tests + all new ones from Tasks 1-5).

- [ ] **Step 6: Commit**

```bash
cd /Users/sebby/Developer/organist_bot/.worktrees/fix-gpt6-astra-responses-api && git add organist_bot/integrations/unified_agent.py tests/test_unified_agent.py && git commit -m "feat: route gpt-6-astra through the Responses API in _call_llm_with_failover

Part 5/7 -- adds the responses_session param and per-candidate routing
branch; a ResponsesApiError now falls through to the next configured
provider exactly like any other failure. Updates the
_DEFAULT_MODEL_KEY_PER_PROVIDER comment to reflect that gpt-6-astra works
standalone now (the failover default itself is unchanged, on purpose).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 6: Thread `responses_session` through `process_message()`

**Files:**
- Modify: `organist_bot/integrations/unified_agent.py` — `process_message`, the top of its `while True:` loop (search for `async def process_message`; line numbers have shifted from the spec's original estimate by the insertions in Tasks 1-5, use the quoted anchor text below)
- Test: `tests/test_unified_agent.py`

**Interfaces:**
- Consumes: `_call_llm_with_failover`'s new `responses_session` parameter (Task 5).
- Produces: no new public interface — `process_message`'s external behavior/signature is unchanged; this task makes gpt-6-astra actually usable end-to-end through the bot.

- [ ] **Step 1: Write the failing test**

Add this test near the bottom of `tests/test_unified_agent.py`, after `test_process_message_stale_provider_resets_to_default` (search for that function name to find the right spot — add this one right after it, in the same module-level style, not inside a class):

```python
@pytest.mark.asyncio
async def test_process_message_gpt_6_astra_two_round_tool_loop(tmp_path, monkeypatch):
    """End-to-end: active model openai/gpt-6-astra, a tool call followed by a
    final text reply -- confirms previous_response_id chaining across the two
    aresponses() calls and that _histories[] ends up in the same flat
    chat-completions shape any other provider would produce."""
    import litellm

    from organist_bot.integrations import agent_state, unified_agent
    from organist_bot.runtime_config_store import runtime_config

    # chdir first -- runtime_config.set/reset below write to a relative
    # data/runtime_config.json, and must not touch the real repo's copy.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agent_state, "_PATH", tmp_path / "agent_state.json")
    monkeypatch.setattr(unified_agent.settings, "openai_api_key", "sk-test")
    cid = 987654
    unified_agent._hydrated.discard(cid)
    runtime_config.set("llm_provider", "openai")
    runtime_config.set("llm_model", "openai/gpt-6-astra")

    first_response = _fake_responses_api_response(
        output=[_fake_responses_function_call("call_xyz", "add_gig", {"url": "https://x"})],
        response_id="resp_first",
    )
    second_response = _fake_responses_api_response(
        output=[_fake_responses_message("Added the gig.")],
        response_id="resp_second",
    )
    mock_aresponses = AsyncMock(side_effect=[first_response, second_response])
    monkeypatch.setattr(litellm, "aresponses", mock_aresponses)
    monkeypatch.setattr(
        litellm, "acompletion", AsyncMock(side_effect=AssertionError("must not be called"))
    )
    # Guard against a routing regression silently trying every configured
    # provider (the acompletion AssertionError above is caught and treated as
    # a normal provider failure by _call_llm_with_failover) and firing a real
    # Telegram alert on the all-failed path.
    monkeypatch.setattr(unified_agent.alert, "send_alert", lambda *a, **k: None)
    monkeypatch.setattr(
        unified_agent, "_execute_tool", AsyncMock(return_value=json.dumps({"result": "ok"}))
    )

    try:
        responses = await unified_agent.process_message(cid, "add this gig")

        assert responses == [unified_agent.AgentResponse(text="Added the gig.")]

        second_call_kwargs = mock_aresponses.call_args_list[1].kwargs
        assert second_call_kwargs["previous_response_id"] == "resp_first"
        assert second_call_kwargs["input"] == [
            {"type": "function_call_output", "call_id": "call_xyz", "output": json.dumps({"result": "ok"})}
        ]

        history = unified_agent._histories[cid]
        assistant_turn = next(m for m in history if m["role"] == "assistant" and m.get("tool_calls"))
        assert assistant_turn["content"] is None
        assert assistant_turn["tool_calls"][0]["id"] == "call_xyz"
        tool_turn = next(m for m in history if m["role"] == "tool")
        assert tool_turn["tool_call_id"] == "call_xyz"
    finally:
        unified_agent._histories.pop(cid, None)
        unified_agent._hydrated.discard(cid)
        runtime_config.reset("llm_provider")
        runtime_config.reset("llm_model")
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd /Users/sebby/Developer/organist_bot/.worktrees/fix-gpt6-astra-responses-api && EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com .venv/bin/pytest tests/test_unified_agent.py -k "test_process_message_gpt_6_astra_two_round_tool_loop" -v
```

Expected: FAIL at `assert second_call_kwargs["previous_response_id"] == "resp_first"`, with the actual value `None`. Reasoning: Task 5's routing is keyed purely on the model string (`m in _RESPONSES_API_MODELS`), so both loop iterations already call the (mocked) `aresponses` regardless of this task — `acompletion` is never reached, so that guard mock never fires. But without this task's change, `process_message` never passes `responses_session` to `_call_llm_with_failover`, so it defaults to a **fresh empty dict on every call** — the second iteration's `_call_openai_responses_api` sees `previous_response_id=None` again instead of `"resp_first"`, and re-bootstraps from history instead of chaining.

- [ ] **Step 3: Implement**

In `organist_bot/integrations/unified_agent.py`, find `process_message` (currently lines 2425-2461):

```python
    _histories[chat_id].append({"role": "user", "content": text})

    responses: list[AgentResponse] = []
    steps: list[str] = []

    while True:
        # provider/model are updated from what actually served the call, so a
        # mid-conversation failover (see _call_llm_with_failover) sticks for
        # the rest of this turn's tool-calling loop instead of retrying the
        # already-failed provider again on every iteration.
        response, provider, model = await _call_llm_with_failover(
            provider,
            model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                *_histories[chat_id],
            ],
            tools=TOOLS,
        )
```

Change to:

```python
    _histories[chat_id].append({"role": "user", "content": text})

    responses: list[AgentResponse] = []
    steps: list[str] = []
    # One responses_session per turn (per process_message() call), never
    # persisted across turns -- see _call_openai_responses_api's docstring for
    # why this is the right scope for previous_response_id chaining.
    responses_session: dict = {}

    while True:
        # provider/model are updated from what actually served the call, so a
        # mid-conversation failover (see _call_llm_with_failover) sticks for
        # the rest of this turn's tool-calling loop instead of retrying the
        # already-failed provider again on every iteration.
        response, provider, model = await _call_llm_with_failover(
            provider,
            model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                *_histories[chat_id],
            ],
            tools=TOOLS,
            responses_session=responses_session,
        )
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd /Users/sebby/Developer/organist_bot/.worktrees/fix-gpt6-astra-responses-api && EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com .venv/bin/pytest tests/test_unified_agent.py -k "test_process_message_gpt_6_astra_two_round_tool_loop" -v
```

Expected: PASS.

- [ ] **Step 5: Full test suite, lint, format, type-check**

```bash
cd /Users/sebby/Developer/organist_bot/.worktrees/fix-gpt6-astra-responses-api && .venv/bin/ruff format organist_bot/ tests/ && .venv/bin/ruff check organist_bot/ tests/ && .venv/bin/mypy organist_bot/ && EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com .venv/bin/pytest --tb=short -q
```

Expected: everything green — the full suite (924 previously-passing tests + all new tests from Tasks 1-6), ruff clean, mypy clean.

- [ ] **Step 6: Commit**

```bash
cd /Users/sebby/Developer/organist_bot/.worktrees/fix-gpt6-astra-responses-api && git add organist_bot/integrations/unified_agent.py tests/test_unified_agent.py && git commit -m "feat: thread responses_session through process_message()'s tool loop

Part 6/7 -- makes gpt-6-astra usable end-to-end: a multi-round tool call
now correctly chains via previous_response_id across process_message()'s
while-loop iterations, and _histories[] ends up in the same flat shape any
other provider would produce, so a provider switch mid-conversation still
works.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 7: Documentation + final full quality gate

**Files:**
- Modify: `CLAUDE.md` (the "Unified Telegram bot" section's provider-switching paragraph)
- No new tests — this task documents and verifies, it doesn't change behavior.

- [ ] **Step 1: Load the writing-style skill**

This repo's `CLAUDE.md` mandates Google developer-documentation style for all documentation edits (active voice, sentence case, serial comma, contractions, no "click here", numbers spelled out zero-nine, em dash not en dash, etc.). Before editing `CLAUDE.md`, run:

Use the Skill tool with `skill: "writing-style"` and follow its guidance for the edit in Step 2.

- [ ] **Step 2: Add the one-sentence documentation note**

In `CLAUDE.md`, find this paragraph (in the "### `telegram_bot.py` — Unified Telegram bot" section):

```
**Provider switching, failover, and usage tracking.** `manage_llm_provider`'s `set` action never switches immediately — it stashes the requested `(provider, model_key)` in `unified_agent._pending_llm_switch` and returns a Confirm/Cancel button (`llm:confirm:<provider>/<model_key>` / `llm:cancel:<provider>/<model_key>`), handled by `telegram_bot.handle_llm_callback` exactly like the existing NEG-draft confirm flow; the switch only takes effect via `llm_confirm_switch`, and a stale or double-tapped button is a no-op once a newer request has replaced it. Independently, if the currently active provider's call fails mid-conversation, `_call_llm_with_failover` retries the other configured providers in a fixed order (`anthropic → openai → gemini`, skipping any without an API key); the first provider to succeed after a failure is persisted as the new default and reported via `alert.send_alert`, so an outage self-heals without a manual switch. Every successful call (first-try or failed-over) is recorded to `llm_usage_store` (`data/llm_usage.json`); `get_llm_usage_summary` reports today/all-time call counts and token totals per provider.
```

Add one sentence at the end of that paragraph, so it reads:

```
**Provider switching, failover, and usage tracking.** `manage_llm_provider`'s `set` action never switches immediately — it stashes the requested `(provider, model_key)` in `unified_agent._pending_llm_switch` and returns a Confirm/Cancel button (`llm:confirm:<provider>/<model_key>` / `llm:cancel:<provider>/<model_key>`), handled by `telegram_bot.handle_llm_callback` exactly like the existing NEG-draft confirm flow; the switch only takes effect via `llm_confirm_switch`, and a stale or double-tapped button is a no-op once a newer request has replaced it. Independently, if the currently active provider's call fails mid-conversation, `_call_llm_with_failover` retries the other configured providers in a fixed order (`anthropic → openai → gemini`, skipping any without an API key); the first provider to succeed after a failure is persisted as the new default and reported via `alert.send_alert`, so an outage self-heals without a manual switch. Every successful call (first-try or failed-over) is recorded to `llm_usage_store` (`data/llm_usage.json`); `get_llm_usage_summary` reports today/all-time call counts and token totals per provider. One model, `openai/gpt-6-astra`, can't serve tool calls through the Chat Completions endpoint at all, so `_call_llm_with_failover` routes it through OpenAI's Responses API (`litellm.aresponses()`, via `_call_openai_responses_api`) instead — every other model keeps using `acompletion()`.
```

- [ ] **Step 3: Verify the doc renders sensibly**

```bash
cd /Users/sebby/Developer/organist_bot/.worktrees/fix-gpt6-astra-responses-api && grep -c "gpt-6-astra" CLAUDE.md
```

Expected: at least 1 (confirms the sentence landed).

- [ ] **Step 4: Run the full local quality gate**

```bash
cd /Users/sebby/Developer/organist_bot/.worktrees/fix-gpt6-astra-responses-api && .venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy organist_bot/ && EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com .venv/bin/pytest --tb=short -q
```

Expected: ruff clean, ruff format clean (no diff), mypy clean, full pytest suite green (924 pre-existing + all new tests from Tasks 1-6, 1 deselected `live` test unaffected).

If `make` is preferred/available instead of calling tools directly (matches this repo's documented `make pre-push` gate), this is equivalent to running:

```bash
cd /Users/sebby/Developer/organist_bot/.worktrees/fix-gpt6-astra-responses-api && make pre-push
```

Note `make pre-push` also runs `bandit`/`semgrep` security scans, which the direct-tool-call sequence above does not — prefer `make pre-push` if it's available and working in this environment, since it's the actual gate `make ship` uses.

- [ ] **Step 5: Commit**

```bash
cd /Users/sebby/Developer/organist_bot/.worktrees/fix-gpt6-astra-responses-api && git add CLAUDE.md && git commit -m "docs: document gpt-6-astra's Responses API routing

Part 7/7 -- one-sentence addition to the Unified Telegram bot section.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Self-Review Notes (for whoever executes this plan)

- **Spec coverage:** §1 (Task 1), §2 (Task 1), §3 (Task 3), §4 (Task 2), §5 (Task 4), §6 (Task 5), §7 (Task 6), §8 (Task 5's comment update), §9 (Task 7). Error Handling section's status/error-raise behavior is Task 4. Every Testing-section bullet has a corresponding test in the task that introduces the function it tests.
- **No placeholders:** every step above has literal code, not a description of code.
- **Type/name consistency check:** `_call_openai_responses_api`'s signature (`model, api_key, messages, responses_session`) matches every call site (Task 5's wiring, Task 4's own tests). `_call_llm_with_failover`'s new `responses_session` parameter name matches its use in `process_message` (Task 6). `RESPONSES_TOOLS`/`_RESPONSES_API_MODELS`/`ResponsesApiError` names are identical everywhere they're referenced across tasks 1-6.
