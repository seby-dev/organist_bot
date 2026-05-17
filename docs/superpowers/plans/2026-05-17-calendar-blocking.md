# Calendar Blocking for Unavailable Periods Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically create and delete Google Calendar blocking events whenever unavailable periods are added or removed, and sync existing periods at bot startup.

**Architecture:** Three new methods on `GoogleCalendarClient` (`_parse_period_dates`, `block_period`, `unblock_period`) handle all calendar-blocking I/O. The `manage_unavailable` handler in `unified_agent.py` calls these after every filter-store mutation. A `sync_calendar_blocks` function runs at bot startup to catch pre-existing periods.

**Tech Stack:** Google Calendar API v3 (`privateExtendedProperty` query for tagging blocking events), existing `GoogleCalendarClient`, `python-telegram-bot` startup hook.

---

## File Map

| File | Change |
|------|--------|
| `organist_bot/integrations/calendar_client.py` | Add module-level `_parse_period_dates`; add `block_period` and `unblock_period` methods to `GoogleCalendarClient` |
| `organist_bot/integrations/unified_agent.py` | Add `sync_calendar_blocks` function; add calendar calls to `manage_unavailable` add/remove handlers |
| `organist_bot/integrations/telegram_bot.py` | Call `sync_calendar_blocks` at startup in `run()` |
| `tests/test_calendar_client.py` | Add `TestParsePeriodDates`, `TestBlockPeriod`, `TestUnblockPeriod` |
| `tests/test_unified_agent.py` | Add `TestSyncCalendarBlocks`; add calendar-call assertions to existing `manage_unavailable` tests |

---

## Task 1: `_parse_period_dates` helper

**Files:**
- Modify: `organist_bot/integrations/calendar_client.py`
- Test: `tests/test_calendar_client.py`

- [ ] **Step 1: Write the failing tests**

Add this class at the bottom of `tests/test_calendar_client.py`. Also add `import datetime as dt` (already present) and add `from organist_bot.integrations.calendar_client import _parse_period_dates` to the import block at the top.

```python
# ── _parse_period_dates ───────────────────────────────────────────────────────


class TestParsePeriodDates:
    def test_single_date(self):
        result = _parse_period_dates("2026-12-25")
        assert result == (dt.date(2026, 12, 25), dt.date(2026, 12, 25))

    def test_range(self):
        result = _parse_period_dates("2026-12-01:2026-12-31")
        assert result == (dt.date(2026, 12, 1), dt.date(2026, 12, 31))

    def test_month_december(self):
        result = _parse_period_dates("2026-12")
        assert result == (dt.date(2026, 12, 1), dt.date(2026, 12, 31))

    def test_month_february_non_leap(self):
        result = _parse_period_dates("2026-02")
        assert result == (dt.date(2026, 2, 1), dt.date(2026, 2, 28))

    def test_invalid_returns_none(self):
        assert _parse_period_dates("not-a-date") is None

    def test_empty_returns_none(self):
        assert _parse_period_dates("") is None
```

- [ ] **Step 2: Run to verify they fail**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_calendar_client.py::TestParsePeriodDates -v
```

Expected: `ImportError` or `AttributeError` — `_parse_period_dates` does not exist yet.

- [ ] **Step 3: Implement `_parse_period_dates` in `calendar_client.py`**

Add these two imports near the top of `calendar_client.py` (after `import datetime`):

```python
import calendar as _cal_mod
import re as _re
```

Add this function just before the `GoogleCalendarClient` class definition:

```python
def _parse_period_dates(period: str) -> tuple[datetime.date, datetime.date] | None:
    """Parse a period token into an inclusive (start, end) date pair.

    Accepts: YYYY-MM-DD, YYYY-MM-DD:YYYY-MM-DD, YYYY-MM.
    Returns None on any parse failure.
    """
    try:
        if ":" in period:
            start_str, end_str = period.split(":", 1)
            return datetime.date.fromisoformat(start_str), datetime.date.fromisoformat(end_str)
        if _re.fullmatch(r"\d{4}-\d{2}", period):
            year, month = int(period[:4]), int(period[5:])
            last_day = _cal_mod.monthrange(year, month)[1]
            return datetime.date(year, month, 1), datetime.date(year, month, last_day)
        d = datetime.date.fromisoformat(period)
        return d, d
    except Exception:
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_calendar_client.py::TestParsePeriodDates -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add organist_bot/integrations/calendar_client.py tests/test_calendar_client.py
git commit -m "feat: add _parse_period_dates helper to calendar_client"
```

---

## Task 2: `block_period` method

**Files:**
- Modify: `organist_bot/integrations/calendar_client.py`
- Test: `tests/test_calendar_client.py`

- [ ] **Step 1: Write the failing tests**

Add this class to `tests/test_calendar_client.py`:

```python
# ── block_period ──────────────────────────────────────────────────────────────


class TestBlockPeriod:
    def test_creates_all_day_event_for_single_date(self, client, mock_service):
        mock_service.events().list().execute.return_value = {"items": []}
        mock_service.events().insert().execute.return_value = {"id": "blk_123"}

        result = client.block_period("2026-12-25")

        assert result == "blk_123"
        body = mock_service.events().insert.call_args[1]["body"]
        assert body["summary"] == "Unavailable"
        assert body["start"] == {"date": "2026-12-25"}
        assert body["end"] == {"date": "2026-12-26"}  # exclusive end
        assert body["extendedProperties"]["private"]["organist_bot_block"] == "1"

    def test_creates_event_for_range(self, client, mock_service):
        mock_service.events().list().execute.return_value = {"items": []}
        mock_service.events().insert().execute.return_value = {"id": "blk_456"}

        result = client.block_period("2026-12-01:2026-12-31")

        assert result == "blk_456"
        body = mock_service.events().insert.call_args[1]["body"]
        assert body["start"] == {"date": "2026-12-01"}
        assert body["end"] == {"date": "2027-01-01"}  # exclusive end

    def test_creates_event_for_month(self, client, mock_service):
        mock_service.events().list().execute.return_value = {"items": []}
        mock_service.events().insert().execute.return_value = {"id": "blk_789"}

        result = client.block_period("2026-12")

        assert result == "blk_789"
        body = mock_service.events().insert.call_args[1]["body"]
        assert body["start"] == {"date": "2026-12-01"}
        assert body["end"] == {"date": "2027-01-01"}  # exclusive end

    def test_idempotent_returns_existing_id_without_insert(self, client, mock_service):
        mock_service.events().list().execute.return_value = {"items": [{"id": "existing_blk"}]}

        result = client.block_period("2026-12-25")

        assert result == "existing_blk"
        mock_service.events().insert.assert_not_called()

    def test_returns_none_on_api_error(self, client, mock_service):
        mock_service.events().list().execute.side_effect = Exception("API error")

        result = client.block_period("2026-12-25")

        assert result is None

    def test_returns_none_for_invalid_period(self, client, mock_service):
        result = client.block_period("not-a-period")

        assert result is None
        mock_service.events().list.assert_not_called()
```

- [ ] **Step 2: Run to verify they fail**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_calendar_client.py::TestBlockPeriod -v
```

Expected: `AttributeError: 'GoogleCalendarClient' object has no attribute 'block_period'`

- [ ] **Step 3: Implement `block_period` in `GoogleCalendarClient`**

Add this method to `GoogleCalendarClient` after `add_gig`:

```python
def block_period(self, period: str) -> str | None:
    """Create an all-day 'Unavailable' blocking event for the given period token.

    Idempotent: returns the existing event ID if a block already exists.
    Returns None on parse failure or API error.
    """
    dates = _parse_period_dates(period)
    if dates is None:
        logger.warning("block_period: cannot parse period %r — skipping", period)
        return None
    start, end = dates
    end_exclusive = end + datetime.timedelta(days=1)
    time_min = datetime.datetime.combine(start, datetime.time.min).isoformat() + "Z"
    time_max = datetime.datetime.combine(end_exclusive, datetime.time.min).isoformat() + "Z"
    try:
        existing = (
            self._service.events()
            .list(
                calendarId=self.calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                privateExtendedProperty="organist_bot_block=1",
                singleEvents=True,
            )
            .execute()
        )
        if existing.get("items"):
            return existing["items"][0]["id"]
        event = {
            "summary": "Unavailable",
            "start": {"date": start.isoformat()},
            "end": {"date": end_exclusive.isoformat()},
            "extendedProperties": {"private": {"organist_bot_block": "1"}},
        }
        created = (
            self._service.events().insert(calendarId=self.calendar_id, body=event).execute()
        )
        event_id = created["id"]
        logger.info("Calendar block created", extra={"period": period, "event_id": event_id})
        return event_id
    except Exception:
        logger.warning("block_period: failed for %r", period, exc_info=True)
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_calendar_client.py::TestBlockPeriod -v
```

Expected: 6 passed.

- [ ] **Step 5: Run full calendar test suite to check for regressions**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_calendar_client.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add organist_bot/integrations/calendar_client.py tests/test_calendar_client.py
git commit -m "feat: add block_period to GoogleCalendarClient"
```

---

## Task 3: `unblock_period` method

**Files:**
- Modify: `organist_bot/integrations/calendar_client.py`
- Test: `tests/test_calendar_client.py`

- [ ] **Step 1: Write the failing tests**

Add this class to `tests/test_calendar_client.py`:

```python
# ── unblock_period ────────────────────────────────────────────────────────────


class TestUnblockPeriod:
    def test_deletes_existing_block(self, client, mock_service):
        mock_service.events().list().execute.return_value = {"items": [{"id": "blk_123"}]}
        mock_service.events().delete().execute.return_value = None

        result = client.unblock_period("2026-12-25")

        assert result is True
        mock_service.events().delete.assert_called_once_with(
            calendarId="cal@test.com", eventId="blk_123"
        )

    def test_deletes_multiple_blocks(self, client, mock_service):
        mock_service.events().list().execute.return_value = {
            "items": [{"id": "blk_1"}, {"id": "blk_2"}]
        }
        mock_service.events().delete().execute.return_value = None

        result = client.unblock_period("2026-12")

        assert result is True
        assert mock_service.events().delete.call_count == 2

    def test_returns_false_when_no_blocks_exist(self, client, mock_service):
        mock_service.events().list().execute.return_value = {"items": []}

        result = client.unblock_period("2026-12-25")

        assert result is False
        mock_service.events().delete.assert_not_called()

    def test_returns_false_on_api_error(self, client, mock_service):
        mock_service.events().list().execute.side_effect = Exception("API error")

        result = client.unblock_period("2026-12-25")

        assert result is False

    def test_returns_false_for_invalid_period(self, client, mock_service):
        result = client.unblock_period("not-a-period")

        assert result is False
        mock_service.events().list.assert_not_called()
```

- [ ] **Step 2: Run to verify they fail**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_calendar_client.py::TestUnblockPeriod -v
```

Expected: `AttributeError: 'GoogleCalendarClient' object has no attribute 'unblock_period'`

- [ ] **Step 3: Implement `unblock_period` in `GoogleCalendarClient`**

Add this method to `GoogleCalendarClient` after `block_period`:

```python
def unblock_period(self, period: str) -> bool:
    """Delete all calendar blocking events for the given period token.

    Returns True if any blocks were deleted. Returns False on parse failure,
    no blocks found, or API error.
    """
    dates = _parse_period_dates(period)
    if dates is None:
        logger.warning("unblock_period: cannot parse period %r — skipping", period)
        return False
    start, end = dates
    end_exclusive = end + datetime.timedelta(days=1)
    time_min = datetime.datetime.combine(start, datetime.time.min).isoformat() + "Z"
    time_max = datetime.datetime.combine(end_exclusive, datetime.time.min).isoformat() + "Z"
    try:
        result = (
            self._service.events()
            .list(
                calendarId=self.calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                privateExtendedProperty="organist_bot_block=1",
                singleEvents=True,
            )
            .execute()
        )
        events = result.get("items", [])
        for ev in events:
            self._service.events().delete(
                calendarId=self.calendar_id, eventId=ev["id"]
            ).execute()
        if events:
            logger.info(
                "Calendar blocks removed",
                extra={"period": period, "count": len(events)},
            )
        return bool(events)
    except Exception:
        logger.warning("unblock_period: failed for %r", period, exc_info=True)
        return False
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_calendar_client.py::TestUnblockPeriod -v
```

Expected: 5 passed.

- [ ] **Step 5: Run full calendar test suite**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_calendar_client.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add organist_bot/integrations/calendar_client.py tests/test_calendar_client.py
git commit -m "feat: add unblock_period to GoogleCalendarClient"
```

---

## Task 4: Wire `block_period` into `manage_unavailable` add

**Files:**
- Modify: `organist_bot/integrations/unified_agent.py`
- Test: `tests/test_unified_agent.py`

- [ ] **Step 1: Write the failing tests**

Add these three tests to the `TestFilterTools` class in `tests/test_unified_agent.py`. Also add `from organist_bot.integrations.unified_agent import ... sync_calendar_blocks` to the imports at the top (leave `sync_calendar_blocks` for Task 6 — only add `_make_calendar_client` here if needed, but since we patch it by string path that's not required).

```python
    @pytest.mark.asyncio
    async def test_manage_unavailable_add_blocks_calendar(self):
        mock_cal = MagicMock()
        with (
            patch("organist_bot.integrations.unified_agent.filter_store") as mock_fs,
            patch(
                "organist_bot.integrations.unified_agent._make_calendar_client",
                return_value=mock_cal,
            ),
        ):
            mock_fs.add_period.return_value = True
            await _execute_tool(
                "manage_unavailable", {"action": "add", "period": "2026-12"}, CHAT_ID
            )
        mock_cal.block_period.assert_called_once_with("2026-12")

    @pytest.mark.asyncio
    async def test_manage_unavailable_add_calendar_failure_does_not_raise(self):
        mock_cal = MagicMock()
        mock_cal.block_period.side_effect = Exception("API down")
        with (
            patch("organist_bot.integrations.unified_agent.filter_store") as mock_fs,
            patch(
                "organist_bot.integrations.unified_agent._make_calendar_client",
                return_value=mock_cal,
            ),
        ):
            mock_fs.add_period.return_value = True
            result = await _execute_tool(
                "manage_unavailable", {"action": "add", "period": "2026-12"}, CHAT_ID
            )
        data = json.loads(result)
        assert "result" in data

    @pytest.mark.asyncio
    async def test_manage_unavailable_add_skips_calendar_when_none(self):
        with (
            patch("organist_bot.integrations.unified_agent.filter_store") as mock_fs,
            patch(
                "organist_bot.integrations.unified_agent._make_calendar_client",
                return_value=None,
            ),
        ):
            mock_fs.add_period.return_value = True
            result = await _execute_tool(
                "manage_unavailable", {"action": "add", "period": "2026-12"}, CHAT_ID
            )
        data = json.loads(result)
        assert "result" in data
```

- [ ] **Step 2: Run to verify they fail**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_unified_agent.py::TestFilterTools::test_manage_unavailable_add_blocks_calendar \
         tests/test_unified_agent.py::TestFilterTools::test_manage_unavailable_add_calendar_failure_does_not_raise \
         tests/test_unified_agent.py::TestFilterTools::test_manage_unavailable_add_skips_calendar_when_none -v
```

Expected: all 3 FAIL — `block_period` is never called.

- [ ] **Step 3: Modify the `manage_unavailable` add handler in `unified_agent.py`**

Find this block (around line 739):

```python
        if action == "add":
            added = filter_store.add_period("unavailable_periods", period)
            msg = (
                f"Marked '{period}' as unavailable."
                if added
                else f"'{period}' already in unavailable list."
            )
            return json.dumps({"result": msg})
```

Replace with:

```python
        if action == "add":
            added = filter_store.add_period("unavailable_periods", period)
            msg = (
                f"Marked '{period}' as unavailable."
                if added
                else f"'{period}' already in unavailable list."
            )
            cal = _make_calendar_client()
            if cal:
                try:
                    cal.block_period(period)
                except Exception:
                    logger.warning(
                        "manage_unavailable: failed to block calendar for %r", period, exc_info=True
                    )
            return json.dumps({"result": msg})
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_unified_agent.py::TestFilterTools::test_manage_unavailable_add_blocks_calendar \
         tests/test_unified_agent.py::TestFilterTools::test_manage_unavailable_add_calendar_failure_does_not_raise \
         tests/test_unified_agent.py::TestFilterTools::test_manage_unavailable_add_skips_calendar_when_none -v
```

Expected: 3 passed.

- [ ] **Step 5: Run full unified agent test suite**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_unified_agent.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add organist_bot/integrations/unified_agent.py tests/test_unified_agent.py
git commit -m "feat: block calendar when marking unavailable period"
```

---

## Task 5: Wire `unblock_period` into `manage_unavailable` remove

**Files:**
- Modify: `organist_bot/integrations/unified_agent.py`
- Test: `tests/test_unified_agent.py`

- [ ] **Step 1: Write the failing tests**

Add these two tests to the `TestFilterTools` class in `tests/test_unified_agent.py`:

```python
    @pytest.mark.asyncio
    async def test_manage_unavailable_remove_unblocks_calendar(self):
        mock_cal = MagicMock()
        with (
            patch("organist_bot.integrations.unified_agent.filter_store") as mock_fs,
            patch(
                "organist_bot.integrations.unified_agent._make_calendar_client",
                return_value=mock_cal,
            ),
        ):
            mock_fs.remove_period.return_value = True
            await _execute_tool(
                "manage_unavailable", {"action": "remove", "period": "2026-12"}, CHAT_ID
            )
        mock_cal.unblock_period.assert_called_once_with("2026-12")

    @pytest.mark.asyncio
    async def test_manage_unavailable_remove_calendar_failure_does_not_raise(self):
        mock_cal = MagicMock()
        mock_cal.unblock_period.side_effect = Exception("API down")
        with (
            patch("organist_bot.integrations.unified_agent.filter_store") as mock_fs,
            patch(
                "organist_bot.integrations.unified_agent._make_calendar_client",
                return_value=mock_cal,
            ),
        ):
            mock_fs.remove_period.return_value = True
            result = await _execute_tool(
                "manage_unavailable", {"action": "remove", "period": "2026-12"}, CHAT_ID
            )
        data = json.loads(result)
        assert "result" in data
```

- [ ] **Step 2: Run to verify they fail**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_unified_agent.py::TestFilterTools::test_manage_unavailable_remove_unblocks_calendar \
         tests/test_unified_agent.py::TestFilterTools::test_manage_unavailable_remove_calendar_failure_does_not_raise -v
```

Expected: both FAIL.

- [ ] **Step 3: Modify the `manage_unavailable` remove handler in `unified_agent.py`**

Find this block (just after the `add` block from Task 4):

```python
        if action == "remove":
            removed = filter_store.remove_period("unavailable_periods", period)
            msg = (
                f"Removed '{period}' from unavailable periods."
                if removed
                else f"'{period}' not found."
            )
            return json.dumps({"result": msg})
```

Replace with:

```python
        if action == "remove":
            removed = filter_store.remove_period("unavailable_periods", period)
            msg = (
                f"Removed '{period}' from unavailable periods."
                if removed
                else f"'{period}' not found."
            )
            cal = _make_calendar_client()
            if cal:
                try:
                    cal.unblock_period(period)
                except Exception:
                    logger.warning(
                        "manage_unavailable: failed to unblock calendar for %r", period, exc_info=True
                    )
            return json.dumps({"result": msg})
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_unified_agent.py::TestFilterTools::test_manage_unavailable_remove_unblocks_calendar \
         tests/test_unified_agent.py::TestFilterTools::test_manage_unavailable_remove_calendar_failure_does_not_raise -v
```

Expected: 2 passed.

- [ ] **Step 5: Run full unified agent test suite**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_unified_agent.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add organist_bot/integrations/unified_agent.py tests/test_unified_agent.py
git commit -m "feat: unblock calendar when removing unavailable period"
```

---

## Task 6: `sync_calendar_blocks` + startup call

**Files:**
- Modify: `organist_bot/integrations/unified_agent.py`
- Modify: `organist_bot/integrations/telegram_bot.py`
- Test: `tests/test_unified_agent.py`

- [ ] **Step 1: Write the failing tests**

Add `sync_calendar_blocks` to the import at the top of `tests/test_unified_agent.py`:

```python
from organist_bot.integrations.unified_agent import (
    _execute_tool,
    _last_gig_listing,
    sync_calendar_blocks,
)
```

Add this class at the bottom of `tests/test_unified_agent.py`:

```python
# ── sync_calendar_blocks ──────────────────────────────────────────────────────


class TestSyncCalendarBlocks:
    def test_calls_block_period_for_each_unavailable_period(self):
        mock_cal = MagicMock()
        with patch("organist_bot.integrations.unified_agent.filter_store") as mock_fs:
            mock_fs.unavailable_periods.return_value = ["2026-12", "2027-01-15"]
            sync_calendar_blocks(mock_cal)
        assert mock_cal.block_period.call_count == 2
        mock_cal.block_period.assert_any_call("2026-12")
        mock_cal.block_period.assert_any_call("2027-01-15")

    def test_no_periods_makes_no_calls(self):
        mock_cal = MagicMock()
        with patch("organist_bot.integrations.unified_agent.filter_store") as mock_fs:
            mock_fs.unavailable_periods.return_value = []
            sync_calendar_blocks(mock_cal)
        mock_cal.block_period.assert_not_called()

    def test_api_failure_on_one_period_does_not_abort_others(self):
        mock_cal = MagicMock()
        mock_cal.block_period.side_effect = [Exception("API error"), "evt_ok"]
        with patch("organist_bot.integrations.unified_agent.filter_store") as mock_fs:
            mock_fs.unavailable_periods.return_value = ["2026-12", "2027-01"]
            sync_calendar_blocks(mock_cal)  # must not raise
        assert mock_cal.block_period.call_count == 2
```

- [ ] **Step 2: Run to verify they fail**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_unified_agent.py::TestSyncCalendarBlocks -v
```

Expected: `ImportError` — `sync_calendar_blocks` does not exist yet.

- [ ] **Step 3: Add `sync_calendar_blocks` to `unified_agent.py`**

Add this function near the top of `unified_agent.py`, after the `_make_calendar_client` function (around line 367):

```python
def sync_calendar_blocks(cal: "GoogleCalendarClient") -> None:
    """Create calendar blocks for all current unavailable periods not already blocked.

    Idempotent — safe to call at every startup.
    """
    periods = filter_store.unavailable_periods()
    for period in periods:
        try:
            cal.block_period(period)
        except Exception:
            logger.warning("sync_calendar_blocks: failed for %r", period, exc_info=True)
    logger.info("sync_calendar_blocks: synced %d period(s)", len(periods))
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_unified_agent.py::TestSyncCalendarBlocks -v
```

Expected: 3 passed.

- [ ] **Step 5: Add startup call in `telegram_bot.py`**

Find `run()` in `organist_bot/integrations/telegram_bot.py`:

```python
def run(token: str) -> None:
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, handle_message))

    logger.info("Telegram bot polling", extra={"chat_id": settings.telegram_chat_id})
    app.run_polling()
```

Replace with:

```python
def run(token: str) -> None:
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, handle_message))

    cal = unified_agent._make_calendar_client()
    if cal:
        unified_agent.sync_calendar_blocks(cal)

    logger.info("Telegram bot polling", extra={"chat_id": settings.telegram_chat_id})
    app.run_polling()
```

- [ ] **Step 6: Run full test suite**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest --tb=short -q
```

Expected: all pass, no regressions.

- [ ] **Step 7: Commit**

```bash
git add organist_bot/integrations/unified_agent.py \
        organist_bot/integrations/telegram_bot.py \
        tests/test_unified_agent.py
git commit -m "feat: sync calendar blocks for unavailable periods at startup"
```
