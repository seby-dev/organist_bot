# Pipeline Anomaly Alerts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Send Telegram alerts when `gig_errors >= 2` in a single scraping run, or when a Google Calendar, Google Maps, or Google Sheets API call fails in a way that affects pipeline correctness or data integrity.

**Architecture:** A new `organist_bot/alert.py` module provides `send_alert(message)` — a fire-and-forget Telegram POST. `main.py` replaces its inline `_send_telegram_alert` with an import of this function and adds a parse-error-rate check. Three existing `except` blocks in `calendar_client.py`, `sheets_logger.py`, and `filters.py` each gain a `send_alert` call after their existing `logger.warning`.

**Tech Stack:** `requests` (already a dependency), `unittest.mock.patch`.

---

## File Structure

| File | Change |
|------|--------|
| `organist_bot/alert.py` | New — `send_alert(message: str) -> None` |
| `main.py` | Replace `_send_telegram_alert`; add parse-error-rate check post-scrape |
| `organist_bot/integrations/calendar_client.py` | Add `alert.send_alert` in `has_event_on_date` except block |
| `organist_bot/integrations/sheets_logger.py` | Add `alert.send_alert` in `drain()` outer except block |
| `organist_bot/filters.py` | Add `alert.send_alert` in `PostcodeFilter._drive_time` except block |
| `tests/test_alert.py` | New — tests for `send_alert` |
| `tests/test_main.py` | Add parse-error alert test |
| `tests/test_filters.py` | Add PostcodeFilter alert test |

---

### Task 1: `organist_bot/alert.py`

**Files:**
- Create: `organist_bot/alert.py`
- Create: `tests/test_alert.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_alert.py`:

```python
# tests/test_alert.py
"""Tests for organist_bot.alert.send_alert."""

from unittest.mock import MagicMock, patch

import pytest

from organist_bot.alert import send_alert


class TestSendAlert:
    def test_posts_to_telegram_when_configured(self):
        """Sends a POST to the Telegram Bot API with the correct payload."""
        mock_post = MagicMock()
        with (
            patch("organist_bot.alert.settings") as mock_settings,
            patch("organist_bot.alert._requests.post", mock_post),
        ):
            mock_settings.telegram_bot_token = "TOKEN123"
            mock_settings.telegram_chat_id = 42
            send_alert("test message")

        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        assert "TOKEN123" in call_kwargs.args[0]
        assert call_kwargs.kwargs["json"]["text"] == "test message"
        assert call_kwargs.kwargs["json"]["chat_id"] == 42
        assert call_kwargs.kwargs["timeout"] == 10

    def test_no_op_when_token_missing(self):
        """Does nothing (no POST) when telegram_bot_token is not set."""
        mock_post = MagicMock()
        with (
            patch("organist_bot.alert.settings") as mock_settings,
            patch("organist_bot.alert._requests.post", mock_post),
        ):
            mock_settings.telegram_bot_token = ""
            mock_settings.telegram_chat_id = 42
            send_alert("test message")

        mock_post.assert_not_called()

    def test_no_op_when_chat_id_missing(self):
        """Does nothing when telegram_chat_id is not set."""
        mock_post = MagicMock()
        with (
            patch("organist_bot.alert.settings") as mock_settings,
            patch("organist_bot.alert._requests.post", mock_post),
        ):
            mock_settings.telegram_bot_token = "TOKEN"
            mock_settings.telegram_chat_id = None
            send_alert("test message")

        mock_post.assert_not_called()

    def test_network_failure_is_swallowed(self):
        """A network error during POST does not propagate."""
        mock_post = MagicMock(side_effect=ConnectionError("timeout"))
        with (
            patch("organist_bot.alert.settings") as mock_settings,
            patch("organist_bot.alert._requests.post", mock_post),
        ):
            mock_settings.telegram_bot_token = "TOKEN"
            mock_settings.telegram_chat_id = 42
            send_alert("test message")  # must not raise
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_alert.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'organist_bot.alert'`

- [ ] **Step 3: Implement `organist_bot/alert.py`**

Create `organist_bot/alert.py`:

```python
"""organist_bot/alert.py
────────────────────────
Fire-and-forget Telegram alerts for pipeline anomalies.

send_alert() posts a plain-text message to the configured Telegram chat.
It never raises — all failures are logged at WARNING so the pipeline
continues regardless of alert delivery status.
"""

import logging
import time

import requests as _requests

from organist_bot.config import settings

logger = logging.getLogger(__name__)


def send_alert(message: str) -> None:
    """Post a plain-text alert to the configured Telegram chat.

    No-op if telegram_bot_token or telegram_chat_id is not configured.
    Any network or API failure is caught and logged at WARNING.
    """
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.debug("send_alert: Telegram not configured — skipping")
        return
    t0 = time.perf_counter()
    try:
        _requests.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json={"chat_id": settings.telegram_chat_id, "text": message},
            timeout=10,
        )
        logger.info(
            "Telegram alert sent",
            extra={"elapsed_ms": int((time.perf_counter() - t0) * 1000)},
        )
    except Exception as exc:
        logger.warning(
            "Telegram alert failed",
            extra={"error": str(exc), "elapsed_ms": int((time.perf_counter() - t0) * 1000)},
        )
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_alert.py -v
```

Expected: 4 PASSED

- [ ] **Step 5: Commit**

```bash
git add organist_bot/alert.py tests/test_alert.py
git commit -m "feat: add alert.send_alert for fire-and-forget Telegram alerts"
```

---

### Task 2: Wire `alert.send_alert` into `main.py`

**Files:**
- Modify: `main.py`
- Modify: `tests/test_main.py`

- [ ] **Step 1: Write the failing test**

Open `tests/test_main.py`. Add this test (you'll need to find the existing test class structure and add it there, or append a new class):

```python
class TestParseErrorAlert:
    def test_alert_sent_when_gig_errors_ge_2(self):
        """send_alert is called when gig_errors >= 2 after scraping."""
        from unittest.mock import MagicMock, call, patch

        import main as main_module

        # Minimal mock scraper that "finds" 5 gigs but fails to parse 2 of them.
        mock_scraper = MagicMock()
        mock_scraper.fetch.return_value = "<html/>"
        mock_scraper.parse_gig_listings.return_value = [MagicMock()] * 5

        parse_error = Exception("parse failure")

        call_count = 0

        def fake_extract(el):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise parse_error
            return {
                "header": "Test",
                "date": "Sunday 1st June 2025",
                "link": "https://example.com/1",
                "fee": "£100",
            }

        mock_scraper.extract_basic_details.side_effect = fake_extract

        with (
            patch("main.alert") as mock_alert,
            patch("main.load_seen_gigs", return_value=set()),
            patch("main.load_listings_hash", return_value="different"),
            patch("main.save_listings_hash"),
            patch("main.save_seen_gigs"),
            patch("main.filter_store"),
            patch("main.GigFilterChain") as mock_chain_cls,
        ):
            mock_chain = MagicMock()
            mock_chain.is_valid.return_value = False
            mock_chain.apply.return_value = []
            mock_chain_cls.return_value = mock_chain

            main_module.main(mock_scraper)

        mock_alert.send_alert.assert_called_once()
        alert_msg = mock_alert.send_alert.call_args.args[0]
        assert "⚠️" in alert_msg
        assert "parse" in alert_msg.lower() or "error" in alert_msg.lower()

    def test_no_alert_when_gig_errors_lt_2(self):
        """send_alert is NOT called when gig_errors < 2."""
        from unittest.mock import MagicMock, patch

        import main as main_module

        mock_scraper = MagicMock()
        mock_scraper.fetch.return_value = "<html/>"
        mock_scraper.parse_gig_listings.return_value = [MagicMock()]

        call_count = 0

        def fake_extract(el):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("one error")
            return {"header": "T", "date": "2025-06-01", "link": "https://x.com/1", "fee": "£100"}

        mock_scraper.extract_basic_details.side_effect = fake_extract

        with (
            patch("main.alert") as mock_alert,
            patch("main.load_seen_gigs", return_value=set()),
            patch("main.load_listings_hash", return_value="different"),
            patch("main.save_listings_hash"),
            patch("main.save_seen_gigs"),
            patch("main.filter_store"),
            patch("main.GigFilterChain") as mock_chain_cls,
        ):
            mock_chain = MagicMock()
            mock_chain.is_valid.return_value = False
            mock_chain.apply.return_value = []
            mock_chain_cls.return_value = mock_chain
            main_module.main(mock_scraper)

        mock_alert.send_alert.assert_not_called()
```

- [ ] **Step 2: Run test to confirm it fails**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_main.py::TestParseErrorAlert -v
```

Expected: FAIL — `main` has no `alert` attribute

- [ ] **Step 3: Update `main.py`**

**3a.** Add the import at the top of `main.py`, alongside the other `organist_bot` imports:

```python
from organist_bot import alert
```

**3b.** Remove the `_send_telegram_alert` function (lines 37–60 in the current file). Replace every call to `_send_telegram_alert(...)` in the `__main__` block with `alert.send_alert(...)`. Currently there is one call:

```python
# OLD:
_send_telegram_alert("❌ OrganistBot crashed — check logs.")
# NEW:
alert.send_alert("❌ OrganistBot crashed — check logs.")
```

**3c.** Add the parse-error-rate check in `main()`, immediately after the `logger.info("Scraping complete", ...)` call and before `pre_filter.log_and_reset_counts(...)` (around line 180 after your edits). Insert:

```python
    if gig_errors >= 2:
        alert.send_alert(
            f"⚠️ Parse errors in run {run_id}: {gig_errors} gig(s) failed to parse "
            f"out of {len(gigs_div)} listed. Check logs for detail."
        )
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_main.py::TestParseErrorAlert -v
```

Expected: 2 PASSED

- [ ] **Step 5: Run full test suite**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest --tb=short -q
```

Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add main.py tests/test_main.py
git commit -m "feat: wire alert module into main.py, add parse-error-rate alert"
```

---

### Task 3: API failure alerts in `calendar_client.py`, `sheets_logger.py`, `filters.py`

**Files:**
- Modify: `organist_bot/integrations/calendar_client.py`
- Modify: `organist_bot/integrations/sheets_logger.py`
- Modify: `organist_bot/filters.py`
- Modify: `tests/test_filters.py`
- Modify: `tests/test_calendar_client.py`
- Modify: `tests/test_sheets_logger.py`

- [ ] **Step 1: Write failing tests**

**In `tests/test_calendar_client.py`**, add to `TestHasEventOnDate`:

```python
    def test_api_failure_triggers_alert(self, client, mock_service):
        """CalendarFilter query failure calls alert.send_alert."""
        mock_service.events().list().execute.side_effect = Exception("API down")
        with patch("organist_bot.integrations.calendar_client.alert") as mock_alert:
            result = client.has_event_on_date("20260301")
        assert result is False  # still fails open
        mock_alert.send_alert.assert_called_once()
        assert "Calendar" in mock_alert.send_alert.call_args.args[0]
```

**In `tests/test_sheets_logger.py`**, add to an existing class or as a new class:

```python
class TestDrainAlerts:
    def test_sheets_api_failure_triggers_alert(self, sheets_logger, mock_service):
        """drain() failure calls alert.send_alert."""
        import logging

        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="hello", args=(), exc_info=None,
        )
        sheets_logger.emit(record)
        # Make the append call fail
        mock_service.spreadsheets().values().append.return_value.execute.side_effect = (
            Exception("quota exceeded")
        )
        with patch("organist_bot.integrations.sheets_logger.alert") as mock_alert:
            with pytest.raises(Exception):
                sheets_logger.drain()
        mock_alert.send_alert.assert_called_once()
        assert "Sheets" in mock_alert.send_alert.call_args.args[0]
```

**In `tests/test_filters.py`**, add to an existing class or new class:

```python
class TestPostcodeFilterAlert:
    def test_maps_api_failure_triggers_alert(self):
        """PostcodeFilter Maps API failure calls alert.send_alert."""
        from organist_bot.filters import PostcodeFilter
        from organist_bot.models import Gig

        mock_client = MagicMock()
        mock_client.distance_matrix.side_effect = Exception("Maps API down")

        with patch("organist_bot.filters.googlemaps.Client", return_value=mock_client):
            pf = PostcodeFilter(
                home_postcode="SW1A 1AA",
                api_key="fake",
                max_minutes=45,
            )

        gig = Gig(
            header="Test", date="2025-06-01", link="https://x.com/1",
            postcode="EC1A 1BB",
        )

        with patch("organist_bot.filters.alert") as mock_alert:
            result = pf(gig)

        assert result is True  # still fails open
        mock_alert.send_alert.assert_called_once()
        assert "Maps" in mock_alert.send_alert.call_args.args[0]
```

- [ ] **Step 2: Run failing tests to confirm they fail**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_calendar_client.py::TestHasEventOnDate::test_api_failure_triggers_alert \
         tests/test_sheets_logger.py::TestDrainAlerts \
         tests/test_filters.py::TestPostcodeFilterAlert -v
```

Expected: 3 FAIL — `alert` not imported in the respective modules

- [ ] **Step 3: Add alert import and call to `calendar_client.py`**

Add the import after the existing imports (after `from organist_bot.models import Gig`):

```python
from organist_bot import alert
```

In `has_event_on_date`, find the `except Exception as exc:` block (around line 113). After the existing `logger.warning(...)` call, add:

```python
            alert.send_alert(
                f"⚠️ Google Calendar API error (CalendarFilter query): {exc}"
            )
```

The full updated block looks like:

```python
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            logger.warning(
                "Calendar check failed — failing open",
                extra={"date": date_str, "error": str(exc), "elapsed_ms": elapsed_ms},
            )
            alert.send_alert(
                f"⚠️ Google Calendar API error (CalendarFilter query): {exc}"
            )
            return False
```

- [ ] **Step 4: Add alert import and call to `sheets_logger.py`**

Add the import after the existing imports (after `from googleapiclient.errors import HttpError`):

```python
from organist_bot import alert
```

In `drain()`, find the outermost `except Exception:` block (around line 306) — the one that restores rows to the buffer. After `with self._lock: self._buffer = rows + self._buffer`, add:

```python
            alert.send_alert(
                f"⚠️ Google Sheets API error (batch append failed): {exc}"
            )
```

The full updated outer except block looks like:

```python
        except Exception as exc:
            with self._lock:
                self._buffer = rows + self._buffer
            alert.send_alert(
                f"⚠️ Google Sheets API error (batch append failed): {exc}"
            )
            raise
```

Note: the existing outer except uses a bare `except Exception:` — change it to `except Exception as exc:` to capture the exception for the alert message.

- [ ] **Step 5: Add alert import and call to `filters.py`**

Add the import after `from organist_bot.models import Gig`:

```python
from organist_bot import alert
```

In `PostcodeFilter._drive_time`, find the `except Exception as exc:` block (around line 394). After the existing `logger.warning(...)` call, add:

```python
            alert.send_alert(
                f"⚠️ Google Maps API error (PostcodeFilter): {exc}"
            )
```

The full updated block looks like:

```python
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            logger.warning(
                "Distance Matrix query failed — failing open",
                extra={
                    "postcode": postcode,
                    "mode": mode,
                    "error": str(exc),
                    "elapsed_ms": elapsed_ms,
                },
            )
            alert.send_alert(
                f"⚠️ Google Maps API error (PostcodeFilter): {exc}"
            )
            return None
```

- [ ] **Step 6: Run the new tests to confirm they pass**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_calendar_client.py::TestHasEventOnDate::test_api_failure_triggers_alert \
         tests/test_sheets_logger.py::TestDrainAlerts \
         tests/test_filters.py::TestPostcodeFilterAlert -v
```

Expected: 3 PASSED

- [ ] **Step 7: Run full test suite**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest --tb=short -q
```

Expected: all pass

- [ ] **Step 8: Commit**

```bash
git add organist_bot/integrations/calendar_client.py \
        organist_bot/integrations/sheets_logger.py \
        organist_bot/filters.py \
        tests/test_calendar_client.py \
        tests/test_sheets_logger.py \
        tests/test_filters.py
git commit -m "feat: add API failure alerts to CalendarFilter, SheetsLogger, PostcodeFilter"
```
