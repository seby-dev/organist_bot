# Filter Suspensions by Date Range Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user temporarily suspend one named gig filter (or all filters except `seen`) for a date range — keyed by the gig's own date, not wall-clock time — managed via free-form Telegram text.

**Architecture:** A new JSON-backed store (`filter_suspension_store.py`, mirroring the existing `filter_store.py` pattern) holds `{filter, period}` entries with an extended period-token grammar supporting open-ended ranges. A new `SuspendableFilter` wrapper class in `filters.py` wraps an existing filter instance with a pre-loaded per-tick snapshot of active suspensions; if the gig's date is covered, the wrapped filter passes the gig through without calling the inner filter. `main.py` builds the snapshot once per tick and wraps each suspendable filter instance at construction time — critically, before it's used both inside `GigFilterChain` and at the direct `_fee_filter(gig)` call site in the NEG-drafts fee-partition block. A new Telegram tool (`manage_filter_suspensions`) exposes list/add/remove, reusing the existing `_resolve_period` helper for relative phrases.

**Tech Stack:** Python 3.13, pytest, pytest-asyncio, existing `atomic_store` (fcntl-locked JSON read/write).

## Global Constraints

- Suspendable filter names: `fee`, `sunday_time`, `blacklist`, `postcode`, `calendar`, `availability`, `all`. `seen` is never suspendable.
- Period token formats: `YYYY-MM-DD`, `YYYY-MM-DD:YYYY-MM-DD`, `YYYY-MM`, `YYYY-MM-DD:` (from date onward), `:YYYY-MM-DD` (up to and including date).
- Suspension containment is keyed by the **gig's own date**, not the date the suspension was created.
- Filter suspension snapshots are loaded once per pipeline tick (not per gig) — matches the existing performance pattern used for `BlacklistFilter`/`AvailabilityFilter` construction.
- Every new env var must be declared on `Settings` — N/A for this feature (no new env vars; purely a JSON-backed runtime store, same as `filter_store.py`).

---

### Task 1: `filter_suspension_store.py` — store, open-ended period parsing, snapshot loading

**Files:**
- Create: `organist_bot/filter_suspension_store.py`
- Test: `tests/test_filter_suspension_store.py`

**Interfaces:**
- Produces: `FILTER_KEYS: tuple[str, ...]`, `list_suspensions() -> list[dict[str, str]]`, `add_suspension(filter_name: str, period: str) -> bool` (raises `ValueError` on bad filter name / unparsable period), `remove_suspension(filter_name: str, period: str) -> bool`, `purge_past_suspensions() -> int`, `load_active() -> list[tuple[str, datetime.date, datetime.date]]`, `is_suspended(snapshot: list[tuple[str, datetime.date, datetime.date]], filter_name: str, gig_date: datetime.date) -> bool`.
- Consumes: `organist_bot.atomic_store` (`read_json`, `write_json`, `file_lock`) — same as `filter_store.py`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_filter_suspension_store.py`:

```python
"""Tests for filter_suspension_store: open-ended period parsing, CRUD, purge, snapshot."""

import datetime
import json

import pytest

import organist_bot.filter_suspension_store as fss


@pytest.fixture(autouse=True)
def use_tmp_dir(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)


def _write(data: dict) -> None:
    path = fss._PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) + "\n")


def _read() -> dict:
    return json.loads(fss._PATH.read_text())


class TestParsePeriodToken:
    def test_single_day(self):
        assert fss._parse_period_token("2026-12-25") == (
            datetime.date(2026, 12, 25),
            datetime.date(2026, 12, 25),
        )

    def test_closed_range(self):
        assert fss._parse_period_token("2026-12-15:2027-01-05") == (
            datetime.date(2026, 12, 15),
            datetime.date(2027, 1, 5),
        )

    def test_whole_month(self):
        assert fss._parse_period_token("2026-04") == (
            datetime.date(2026, 4, 1),
            datetime.date(2026, 4, 30),
        )

    def test_open_ended_from(self):
        start, end = fss._parse_period_token("2026-08-01:")
        assert start == datetime.date(2026, 8, 1)
        assert end == datetime.date.max

    def test_open_ended_until(self):
        start, end = fss._parse_period_token(":2026-08-01")
        assert start == datetime.date.min
        assert end == datetime.date(2026, 8, 1)

    def test_unparseable_returns_none(self):
        assert fss._parse_period_token("not-a-date") is None

    def test_empty_string_returns_none(self):
        assert fss._parse_period_token("") is None

    def test_bare_colon_returns_none(self):
        assert fss._parse_period_token(":") is None


class TestAddSuspension:
    def test_adds_new_entry(self):
        added = fss.add_suspension("postcode", "2026-12")
        assert added is True
        assert _read()["suspensions"] == [{"filter": "postcode", "period": "2026-12"}]

    def test_duplicate_returns_false(self):
        fss.add_suspension("postcode", "2026-12")
        added = fss.add_suspension("postcode", "2026-12")
        assert added is False
        assert len(_read()["suspensions"]) == 1

    def test_same_period_different_filter_is_not_duplicate(self):
        fss.add_suspension("postcode", "2026-12")
        added = fss.add_suspension("fee", "2026-12")
        assert added is True
        assert len(_read()["suspensions"]) == 2

    def test_invalid_filter_name_raises(self):
        with pytest.raises(ValueError):
            fss.add_suspension("seen", "2026-12")

    def test_unparseable_period_raises(self):
        with pytest.raises(ValueError):
            fss.add_suspension("postcode", "not-a-date")

    def test_all_is_a_valid_filter_name(self):
        assert fss.add_suspension("all", "2026-12") is True


class TestRemoveSuspension:
    def test_removes_exact_match(self):
        fss.add_suspension("postcode", "2026-12")
        removed = fss.remove_suspension("postcode", "2026-12")
        assert removed is True
        assert _read()["suspensions"] == []

    def test_no_match_returns_false(self):
        fss.add_suspension("postcode", "2026-12")
        removed = fss.remove_suspension("fee", "2026-12")
        assert removed is False
        assert len(_read()["suspensions"]) == 1


class TestPurgePastSuspensions:
    def test_removes_expired_closed_range(self):
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        _write({"suspensions": [{"filter": "fee", "period": f"2020-01-01:{yesterday}"}]})
        removed = fss.purge_past_suspensions()
        assert removed == 1
        assert _read()["suspensions"] == []

    def test_keeps_range_ending_today(self):
        today = datetime.date.today().isoformat()
        _write({"suspensions": [{"filter": "fee", "period": f"2020-01-01:{today}"}]})
        removed = fss.purge_past_suspensions()
        assert removed == 0

    def test_never_purges_open_ended_from(self):
        _write({"suspensions": [{"filter": "fee", "period": "2020-01-01:"}]})
        removed = fss.purge_past_suspensions()
        assert removed == 0
        assert len(_read()["suspensions"]) == 1

    def test_purges_expired_open_ended_until(self):
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        _write({"suspensions": [{"filter": "fee", "period": f":{yesterday}"}]})
        removed = fss.purge_past_suspensions()
        assert removed == 1

    def test_leaves_unparseable_entries(self):
        _write({"suspensions": [{"filter": "fee", "period": "garbage"}]})
        removed = fss.purge_past_suspensions()
        assert removed == 0
        assert len(_read()["suspensions"]) == 1

    def test_returns_zero_when_no_file(self):
        assert fss.purge_past_suspensions() == 0


class TestLoadActive:
    def test_parses_all_entries(self):
        fss.add_suspension("postcode", "2026-12")
        fss.add_suspension("fee", "2026-08-01:")
        snapshot = fss.load_active()
        assert ("postcode", datetime.date(2026, 12, 1), datetime.date(2026, 12, 31)) in snapshot
        assert ("fee", datetime.date(2026, 8, 1), datetime.date.max) in snapshot

    def test_skips_unparseable_entries(self):
        _write({"suspensions": [{"filter": "fee", "period": "garbage"}]})
        assert fss.load_active() == []

    def test_empty_store_returns_empty_list(self):
        assert fss.load_active() == []


class TestIsSuspended:
    def test_matches_named_filter_within_range(self):
        snapshot = [("postcode", datetime.date(2026, 12, 1), datetime.date(2026, 12, 31))]
        assert fss.is_suspended(snapshot, "postcode", datetime.date(2026, 12, 15)) is True

    def test_does_not_match_outside_range(self):
        snapshot = [("postcode", datetime.date(2026, 12, 1), datetime.date(2026, 12, 31))]
        assert fss.is_suspended(snapshot, "postcode", datetime.date(2027, 1, 1)) is False

    def test_does_not_match_different_filter(self):
        snapshot = [("postcode", datetime.date(2026, 12, 1), datetime.date(2026, 12, 31))]
        assert fss.is_suspended(snapshot, "fee", datetime.date(2026, 12, 15)) is False

    def test_all_matches_any_filter_name(self):
        snapshot = [("all", datetime.date(2026, 12, 1), datetime.date(2026, 12, 31))]
        assert fss.is_suspended(snapshot, "fee", datetime.date(2026, 12, 15)) is True
        assert fss.is_suspended(snapshot, "postcode", datetime.date(2026, 12, 15)) is True

    def test_empty_snapshot_matches_nothing(self):
        assert fss.is_suspended([], "fee", datetime.date(2026, 12, 15)) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_filter_suspension_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'organist_bot.filter_suspension_store'`

- [ ] **Step 3: Implement `organist_bot/filter_suspension_store.py`**

```python
"""organist_bot/filter_suspension_store.py
──────────────────────────────────────────
File-backed store for runtime filter suspensions — temporarily exempting
gigs whose OWN date falls within a period from a named filter (or "all"
filters except "seen"). The store lives at data/filter_suspensions.json
and is read fresh on every call, mirroring filter_store.py, so the
Telegram bot can mutate it and main.py picks up the changes on the very
next polling tick without a restart.
"""

from __future__ import annotations

import calendar
import datetime
import logging
import re
from pathlib import Path

from organist_bot import atomic_store

logger = logging.getLogger(__name__)

_PATH = Path("data/filter_suspensions.json")

# "seen" is deliberately excluded: suspending it wouldn't exempt a category
# of gig, it would just re-send the same application every poll tick.
FILTER_KEYS = ("fee", "sunday_time", "blacklist", "postcode", "calendar", "availability", "all")


def _read() -> dict[str, list[dict[str, str]]]:
    raw = atomic_store.read_json(_PATH, {})
    return {"suspensions": list(raw.get("suspensions", []))}


def _parse_period_token(token: str) -> tuple[datetime.date, datetime.date] | None:
    """Parse a period token into an inclusive (start, end) date range.

    Accepted formats:
      "2026-12-25"            – single day
      "2026-12-15:2027-01-05" – inclusive date range
      "2026-12"               – full calendar month
      "2026-08-01:"           – from that date onward (open-ended end)
      ":2026-08-01"           – up to and including that date (open-ended start)
    Returns None if the token cannot be parsed.
    """
    token = token.strip()
    try:
        if token.startswith(":"):
            end = datetime.date.fromisoformat(token[1:].strip())
            return (datetime.date.min, end)
        if token.endswith(":") and token.count(":") == 1:
            start = datetime.date.fromisoformat(token[:-1].strip())
            return (start, datetime.date.max)
        if ":" in token:
            start_str, end_str = token.split(":", 1)
            return (
                datetime.date.fromisoformat(start_str.strip()),
                datetime.date.fromisoformat(end_str.strip()),
            )
        if re.fullmatch(r"\d{4}-\d{2}", token):
            year, month = int(token[:4]), int(token[5:])
            last_day = calendar.monthrange(year, month)[1]
            return (datetime.date(year, month, 1), datetime.date(year, month, last_day))
        d = datetime.date.fromisoformat(token)
        return (d, d)
    except (ValueError, AttributeError):
        return None


# ── Read helpers (fresh read each call) ───────────────────────────────────────


def list_suspensions() -> list[dict[str, str]]:
    return _read()["suspensions"]


# ── Mutations ──────────────────────────────────────────────────────────────────


def add_suspension(filter_name: str, period: str) -> bool:
    """Add a suspension. Returns True if added, False if an identical
    (filter, period) pair already exists.

    Raises ValueError if filter_name is not in FILTER_KEYS, or period cannot
    be parsed.
    """
    if filter_name not in FILTER_KEYS:
        raise ValueError(f"Unknown filter {filter_name!r}; must be one of {FILTER_KEYS}")
    if _parse_period_token(period) is None:
        raise ValueError(f"Could not parse period {period!r}")
    with atomic_store.file_lock(_PATH):
        data = _read()
        for entry in data["suspensions"]:
            if entry["filter"] == filter_name and entry["period"] == period:
                return False
        data["suspensions"].append({"filter": filter_name, "period": period})
        atomic_store.write_json(_PATH, data, lock=False)
    return True


def remove_suspension(filter_name: str, period: str) -> bool:
    """Remove a suspension by exact (filter, period) match. Returns True if removed."""
    with atomic_store.file_lock(_PATH):
        data = _read()
        before = len(data["suspensions"])
        data["suspensions"] = [
            e
            for e in data["suspensions"]
            if not (e["filter"] == filter_name and e["period"] == period)
        ]
        if len(data["suspensions"]) == before:
            return False
        atomic_store.write_json(_PATH, data, lock=False)
    return True


def purge_past_suspensions() -> int:
    """Remove suspensions whose parsed end date is in the past.

    Open-ended "from X" entries (end == date.max) are never purged — they're
    meant to be indefinite until manually removed. Unparseable entries are
    left alone (they're surfaced via load_active's warning instead).
    Returns the count removed.
    """
    today = datetime.date.today()
    with atomic_store.file_lock(_PATH):
        data = _read()
        before = len(data["suspensions"])
        kept = []
        for entry in data["suspensions"]:
            parsed = _parse_period_token(entry.get("period", ""))
            if parsed is None:
                kept.append(entry)
                continue
            _, end = parsed
            if end == datetime.date.max or end >= today:
                kept.append(entry)
        removed = before - len(kept)
        if removed:
            data["suspensions"] = kept
            atomic_store.write_json(_PATH, data, lock=False)
        return removed


def load_active() -> list[tuple[str, datetime.date, datetime.date]]:
    """Parse all current suspensions into (filter_name, start, end) tuples for
    a single per-tick snapshot. Unparseable entries are skipped with a warning."""
    snapshot = []
    for entry in list_suspensions():
        parsed = _parse_period_token(entry.get("period", ""))
        if parsed is None:
            logger.warning(
                "load_active: could not parse period %r — skipping", entry.get("period")
            )
            continue
        start, end = parsed
        snapshot.append((entry.get("filter", ""), start, end))
    return snapshot


def is_suspended(
    snapshot: list[tuple[str, datetime.date, datetime.date]],
    filter_name: str,
    gig_date: datetime.date,
) -> bool:
    """Pure check: True if snapshot has an entry for filter_name (or "all")
    covering gig_date. No I/O — snapshot must come from load_active()."""
    return any(
        (name == filter_name or name == "all") and start <= gig_date <= end
        for name, start, end in snapshot
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_filter_suspension_store.py -v`
Expected: PASS (all tests green)

- [ ] **Step 5: Lint and type-check**

Run: `ruff check organist_bot/filter_suspension_store.py tests/test_filter_suspension_store.py && ruff format organist_bot/filter_suspension_store.py tests/test_filter_suspension_store.py && mypy organist_bot/filter_suspension_store.py`
Expected: no errors (fix any and re-run before committing)

- [ ] **Step 6: Commit**

```bash
git add organist_bot/filter_suspension_store.py tests/test_filter_suspension_store.py
git commit -m "feat: add filter_suspension_store with open-ended period support"
```

---

### Task 2: `SuspendableFilter` wrapper in `filters.py`

**Files:**
- Modify: `organist_bot/filters.py`
- Test: `tests/test_filters.py`

**Interfaces:**
- Consumes: `organist_bot.filter_suspension_store.is_suspended(snapshot, filter_name, gig_date) -> bool` (Task 1), `normalize_to_yyyymmdd(gig.date) -> str | None` (already in `filters.py`).
- Produces: `SuspendableFilter(filter_name: str, inner: Callable[[Gig], bool], snapshot: list[tuple[str, date, date]])` — callable, `SuspendableFilter(gig) -> bool`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_filters.py` (append after the `TestAvailabilityFilterIntegration` class, before `TestPostcodeFilterAlert`):

```python
class TestSuspendableFilter:
    def test_suspended_date_passes_without_calling_inner(self):
        inner_calls = []

        def inner(gig):
            inner_calls.append(gig)
            return False  # would reject if actually called

        snapshot = [("fee", datetime.date(2026, 12, 1), datetime.date(2026, 12, 31))]
        f = SuspendableFilter("fee", inner, snapshot)
        assert f(_gig("Sunday, 20 December 2026")) is True
        assert inner_calls == []

    def test_non_suspended_date_delegates_to_inner(self):
        def inner(gig):
            return False

        snapshot = [("fee", datetime.date(2026, 12, 1), datetime.date(2026, 12, 31))]
        f = SuspendableFilter("fee", inner, snapshot)
        assert f(_gig("Sunday, 3 May 2026")) is False

    def test_delegates_to_inner_when_inner_passes(self):
        def inner(gig):
            return True

        snapshot = []
        f = SuspendableFilter("fee", inner, snapshot)
        assert f(_gig("Sunday, 3 May 2026")) is True

    def test_all_suspension_covers_any_filter_name(self):
        def inner(gig):
            return False

        snapshot = [("all", datetime.date(2026, 12, 1), datetime.date(2026, 12, 31))]
        f = SuspendableFilter("postcode", inner, snapshot)
        assert f(_gig("Sunday, 20 December 2026")) is True

    def test_unparseable_gig_date_fails_open_to_inner(self):
        def inner(gig):
            return False

        snapshot = [("fee", datetime.date(2026, 12, 1), datetime.date(2026, 12, 31))]
        f = SuspendableFilter("fee", inner, snapshot)
        assert f(_gig("not a real date")) is False  # falls through to inner, which rejects

    def test_repr_includes_filter_name_and_inner(self):
        f = SuspendableFilter("fee", FeeFilter(min_fee=100), [])
        assert "fee" in repr(f)
```

Update the import block at the top of `tests/test_filters.py` to include `SuspendableFilter`:

```python
from organist_bot.filters import (
    AvailabilityFilter,
    BlacklistFilter,
    BookedDateFilter,
    CalendarFilter,
    FeeFilter,
    GigFilterChain,
    PostcodeFilter,
    SeenFilter,
    SundayTimeFilter,
    SuspendableFilter,
    _date_in_periods,
    _parse_periods,
    is_negotiable,
    normalize_to_yyyymmdd,
    parse_min_fee,
    parse_start_time,
    parse_weekday,
)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_filters.py::TestSuspendableFilter -v`
Expected: FAIL with `ImportError: cannot import name 'SuspendableFilter'`

- [ ] **Step 3: Implement `SuspendableFilter` in `organist_bot/filters.py`**

Add the import near the top of `organist_bot/filters.py` (after the existing `from organist_bot import alert` line, ~line 11):

```python
from organist_bot import alert
from organist_bot import filter_suspension_store
from organist_bot.models import Gig
```

Add the class after `CalendarFilter` (after line 508, before the "Availability period helpers" section comment):

```python
class SuspendableFilter:
    """Wraps a filter; passes gigs through unconditionally if an active
    suspension snapshot entry covers this filter's name (or "all") for the
    gig's date.

    The snapshot is loaded once per tick via filter_suspension_store.load_active()
    and passed in at construction time — this class performs no I/O itself, so
    wrapping many gigs in a tight loop stays cheap.

    Fails open (delegates to inner) if the gig's date cannot be parsed, matching
    every other filter's fail-open convention in this module.
    """

    def __init__(
        self,
        filter_name: str,
        inner: Callable[[Gig], bool],
        snapshot: list[tuple[str, datetime.date, datetime.date]],
    ) -> None:
        self.filter_name = filter_name
        self.inner = inner
        self._snapshot = snapshot

    def __call__(self, gig: Gig) -> bool:
        normalized = normalize_to_yyyymmdd(gig.date)
        if normalized is not None:
            d = datetime.datetime.strptime(normalized, "%Y%m%d").date()
            if filter_suspension_store.is_suspended(self._snapshot, self.filter_name, d):
                return True  # suspended — pass through
        return self.inner(gig)

    def __repr__(self) -> str:
        return f"SuspendableFilter({self.filter_name!r}, {self.inner!r})"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_filters.py -v`
Expected: PASS (all tests green, including the full existing `test_filters.py` suite — no regressions)

- [ ] **Step 5: Lint and type-check**

Run: `ruff check organist_bot/filters.py tests/test_filters.py && ruff format organist_bot/filters.py tests/test_filters.py && mypy organist_bot/filters.py`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add organist_bot/filters.py tests/test_filters.py
git commit -m "feat: add SuspendableFilter wrapper for date-ranged filter suspensions"
```

---

### Task 3: Wire suspensions into `main.py`

**Files:**
- Modify: `main.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `organist_bot.filter_suspension_store.load_active() -> list[tuple[str, date, date]]` and `.purge_past_suspensions() -> int` (Task 1), `organist_bot.filters.SuspendableFilter` (Task 2).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_main.py`, inside `class TestMain` (after `test_calendar_filter_in_prefilter_skips_detail_page_fetch`, i.e. after line 386):

```python
    def test_suspended_blacklist_filter_lets_gig_through(self):
        """A blacklist suspension covering the gig's date must let a
        blacklisted email through the Phase 2 chain."""
        mock_settings = self._make_minimal_settings()
        mock_settings.enable_blacklist_filter = True

        basic = dict(
            header="Test Gig",
            organisation="Church",
            locality="London",
            date="Sunday, March 1, 2026",
            time="10:00 AM",
            fee="£120",
            link="https://organistsonline.org/required/test",
        )
        full = {"email": "blacklisted@example.com"}

        mock_scraper = MagicMock()
        mock_scraper.fetch.return_value = "<html></html>"
        mock_scraper.parse_gig_listings.return_value = [MagicMock()]
        mock_scraper.extract_basic_details.return_value = basic
        mock_scraper.extract_full_details.return_value = full

        with (
            patch("main.settings", mock_settings),
            patch("main.Notifier") as MockNotifier,
            patch("main.SMTPTransport"),
            patch("main.load_seen_gigs", return_value=set()),
            patch("main.save_seen_gigs"),
            patch("main.load_listings_hash", return_value=None),
            patch("main.save_listings_hash"),
            patch("main.set_run_id"),
            patch("main.filter_store") as mock_filter_store,
            patch("main.filter_suspension_store") as mock_fss,
        ):
            mock_filter_store.blacklist_emails.return_value = ["blacklisted@example.com"]
            mock_fss.load_active.return_value = [
                ("blacklist", _dt.date(2026, 1, 1), _dt.date(2026, 12, 31))
            ]
            mock_fss.purge_past_suspensions.return_value = 0
            notifier_inst = MockNotifier.return_value
            main_module.main(mock_scraper)

        notifier_inst.send_summary.assert_called_once()
        assert len(notifier_inst.send_summary.call_args[0][0]) == 1

    def test_purge_past_suspensions_called_each_tick(self):
        """purge_past_suspensions() must run once per tick, alongside expire_past_applied()."""
        mock_settings = self._make_minimal_settings()
        mock_scraper = MagicMock()
        mock_scraper.fetch.return_value = "<html></html>"
        mock_scraper.parse_gig_listings.return_value = []

        with (
            patch("main.settings", mock_settings),
            patch("main.Notifier"),
            patch("main.SMTPTransport"),
            patch("main.load_seen_gigs", return_value=set()),
            patch("main.save_seen_gigs"),
            patch("main.load_listings_hash", return_value=None),
            patch("main.save_listings_hash"),
            patch("main.set_run_id"),
            patch("main.filter_suspension_store") as mock_fss,
        ):
            mock_fss.load_active.return_value = []
            mock_fss.purge_past_suspensions.return_value = 0
            main_module.main(mock_scraper)

        mock_fss.purge_past_suspensions.assert_called_once()
```

Add to `class TestNegDrafts` (after `test_normal_gig_above_min_fee_still_notified`, i.e. after line 833):

```python
    def test_suspended_fee_filter_bypasses_neg_partition(self, tmp_path, monkeypatch):
        """A fee suspension covering the gig's date must let a below-threshold,
        non-negotiable-worded gig through as normal — not partitioned to
        neg_pending or dropped. This proves the suspension applies at the
        direct _fee_filter(gig) call site inside the NEG partition, not just
        inside GigFilterChain."""
        monkeypatch.setattr(application_store, "_PATH", tmp_path / "applications.json")
        mock_settings = self._settings()  # enable_neg_drafts=True, enable_fee_filter=True, min_fee=100
        scraper = self._mock_scraper_with_one_gig(fee="£50")  # below min_fee, not "NEG"-worded

        with (
            patch("main.alert"),
            patch("main.settings", mock_settings),
            patch("organist_bot.notifier.application_store"),
            patch("main.load_seen_gigs", return_value=set()),
            patch("main.load_listings_hash", return_value="old_hash"),
            patch("main.save_listings_hash"),
            patch("main.save_seen_gigs"),
            patch("main.filter_store"),
            patch("main.SMTPTransport"),
            patch("main.set_run_id"),
            patch("main.runtime_config") as mock_rc,
            patch("main.filter_suspension_store") as mock_fss,
            patch("main.Notifier") as mock_notifier_cls,
        ):
            mock_rc.get.side_effect = lambda k, d: d
            mock_fss.load_active.return_value = [("fee", _dt.date.min, _dt.date.max)]
            mock_fss.purge_past_suspensions.return_value = 0
            main_module.main(scraper)

        assert application_store.list_neg_pending() == []
        mock_notifier_cls.return_value.send_summary.assert_called_once()
        assert len(mock_notifier_cls.return_value.send_summary.call_args[0][0]) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_main.py -k "suspended or purge_past_suspensions" -v`
Expected: FAIL — `AttributeError: <MagicMock ...> does not have the attribute 'filter_suspension_store'` (module not yet imported in `main.py`), or the blacklist/fee gigs get rejected (assertions fail) since suspension wiring doesn't exist yet.

- [ ] **Step 3: Wire suspensions into `main.py`**

Add the import (alongside the existing `organist_bot.filter_store` import, ~line 15):

```python
import organist_bot.filter_store as filter_store
import organist_bot.filter_suspension_store as filter_suspension_store
```

Add `SuspendableFilter` to the existing `from organist_bot.filters import (...)` block (~lines 17-27):

```python
from organist_bot.filters import (
    AvailabilityFilter,
    BlacklistFilter,
    CalendarFilter,
    FeeFilter,
    GigFilterChain,
    PostcodeFilter,
    SeenFilter,
    SundayTimeFilter,
    SuspendableFilter,
    is_negotiable,
)
```

Load the snapshot once per tick and wrap `_fee_filter`/`_sunday_time_filter`/availability filters at construction time (replace the block at ~lines 176-195):

```python
    # Suspensions snapshot: loaded once per tick, not per gig — same performance
    # pattern already used for blacklist/availability filter construction below.
    suspension_snapshot = filter_suspension_store.load_active()

    _fee_filter = (
        FeeFilter(min_fee=runtime_config.get("min_fee", settings.min_fee))
        if settings.enable_fee_filter
        else None
    )
    if _fee_filter is not None:
        # Wrapped here (not just when added to a chain) so the NEG-drafts fee
        # partition below — which calls _fee_filter(gig) directly — also
        # respects fee suspensions.
        _fee_filter = SuspendableFilter("fee", _fee_filter, suspension_snapshot)
    # When NEG drafting is enabled we remove FeeFilter from BOTH chains so NEG
    # gigs survive past pre_filter (needed for the detail-page fetch that gets
    # us the contact email) and past filter_chain. The explicit partition gate
    # below Phase 2 then sorts them into normal / NEG / drop.
    _include_fee_in_chains = _fee_filter is not None and not settings.enable_neg_drafts

    _sunday_time_filter = SundayTimeFilter() if settings.enable_sunday_time_filter else None
    if _sunday_time_filter is not None:
        _sunday_time_filter = SuspendableFilter(
            "sunday_time", _sunday_time_filter, suspension_snapshot
        )
    _avail_filters: list = []
    if settings.enable_availability_filter:
        unavail = filter_store.unavailable_periods()
        avail_only = filter_store.available_only_periods()
        if unavail:
            _avail_filters.append(
                SuspendableFilter(
                    "availability", AvailabilityFilter(unavail, mode="block"), suspension_snapshot
                )
            )
        if avail_only:
            _avail_filters.append(
                SuspendableFilter(
                    "availability",
                    AvailabilityFilter(avail_only, mode="only"),
                    suspension_snapshot,
                )
            )
```

Wrap `CalendarFilter` where it's added to `pre_filter` (~line 218):

```python
        pre_filter.add(SuspendableFilter("calendar", CalendarFilter(cal_client), suspension_snapshot))
```

Wrap `BlacklistFilter` where it's added to `filter_chain` (~line 310):

```python
    if settings.enable_blacklist_filter:
        filter_chain.add(
            SuspendableFilter(
                "blacklist", BlacklistFilter(filter_store.blacklist_emails()), suspension_snapshot
            )
        )
    else:
        logger.info("BlacklistFilter disabled")
```

Wrap `PostcodeFilter` where it's added to `filter_chain` (~lines 320-327):

```python
    if settings.enable_postcode_filter and settings.home_postcode and settings.google_maps_api_key:
        filter_chain.add(
            SuspendableFilter(
                "postcode",
                PostcodeFilter(
                    home_postcode=settings.home_postcode,
                    api_key=settings.google_maps_api_key,
                    max_minutes=runtime_config.get("max_travel_minutes", settings.max_travel_minutes),
                ),
                suspension_snapshot,
            )
        )
```

Add the purge call in the post-pipeline section, alongside the existing `expire_past_applied()` call (~after line 471):

```python
    try:
        expired = application_store.expire_past_applied()
        if expired > 0:
            logger.info("Expired past applications as no_response", extra={"count": expired})
    except Exception:
        logger.warning("application_store: expire_past_applied failed", exc_info=True)

    try:
        removed_suspensions = filter_suspension_store.purge_past_suspensions()
        if removed_suspensions > 0:
            logger.info(
                "Purged expired filter suspensions", extra={"count": removed_suspensions}
            )
    except Exception:
        logger.warning("filter_suspension_store: purge_past_suspensions failed", exc_info=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_main.py -v`
Expected: PASS (all tests green, including the full existing `test_main.py` suite — no regressions, especially `TestNegDrafts` and the pre-filter detail-page-fetch tests)

- [ ] **Step 5: Run the full test suite**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest --tb=short -q`
Expected: PASS, no regressions anywhere

- [ ] **Step 6: Lint and type-check**

Run: `ruff check main.py tests/test_main.py && ruff format main.py tests/test_main.py && mypy organist_bot/`
Expected: no errors

- [ ] **Step 7: Commit**

```bash
git add main.py tests/test_main.py
git commit -m "feat: wire filter suspensions into the scraper pipeline"
```

---

### Task 4: Telegram tool `manage_filter_suspensions` in `unified_agent.py`

**Files:**
- Modify: `organist_bot/integrations/unified_agent.py`
- Test: `tests/test_unified_agent.py`

**Interfaces:**
- Consumes: `organist_bot.filter_suspension_store` module (Task 1): `list_suspensions`, `add_suspension`, `remove_suspension`. Reuses existing `_resolve_period(text: str) -> str` and `_format_period(token: str) -> str` helpers already in `unified_agent.py`.
- Produces: tool `manage_filter_suspensions` registered in `_TOOL_HANDLERS` via `@_handler("manage_filter_suspensions")`; entry added to the `TOOLS` list.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_unified_agent.py`, inside `class TestFilterTools` (after `test_manage_unavailable_remove_calendar_failure_does_not_raise`, i.e. after line 927):

```python
    @pytest.mark.asyncio
    async def test_manage_filter_suspensions_list_empty(self):
        with patch(
            "organist_bot.integrations.unified_agent.filter_suspension_store"
        ) as mock_fss:
            mock_fss.list_suspensions.return_value = []
            result = await _execute_tool(
                "manage_filter_suspensions", {"action": "list"}, CHAT_ID
            )
        assert "no filter suspensions" in result.lower()

    @pytest.mark.asyncio
    async def test_manage_filter_suspensions_list_shows_filter_and_period(self):
        with patch(
            "organist_bot.integrations.unified_agent.filter_suspension_store"
        ) as mock_fss:
            mock_fss.list_suspensions.return_value = [{"filter": "postcode", "period": "2026-12"}]
            result = await _execute_tool(
                "manage_filter_suspensions", {"action": "list"}, CHAT_ID
            )
        assert "postcode" in result
        assert "01 Dec 2026" in result

    @pytest.mark.asyncio
    async def test_manage_filter_suspensions_list_shows_open_ended_from(self):
        with patch(
            "organist_bot.integrations.unified_agent.filter_suspension_store"
        ) as mock_fss:
            mock_fss.list_suspensions.return_value = [{"filter": "fee", "period": "2026-08-01:"}]
            result = await _execute_tool(
                "manage_filter_suspensions", {"action": "list"}, CHAT_ID
            )
        assert "from" in result.lower()
        assert "onward" in result.lower()

    @pytest.mark.asyncio
    async def test_manage_filter_suspensions_list_shows_open_ended_until(self):
        with patch(
            "organist_bot.integrations.unified_agent.filter_suspension_store"
        ) as mock_fss:
            mock_fss.list_suspensions.return_value = [{"filter": "all", "period": ":2026-01-05"}]
            result = await _execute_tool(
                "manage_filter_suspensions", {"action": "list"}, CHAT_ID
            )
        assert "through" in result.lower()

    @pytest.mark.asyncio
    async def test_manage_filter_suspensions_add_success(self):
        with patch(
            "organist_bot.integrations.unified_agent.filter_suspension_store"
        ) as mock_fss:
            mock_fss.add_suspension.return_value = True
            result = await _execute_tool(
                "manage_filter_suspensions",
                {"action": "add", "filter": "postcode", "period": "2026-12"},
                CHAT_ID,
            )
        mock_fss.add_suspension.assert_called_once_with("postcode", "2026-12")
        data = json.loads(result)
        assert "suspended" in data["result"].lower()

    @pytest.mark.asyncio
    async def test_manage_filter_suspensions_add_duplicate(self):
        with patch(
            "organist_bot.integrations.unified_agent.filter_suspension_store"
        ) as mock_fss:
            mock_fss.add_suspension.return_value = False
            result = await _execute_tool(
                "manage_filter_suspensions",
                {"action": "add", "filter": "postcode", "period": "2026-12"},
                CHAT_ID,
            )
        data = json.loads(result)
        assert "already" in data["result"].lower()

    @pytest.mark.asyncio
    async def test_manage_filter_suspensions_add_invalid_returns_error(self):
        with patch(
            "organist_bot.integrations.unified_agent.filter_suspension_store"
        ) as mock_fss:
            mock_fss.add_suspension.side_effect = ValueError("Could not parse period 'nonsense'")
            result = await _execute_tool(
                "manage_filter_suspensions",
                {"action": "add", "filter": "fee", "period": "nonsense"},
                CHAT_ID,
            )
        data = json.loads(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_manage_filter_suspensions_add_resolves_relative_period(self):
        with patch(
            "organist_bot.integrations.unified_agent.filter_suspension_store"
        ) as mock_fss:
            mock_fss.add_suspension.return_value = True
            await _execute_tool(
                "manage_filter_suspensions",
                {"action": "add", "filter": "fee", "period": "next month"},
                CHAT_ID,
            )
        called_period = mock_fss.add_suspension.call_args[0][1]
        assert called_period != "next month"  # resolved to a real token

    @pytest.mark.asyncio
    async def test_manage_filter_suspensions_remove_success(self):
        with patch(
            "organist_bot.integrations.unified_agent.filter_suspension_store"
        ) as mock_fss:
            mock_fss.remove_suspension.return_value = True
            result = await _execute_tool(
                "manage_filter_suspensions",
                {"action": "remove", "filter": "postcode", "period": "2026-12"},
                CHAT_ID,
            )
        mock_fss.remove_suspension.assert_called_once_with("postcode", "2026-12")
        data = json.loads(result)
        assert "resumed" in data["result"].lower()

    @pytest.mark.asyncio
    async def test_manage_filter_suspensions_remove_not_found(self):
        with patch(
            "organist_bot.integrations.unified_agent.filter_suspension_store"
        ) as mock_fss:
            mock_fss.remove_suspension.return_value = False
            result = await _execute_tool(
                "manage_filter_suspensions",
                {"action": "remove", "filter": "postcode", "period": "2026-12"},
                CHAT_ID,
            )
        data = json.loads(result)
        assert "no matching" in data["result"].lower()

    def test_seen_not_in_manage_filter_suspensions_enum(self):
        tool_def = next(t for t in TOOLS if t["name"] == "manage_filter_suspensions")
        assert "seen" not in tool_def["input_schema"]["properties"]["filter"]["enum"]
```

Update the import block at the top of `tests/test_unified_agent.py` to include `TOOLS`:

```python
from organist_bot.integrations.unified_agent import (
    TOOLS,
    UnifiedAgent,
    _execute_tool,
    _last_gig_listing,
    sync_calendar_blocks,
)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_unified_agent.py -k manage_filter_suspensions -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'filter_suspension_store'` and `StopIteration` from the `next(...)` enum test (tool not yet in `TOOLS`)

- [ ] **Step 3: Add the tool, handler, and system-prompt examples to `unified_agent.py`**

Add the import (alongside the existing `from organist_bot import analytics, application_store, filter_store, travel` line, ~line 10):

```python
from organist_bot import analytics, application_store, filter_store, filter_suspension_store, travel
```

Add to the `SYSTEM_PROMPT` string, inside the existing `## Filter management` section, right after the "Period formats" line (~line 70):

```python
- "Turn off the postcode filter for all of December" → manage_filter_suspensions(action=add, filter=postcode, period=2026-12).
- "Ignore the fee filter from August 1st onward" → manage_filter_suspensions(action=add, filter=fee, period=2026-08-01:).
- "Disable every filter until the 5th of January" → manage_filter_suspensions(action=add, filter=all, period=:2026-01-05).
- "What filters are currently suspended?" → manage_filter_suspensions(action=list).
- "Resume the postcode filter" → manage_filter_suspensions(action=remove, filter=postcode, period=<period from the last list>).
- The 'seen' filter cannot be suspended — if asked, explain that suspending it would just resend the same application every poll tick instead of exempting a category of gig.
```

Add the tool definition to the `TOOLS` list, immediately after the `manage_available` entry (~after line 427, before the `# ── Meta ──` comment):

```python
    {
        "name": "manage_filter_suspensions",
        "description": (
            "Suspend or resume gig filters for a date range, keyed by the GIG's own date "
            "(not today's date) — e.g. 'ignore the postcode filter for gigs in December'. "
            "action: list, add, or remove. filter: fee, sunday_time, blacklist, postcode, "
            "calendar, availability, or all. The 'seen' filter cannot be suspended — doing so "
            "would just re-send the same application every poll tick instead of exempting a "
            "category of gig. period formats: YYYY-MM-DD, YYYY-MM-DD:YYYY-MM-DD, YYYY-MM, "
            "YYYY-MM-DD: (from that date onward, open-ended), :YYYY-MM-DD (up to and including "
            "that date). Also accepts the same relative phrases as manage_unavailable: today, "
            "tomorrow, this/next <weekday>, this weekend, next week, this/next month."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "add", "remove"]},
                "filter": {
                    "type": "string",
                    "enum": [
                        "fee",
                        "sunday_time",
                        "blacklist",
                        "postcode",
                        "calendar",
                        "availability",
                        "all",
                    ],
                },
                "period": {"type": "string"},
            },
            "required": ["action"],
        },
    },
```

Add the formatting helpers and handler in `unified_agent.py`, immediately after `_format_periods_list` and before `_handle_manage_unavailable` (~after line 1370):

```python
def _format_suspension(entry: dict) -> str:
    """Format a suspension entry (filter + period) into a readable label,
    including open-ended bounds."""
    period = entry.get("period", "")
    filter_name = entry.get("filter", "")
    if period.startswith(":"):
        label = f"through {_format_period(period[1:])}"
    elif period.endswith(":") and period.count(":") == 1:
        label = f"from {_format_period(period[:-1])} onward"
    else:
        label = _format_period(period)
    return f"{filter_name}: {label}"


def _format_suspensions_list(suspensions: list[dict]) -> str:
    if not suspensions:
        return "No filter suspensions set."
    lines = "\n".join(f"  • {_format_suspension(e)}" for e in suspensions)
    return f"Filter suspensions ({len(suspensions)}):\n{lines}"


@_handler("manage_filter_suspensions")
async def _handle_manage_filter_suspensions(input_data: dict, chat_id: int) -> str:
    action = input_data["action"]
    if action == "list":
        suspensions = filter_suspension_store.list_suspensions()
        return _format_suspensions_list(suspensions)

    filter_name = input_data.get("filter", "")
    period = _resolve_period(input_data.get("period", ""))

    if action == "add":
        try:
            added = filter_suspension_store.add_suspension(filter_name, period)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        msg = (
            f"Suspended '{filter_name}' for '{period}'."
            if added
            else f"'{filter_name}' is already suspended for '{period}'."
        )
        return json.dumps({"result": msg})

    if action == "remove":
        removed = filter_suspension_store.remove_suspension(filter_name, period)
        msg = (
            f"Resumed '{filter_name}' for '{period}'."
            if removed
            else f"No matching suspension for '{filter_name}' / '{period}'."
        )
        return json.dumps({"result": msg})

    return json.dumps({"error": f"Unknown action: {action}"})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_unified_agent.py -v`
Expected: PASS (all tests green, including the full existing `test_unified_agent.py` suite — no regressions)

- [ ] **Step 5: Run the full test suite**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest --tb=short -q`
Expected: PASS

- [ ] **Step 6: Lint and type-check**

Run: `ruff check organist_bot/integrations/unified_agent.py tests/test_unified_agent.py && ruff format organist_bot/integrations/unified_agent.py tests/test_unified_agent.py && mypy organist_bot/`
Expected: no errors

- [ ] **Step 7: Commit**

```bash
git add organist_bot/integrations/unified_agent.py tests/test_unified_agent.py
git commit -m "feat: add manage_filter_suspensions Telegram tool"
```

---

### Task 5: Document the feature in `CLAUDE.md`

**Files:**
- Modify: `CLAUDE.md`

**Interfaces:**
- None (documentation only).

- [ ] **Step 1: Add `filter_suspension_store.py` to the top-level modules list**

In the `### organist_bot/ — top-level modules` section, add a new line immediately after the existing `filter_store.py` bullet:

```markdown
- `filter_store.py` — JSON-backed runtime filter values (blacklist, unavail/avail periods); read fresh each tick
- `filter_suspension_store.py` — JSON-backed store for date-ranged filter suspensions (temporarily exempt gigs, by their own date, from a named filter or all filters except `seen`); read fresh each tick
```

- [ ] **Step 2: Add the tool to the `telegram_bot.py` section**

In the `### telegram_bot.py — Unified Telegram bot` section, update the existing filter-management bullet:

```markdown
- **Filter management** — `manage_blacklist`, `manage_unavailable`, `manage_available` (writes to `filter_store`), `manage_filter_suspensions` (writes to `filter_suspension_store`)
```

- [ ] **Step 3: Add a "Filter suspensions" subsection under `## Filters`**

Add a new subsection immediately after the existing `### NEG-fee drafts` subsection, at the end of the `## Filters` section:

```markdown
### Filter suspensions

Any filter except `SeenFilter` can be temporarily suspended for a date range via the Telegram agent's `manage_filter_suspensions` tool, backed by `filter_suspension_store.py` (`data/filter_suspensions.json`). Suspension containment is keyed by the **gig's own date** (same model as `unavailable_periods`/`available_only_periods`), not the date the suspension was created. Period tokens support the existing formats (`YYYY-MM-DD`, `YYYY-MM-DD:YYYY-MM-DD`, `YYYY-MM`) plus two open-ended forms: `YYYY-MM-DD:` (from that date onward, never auto-expires) and `:YYYY-MM-DD` (up to and including that date, auto-expires like any closed range).

In `main.py`, each suspendable filter instance is wrapped in a `SuspendableFilter` (`filters.py`) at construction time, using a suspension snapshot loaded once per tick via `filter_suspension_store.load_active()`. Wrapping happens before the instance is used anywhere — including the direct `_fee_filter(gig)` call inside the NEG-drafts fee-partition block — so a fee suspension takes effect there too, not only inside `GigFilterChain`. `filter="all"` suspends every wrapped filter but never reaches `SeenFilter`, since it's never wrapped in the first place.
```

- [ ] **Step 4: Add the data file to the Data files table**

In the `## Data files` table, add a new row immediately after the `data/filter_config.json` row:

```markdown
| `data/filter_suspensions.json` | Runtime filter suspensions (written by `filter_suspension_store`): which filter (or `all`) is exempted for which date range |
```

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document filter suspensions in CLAUDE.md"
```

---

## Final Verification

- [ ] Run the full suite one more time: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest --tb=short -q`
- [ ] Run `ruff check .` and `ruff format --check .` across the whole repo
- [ ] Run `mypy organist_bot/` across the whole package
- [ ] Confirm all 5 tasks are committed as separate commits (`git log --oneline -6`)
