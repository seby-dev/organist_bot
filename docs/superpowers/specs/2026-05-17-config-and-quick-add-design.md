# Runtime Config + Availability Quick-add via Telegram

**Date:** 2026-05-17

## Problem

Two gaps in the current Telegram interface:

1. **No runtime config** — `min_fee`, `max_travel_minutes`, and `poll_minutes` are baked into `.env` and require a restart to change. During an active gigging period you may want to temporarily lower the fee threshold or extend the travel radius without touching files.

2. **Clunky availability dates** — the `manage_unavailable` tool requires dates in `YYYY-MM-DD` format. Expressing "I'm unavailable this weekend" requires looking up and typing two specific dates. The agent should resolve common relative expressions automatically.

## Goals

- Allow `min_fee`, `max_travel_minutes`, and `poll_minutes` to be read and changed via Telegram, taking effect on the next polling tick with no restart.
- Allow relative date expressions ("today", "tomorrow", "this weekend", "next week", etc.) to be understood by the `manage_unavailable` tool.

---

## Design

### 1. `organist_bot/runtime_config_store.py` (new file)

Follows the same pattern as `filter_store.py`: file-backed JSON store, fresh read on every call, module-level singleton.

**File:** `data/runtime_config.json`

**Class:** `RuntimeConfigStore`

| Method | Signature | Description |
|--------|-----------|-------------|
| `get` | `(key: str, default: int) -> int` | Returns the stored override if present, else `default` |
| `set` | `(key: str, value: int) -> None` | Writes override to file |
| `reset` | `(key: str) -> bool` | Removes override key; returns True if it existed |
| `all` | `() -> dict[str, int]` | Returns the full overrides dict (may be empty) |

**Valid keys:** `min_fee`, `max_travel_minutes`, `poll_minutes`

JSON structure:
```json
{
  "min_fee": 150,
  "poll_minutes": 5
}
```
Keys not present in the file fall back to the `.env` default at the call site.

Module-level singleton at bottom of file:
```python
runtime_config = RuntimeConfigStore()
```

---

### 2. Pipeline wiring (`main.py`)

**Filter construction — read runtime config fresh each tick:**

Replace:
```python
FeeFilter(min_fee=settings.min_fee)
PostcodeFilter(max_minutes=settings.max_travel_minutes)
```
With:
```python
FeeFilter(min_fee=runtime_config.get("min_fee", settings.min_fee))
PostcodeFilter(max_minutes=runtime_config.get("max_travel_minutes", settings.max_travel_minutes))
```

Both filter objects are already constructed inside the `run()` function (called each tick), so this change alone is sufficient — no caching to invalidate.

**Poll interval — reschedule if changed:**

After `run()` completes, compare `runtime_config.get("poll_minutes", settings.poll_minutes)` to the current job's interval. If different, cancel the existing scheduled job and re-add it with the new interval. The change takes effect on the next tick.

Implementation sketch (in the scheduler loop):
```python
current_interval = scheduled_job.interval
desired_interval = runtime_config.get("poll_minutes", settings.poll_minutes)
if desired_interval != current_interval:
    schedule.cancel_job(scheduled_job)
    scheduled_job = schedule.every(desired_interval).minutes.do(run, ...)
```

---

### 3. `manage_config` tool (`unified_agent.py`)

**Tool schema** (added to `TOOLS` list):

```python
{
    "name": "manage_config",
    "description": (
        "Read or update runtime configuration. Editable settings: min_fee (int, ≥0), "
        "max_travel_minutes (int, 1–300), poll_minutes (int, 1–60). "
        "Changes take effect on the next polling tick. Use action='reset' to restore the .env default."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["get", "set", "reset"],
                "description": "get=show current values, set=update a value, reset=restore .env default",
            },
            "key": {
                "type": "string",
                "enum": ["min_fee", "max_travel_minutes", "poll_minutes"],
                "description": "Required for set and reset.",
            },
            "value": {
                "type": "integer",
                "description": "New value. Required for set.",
            },
        },
        "required": ["action"],
    },
}
```

**`_execute_tool` branch** for `manage_config`:

- `action == "get"`: reads all three keys via `runtime_config.get(key, settings.<key>)`, marks each as "(override)" or "(default)", returns formatted text:
  ```
  min_fee:            150  (override, default: 100)
  max_travel_minutes:  45  (default)
  poll_minutes:         5  (override, default: 2)
  ```

- `action == "set"`: validates `key` is present, `value` is in range (min_fee ≥ 0, max_travel_minutes 1–300, poll_minutes 1–60). Calls `runtime_config.set(key, value)`. Returns confirmation: `"min_fee set to 150. Takes effect on the next polling tick."`.

- `action == "reset"`: calls `runtime_config.reset(key)`. Returns `"min_fee reset to default (100)."` or `"min_fee was already using the default."`.

Validation failures return a plain-English error message rather than raising.

---

### 4. Relative date resolution for `manage_unavailable` (`unified_agent.py`)

**New private function `_resolve_period(text: str) -> str`:**

Called inside the `manage_unavailable` add handler before the period is passed to `filter_store.add_period()`. Normalises `text` to lowercase and strips whitespace, then resolves:

| Input pattern | Output |
|---|---|
| `today` | `YYYY-MM-DD` (today) |
| `tomorrow` | `YYYY-MM-DD` (tomorrow) |
| `this <weekday>` / `next <weekday>` | nearest future occurrence of that weekday as `YYYY-MM-DD` |
| `this weekend` / `next weekend` | `YYYY-MM-DD:YYYY-MM-DD` (Sat:Sun of the nearest future weekend) |
| `next week` | `YYYY-MM-DD:YYYY-MM-DD` (Mon:Sun of next calendar week) |
| `this month` | `YYYY-MM` (current month) |
| `next month` | `YYYY-MM` (next month) |
| anything else | returned unchanged (falls through to existing period validation) |

Weekdays: Monday, Tuesday, Wednesday, Thursday, Friday, Saturday, Sunday (case-insensitive).

"This weekend" means the coming Saturday and Sunday. If today is Saturday, "this weekend" means today and tomorrow. If today is Sunday, "this weekend" means today.

"This `<weekday>`" and "next `<weekday>`" both mean the nearest future occurrence of that day (never today itself — if today is Sunday and the input is "this Sunday", it resolves to the *next* Sunday, 7 days away).

Uses only `datetime.date.today()` — no third-party date libraries.

**Tool description update:** The `manage_unavailable` tool's `period` field description is updated to include: `"Also accepts: today, tomorrow, this/next <weekday>, this weekend, next week, this/next month."`

---

## Error Handling

- `RuntimeConfigStore` — if `data/runtime_config.json` is missing or malformed, treats it as empty (same pattern as `FilterStore`).
- `manage_config set` — out-of-range values return a clear error without writing.
- `_resolve_period` — unrecognised expressions pass through unchanged; existing downstream validation handles rejection.
- Poll interval reschedule — if `schedule.cancel_job` fails (job already gone), log at WARNING and re-add unconditionally.

---

## Files Changed

| File | Change |
|------|--------|
| `organist_bot/runtime_config_store.py` | New file — `RuntimeConfigStore` class + `runtime_config` singleton |
| `main.py` | Import `runtime_config`; read overrides for `min_fee`, `max_travel_minutes`, `poll_minutes`; reschedule logic |
| `organist_bot/integrations/unified_agent.py` | Add `manage_config` tool schema + `_execute_tool` branch; add `_resolve_period`; update `manage_unavailable` tool description and add handler |
| `tests/test_runtime_config_store.py` | Tests for `RuntimeConfigStore` (get/set/reset/all, missing file, malformed JSON) |
| `tests/test_unified_agent.py` | Tests for `manage_config` (get/set/reset, validation); tests for `_resolve_period` (all patterns) |

---

## Out of Scope

- Editing credentials, API keys, or file paths at runtime (security risk, restart required anyway).
- Filter toggles (`enable_fee_filter` etc.) — these are deployment-time decisions, not runtime ones.
- Persisting `.env` overrides back to disk — the runtime config store is the authoritative override layer.
- `/unavail` slash command (Option B) — natural language via the agent is sufficient.
