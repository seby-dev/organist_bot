# Reliability & Maintainability Improvements — Design

**Date:** 2026-05-30
**Status:** Approved (design); pending spec review
**Scope:** Four independent improvements identified in the codebase analysis, shipped as four separate PRs.

## Background

A four-area analysis of `organist_bot` surfaced clustered risks around **state-store durability** (non-atomic writes, cross-process races) and the **agent surface** (a monolithic tool dispatcher, untested classifier paths). This design addresses the four highest-leverage items. Each ships as its own PR for independent review and revert.

Decisions locked during brainstorming:

| Topic | Decision |
|---|---|
| Phase-2 seen-gig retention | **Mark all evaluated gigs seen** (simplest; matches applied-gig behavior) |
| Agent refactor shape | **Handler modules + dispatch registry** with a `ToolContext` |
| Agent refactor scope | **Full cleanup** (structure + response-dispatch + state persistence + max_tokens) |
| Shipping | **Per-feature PRs** (PR1 → PR2 sequenced; PR3, PR4 independent) |

## Sequencing

```
main
 ├─ PR1  atomic-store helper + locking        (merge first)
 │   └─ PR2  Phase-2 seen-gig fix             (depends on PR1: both touch storage.py)
 ├─ PR3  unified_agent refactor               (independent)
 └─ PR4  test additions                       (independent)
```

PR1 must merge before PR2 (PR2 consumes PR1's atomic writer for the seen-gigs CSV). PR3 and PR4 branch off `main` and can proceed in parallel.

---

## PR1 — Atomic writes + cross-process locking

### Problem
`filter_store._write` ([filter_store.py:37](../../../organist_bot/filter_store.py)), `runtime_config_store._write` ([runtime_config_store.py:22](../../../organist_bot/runtime_config_store.py)), and `storage.save_seen_gigs`/`save_listings_hash` ([storage.py:40](../../../organist_bot/storage.py)) write directly to the target path. A crash mid-write truncates the file; the `_read` `except` then silently returns an empty config — **wiping all blacklist / unavailability / seen-gig state with no alert**. Additionally, the scheduler (`main.py`) and the bot (`telegram_bot.py`) are separate processes doing unlocked read-modify-write on the same JSON files, so interleaved writes clobber each other.

`application_store._write` ([application_store.py:37-50](../../../organist_bot/application_store.py)) already does this correctly (`tempfile.mkstemp` sibling + `os.replace`). The fix generalizes that pattern and adds locking.

### Design
New module `organist_bot/atomic_store.py`:

```python
def read_json(path: Path, default):           # parse-fail → alert + return default
def write_json(path: Path, data, *, lock=True) # mkstemp sibling + os.replace, under flock
@contextmanager
def file_lock(path: Path)                       # fcntl.flock on a "<path>.lock" sidecar
```

- **Atomicity:** `tempfile.mkstemp(dir=path.parent)` (same filesystem, so `os.replace` is atomic) → write+flush+`os.fsync` → `os.replace(tmp, path)`; unlink tmp on failure.
- **Cross-process safety:** wrap the read-modify-write in `fcntl.flock(LOCK_EX)` on a `<path>.lock` sidecar. Both processes run on the same host, so advisory `flock` is sufficient.
- **Loud failure:** on `JSONDecodeError`, call `alert.send_alert(...)` *before* returning the default, so a corrupt store is visible in Telegram instead of silently emptying filters.

A non-JSON variant (`write_text_atomic`) covers `save_seen_gigs` (CSV) and `save_listings_hash`.

### Migration
- `filter_store`, `runtime_config_store` → use `read_json`/`write_json`. For multi-step read-modify-write (e.g. `filter_store.add_period` → `purge_past_periods` → `_read` → `_write`), hold a single `file_lock` across the whole sequence to close the TOCTOU window.
- `storage.save_seen_gigs` / `save_listings_hash` → `write_text_atomic`.
- `application_store` → adopt the shared helper (delete its private `_write`), keeping behavior identical.

### Error handling
- Corrupt file → alert + default (filters fail safe: an empty blacklist means *more* gigs evaluated, not applications to blacklisted orgs, because blacklist only *rejects*). Document this explicitly.
- Lock acquisition is blocking with a short timeout; on timeout, log + proceed without the lock (availability over strict consistency for a 2-minute poll loop).

### Testing
- Atomicity: monkeypatch `os.replace` to raise mid-write; assert the original file is intact and the temp file is cleaned up.
- Corruption recovery: write garbage to a store file; assert `read_json` returns the default *and* `alert.send_alert` was called.
- Concurrency: two threads incrementing a list via `write_json(lock=True)`; assert no lost updates.

---

## PR2 — Phase-2-rejected seen-gig fix

### Problem
`main.py` writes seen gigs only when `valid_gigs` is non-empty ([main.py:306](../../../main.py)): `save_seen_gigs(seen_gigs_set | {g.link for g in valid_gigs})`. A gig that passes the pre-filter but is rejected at Phase 2 — only possible via `BlacklistFilter` or `PostcodeFilter`, since `Fee`/`SundayTime`/`Availability` run in *both* passes — is never recorded, so it is re-fetched (detail-page HTTP) on every listings change.

### Design
After Phase 2, persist **every detail-fetched gig** (`gig_list`), not just `valid_gigs`:

```python
newly_seen = {g.link for g in gig_list if g.link}
save_seen_gigs(seen_gigs_set | newly_seen)   # via PR1's atomic writer
```

Move the save out of the `if valid_gigs:` branch so it runs whenever `gig_list` is non-empty (still skipped in `dry_run`). `link=None` gigs are excluded from the seen-set (they cannot be deduped by URL anyway — see note).

### Consequences (accepted)
Un-blacklisting an email or raising `max_travel_minutes` will not re-surface a gig already marked seen. These are rare admin actions; the efficiency win (no per-tick re-fetch) outweighs it. Documented in the function docstring.

### Note (out of scope, flagged)
`Gig.link` can be `None` when the scraper misses the field; `SeenFilter` then lets it through every tick. That is a *separate* correctness item (model-level) not bundled here.

### Testing
- A gig rejected by `BlacklistFilter` in Phase 2 is written to `seen_gigs.csv` and, on the next tick, is rejected by the pre-filter `SeenFilter` **without** a detail-page fetch (assert the scraper's detail `fetch` is not called for it).
- `dry_run` still writes nothing.

---

## PR3 — `unified_agent` refactor (full cleanup)

### Problem
`integrations/unified_agent.py` is ~1600 lines; `_execute_tool` ([unified_agent.py:687](../../../organist_bot/integrations/unified_agent.py)) is an ~850-line `if name == …` chain over ~28 tools in 6 domains, all sharing four module-level per-chat dicts (`_histories`, `_last_invoice`, `_last_gig_listing`, `_last_application_listing`). Response dispatch peeks at tool *names* (`_VERBATIM_RESPONSE_TOOLS`, `_PDF_RESPONSE_TOOLS`); per-chat state is lost on restart; `max_tokens=1024` truncates multi-tool replies.

### Design
**Structure** — `integrations/agent_tools/` package, one module per domain mirroring the system-prompt sections:
`gig_tools.py`, `client_tools.py`, `invoice_tools.py`, `filter_tools.py`, `analytics_tools.py`, `config_tools.py`.

Each module exposes its tool schemas and registers handlers in a shared registry:

```python
# registry.py
TOOL_REGISTRY: dict[str, Handler] = {}
def register(name): ...                      # decorator
async def dispatch(name, input_data, ctx) -> ToolResult
```

**`ToolContext`** — a dataclass carrying `chat_id` and references to the per-chat state, threaded into every handler instead of reaching for module globals:

```python
@dataclass
class ToolContext:
    chat_id: int
    state: ChatState           # history, last_invoice, last_gig_listing, last_application_listing
```

**Typed results** — handlers return `ToolResult` (`TextResult | PDFResult | VerbatimResult`) so the message loop dispatches on the *type*, deleting the name-set heuristics.

**State persistence** — `ChatState` is backed by `data/agent_state.json` via PR1's atomic store (or an independent atomic write if PR3 lands first), so restarts no longer drop "last invoice"/"last listing" context. Keyed by `chat_id`.

**max_tokens** — raise from 1024 to a value sized for multi-tool replies (e.g. 4096); on `stop_reason != "end_turn"` keep the existing truncation guard but log it.

**Tooling** — update `.claude/hooks/validate_invoice_tools.py` to validate the **registry** (every schema name has a registered handler) instead of scanning for `if name ==` branches in `_execute_tool`, which this refactor removes.

### Constraints
- Behavior-preserving for tool semantics: existing `tests/test_unified_agent.py` must pass (adjusting only import paths/wiring, not asserted behavior).
- `telegram_bot.py` reaches into `unified_agent._make_calendar_client` ([reply_monitor + telegram_bot]) — expose a clean public entry point as part of the move.

### Testing
- Registry completeness test: every schema name resolves to a handler (replaces the old hook's job at the unit level too).
- `ChatState` round-trips through disk; a simulated restart preserves `last_invoice`.
- Typed-result dispatch: a PDF tool returns `PDFResult` and the loop sends the document; a text tool returns `TextResult`.

---

## PR4 — Test additions

Three high-leverage gaps on critical, currently-untested paths:

1. **`reply_monitor._classify_reply`** — mock the `anthropic.Anthropic` client; assert (a) prompt interpolation, (b) an unexpected label normalizes to `"unclear"`, (c) an API exception returns `"unclear"` without raising. This sits on the "accepted booking" flow and is fully mocked-away today.
2. **`main._run` drain path** — pass a mock `SheetsLogger` into `main()`; assert `drain()` is called once per successful run and that a `drain()` exception is caught *and* `alert.send_alert` fires. Currently never exercised.
3. **`logging_config`** — `JSONFormatter` emits the expected keys and excludes `_STDLIB_FIELDS` extras; `RunIdFilter` injects the `run_id` from the `ContextVar`. A regression here silently corrupts every structured log line and breaks the dashboard query.

### Testing
These *are* tests; success = new tests pass and meaningfully fail when the underlying logic is broken (verified by a quick mutation check during implementation).

---

## Out of scope (flagged, not addressed here)
- `Gig.link is None` leaking through `SeenFilter` (model-level correctness).
- Per-chat history pruning / unbounded `SheetsLogger._buffer` growth.
- `reply_monitor` not calling `set_run_id()` (observability correlation gap).

These are recorded for a future pass.
