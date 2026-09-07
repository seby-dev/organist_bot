# Gig Calendar Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `/gigs` and `/deletegig` Telegram commands, auto-add confirmed gig dates to unavailable periods, and auto-purge past unavailable periods on every read/write.

**Architecture:** Four focused changes across four existing files — no new modules. `filter_store.py` gets a `purge_past_periods()` function called by every unavailable operation. `calendar_client.py` gets `list_upcoming_events` and `delete_event`. `telegram_bot.py` gets two new command handlers with a module-level listing cache. `gig_agent.py` gets auto-add-to-unavailable after confirmed calendar write.

**Tech Stack:** python-telegram-bot, Google Calendar API v3, pytest (asyncio_mode=auto), existing `filter_store` read/write pattern.

---

## File Map

| File | Change |
|---|---|
| `organist_bot/filter_store.py` | Add `purge_past_periods()`; call it from `unavailable_periods()`, `add_period` (unavailable key), `remove_period` (unavailable key) |
| `organist_bot/integrations/calendar_client.py` | Add `list_upcoming_events(max_results=10)` and `delete_event(event_id)` |
| `organist_bot/integrations/telegram_bot.py` | Add `_gig_listing` cache, `_make_calendar_client()` helper, `cmd_gigs`, `cmd_deletegig`; update `_HELP`; register handlers |
| `organist_bot/integrations/gig_agent.py` | After confirmed=true success, add date to unavailable via `filter_store.add_period` |
| `tests/test_filter_store.py` | New file — tests for `purge_past_periods` and the auto-purge side effects |
| `tests/test_calendar_client.py` | Extend — tests for `list_upcoming_events` and `delete_event` |
| `tests/test_gig_agent.py` | Extend — test that confirmed=true triggers `filter_store.add_period` |
| `tests/test_telegram_integration.py` | Extend — tests for `cmd_gigs` and `cmd_deletegig` |

---

## Task 1: `filter_store.py` — `purge_past_periods`

**Files:**
- Modify: `organist_bot/filter_store.py`
- Create: `tests/test_filter_store.py`

### Background

`_PATH = Path("data/filter_config.json")` is relative, so tests must `monkeypatch.chdir(tmp_path)` to redirect file I/O — the same pattern used elsewhere in the test suite. `purge_past_periods()` is standalone: it does its own `_read()` → filter → `_write()` cycle. It is called by the three unavailable-touching functions before their own logic.

Period token end-date rules (replicated from `_parse_periods` in `filters.py`):
- Single day `2026-12-25` → end = `2026-12-25`
- Range `2026-12-15:2027-01-05` → end = `2027-01-05`
- Month `2026-12` → end = last day of that month
- Unparseable → left untouched (fail-safe)

- [ ] **Step 1: Write failing tests**

Create `tests/test_filter_store.py`:

```python
"""Tests for filter_store, focusing on purge_past_periods and auto-purge integration."""

import datetime
import json

import pytest

import organist_bot.filter_store as fs


@pytest.fixture(autouse=True)
def use_tmp_dir(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)


def _write_config(data: dict) -> None:
    path = fs._PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) + "\n")


def _read_config() -> dict:
    return json.loads(fs._PATH.read_text())


class TestPurgePastPeriods:
    def test_removes_past_single_day(self):
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        _write_config({"unavailable_periods": [yesterday], "blacklist_emails": [], "available_only_periods": []})
        removed = fs.purge_past_periods()
        assert removed == 1
        assert _read_config()["unavailable_periods"] == []

    def test_keeps_today(self):
        today = datetime.date.today().isoformat()
        _write_config({"unavailable_periods": [today], "blacklist_emails": [], "available_only_periods": []})
        removed = fs.purge_past_periods()
        assert removed == 0
        assert today in _read_config()["unavailable_periods"]

    def test_keeps_future_single_day(self):
        future = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
        _write_config({"unavailable_periods": [future], "blacklist_emails": [], "available_only_periods": []})
        removed = fs.purge_past_periods()
        assert removed == 0

    def test_removes_past_range_by_end_date(self):
        # range whose end date is yesterday
        start = (datetime.date.today() - datetime.timedelta(days=10)).isoformat()
        end = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        token = f"{start}:{end}"
        _write_config({"unavailable_periods": [token], "blacklist_emails": [], "available_only_periods": []})
        removed = fs.purge_past_periods()
        assert removed == 1

    def test_keeps_range_ending_today(self):
        start = (datetime.date.today() - datetime.timedelta(days=5)).isoformat()
        end = datetime.date.today().isoformat()
        token = f"{start}:{end}"
        _write_config({"unavailable_periods": [token], "blacklist_emails": [], "available_only_periods": []})
        removed = fs.purge_past_periods()
        assert removed == 0

    def test_removes_past_month(self):
        # A month entirely in the past — use last month
        today = datetime.date.today()
        if today.month == 1:
            past_month = f"{today.year - 1}-12"
        else:
            past_month = f"{today.year}-{today.month - 1:02d}"
        _write_config({"unavailable_periods": [past_month], "blacklist_emails": [], "available_only_periods": []})
        removed = fs.purge_past_periods()
        assert removed == 1

    def test_leaves_unparseable_tokens(self):
        _write_config({"unavailable_periods": ["not-a-date"], "blacklist_emails": [], "available_only_periods": []})
        removed = fs.purge_past_periods()
        assert removed == 0
        assert "not-a-date" in _read_config()["unavailable_periods"]

    def test_does_not_touch_other_keys(self):
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        future = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        _write_config({
            "unavailable_periods": [yesterday],
            "blacklist_emails": ["a@b.com"],
            "available_only_periods": [future],
        })
        fs.purge_past_periods()
        data = _read_config()
        assert data["blacklist_emails"] == ["a@b.com"]
        assert future in data["available_only_periods"]

    def test_returns_zero_when_no_file(self):
        removed = fs.purge_past_periods()
        assert removed == 0

    def test_mixed_keeps_future_removes_past(self):
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        future = (datetime.date.today() + datetime.timedelta(days=10)).isoformat()
        _write_config({"unavailable_periods": [yesterday, future], "blacklist_emails": [], "available_only_periods": []})
        removed = fs.purge_past_periods()
        assert removed == 1
        data = _read_config()
        assert future in data["unavailable_periods"]
        assert yesterday not in data["unavailable_periods"]


class TestAutoPurgeOnUnavailableOperations:
    def test_unavailable_periods_getter_purges_stale(self):
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        future = (datetime.date.today() + datetime.timedelta(days=5)).isoformat()
        _write_config({"unavailable_periods": [yesterday, future], "blacklist_emails": [], "available_only_periods": []})
        result = fs.unavailable_periods()
        assert yesterday not in result
        assert future in result

    def test_add_period_unavailable_purges_first(self):
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        future = (datetime.date.today() + datetime.timedelta(days=5)).isoformat()
        _write_config({"unavailable_periods": [yesterday], "blacklist_emails": [], "available_only_periods": []})
        fs.add_period("unavailable_periods", future)
        data = _read_config()
        assert yesterday not in data["unavailable_periods"]
        assert future in data["unavailable_periods"]

    def test_remove_period_unavailable_purges_first(self):
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        future = (datetime.date.today() + datetime.timedelta(days=5)).isoformat()
        _write_config({"unavailable_periods": [yesterday, future], "blacklist_emails": [], "available_only_periods": []})
        fs.remove_period("unavailable_periods", future)
        data = _read_config()
        assert yesterday not in data["unavailable_periods"]
        assert future not in data["unavailable_periods"]

    def test_add_period_blacklist_does_not_purge_unavailable(self):
        """Only unavailable_periods operations trigger purge — not blacklist operations."""
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        _write_config({"unavailable_periods": [yesterday], "blacklist_emails": [], "available_only_periods": []})
        fs.add_blacklist_email("x@y.com")
        # blacklist add should NOT have purged unavailable
        data = _read_config()
        assert yesterday in data["unavailable_periods"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_filter_store.py --tb=short -q
```

Expected: multiple failures — `purge_past_periods` not defined, `unavailable_periods` does not purge, etc.

- [ ] **Step 3: Implement `purge_past_periods` and wire it into `filter_store.py`**

Add to `organist_bot/filter_store.py` after the imports:

```python
import calendar
import datetime
```

Add this function after `available_only_periods()`:

```python
def _period_end_date(token: str) -> datetime.date | None:
    """Return the end date of a period token, or None if unparseable."""
    try:
        if ":" in token:
            end_str = token.split(":")[1]
            return datetime.date.fromisoformat(end_str)
        parts = token.split("-")
        if len(parts) == 2:
            year, month = int(parts[0]), int(parts[1])
            last_day = calendar.monthrange(year, month)[1]
            return datetime.date(year, month, last_day)
        return datetime.date.fromisoformat(token)
    except Exception:
        return None


def purge_past_periods() -> int:
    """Remove past unavailable_periods tokens. Returns count removed."""
    today = datetime.date.today()
    data = _read()
    before = len(data["unavailable_periods"])
    data["unavailable_periods"] = [
        t for t in data["unavailable_periods"]
        if (end := _period_end_date(t)) is None or end >= today
    ]
    removed = before - len(data["unavailable_periods"])
    if removed:
        _write(data)
    return removed
```

Update `unavailable_periods()`:

```python
def unavailable_periods() -> list[str]:
    purge_past_periods()
    return _read()["unavailable_periods"]
```

Update `add_period()`:

```python
def add_period(key: str, period: str) -> bool:
    """Add a period token. Returns True if added, False if already present."""
    if key == "unavailable_periods":
        purge_past_periods()
    data = _read()
    if period in data[key]:
        return False
    data[key].append(period)
    _write(data)
    return True
```

Update `remove_period()`:

```python
def remove_period(key: str, period: str) -> bool:
    """Remove a period token. Returns True if removed, False if not found."""
    if key == "unavailable_periods":
        purge_past_periods()
    data = _read()
    before = len(data[key])
    data[key] = [p for p in data[key] if p != period]
    if len(data[key]) == before:
        return False
    _write(data)
    return True
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_filter_store.py --tb=short -q
```

Expected: all pass.

- [ ] **Step 5: Run full suite to check for regressions**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest --tb=short -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add organist_bot/filter_store.py tests/test_filter_store.py
git commit -m "feat: purge past unavailable periods on every unavailable operation"
```

---

## Task 2: `calendar_client.py` — `list_upcoming_events` and `delete_event`

**Files:**
- Modify: `organist_bot/integrations/calendar_client.py`
- Modify: `tests/test_calendar_client.py`

### Background

`list_upcoming_events` fails open (returns `[]` on error — same pattern as `has_event_on_date`). `delete_event` raises on failure — a silent failure here would leave the user believing the gig was deleted when it wasn't.

The `list_upcoming_events` return dicts must include `id`, `summary`, `start_dt` (timezone-aware datetime), and `date_str` (ISO `YYYY-MM-DD`). The Google Calendar API returns all-day events with `start.date` and timed events with `start.dateTime`. Handle both.

- [ ] **Step 1: Write failing tests**

Add to `tests/test_calendar_client.py`:

```python
import datetime as dt


class TestListUpcomingEvents:
    def test_returns_list_of_event_dicts(self, client, mock_service):
        mock_service.events().list().execute.return_value = {
            "items": [
                {
                    "id": "evt1",
                    "summary": "Sunday Service",
                    "start": {"dateTime": "2026-06-01T10:30:00+01:00"},
                },
                {
                    "id": "evt2",
                    "summary": "Evensong",
                    "start": {"dateTime": "2026-06-14T18:00:00+01:00"},
                },
            ]
        }
        events = client.list_upcoming_events()
        assert len(events) == 2
        assert events[0]["id"] == "evt1"
        assert events[0]["summary"] == "Sunday Service"
        assert events[0]["date_str"] == "2026-06-01"
        assert isinstance(events[0]["start_dt"], dt.datetime)

    def test_returns_empty_list_when_no_events(self, client, mock_service):
        mock_service.events().list().execute.return_value = {"items": []}
        assert client.list_upcoming_events() == []

    def test_returns_empty_list_on_api_error(self, client, mock_service):
        """Fail-open: errors must not propagate."""
        mock_service.events().list().execute.side_effect = Exception("API down")
        assert client.list_upcoming_events() == []

    def test_respects_max_results(self, client, mock_service):
        mock_service.events().list().execute.return_value = {"items": []}
        client.list_upcoming_events(max_results=5)
        call_kwargs = mock_service.events().list.call_args[1]
        assert call_kwargs["maxResults"] == 5

    def test_handles_all_day_event(self, client, mock_service):
        """All-day events use start.date instead of start.dateTime."""
        mock_service.events().list().execute.return_value = {
            "items": [{"id": "a1", "summary": "Holiday", "start": {"date": "2026-12-25"}}]
        }
        events = client.list_upcoming_events()
        assert events[0]["date_str"] == "2026-12-25"
        assert isinstance(events[0]["start_dt"], dt.datetime)

    def test_events_missing_summary_use_no_title(self, client, mock_service):
        """Events without a summary field should not raise."""
        mock_service.events().list().execute.return_value = {
            "items": [{"id": "x1", "start": {"dateTime": "2026-07-01T10:00:00Z"}}]
        }
        events = client.list_upcoming_events()
        assert events[0]["summary"] == "(No title)"


class TestDeleteEvent:
    def test_calls_delete_with_correct_args(self, client, mock_service):
        mock_service.events().delete().execute.return_value = None
        client.delete_event("evt_123")
        call_kwargs = mock_service.events().delete.call_args[1]
        assert call_kwargs["calendarId"] == "cal@test.com"
        assert call_kwargs["eventId"] == "evt_123"

    def test_raises_on_api_error(self, client, mock_service):
        """delete_event must raise — silent failure is dangerous."""
        mock_service.events().delete().execute.side_effect = Exception("Not found")
        with pytest.raises(Exception, match="Not found"):
            client.delete_event("nonexistent_id")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_calendar_client.py -k "TestListUpcomingEvents or TestDeleteEvent" --tb=short -q
```

Expected: `AttributeError` — methods don't exist yet.

- [ ] **Step 3: Implement both methods in `calendar_client.py`**

Add after `has_event_on_date`, before `add_gig`:

```python
def list_upcoming_events(self, max_results: int = 10) -> list[dict]:
    """Return upcoming events from now, ordered by start time ascending.

    Each dict: {id, summary, start_dt (timezone-aware datetime), date_str (YYYY-MM-DD)}.
    Fails open — returns [] on any API error.
    """
    try:
        now = datetime.datetime.utcnow().isoformat() + "Z"
        result = (
            self._service.events()
            .list(
                calendarId=self.calendar_id,
                timeMin=now,
                maxResults=max_results,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        events = []
        for item in result.get("items", []):
            start = item.get("start", {})
            if "dateTime" in start:
                start_dt = datetime.datetime.fromisoformat(start["dateTime"])
                date_str = start_dt.date().isoformat()
            else:
                date_str = start.get("date", "")
                start_dt = datetime.datetime(
                    *[int(p) for p in date_str.split("-")],
                    tzinfo=datetime.timezone.utc,
                )
            events.append({
                "id": item["id"],
                "summary": item.get("summary", "(No title)"),
                "start_dt": start_dt,
                "date_str": date_str,
            })
        return events
    except Exception as exc:
        logger.warning("list_upcoming_events failed — returning []", extra={"error": str(exc)})
        return []

def delete_event(self, event_id: str) -> None:
    """Delete a calendar event by ID. Raises on failure."""
    self._service.events().delete(
        calendarId=self.calendar_id, eventId=event_id
    ).execute()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_calendar_client.py --tb=short -q
```

Expected: all pass.

- [ ] **Step 5: Run full suite**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest --tb=short -q
```

- [ ] **Step 6: Commit**

```bash
git add organist_bot/integrations/calendar_client.py tests/test_calendar_client.py
git commit -m "feat: add list_upcoming_events and delete_event to GoogleCalendarClient"
```

---

## Task 3: `telegram_bot.py` — `/gigs` and `/deletegig` commands

**Files:**
- Modify: `organist_bot/integrations/telegram_bot.py`
- Modify: `tests/test_telegram_integration.py`

### Background

The module-level `_gig_listing: dict[int, list[dict]] = {}` cache is keyed by `chat_id`, exactly like `_histories` in `gig_agent.py`. No TTL — the `/gigs` reply includes "Fetched at HH:MM" so the user knows when to refresh.

`_make_calendar_client()` is a local helper (mirrors the one in `gig_agent.py`) — do not import from `gig_agent.py`. The spec explicitly says `/deletegig` must check `_make_calendar_client() is None` before calling `delete_event`.

The `/gigs` reply uses Telegram's MarkdownV2 (`parse_mode="MarkdownV2"`) — dots in dates and parentheses must be escaped. Use `ParseMode.MARKDOWN_V2` or just the string `"MarkdownV2"`.

The format for a /gigs reply line: `1\. Sunday Service — St Mary's · Sun 1 Jun 2026 · 10:30am`

The `start_dt` from `list_upcoming_events` may be timezone-aware. Format it with `.strftime("%a %-d %b %Y · %-I:%M%p").lower()` adjusted for the timezone.

- [ ] **Step 1: Write failing tests**

Add to `tests/test_telegram_integration.py`:

```python
import datetime
from organist_bot.integrations.telegram_bot import cmd_gigs, cmd_deletegig, _gig_listing


def _make_event(n: int = 1) -> dict:
    return {
        "id": f"evt{n}",
        "summary": f"Sunday Service {n}",
        "start_dt": datetime.datetime(2026, 6, n, 10, 30, tzinfo=datetime.timezone.utc),
        "date_str": f"2026-06-0{n}",
    }


class TestCmdGigs:
    @pytest.fixture(autouse=True)
    def patch_settings(self):
        with patch("organist_bot.integrations.telegram_bot.settings") as mock:
            mock.telegram_chat_id = "7973955362"
            mock.google_calendar_id = "cal@test.com"
            mock.google_calendar_credentials_file = "fake.json"
            yield mock

    @pytest.mark.asyncio
    async def test_lists_events_as_numbered_reply(self):
        update = _make_update()
        events = [_make_event(1), _make_event(2)]
        with patch("organist_bot.integrations.telegram_bot._make_calendar_client") as mock_factory:
            mock_cal = MagicMock()
            mock_cal.list_upcoming_events.return_value = events
            mock_factory.return_value = mock_cal
            await cmd_gigs(update, MagicMock())
        update.message.reply_text.assert_called_once()
        reply_text = update.message.reply_text.call_args[0][0]
        assert "1" in reply_text
        assert "Sunday Service 1" in reply_text
        assert "2" in reply_text

    @pytest.mark.asyncio
    async def test_stores_events_in_listing_cache(self):
        update = _make_update(chat_id=7973955362)
        events = [_make_event(1)]
        _gig_listing.clear()
        with patch("organist_bot.integrations.telegram_bot._make_calendar_client") as mock_factory:
            mock_cal = MagicMock()
            mock_cal.list_upcoming_events.return_value = events
            mock_factory.return_value = mock_cal
            await cmd_gigs(update, MagicMock())
        assert _gig_listing.get(7973955362) == events

    @pytest.mark.asyncio
    async def test_replies_no_upcoming_gigs_when_empty(self):
        update = _make_update()
        with patch("organist_bot.integrations.telegram_bot._make_calendar_client") as mock_factory:
            mock_cal = MagicMock()
            mock_cal.list_upcoming_events.return_value = []
            mock_factory.return_value = mock_cal
            await cmd_gigs(update, MagicMock())
        reply = update.message.reply_text.call_args[0][0]
        assert "No upcoming gigs" in reply

    @pytest.mark.asyncio
    async def test_rejects_unauthorised_chat(self):
        update = _make_update(chat_id=9999)
        with patch("organist_bot.integrations.telegram_bot._make_calendar_client") as mock_factory:
            await cmd_gigs(update, MagicMock())
        mock_factory.assert_not_called()

    @pytest.mark.asyncio
    async def test_replies_not_configured_when_no_calendar(self):
        update = _make_update()
        with patch("organist_bot.integrations.telegram_bot._make_calendar_client", return_value=None):
            await cmd_gigs(update, MagicMock())
        reply = update.message.reply_text.call_args[0][0]
        assert "not configured" in reply.lower()


class TestCmdDeletegig:
    @pytest.fixture(autouse=True)
    def patch_settings(self):
        with patch("organist_bot.integrations.telegram_bot.settings") as mock:
            mock.telegram_chat_id = "7973955362"
            mock.google_calendar_id = "cal@test.com"
            mock.google_calendar_credentials_file = "fake.json"
            yield mock

    @pytest.fixture(autouse=True)
    def seed_cache(self):
        _gig_listing[7973955362] = [_make_event(1), _make_event(2)]
        yield
        _gig_listing.pop(7973955362, None)

    @pytest.mark.asyncio
    async def test_deletes_event_and_replies_confirmation(self):
        update = _make_update()
        context = MagicMock()
        context.args = ["1"]
        with (
            patch("organist_bot.integrations.telegram_bot._make_calendar_client") as mock_factory,
            patch("organist_bot.integrations.telegram_bot.filter_store") as mock_fs,
        ):
            mock_cal = MagicMock()
            mock_factory.return_value = mock_cal
            await cmd_deletegig(update, context)
        mock_cal.delete_event.assert_called_once_with("evt1")
        reply = update.message.reply_text.call_args[0][0]
        assert "Sunday Service 1" in reply

    @pytest.mark.asyncio
    async def test_removes_date_from_unavailable(self):
        update = _make_update()
        context = MagicMock()
        context.args = ["1"]
        with (
            patch("organist_bot.integrations.telegram_bot._make_calendar_client") as mock_factory,
            patch("organist_bot.integrations.telegram_bot.filter_store") as mock_fs,
        ):
            mock_cal = MagicMock()
            mock_factory.return_value = mock_cal
            await cmd_deletegig(update, context)
        mock_fs.remove_period.assert_called_once_with("unavailable_periods", "2026-06-01")

    @pytest.mark.asyncio
    async def test_updates_cache_after_delete(self):
        update = _make_update(chat_id=7973955362)
        context = MagicMock()
        context.args = ["1"]
        with (
            patch("organist_bot.integrations.telegram_bot._make_calendar_client") as mock_factory,
            patch("organist_bot.integrations.telegram_bot.filter_store"),
        ):
            mock_factory.return_value = MagicMock()
            await cmd_deletegig(update, context)
        # After deleting index 1, only event 2 remains
        assert len(_gig_listing[7973955362]) == 1
        assert _gig_listing[7973955362][0]["id"] == "evt2"

    @pytest.mark.asyncio
    async def test_no_args_replies_usage_hint(self):
        update = _make_update()
        context = MagicMock()
        context.args = []
        await cmd_deletegig(update, context)
        reply = update.message.reply_text.call_args[0][0]
        assert "/deletegig" in reply

    @pytest.mark.asyncio
    async def test_out_of_range_replies_error(self):
        update = _make_update()
        context = MagicMock()
        context.args = ["99"]
        await cmd_deletegig(update, context)
        reply = update.message.reply_text.call_args[0][0]
        assert "99" in reply

    @pytest.mark.asyncio
    async def test_empty_cache_prompts_run_gigs(self):
        _gig_listing.pop(7973955362, None)
        update = _make_update(chat_id=7973955362)
        context = MagicMock()
        context.args = ["1"]
        await cmd_deletegig(update, context)
        reply = update.message.reply_text.call_args[0][0]
        assert "/gigs" in reply

    @pytest.mark.asyncio
    async def test_delete_failure_replies_error(self):
        update = _make_update()
        context = MagicMock()
        context.args = ["1"]
        with (
            patch("organist_bot.integrations.telegram_bot._make_calendar_client") as mock_factory,
            patch("organist_bot.integrations.telegram_bot.filter_store"),
        ):
            mock_cal = MagicMock()
            mock_cal.delete_event.side_effect = Exception("API error")
            mock_factory.return_value = mock_cal
            await cmd_deletegig(update, context)
        reply = update.message.reply_text.call_args[0][0]
        assert "API error" in reply or "error" in reply.lower()

    @pytest.mark.asyncio
    async def test_rejects_unauthorised_chat(self):
        update = _make_update(chat_id=9999)
        context = MagicMock()
        context.args = ["1"]
        with patch("organist_bot.integrations.telegram_bot._make_calendar_client") as mock_factory:
            await cmd_deletegig(update, context)
        mock_factory.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_telegram_integration.py -k "TestCmdGigs or TestCmdDeletegig" --tb=short -q
```

Expected: `ImportError` — `cmd_gigs`, `cmd_deletegig`, `_gig_listing` not exported.

- [ ] **Step 3: Implement in `telegram_bot.py`**

First, update `_HELP` — replace the existing `_HELP` constant (which starts with `"*Organist Bot*\n\n"`) with this full replacement that adds `/gigs` and `/deletegig` before the existing filter commands:

```python
_HELP = (
    "*Organist Bot*\n\n"
    "*Gig calendar*\n"
    "  /gigs           — View upcoming gigs\n"
    "  /deletegig \\<n\\> — Delete gig by number\n"
    "  /addgig \\<url\\> — Add a gig by URL\n"
    "  /addgig         — Add a gig via conversation\n"
    "  /cancel         — Cancel gig entry\n\n"
    "*Filters*\n"
    "  /blacklist \\[add \\<email\\>|rm \\<email\\>|list\\]\n"
    "  /unavailable \\[add \\<period\\>|rm \\<period\\>|list\\]\n"
    "  /available \\[add \\<period\\>|rm \\<period\\>|list\\]\n"
    "  Period formats: `2026-12-25` · `2026-12-20:2027-01-05` · `2026-12`\n\n"
    "*Invoicing*\n"
    "  Just type your request in plain English, e.g.:\n"
    '  "Send an invoice to Holy Cross for March Masses, £240"\n'
    '  "List my clients"\n'
    "  /reset — Clear invoice conversation history"
)
```

Then add the module-level cache after the updated `_HELP` constant (no new top-level imports needed — `GoogleCalendarClient` is imported lazily inside the helper, consistent with how `filter_store` is imported lazily throughout the existing handlers):

```python
_gig_listing: dict[int, list[dict]] = {}


def _make_calendar_client():
    if settings.google_calendar_id and settings.google_calendar_credentials_file:
        from organist_bot.integrations.calendar_client import GoogleCalendarClient
        return GoogleCalendarClient(
            credentials_file=settings.google_calendar_credentials_file,
            calendar_id=settings.google_calendar_id,
        )
    return None
```

Add both command handler functions before `run()`:

```python
async def cmd_gigs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update):
        _reject(update)
        return
    cal = _make_calendar_client()
    if cal is None:
        await update.message.reply_text("Google Calendar not configured.")
        return
    events = cal.list_upcoming_events(max_results=10)
    chat_id = update.effective_chat.id
    _gig_listing[chat_id] = events
    if not events:
        await update.message.reply_text("No upcoming gigs found.")
        return

    import datetime as _dt
    now_str = _dt.datetime.now().strftime("%H:%M")
    lines = [f"*Upcoming gigs* \\(fetched at {now_str}\\)"]
    for i, ev in enumerate(events, start=1):
        start_dt = ev["start_dt"]
        time_str = start_dt.strftime("%-I:%M%p").lower()
        date_str = start_dt.strftime("%a %-d %b %Y")
        summary = ev["summary"].replace(".", "\\.").replace("-", "\\-").replace("(", "\\(").replace(")", "\\)")
        lines.append(f"{i}\\. {summary} · {date_str} · {time_str}")
    lines.append("\nUse /deletegig \\<number\\> to remove one\\.")
    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")


async def cmd_deletegig(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorised(update):
        _reject(update)
        return
    if not context.args:
        await update.message.reply_text("Usage: /deletegig <number>  — run /gigs first to see the list.")
        return
    try:
        n = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Usage: /deletegig <number>  — e.g. /deletegig 2")
        return

    chat_id = update.effective_chat.id
    listing = _gig_listing.get(chat_id)
    if not listing:
        await update.message.reply_text("Run /gigs first to see your upcoming gigs.")
        return
    if n < 1 or n > len(listing):
        await update.message.reply_text(f"No gig number {n}. Run /gigs to see the list.")
        return

    event = listing[n - 1]
    cal = _make_calendar_client()
    if cal is None:
        await update.message.reply_text("Google Calendar not configured.")
        return
    try:
        cal.delete_event(event["id"])
    except Exception as exc:
        await update.message.reply_text(f"Failed to delete: {exc}")
        return

    from organist_bot import filter_store
    filter_store.remove_period("unavailable_periods", event["date_str"])
    _gig_listing[chat_id] = [e for i, e in enumerate(listing) if i != n - 1]
    await update.message.reply_text(
        f"✓ Deleted {event['summary']}. Date removed from unavailable if it was there."
    )
```

Register in `run()` after existing filter command handlers:

```python
application.add_handler(CommandHandler("gigs", cmd_gigs))
application.add_handler(CommandHandler("deletegig", cmd_deletegig))
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_telegram_integration.py --tb=short -q
```

Expected: all pass.

- [ ] **Step 5: Run full suite**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest --tb=short -q
```

- [ ] **Step 6: Commit**

```bash
git add organist_bot/integrations/telegram_bot.py tests/test_telegram_integration.py
git commit -m "feat: add /gigs and /deletegig Telegram commands"
```

---

## Task 4: `gig_agent.py` — Auto-add gig date to unavailable on confirmation

**Files:**
- Modify: `organist_bot/integrations/gig_agent.py`
- Modify: `tests/test_gig_agent.py`

### Background

After `cal.add_gig(gig)` succeeds in the `confirmed=true` branch, parse `fields["date"]` with `normalize_to_yyyymmdd` (which returns `YYYYMMDD` format), convert to `YYYY-MM-DD`, and call `filter_store.add_period("unavailable_periods", date_str)`. If parsing fails, log a warning — the calendar write still succeeded; the unavailable sync is best-effort.

Key detail: `normalize_to_yyyymmdd` is in `organist_bot.filters`, not currently imported in `gig_agent.py`. `filter_store` is also not currently imported.

- [ ] **Step 1: Write failing tests**

Add to `tests/test_gig_agent.py`:

```python
class TestExecuteToolAddGigAutoUnavailable:
    @pytest.mark.asyncio
    async def test_adds_date_to_unavailable_on_success(self):
        """confirmed=true after calendar write adds YYYY-MM-DD date to unavailable_periods."""
        input_data = {**_FULL_INPUT, "confirmed": True}
        # "Sunday 1st June 2025" should normalize to 20250601 → 2025-06-01
        with (
            patch("organist_bot.integrations.gig_agent._make_calendar_client") as mock_factory,
            patch("organist_bot.integrations.gig_agent.filter_store") as mock_fs,
        ):
            mock_cal = MagicMock()
            mock_cal.add_gig.return_value = "evt_xyz"
            mock_factory.return_value = mock_cal
            await _execute_tool("add_gig", input_data)
        mock_fs.add_period.assert_called_once_with("unavailable_periods", "2025-06-01")

    @pytest.mark.asyncio
    async def test_calendar_failure_does_not_call_add_period(self):
        """If calendar write fails, unavailable is not touched."""
        input_data = {**_FULL_INPUT, "confirmed": True}
        with (
            patch("organist_bot.integrations.gig_agent._make_calendar_client") as mock_factory,
            patch("organist_bot.integrations.gig_agent.filter_store") as mock_fs,
        ):
            mock_cal = MagicMock()
            mock_cal.add_gig.side_effect = RuntimeError("down")
            mock_factory.return_value = mock_cal
            await _execute_tool("add_gig", input_data)
        mock_fs.add_period.assert_not_called()

    @pytest.mark.asyncio
    async def test_unparseable_date_does_not_raise(self):
        """If date can't be parsed, calendar write still succeeds and no error is returned."""
        input_data = {**_FULL_INPUT, "confirmed": True, "date": "sometime in June"}
        with (
            patch("organist_bot.integrations.gig_agent._make_calendar_client") as mock_factory,
            patch("organist_bot.integrations.gig_agent.filter_store") as mock_fs,
        ):
            mock_cal = MagicMock()
            mock_cal.add_gig.return_value = "evt_abc"
            mock_factory.return_value = mock_cal
            result = await _execute_tool("add_gig", input_data)
        import json
        data = json.loads(result)
        assert "result" in data  # calendar write succeeded
        mock_fs.add_period.assert_not_called()  # but unavailable not touched
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_gig_agent.py -k "TestExecuteToolAddGigAutoUnavailable" --tb=short -q
```

Expected: failures — `filter_store` attribute not patched / `add_period` never called.

- [ ] **Step 3: Implement in `gig_agent.py`**

Add two new imports near the top (after `from organist_bot.models import Gig`):

```python
import datetime

from organist_bot import filter_store
from organist_bot.filters import normalize_to_yyyymmdd
```

In `_execute_tool`, after `event_id = cal.add_gig(gig)` (inside the `try` block of the `confirmed=true` branch), add:

```python
yyyymmdd = normalize_to_yyyymmdd(fields["date"])
if yyyymmdd:
    try:
        date_str = datetime.datetime.strptime(yyyymmdd, "%Y%m%d").strftime("%Y-%m-%d")
        filter_store.add_period("unavailable_periods", date_str)
    except Exception:
        logger.warning("Failed to add gig date to unavailable periods", extra={"date": fields["date"]})
```

The full `confirmed=true` section after the change:

```python
event_id = cal.add_gig(gig)
yyyymmdd = normalize_to_yyyymmdd(fields["date"])
if yyyymmdd:
    try:
        date_str = datetime.datetime.strptime(yyyymmdd, "%Y%m%d").strftime("%Y-%m-%d")
        filter_store.add_period("unavailable_periods", date_str)
    except Exception:
        logger.warning("Failed to add gig date to unavailable periods", extra={"date": fields["date"]})
return json.dumps({"result": f"Added to calendar. Event ID: {event_id}"})
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_gig_agent.py --tb=short -q
```

Expected: all pass.

- [ ] **Step 5: Run full suite**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest --tb=short -q
```

Expected: all pass. This is the final regression check across all four tasks.

- [ ] **Step 6: Lint and type-check**

```bash
ruff check organist_bot/ && ruff format --check organist_bot/ && mypy organist_bot/
```

Fix any issues before committing.

- [ ] **Step 7: Commit**

```bash
git add organist_bot/integrations/gig_agent.py tests/test_gig_agent.py
git commit -m "feat: auto-add confirmed gig date to unavailable periods"
```
