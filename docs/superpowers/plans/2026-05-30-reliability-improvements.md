# Reliability & Maintainability Improvements — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the file-backed stores crash-safe and race-safe, stop re-fetching Phase-2-rejected gigs, refactor the monolithic unified-agent tool dispatcher into a registry, and cover three untested critical paths.

**Architecture:** Four independently shippable PRs. PR1 introduces a shared atomic/locked persistence helper and migrates the unsafe stores onto it; PR2 (depends on PR1) widens the seen-gig write; PR3 restructures `unified_agent` behind a handler registry with a `ToolContext`; PR4 adds tests. Each PR ends with its own ship step.

**Tech Stack:** Python 3.12, pydantic-settings, pytest, `fcntl`/`tempfile`/`os.replace`, Anthropic SDK, ruff + mypy (enforced by pre-commit and the Stop hook).

**Test env:** all `pytest` commands require dummy env vars: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com`.

**Merge order:** PR1 → PR2 (both touch `storage.py`). PR3 and PR4 branch off `main` independently.

**Per-PR ship procedure (used at each "Ship" step):**
```bash
# from the PR branch, with a clean tree for the touched files:
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest --tb=short -q
ruff check . && mypy organist_bot/
git push -u origin HEAD
# run BOTH reviewers (gate), then bypass sentinel, then:
gh pr create --title "<title>" --body "<body>"
gh pr merge --squash --auto --delete-branch
```
The `require_pr_reviewers.sh` PreToolUse gate blocks `gh pr create` until CodeRabbit + `pipeline-impact-reviewer` run against HEAD and the printed sentinel path is `touch`ed (in a **separate** Bash call — the gate checks the sentinel before the command runs).

---

## File Structure

**PR1**
- Create: `organist_bot/atomic_store.py` — atomic+locked JSON/text persistence.
- Create: `tests/test_atomic_store.py`
- Modify: `organist_bot/filter_store.py`, `organist_bot/runtime_config_store.py`, `organist_bot/storage.py`, `organist_bot/application_store.py`

**PR2**
- Modify: `main.py` (Phase-3 seen-gig write)
- Modify: `tests/test_main.py`

**PR3**
- Create: `organist_bot/integrations/agent_tools/__init__.py`, `registry.py`, `context.py`, `results.py`, and `{gig,client,invoice,filter,analytics,config}_tools.py`
- Create: `organist_bot/integrations/agent_state.py` (disk-backed `ChatState`)
- Modify: `organist_bot/integrations/unified_agent.py` (becomes thin loop over the registry), `.claude/hooks/validate_invoice_tools.py`
- Modify/extend: `tests/test_unified_agent.py`, add `tests/test_agent_registry.py`, `tests/test_agent_state.py`

**PR4**
- Modify: `tests/test_reply_monitor.py`, `tests/test_main.py`
- Create: `tests/test_logging_config.py`

---

# PR1 — Atomic store + cross-process locking

**Branch:** `feat/atomic-store`

### Task 1.1: `file_lock` context manager (TDD)

**Files:**
- Create: `organist_bot/atomic_store.py`
- Test: `tests/test_atomic_store.py`

- [ ] **Step 1: Write the failing test**
```python
# tests/test_atomic_store.py
from pathlib import Path
from organist_bot import atomic_store

def test_file_lock_is_reentrant_across_sequential_calls(tmp_path: Path):
    p = tmp_path / "x.json"
    with atomic_store.file_lock(p):
        pass
    # second acquisition after release must succeed (no deadlock, lock file reused)
    with atomic_store.file_lock(p):
        assert (tmp_path / "x.json.lock").exists()
```

- [ ] **Step 2: Run it, expect failure**
Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_atomic_store.py::test_file_lock_is_reentrant_across_sequential_calls -v`
Expected: FAIL (`ModuleNotFoundError`/`AttributeError`).

- [ ] **Step 3: Implement `atomic_store.py`**
```python
"""organist_bot/atomic_store.py
Atomic, lockable JSON/text persistence shared by the file-backed stores.

Generalizes the tempfile + os.replace pattern from application_store and adds
cross-process advisory locking (fcntl.flock) plus loud failure on corruption.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import organist_bot.alert as alert

logger = logging.getLogger(__name__)

_LOCK_TIMEOUT_S = 5.0


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Advisory exclusive lock on '<path>.lock'. Best-effort: on timeout, log
    and proceed unlocked (availability over strict consistency for a 2-min poll)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        deadline = time.monotonic() + _LOCK_TIMEOUT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    logger.warning("file_lock: timeout on %s — proceeding unlocked", lock_path)
                    break
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _atomic_replace(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(path))
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def write_json(path: Path, data: Any, *, lock: bool = True) -> None:
    payload = json.dumps(data, indent=2) + "\n"
    if lock:
        with file_lock(path):
            _atomic_replace(path, payload)
    else:
        _atomic_replace(path, payload)


def write_text_atomic(path: Path, text: str, *, lock: bool = True) -> None:
    if lock:
        with file_lock(path):
            _atomic_replace(path, text)
    else:
        _atomic_replace(path, text)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.exception("atomic_store: corrupt/unreadable %s", path)
        alert.send_alert(f"⚠️ Corrupt data file {path.name} — using default ({exc}).")
        return default
```

- [ ] **Step 4: Run test, expect pass**
Run: same command as Step 2. Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add organist_bot/atomic_store.py tests/test_atomic_store.py
git commit -m "feat: atomic_store helper with advisory file locking"
```

### Task 1.2: atomicity + corruption-recovery tests

**Files:** Test: `tests/test_atomic_store.py`

- [ ] **Step 1: Write failing tests**
```python
import json
import pytest
from organist_bot import atomic_store

def test_write_json_roundtrip(tmp_path):
    p = tmp_path / "d.json"
    atomic_store.write_json(p, {"a": 1})
    assert atomic_store.read_json(p, {}) == {"a": 1}

def test_failed_replace_leaves_original_intact(tmp_path, monkeypatch):
    p = tmp_path / "d.json"
    atomic_store.write_json(p, {"ok": True})
    monkeypatch.setattr(atomic_store.os, "replace",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError):
        atomic_store.write_json(p, {"ok": False})
    assert atomic_store.read_json(p, {}) == {"ok": True}            # unchanged
    assert list(p.parent.glob("tmp*")) == []                       # temp cleaned up

def test_corrupt_file_returns_default_and_alerts(tmp_path, monkeypatch):
    p = tmp_path / "d.json"
    p.write_text("{not valid json")
    calls = []
    monkeypatch.setattr(atomic_store.alert, "send_alert", lambda m: calls.append(m))
    assert atomic_store.read_json(p, {"default": True}) == {"default": True}
    assert len(calls) == 1
```

- [ ] **Step 2: Run, expect pass** (helper already implemented)
Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_atomic_store.py -v`
Expected: PASS (all 4). If `test_failed_replace...` finds a leftover `tmp*` file, the cleanup branch is wrong — fix before proceeding.

- [ ] **Step 3: Commit**
```bash
git add tests/test_atomic_store.py
git commit -m "test: atomicity and corruption-recovery for atomic_store"
```

### Task 1.3: migrate `runtime_config_store` (smallest store first)

**Files:** Modify: `organist_bot/runtime_config_store.py`

- [ ] **Step 1: Replace `_read`/`_write`**
Replace the bodies so reads/writes go through the helper (keep `RuntimeConfigStore` API identical):
```python
from organist_bot import atomic_store

_PATH = Path("data/runtime_config.json")

def _read() -> dict[str, int]:
    return dict(atomic_store.read_json(_PATH, {}))

def _write(data: dict[str, int]) -> None:
    atomic_store.write_json(_PATH, data)
```
For `set`/`reset`, wrap the read-modify-write in a single lock to close the TOCTOU window:
```python
def set(self, key: str, value: int) -> None:
    with atomic_store.file_lock(_PATH):
        data = dict(atomic_store.read_json(_PATH, {}))
        data[key] = value
        atomic_store.write_json(_PATH, data, lock=False)
```
(`reset` follows the same lock-wrapped pattern.)

- [ ] **Step 2: Run existing store tests**
Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/ -k runtime_config -v`
Expected: PASS. If no such tests exist, run the full suite (Step uses `pytest -q`).

- [ ] **Step 3: Commit**
```bash
git add organist_bot/runtime_config_store.py
git commit -m "refactor: runtime_config_store uses atomic_store"
```

### Task 1.4: migrate `filter_store`

**Files:** Modify: `organist_bot/filter_store.py`

- [ ] **Step 1: Swap `_read`/`_write` to the helper**
```python
from organist_bot import atomic_store

def _read() -> dict[str, list[str]]:
    raw = atomic_store.read_json(_PATH, {})
    return {k: list(raw.get(k, [])) for k in _KEYS}

def _write(data: dict[str, list[str]]) -> None:
    atomic_store.write_json(_PATH, data)
```

- [ ] **Step 2: Lock-wrap the multi-step mutators**
Wrap each read-modify-write mutator (`add_blacklist_email`, `remove_blacklist_email`, `add_period`, `remove_period`, `purge_past_periods`) in `with atomic_store.file_lock(_PATH):` and call the inner `read_json`/`write_json(..., lock=False)` so the purge-then-add sequence is atomic. Example for `add_period`:
```python
def add_period(key: str, period: str) -> bool:
    with atomic_store.file_lock(_PATH):
        if key == "unavailable_periods":
            _purge_past_periods_locked()      # extract a no-lock inner used by both
        data = {k: list(atomic_store.read_json(_PATH, {}).get(k, [])) for k in _KEYS}
        if period in data[key]:
            return False
        data[key].append(period)
        atomic_store.write_json(_PATH, data, lock=False)
        return True
```
Extract `_purge_past_periods_locked()` (no lock) and have the public `purge_past_periods()` wrap it in the lock.

- [ ] **Step 3: Run filter_store tests**
Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/ -k filter_store -v`
Expected: PASS.

- [ ] **Step 4: Commit**
```bash
git add organist_bot/filter_store.py
git commit -m "refactor: filter_store uses atomic_store with locked mutators"
```

### Task 1.5: migrate `storage` (CSV + hash) and `application_store`

**Files:** Modify: `organist_bot/storage.py`, `organist_bot/application_store.py`

- [ ] **Step 1: `storage.save_seen_gigs` / `save_listings_hash` → `write_text_atomic`**
```python
from organist_bot import atomic_store

def save_seen_gigs(seen: set[str], filepath: str = "data/seen_gigs.csv") -> None:
    path = Path(filepath)
    try:
        payload = "".join(f"{link}\r\n" for link in sorted(seen))   # csv.writer default lineterminator
        atomic_store.write_text_atomic(path, payload)
        logger.info("Saved seen gigs", extra={"count": len(seen), "filepath": str(path.resolve())})
    except Exception:
        logger.exception("Failed to save seen gigs", extra={"filepath": str(path), "count": len(seen)})
        raise

def save_listings_hash(hash_str: str, filepath: str = "data/listings_hash.txt") -> None:
    path = Path(filepath)
    try:
        atomic_store.write_text_atomic(path, hash_str)
    except Exception:
        logger.exception("Failed to save listings hash", extra={"filepath": str(path)})
        raise
```
NOTE: keep the CSV line terminator consistent with what `load_seen_gigs` (csv.reader) expects; `\r\n` matches Python's csv default. Verify the round-trip test in Step 2.

- [ ] **Step 2: `application_store._write` → delegate to helper**
Replace the private `_write` body with `atomic_store.write_json(_PATH, records)` and `_read` with `atomic_store.read_json(_PATH, [])`. Keep all public functions unchanged.

- [ ] **Step 3: Run storage + application_store tests**
Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/ -k "storage or application" -v`
Expected: PASS. Pay attention to any seen-gigs round-trip test — if it fails on line endings, adjust the terminator.

- [ ] **Step 4: Full suite + lint + types**
Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest --tb=short -q && ruff check . && mypy organist_bot/`
Expected: all PASS.

- [ ] **Step 5: Commit**
```bash
git add organist_bot/storage.py organist_bot/application_store.py
git commit -m "refactor: storage and application_store use atomic_store"
```

### Task 1.6: Ship PR1
- [ ] Push, run both reviewers, bypass sentinel, `gh pr create` (title `feat: atomic + locked file stores`), enable squash auto-merge per the ship procedure. After merge, `git checkout main && git pull`.

---

# PR2 — Phase-2-rejected seen-gig fix

**Branch:** `feat/seen-gig-phase2` (off `main` AFTER PR1 merges)

### Task 2.1: failing test — rejected gig is recorded & not re-fetched

**Files:** Modify: `tests/test_main.py`

- [ ] **Step 1: Write the failing test**
```python
def test_phase2_rejected_gig_is_marked_seen(tmp_path, monkeypatch):
    """A gig rejected by a Phase-2-only filter (blacklist) must be written to
    seen_gigs.csv so it is not detail-fetched again next tick."""
    # Arrange a scraper stub returning one gig whose detail page yields a
    # blacklisted contact email; blacklist contains that email.
    # Run main._run(scraper, dry_run=False) with seen-gigs path pointed at tmp.
    # Assert the gig.link IS present in the saved seen set.
    ...
```
Implement using the existing `test_main` scraper-stub fixtures (mirror the closest existing `_run` test). The key assertion: after `_run`, `load_seen_gigs(<tmp csv>)` contains the rejected gig's URL.

- [ ] **Step 2: Run, expect failure**
Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_main.py::test_phase2_rejected_gig_is_marked_seen -v`
Expected: FAIL (URL absent — current code only saves `valid_gigs`).

### Task 2.2: widen the seen-gig write

**Files:** Modify: `main.py` (the Phase-3 block around the current `save_seen_gigs` call)

- [ ] **Step 1: Move + widen the save**
Replace the `save_seen_gigs(seen=seen_gigs_set | set(g.link for g in valid_gigs))` (inside `if valid_gigs:`) with a save that runs whenever any gig was detail-fetched, covering all of `gig_list`:
```python
# After Phase 2, before the post-pipeline steps. Record every gig we evaluated
# at detail level (valid OR rejected by a Phase-2-only filter) so blacklist/
# postcode rejections are not re-fetched on the next listings change.
if not dry_run:
    newly_seen = {g.link for g in gig_list if g.link}
    if newly_seen:
        save_seen_gigs(seen=seen_gigs_set | newly_seen)
```
Remove the old in-branch `save_seen_gigs` call. Keep the `if valid_gigs:` notify block otherwise unchanged.

- [ ] **Step 2: Run the new test + the existing main tests**
Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_main.py -v`
Expected: PASS, including any existing dry-run test (still writes nothing).

- [ ] **Step 3: Update the docstring/comment** documenting the accepted trade-off (un-blacklisting / raising max_travel won't re-surface a seen gig).

- [ ] **Step 4: Full suite + lint + types**
Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest --tb=short -q && ruff check . && mypy organist_bot/`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add main.py tests/test_main.py
git commit -m "fix: mark all detail-evaluated gigs seen to stop re-fetching Phase-2 rejections"
```

### Task 2.3: Ship PR2
- [ ] Ship per procedure (title `fix: stop re-fetching Phase-2-rejected gigs`). After merge, `git checkout main && git pull`.

---

# PR3 — unified_agent refactor (full cleanup)

**Branch:** `refactor/agent-registry` (off `main`)

> This is a structural move of ~28 handlers out of one `if`-chain into 6 domain modules behind a registry, plus typed results and disk-backed chat state. Behavior is preserved: `tests/test_unified_agent.py` must keep passing (wiring/import changes only). Implement one domain module at a time, running the full agent test module after each so a regression is localized immediately.

### Task 3.1: define the contracts (no behavior yet)

**Files:** Create `integrations/agent_tools/{__init__.py,context.py,results.py,registry.py}`

- [ ] **Step 1: `context.py` — `ToolContext` + `ChatState`**
```python
from __future__ import annotations
from dataclasses import dataclass, field

@dataclass
class ChatState:
    history: list = field(default_factory=list)
    last_invoice: dict | None = None
    last_gig_listing: list | None = None
    last_application_listing: list | None = None

@dataclass
class ToolContext:
    chat_id: int
    state: ChatState
```

- [ ] **Step 2: `results.py` — typed results**
```python
from __future__ import annotations
from dataclasses import dataclass

@dataclass
class TextResult:
    text: str

@dataclass
class VerbatimResult:           # sent to the user without further LLM phrasing
    text: str

@dataclass
class PDFResult:
    path: str
    caption: str = ""

ToolResult = TextResult | VerbatimResult | PDFResult
```

- [ ] **Step 3: `registry.py` — register + dispatch**
```python
from __future__ import annotations
from collections.abc import Awaitable, Callable
from .context import ToolContext
from .results import ToolResult

Handler = Callable[[dict, ToolContext], Awaitable[ToolResult]]
TOOL_REGISTRY: dict[str, Handler] = {}
TOOL_SCHEMAS: list[dict] = []

def register(schema: dict):
    def deco(fn: Handler) -> Handler:
        TOOL_REGISTRY[schema["name"]] = fn
        TOOL_SCHEMAS.append(schema)
        return fn
    return deco

async def dispatch(name: str, input_data: dict, ctx: ToolContext) -> ToolResult:
    handler = TOOL_REGISTRY.get(name)
    if handler is None:
        from .results import TextResult
        return TextResult(text=f"Unknown tool: {name}")
    return await handler(input_data, ctx)
```

- [ ] **Step 4: Commit scaffolding**
```bash
git add organist_bot/integrations/agent_tools/
git commit -m "feat: agent tool registry, ToolContext, and typed results scaffolding"
```

### Task 3.2: disk-backed `ChatState` (TDD)

**Files:** Create `integrations/agent_state.py`, `tests/test_agent_state.py`

- [ ] **Step 1: Failing test — round-trip through disk**
```python
def test_chat_state_roundtrips(tmp_path, monkeypatch):
    from organist_bot.integrations import agent_state
    monkeypatch.setattr(agent_state, "_PATH", tmp_path / "agent_state.json")
    agent_state.save(123, last_invoice={"number": "INV-1"})
    loaded = agent_state.load(123)
    assert loaded.last_invoice == {"number": "INV-1"}
```

- [ ] **Step 2: Run, expect fail.** `pytest tests/test_agent_state.py -v`

- [ ] **Step 3: Implement `agent_state.py`** using `atomic_store` (PR1) if present on `main`; if PR3 lands before PR1, inline a minimal `tempfile`+`os.replace` writer here and leave a `# TODO: switch to atomic_store once merged` comment. Persist a `{chat_id: {…}}` map; `load(chat_id)` returns a `ChatState`, `save(chat_id, **fields)` merges + writes. (History is NOT persisted — only last_invoice / last_gig_listing / last_application_listing — to bound file size.)

- [ ] **Step 4: Run, expect pass. Commit.**
```bash
git add organist_bot/integrations/agent_state.py tests/test_agent_state.py
git commit -m "feat: disk-backed per-chat agent state"
```

### Task 3.3 – 3.8: migrate one domain per task

For each domain in order — **gig, client, invoice, filter, analytics, config** — perform this identical task (repeat the steps; do NOT batch):

- [ ] **Step 1:** Move that domain's `if name == "…"` branches from `unified_agent._execute_tool` into `agent_tools/<domain>_tools.py`, each wrapped as an `async def` decorated with `@register(<schema dict moved from TOOLS>)`, taking `(input_data, ctx)` and returning a `ToolResult`. Replace reads of the old module globals (`_last_invoice[chat_id]` etc.) with `ctx.state.*`. Replace the response-name-set membership (`_VERBATIM_RESPONSE_TOOLS`/`_PDF_RESPONSE_TOOLS`) by returning `VerbatimResult`/`PDFResult` directly.
- [ ] **Step 2:** Delete those branches + their `TOOLS` entries from `unified_agent.py`; import the new module so its `@register` runs.
- [ ] **Step 3:** Run `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_unified_agent.py -v`. Expected: PASS (fix wiring until green before moving to the next domain).
- [ ] **Step 4:** Commit `refactor: move <domain> tools into registry module`.

### Task 3.9: thin the agent loop + raise max_tokens

**Files:** Modify `unified_agent.py`

- [ ] **Step 1:** Replace `_execute_tool` with a call to `registry.dispatch`; build the Anthropic request `tools=` from `registry.TOOL_SCHEMAS`; dispatch the response on `ToolResult` *type* (PDF → send document, Verbatim → send raw, Text → normal). Construct a `ToolContext` per message from `agent_state.load(chat_id)` and `save` back after mutations. Raise `max_tokens` 1024 → 4096.
- [ ] **Step 2:** Expose a public `make_calendar_client()` (drop the leading underscore or re-export) and update `telegram_bot.py` / `reply_monitor.py` callers.
- [ ] **Step 3:** Run full agent tests + suite. `EMAIL_SENDER=… pytest tests/test_unified_agent.py -q && pytest -q`. Expected PASS.
- [ ] **Step 4:** Commit `refactor: unified_agent loop dispatches via registry`.

### Task 3.10: update the validator hook + registry test

**Files:** Modify `.claude/hooks/validate_invoice_tools.py`; create `tests/test_agent_registry.py`

- [ ] **Step 1:** Rewrite `validate_invoice_tools.py` to import the registry and assert `set(s["name"] for s in TOOL_SCHEMAS) == set(TOOL_REGISTRY)` (every schema has a handler), printing the `systemMessage` warning on mismatch. (It no longer parses `_execute_tool`.)
- [ ] **Step 2:** Add `tests/test_agent_registry.py::test_every_schema_has_handler` asserting the same invariant at unit level.
- [ ] **Step 3:** Run it; pipe-test the hook: `echo '{}' | python3 .claude/hooks/validate_invoice_tools.py` → silent. Commit.

### Task 3.11: Ship PR3
- [ ] Full suite + lint + types green, then ship per procedure (title `refactor: unified_agent handler registry + persisted chat state`). After merge, `git checkout main && git pull`.

---

# PR4 — Test additions

**Branch:** `test/critical-path-coverage` (off `main`)

### Task 4.1: `_classify_reply` (TDD against a mocked Anthropic client)

**Files:** Modify `tests/test_reply_monitor.py`

- [ ] **Step 1: Write tests**
```python
def test_classify_reply_unexpected_label_is_unclear(monkeypatch):
    import organist_bot.reply_monitor as rm
    fake = _fake_anthropic(text="maybe")           # returns a TextBlock with "maybe"
    monkeypatch.setattr(rm, "anthropic", fake.module)
    assert rm._classify_reply("subject", "body") == "unclear"

def test_classify_reply_api_exception_is_unclear(monkeypatch):
    import organist_bot.reply_monitor as rm
    monkeypatch.setattr(rm, "anthropic", _raising_anthropic())
    assert rm._classify_reply("subject", "body") == "unclear"

def test_classify_reply_accepted(monkeypatch):
    import organist_bot.reply_monitor as rm
    monkeypatch.setattr(rm, "anthropic", _fake_anthropic(text="accepted").module)
    assert rm._classify_reply("subject", "body") == "accepted"
```
Add `_fake_anthropic`/`_raising_anthropic` helpers building a stub `Anthropic` whose `.messages.create(...)` returns an object with `.content = [TextBlock(text=…)]` or raises. Inspect `reply_monitor._classify_reply` first to match the exact client call + label set.

- [ ] **Step 2: Run, fix helpers until green.** `EMAIL_SENDER=… pytest tests/test_reply_monitor.py -k classify -v`
- [ ] **Step 3: Commit** `test: cover reply_monitor._classify_reply label + error paths`.

### Task 4.2: `main._run` drain + alert path

**Files:** Modify `tests/test_main.py`

- [ ] **Step 1: Write tests**
```python
def test_run_drains_sheets_logger_once(...):
    sheets = Mock()
    sheets.drain.return_value = 3
    main._run(scraper_stub, sheets_logger=sheets, dry_run=False)
    sheets.drain.assert_called_once()

def test_run_alerts_when_drain_raises(monkeypatch, ...):
    sheets = Mock()
    sheets.drain.side_effect = RuntimeError("sheets down")
    calls = []
    monkeypatch.setattr(main.alert, "send_alert", lambda m: calls.append(m))
    main._run(scraper_stub, sheets_logger=sheets, dry_run=False)   # must NOT raise
    # current code logs a warning but does not alert — see Step 2
```
NOTE: the current `_run` drain block logs a warning but does **not** call `alert`. The spec asks the test to assert an alert fires — so Step 2 adds that alert (a one-line behavior addition), making this PR also a tiny fix, not pure test.

- [ ] **Step 2: Add the alert in `main._run`** drain `except`: `alert.send_alert(f"⚠️ Sheets flush failed — {exc}")` alongside the existing warning. Run tests green.
- [ ] **Step 3: Commit** `test: cover drain path; alert on Sheets flush failure`.

### Task 4.3: `logging_config` formatter/filter

**Files:** Create `tests/test_logging_config.py`

- [ ] **Step 1: Write tests** (read `logging_config.py` first for exact names)
```python
def test_jsonformatter_includes_core_and_excludes_stdlib_extras():
    import json, logging
    from organist_bot.logging_config import JSONFormatter
    rec = logging.LogRecord("n", logging.INFO, __file__, 1, "msg", None, None)
    rec.custom = "x"
    out = json.loads(JSONFormatter().format(rec))
    assert out["message"] == "msg" and out["level"] == "INFO"
    assert out["custom"] == "x"
    assert "args" not in out and "msecs" not in out      # _STDLIB_FIELDS excluded

def test_runidfilter_injects_run_id():
    from organist_bot.logging_config import RunIdFilter, set_run_id
    set_run_id("abc123")
    rec = logging.LogRecord("n", logging.INFO, __file__, 1, "m", None, None)
    assert RunIdFilter().filter(rec) and rec.run_id == "abc123"
```

- [ ] **Step 2: Run; adjust to real attribute names** (`run_id` field, formatter key names) until green.
- [ ] **Step 3: Commit** `test: cover JSONFormatter and RunIdFilter`.

### Task 4.4: Ship PR4
- [ ] Full suite + lint + types green, then ship per procedure (title `test: cover classifier, drain, and log formatter paths`).

---

## Self-Review (completed during authoring)
- **Spec coverage:** PR1 §atomic+lock+alert → Tasks 1.1–1.5; PR2 §seen-gig → 2.1–2.2; PR3 §registry/ToolContext/typed-results/state/max_tokens/validator → 3.1–3.10; PR4 §three tests → 4.1–4.3. ✓
- **Placeholders:** the only deferred specifics are "match existing fixtures / read the module first" for tests whose exact stubs depend on current signatures (test_main scraper stub, `_classify_reply` client shape, `logging_config` names) — these are *inspection* instructions, not unwritten logic; flagged explicitly at each.
- **Type consistency:** `ToolContext`/`ChatState`/`ToolResult`/`register`/`dispatch`/`TOOL_SCHEMAS`/`TOOL_REGISTRY` names are used identically across Tasks 3.1, 3.3–3.10. `write_json`/`read_json`/`write_text_atomic`/`file_lock` consistent across PR1 and 3.2.
