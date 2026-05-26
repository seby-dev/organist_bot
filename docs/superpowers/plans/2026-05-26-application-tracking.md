# Application Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Track every gig the bot applies to through its full lifecycle — applied → accepted / no_response / declined — with automatic transitions and a Telegram interface for querying and manual overrides.

**Architecture:** A new JSON-backed store (`application_store.py`, mirroring `filter_store.py`) is wired into three existing modules: `notifier.py` records on send, `main.py` expires stale records each tick, and `unified_agent.py` upserts to accepted on calendar add and exposes a `manage_applications` Telegram tool.

**Tech Stack:** Python, `pytest`, `unittest.mock`, `tempfile`/`monkeypatch`, existing `organist_bot` patterns.

---

## File Map

| File | Change |
|------|--------|
| `organist_bot/application_store.py` | Create — flat JSON store backed by `data/applications.json` |
| `organist_bot/notifier.py` | Modify — import store; call `record_application` in `apply_to_gig` |
| `main.py` | Modify — import store; call `expire_past_applied()` each tick |
| `organist_bot/integrations/unified_agent.py` | Modify — add `url` to `add_gig` schema; import store; `upsert_accepted` in `add_gig`; `manage_applications` tool |
| `tests/test_application_store.py` | Create — 12 unit tests for all store functions |
| `tests/test_notifier.py` | Modify — 1 new test asserting `record_application` is called |
| `tests/test_main.py` | Modify — 1 new test class asserting `expire_past_applied` called each tick |
| `tests/test_unified_agent.py` | Modify — 3 `add_gig` tests + 5 `manage_applications` tests |

---

### Task 1: `organist_bot/application_store.py`

**Files:**
- Create: `organist_bot/application_store.py`
- Create: `tests/test_application_store.py`

---

- [ ] **Step 1: Write the failing tests**

Create `tests/test_application_store.py`:

```python
"""Tests for organist_bot.application_store."""

import datetime
import json
from pathlib import Path

import pytest

import organist_bot.application_store as store
from organist_bot.models import Gig


def _make_gig(**overrides) -> Gig:
    defaults = dict(
        header="Sunday Service",
        organisation="St Paul's",
        locality="London",
        date="Sunday, 15 June 2026",
        time="10:00 AM",
        fee="£80",
        link="https://organistsonline.org/gig/123",
        email="contact@stpauls.com",
    )
    defaults.update(overrides)
    return Gig(**defaults)


@pytest.fixture(autouse=True)
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_PATH", tmp_path / "applications.json")


# ── record_application ────────────────────────────────────────────────────────


class TestRecordApplication:
    def test_record_application_writes_applied_record(self):
        gig = _make_gig()
        result = store.record_application(gig)
        assert result is True
        records = json.loads(store._PATH.read_text())
        assert len(records) == 1
        assert records[0]["url"] == "https://organistsonline.org/gig/123"
        assert records[0]["status"] == "applied"
        assert records[0]["header"] == "Sunday Service"
        assert records[0]["organisation"] == "St Paul's"
        assert records[0]["fee"] == "£80"
        assert records[0]["email"] == "contact@stpauls.com"

    def test_record_application_idempotent(self):
        gig = _make_gig()
        store.record_application(gig)
        result = store.record_application(gig)
        assert result is False
        records = json.loads(store._PATH.read_text())
        assert len(records) == 1


# ── update_status ─────────────────────────────────────────────────────────────


class TestUpdateStatus:
    def test_update_status_changes_status_and_updated_at(self):
        gig = _make_gig()
        store.record_application(gig)
        before = json.loads(store._PATH.read_text())[0]["updated_at"]
        result = store.update_status("https://organistsonline.org/gig/123", "declined")
        assert result is True
        records = json.loads(store._PATH.read_text())
        assert records[0]["status"] == "declined"
        assert records[0]["updated_at"] >= before

    def test_update_status_returns_false_when_not_found(self):
        result = store.update_status("https://unknown.com/gig/999", "declined")
        assert result is False
        assert not store._PATH.exists()


# ── upsert_accepted ───────────────────────────────────────────────────────────


class TestUpsertAccepted:
    def test_upsert_accepted_updates_existing_record(self):
        gig = _make_gig()
        store.record_application(gig)
        store.upsert_accepted(
            url="https://organistsonline.org/gig/123",
            header="Sunday Service",
            organisation="St Paul's",
            date="Sunday, 15 June 2026",
            fee="£80",
        )
        records = json.loads(store._PATH.read_text())
        assert len(records) == 1
        assert records[0]["status"] == "accepted"

    def test_upsert_accepted_creates_new_when_no_match(self):
        store.upsert_accepted(
            url="https://organistsonline.org/gig/456",
            header="Evensong",
            organisation="All Saints",
            date="2026-06-22",
            fee="£100",
        )
        records = json.loads(store._PATH.read_text())
        assert len(records) == 1
        assert records[0]["status"] == "accepted"
        assert records[0]["url"] == "https://organistsonline.org/gig/456"

    def test_upsert_accepted_creates_new_when_url_none(self):
        store.upsert_accepted(
            url=None,
            header="Manual Gig",
            organisation="St John's",
            date="2026-07-01",
            fee="£90",
        )
        records = json.loads(store._PATH.read_text())
        assert len(records) == 1
        assert records[0]["status"] == "accepted"
        assert records[0]["url"] == ""


# ── expire_past_applied ───────────────────────────────────────────────────────


class TestExpirePastApplied:
    def _add_applied(self, url: str, date: str) -> None:
        store.record_application(_make_gig(link=url, date=date))

    def test_expire_past_applied_marks_old_records(self):
        # 2020-01-01 is unambiguously in the past
        self._add_applied("https://organistsonline.org/gig/1", "Sunday, 1 January 2020")
        changed = store.expire_past_applied()
        assert changed == 1
        records = json.loads(store._PATH.read_text())
        assert records[0]["status"] == "no_response"

    def test_expire_past_applied_leaves_future_records(self):
        # 2099-12-31 is unambiguously in the future
        self._add_applied("https://organistsonline.org/gig/1", "Sunday, 31 December 2099")
        changed = store.expire_past_applied()
        assert changed == 0
        records = json.loads(store._PATH.read_text())
        assert records[0]["status"] == "applied"

    def test_expire_past_applied_leaves_non_applied_records(self):
        self._add_applied("https://organistsonline.org/gig/1", "Sunday, 1 January 2020")
        store.update_status("https://organistsonline.org/gig/1", "accepted")
        changed = store.expire_past_applied()
        assert changed == 0
        records = json.loads(store._PATH.read_text())
        assert records[0]["status"] == "accepted"

    def test_expire_returns_count_of_changed_records(self):
        self._add_applied("https://organistsonline.org/gig/1", "Sunday, 1 January 2020")
        self._add_applied("https://organistsonline.org/gig/2", "Sunday, 8 January 2020")
        self._add_applied(
            "https://organistsonline.org/gig/3", "Sunday, 31 December 2099"
        )  # future — unchanged
        changed = store.expire_past_applied()
        assert changed == 2


# ── list_applications ─────────────────────────────────────────────────────────


class TestListApplications:
    def test_list_applications_filters_by_days(self):
        gig = _make_gig()
        store.record_application(gig)
        # Back-date applied_at to 60 days ago so it falls outside a 30-day window
        data = json.loads(store._PATH.read_text())
        old_ts = (
            datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=60)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        data[0]["applied_at"] = old_ts
        store._PATH.write_text(json.dumps(data, indent=2) + "\n")

        assert store.list_applications(days=30) == []
        assert len(store.list_applications(days=61)) == 1
```

- [ ] **Step 2: Run the new tests to confirm they fail**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_application_store.py --tb=short -q
```

Expected: `ModuleNotFoundError` or `ImportError` — `application_store` does not exist yet.

- [ ] **Step 3: Implement `organist_bot/application_store.py`**

Create `organist_bot/application_store.py`:

```python
"""organist_bot/application_store.py
──────────────────────────────────────────────────
Track every gig application through its lifecycle.
Backed by data/applications.json — a flat JSON array, one object per application.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import tempfile
from pathlib import Path

from organist_bot.models import Gig

logger = logging.getLogger(__name__)

_PATH = Path("data/applications.json")


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read() -> list[dict]:
    if not _PATH.exists():
        return []
    try:
        return json.loads(_PATH.read_text())
    except Exception:
        logger.exception("application_store: failed to read %s", _PATH)
        return []


def _write(records: list[dict]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(_PATH.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(records, f, indent=2)
            f.write("\n")
        os.replace(tmp, str(_PATH))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def record_application(gig: Gig) -> bool:
    """Write a new 'applied' record. Returns False if URL already exists (idempotent)."""
    records = _read()
    if any(r["url"] == gig.link for r in records):
        return False
    now = _now_iso()
    records.append(
        {
            "url": gig.link,
            "header": gig.header,
            "organisation": gig.organisation or "",
            "date": gig.date,
            "fee": gig.fee or "",
            "email": gig.email or "",
            "status": "applied",
            "applied_at": now,
            "updated_at": now,
        }
    )
    _write(records)
    return True


def update_status(url: str, status: str) -> bool:
    """Update status and updated_at for the record with the given URL. Returns False if not found."""
    records = _read()
    for r in records:
        if r["url"] == url:
            r["status"] = status
            r["updated_at"] = _now_iso()
            _write(records)
            return True
    return False


def upsert_accepted(
    url: str | None,
    header: str,
    organisation: str,
    date: str,
    fee: str,
) -> None:
    """Create or update a record to 'accepted'.

    If url is given and matches an existing record, updates it in place.
    Otherwise creates a new 'accepted' record (url may be None for manual entries).
    """
    records = _read()
    now = _now_iso()
    if url is not None:
        for r in records:
            if r["url"] == url:
                r["status"] = "accepted"
                r["updated_at"] = now
                _write(records)
                return
    records.append(
        {
            "url": url or "",
            "header": header,
            "organisation": organisation,
            "date": date,
            "fee": fee,
            "email": "",
            "status": "accepted",
            "applied_at": now,
            "updated_at": now,
        }
    )
    _write(records)


def expire_past_applied() -> int:
    """Mark all 'applied' records whose date < today as 'no_response'. Returns count changed."""
    from organist_bot.filters import normalize_to_yyyymmdd

    today = datetime.date.today()
    records = _read()
    changed = 0
    now = _now_iso()
    for r in records:
        if r["status"] != "applied":
            continue
        normalized = normalize_to_yyyymmdd(r["date"])
        if normalized is None:
            continue
        try:
            gig_date = datetime.datetime.strptime(normalized, "%Y%m%d").date()
        except ValueError:
            continue
        if gig_date < today:
            r["status"] = "no_response"
            r["updated_at"] = now
            changed += 1
    if changed:
        _write(records)
    return changed


def list_applications(days: int = 30) -> list[dict]:
    """Return all records with applied_at within the last N days, newest first."""
    records = _read()
    cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days)
    result = []
    for r in records:
        try:
            applied_at = datetime.datetime.fromisoformat(
                r["applied_at"].replace("Z", "+00:00")
            )
        except Exception:
            continue
        if applied_at >= cutoff:
            result.append(r)
    result.sort(key=lambda r: r["applied_at"], reverse=True)
    return result
```

- [ ] **Step 4: Run the tests to confirm they pass**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_application_store.py --tb=short -q
```

Expected: 12 passed.

- [ ] **Step 5: Commit**

```bash
git add organist_bot/application_store.py tests/test_application_store.py
git commit -m "feat: add application_store — JSON-backed gig application tracker"
```

---

### Task 2: Wire `notifier.py` → `record_application`

**Files:**
- Modify: `organist_bot/notifier.py` (lines ~1–10 imports, lines 170–193 `apply_to_gig`)
- Modify: `tests/test_notifier.py` (add 1 test near the end)

---

- [ ] **Step 1: Write the failing test**

Open `tests/test_notifier.py`. At the end of the file (after the last existing test class), add:

```python
# ── apply_to_gig records application ─────────────────────────────────────────


class TestApplyToGigRecordsApplication:
    def test_apply_to_gig_records_application(self):
        """apply_to_gig must call record_application once with the gig."""
        settings = _make_settings()
        transport = FakeTransport()
        notifier = Notifier(settings, transport)
        gig = _make_gig(email="test@church.com")
        with patch(
            "organist_bot.notifier.application_store"
        ) as mock_store:
            mock_store.record_application.return_value = True
            notifier.apply_to_gig(gig)
        mock_store.record_application.assert_called_once_with(gig)
```

(The existing `_make_settings`, `_make_gig`, `FakeTransport`, and `Notifier` imports are already in the file — do not re-import them.)

- [ ] **Step 2: Run the new test to confirm it fails**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_notifier.py::TestApplyToGigRecordsApplication --tb=short -q
```

Expected: `AttributeError` — `application_store` not imported in `notifier.py`.

- [ ] **Step 3: Implement the change in `notifier.py`**

In `organist_bot/notifier.py`, add the import at the top (with the other `organist_bot` imports):

```python
import organist_bot.application_store as application_store
```

Then in `apply_to_gig`, after the `self._dispatch(...)` call and **before** the end of the method, add:

```python
        try:
            application_store.record_application(gig)
        except Exception:
            logger.warning(
                "application_store: record_application failed",
                extra={"link": gig.link},
                exc_info=True,
            )
```

The full method after the change should look like:

```python
    def apply_to_gig(self, gig: Gig) -> None:
        """Send an application email directly to the gig's contact."""
        if not gig.email:
            logger.warning(
                "Application skipped — no contact email",
                extra={"header": gig.header, "date": gig.date, "org": gig.organisation},
            )
            return

        body = self._render(
            "application.html.j2",
            gig=gig,
            applicant_name=self._settings.applicant_name,
            applicant_mobile=self._settings.applicant_mobile,
            applicant_video_1=self._settings.applicant_video_1,
            applicant_video_2=self._settings.applicant_video_2,
        )
        cc = [self._settings.cc_email] if self._settings.cc_email else None
        self._dispatch(
            subject=f"Application for Organist Position – {gig.date}",
            body=body,
            recipient=gig.email,
            cc=cc,
        )
        try:
            application_store.record_application(gig)
        except Exception:
            logger.warning(
                "application_store: record_application failed",
                extra={"link": gig.link},
                exc_info=True,
            )
```

- [ ] **Step 4: Run the test to confirm it passes**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_notifier.py --tb=short -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add organist_bot/notifier.py tests/test_notifier.py
git commit -m "feat: record application in store after apply_to_gig sends"
```

---

### Task 3: Wire `main.py` → `expire_past_applied`

**Files:**
- Modify: `main.py` (imports section ~line 1–35; end of `_run` ~line 272)
- Modify: `tests/test_main.py` (add new test class)

---

- [ ] **Step 1: Write the failing test**

Open `tests/test_main.py`. Find the end of the file and add this class. The `_make_minimal_settings` helper is defined per-class in the existing tests — define it again here:

```python
# ── expire_past_applied called each tick ──────────────────────────────────────


class TestExpirePastApplied:
    def _make_minimal_settings(self):
        s = MagicMock()
        s.target_url = "https://organistsonline.org/required/"
        s.min_fee = 100
        s.poll_minutes = 2
        s.enable_seen_filter = False
        s.enable_fee_filter = False
        s.enable_sunday_time_filter = False
        s.enable_blacklist_filter = False
        s.enable_booked_date_filter = False
        s.enable_postcode_filter = False
        s.enable_calendar_filter = False
        s.enable_availability_filter = False
        s.email_password = "pass"
        return s

    def test_expire_past_applied_called_each_tick(self):
        """expire_past_applied must be called once per _run, even when no gigs are found."""
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
            patch("main.application_store") as mock_store,
        ):
            mock_store.expire_past_applied.return_value = 0
            main_module.main(mock_scraper)

        mock_store.expire_past_applied.assert_called_once()
```

- [ ] **Step 2: Run the new test to confirm it fails**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_main.py::TestExpirePastApplied --tb=short -q
```

Expected: `AttributeError` — `application_store` not in `main` module yet.

- [ ] **Step 3: Implement the change in `main.py`**

In `main.py`, add the import near the top (after the existing `organist_bot` imports):

```python
import organist_bot.application_store as application_store
```

Then in `_run`, after the `if valid_gigs: ... else: logger.info("No new gigs...")` block and **before** the `# ── Run summary` comment, add:

```python
    try:
        expired = application_store.expire_past_applied()
        if expired > 0:
            logger.info(
                "Expired past applications as no_response", extra={"count": expired}
            )
    except Exception:
        logger.warning("application_store: expire_past_applied failed", exc_info=True)
```

The relevant section of `_run` after the change should look like:

```python
    else:
        logger.info("No new gigs passed the filters — notifications skipped")

    try:
        expired = application_store.expire_past_applied()
        if expired > 0:
            logger.info(
                "Expired past applications as no_response", extra={"count": expired}
            )
    except Exception:
        logger.warning("application_store: expire_past_applied failed", exc_info=True)

    # ── Run summary ───────────────────────────────────────────────────────────
    logger.info(
        "Run summary",
        ...
    )
```

- [ ] **Step 4: Run the test to confirm it passes**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_main.py::TestExpirePastApplied --tb=short -q
```

Expected: 1 passed.

- [ ] **Step 5: Run the full `test_main.py` to check for regressions**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_main.py --tb=short -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add main.py tests/test_main.py
git commit -m "feat: expire past applications as no_response each scheduler tick"
```

---

### Task 4: Wire `add_gig` in `unified_agent.py` → `upsert_accepted`

**Files:**
- Modify: `organist_bot/integrations/unified_agent.py` (imports ~line 1–26; `add_gig` tool schema ~line 96–108; `add_gig` handler ~line 573–599)
- Modify: `tests/test_unified_agent.py` (add `TestAddGigApplicationStore` class)

---

- [ ] **Step 1: Write the failing tests**

Open `tests/test_unified_agent.py`. After the existing `TestAddGigAutoUnavailable` class, add:

```python
# ── add_gig → application_store.upsert_accepted ───────────────────────────────


class TestAddGigApplicationStore:
    @pytest.mark.asyncio
    async def test_add_gig_url_match_updates_to_accepted(self):
        """When url is provided, upsert_accepted is called with that url."""
        input_data = {
            **_GIG_INPUT_BASE,
            "confirmed": True,
            "url": "https://organistsonline.org/gig/1",
        }
        with (
            patch(
                "organist_bot.integrations.unified_agent._make_calendar_client"
            ) as mock_factory,
            patch(
                "organist_bot.integrations.unified_agent.application_store"
            ) as mock_store,
        ):
            mock_cal = MagicMock()
            mock_cal.add_gig.return_value = "evt_abc"
            mock_factory.return_value = mock_cal
            await _execute_tool("add_gig", input_data, CHAT_ID)
        mock_store.upsert_accepted.assert_called_once_with(
            url="https://organistsonline.org/gig/1",
            header="Sunday Service",
            organisation="St Mary's",
            date="Sunday 1st June 2025",
            fee="£150",
        )

    @pytest.mark.asyncio
    async def test_add_gig_url_no_match_creates_accepted(self):
        """upsert_accepted is called with url even when no prior record exists."""
        input_data = {
            **_GIG_INPUT_BASE,
            "confirmed": True,
            "url": "https://organistsonline.org/gig/99",
        }
        with (
            patch(
                "organist_bot.integrations.unified_agent._make_calendar_client"
            ) as mock_factory,
            patch(
                "organist_bot.integrations.unified_agent.application_store"
            ) as mock_store,
        ):
            mock_cal = MagicMock()
            mock_cal.add_gig.return_value = "evt_abc"
            mock_factory.return_value = mock_cal
            await _execute_tool("add_gig", input_data, CHAT_ID)
        mock_store.upsert_accepted.assert_called_once_with(
            url="https://organistsonline.org/gig/99",
            header="Sunday Service",
            organisation="St Mary's",
            date="Sunday 1st June 2025",
            fee="£150",
        )

    @pytest.mark.asyncio
    async def test_add_gig_manual_entry_creates_accepted(self):
        """When no url is provided (manual entry), upsert_accepted is called with url=None."""
        input_data = {**_GIG_INPUT_BASE, "confirmed": True}  # no "url" key
        with (
            patch(
                "organist_bot.integrations.unified_agent._make_calendar_client"
            ) as mock_factory,
            patch(
                "organist_bot.integrations.unified_agent.application_store"
            ) as mock_store,
        ):
            mock_cal = MagicMock()
            mock_cal.add_gig.return_value = "evt_abc"
            mock_factory.return_value = mock_cal
            await _execute_tool("add_gig", input_data, CHAT_ID)
        mock_store.upsert_accepted.assert_called_once_with(
            url=None,
            header="Sunday Service",
            organisation="St Mary's",
            date="Sunday 1st June 2025",
            fee="£150",
        )
```

- [ ] **Step 2: Run the new tests to confirm they fail**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_unified_agent.py::TestAddGigApplicationStore --tb=short -q
```

Expected: 3 failures — `application_store` not imported and not called.

- [ ] **Step 3: Implement the changes in `unified_agent.py`**

**3a.** Add the import at the top of `organist_bot/integrations/unified_agent.py` (with the other `organist_bot` imports):

```python
import organist_bot.application_store as application_store
```

**3b.** In the `TOOLS` list, find the `add_gig` tool's `input_schema.properties` object (around line 98). Add a `url` property:

```python
"url": {
    "type": "string",
    "description": "Source gig URL from fetch_gig_details. Omit for manual entries.",
},
```

The properties block after the change:

```python
"properties": {
    "confirmed": {"type": "boolean"},
    "header": {"type": "string"},
    "organisation": {"type": "string"},
    "locality": {"type": "string"},
    "date": {"type": "string", "description": "e.g. 'Sunday 1st June 2025'"},
    "time": {"type": "string", "description": "e.g. '10:30am'"},
    "fee": {"type": "string", "description": "e.g. '£150'"},
    "url": {
        "type": "string",
        "description": "Source gig URL from fetch_gig_details. Omit for manual entries.",
    },
},
```

**3c.** In the `add_gig` handler (the `if name == "add_gig":` block, around line 552), after the line `event_id = cal.add_gig(gig)` succeeds and before the `return json.dumps(...)` at the end of the `try` block, add:

```python
            url = input_data.get("url") or None
            try:
                application_store.upsert_accepted(
                    url=url,
                    header=fields["header"],
                    organisation=fields.get("organisation", ""),
                    date=fields["date"],
                    fee=fields["fee"] if fields["fee"] != "not specified" else "",
                )
            except Exception:
                logger.warning(
                    "add_gig: upsert_accepted failed",
                    extra={"url": url},
                    exc_info=True,
                )
```

The relevant portion of the `add_gig` confirmed handler after the change:

```python
        try:
            cal = _make_calendar_client()
            if cal is None:
                return json.dumps({"error": "Google Calendar not configured."})
            gig = Gig(
                header=fields["header"],
                organisation=fields["organisation"],
                locality=fields["locality"],
                date=fields["date"],
                time=fields["time"],
                fee=fields["fee"] if fields["fee"] != "not specified" else None,
                link="",
            )
            event_id = cal.add_gig(gig)
            yyyymmdd = normalize_to_yyyymmdd(fields["date"])
            if yyyymmdd:
                try:
                    date_str = datetime.datetime.strptime(yyyymmdd, "%Y%m%d").strftime("%Y-%m-%d")
                    filter_store.add_period("unavailable_periods", date_str)
                except Exception:
                    logger.warning(
                        "Failed to add gig date to unavailable periods",
                        extra={"date": fields["date"]},
                    )
            url = input_data.get("url") or None
            try:
                application_store.upsert_accepted(
                    url=url,
                    header=fields["header"],
                    organisation=fields.get("organisation", ""),
                    date=fields["date"],
                    fee=fields["fee"] if fields["fee"] != "not specified" else "",
                )
            except Exception:
                logger.warning(
                    "add_gig: upsert_accepted failed",
                    extra={"url": url},
                    exc_info=True,
                )
            return json.dumps({"result": f"Added to calendar. Event ID: {event_id}"})
        except Exception as exc:
            return json.dumps({"error": str(exc)})
```

- [ ] **Step 4: Run the new tests to confirm they pass**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_unified_agent.py::TestAddGigApplicationStore --tb=short -q
```

Expected: 3 passed.

- [ ] **Step 5: Run the full `test_unified_agent.py` to check for regressions**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_unified_agent.py --tb=short -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add organist_bot/integrations/unified_agent.py tests/test_unified_agent.py
git commit -m "feat: upsert application to accepted when gig added to calendar"
```

---

### Task 5: `manage_applications` tool in `unified_agent.py`

**Files:**
- Modify: `organist_bot/integrations/unified_agent.py` (SYSTEM_PROMPT, TOOLS, per-chat state, `_VERBATIM_RESPONSE_TOOLS`, `_execute_tool`, `reset_conversation`)
- Modify: `tests/test_unified_agent.py` (add `TestManageApplications` class)

---

- [ ] **Step 1: Write the failing tests**

Open `tests/test_unified_agent.py`. After the `TestAddGigApplicationStore` class, add:

```python
# ── manage_applications ───────────────────────────────────────────────────────


def _make_app_record(**overrides) -> dict:
    defaults = {
        "url": "https://organistsonline.org/gig/1",
        "header": "Sunday Service",
        "organisation": "St Mary's",
        "date": "Sunday, 15 June 2026",
        "fee": "£80",
        "status": "applied",
        "applied_at": "2026-05-01T10:00:00Z",
        "updated_at": "2026-05-01T10:00:00Z",
    }
    defaults.update(overrides)
    return defaults


class TestManageApplications:
    @pytest.mark.asyncio
    async def test_summary_returns_status_counts(self):
        records = [
            _make_app_record(status="accepted"),
            _make_app_record(url="u2", status="applied"),
            _make_app_record(url="u3", status="no_response"),
        ]
        with patch(
            "organist_bot.integrations.unified_agent.application_store"
        ) as mock_store:
            mock_store.list_applications.return_value = records
            result = await _execute_tool(
                "manage_applications", {"action": "summary"}, CHAT_ID
            )
        assert "Accepted" in result
        assert "Pending" in result
        assert "No response" in result

    @pytest.mark.asyncio
    async def test_list_returns_numbered_entries_with_emoji(self):
        records = [_make_app_record(status="accepted")]
        with patch(
            "organist_bot.integrations.unified_agent.application_store"
        ) as mock_store:
            mock_store.list_applications.return_value = records
            result = await _execute_tool(
                "manage_applications", {"action": "list"}, CHAT_ID
            )
        assert "Sunday Service" in result
        assert "St Mary's" in result
        assert "✅" in result
        assert "1." in result

    @pytest.mark.asyncio
    async def test_list_empty_returns_no_applications_message(self):
        with patch(
            "organist_bot.integrations.unified_agent.application_store"
        ) as mock_store:
            mock_store.list_applications.return_value = []
            result = await _execute_tool(
                "manage_applications", {"action": "list"}, CHAT_ID
            )
        data = json.loads(result)
        assert "result" in data

    @pytest.mark.asyncio
    async def test_update_changes_status_via_cached_listing(self):
        records = [_make_app_record(status="applied")]
        with patch(
            "organist_bot.integrations.unified_agent.application_store"
        ) as mock_store:
            mock_store.list_applications.return_value = records
            mock_store.update_status.return_value = True
            # populate the listing cache first
            await _execute_tool("manage_applications", {"action": "list"}, CHAT_ID)
            result = await _execute_tool(
                "manage_applications",
                {"action": "update", "number": 1, "status": "declined"},
                CHAT_ID,
            )
        mock_store.update_status.assert_called_once_with(
            "https://organistsonline.org/gig/1", "declined"
        )
        data = json.loads(result)
        assert "result" in data

    @pytest.mark.asyncio
    async def test_update_no_listing_cached_returns_error(self):
        from organist_bot.integrations.unified_agent import _last_application_listing

        _last_application_listing.pop(CHAT_ID, None)
        with patch(
            "organist_bot.integrations.unified_agent.application_store"
        ) as mock_store:
            mock_store.list_applications.return_value = []
            result = await _execute_tool(
                "manage_applications",
                {"action": "update", "number": 1, "status": "declined"},
                CHAT_ID,
            )
        data = json.loads(result)
        assert "error" in data
```

- [ ] **Step 2: Run the new tests to confirm they fail**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_unified_agent.py::TestManageApplications --tb=short -q
```

Expected: failures — `manage_applications` handler and `_last_application_listing` do not exist.

- [ ] **Step 3: Implement the changes in `unified_agent.py`**

**3a.** In `SYSTEM_PROMPT`, after the `## Runtime config` section and before `## Conversation`, add:

```python
## Application tracking
- "What applications are pending?" / "show my applications" → manage_applications(action=list).
- "Application summary" / "how many gigs have I applied to?" → manage_applications(action=summary).
- "Mark application 2 as declined" → manage_applications(action=update, number=2, status=declined).
- Valid statuses for update: applied, accepted, no_response, declined.

```

**3b.** In the `TOOLS` list, after the `manage_config` tool entry and **before** the closing `]`, add:

```python
    # ── Application tracking ────────────────────────────────────────────────
    {
        "name": "manage_applications",
        "description": (
            "Query or update gig application tracking. "
            "'summary' returns status counts for the last N days. "
            "'list' returns a numbered listing (most recent first). "
            "'update' changes the status of an application by its number from the last list call. "
            "Valid statuses: applied, accepted, no_response, declined."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["summary", "list", "update"],
                    "description": "summary=status counts, list=numbered listing, update=change status",
                },
                "days": {
                    "type": "integer",
                    "description": "Lookback window in days for summary/list (default 30).",
                },
                "number": {
                    "type": "integer",
                    "description": "1-based position from the last list call. Required for update.",
                },
                "status": {
                    "type": "string",
                    "enum": ["applied", "accepted", "no_response", "declined"],
                    "description": "New status. Required for update.",
                },
            },
            "required": ["action"],
        },
    },
```

**3c.** In the per-chat state section (around line 416–418), add:

```python
_last_application_listing: dict[int, list[dict]] = {}
```

**3d.** In `_VERBATIM_RESPONSE_TOOLS` (line 421), add `"manage_applications"`:

```python
_VERBATIM_RESPONSE_TOOLS = {"list_upcoming_gigs", "get_gig_stats", "manage_config", "manage_applications"}
```

**3e.** Add a module-level helper `_fmt_application_date` near the other helpers (after `_resolve_period`):

```python
def _fmt_application_date(date_str: str) -> str:
    """Format a gig date string as 'D Mon' (e.g. '15 Jun') for application listings."""
    yyyymmdd = normalize_to_yyyymmdd(date_str)
    if yyyymmdd:
        try:
            dt = datetime.datetime.strptime(yyyymmdd, "%Y%m%d")
            return f"{dt.day} {dt.strftime('%b')}"
        except ValueError:
            pass
    return date_str
```

**3f.** In `_execute_tool`, after the `manage_available` block (around line 970) and **before** the `clear_conversation` block, add:

```python
    # ── manage_applications ──────────────────────────────────────────────────
    if name == "manage_applications":
        action = input_data.get("action", "summary")
        days = input_data.get("days", 30)
        records = application_store.list_applications(days)

        if action == "summary":
            counts = {
                "accepted": sum(1 for r in records if r["status"] == "accepted"),
                "applied": sum(1 for r in records if r["status"] == "applied"),
                "no_response": sum(1 for r in records if r["status"] == "no_response"),
                "declined": sum(1 for r in records if r["status"] == "declined"),
            }
            total = len(records)
            lines = [
                f"📋 Applications — last {days} days",
                "",
                f"Applied:      {total}",
                f"Accepted:     {counts['accepted']}",
                f"No response:  {counts['no_response']}",
                f"Declined:     {counts['declined']}",
                f"Pending:      {counts['applied']}",
            ]
            return json.dumps({"result": "\n".join(lines)})

        if action == "list":
            if not records:
                return json.dumps(
                    {"result": f"No applications in the last {days} days."}
                )
            _last_application_listing[chat_id] = records
            _status_emoji = {
                "accepted": "✅",
                "applied": "⏳",
                "no_response": "🔕",
                "declined": "❌",
            }
            lines = [f"📋 Applications — last {days} days", ""]
            for i, r in enumerate(records, start=1):
                emoji = _status_emoji.get(r["status"], "❓")
                org_part = f" — {r['organisation']}" if r.get("organisation") else ""
                date_part = _fmt_application_date(r.get("date", ""))
                fee_part = f"  {r['fee']}" if r.get("fee") else ""
                lines.append(f"{i}. {emoji} {r['header']}{org_part}  ({date_part}){fee_part}")
            return json.dumps({"result": "\n".join(lines)})

        if action == "update":
            n = input_data.get("number")
            status = input_data.get("status")
            listing = _last_application_listing.get(chat_id)
            if not listing:
                return json.dumps(
                    {"error": "No application listing cached. Ask to list applications first."}
                )
            if n is None or n < 1 or n > len(listing):
                return json.dumps({"error": f"No application number {n}."})
            record = listing[n - 1]
            url = record.get("url", "")
            if not url:
                return json.dumps({"error": "Cannot update a manual entry with no URL."})
            ok = application_store.update_status(url, status)
            if ok:
                listing[n - 1]["status"] = status
                return json.dumps({"result": f"Updated application {n} to '{status}'."})
            return json.dumps({"error": "Application not found in store."})

        return json.dumps({"error": f"Unknown action: {action}"})

```

**3g.** In `reset_conversation` (at the end of the file), add the `_last_application_listing` cleanup:

```python
def reset_conversation(chat_id: int) -> None:
    _histories.pop(chat_id, None)
    _last_invoice.pop(chat_id, None)
    _last_gig_listing.pop(chat_id, None)
    _last_application_listing.pop(chat_id, None)
```

- [ ] **Step 4: Run the new tests to confirm they pass**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_unified_agent.py::TestManageApplications --tb=short -q
```

Expected: 5 passed.

- [ ] **Step 5: Run the full test suite**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest --tb=short -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add organist_bot/integrations/unified_agent.py tests/test_unified_agent.py
git commit -m "feat: add manage_applications Telegram tool for application tracking"
```
