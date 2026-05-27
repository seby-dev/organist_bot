# Runtime Config + Availability Quick-add Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow `min_fee`, `max_travel_minutes`, and `poll_minutes` to be read and changed via Telegram without restarting the bot; allow natural-language relative date expressions ("today", "this weekend", "next week") in the `manage_unavailable` tool.

**Architecture:** A new `organist_bot/runtime_config_store.py` (following the `filter_store.py` pattern) provides a file-backed JSON override store at `data/runtime_config.json`. `main.py` reads overrides fresh on each tick for filter construction and reschedules the poll job when `poll_minutes` changes. `unified_agent.py` gets a `manage_config` tool and a `_resolve_period` helper that resolves relative date expressions before passing to `filter_store`.

**Tech Stack:** Python 3.13, `json`, `pathlib`, `datetime`, `schedule`, `pytest`, `unittest.mock`.

---

## Status: ALL TASKS COMPLETE

All three implementation tasks and their tests are fully implemented in the codebase.

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
- Test: `tests/test_runtime_config_store.py`

- [x] **Step 1: Write the failing tests**

```python
# tests/test_runtime_config_store.py
import json, pytest
from pathlib import Path
from organist_bot.runtime_config_store import RuntimeConfigStore

def test_get_returns_default_when_file_missing(tmp_path):
    store = RuntimeConfigStore(data_file=tmp_path / "rc.json")
    assert store.get("min_fee", 100) == 100

def test_set_persists_and_get_returns_override(tmp_path):
    store = RuntimeConfigStore(data_file=tmp_path / "rc.json")
    store.set("min_fee", 150)
    assert store.get("min_fee", 100) == 150

def test_reset_removes_key(tmp_path):
    store = RuntimeConfigStore(data_file=tmp_path / "rc.json")
    store.set("min_fee", 150)
    assert store.reset("min_fee") is True
    assert store.get("min_fee", 100) == 100

def test_reset_returns_false_when_key_absent(tmp_path):
    store = RuntimeConfigStore(data_file=tmp_path / "rc.json")
    assert store.reset("min_fee") is False

def test_all_returns_all_overrides(tmp_path):
    store = RuntimeConfigStore(data_file=tmp_path / "rc.json")
    store.set("min_fee", 150)
    store.set("poll_minutes", 5)
    assert store.all() == {"min_fee": 150, "poll_minutes": 5}

def test_all_returns_empty_when_no_overrides(tmp_path):
    store = RuntimeConfigStore(data_file=tmp_path / "rc.json")
    assert store.all() == {}

def test_malformed_json_treated_as_empty(tmp_path):
    f = tmp_path / "rc.json"
    f.write_text("not json")
    store = RuntimeConfigStore(data_file=f)
    assert store.get("min_fee", 100) == 100

def test_keys_are_independent(tmp_path):
    store = RuntimeConfigStore(data_file=tmp_path / "rc.json")
    store.set("min_fee", 150)
    store.set("poll_minutes", 5)
    store.reset("min_fee")
    assert store.get("min_fee", 100) == 100
    assert store.get("poll_minutes", 2) == 5
```

- [x] **Step 2: Run tests to confirm they fail**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_runtime_config_store.py -v
```
Expected: ImportError or FAILED

- [x] **Step 3: Implement `organist_bot/runtime_config_store.py`**

```python
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)
_DEFAULT_PATH = Path("data/runtime_config.json")


class RuntimeConfigStore:
    def __init__(self, data_file: Path = _DEFAULT_PATH) -> None:
        self._path = data_file

    def _read(self) -> dict:
        try:
            return json.loads(self._path.read_text())
        except Exception:
            return {}

    def _write(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, indent=2))

    def get(self, key: str, default: int) -> int:
        return self._read().get(key, default)

    def set(self, key: str, value: int) -> None:
        data = self._read()
        data[key] = value
        self._write(data)

    def reset(self, key: str) -> bool:
        data = self._read()
        if key not in data:
            return False
        del data[key]
        self._write(data)
        return True

    def all(self) -> dict[str, int]:
        return self._read()


runtime_config = RuntimeConfigStore()
```

- [x] **Step 4: Run tests to confirm they pass**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_runtime_config_store.py -v
```
Expected: 8 PASSED

- [x] **Step 5: Commit**

```bash
git add organist_bot/runtime_config_store.py tests/test_runtime_config_store.py
git commit -m "feat: add RuntimeConfigStore for runtime-editable pipeline config"
```

---

### Task 2: Wire `runtime_config` into `main.py`

**Files:**
- Modify: `main.py`

- [x] **Step 1: Add import**

```python
from organist_bot.runtime_config_store import runtime_config
```

- [x] **Step 2: Use `runtime_config` for filter construction in both pre-filter and full-filter passes**

```python
FeeFilter(min_fee=runtime_config.get("min_fee", settings.min_fee))
PostcodeFilter(max_minutes=runtime_config.get("max_travel_minutes", settings.max_travel_minutes))
```

- [x] **Step 3: Add poll-interval reschedule in the `__main__` scheduler block**

```python
current_poll = runtime_config.get("poll_minutes", settings.poll_minutes)
job = schedule.every(current_poll).minutes.do(run, ...)

_tick = 0
while True:
    schedule.run_pending()
    _tick += 1
    if _tick % 10 == 0:
        desired_poll = runtime_config.get("poll_minutes", settings.poll_minutes)
        if desired_poll != current_poll:
            schedule.cancel_job(job)
            job = schedule.every(desired_poll).minutes.do(run, ...)
            current_poll = desired_poll
    time.sleep(1)
```

- [x] **Step 4: Run full test suite**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest --tb=short -q
```
Expected: all passing

- [x] **Step 5: Commit**

```bash
git add main.py
git commit -m "feat: wire runtime_config into main.py filter construction and poll reschedule"
```

---

### Task 3: `manage_config` tool + `_resolve_period` in `unified_agent.py`

**Files:**
- Modify: `organist_bot/integrations/unified_agent.py`
- Test: `tests/test_unified_agent.py`

- [x] **Step 1: Write failing tests for `_resolve_period`**

```python
# In tests/test_unified_agent.py — TestResolvePeriod class
import datetime
from unittest.mock import patch
from organist_bot.integrations.unified_agent import _resolve_period

class TestResolvePeriod:
    def _today(self, year, month, day):
        return datetime.date(year, month, day)

    def test_today(self):
        with patch("organist_bot.integrations.unified_agent.datetime") as m:
            m.date.today.return_value = self._today(2026, 6, 10)
            assert _resolve_period("today") == "2026-06-10"

    def test_tomorrow(self):
        with patch("organist_bot.integrations.unified_agent.datetime") as m:
            m.date.today.return_value = self._today(2026, 6, 10)
            assert _resolve_period("tomorrow") == "2026-06-11"

    def test_this_month(self):
        with patch("organist_bot.integrations.unified_agent.datetime") as m:
            m.date.today.return_value = self._today(2026, 6, 10)
            assert _resolve_period("this month") == "2026-06"

    def test_next_month(self):
        with patch("organist_bot.integrations.unified_agent.datetime") as m:
            m.date.today.return_value = self._today(2026, 6, 10)
            assert _resolve_period("next month") == "2026-07"

    def test_next_week(self):
        with patch("organist_bot.integrations.unified_agent.datetime") as m:
            m.date.today.return_value = self._today(2026, 6, 10)  # Wednesday
            assert _resolve_period("next week") == "2026-06-15:2026-06-21"

    def test_this_weekend_on_weekday(self):
        with patch("organist_bot.integrations.unified_agent.datetime") as m:
            m.date.today.return_value = self._today(2026, 6, 10)  # Wednesday
            assert _resolve_period("this weekend") == "2026-06-13:2026-06-14"

    def test_this_weekend_on_saturday(self):
        with patch("organist_bot.integrations.unified_agent.datetime") as m:
            m.date.today.return_value = self._today(2026, 6, 13)  # Saturday
            assert _resolve_period("this weekend") == "2026-06-13:2026-06-14"

    def test_this_weekend_on_sunday(self):
        with patch("organist_bot.integrations.unified_agent.datetime") as m:
            m.date.today.return_value = self._today(2026, 6, 14)  # Sunday
            assert _resolve_period("this weekend") == "2026-06-14"

    def test_this_weekday_never_today(self):
        with patch("organist_bot.integrations.unified_agent.datetime") as m:
            m.date.today.return_value = self._today(2026, 6, 14)  # Sunday
            # "this sunday" when today is Sunday → next Sunday (7 days)
            assert _resolve_period("this sunday") == "2026-06-21"

    def test_next_weekday(self):
        with patch("organist_bot.integrations.unified_agent.datetime") as m:
            m.date.today.return_value = self._today(2026, 6, 10)  # Wednesday
            assert _resolve_period("next friday") == "2026-06-12"

    def test_passthrough_unknown(self):
        assert _resolve_period("2026-06-01:2026-06-30") == "2026-06-01:2026-06-30"
```

- [x] **Step 2: Write failing tests for `manage_config`**

```python
class TestManageConfig:
    @pytest.mark.asyncio
    async def test_get_shows_all_keys(self):
        with patch("organist_bot.integrations.unified_agent.runtime_config") as m:
            m.get.side_effect = lambda k, d: d
            m.all.return_value = {}
            result = await _execute_tool("manage_config", {"action": "get"}, CHAT_ID)
        assert "min_fee" in result
        assert "max_travel_minutes" in result
        assert "poll_minutes" in result

    @pytest.mark.asyncio
    async def test_set_valid_value(self):
        with patch("organist_bot.integrations.unified_agent.runtime_config") as m:
            result = await _execute_tool(
                "manage_config", {"action": "set", "key": "min_fee", "value": 150}, CHAT_ID
            )
        assert "150" in result
        m.set.assert_called_once_with("min_fee", 150)

    @pytest.mark.asyncio
    async def test_set_out_of_range_returns_error(self):
        result = await _execute_tool(
            "manage_config", {"action": "set", "key": "poll_minutes", "value": 999}, CHAT_ID
        )
        assert "invalid" in result.lower() or "range" in result.lower() or "must be" in result.lower()

    @pytest.mark.asyncio
    async def test_reset_existing_key(self):
        with patch("organist_bot.integrations.unified_agent.runtime_config") as m:
            m.reset.return_value = True
            m.get.return_value = 100
            result = await _execute_tool(
                "manage_config", {"action": "reset", "key": "min_fee"}, CHAT_ID
            )
        assert "reset" in result.lower() or "default" in result.lower()

    @pytest.mark.asyncio
    async def test_reset_absent_key(self):
        with patch("organist_bot.integrations.unified_agent.runtime_config") as m:
            m.reset.return_value = False
            m.get.return_value = 100
            result = await _execute_tool(
                "manage_config", {"action": "reset", "key": "min_fee"}, CHAT_ID
            )
        assert "default" in result.lower() or "already" in result.lower()
```

- [x] **Step 3: Implement `_resolve_period` in `unified_agent.py`**

Private function before `_execute_tool`. Uses only `datetime.date.today()`.

- [x] **Step 4: Wire `_resolve_period` into `manage_unavailable` add handler**

```python
period = _resolve_period(input_data.get("period", ""))
```

- [x] **Step 5: Add `manage_config` tool schema to `TOOLS`**

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
            "action": {"type": "string", "enum": ["get", "set", "reset"]},
            "key": {"type": "string", "enum": ["min_fee", "max_travel_minutes", "poll_minutes"]},
            "value": {"type": "integer"},
        },
        "required": ["action"],
    },
}
```

- [x] **Step 6: Add `manage_config` handler in `_execute_tool`**

Ranges: `min_fee` (0–100000), `max_travel_minutes` (1–300), `poll_minutes` (1–60).

- [x] **Step 7: Add `manage_config` to `_VERBATIM_RESPONSE_TOOLS`**

- [x] **Step 8: Run all new tests**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_unified_agent.py::TestResolvePeriod \
         tests/test_unified_agent.py::TestManageConfig -v
```
Expected: all PASSED

- [x] **Step 9: Run full test suite**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest --tb=short -q
```
Expected: all passing

- [x] **Step 10: Commit**

```bash
git add organist_bot/integrations/unified_agent.py tests/test_unified_agent.py
git commit -m "feat: add manage_config tool and _resolve_period for relative date expressions"
```
