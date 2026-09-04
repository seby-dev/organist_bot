# Multi-Provider LLM Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the Telegram unified agent run on Claude, OpenAI, or Gemini — switchable
at runtime via natural language — with full tool-calling parity across all three, by
replacing the direct Anthropic SDK call in `process_message()` with `litellm.acompletion`.

**Architecture:** LiteLLM normalizes all three providers behind one OpenAI-shaped
request/response format, so no per-provider adapter classes are needed — one generic
loop in `process_message()` replaces the current Anthropic-specific one. A new
`manage_llm_provider` tool (modeled on the existing `manage_config` tool) reads/writes
the active provider+model to `runtime_config_store` (widened to hold strings, not just
ints), which `process_message()` consults once at the top of each call.

**Tech Stack:** Python 3.12, `litellm` (new dependency), `pydantic-settings`, `pytest` +
`pytest-asyncio` + `pytest-mock`.

**Spec:** `docs/superpowers/specs/2026-09-04-multi-provider-llm-design.md` (already
reviewed by an independent Fable advisor against the real code, with all findings
folded in, and approved by the user — read it for the full rationale behind every
decision below; this plan only decomposes it into tasks).

## Global Constraints

- Full tool-calling parity: all ~33 existing tools plus the new `manage_llm_provider`
  must work identically regardless of active provider.
- `reply_monitor.py` / `invoice_monitor.py`'s Claude Haiku classification calls are
  **out of scope** — do not touch them.
- Provider/model selection is a single global default via `runtime_config_store`, not
  per-chat.
- Switching stays natural-language through the existing tool-calling loop — no new
  Telegram slash-command.
- Model IDs (all verified against each provider's own docs on 2026-09-04 — use these
  exact strings, do not substitute guessed ones):
  - Anthropic: `anthropic/claude-sonnet-4-6` (today's unchanged default),
    `anthropic/claude-opus-4-6`, `anthropic/claude-haiku-4-5-20251001`
  - OpenAI: `openai/gpt-6-astra` (flagship), `openai/gpt-5.6-luna` (cost-efficient)
  - Gemini: `gemini/gemini-3.1-pro-preview`, `gemini/gemini-3.8-flash`
- Never pass `exclude_none=True` to a LiteLLM message's `.model_dump()` — it drops the
  `content: None` key on tool-call turns, which breaks the Anthropic backend on the
  *next* turn (LiteLLM's Anthropic translation requires `content` present, even as
  `null`). This was caught in spec review — do not reintroduce it.

---

## Task order and parallelism

Tasks 1, 2, and 3 touch disjoint files and have no dependencies on each other — dispatch
them in parallel. Task 4 depends on 1 and 2 (needs the new `Settings` fields and string
support in `runtime_config_store`) and touches the same files as Task 3
(`unified_agent.py` / `tests/test_unified_agent.py`), so it must run *after* both 1+2 and
3 are committed. Tasks 5 and 6 both touch `unified_agent.py` /
`tests/test_unified_agent.py` too, so they run strictly in sequence after 4: 4 → 5 → 6.
Task 7 (docs) depends on all code tasks being done, since it describes their combined
result.

```
{1, 2, 3} (parallel) → 4 → 5 → 6 → 7
```

---

### Task 1: Add `litellm` dependency and new `Settings` API-key fields

**Files:**
- Modify: `pyproject.toml:19` (dependencies list)
- Modify: `organist_bot/config.py:43` (Settings class)
- Test: `tests/test_unified_agent.py` (new, small — see below; no dedicated
  `test_config.py` exists in this repo, and no other optional string field has one, so
  this stays consistent with that convention rather than inventing a new test file)

**Interfaces:**
- Produces: `settings.openai_api_key: str` and `settings.gemini_api_key: str`, both
  defaulting to `""`, for Task 4/5 to read.

- [ ] **Step 1: Add the dependency**

In `pyproject.toml`, in the `[project] dependencies` list, add a line right after
`"anthropic>=0.40",`:

```toml
    "anthropic>=0.40",
    "litellm>=1.80",
```

- [ ] **Step 2: Install it**

Run: `uv sync --extra dev`
Expected: `litellm` and its transitive dependencies appear in the install output.

- [ ] **Step 3: Add the two new Settings fields**

In `organist_bot/config.py`, the existing block reads:

```python
    # ── Invoice agent ─────────────────────────────────────────────────────────
    anthropic_api_key: str = ""
```

Change it to:

```python
    # ── Invoice agent ─────────────────────────────────────────────────────────
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""
```

- [ ] **Step 4: Write a test confirming the fields exist with the right defaults**

Add this to `tests/test_unified_agent.py` (top-level, not inside a class — place it
right after the existing top-level `test_agent_response_buttons_defaults_to_none`
function so config-adjacent smoke tests live together):

```python
def test_settings_has_openai_and_gemini_api_key_fields():
    from organist_bot.config import Settings

    s = Settings(email_sender="a@b.com", email_password="x", cc_email="a@b.com")
    assert s.openai_api_key == ""
    assert s.gemini_api_key == ""
```

- [ ] **Step 5: Run it**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com .venv/bin/pytest tests/test_unified_agent.py -k test_settings_has_openai_and_gemini_api_key_fields -v`
Expected: PASS

- [ ] **Step 6: Run the full suite to confirm nothing else broke**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com .venv/bin/pytest --tb=short -q`
Expected: all tests pass (same count as baseline plus 1).

- [ ] **Step 7: Lint/format/type-check**

Run: `.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy organist_bot/ main.py`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml uv.lock organist_bot/config.py tests/test_unified_agent.py
git commit -m "feat: add litellm dependency and openai/gemini API key settings"
```

---

### Task 2: Widen `RuntimeConfigStore` to hold `int | str` values

**Files:**
- Modify: `organist_bot/runtime_config_store.py` (entire file — it's 47 lines, shown in
  full below)
- Test: `tests/test_runtime_config_store.py`

**Interfaces:**
- Produces: `runtime_config.get(key: str, default: int) -> int` (unchanged behavior),
  `runtime_config.get(key: str, default: str) -> str` (new overload),
  `runtime_config.set(key: str, value: int | str) -> None` (widened, no overload
  needed — an `int` argument already satisfies `int | str`).

The current file:

```python
from __future__ import annotations

import logging
from pathlib import Path

from organist_bot import atomic_store

logger = logging.getLogger(__name__)

_PATH = Path("data/runtime_config.json")


def _read() -> dict[str, int]:
    return dict(atomic_store.read_json(_PATH, {}))


class RuntimeConfigStore:
    """File-backed store for runtime pipeline config overrides."""

    def get(self, key: str, default: int) -> int:
        """Return the stored override for key, or default if not set."""
        return _read().get(key, default)

    def set(self, key: str, value: int) -> None:
        """Write an override value for key."""
        with atomic_store.file_lock(_PATH):
            data = dict(atomic_store.read_json(_PATH, {}))
            data[key] = value
            atomic_store.write_json(_PATH, data, lock=False)

    def reset(self, key: str) -> bool:
        """Remove the override for key. Returns True if the key existed."""
        with atomic_store.file_lock(_PATH):
            data = dict(atomic_store.read_json(_PATH, {}))
            if key not in data:
                return False
            del data[key]
            atomic_store.write_json(_PATH, data, lock=False)
        return True

    def all(self) -> dict[str, int]:
        """Return all current overrides."""
        return _read()


runtime_config = RuntimeConfigStore()
```

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_runtime_config_store.py`, inside `TestRuntimeConfigStore` (after the
existing `test_all_returns_current_overrides` method):

```python
    def test_get_returns_string_override_when_set(self, tmp_path, monkeypatch):
        from organist_bot.runtime_config_store import RuntimeConfigStore

        monkeypatch.chdir(tmp_path)
        store = RuntimeConfigStore()
        store.set("llm_provider", "openai")
        assert store.get("llm_provider", "anthropic") == "openai"

    def test_string_and_int_values_coexist(self, tmp_path, monkeypatch):
        from organist_bot.runtime_config_store import RuntimeConfigStore

        monkeypatch.chdir(tmp_path)
        store = RuntimeConfigStore()
        store.set("min_fee", 150)
        store.set("llm_provider", "gemini")
        assert store.all() == {"min_fee": 150, "llm_provider": "gemini"}
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_runtime_config_store.py -k "string" -v`
Expected: both FAIL — `set()`/`get()` currently work fine at runtime for strings (no
type enforcement in plain Python), so these may actually *pass* already at runtime
despite being unannotated for `str`. That's fine — the point of this task is the type
annotations for mypy, not new runtime behavior. Confirm this by running mypy on the
current file first: `.venv/bin/mypy organist_bot/runtime_config_store.py` should show
no errors yet (nothing calls `set`/`get` with a `str` value yet, so nothing to catch).

- [ ] **Step 3: Widen the store's type signatures**

Replace the full contents of `organist_bot/runtime_config_store.py` with:

```python
from __future__ import annotations

import logging
from pathlib import Path
from typing import overload

from organist_bot import atomic_store

logger = logging.getLogger(__name__)

_PATH = Path("data/runtime_config.json")

RuntimeValue = int | str


def _read() -> dict[str, RuntimeValue]:
    return dict(atomic_store.read_json(_PATH, {}))


class RuntimeConfigStore:
    """File-backed store for runtime pipeline config overrides."""

    @overload
    def get(self, key: str, default: int) -> int: ...
    @overload
    def get(self, key: str, default: str) -> str: ...

    def get(self, key: str, default: RuntimeValue) -> RuntimeValue:
        """Return the stored override for key, or default if not set."""
        return _read().get(key, default)

    def set(self, key: str, value: RuntimeValue) -> None:
        """Write an override value for key."""
        with atomic_store.file_lock(_PATH):
            data = dict(atomic_store.read_json(_PATH, {}))
            data[key] = value
            atomic_store.write_json(_PATH, data, lock=False)

    def reset(self, key: str) -> bool:
        """Remove the override for key. Returns True if the key existed."""
        with atomic_store.file_lock(_PATH):
            data = dict(atomic_store.read_json(_PATH, {}))
            if key not in data:
                return False
            del data[key]
            atomic_store.write_json(_PATH, data, lock=False)
        return True

    def all(self) -> dict[str, RuntimeValue]:
        """Return all current overrides."""
        return _read()


runtime_config = RuntimeConfigStore()
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `.venv/bin/pytest tests/test_runtime_config_store.py -v`
Expected: all PASS, including the two new ones.

- [ ] **Step 5: Type-check — this is the step that actually proves the widening is safe**

Run: `.venv/bin/mypy organist_bot/ main.py`
Expected: no new errors. In particular, `main.py`'s
`runtime_config.get("poll_minutes", settings.poll_minutes)` (used arithmetically
elsewhere in that file) must still resolve to `int`, not `int | str` — the `@overload`
pair is what guarantees this. If mypy reports an `int | str` inferred anywhere an `int`
is expected, the overloads are missing or wrong — re-check Step 3's exact code against
what was written.

- [ ] **Step 6: Run the full test suite**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com .venv/bin/pytest --tb=short -q`
Expected: all pass.

- [ ] **Step 7: Lint/format**

Run: `.venv/bin/ruff check . && .venv/bin/ruff format --check .`
Expected: pass.

- [ ] **Step 8: Commit**

```bash
git add organist_bot/runtime_config_store.py tests/test_runtime_config_store.py
git commit -m "feat: widen RuntimeConfigStore to hold int|str values"
```

---

### Task 3: Convert `TOOLS` to OpenAI function-calling shape

**Files:**
- Modify: `organist_bot/integrations/unified_agent.py:117` (the `TOOLS` list — renamed,
  not hand-edited entry-by-entry) and `:763` area (add the wrapper function right
  before `_TOOL_HANDLERS`)
- Test: `tests/test_unified_agent.py:1032-1034` (fix the existing broken test) and a new
  structural test

**Interfaces:**
- Produces: `TOOLS: list[dict]` where every entry is
  `{"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}`
  — for Task 4 (new tool entry) and Task 5 (`process_message()`'s `tools=TOOLS` call) to
  consume. `_TOOL_HANDLERS`, `_execute_tool`, `_VERBATIM_RESPONSE_TOOLS`,
  `_PDF_RESPONSE_TOOLS` are untouched — they all key off tool *name* strings, never the
  schema shape.

**Why a wrapper function instead of hand-editing all ~33 entries:** the spec describes
this as "a mechanical, 1:1 rewrite... no schema content changes, only the wrapping."
Retyping ~33 large dict literals by hand risks a transcription error in exactly one of
them going unnoticed. Renaming the existing list and wrapping it with one small function
achieves the identical final shape with a five-line diff instead of a 560-line one.

- [ ] **Step 1: Write the failing test for the new shape**

Add to `tests/test_unified_agent.py`, right after the existing
`test_agent_response_buttons_defaults_to_none` function:

```python
def test_every_tool_uses_openai_function_calling_shape():
    from organist_bot.integrations.unified_agent import TOOLS

    assert len(TOOLS) > 0
    for tool in TOOLS:
        assert tool["type"] == "function"
        fn = tool["function"]
        assert isinstance(fn["name"], str) and fn["name"]
        assert isinstance(fn["description"], str) and fn["description"]
        assert isinstance(fn["parameters"], dict)
        assert "input_schema" not in tool
        assert "name" not in tool  # top-level — only under "function"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_unified_agent.py -k test_every_tool_uses_openai_function_calling_shape -v`
Expected: FAIL — `TOOLS[0]` currently has `"name"`/`"input_schema"` at the top level,
not `"type"`/`"function"`.

- [ ] **Step 3: Rename the existing list and add the wrapper**

In `organist_bot/integrations/unified_agent.py`, find the line:

```python
TOOLS: list[dict] = [
```

Change **only this one line** to:

```python
_TOOLS_SCHEMA: list[dict] = [
```

Leave every entry inside the list (all ~33 of them, down to the closing `]` at what is
currently line 677) completely untouched — same `"name"`/`"description"`/`"input_schema"`
keys, same content, same comments, same order.

Then, immediately after the closing `]` of that list (i.e. right before the blank lines
leading into `_TOOL_HANDLERS: dict[...] = {}`), add:

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

- [ ] **Step 4: Run the new test to verify it passes**

Run: `.venv/bin/pytest tests/test_unified_agent.py -k test_every_tool_uses_openai_function_calling_shape -v`
Expected: PASS.

- [ ] **Step 5: Fix the now-broken existing test**

`tests/test_unified_agent.py:1032-1034` currently reads:

```python
    def test_seen_not_in_manage_filter_suspensions_enum(self):
        tool_def = next(t for t in TOOLS if t["name"] == "manage_filter_suspensions")
        assert "seen" not in tool_def["input_schema"]["properties"]["filter"]["enum"]
```

It breaks two ways under the new shape: `t["name"]` no longer exists at the top level
(it's under `t["function"]["name"]`), and `input_schema` is now
`function`→`parameters`. Replace those two lines with:

```python
    def test_seen_not_in_manage_filter_suspensions_enum(self):
        tool_def = next(t for t in TOOLS if t["function"]["name"] == "manage_filter_suspensions")
        assert "seen" not in tool_def["function"]["parameters"]["properties"]["filter"]["enum"]
```

- [ ] **Step 6: Run it to verify it passes**

Run: `.venv/bin/pytest tests/test_unified_agent.py -k test_seen_not_in_manage_filter_suspensions_enum -v`
Expected: PASS.

- [ ] **Step 7: Search for any other place reading `TOOLS` entries by the old shape**

Run: `grep -rn '"input_schema"' tests/ organist_bot/ main.py`
Expected: no hits (the one that existed was just fixed in Step 5). If this finds
anything else, fix it the same way (`input_schema` → `function`/`parameters`, and check
whether it also reads `t["name"]` directly and needs the same `t["function"]["name"]`
fix).

- [ ] **Step 8: Run the full test suite**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com .venv/bin/pytest --tb=short -q`
Expected: all pass. (`process_message()` itself still calls the old Anthropic API at
this point — Task 5 rewrites that — so this task alone doesn't change runtime behavior,
only the static `TOOLS` shape; no test exercises `tools=TOOLS` against a live/mocked
model call yet other than the ones Task 5 will rewrite.)

- [ ] **Step 9: Lint/format/type-check**

Run: `.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy organist_bot/ main.py`
Expected: pass. Note `process_message()`'s existing `tools=TOOLS,  # type: ignore[arg-type]`
comment (line 2108) — leave that `# type: ignore` in place for now; Task 5 rewrites that
whole call site anyway.

- [ ] **Step 10: Commit**

```bash
git add organist_bot/integrations/unified_agent.py tests/test_unified_agent.py
git commit -m "refactor: convert TOOLS to OpenAI function-calling shape"
```

---

### Task 4: `_PROVIDER_MODELS` registry and the `manage_llm_provider` tool

**Depends on:** Task 1 (`settings.openai_api_key`/`settings.gemini_api_key`), Task 2
(`runtime_config` accepts `str`), Task 3 (`_TOOLS_SCHEMA`/`_to_function_tool` exist —
this task adds one more entry to `_TOOLS_SCHEMA`, in the same Anthropic-shaped form as
every other entry there, so it goes through the same wrapping automatically).

**Files:**
- Modify: `organist_bot/integrations/unified_agent.py` — add the registry near the top
  (after imports, before `SYSTEM_PROMPT`), add one `_TOOLS_SCHEMA` entry, add the
  handler near `manage_config`'s handler, add a system-prompt section
- Test: `tests/test_unified_agent.py` (new `TestManageLlmProvider` class)

**Interfaces:**
- Consumes: `settings.openai_api_key`, `settings.gemini_api_key`, `settings.anthropic_api_key`
  (Task 1); `runtime_config.get`/`.set`/`.reset` accepting `str` (Task 2); `_handler`
  decorator, `_TOOL_HANDLERS`, `_VERBATIM_RESPONSE_TOOLS` (pre-existing, unchanged).
- Produces: `_PROVIDER_MODELS: dict[str, dict[str, str]]`, `_DEFAULT_PROVIDER: str`,
  `_DEFAULT_MODEL_KEY: str`, `_PROVIDER_API_KEY_FIELD: dict[str, str]` — for Task 5's
  `process_message()` rewrite to import/use directly (same module, no import needed).

- [ ] **Step 1: Add the registry constants**

In `organist_bot/integrations/unified_agent.py`, find the line `SYSTEM_PROMPT = """\`
(currently line 46) and insert the following block immediately **before** it:

```python
_PROVIDER_MODELS: dict[str, dict[str, str]] = {
    "anthropic": {
        "sonnet": "anthropic/claude-sonnet-4-6",
        "opus": "anthropic/claude-opus-4-6",
        "haiku": "anthropic/claude-haiku-4-5-20251001",
    },
    "openai": {
        "gpt-6-astra": "openai/gpt-6-astra",
        "gpt-5.6-luna": "openai/gpt-5.6-luna",
    },
    "gemini": {
        "gemini-pro": "gemini/gemini-3.1-pro-preview",
        "gemini-3.8-flash": "gemini/gemini-3.8-flash",
    },
}
_DEFAULT_PROVIDER = "anthropic"
_DEFAULT_MODEL_KEY = "sonnet"
_PROVIDER_API_KEY_FIELD = {
    "anthropic": "anthropic_api_key",
    "openai": "openai_api_key",
    "gemini": "gemini_api_key",
}


def _default_model_string() -> str:
    return _PROVIDER_MODELS[_DEFAULT_PROVIDER][_DEFAULT_MODEL_KEY]


```

- [ ] **Step 2: Write the failing tests for the tool**

Add to `tests/test_unified_agent.py`, right after `TestNegConfirmButtons` (i.e. right
before `class TestNegDeterministicActions:` — keep it near the other small
self-contained tool-handler test classes):

```python
class TestManageLlmProvider:
    def teardown_method(self):
        from organist_bot.runtime_config_store import runtime_config

        runtime_config.reset("llm_provider")
        runtime_config.reset("llm_model")

    async def test_get_returns_default_before_any_switch(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = await _execute_tool("manage_llm_provider", {"action": "get"}, CHAT_ID)
        data = json.loads(result)
        assert "anthropic" in data["result"]
        assert "claude-sonnet-4-6" in data["result"]

    async def test_set_missing_provider_returns_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = await _execute_tool("manage_llm_provider", {"action": "set"}, CHAT_ID)
        data = json.loads(result)
        assert "provider is required" in data["result"].lower()

    async def test_set_unknown_provider_returns_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = await _execute_tool(
            "manage_llm_provider", {"action": "set", "provider": "cohere"}, CHAT_ID
        )
        data = json.loads(result)
        assert "unknown provider" in data["result"].lower()

    async def test_set_without_configured_key_refuses(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(unified_agent.settings, "openai_api_key", "")
        result = await _execute_tool(
            "manage_llm_provider", {"action": "set", "provider": "openai"}, CHAT_ID
        )
        data = json.loads(result)
        assert "openai_api_key" in data["result"]
        from organist_bot.runtime_config_store import runtime_config

        assert runtime_config.get("llm_provider", "anthropic") == "anthropic"

    async def test_set_without_model_lists_options_and_does_not_switch(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(unified_agent.settings, "openai_api_key", "sk-test")
        result = await _execute_tool(
            "manage_llm_provider", {"action": "set", "provider": "openai"}, CHAT_ID
        )
        data = json.loads(result)
        assert "gpt-6-astra" in data["result"]
        assert "gpt-5.6-luna" in data["result"]
        from organist_bot.runtime_config_store import runtime_config

        assert runtime_config.get("llm_provider", "anthropic") == "anthropic"

    async def test_set_unknown_model_returns_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(unified_agent.settings, "openai_api_key", "sk-test")
        result = await _execute_tool(
            "manage_llm_provider",
            {"action": "set", "provider": "openai", "model": "gpt-9-fictional"},
            CHAT_ID,
        )
        data = json.loads(result)
        assert "unknown model" in data["result"].lower()

    async def test_set_with_valid_provider_and_model_switches(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(unified_agent.settings, "openai_api_key", "sk-test")
        result = await _execute_tool(
            "manage_llm_provider",
            {"action": "set", "provider": "openai", "model": "gpt-5.6-luna"},
            CHAT_ID,
        )
        data = json.loads(result)
        assert "switched" in data["result"].lower()
        from organist_bot.runtime_config_store import runtime_config

        assert runtime_config.get("llm_provider", "") == "openai"
        assert runtime_config.get("llm_model", "") == "openai/gpt-5.6-luna"

    async def test_get_reflects_switch(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(unified_agent.settings, "openai_api_key", "sk-test")
        await _execute_tool(
            "manage_llm_provider",
            {"action": "set", "provider": "openai", "model": "gpt-5.6-luna"},
            CHAT_ID,
        )
        result = await _execute_tool("manage_llm_provider", {"action": "get"}, CHAT_ID)
        data = json.loads(result)
        assert "openai" in data["result"]
        assert "gpt-5.6-luna" in data["result"] or "openai/gpt-5.6-luna" in data["result"]

    async def test_reset_restores_default(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(unified_agent.settings, "openai_api_key", "sk-test")
        await _execute_tool(
            "manage_llm_provider",
            {"action": "set", "provider": "openai", "model": "gpt-5.6-luna"},
            CHAT_ID,
        )
        result = await _execute_tool("manage_llm_provider", {"action": "reset"}, CHAT_ID)
        data = json.loads(result)
        assert "reset" in data["result"].lower() or "default" in data["result"].lower()
        from organist_bot.runtime_config_store import runtime_config

        assert runtime_config.get("llm_provider", "anthropic") == "anthropic"
```

This test class needs `unified_agent` imported at module scope in the test file — it
already is (`from organist_bot.integrations import unified_agent` near the
`TestNegActiveDraftState` section, confirmed present in the file already).

- [ ] **Step 3: Run them to verify they fail**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com .venv/bin/pytest tests/test_unified_agent.py -k TestManageLlmProvider -v`
Expected: all FAIL with `json.dumps({"error": f"Tool not implemented: manage_llm_provider"})`
being parsed instead of the expected result shape (or a `KeyError`/`AssertionError`) —
the tool doesn't exist yet.

- [ ] **Step 4: Add the tool schema**

In `organist_bot/integrations/unified_agent.py`, find (inside `_TOOLS_SCHEMA`, formerly
`TOOLS`) the `manage_config` entry's closing:

```python
            "required": ["action"],
        },
    },
    # ── Application tracking ────────────────────────────────────────────────
```

Insert a new entry between those two lines (i.e. right after `manage_config`'s closing
`},`, right before the `# ── Application tracking ──` comment):

```python
    # ── LLM provider ─────────────────────────────────────────────────────────
    {
        "name": "manage_llm_provider",
        "description": (
            "Read or switch which LLM provider/model powers this conversation. "
            "Providers: anthropic (sonnet/opus/haiku), openai (gpt-6-astra/gpt-5.6-luna), "
            "gemini (gemini-pro/gemini-3.8-flash). "
            "Use action='get' to show the current provider/model. "
            "Use action='set' with 'provider' to switch — if 'model' is omitted, list "
            "that provider's options and ask the user to pick one before calling set "
            "again. Use action='reset' to restore the default (anthropic/sonnet). "
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
    },
```

- [ ] **Step 5: Add `manage_llm_provider` to `_VERBATIM_RESPONSE_TOOLS`**

Find:

```python
_VERBATIM_RESPONSE_TOOLS = {
    "list_upcoming_gigs",
    "manage_config",
```

Change to:

```python
_VERBATIM_RESPONSE_TOOLS = {
    "list_upcoming_gigs",
    "manage_config",
    "manage_llm_provider",
```

- [ ] **Step 6: Add the handler**

In `organist_bot/integrations/unified_agent.py`, immediately after `_handle_manage_config`'s
closing (i.e. right after the line `return json.dumps({"error": f"Unknown action: {action}"})`
that ends that function, right before `async def process_message(`), add:

```python
@_handler("manage_llm_provider")
async def _handle_manage_llm_provider(input_data: dict, chat_id: int) -> str:
    action = input_data.get("action", "")

    if action == "get":
        provider = runtime_config.get("llm_provider", _DEFAULT_PROVIDER)
        model = runtime_config.get("llm_model", _default_model_string())
        return json.dumps({"result": f"Current provider: {provider}\nCurrent model: {model}"})

    if action == "set":
        provider = input_data.get("provider", "")
        if not provider:
            return json.dumps({"result": "provider is required for action='set'."})
        if provider not in _PROVIDER_MODELS:
            valid = ", ".join(_PROVIDER_MODELS)
            return json.dumps({"result": f"Unknown provider '{provider}'. Valid: {valid}."})

        api_key_field = _PROVIDER_API_KEY_FIELD[provider]
        if not getattr(settings, api_key_field):
            return json.dumps(
                {"result": f"Can't switch to {provider} — {api_key_field.upper()} isn't set."}
            )

        model_key = input_data.get("model", "")
        provider_models = _PROVIDER_MODELS[provider]
        if not model_key:
            options = ", ".join(provider_models)
            return json.dumps({"result": f"Which {provider} model? Options: {options}."})
        if model_key not in provider_models:
            valid = ", ".join(provider_models)
            return json.dumps(
                {"result": f"Unknown model '{model_key}' for {provider}. Valid: {valid}."}
            )

        runtime_config.set("llm_provider", provider)
        runtime_config.set("llm_model", provider_models[model_key])
        return json.dumps(
            {
                "result": (
                    f"Switched to {provider}/{model_key}. Takes effect on your next message."
                )
            }
        )

    if action == "reset":
        runtime_config.reset("llm_provider")
        runtime_config.reset("llm_model")
        return json.dumps({"result": "Reset to default (anthropic/sonnet)."})

    return json.dumps({"error": f"Unknown action: {action}"})


```

- [ ] **Step 7: Add the system-prompt section**

In `organist_bot/integrations/unified_agent.py`, find:

```
## Runtime config
- "What's the current config?" / "show config" → manage_config(action=get).
- "Set min fee to 150" → manage_config(action=set, key=min_fee, value=150).
- "Reset min fee to default" → manage_config(action=reset, key=min_fee).
- Editable keys: min_fee, max_travel_minutes, poll_minutes, negotiable_fee.

## Application tracking
```

Insert a new section between them:

```
## Runtime config
- "What's the current config?" / "show config" → manage_config(action=get).
- "Set min fee to 150" → manage_config(action=set, key=min_fee, value=150).
- "Reset min fee to default" → manage_config(action=reset, key=min_fee).
- Editable keys: min_fee, max_travel_minutes, poll_minutes, negotiable_fee.

## LLM provider
- "Switch to GPT-6 Astra" / "use Gemini" / "what model are we using?" → manage_llm_provider.
- If you say a provider without a model, I'll list that provider's options and ask which one.

## Application tracking
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com .venv/bin/pytest tests/test_unified_agent.py -k TestManageLlmProvider -v`
Expected: all PASS.

- [ ] **Step 9: Run the full test suite**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com .venv/bin/pytest --tb=short -q`
Expected: all pass.

- [ ] **Step 10: Lint/format/type-check**

Run: `.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy organist_bot/ main.py`
Expected: pass.

- [ ] **Step 11: Commit**

```bash
git add organist_bot/integrations/unified_agent.py tests/test_unified_agent.py
git commit -m "feat: add manage_llm_provider tool and provider/model registry"
```

---

### Task 5: Rewrite `process_message()` to use `litellm.acompletion`

**Depends on:** Task 3 (`TOOLS` in OpenAI shape), Task 4 (`_PROVIDER_MODELS`,
`_DEFAULT_PROVIDER`, `_DEFAULT_MODEL_KEY`, `_PROVIDER_API_KEY_FIELD`,
`_default_model_string()`).

**Files:**
- Modify: `organist_bot/integrations/unified_agent.py:2084-2178` (the whole
  `process_message` function body, reproduced in full above in this plan's context —
  see Task 4's file for exact current content, or re-read the file directly)
- Test: `tests/test_unified_agent.py` — rewrite the 4 existing `process_message()`-level
  tests (`test_process_message_reports_on_step_progress`,
  `test_process_message_without_on_step_is_unaffected`,
  `test_process_message_passes_through_tool_buttons`,
  `test_process_message_stashes_instruction_on_needs_pick`), plus one new test for the
  history round-trip bug caught in spec review.

**Interfaces:**
- Consumes: `_PROVIDER_MODELS`, `_DEFAULT_PROVIDER`, `_DEFAULT_MODEL_KEY`,
  `_PROVIDER_API_KEY_FIELD`, `_default_model_string()` (Task 4); `TOOLS` (Task 3);
  `runtime_config.get` (Task 2, now accepting `str` defaults).
- Produces: `_histories[chat_id]` entries in the new flat OpenAI-style shape (`role`:
  `system`/`user`/`assistant`/`tool`) — for Task 6's `_trim_history` test rewrite to
  build fixtures against.

- [ ] **Step 1: Replace `process_message()`'s body**

Replace the entire function (currently `organist_bot/integrations/unified_agent.py:2084-2178`,
from `async def process_message(` through the `return responses` line and the blank
line after it) with:

```python
async def process_message(
    chat_id: int,
    text: str,
    on_step: Callable[[str], Awaitable[None]] | None = None,
) -> list[AgentResponse]:
    import litellm

    provider = runtime_config.get("llm_provider", _DEFAULT_PROVIDER)
    if provider not in _PROVIDER_MODELS:
        logger.warning("Unknown stored llm_provider %r, resetting to default", provider)
        runtime_config.reset("llm_provider")
        runtime_config.reset("llm_model")
        provider = _DEFAULT_PROVIDER
    model = runtime_config.get("llm_model", _default_model_string())
    api_key = getattr(settings, _PROVIDER_API_KEY_FIELD[provider])

    _hydrate_chat(chat_id)

    if chat_id not in _histories:
        _histories[chat_id] = []

    _histories[chat_id].append({"role": "user", "content": text})

    responses: list[AgentResponse] = []
    steps: list[str] = []

    while True:
        response = await litellm.acompletion(
            model=model,
            max_tokens=4096,
            api_key=api_key,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                *_histories[chat_id],
            ],
            tools=TOOLS,
        )

        msg = response.choices[0].message
        # NOT exclude_none=True — see Global Constraints. A tool-call turn has
        # content: None alongside tool_calls; dropping that key entirely (rather
        # than keeping it as an explicit null) breaks the Anthropic backend on the
        # NEXT turn, since LiteLLM's Anthropic translation requires `content` to be
        # present on every message.
        _histories[chat_id].append(msg.model_dump())

        if not msg.tool_calls:
            if msg.content:
                responses.append(AgentResponse(text=msg.content))
            break

        tool_results = []
        for tc in msg.tool_calls:
            name = tc.function.name
            args = json.loads(tc.function.arguments)
            logger.info("Unified agent tool call: %s(%s)", name, json.dumps(args))

            steps.append(f"🔧 {name}")
            if on_step is not None:
                await on_step("\n".join(steps))

            try:
                result = await _execute_tool(name, args, chat_id)
            except Exception as e:
                logger.error("Tool execution failed: %s", e)
                result = json.dumps({"error": str(e)})

            steps[-1] = f"✅ {name}"
            if on_step is not None:
                await on_step("\n".join(steps))

            if name in _VERBATIM_RESPONSE_TOOLS:
                try:
                    data = json.loads(result)
                    if "result" in data:
                        responses.append(
                            AgentResponse(text=data["result"], buttons=data.get("buttons"))
                        )
                        if data.get("needs_pick"):
                            stash_pending_neg_instruction(chat_id, text)
                        result = json.dumps({"result": "Listing sent to user."})
                except (json.JSONDecodeError, KeyError):
                    pass

            tool_results.append(
                {"role": "tool", "tool_call_id": tc.id, "name": name, "content": result}
            )

            if name in _PDF_RESPONSE_TOOLS and chat_id in _last_invoice:
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

        _histories[chat_id].extend(tool_results)

    _trim_history(chat_id)
    _persist_chat(chat_id)
    return responses

```

Note what's deliberately unchanged: `_execute_tool`, the `_VERBATIM_RESPONSE_TOOLS`/
`_PDF_RESPONSE_TOOLS` special-casing, `stash_pending_neg_instruction`, `on_step`
progress reporting, `_trim_history`/`_persist_chat` at the end — none of these
reference message/provider shape, so none of them change.

- [ ] **Step 2: Add the shared test helpers**

In `tests/test_unified_agent.py`, right before the
`# ── process_message on_step progress reporting ──` section header (i.e. right before
`test_process_message_reports_on_step_progress`), add:

```python
def _fake_tool_call(call_id: str, name: str, arguments: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def _fake_litellm_response(
    content: str | None = None, tool_calls: list | None = None
) -> SimpleNamespace:
    dumped = {
        "role": "assistant",
        "content": content,
        "tool_calls": (
            [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in tool_calls
            ]
            if tool_calls
            else None
        ),
    }
    # Intentionally takes NO kwargs: if process_message() ever calls
    # msg.model_dump(exclude_none=True), this raises TypeError instead of silently
    # dropping the `content: None` key — pinning the history round-trip bug fixed
    # during spec review (see Global Constraints in the plan/spec).
    message = SimpleNamespace(content=content, tool_calls=tool_calls, model_dump=lambda: dumped)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


```

This needs `from types import SimpleNamespace` at module scope. The file currently
starts:

```python
"""Tests for unified_agent._execute_tool and supporting utilities."""

import datetime
import json
from unittest.mock import AsyncMock, MagicMock, patch
```

Change it to:

```python
"""Tests for unified_agent._execute_tool and supporting utilities."""

import datetime
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
```

Each of the four tests rewritten in Steps 3-6 below currently has a local
`from types import SimpleNamespace` inside the test function — the rewritten versions in
those steps don't call `SimpleNamespace` directly at all anymore (only
`_fake_litellm_response`/`_fake_tool_call` do, and they get it from this new top-level
import), so that local import line is simply dropped when you replace each function
body per Steps 3-6.

- [ ] **Step 3: Rewrite `test_process_message_reports_on_step_progress`**

Replace the entire function body with:

```python
@pytest.mark.asyncio
async def test_process_message_reports_on_step_progress(tmp_path, monkeypatch):
    """process_message must report a 🔧 step when a tool call starts and flip
    it to ✅ once the tool call returns, via the on_step callback."""
    import litellm

    from organist_bot.integrations import agent_state, unified_agent

    monkeypatch.setattr(agent_state, "_PATH", tmp_path / "agent_state.json")
    cid = 314159
    unified_agent._hydrated.discard(cid)

    tool_use_response = _fake_litellm_response(
        tool_calls=[
            _fake_tool_call("tool_1", "add_gig", {"url": "https://example.com/gig/1"})
        ]
    )
    end_turn_response = _fake_litellm_response(content="Added the gig.")

    monkeypatch.setattr(
        litellm, "acompletion", AsyncMock(side_effect=[tool_use_response, end_turn_response])
    )
    monkeypatch.setattr(
        unified_agent, "_execute_tool", AsyncMock(return_value=json.dumps({"result": "ok"}))
    )

    steps: list[str] = []

    async def on_step(status_text: str) -> None:
        steps.append(status_text)

    try:
        responses = await unified_agent.process_message(cid, "add this gig", on_step=on_step)
        # Confirms the history round-trip: the assistant's tool-call turn keeps an
        # explicit content: None (not a dropped key), and the tool result landed as
        # a flat role="tool" message — both required for the Anthropic backend to
        # accept the next turn.
        history = unified_agent._histories[cid]
        assistant_turn = next(m for m in history if m["role"] == "assistant")
        assert assistant_turn["content"] is None
        assert "tool_calls" in assistant_turn
        tool_turn = next(m for m in history if m["role"] == "tool")
        assert tool_turn["tool_call_id"] == "tool_1"
    finally:
        unified_agent._histories.pop(cid, None)
        unified_agent._hydrated.discard(cid)

    assert steps == ["🔧 add_gig", "✅ add_gig"]
    assert responses == [unified_agent.AgentResponse(text="Added the gig.")]
```

- [ ] **Step 4: Rewrite `test_process_message_without_on_step_is_unaffected`**

Replace the entire function body with:

```python
@pytest.mark.asyncio
async def test_process_message_without_on_step_is_unaffected(tmp_path, monkeypatch):
    """Omitting on_step (the default) must not change existing behavior."""
    import litellm

    from organist_bot.integrations import agent_state, unified_agent

    monkeypatch.setattr(agent_state, "_PATH", tmp_path / "agent_state.json")
    cid = 271828
    unified_agent._hydrated.discard(cid)

    end_turn_response = _fake_litellm_response(content="All set.")
    monkeypatch.setattr(litellm, "acompletion", AsyncMock(return_value=end_turn_response))

    try:
        responses = await unified_agent.process_message(cid, "hello")
    finally:
        unified_agent._histories.pop(cid, None)
        unified_agent._hydrated.discard(cid)

    assert responses == [unified_agent.AgentResponse(text="All set.")]
```

- [ ] **Step 5: Rewrite `test_process_message_passes_through_tool_buttons`**

Replace the entire function body with:

```python
@pytest.mark.asyncio
async def test_process_message_passes_through_tool_buttons(tmp_path, monkeypatch):
    import litellm

    from organist_bot.integrations import agent_state, unified_agent

    monkeypatch.setattr(agent_state, "_PATH", tmp_path / "agent_state.json")
    cid = 424242
    unified_agent._hydrated.discard(cid)

    tool_use_response = _fake_litellm_response(
        tool_calls=[_fake_tool_call("t1", "approve_neg_application", {"gig_id": "abc123"})]
    )
    end_turn_response = _fake_litellm_response(content="ok")

    monkeypatch.setattr(
        litellm, "acompletion", AsyncMock(side_effect=[tool_use_response, end_turn_response])
    )

    buttons = [[{"text": "Confirm", "callback_data": "neg:confirm_send:abc123"}]]
    monkeypatch.setattr(
        unified_agent,
        "_execute_tool",
        AsyncMock(return_value=json.dumps({"result": "Will send.", "buttons": buttons})),
    )

    try:
        responses = await unified_agent.process_message(cid, "approve abc123")
    finally:
        unified_agent._histories.pop(cid, None)
        unified_agent._hydrated.discard(cid)

    assert responses[0].buttons == buttons
```

- [ ] **Step 6: Rewrite `test_process_message_stashes_instruction_on_needs_pick`**

Replace the entire function body with:

```python
@pytest.mark.asyncio
async def test_process_message_stashes_instruction_on_needs_pick(tmp_path, monkeypatch):
    import litellm

    from organist_bot.integrations import agent_state, unified_agent

    monkeypatch.setattr(agent_state, "_PATH", tmp_path / "agent_state.json")
    cid = 535353
    unified_agent._hydrated.discard(cid)
    unified_agent._pending_neg_instruction.pop(cid, None)

    tool_use_response = _fake_litellm_response(
        tool_calls=[_fake_tool_call("t1", "approve_neg_application", {})]
    )
    end_turn_response = _fake_litellm_response(content="ok")

    monkeypatch.setattr(
        litellm, "acompletion", AsyncMock(side_effect=[tool_use_response, end_turn_response])
    )

    picker_buttons = [[{"text": "A", "callback_data": "neg:pick:aaa"}]]
    monkeypatch.setattr(
        unified_agent,
        "_execute_tool",
        AsyncMock(
            return_value=json.dumps(
                {"result": "Which draft?", "buttons": picker_buttons, "needs_pick": True}
            )
        ),
    )

    try:
        responses = await unified_agent.process_message(cid, "approve it")
        assert unified_agent.pop_pending_neg_instruction(cid) == "approve it"
    finally:
        unified_agent._histories.pop(cid, None)
        unified_agent._hydrated.discard(cid)
        unified_agent._pending_neg_instruction.pop(cid, None)

    assert responses[0].buttons == picker_buttons
```

- [ ] **Step 7: Run all five (four rewritten + new helper-adjacent) tests**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com .venv/bin/pytest tests/test_unified_agent.py -k "process_message" -v`
Expected: all PASS. If `test_process_message_reports_on_step_progress` fails on the
`assert assistant_turn["content"] is None` line specifically, re-check Step 1's
`process_message()` body — this is the exact assertion that would fail if `exclude_none=True`
had been reintroduced.

- [ ] **Step 8: Run the full test suite**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com .venv/bin/pytest --tb=short -q`
Expected: all pass **except** `TestTrimHistory` — that class is expected to fail at
this point (Task 6 fixes it) because its fixtures still build the old nested Anthropic
shape while `_trim_history` itself never changed behavior, so those tests may still
incidentally pass or fail depending on data shape; don't worry about `TestTrimHistory`'s
outcome here specifically, only that nothing *else* regressed. Confirm by running
everything except that class:
`EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com .venv/bin/pytest --tb=short -q --deselect tests/test_unified_agent.py::TestTrimHistory`
Expected: all pass.

- [ ] **Step 9: Lint/format/type-check**

Run: `.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy organist_bot/ main.py`
Expected: pass. The old `tools=TOOLS,  # type: ignore[arg-type]` comment is gone now
(Step 1's rewrite doesn't include it) — if mypy complains about `tools=TOOLS` here,
add back a `# type: ignore[arg-type]` on that line, matching the original's reason
(LiteLLM's stub types for `tools` may not exactly match a plain `list[dict]`).

- [ ] **Step 10: Commit**

```bash
git add organist_bot/integrations/unified_agent.py tests/test_unified_agent.py
git commit -m "feat: rewrite process_message() to use litellm.acompletion"
```

---

### Task 6: Rewrite `TestTrimHistory` for the new flat message shape

**Depends on:** Task 5 (defines the flat `role: "tool"` shape `_trim_history` now
actually receives in production).

**Files:**
- Modify: `tests/test_unified_agent.py:2245-2302` (the `_turn_with_tool_call` helper and
  the whole `TestTrimHistory` class)

**Interfaces:**
- Consumes: `unified_agent._trim_history`, `unified_agent._histories`,
  `unified_agent._MAX_HISTORY_MESSAGES` (all pre-existing, unchanged — `_trim_history`'s
  own logic needs no code change per the spec, only its test fixtures/assertions do).

- [ ] **Step 1: Replace the `_turn_with_tool_call` helper**

Replace:

```python
def _turn_with_tool_call(n: int) -> list[dict]:
    """One user(str) turn followed by an assistant tool_use + user(list) tool_result pair."""
    return [
        {"role": "user", "content": f"do thing {n}"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": f"tool_{n}", "name": "noop", "input": {}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": f"tool_{n}", "content": "ok"}],
        },
    ]
```

with:

```python
def _turn_with_tool_call(n: int) -> list[dict]:
    """One user(str) turn followed by an assistant tool_calls turn and a flat
    role="tool" result message — the shape litellm.acompletion's response actually
    produces (see process_message() in unified_agent.py)."""
    return [
        {"role": "user", "content": f"do thing {n}"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"tool_{n}",
                    "type": "function",
                    "function": {"name": "noop", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": f"tool_{n}", "name": "noop", "content": "ok"},
    ]
```

- [ ] **Step 2: Replace the pairing-check assertion in `test_over_cap_trims_to_a_user_text_boundary`**

Within `class TestTrimHistory`, replace:

```python
        # No tool_use block should be left without its matching tool_result.
        pending_tool_use_ids: set[str] = set()
        for msg in trimmed:
            content = msg["content"]
            if not isinstance(content, list):
                continue
            for block in content:
                if block.get("type") == "tool_use":
                    pending_tool_use_ids.add(block["id"])
                elif block.get("type") == "tool_result":
                    pending_tool_use_ids.discard(block["tool_use_id"])
        assert pending_tool_use_ids == set()
```

with:

```python
        # No assistant tool_calls entry should be left without its matching
        # role="tool" result message.
        pending_tool_call_ids: set[str] = set()
        for msg in trimmed:
            if msg["role"] == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    pending_tool_call_ids.add(tc["id"])
            elif msg["role"] == "tool":
                pending_tool_call_ids.discard(msg["tool_call_id"])
        assert pending_tool_call_ids == set()
```

Leave the rest of that test function (the setup loop building 30 turns, and the
`len(trimmed) <= ...` / `trimmed[0]["role"] == "user"` /
`isinstance(trimmed[0]["content"], str)` assertions before this block) exactly as-is —
those already worked correctly against the new shape without modification (per the
spec's analysis: `_trim_history`'s cut logic never needed to change).

`test_under_cap_is_untouched` and `test_missing_chat_id_is_a_noop` need no changes at
all beyond what Step 1 already gives them (they call `_turn_with_tool_call` but don't
inspect its internal shape directly).

- [ ] **Step 3: Run `TestTrimHistory`**

Run: `.venv/bin/pytest tests/test_unified_agent.py -k TestTrimHistory -v`
Expected: all 3 tests PASS.

- [ ] **Step 4: Sanity-check the pairing-check actually catches a broken trim**

This is a one-off manual check, not a permanent test — do this by hand to build
confidence, then discard it (do not commit a deliberately-broken assertion):

Temporarily change `_trim_history`'s cut condition (e.g. change
`history[cut]["role"] == "user"` to `True`, forcing it to cut anywhere) and re-run
`test_over_cap_trims_to_a_user_text_boundary` — confirm it now FAILS (proving the
pairing-check assertion added in Step 2 genuinely detects an orphaned tool call, unlike
the old vacuous version the Fable review caught). Then revert the temporary change
(`git checkout -- organist_bot/integrations/unified_agent.py` if only that file was
touched, or manually undo the one-line edit) and re-run to confirm it passes again
before moving on.

- [ ] **Step 5: Run the full test suite**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com .venv/bin/pytest --tb=short -q`
Expected: all pass — this is the first point in the plan where the *entire* suite,
including `TestTrimHistory`, is expected to be fully green again.

- [ ] **Step 6: Lint/format/type-check**

Run: `.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy organist_bot/ main.py`
Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add tests/test_unified_agent.py
git commit -m "test: rewrite TestTrimHistory for the flat OpenAI-style message shape"
```

---

### Task 7: Update `CLAUDE.md` and `README.md`

**Depends on:** all prior tasks (describes their combined result).

**Files:**
- Modify: `CLAUDE.md` (tool count, tool list, config section)
- Modify: `README.md` (config table)

**Interfaces:** none — documentation only, no code.

- [ ] **Step 1: Update `CLAUDE.md`'s tool count and tool list**

Find:

```
A single python-telegram-bot polling bot, gated by `TELEGRAM_CHAT_ID`. **Every free-text message is forwarded to `unified_agent.process_message`** (`integrations/unified_agent.py`) — a multi-domain Claude Sonnet 4.6 agent with ~33 tools spanning:
- **Gig calendar** — `add_gig` (from URL or fields), `list_upcoming_gigs`, `manage_competing_gigs`
- **Invoicing** — `generate_invoice`, `email_invoice`, `list_clients`, `list_invoices`
- **Filter management** — `manage_blacklist`, `manage_unavailable`, `manage_available` (writes to `filter_store`), `manage_filter_suspensions` (writes to `filter_suspension_store`)
- **Runtime config** — `manage_config` (writes to `runtime_config_store`: `min_fee`, `max_travel_minutes`, `poll_minutes`)
- **Applications & income** — `manage_applications`, `get_income_forecast` (reads from `application_store`)
```

Replace with:

```
A single python-telegram-bot polling bot, gated by `TELEGRAM_CHAT_ID`. **Every free-text message is forwarded to `unified_agent.process_message`** (`integrations/unified_agent.py`) — a multi-domain agent with ~34 tools spanning:
- **Gig calendar** — `add_gig` (from URL or fields), `list_upcoming_gigs`, `manage_competing_gigs`
- **Invoicing** — `generate_invoice`, `email_invoice`, `list_clients`, `list_invoices`
- **Filter management** — `manage_blacklist`, `manage_unavailable`, `manage_available` (writes to `filter_store`), `manage_filter_suspensions` (writes to `filter_suspension_store`)
- **Runtime config** — `manage_config` (writes to `runtime_config_store`: `min_fee`, `max_travel_minutes`, `poll_minutes`)
- **LLM provider** — `manage_llm_provider` (switches between Claude/OpenAI/Gemini via `litellm.acompletion`, backed by `runtime_config_store`'s `llm_provider`/`llm_model` keys)
- **Applications & income** — `manage_applications`, `get_income_forecast` (reads from `application_store`)

The agent runs on whichever provider/model `runtime_config_store` currently holds (default: Anthropic Claude Sonnet, `anthropic/claude-sonnet-4-6`) — see "LLM providers" below.
```

- [ ] **Step 2: Update the `unified_agent.py` line in the integrations file list**

Find:

```
- `unified_agent.py` — Claude SDK agentic loop, ~33 tools, per-chat state
```

Replace with:

```
- `unified_agent.py` — litellm-backed agentic loop (Claude/OpenAI/Gemini), ~34 tools, per-chat state
```

- [ ] **Step 3: Update the Configuration section**

Find:

```
- **Anthropic** — `ANTHROPIC_API_KEY`
```

Replace with:

```
- **LLM providers** — `ANTHROPIC_API_KEY` (default provider), `OPENAI_API_KEY`, `GEMINI_API_KEY` (all optional; `manage_llm_provider` refuses to switch to a provider whose key isn't set)
```

- [ ] **Step 4: Update `README.md`'s config table**

Find:

```
| `ANTHROPIC_API_KEY` | API key for the invoice AI agent |
```

Replace with:

```
| `ANTHROPIC_API_KEY` | API key for the default LLM provider (Claude) |
| `OPENAI_API_KEY` | API key for OpenAI, if you switch the agent to it via `manage_llm_provider` |
| `GEMINI_API_KEY` | API key for Gemini, if you switch the agent to it via `manage_llm_provider` |
```

- [ ] **Step 5: Verify no other stale "~33 tools" / "Claude Sonnet 4.6 agent" mentions remain**

Run: `grep -rn "~33 tools\|Claude Sonnet 4.6 agent" CLAUDE.md README.md`
Expected: no hits (both were fixed in Steps 1-2). If any turn up elsewhere in these two
files, fix them the same way.

- [ ] **Step 6: Run the full test suite one final time**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com .venv/bin/pytest --tb=short -q`
Expected: all pass (docs-only changes, but confirms nothing from earlier tasks was left
in a broken state).

- [ ] **Step 7: Lint/format/type-check/security — the full local gate**

Run: `.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy organist_bot/ main.py && .venv/bin/bandit -r organist_bot/ -ll`
Expected: all pass. (Semgrep is run separately by `make pre-push`/`make ship` at ship
time — no need to run it standalone here.)

- [ ] **Step 8: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "docs: document multi-provider LLM support"
```

---

## After all tasks: ship

Not a task in the numbered sequence above — this repo has no persistent dev/staging
worktree structure (confirmed via `git worktree list` earlier this session), so
"ship" here means this repo's own normal workflow: from inside this worktree, run
`make ship` (runs the full `make pre-push` gate — ruff, mypy, bandit, semgrep, pytest —
then pushes the branch, opens a PR, and enables squash auto-merge). Before opening the
PR, this repo's own `.claude/hooks` machinery requires running the
`pipeline-impact-reviewer` agent against `git diff main...HEAD` and satisfying its
sentinel-file gate (encountered and handled twice already this session for other
branches) — do this before the first `gh pr create` attempt, not after it's blocked.
