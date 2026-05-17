# Runtime Config + Availability Quick-add Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow `min_fee`, `max_travel_minutes`, and `poll_minutes` to be read and changed via Telegram without restarting the bot; allow natural-language relative date expressions ("today", "this weekend", "next week") in the `manage_unavailable` tool.

**Architecture:** A new `organist_bot/runtime_config_store.py` (following the `filter_store.py` pattern) provides a file-backed JSON override store. `main.py` reads overrides fresh on each tick for filter construction, and reschedules the poll job when `poll_minutes` changes. `unified_agent.py` gets a `manage_config` tool and a `_resolve_period` helper that resolves relative date expressions before passing to `filter_store`.

**Tech Stack:** `json`, `pathlib`, `datetime`, `schedule`, `unittest.mock`.

---

## File Structure

| File | Change |
|------|--------|
| `organist_bot/runtime_config_store.py` | New — `RuntimeConfigStore` class + `runtime_config` singleton |
| `main.py` | Import `runtime_config`; read overrides for filter construction; poll-interval reschedule loop |
| `organist_bot/integrations/unified_agent.py` | Add `manage_config` tool + `_execute_tool` branch; add `_resolve_period`; update `manage_unavailable` add handler; update `_VERBATIM_RESPONSE_TOOLS` and `SYSTEM_PROMPT` |
| `tests/test_runtime_config_store.py` | New — full test suite for `RuntimeConfigStore` |
| `tests/test_unified_agent.py` | Add `TestManageConfig` and `TestResolvePeriod` classes |

---

### Task 1: `organist_bot/runtime_config_store.py`

**Files:**
- Create: `organist_bot/runtime_config_store.py`
- Create: `tests/test_runtime_config_store.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_runtime_config_store.py`:

```python
# tests/test_runtime_config_store.py
"""Tests for RuntimeConfigStore."""

import json
import pytest


class TestRuntimeConfigStore:
    def test_get_returns_default_when_key_absent(self, tmp_path, monkeypatch):
        """get() returns the supplied default when the key has no override."""
        from organist_bot.runtime_config_store import RuntimeConfigStore

        monkeypatch.chdir(tmp_path)
        store = RuntimeConfigStore()
        assert store.get("min_fee", 100) == 100

    def test_get_returns_override_when_set(self, tmp_path, monkeypatch):
        """get() returns the stored override value."""
        from organist_bot.runtime_config_store import RuntimeConfigStore

        monkeypatch.chdir(tmp_path)
        store = RuntimeConfigStore()
        store.set("min_fee", 150)
        assert store.get("min_fee", 100) == 150

    def test_set_persists_to_file(self, tmp_path, monkeypatch):
        """set() writes the value to data/runtime_config.json."""
        from organist_bot.runtime_config_store import RuntimeConfigStore

        monkeypatch.chdir(tmp_path)
        store = RuntimeConfigStore()
        store.set("poll_minutes", 5)

        raw = json.loads((tmp_path / "data" / "runtime_config.json").read_text())
        assert raw["poll_minutes"] == 5

    def test_reset_removes_override(self, tmp_path, monkeypatch):
        """reset() removes the key and get() returns default again."""
        from organist_bot.runtime_config_store import RuntimeConfigStore

        monkeypatch.chdir(tmp_path)
        store = RuntimeConfigStore()
        store.set("max_travel_minutes", 60)
        assert store.reset("max_travel_minutes") is True
        assert store.get("max_travel_minutes", 45) == 45

    def test_reset_returns_false_when_key_absent(self, tmp_path, monkeypatch):
        """reset() returns False when the key was not set."""
        from organist_bot.runtime_config_store import RuntimeConfigStore

        monkeypatch.chdir(tmp_path)
        store = RuntimeConfigStore()
        assert store.reset("min_fee") is False

    def test_all_returns_current_overrides(self, tmp_path, monkeypatch):
        """all() returns a dict of all current overrides."""
        from organist_bot.runtime_config_store import RuntimeConfigStore

        monkeypatch.chdir(tmp_path)
        store = RuntimeConfigStore()
        store.set("min_fee", 120)
        store.set("poll_minutes", 3)
        result = store.all()
        assert result == {"min_fee": 120, "poll_minutes": 3}

    def test_missing_file_treated_as_empty(self, tmp_path, monkeypatch):
        """A missing data/runtime_config.json is treated as no overrides."""
        from organist_bot.runtime_config_store import RuntimeConfigStore

        monkeypatch.chdir(tmp_path)
        store = RuntimeConfigStore()
        assert store.all() == {}

    def test_malformed_json_treated_as_empty(self, tmp_path, monkeypatch):
        """Malformed JSON in the config file is handled gracefully."""
        from organist_bot.runtime_config_store import RuntimeConfigStore

        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "runtime_config.json").write_text("not json")
        store = RuntimeConfigStore()
        assert store.get("min_fee", 100) == 100

    def test_multiple_keys_are_independent(self, tmp_path, monkeypatch):
        """Setting one key does not affect other keys."""
        from organist_bot.runtime_config_store import RuntimeConfigStore

        monkeypatch.chdir(tmp_path)
        store = RuntimeConfigStore()
        store.set("min_fee", 200)
        store.set("max_travel_minutes", 60)
        store.reset("min_fee")
        assert store.get("min_fee", 100) == 100
        assert store.get("max_travel_minutes", 45) == 60
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_runtime_config_store.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'organist_bot.runtime_config_store'`

- [ ] **Step 3: Implement `organist_bot/runtime_config_store.py`**

Create `organist_bot/runtime_config_store.py`:

```python
"""organist_bot/runtime_config_store.py
────────────────────────────────────────
File-backed store for runtime pipeline configuration overrides.

The store lives at data/runtime_config.json and is read fresh on every call —
so the Telegram bot can mutate it and main.py picks up the changes on the very
next polling tick without a restart.

Three keys are supported: min_fee (int), max_travel_minutes (int), poll_minutes (int).
Keys not present in the file fall back to the .env default at the call site.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_PATH = Path("data/runtime_config.json")


def _read() -> dict[str, int]:
    if not _PATH.exists():
        return {}
    try:
        return dict(json.loads(_PATH.read_text()))
    except Exception:
        logger.exception(
            "runtime_config_store: failed to read %s — using empty config", _PATH
        )
        return {}


def _write(data: dict[str, int]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(data, indent=2) + "\n")


class RuntimeConfigStore:
    """File-backed store for runtime pipeline config overrides."""

    def get(self, key: str, default: int) -> int:
        """Return the stored override for key, or default if not set."""
        return _read().get(key, default)

    def set(self, key: str, value: int) -> None:
        """Write an override value for key."""
        data = _read()
        data[key] = value
        _write(data)

    def reset(self, key: str) -> bool:
        """Remove the override for key. Returns True if the key existed."""
        data = _read()
        if key not in data:
            return False
        del data[key]
        _write(data)
        return True

    def all(self) -> dict[str, int]:
        """Return all current overrides."""
        return _read()


runtime_config = RuntimeConfigStore()
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_runtime_config_store.py -v
```

Expected: 9 PASSED

- [ ] **Step 5: Commit**

```bash
git add organist_bot/runtime_config_store.py tests/test_runtime_config_store.py
git commit -m "feat: add RuntimeConfigStore for runtime-editable pipeline config"
```

---

### Task 2: Wire `runtime_config` into `main.py`

**Files:**
- Modify: `main.py`

- [ ] **Step 1: Add `runtime_config` import to `main.py`**

Add alongside the other `organist_bot` imports (after `import organist_bot.filter_store as filter_store`):

```python
from organist_bot.runtime_config_store import runtime_config
```

- [ ] **Step 2: Use `runtime_config` for filter construction in `main()`**

In `main()`, Phase 1 pre-filter construction (around line 98), change:

```python
# OLD:
        pre_filter.add(FeeFilter(min_fee=settings.min_fee))
# NEW:
        pre_filter.add(FeeFilter(min_fee=runtime_config.get("min_fee", settings.min_fee)))
```

In Phase 2 filter construction (around line 189), change:

```python
# OLD:
        filter_chain.add(FeeFilter(min_fee=settings.min_fee))
# NEW:
        filter_chain.add(FeeFilter(min_fee=runtime_config.get("min_fee", settings.min_fee)))
```

And change the PostcodeFilter construction (around line 214):

```python
# OLD:
        filter_chain.add(
            PostcodeFilter(
                home_postcode=settings.home_postcode,
                api_key=settings.google_maps_api_key,
                max_minutes=settings.max_travel_minutes,
            )
        )
# NEW:
        filter_chain.add(
            PostcodeFilter(
                home_postcode=settings.home_postcode,
                api_key=settings.google_maps_api_key,
                max_minutes=runtime_config.get(
                    "max_travel_minutes", settings.max_travel_minutes
                ),
            )
        )
```

- [ ] **Step 3: Add poll-interval reschedule in the `__main__` block**

In the `__main__` block, replace the current scheduler setup and loop:

```python
# OLD:
    scraper = Scraper()
    try:
        main(scraper, sheets_logger)
        schedule.every(settings.poll_minutes).minutes.do(main, scraper, sheets_logger)

        while True:
            try:
                schedule.run_pending()
            except Exception:
                logger.exception("Unhandled exception in scheduled run")
                alert.send_alert("❌ OrganistBot crashed — check logs.")
            time.sleep(1)
    finally:
        scraper.session.close()
        logger.info("Scraper session closed — bot shutting down")
```

```python
# NEW:
    scraper = Scraper()
    try:
        main(scraper, sheets_logger)
        current_poll = runtime_config.get("poll_minutes", settings.poll_minutes)
        job = schedule.every(current_poll).minutes.do(main, scraper, sheets_logger)

        while True:
            try:
                schedule.run_pending()
            except Exception:
                logger.exception("Unhandled exception in scheduled run")
                alert.send_alert("❌ OrganistBot crashed — check logs.")

            desired_poll = runtime_config.get("poll_minutes", settings.poll_minutes)
            if desired_poll != current_poll:
                schedule.cancel_job(job)
                job = schedule.every(desired_poll).minutes.do(main, scraper, sheets_logger)
                current_poll = desired_poll
                logger.info(
                    "Poll interval updated",
                    extra={"poll_minutes": desired_poll},
                )

            time.sleep(1)
    finally:
        scraper.session.close()
        logger.info("Scraper session closed — bot shutting down")
```

- [ ] **Step 4: Run full test suite**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest --tb=short -q
```

Expected: all pass (no behavioural change when no overrides are set)

- [ ] **Step 5: Commit**

```bash
git add main.py
git commit -m "feat: wire runtime_config into main.py filter construction and poll reschedule"
```

---

### Task 3: `manage_config` tool + `_resolve_period` in `unified_agent.py`

**Files:**
- Modify: `organist_bot/integrations/unified_agent.py`
- Modify: `tests/test_unified_agent.py`

- [ ] **Step 1: Write the failing tests**

Add these two classes to `tests/test_unified_agent.py`:

```python
# ── _resolve_period ───────────────────────────────────────────────────────────


class TestResolvePeriod:
    def test_today(self):
        import datetime
        from organist_bot.integrations.unified_agent import _resolve_period

        today = datetime.date.today().isoformat()
        assert _resolve_period("today") == today
        assert _resolve_period("Today") == today

    def test_tomorrow(self):
        import datetime
        from organist_bot.integrations.unified_agent import _resolve_period

        tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        assert _resolve_period("tomorrow") == tomorrow

    def test_this_month(self):
        import datetime
        from organist_bot.integrations.unified_agent import _resolve_period

        expected = datetime.date.today().strftime("%Y-%m")
        assert _resolve_period("this month") == expected

    def test_next_month(self):
        import datetime
        from organist_bot.integrations.unified_agent import _resolve_period

        today = datetime.date.today()
        if today.month == 12:
            expected = f"{today.year + 1}-01"
        else:
            expected = f"{today.year}-{today.month + 1:02d}"
        assert _resolve_period("next month") == expected

    def test_next_week_is_monday_to_sunday(self):
        import datetime
        from organist_bot.integrations.unified_agent import _resolve_period

        result = _resolve_period("next week")
        assert ":" in result
        start_str, end_str = result.split(":")
        start = datetime.date.fromisoformat(start_str)
        end = datetime.date.fromisoformat(end_str)
        assert start.weekday() == 0  # Monday
        assert end.weekday() == 6    # Sunday
        assert (end - start).days == 6

    def test_this_weekend_is_sat_and_sun(self):
        import datetime
        from organist_bot.integrations.unified_agent import _resolve_period

        result = _resolve_period("this weekend")
        today = datetime.date.today()
        # If today is Sunday, just today is returned
        if today.weekday() == 6:
            assert result == today.isoformat()
        elif today.weekday() == 5:
            assert result == f"{today.isoformat()}:{(today + datetime.timedelta(days=1)).isoformat()}"
        else:
            assert ":" in result
            start, end = result.split(":")
            start_d = datetime.date.fromisoformat(start)
            end_d = datetime.date.fromisoformat(end)
            assert start_d.weekday() == 5   # Saturday
            assert end_d.weekday() == 6     # Sunday

    def test_this_weekday(self):
        import datetime
        from organist_bot.integrations.unified_agent import _resolve_period

        result = _resolve_period("this Sunday")
        d = datetime.date.fromisoformat(result)
        assert d.weekday() == 6       # Sunday
        assert d > datetime.date.today()  # always in the future

    def test_next_weekday(self):
        import datetime
        from organist_bot.integrations.unified_agent import _resolve_period

        result = _resolve_period("next Monday")
        d = datetime.date.fromisoformat(result)
        assert d.weekday() == 0  # Monday

    def test_unknown_expression_passthrough(self):
        from organist_bot.integrations.unified_agent import _resolve_period

        assert _resolve_period("2026-12-25") == "2026-12-25"
        assert _resolve_period("gibberish") == "gibberish"
        assert _resolve_period("2026-12") == "2026-12"


# ── manage_config ─────────────────────────────────────────────────────────────


class TestManageConfig:
    @pytest.mark.asyncio
    async def test_get_shows_all_three_keys(self):
        """get action returns all three config keys."""
        from unittest.mock import MagicMock, patch

        mock_store = MagicMock()
        mock_store.all.return_value = {"min_fee": 150}
        with patch(
            "organist_bot.integrations.unified_agent.runtime_config", mock_store
        ):
            result = await _execute_tool("manage_config", {"action": "get"}, CHAT_ID)
        data = json.loads(result)
        assert "result" in data
        assert "min_fee" in data["result"]
        assert "max_travel_minutes" in data["result"]
        assert "poll_minutes" in data["result"]

    @pytest.mark.asyncio
    async def test_set_valid_value(self):
        """set action with a valid value calls runtime_config.set."""
        mock_store = MagicMock()
        with patch(
            "organist_bot.integrations.unified_agent.runtime_config", mock_store
        ):
            result = await _execute_tool(
                "manage_config", {"action": "set", "key": "min_fee", "value": 150}, CHAT_ID
            )
        mock_store.set.assert_called_once_with("min_fee", 150)
        data = json.loads(result)
        assert "result" in data
        assert "150" in data["result"]

    @pytest.mark.asyncio
    async def test_set_invalid_range_returns_error(self):
        """set action with out-of-range value returns an error without writing."""
        mock_store = MagicMock()
        with patch(
            "organist_bot.integrations.unified_agent.runtime_config", mock_store
        ):
            result = await _execute_tool(
                "manage_config",
                {"action": "set", "key": "poll_minutes", "value": 999},
                CHAT_ID,
            )
        mock_store.set.assert_not_called()
        data = json.loads(result)
        assert "error" in data or (
            "result" in data and ("invalid" in data["result"].lower() or "range" in data["result"].lower())
        )

    @pytest.mark.asyncio
    async def test_reset_calls_store_reset(self):
        """reset action calls runtime_config.reset with the correct key."""
        mock_store = MagicMock()
        mock_store.reset.return_value = True
        with patch(
            "organist_bot.integrations.unified_agent.runtime_config", mock_store
        ):
            result = await _execute_tool(
                "manage_config", {"action": "reset", "key": "min_fee"}, CHAT_ID
            )
        mock_store.reset.assert_called_once_with("min_fee")
        data = json.loads(result)
        assert "result" in data

    @pytest.mark.asyncio
    async def test_reset_not_set_returns_message(self):
        """reset on a key that has no override returns a suitable message."""
        mock_store = MagicMock()
        mock_store.reset.return_value = False
        with patch(
            "organist_bot.integrations.unified_agent.runtime_config", mock_store
        ):
            result = await _execute_tool(
                "manage_config", {"action": "reset", "key": "min_fee"}, CHAT_ID
            )
        data = json.loads(result)
        assert "result" in data
        assert "default" in data["result"].lower()
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_unified_agent.py::TestResolvePeriod \
         tests/test_unified_agent.py::TestManageConfig -v
```

Expected: FAIL — `_resolve_period` not importable, `manage_config` not implemented

- [ ] **Step 3: Add `_resolve_period` to `unified_agent.py`**

Add this function immediately before `_execute_tool` (after `_make_sheets_logger` if already added, otherwise after `sync_calendar_blocks`):

```python
def _resolve_period(text: str) -> str:
    """Resolve relative date expressions to period token format.

    Handles: today, tomorrow, this/next month, next week, this weekend,
    this/next <weekday>. Unrecognised text is returned unchanged.
    """
    import datetime as _dt

    t = text.strip().lower()
    today = _dt.date.today()

    if t == "today":
        return today.isoformat()

    if t == "tomorrow":
        return (today + _dt.timedelta(days=1)).isoformat()

    if t in ("this month", "this-month"):
        return today.strftime("%Y-%m")

    if t in ("next month", "next-month"):
        if today.month == 12:
            return f"{today.year + 1}-01"
        return f"{today.year}-{today.month + 1:02d}"

    if t in ("next week", "next-week"):
        days_until_monday = (7 - today.weekday()) % 7
        if days_until_monday == 0:
            days_until_monday = 7
        next_mon = today + _dt.timedelta(days=days_until_monday)
        next_sun = next_mon + _dt.timedelta(days=6)
        return f"{next_mon.isoformat()}:{next_sun.isoformat()}"

    if t in ("this weekend", "this-weekend", "next weekend", "next-weekend"):
        is_next = "next" in t
        if not is_next and today.weekday() == 6:  # Sunday
            return today.isoformat()
        if not is_next and today.weekday() == 5:  # Saturday
            return f"{today.isoformat()}:{(today + _dt.timedelta(days=1)).isoformat()}"
        days_until_sat = (5 - today.weekday()) % 7
        if days_until_sat == 0 or is_next:
            days_until_sat = (5 - today.weekday()) % 7 + 7
        sat = today + _dt.timedelta(days=days_until_sat)
        sun = sat + _dt.timedelta(days=1)
        return f"{sat.isoformat()}:{sun.isoformat()}"

    _WEEKDAYS = {
        "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
        "friday": 4, "saturday": 5, "sunday": 6,
    }
    for prefix in ("this ", "next "):
        if t.startswith(prefix):
            day_name = t[len(prefix):]
            if day_name in _WEEKDAYS:
                target = _WEEKDAYS[day_name]
                days_ahead = (target - today.weekday()) % 7
                if days_ahead == 0:
                    days_ahead = 7
                return (today + _dt.timedelta(days=days_ahead)).isoformat()

    return text
```

- [ ] **Step 4: Wire `_resolve_period` into the `manage_unavailable` add handler**

In `_execute_tool`, find the `manage_unavailable` add handler (around line 752):

```python
# OLD:
        period = input_data.get("period", "")
        if action == "add":
            added = filter_store.add_period("unavailable_periods", period)
```

```python
# NEW:
        period = _resolve_period(input_data.get("period", ""))
        if action == "add":
            added = filter_store.add_period("unavailable_periods", period)
```

Also update the tool schema for `manage_unavailable`. Find the `period` field description in the TOOLS list (around line 313) and update it to mention relative expressions:

```python
# Find and update the period description in the manage_unavailable tool schema:
"period": {
    "type": "string",
    "description": (
        "Period token: YYYY-MM-DD, YYYY-MM-DD:YYYY-MM-DD, or YYYY-MM. "
        "Also accepts: today, tomorrow, this/next <weekday>, "
        "this weekend, next week, this/next month."
    ),
},
```

- [ ] **Step 5: Add `runtime_config` import to `unified_agent.py`**

Add after the existing imports (after `from organist_bot import filter_store`):

```python
from organist_bot.runtime_config_store import runtime_config
```

- [ ] **Step 6: Add the `manage_config` tool schema to `TOOLS`**

Append to the `TOOLS` list (before the closing `]`):

```python
    # ── Runtime config ──────────────────────────────────────────────────────
    {
        "name": "manage_config",
        "description": (
            "Read or update runtime pipeline configuration. "
            "Editable keys: min_fee (int, ≥0), max_travel_minutes (int, 1–300), "
            "poll_minutes (int, 1–60). Changes take effect on the next polling tick. "
            "Use action='reset' to restore the .env default for a key."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["get", "set", "reset"],
                    "description": (
                        "get=show all values, set=update one value, "
                        "reset=restore .env default for one key"
                    ),
                },
                "key": {
                    "type": "string",
                    "enum": ["min_fee", "max_travel_minutes", "poll_minutes"],
                    "description": "Required for set and reset actions.",
                },
                "value": {
                    "type": "integer",
                    "description": "New value. Required for set.",
                },
            },
            "required": ["action"],
        },
    },
```

- [ ] **Step 7: Add the `manage_config` branch in `_execute_tool`**

Add immediately before the final `return json.dumps({"error": f"Tool not implemented: {name}"})` line:

```python
    # ── manage_config ────────────────────────────────────────────────────────
    if name == "manage_config":
        action = input_data["action"]

        _RANGES: dict[str, tuple[int, int]] = {
            "min_fee": (0, 100_000),
            "max_travel_minutes": (1, 300),
            "poll_minutes": (1, 60),
        }
        _DEFAULTS = {
            "min_fee": settings.min_fee,
            "max_travel_minutes": settings.max_travel_minutes,
            "poll_minutes": settings.poll_minutes,
        }

        if action == "get":
            overrides = runtime_config.all()
            lines = []
            for key, default in _DEFAULTS.items():
                if key in overrides:
                    lines.append(
                        f"{key:<20} {overrides[key]}  (override, default: {default})"
                    )
                else:
                    lines.append(f"{key:<20} {default}  (default)")
            return json.dumps({"result": "\n".join(lines)})

        if action == "set":
            key = input_data.get("key", "")
            value = input_data.get("value")
            if key not in _RANGES:
                return json.dumps(
                    {"result": f"Unknown key '{key}'. Valid keys: {', '.join(_RANGES)}."}
                )
            if value is None:
                return json.dumps({"result": "value is required for set."})
            lo, hi = _RANGES[key]
            if not (lo <= int(value) <= hi):
                return json.dumps(
                    {"result": f"Invalid value {value} for {key}. Must be between {lo} and {hi}."}
                )
            runtime_config.set(key, int(value))
            return json.dumps(
                {"result": f"{key} set to {value}. Takes effect on the next polling tick."}
            )

        if action == "reset":
            key = input_data.get("key", "")
            if key not in _DEFAULTS:
                return json.dumps(
                    {"result": f"Unknown key '{key}'. Valid keys: {', '.join(_DEFAULTS)}."}
                )
            existed = runtime_config.reset(key)
            if existed:
                return json.dumps(
                    {"result": f"{key} reset to default ({_DEFAULTS[key]})."}
                )
            return json.dumps(
                {"result": f"{key} was already using the default ({_DEFAULTS[key]})."}
            )

        return json.dumps({"error": f"Unknown action: {action}"})
```

- [ ] **Step 8: Add `manage_config` to `_VERBATIM_RESPONSE_TOOLS`**

Update the existing line (or add `manage_config` to the set built in the previous gig-stats task):

```python
_VERBATIM_RESPONSE_TOOLS = {"list_upcoming_gigs", "get_gig_stats", "manage_config"}
```

- [ ] **Step 9: Update `SYSTEM_PROMPT` for new capabilities**

Add a new section after the existing `## Filter management` section:

```python
## Runtime config
- "What's the current config?" / "show config" → manage_config(action=get).
- "Set min fee to 150" → manage_config(action=set, key=min_fee, value=150).
- "Reset min fee to default" → manage_config(action=reset, key=min_fee).
- Editable keys: min_fee, max_travel_minutes, poll_minutes.
```

Also update the Filter management section — find the line:
```
- Period formats: YYYY-MM-DD, YYYY-MM-DD:YYYY-MM-DD, YYYY-MM.
```
And update it to:
```
- Period formats: YYYY-MM-DD, YYYY-MM-DD:YYYY-MM-DD, YYYY-MM. Also: today, tomorrow, this/next <weekday>, this weekend, next week, this/next month.
```

- [ ] **Step 10: Run the new tests to confirm they pass**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_unified_agent.py::TestResolvePeriod \
         tests/test_unified_agent.py::TestManageConfig -v
```

Expected: all PASSED

- [ ] **Step 11: Run full test suite**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest --tb=short -q
```

Expected: all pass

- [ ] **Step 12: Commit**

```bash
git add organist_bot/integrations/unified_agent.py tests/test_unified_agent.py
git commit -m "feat: add manage_config tool and _resolve_period for relative date expressions"
```
