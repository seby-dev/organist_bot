# Multi-Provider LLM Support for the Telegram Agent

**Date:** 2026-09-04

## Problem

The Telegram unified agent (`organist_bot/integrations/unified_agent.py`) is hardcoded to
Anthropic's Claude via `anthropic.AsyncAnthropic().messages.create(...)`, using
Anthropic's native message/content-block format and its own tool-calling shape. There is
no way to try OpenAI or Gemini models, and no way to switch providers without a code
change and redeploy.

## Goal

Add OpenAI and Gemini as alternative providers for the *main conversational agent only*
(not `reply_monitor.py` / `invoice_monitor.py`'s Haiku classifiers, which stay as-is —
out of scope), with full tool-calling parity across all three (all ~33 tools work
identically regardless of provider), and a natural-language way to switch between them
and their curated models from Telegram, e.g. "switch to GPT-6 Astra" or "use Gemini".

## Approach

Use [LiteLLM](https://github.com/BerriAI/litellm) (`litellm.acompletion`) instead of
calling the Anthropic SDK directly. LiteLLM accepts one canonical OpenAI-shaped
`messages`/`tools`/response format and internally translates to whichever backend the
`model="<provider>/<model-id>"` prefix selects (`anthropic/...`, `openai/...`,
`gemini/...`). This means **no separate per-provider adapter classes are needed** — one
generic call replaces the current Anthropic-specific one, and `process_message()`'s loop
becomes provider-agnostic by construction rather than by added abstraction.

This was chosen over hand-rolling three adapter classes: less code to write and
maintain, at the cost of one new runtime dependency. Discussed and approved with the
user directly (this repo otherwise hand-builds every integration).

---

## Design

### 1. New dependency

Add `litellm` to `pyproject.toml` (`[project] dependencies`, alongside the existing
`anthropic` — the raw `anthropic` package is no longer imported by `unified_agent.py`,
but LiteLLM depends on it internally, so no explicit removal is needed there).

### 2. Config (`organist_bot/config.py`)

Add two new `Settings` fields, matching the existing `anthropic_api_key: str = ""`
pattern:

```python
openai_api_key: str = ""
gemini_api_key: str = ""
```

### 3. Curated model registry (`unified_agent.py`)

A small module-level registry, one canonical LiteLLM `model=` string per entry:

```python
_PROVIDER_MODELS: dict[str, dict[str, str]] = {
    "anthropic": {
        "sonnet": "anthropic/claude-sonnet-4-6",   # today's existing default — unchanged
        "opus": "anthropic/claude-opus-4-6",         # VERIFY exact ID at implementation time
        "haiku": "anthropic/claude-haiku-4-5-20251001",  # matches reply_monitor.py's existing string
    },
    "openai": {
        "gpt-6-astra": "openai/gpt-6-astra",         # flagship — confirmed via OpenAI's own model docs (2026-09-04)
        "gpt-5.6-luna": "openai/gpt-5.6-luna",        # cost-efficient tier — confirmed via OpenAI's own model docs
    },
    "gemini": {
        "gemini-pro": "gemini/gemini-3.1-pro-preview",  # UNCONFIRMED — sources disagreed (see note below)
        "gemini-3.8-flash": "gemini/gemini-3.8-flash",  # flash tier — confirmed via Gemini's own model docs
    },
}
_DEFAULT_PROVIDER = "anthropic"
_DEFAULT_MODEL_KEY = "sonnet"  # -> anthropic/claude-sonnet-4-6, today's unchanged default
```

Verified against each provider's own model documentation on 2026-09-04 (not guessed) —
except the Gemini pro-tier ID, where two lookups disagreed: one indicated
`gemini-3.1-pro-preview` is the current top-tier model, another (likely surfacing a
stale example from Google's docs) said `gemini-2.5-pro`. **Confirm the actual current
Gemini pro-tier ID via Google AI Studio's live model picker before implementation** —
everything else in this registry is confirmed. This is the one open item this spec
defers to implementation time; the *structure* (three providers, 2 curated options each,
one designated default) is otherwise fully specified. Model names drift fast regardless,
so re-verify all of these immediately before writing the code, not from this document
alone.

`_PROVIDER_API_KEY_FIELD = {"anthropic": "anthropic_api_key", "openai": "openai_api_key", "gemini": "gemini_api_key"}`
maps provider name to the `Settings` field to check for configuration.

### 4. Runtime storage — widen `RuntimeConfigStore`

`organist_bot/runtime_config_store.py` is currently typed for `int` values only
(`min_fee`, `max_travel_minutes`, `poll_minutes`, `negotiable_fee`). Widen its value type
to `int | str` rather than adding a new store file — `llm_provider`/`llm_model` are
conceptually the same kind of thing (a global runtime override with a `.env`-derived
default), and this keeps one file/one tool-family for "runtime config" rather than
proliferating JSON stores. `data/runtime_config.json` gains two new possible keys:
`llm_provider` (default `"anthropic"`), `llm_model` (default the resolved
`anthropic/claude-sonnet-4-6` string, i.e. `_PROVIDER_MODELS["anthropic"]["sonnet"]`).

**`get()`'s widening must use `@overload`, not a flat `int | str` return type** — existing
callers (e.g. the scheduler's `runtime_config.get("poll_minutes", settings.poll_minutes)`,
used arithmetically) assume an `int` back and would fail mypy under a flattened
`int | str` return:

```python
@overload
def get(self, key: str, default: int) -> int: ...
@overload
def get(self, key: str, default: str) -> str: ...
def get(self, key: str, default: int | str) -> int | str:
    return _read().get(key, default)
```

`set(self, key: str, value: int | str) -> None` needs no overload — passing an `int`
already satisfies a widened `int | str` parameter, so no existing caller breaks.

`manage_config`'s existing `_RANGES`/`_DEFAULTS`-based int validation is untouched — it
only ever reads/writes its existing four int keys, unaffected by the wider store type.

### 5. New tool: `manage_llm_provider`

Added to `TOOLS`, alongside `manage_config`, using the same `action` pattern:

```python
{
    "name": "manage_llm_provider",
    "description": (
        "Read or switch which LLM provider/model powers this conversation. "
        "Providers: anthropic (sonnet/opus/haiku), openai (gpt-6-astra/gpt-5.6-luna), "
        "gemini (gemini-pro/gemini-3.8-flash). "
        "Use action='get' to show the current provider/model. "
        "Use action='set' with 'provider' to switch — if 'model' is omitted, list that "
        "provider's options and ask the user to pick one before calling set again. "
        "Use action='reset' to restore the default (anthropic/sonnet). "
        "Checks the provider's API key is configured before doing anything else — "
        "refuses immediately (even before listing model options) if it isn't."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["get", "set", "reset"]},
            "provider": {
                "type": "string",
                "enum": ["anthropic", "openai", "gemini"],
                "description": "Required for set.",
            },
            "model": {
                "type": "string",
                "description": (
                    "One of that provider's curated model keys (e.g. 'sonnet', "
                    "'gpt-5.6-luna'). Optional for set — omit to see the options."
                ),
            },
        },
        "required": ["action"],
    },
}
```

**Handler behavior** (`_handle_manage_llm_provider`, registered via `@_handler("manage_llm_provider")`,
in `_VERBATIM_RESPONSE_TOOLS`):

- `get`: reads `runtime_config.get("llm_provider", _DEFAULT_PROVIDER)` and
  `runtime_config.get("llm_model", <default model string>)`, returns them formatted.
- `set` with no `provider` at all (schema only requires `action`, not `provider`):
  explicit error — `"provider is required for action='set'."`
- `set` with `provider` not in `_PROVIDER_MODELS`: error listing valid providers.
- `set` with valid `provider`: checks `settings.<provider>_api_key` is non-empty
  *first, before anything else* (see Error Handling) — refuses immediately if not, even
  before getting to the missing/invalid-`model` case below.
- `set` with valid, configured `provider` but missing/invalid `model`: returns the
  curated list for that provider as plain text (e.g. "Which OpenAI model? gpt-6-astra
  (flagship) or gpt-5.6-luna (cost-efficient)") and does **not** change any config. No
  new stashing/callback-button plumbing is needed here (unlike the NEG-draft picker) —
  the *current* provider is still active, so the agent can just ask conversationally and
  the user's next natural-language reply (e.g. "gpt-6-astra") drives a second `set` call
  with `model` filled in, entirely within the existing tool-calling loop.
- `set` with valid `provider` and `model`: validates `settings.<provider>_api_key` is
  set (else refuses — see Error Handling), then
  `runtime_config.set("llm_provider", provider)` and
  `runtime_config.set("llm_model", _PROVIDER_MODELS[provider][model])`. Returns
  confirmation: `"Switched to {provider}/{model}. Takes effect on your next message."`
- `reset`: `runtime_config.reset("llm_provider")` + `runtime_config.reset("llm_model")`.

### 6. `process_message()` rewrite

Current (Anthropic-specific):

```python
import anthropic
client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
...
response = await client.messages.create(
    model="claude-sonnet-4-6", max_tokens=4096,
    system=SYSTEM_PROMPT, tools=TOOLS, messages=_histories[chat_id],
)
_histories[chat_id].append({"role": "assistant", "content": response.content})
if response.stop_reason == "end_turn":
    for block in response.content:
        if hasattr(block, "text"):
            responses.append(AgentResponse(text=block.text))
    break
...
for block in response.content:
    if block.type != "tool_use": continue
    ...
    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
```

New (provider-agnostic, via LiteLLM):

```python
import litellm

provider = runtime_config.get("llm_provider", _DEFAULT_PROVIDER)
if provider not in _PROVIDER_MODELS:
    # Guards against a stale/invalid value left in runtime_config.json — e.g. from a
    # prior session, after a future code update removes a provider from the registry.
    logger.warning("Unknown stored llm_provider %r, resetting to default", provider)
    runtime_config.reset("llm_provider")
    runtime_config.reset("llm_model")
    provider = _DEFAULT_PROVIDER
model = runtime_config.get("llm_model", _PROVIDER_MODELS[_DEFAULT_PROVIDER][_DEFAULT_MODEL_KEY])
api_key = getattr(settings, _PROVIDER_API_KEY_FIELD[provider])
...
response = await litellm.acompletion(
    model=model, max_tokens=4096, api_key=api_key,
    messages=[{"role": "system", "content": SYSTEM_PROMPT}, *_histories[chat_id]],
    tools=TOOLS,  # now OpenAI function-calling shape — see item 7
)
msg = response.choices[0].message
_histories[chat_id].append(msg.model_dump())  # NOT exclude_none=True — see note below
if not msg.tool_calls:
    if msg.content:
        responses.append(AgentResponse(text=msg.content))
    break
tool_results = []
for tc in msg.tool_calls:
    name, args = tc.function.name, json.loads(tc.function.arguments)
    ...
    result = await _execute_tool(name, args, chat_id)
    ...
    tool_results.append({"role": "tool", "tool_call_id": tc.id, "name": name, "content": result})
_histories[chat_id].extend(tool_results)
```

Key changes from today: system prompt is passed as the first `messages` entry (not a
separate `system=` kwarg — Anthropic-only concept; LiteLLM translates a `system` role
message into whatever the backend provider expects), tool results become `role: "tool"`
messages appended flat to history (not nested inside a `role: "user"` content-block
list), and `msg.model_dump()` stores a plain, JSON-serializable dict instead of raw
Anthropic SDK objects (today's `response.content` is stored as literal SDK objects — this
was never provider-portable, and this rewrite fixes that as a byproduct).

**Do not pass `exclude_none=True` to `model_dump()`.** A tool-call turn has
`content: None` alongside `tool_calls: [...]`; dropping the `None` key entirely (rather
than keeping it as an explicit null) breaks the *next* call when the active provider is
Anthropic — LiteLLM's Anthropic translation requires `content` to be present on every
message (empty string/list, never an absent key), so a history built with
`exclude_none=True` works on turn 1 but raises a validation error on turn 2 whenever the
previous turn was a tool call. Caught in spec review, not by construction — confirm this
with a real multi-turn tool-call test against `anthropic/...` during implementation, not
just a shape assertion.

`_execute_tool`, all `_TOOL_HANDLERS` implementations, `on_step` progress callbacks,
`_VERBATIM_RESPONSE_TOOLS`/`_PDF_RESPONSE_TOOLS` special-casing, and
`stash_pending_neg_instruction` are **entirely unaffected** — they operate on tool
name/args/result strings, never on provider-specific message shapes.

### 7. `TOOLS` list — schema shape change

Every entry's `"input_schema"` key is renamed and the entry wrapped to OpenAI's function-
calling shape (LiteLLM's canonical input format regardless of backend):

```python
# Before (Anthropic-native):
{"name": "add_gig", "description": "...", "input_schema": {...}}

# After (OpenAI-shape, LiteLLM canonical):
{"type": "function", "function": {"name": "add_gig", "description": "...", "parameters": {...}}}
```

This is a mechanical, 1:1 rewrite of every one of the ~33 entries — no schema content
changes, only the wrapping. `_TOOL_HANDLERS`/`_execute_tool`/`_VERBATIM_RESPONSE_TOOLS`
key off `name` alone and are unaffected.

### 8. `_trim_history()` re-check

`_trim_history` (in `unified_agent.py`) currently cuts only at a
`{"role": "user", "content": <str>}` boundary, to avoid orphaning an Anthropic
`tool_use`/`tool_result` pair. Under the new OpenAI-style flat role scheme
(`system`/`user`/`assistant`/`tool`), a tool result is its own `role: "tool"` message,
never nested inside a `user` turn — so the same "cut only at a plain-string `user`
message" rule still correctly avoids ever separating an `assistant` tool-call message
from its `tool` result message(s), since those always sit strictly *after* the `user`
turn that triggered them and *before* the next one. No `_trim_history` logic change
needed.

**The existing `TestTrimHistory` test (`tests/test_unified_agent.py`) needs more than a
fixture swap, though.** Its current pairing-check walks each message's list-type
`content` looking for `type: "tool_use"`/`type: "tool_result"` blocks to detect an
orphaned pair — that's an Anthropic-shape-specific check. Under the new flat shape, tool
call IDs live on the assistant message's top-level `tool_calls` field and results are
top-level `role: "tool"` messages, neither of which is inside a list-content block. If
only the fixture data is swapped to the new shape and this check is left as-is, the loop
finds no list-content blocks to iterate, `pending_tool_use_ids` never gets populated, and
the assertion passes vacuously — silently testing nothing. The pairing-check *logic*
must be rewritten too, walking `tool_calls` on assistant messages and matching against
`tool_call_id` on subsequent `role: "tool"` messages.

### 9. System prompt

No content change needed for the "## Runtime config" section's sibling sections — add
one new short section (matching the style of the removed "## Pipeline stats" section)
documenting the new tool:

```
## LLM provider
- "Switch to GPT-6 Astra" / "use Gemini" / "what model are we using?" → manage_llm_provider.
- If you say a provider without a model, I'll list that provider's options and ask which one.
```

---

## Error Handling

- **Missing API key on `set`**: refuses immediately, no config change —
  `"Can't switch to openai — OPENAI_API_KEY isn't set."` (per explicit product decision:
  fail fast at switch time, not on the next message).
- **Unknown provider/model key**: `"Unknown provider 'x'. Valid: anthropic, openai, gemini."`
  / `"Unknown model 'y' for openai. Valid: gpt-6-astra, gpt-5.6-luna."`
- **LiteLLM API call failure** (network, rate limit, invalid model ID post-switch):
  propagates as an exception out of `litellm.acompletion` inside `process_message()`'s
  loop — currently *uncaught* there today (an Anthropic SDK error would already crash
  the same way), so behavior is unchanged, not a regression. Out of scope for this spec
  to add new top-level error handling to `process_message()` beyond what exists today.
- **Tool execution errors**: entirely unchanged — `_execute_tool`'s existing
  try/except-and-return-error-json wrapping around each `_TOOL_HANDLERS` call is
  untouched by this change.

---

## Testing

- **Rewrite** the `process_message()`-level tests in `tests/test_unified_agent.py` that
  currently monkeypatch `sys.modules["anthropic"]` with a `SimpleNamespace`/`MagicMock`
  fake client (the `fake_anthropic_module` pattern, several call sites) to instead
  monkeypatch `litellm.acompletion` directly (an `AsyncMock` returning an object shaped
  like LiteLLM's response — `response.choices[0].message.tool_calls` /
  `.message.content`).
- **New tests** for `manage_llm_provider`: `get` (default + after a set), `set` with a
  configured key (success), `set` with a missing key (refusal, no config mutation), `set`
  with no `provider` at all, `set` with no model (returns curated list, no config
  mutation), `set` with an unknown provider/model, `reset`, and a stale/invalid stored
  `llm_provider` value falling back to the default instead of crashing.
- **Rewrite** `TestTrimHistory` — both the fixtures (flat `role: "tool"` shape) *and* the
  pairing-check assertion logic itself, which currently only understands Anthropic's
  nested content-block shape and would otherwise pass vacuously against the new format
  (item 8).
- **New test**: `TOOLS` list — every entry has the new
  `{"type": "function", "function": {...}}` shape (a single structural assertion over
  the whole list catches any entry accidentally left in the old shape).
- **Fix existing test** `test_seen_not_in_manage_filter_suspensions_enum`
  (`tests/test_unified_agent.py:1032-1034`) — breaks in two ways under the new `TOOLS`
  shape, not just one: `next(t for t in TOOLS if t["name"] == "manage_filter_suspensions")`
  raises `KeyError` on `t["name"]` (name moves to `t["function"]["name"]`), and even once
  that's fixed, `tool_def["input_schema"]["properties"]["filter"]["enum"]` must become
  `tool_def["function"]["parameters"]["properties"]["filter"]["enum"]`. This is the only
  existing test in the file found reading a tool's schema directly by key (confirmed via
  `grep -n '"input_schema"' tests/test_unified_agent.py` — one hit), but re-run that grep
  during implementation in case another was added since.
- A real multi-turn tool-call conversation against `model="anthropic/..."` (not just a
  response-shape assertion) to catch the `content: None` history round-trip issue noted
  in item 6 before it reaches production.
- Existing tests for individual tool handlers (`_TOOL_HANDLERS["add_gig"]` etc.) are
  **unaffected** — they call handlers directly, never through `process_message()`.

---

## Files Changed

| File | Change |
|------|--------|
| `pyproject.toml` | Add `litellm` dependency |
| `organist_bot/config.py` | Add `openai_api_key`, `gemini_api_key` fields |
| `organist_bot/runtime_config_store.py` | Widen value type `int` → `int \| str`; add `@overload` to `get()` |
| `organist_bot/integrations/unified_agent.py` | New `_PROVIDER_MODELS` registry; new `manage_llm_provider` tool + handler; rewrite `process_message()` to use `litellm.acompletion`; rewrite `TOOLS` to OpenAI function-calling shape; add "LLM provider" system-prompt section |
| `tests/test_unified_agent.py` | Rewrite `process_message()`-level tests to mock `litellm.acompletion`; rewrite `TestTrimHistory` fixtures *and* pairing-check logic; new `manage_llm_provider` tests; new `TOOLS`-shape test; fix `test_seen_not_in_manage_filter_suspensions_enum`'s schema key path |
| `README.md` / `CLAUDE.md` | Document `OPENAI_API_KEY`/`GEMINI_API_KEY` config, the `manage_llm_provider` tool, and update the "~33 tools" count to ~34 |

---

## Out of Scope

- `reply_monitor.py` / `invoice_monitor.py`'s Claude Haiku classification calls — stay
  hardcoded to Anthropic, unrelated to this feature's tool-calling problem (explicit
  product decision).
- Per-chat provider selection — this is a single global default for the whole bot
  process (explicit product decision), consistent with `runtime_config_store`'s existing
  min_fee/poll_minutes pattern.
- Streaming responses — the current code doesn't stream (`client.messages.create`, not
  `.stream()`), and this spec doesn't introduce it.
- A real Telegram slash-command (`/provider ...`) — switching stays natural-language,
  through the existing free-text → unified-agent path, consistent with everything else
  in the bot (explicit product decision).
- Cost/usage tracking per provider.
