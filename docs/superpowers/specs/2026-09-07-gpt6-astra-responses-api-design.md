# Native Responses API Support for gpt-6-astra

**Date:** 2026-09-07

## Problem

`_PROVIDER_MODELS["openai"]["gpt-6-astra"]` (`unified_agent.py`) maps to the real model
ID `openai/gpt-6-astra`, offered to users via `manage_llm_provider` (its tool
description lists "openai (gpt-6-astra/gpt-5.6-luna)"). Every conversation through the
unified agent always sends `tools` (it has ~35 of them), and `gpt-6-astra` cannot be
used with function tools through LiteLLM's `litellm.acompletion()` (the classic
`/v1/chat/completions` wrapper) — confirmed live against OpenAI's real API:

- A plain call with `tools` and no `reasoning_effort` fails: *"Function tools with
  reasoning_effort are not supported for gpt-6-astra in /v1/chat/completions. To use
  function tools, use /v1/responses or set reasoning_effort to 'none'."*
- Forcing `reasoning_effort="none"` (via LiteLLM's `allowed_openai_params`) is then
  rejected by OpenAI itself: *"Unsupported value: 'reasoning_effort' does not support
  'none' with this model. Supported values are: 'low', 'medium', 'high', and 'xhigh'."*

There is no way to satisfy both constraints through `/v1/chat/completions`. A user who
manually selects `gpt-6-astra` via `manage_llm_provider(action="set", provider="openai",
model="gpt-6-astra")` gets a working switch confirmation, then every single message
afterward fails with no indication why. (`_DEFAULT_MODEL_KEY_PER_PROVIDER["openai"]` was
already changed to `"gpt-5.6-luna"` in a prior fix so *automatic failover* never lands on
this dead end — that fix is unrelated and stays as-is; see Out of Scope.)

## Approach

Add real support for OpenAI's `/v1/responses` API (`litellm.aresponses()`), used only
for `gpt-6-astra`. Every other model keeps using `litellm.acompletion()` unchanged.

This was the explicit product decision (over simply dropping the model from
`_PROVIDER_MODELS`) — confirmed with the user directly, who prioritized keeping the
model option over minimizing implementation work.

### Why this needs care, not just a different function call

The Responses API uses a different request/response shape than Chat Completions:
flat function-tool schemas (`{"type": "function", "name", "description", "parameters"}`,
no nested `"function"` key), `input` items instead of `messages`, and — critically for a
reasoning model like `gpt-6-astra` — **an item-pairing constraint**: when a response
includes a `reasoning` item followed by a `function_call` item, that `reasoning` item
must be threaded back verbatim (in the same position) on the follow-up request that
supplies the matching `function_call_output`, or OpenAI's API rejects the request with
`invalid_request_error: Item 'rs_...' of type 'reasoning' was provided without its
required following item.` This is a well-documented, common failure mode for anyone
hand-rolling Responses API tool loops (confirmed via multiple OpenAI community/GitHub
reports during spec research) — silently dropping the reasoning item (e.g. by naively
round-tripping through this codebase's existing OpenAI-chat-completions-shaped
`_histories[chat_id]`, which has no field to carry it) would trade one broken model for
another, differently broken, in the one interaction pattern (tool calling) this agent
exists for.

**Design response:** avoid ever needing to manually replay a `reasoning` item at all, by
using OpenAI's `previous_response_id` chaining for the multi-round tool-call loop within
one turn. That mechanism keeps the full response (including any reasoning items) on
OpenAI's servers; a follow-up request only needs to supply the *new* items (the tool
results) plus the id of the response it's continuing. This sidesteps the pairing
constraint entirely rather than trying to satisfy it by hand.

There's a second, independent reason the bootstrap translator (§3) never replays
*historical* tool-call round trips either, even ones with no reasoning item at stake:
`_histories[chat_id]` is cross-provider. A tool call from three turns ago may carry an
Anthropic `toolu_...` id or a Gemini-originated one — replaying that as a `call_id` into
an OpenAI Responses API request is unlikely to be meaningful to OpenAI's servers and
isn't a scenario either provider's SDK documents. Dropping historical tool-call round
trips avoids that undefined territory regardless of the reasoning-item question. (The
reasoning-item pairing constraint, confirmed via community reports during spec research,
is not independently verified against a live call in this session — treated here as the
documented, likely behavior, not a certainty.)

---

## Design

### 1. Which models use the Responses API

```python
# Models that reject function tools on /v1/chat/completions and must go through
# /v1/responses instead (litellm.aresponses()) -- see spec 2026-09-07. Currently
# just gpt-6-astra; every other curated model keeps using acompletion().
_RESPONSES_API_MODELS = {"openai/gpt-6-astra"}
```

Keyed by the resolved model string (`_PROVIDER_MODELS["openai"]["gpt-6-astra"]`), not
provider name — `openai/gpt-5.6-luna` stays on `acompletion()`.

### 2. Flat tool schema for the Responses API

`_TOOLS_SCHEMA` (Anthropic-shaped: `name`/`description`/`input_schema`) already feeds
`_to_function_tool()` → `TOOLS` (nested OpenAI chat-completions shape). Add a second,
flat converter and list, both derived from the same `_TOOLS_SCHEMA` source of truth so
the two never drift:

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

### 3. Translating this app's history into Responses API `input` items

Two translators, both operating on the same OpenAI-chat-completions-shaped `messages`
list (`[{"role": "system", ...}, *_histories[chat_id]]`) this module already builds for
every `acompletion()` call — no new history format is introduced.

**Bootstrap translator** — used for the *first* Responses API call of a turn (no
`previous_response_id` yet). Deliberately **drops** completed tool-call round trips from
earlier turns (any `assistant` message with `tool_calls` and the `tool` messages that
follow it) rather than re-expressing them as `function_call`/`function_call_output`
items:

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
    function_call_output is replayed -- see spec 2026-09-07.

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
```

This is a one-way information loss versus what a Claude/Gemini turn would see (those get
the full history, including past tool calls) — accepted as a minor cross-provider
context gap for a rarely-used curated model option, not a correctness bug: the
surrounding user/assistant text turns (which usually recap what a tool call did) still
carry the gist forward. See Out of Scope.

**Delta translator** — used for every subsequent call within the same turn's tool loop,
sent alongside `previous_response_id`. Translates *only* the new `tool` messages (the
`assistant` tool-calls message itself needs no replay — OpenAI's server already has it,
as part of the response being continued):

```python
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

### 4. Translating a Responses API result back into chat-completions shape

So `process_message()`'s loop, `_record_llm_usage`, and history bookkeeping need **no
branching** and work identically regardless of which endpoint actually served the call:

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
    content = "".join(text_parts) or None if not tool_calls_out else None

    def _model_dump() -> dict:
        return {
            "role": "assistant",
            "content": content,
            "tool_calls": (
                [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
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

Field names above (`call_id`, `name`, `arguments` on a `function_call` output item;
`content`/`role`/`type` on a `message` item; `text`/`type` on an `output_text` content
item; `input_tokens`/`output_tokens` on usage) were confirmed directly against the
installed `openai`/`litellm` packages' pydantic model fields in this repo's own `.venv`
during spec research, not guessed.

**Critical id detail:** the synthetic tool call's `id` must be the Responses API item's
`call_id` (`call_...`), not its `id` (`fc_...`) — `call_id` is what `output` must
reference in the matching `function_call_output` item, and it's what
`process_message()` already stores as `tool_call_id` on the `tool`-role history message
(line ~2510: `{"role": "tool", "tool_call_id": tc.id, ...}`) and threads back via
`_responses_tool_outputs_from_messages` above. Using the wrong id silently breaks the
`call_id` linkage OpenAI needs to match a tool result to its call.

### 5. The adapter: `_call_openai_responses_api`

```python
class ResponsesApiError(RuntimeError):
    """Raised when OpenAI's /v1/responses returns a non-"completed" response
    (status "failed"/"incomplete", or a populated `error` field) instead of
    raising an HTTP-level exception. litellm.aresponses() does not itself raise
    for these -- they're a normal 200 response with a status field describing
    what went wrong -- so _call_openai_responses_api raises explicitly to fold
    them into _call_llm_with_failover's existing except/failover path instead
    of silently returning an empty turn (see spec 2026-09-07)."""


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
    see spec 2026-09-07 for why this replaces manually replaying reasoning
    items. previous_response_id chaining requires the referenced response to
    be stored server-side, hence `store=True` below (litellm's/OpenAI's
    default already, but pinned explicitly since correctness depends on it --
    an org-level zero-data-retention setting would break every second round of
    a tool loop otherwise).

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

### 6. Wiring into `_call_llm_with_failover`

Add an optional `responses_session` parameter (defaults to a fresh dict so every
existing direct call/test that doesn't pass one keeps working unchanged), and branch per
candidate on whether its model needs the Responses API:

```python
async def _call_llm_with_failover(
    provider: str,
    model: str,
    messages: list[dict],
    tools: list[dict],
    responses_session: dict | None = None,
):
    import litellm

    if responses_session is None:
        responses_session = {}
    ...
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
                    max_completion_tokens=4096,
                    api_key=getattr(settings, _PROVIDER_API_KEY_FIELD[p]),
                    messages=messages,
                    tools=tools,
                )
        except Exception as exc:
            ...  # unchanged
```

Everything after the `try` (usage recording, failover promotion + alert, the
all-failed summary/alert/raise) is **unchanged** — it already only touches the returned
`response`/`exc`, never which branch produced them.

Since `gpt-6-astra` is never a failover *target* (`_DEFAULT_MODEL_KEY_PER_PROVIDER`
still resolves openai's failover default to `gpt-5.6-luna`), the Responses API branch is
only ever exercised for `candidates[0]` — the caller's own explicitly-active
provider/model. No change needed to the failover-candidate-building logic above this
loop.

### 7. `process_message()` — threading `responses_session` per turn

One line added before the `while True:` loop, and the new kwarg passed through the
existing call:

```python
responses_session: dict = {}
while True:
    response, provider, model = await _call_llm_with_failover(
        provider,
        model,
        messages=[{"role": "system", "content": SYSTEM_PROMPT}, *_histories[chat_id]],
        tools=TOOLS,
        responses_session=responses_session,
    )
    ...
```

A fresh dict per call to `process_message()` (i.e. per incoming user text message) is
correct and sufficient: `_histories[chat_id]` never leaves a dangling unresolved
`tool_calls` message between turns (every `tool_calls` message is always immediately
followed by its matching `tool` messages before the loop can exit — see the existing
`if not tool_results: ... break` / `_histories[chat_id].extend(tool_results)` structure),
so every new turn starts from a clean boundary and the bootstrap translator (§3) is
always a safe, correct starting point. `responses_session` never needs to survive a
provider switch, a bot restart, or `_trim_history()`/`reset_conversation()` — it simply
doesn't outlive the call that created it.

### 8. `_DEFAULT_MODEL_KEY_PER_PROVIDER` comment update

The existing comment (lines ~76-84) says gpt-6-astra "can never actually serve a
failover call" — no longer accurate now that the Responses API branch exists. Update the
comment to explain the current, narrower reasoning: the failover default stays
`gpt-5.6-luna` deliberately, not because gpt-6-astra is broken anymore, but because an
outage-driven failover should land on the plain, fast `acompletion()` path rather than
the added Responses API round-trip machinery. This is a comment-only change — the actual
default value is unchanged, and reverting it to `gpt-6-astra` is out of scope (see Out
of Scope).

### 9. Documentation

`CLAUDE.md`'s "Unified Telegram bot" section (the paragraph describing
`manage_llm_provider`/`_call_llm_with_failover`) gets one short addition noting that
`gpt-6-astra` specifically routes through `litellm.aresponses()` (the Responses API)
rather than `acompletion()`, and why — one sentence, matching the section's existing
density, not a restatement of this whole spec.

---

## Error Handling

- **`litellm.aresponses()` raising** (network error, invalid request, etc.): caught by
  the existing `except Exception as exc:` in `_call_llm_with_failover`'s per-candidate
  loop exactly like an `acompletion()` failure — falls through to the next configured
  provider via the existing failover machinery. No new error handling needed; this is
  the same `try` block, unchanged.
- **A "completed" response with no usable content** (status `"failed"`/`"incomplete"`,
  or a populated `error` field — e.g. the output-token cap was hit mid-reasoning, or the
  request was flagged) is not itself an exception from `litellm.aresponses()`; left
  unchecked it would produce a chat-completions-shaped message with `content=None,
  tool_calls=None`, which `process_message()` would append to history and then return no
  reply to the user at all. `_call_openai_responses_api` raises `ResponsesApiError`
  itself in this case (§5), which the existing `except Exception` in
  `_call_llm_with_failover` catches identically to a network failure, engaging failover.
- **A stale `responses_session` never applies across turns** — since it's a fresh local
  per `process_message()` call, there's no "stale previous_response_id from a prior
  turn" failure mode to guard against by construction (§7).
- **`RESPONSES_TOOLS` / `_TOOLS_SCHEMA` drift**: impossible by construction — both
  `TOOLS` and `RESPONSES_TOOLS` are derived from the same `_TOOLS_SCHEMA` list.
- **Failover permanence**: like every other provider today, a single transient
  `gpt-6-astra` failure mid-turn permanently switches the persisted default (`_i > 0`
  promotion logic, unchanged) away from a model the user explicitly chose, not just for
  that one call. Pre-existing behavior for all providers, not new — noted here because
  the Responses API path now has an extra failure mode (the status/error check above)
  the `acompletion()` providers don't.

---

## Testing

- **`_to_responses_tool` / `RESPONSES_TOOLS`**: one structural test asserting every
  entry is the flat `{"type": "function", "name", "description", "parameters"}` shape
  (mirrors the existing `TOOLS`-shape test pattern) and that `len(RESPONSES_TOOLS) ==
  len(TOOLS)`.
- **`_responses_input_from_messages`**: user text → `message`/`user` item; assistant
  text-only turn → `message`/`assistant` item; an assistant `tool_calls` message plus its
  `tool` messages → both dropped (empty output for that pair); an assistant message with
  *both* `content` and `tool_calls` → the text is kept, the tool_calls are still dropped;
  a **dangling** assistant `tool_calls` message with no matching `tool` message at all
  (simulating a crash mid-turn on a previous call, e.g. `[user, assistant(tool_calls,
  content=None), user]`) → still cleanly dropped, producing just the two user items —
  confirms the translator doesn't rely on turns always being cleanly resolved, it's
  simply unconditional about dropping any assistant-with-tool_calls-and-no-content
  message. Caller is responsible for passing `messages[1:]` (system message excluded) —
  a test with a `role: "system"` first element left in would only pass by accident since
  the translator has no `elif role == "system"` branch; assert directly that a bare
  `[{"role": "system", ...}, ...]` list is never passed to this function in
  `_call_openai_responses_api`'s own test instead (see below).
- **`_responses_tool_outputs_from_messages`**: given `[assistant_tool_calls_msg,
  tool_msg]`, returns exactly one `function_call_output` item with the tool message's
  `tool_call_id`/`content` as `call_id`/`output`; the assistant message itself produces
  no item.
- **`_chat_message_from_responses_output`**: a `message`-type output item with
  `output_text` content → `.content` set, `.tool_calls` `None`; one or more
  `function_call`-type items → `.tool_calls` populated with `.id` set to the item's
  `call_id` (not `id`) and `.content` `None`; `.model_dump()` matches the exact dict
  shape `_fake_litellm_response`'s existing test helper already asserts against
  elsewhere in the suite (explicit `content: None` key, not an absent key — same
  constraint noted in the original multi-provider spec).
- **`_chat_response_from_responses_api`**: usage translation —
  `usage.input_tokens`/`usage.output_tokens` in → `.usage.prompt_tokens`/
  `.usage.completion_tokens` out; `usage=None` → `.usage` `None` (matches
  `_record_llm_usage`'s existing `getattr(response, "usage", None)` early-return path).
- **`_call_openai_responses_api`**: mock `litellm.aresponses` (an `AsyncMock`, matching
  this file's existing `litellm.acompletion` mocking convention) —
  - first call (`responses_session` empty): asserts `previous_response_id=None` was
    passed and `input` came from the bootstrap translator.
  - second call (`responses_session` pre-populated from a first call): asserts
    `previous_response_id` was passed through and `input` came from the delta
    translator (only the new tool messages, not the full history).
  - asserts `responses_session` is mutated in place with the new `previous_response_id`
    and `synced_len` after each call.
  - asserts the first call's `input` is built from `messages[1:]` (not the full list
    including the system message at index 0) — pins the slicing responsibility living in
    the adapter, not the translator.
  - asserts `store=True` is passed.
  - a response with `status="incomplete"` (or `status="completed"` with a non-`None`
    `error`) raises `ResponsesApiError` and does **not** mutate `responses_session` —
    confirms the fix for the "silently empty turn" failure mode (§5, Error Handling).
- **`_call_llm_with_failover` routing**: a candidate whose model is in
  `_RESPONSES_API_MODELS` calls the (mocked) Responses adapter, not `acompletion`; a
  candidate whose model isn't still calls `acompletion` as today — extends the existing
  `TestCallLlmWithFailover` class, same mocking conventions (`monkeypatch.setattr(litellm,
  "acompletion", ...)` alongside a new `monkeypatch.setattr(litellm, "aresponses",
  ...)`). Also: a `ResponsesApiError` from the `gpt-6-astra` candidate (e.g. an
  `"incomplete"` status) falls through to the next configured provider exactly like a
  network exception would — reuses the existing `test_failure_then_success_promotes_the_
  working_provider`-style setup with `aresponses` as the failing call instead of
  `acompletion`.
- **End-to-end `process_message()` test**: active model `openai/gpt-6-astra` (set via
  `runtime_config.set("llm_provider", "openai")` /
  `runtime_config.set("llm_model", "openai/gpt-6-astra")`, with a teardown resetting both
  — same pattern `TestCallLlmWithFailover` already uses), `settings.openai_api_key`
  monkeypatched non-empty, and `litellm.acompletion` also monkeypatched (to a mock that
  fails the test if called) so that if the `aresponses` mock is ever skipped by a routing
  bug, the test fails loudly instead of silently falling through to a real network call.
  Mock `litellm.aresponses` with a `side_effect` of two responses — first with a
  `function_call` output item (`status="completed"`), second with a plain
  `message`/`output_text` item (`status="completed"`) — and assert: (a) the tool actually
  executes (mirrors the existing on_step-progress test's pattern), (b) the second
  `aresponses` call received `previous_response_id` matching the first response's `id`
  and an `input` containing only the `function_call_output` item (not the full history),
  and (c) `_histories[chat_id]` ends up in the same flat chat-completions shape
  (`role: "assistant"` with `tool_calls`, `role: "tool"` with `tool_call_id`) that an
  Anthropic/Gemini turn would produce — so a provider switch immediately after this turn
  works unchanged.
- Existing `TestCallLlmWithFailover`/`TestManageLlmProvider`/process_message tests that
  don't touch `gpt-6-astra` are **unaffected** — `responses_session` defaults to `None`
  →`{}` and every candidate they exercise stays on the `acompletion()` branch.

---

## Files Changed

| File | Change |
|------|--------|
| `organist_bot/integrations/unified_agent.py` | Add `_RESPONSES_API_MODELS`, `_to_responses_tool`/`RESPONSES_TOOLS`, the four translation helpers (§3-4), `ResponsesApiError`, `_call_openai_responses_api`; extend `_call_llm_with_failover` with `responses_session` + per-candidate branching; thread `responses_session` through `process_message()`; update the `_DEFAULT_MODEL_KEY_PER_PROVIDER` comment |
| `tests/test_unified_agent.py` | New tests per the Testing section above |
| `CLAUDE.md` | One-sentence addition noting `gpt-6-astra` routes through the Responses API and why |

---

## Out of Scope

- Reverting `_DEFAULT_MODEL_KEY_PER_PROVIDER["openai"]` back to `gpt-6-astra` for
  automatic failover — that was a separate, already-shipped fix; nothing about adding
  standalone support changes the argument for keeping failover on the simpler,
  `acompletion()`-only path (§8). Not requested, and changing it would need its own
  justification (e.g. cost/latency comparison) this spec doesn't make.
- Responses API support for any other model/provider — `_RESPONSES_API_MODELS` is
  scoped to the one model that actually needs it today.
- Preserving full tool-call history fidelity across provider switches (§3's bootstrap
  translator drops old tool-call round trips for the Responses API specifically) — an
  accepted, documented tradeoff, not something this spec attempts to fix generally.
- Streaming responses — `litellm.aresponses()` is called without `stream=True`, matching
  this module's existing non-streaming `acompletion()` usage.
- Any change to `manage_llm_provider`'s tool description, schema, or confirm/cancel
  flow — `gpt-6-astra` was already a listed, selectable option; this spec makes that
  option actually work, it doesn't change how it's selected.
- **Fixing the pre-existing "dangling `tool_calls` message" hazard in `process_message()`
  itself** — spec review found that an unguarded `json.loads(tc.function.arguments)` or
  an unhandled Telegram exception from `on_step()` can leave `_histories[chat_id]` with
  an assistant `tool_calls` message and no matching `tool` messages if either raises
  mid-loop, on *any* provider, not just `gpt-6-astra`. This spec's bootstrap translator
  (§3) is tolerant of that state by construction, so it's not a blocker here, but the
  underlying hazard predates this spec and affects every provider — worth its own fix,
  tracked separately rather than folded into this change's scope.
