# Sebby Pilot Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate `organist_bot`'s hand-rolled Anthropic prompt-cache breakpoint logic to `sebby.llm.cache`, and adopt the `sebby-toolkit` Claude Code plugin for `organist_bot`'s generic hooks — the first real-world validation of the shared toolkit built across four prior plans in the `sebby` repo.

**Architecture:** This is deliberately a narrow, surgical migration, not a wholesale replacement of `organist_bot`'s LLM layer. `organist_bot`'s `_call_llm_with_failover` loop has real, organist_bot-specific concerns (a Responses API adapter for one model, Telegram alerting, `runtime_config` persistence, its own usage-tracking store) that `sebby.llm.LLMClient` doesn't support and shouldn't be forced to absorb. What genuinely IS generic — and was the whole reason `sebby` exists — is the prompt-cache breakpoint-marking mechanics, which move to `sebby.llm.cache` with byte-identical output. Separately, the Claude Code hooks that are already generic (secret safety, quality gates, PR/docs workflow) move to the `sebby-toolkit` plugin; hooks that are genuinely `organist_bot`-specific (filter/tool-registration validation, the PR-reviewer sentinel) stay local.

**Tech Stack:** `sebby` (as a `uv` git dependency, pinned to `v0.1.0`, no extras needed — this migration only uses `sebby.llm.cache`, which has zero third-party dependencies), `pytest` (organist_bot's existing suite), the `sebby-toolkit` Claude Code plugin.

**Spec:** `docs/superpowers/specs/2026-09-16-shared-toolkit-design.md` in the `sebby` repo (`/Users/sebby/Developer/sebby`) — this plan implements that spec's "pilot-migrate organist_bot" rollout step.

**Note on scope:** `sebby` is now pushed to `github.com/seby-dev/sebby`, tagged `v0.1.0`. This plan does NOT touch: `SYSTEM_PROMPT`, the tool schema, any of the ~50 gig/invoice/client/filter tool handlers, `process_message`'s core loop structure, provider/model selection (`_PROVIDER_MODELS`, `_FAILOVER_ORDER`), the Responses API adapter for `gpt-6-astra`, Telegram alerting, `runtime_config` persistence, or `llm_usage_store`. All of those stay exactly as they are.

**Known pre-existing issues found during investigation, left alone (out of scope, flagged for the human's awareness):**
- `.claude/hooks/block_env_git.py` exists on disk but was NOT wired into `.claude/settings.json` before this migration (no `PreToolUse` entry referenced it) — adopting the plugin fixes this incidentally, since the plugin's `hooks.json` does register it.
- `.claude/settings.local.json` (separate from `settings.json`) contains its own, largely redundant `PostToolUse` hooks with hardcoded absolute paths pointing at `/Users/sebby/Documents/Dev/organist_bot` — a different, stale path from this repo's actual location. This file is untouched by this plan; investigate separately whether it's still in active use.

## Global Constraints

- `sebby` is added as a `uv` dependency: `sebby @ git+https://github.com/seby-dev/sebby@v0.1.0` (no extras — this migration only needs `sebby.llm.cache`, which has no third-party imports at module level).
- `_with_anthropic_cache_control`'s OBSERVABLE behavior must not change: cache breakpoints still land in exactly the same two places (system prompt content, last tool), verified by the existing `TestAnthropicCacheControl` test class continuing to pass unmodified.
- The generic hooks migrated to the plugin (`block_env_git.py`, `scrub_env_transcript.py`, `post_edit_lint.py`, `stop_quality_check.py`, `post_pr_created.py`, `post_merge_docs_update.py`) are removed from `.claude/hooks/` and their registrations removed from `.claude/settings.json`, replaced by plugin installation + environment-variable configuration. Organist_bot-specific hooks (`require_pr_reviewers.sh`, `validate_filter_registration.py`, `validate_invoice_tools.py`) stay exactly where they are.
- **This plan's hook-removal task creates a real gap until the plugin is actually installed** (`/plugin marketplace add` + `/plugin install` are manual, interactive steps this plan cannot run itself) — this must be called out prominently in the final commit/PR so the human does that step immediately.
- All of organist_bot's existing tests continue to pass; no test is deleted, only the two cache-related tests are read to confirm they still pass unmodified (their assertions target output shape, which doesn't change).

---

### Task 1: Add `sebby` as a dependency

**Files:**
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: nothing
- Produces: `sebby.llm.cache` importable from organist_bot's codebase

- [ ] **Step 1: Add the dependency**

Edit `pyproject.toml`'s `[project].dependencies` list to add:

```toml
    "sebby @ git+https://github.com/seby-dev/sebby@v0.1.0",
```

(Insert alongside the existing `anthropic`/`litellm` entries, keeping the list's existing style.)

- [ ] **Step 2: Sync the environment**

Run: `cd /Users/sebby/Developer/organist_bot && uv sync --extra dev`
Expected: installs `sebby` from the GitHub tag with no errors. Confirm with:

```bash
uv run python -c "from sebby.llm.cache import mark_cache_breakpoint, mark_cache_breakpoint_on_tools; print('ok')"
```

Expected output: `ok`

- [ ] **Step 3: Run the full test suite to confirm the dependency addition alone doesn't break anything**

Run: `cd /Users/sebby/Developer/organist_bot && uv run pytest --tb=short -q`
Expected: same pass/fail state as before this change (this step only adds a dependency, doesn't use it yet)

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore: add sebby as a dependency (pinned to v0.1.0)"
```

---

### Task 2: Delegate prompt-cache breakpoint marking to `sebby.llm.cache`

**Files:**
- Modify: `organist_bot/integrations/unified_agent.py` (the `_with_anthropic_cache_control` function, and its import block)

**Interfaces:**
- Consumes: `sebby.llm.cache.mark_cache_breakpoint(messages: list[dict]) -> list[dict]`, `sebby.llm.cache.mark_cache_breakpoint_on_tools(tools: list[dict]) -> list[dict]`
- Produces: `_with_anthropic_cache_control` keeps its existing signature and observable behavior — no caller-visible change

- [ ] **Step 1: Read the current implementation and its call sites to confirm nothing has drifted since this plan was written**

Run: `grep -n "_with_anthropic_cache_control" /Users/sebby/Developer/organist_bot/organist_bot/integrations/unified_agent.py`
Expected: the function definition (around line 364) and its call site inside `_call_llm_with_failover` (around line 459-461). If the line numbers or the function body differ meaningfully from what's shown below, read the actual current code and adapt this task to it rather than blindly overwriting — the intent (delegate the marking mechanics to `sebby.llm.cache`, preserve the exact same breakpoint locations) is what matters, not exact line numbers.

- [ ] **Step 2: Replace the function body**

Find this function in `organist_bot/integrations/unified_agent.py`:

```python
def _with_anthropic_cache_control(
    messages: list[dict], tools: list[dict]
) -> tuple[list[dict], list[dict]]:
    """Return copies of `messages`/`tools` with Anthropic prompt-cache
    breakpoints on the system prompt and the last tool. Anthropic caches the
    entire prefix up to and including a marked block, so these two
    breakpoints cover SYSTEM_PROMPT + TOOLS -- otherwise resent verbatim on
    every call, including every iteration of one turn's tool-calling loop.

    Anthropic-only: OpenAI and Gemini both apply automatic prompt-prefix
    caching already, and don't understand this field. Always returns new
    list/dict objects rather than mutating `messages`/`tools` in place --
    those are the shared SYSTEM_PROMPT/TOOLS module constants (or a history
    list still owned by the caller), reused by every other provider's calls,
    and must come back out exactly as they went in."""
    cached_messages = list(messages)
    if cached_messages and cached_messages[0].get("role") == "system":
        system = cached_messages[0]
        content = system["content"]
        if isinstance(content, str):
            cached_messages[0] = {
                **system,
                "content": [
                    {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
                ],
            }

    cached_tools = list(tools)
    if cached_tools:
        cached_tools[-1] = {**cached_tools[-1], "cache_control": {"type": "ephemeral"}}

    return cached_messages, cached_tools
```

Replace it with:

```python
def _with_anthropic_cache_control(
    messages: list[dict], tools: list[dict]
) -> tuple[list[dict], list[dict]]:
    """Return copies of `messages`/`tools` with Anthropic prompt-cache
    breakpoints on the system prompt and the last tool. Anthropic caches the
    entire prefix up to and including a marked block, so these two
    breakpoints cover SYSTEM_PROMPT + TOOLS -- otherwise resent verbatim on
    every call, including every iteration of one turn's tool-calling loop.

    Anthropic-only: OpenAI and Gemini both apply automatic prompt-prefix
    caching already, and don't understand this field.

    Delegates the actual breakpoint-marking mechanics to `sebby.llm.cache`
    (shared across sebby's projects, see that module's docstrings for the
    shallow-copy/never-mutates guarantee this relies on). `mark_cache_breakpoint`
    marks the LAST entry of the list it's given a breakpoint -- passing a
    single-element list containing only the system message makes it mark
    that message specifically.
    """
    cached_messages = list(messages)
    if cached_messages and cached_messages[0].get("role") == "system":
        cached_messages[0] = mark_cache_breakpoint([cached_messages[0]])[0]

    cached_tools = mark_cache_breakpoint_on_tools(list(tools))

    return cached_messages, cached_tools
```

- [ ] **Step 3: Add the import**

Find the imports block at the top of `organist_bot/integrations/unified_agent.py` and add, near the other third-party imports:

```python
from sebby.llm.cache import mark_cache_breakpoint, mark_cache_breakpoint_on_tools
```

- [ ] **Step 4: Run the cache-specific test class to verify behavior is unchanged**

Run: `cd /Users/sebby/Developer/organist_bot && uv run pytest tests/test_unified_agent.py -k "TestAnthropicCacheControl" -v`
Expected: PASS, same tests that passed before this change — `test_openai_call_does_not_get_cache_control`, `test_gemini_call_does_not_get_cache_control`, `test_failover_to_anthropic_still_gets_cache_control`, and whatever other tests are in that class. If any test fails, read its assertion carefully: `sebby.llm.cache.mark_cache_breakpoint` handles one edge case differently from the original code (it marks the system message even if its `content` is already a list of blocks, not just a string — the original code's `isinstance(content, str)` guard skipped marking in that case). This is a real, intentional, minor behavior improvement, not a bug — if a test specifically asserts the OLD (arguably-a-bug) skip-when-already-a-list behavior, that test's assertion is now wrong and should be updated to expect the corrected behavior; do not revert the fix to make an incorrect assertion pass. If you're unsure whether a failing assertion reflects this case or something else, stop and report NEEDS_CONTEXT with the specific failure.

- [ ] **Step 5: Run the full failover test class too, since it exercises `_with_anthropic_cache_control` indirectly**

Run: `cd /Users/sebby/Developer/organist_bot && uv run pytest tests/test_unified_agent.py -k "TestCallLlmWithFailover" -v`
Expected: PASS, unchanged from before this task

- [ ] **Step 6: Run lint and type checks**

Run: `cd /Users/sebby/Developer/organist_bot && uv run ruff check . && uv run mypy organist_bot/`
Expected: both clean

- [ ] **Step 7: Run the full test suite**

Run: `cd /Users/sebby/Developer/organist_bot && uv run pytest --tb=short -q`
Expected: same pass/fail state as Task 1 Step 3 (no new failures, no fewer passes)

- [ ] **Step 8: Commit**

```bash
git add organist_bot/integrations/unified_agent.py
git commit -m "refactor: delegate prompt-cache breakpoint marking to sebby.llm.cache"
```

---

### Task 3: Adopt the `sebby-toolkit` plugin for generic hooks

**Files:**
- Delete: `.claude/hooks/block_env_git.py`
- Delete: `.claude/hooks/scrub_env_transcript.py`
- Delete: `.claude/hooks/post_edit_lint.py`
- Delete: `.claude/hooks/stop_quality_check.py`
- Delete: `.claude/hooks/post_pr_created.py`
- Delete: `.claude/hooks/post_merge_docs_update.py`
- Modify: `.claude/settings.json` (remove the registrations for the six deleted hooks; keep everything else — `require_pr_reviewers.sh`'s `PreToolUse` entry, `validate_filter_registration.py`/`validate_invoice_tools.py`'s `PostToolUse` entries, the permissions block)
- Create: `.claude/PLUGIN_MIGRATION.md` (a short, prominent note for the human — see Step 4)

**Interfaces:**
- Consumes: the `sebby-toolkit` plugin (external — installed via `/plugin marketplace add seby-dev/sebby` + `/plugin install sebby-toolkit`, a manual step this plan cannot perform)
- Produces: nothing importable — this task removes duplicated files and config

**Note:** this task does NOT install the plugin (that's an interactive `/plugin` command this automated plan can't run) — it removes the now-duplicated local copies and leaves clear, prominent instructions. The human must run the install step immediately after this task's changes land, or these six hooks simply won't fire until they do.

- [ ] **Step 1: Delete the six generic hook scripts**

```bash
cd /Users/sebby/Developer/organist_bot
git rm .claude/hooks/block_env_git.py
git rm .claude/hooks/scrub_env_transcript.py
git rm .claude/hooks/post_edit_lint.py
git rm .claude/hooks/stop_quality_check.py
git rm .claude/hooks/post_pr_created.py
git rm .claude/hooks/post_merge_docs_update.py
```

- [ ] **Step 2: Update `.claude/settings.json`**

Read the current `.claude/settings.json` first (`cat .claude/settings.json`) to confirm its exact current structure before editing — the plan's earlier investigation found this content, but confirm it matches before you edit:

```json
{
  "permissions": {
    "defaultMode": "acceptEdits",
    "allow": [
      "Read(**)",
      "Write(**)",
      "Edit(**)",
      "Glob(**)",
      "Grep(**)",
      "Bash(git *)",
      "Bash(npm run *)",
      "Bash(npx *)",
      "Bash(python *)",
      "Bash(pytest *)",
      "Bash(ruff *)",
      "Bash(mypy *)",
      "Bash(uv *)",
      "Bash(EMAIL_SENDER=* *)",
      "Bash(EMAIL_PASSWORD=* *)",
      "Bash(CC_EMAIL=* *)",
      "Bash(sed *)"
    ],
    "deny": [
      "Read(secrets/**)",
      "Bash(rm -rf *)",
      "Bash(sudo *)"
    ],
    "ask": [
      "Read(.env)",
      "Read(.env.*)"
    ]
  },
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "bash \"${CLAUDE_PROJECT_DIR}/.claude/hooks/require_pr_reviewers.sh\"",
            "if": "Bash(gh pr create:*)"
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Write|Edit",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"${CLAUDE_PROJECT_DIR}/.claude/hooks/validate_filter_registration.py\""
          },
          {
            "type": "command",
            "command": "python3 \"${CLAUDE_PROJECT_DIR}/.claude/hooks/validate_invoice_tools.py\" \"${CLAUDE_PROJECT_DIR}/organist_bot/integrations/unified_agent.py\""
          }
        ]
      }
    ]
  }
}
```

This removes: the `PreToolUse`/`Bash` entry that had `require_pr_reviewers.sh` alongside nothing else (unchanged — it's kept, just noting no other `PreToolUse` entries existed to remove), the `PostToolUse`/`Bash` entries for `post_pr_created.py` and `post_merge_docs_update.py` (now provided by the plugin, gated the same way via `if`), the `post_edit_lint.py` entry from the `Write|Edit` matcher (now provided by the plugin), the `Read|Write|Edit` matcher block entirely (it only contained `scrub_env_transcript.py`, now provided by the plugin), and the entire `Stop` block (it only contained `stop_quality_check.py` and `scrub_env_transcript.py`, both now provided by the plugin).

If your read of the actual current file differs from the "Note on scope" section's description at the top of this plan (e.g. `settings.local.json` overlaps, or the file has drifted since this plan was written), adapt this edit to match reality — the goal is: keep `require_pr_reviewers.sh`, `validate_filter_registration.py`, `validate_invoice_tools.py` wired exactly as they are now, remove every reference to the six deleted hook scripts, touch nothing else.

- [ ] **Step 3: Verify the JSON is well-formed**

Run: `cd /Users/sebby/Developer/organist_bot && python3 -c "import json; json.load(open('.claude/settings.json'))" && echo OK`
Expected: `OK`

- [ ] **Step 4: Write `.claude/PLUGIN_MIGRATION.md`**

```markdown
# Action required: install the sebby-toolkit plugin

This project's generic Claude Code hooks (env-file git-add blocking,
transcript secret scrubbing, post-edit lint, stop-time quality gate,
post-PR-created nudge, post-merge docs-sync nudge) were removed from
`.claude/hooks/` in favor of the shared `sebby-toolkit` plugin. **Until you
install it, none of those hooks will fire.**

## Install

    /plugin marketplace add seby-dev/sebby
    /plugin install sebby-toolkit

## Configure

Add an `"env"` block to `.claude/settings.json` (or set these in your
shell environment) so the plugin's hooks match this project's previous
behavior:

    "env": {
      "SEBBY_LINT_COMMAND": "ruff check --output-format=concise",
      "SEBBY_TYPECHECK_COMMAND": "mypy organist_bot/",
      "SEBBY_DOCS_FILES": "README.md,technical-report.html"
    }

`SEBBY_LINT_COMMAND`'s default already matches what this project used, so
that one is optional; `SEBBY_TYPECHECK_COMMAND` and `SEBBY_DOCS_FILES` need
to be set explicitly to match this project's prior `stop_quality_check.py`
(which checked `organist_bot/` specifically) and `post_merge_docs_update.py`
(which checked both `README.md` and `technical-report.html`) behavior.

Once installed and configured, delete this file.
```

- [ ] **Step 5: Commit**

```bash
git add .claude/settings.json .claude/PLUGIN_MIGRATION.md
git commit -m "chore: adopt sebby-toolkit plugin for generic hooks

Removes duplicated local hook scripts (block_env_git, scrub_env_transcript,
post_edit_lint, stop_quality_check, post_pr_created, post_merge_docs_update)
in favor of the shared sebby-toolkit Claude Code plugin. See
.claude/PLUGIN_MIGRATION.md for the required manual install step -- these
hooks will not fire again until the plugin is installed and configured."
```

---

### Task 4: Final verification

**Files:**
- None (verification only)

**Interfaces:**
- Consumes: everything from Tasks 1-3
- Produces: nothing — confirms the migration is complete and correct

- [ ] **Step 1: Run the full test suite one more time**

Run: `cd /Users/sebby/Developer/organist_bot && uv run pytest --tb=short -q`
Expected: same pass/fail state as before this migration started (Task 1 Step 3's baseline)

- [ ] **Step 2: Run the project's CI checks locally**

Run: `cd /Users/sebby/Developer/organist_bot && uv run ruff check . && uv run ruff format --check . && uv run mypy organist_bot/`
Expected: all clean, matching what `.github/workflows/ci.yml`'s `lint` job runs

- [ ] **Step 3: Confirm no dangling references to the deleted hook files**

Run: `cd /Users/sebby/Developer/organist_bot && grep -rn "block_env_git\|scrub_env_transcript\|post_edit_lint\|stop_quality_check\|post_pr_created\|post_merge_docs_update" --include="*.json" --include="*.py" --include="*.md" . | grep -v PLUGIN_MIGRATION.md`
Expected: no output (the only remaining reference should be in `PLUGIN_MIGRATION.md`, which the grep excludes)

- [ ] **Step 4: Report a summary**

In your task report, summarize: what changed, the before/after test pass counts (should be identical), and — prominently — restate that `/plugin marketplace add seby-dev/sebby` + `/plugin install sebby-toolkit` + the `.claude/settings.json` env block from `PLUGIN_MIGRATION.md` are required manual follow-ups, not yet done by this plan.
