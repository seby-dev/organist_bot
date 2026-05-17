# Pipeline Anomaly Alerts

**Date:** 2026-05-17

## Problem

The pipeline already sends a Telegram alert on unhandled exceptions (via `_send_telegram_alert()` in `main.py`), but two categories of failure are currently silent:

1. **High parse error rate** — `gig_errors >= 2` in a single run means gig detail pages are failing to parse. The run continues, but you may be missing gigs with no visible indication.
2. **External API failures** — Google Calendar, Google Maps, and Google Sheets errors are caught and logged at WARNING but never alerted. A misconfigured credential or API outage can go unnoticed for days.

## Goal

- Alert immediately via Telegram when `gig_errors >= 2` in a single scraping run.
- Alert immediately via Telegram when a Google Calendar, Google Maps, or Google Sheets API call fails in a way that affects pipeline correctness or data integrity.
- Centralise alert delivery so it is importable from any module without circular imports.

---

## Design

### 1. `organist_bot/alert.py` (new file)

Single public function, no state:

```python
def send_alert(message: str) -> None:
    """Send a Telegram alert to the configured chat. Fire-and-forget."""
```

Implementation:
- Reads `settings.telegram_bot_token` and `settings.telegram_chat_id`.
- POSTs to `https://api.telegram.org/bot{token}/sendMessage` with `{"chat_id": ..., "text": message}` and `timeout=10`.
- Catches all exceptions and logs at WARNING — an alert failure must never crash the pipeline.
- Returns immediately if either setting is missing (no-op, logged at DEBUG).

`main.py` replaces its existing inline `_send_telegram_alert()` with an import of this function.

---

### 2. Parse error rate (`main.py`)

After the scraping phase (Phase 1) completes and `gig_errors` is known, add:

```python
if gig_errors >= 2:
    alert.send_alert(
        f"⚠️ Parse errors in run {run_id}: {gig_errors} gig(s) failed to parse "
        f"out of {listed} listed. Check logs for detail."
    )
```

Threshold: **2 errors absolute**. One parse failure is noise; two or more in a single run indicates a structural problem (broken detail page, changed HTML, network issue).

---

### 3. External API failures

#### `organist_bot/integrations/calendar_client.py`

Alert site: the `events().list()` call inside `CalendarFilter` — the method that queries for existing calendar events to detect date clashes. A failure here causes `CalendarFilter` to silently pass gigs it should reject.

At the existing `except Exception` handler, after `logger.warning(...)`, add:

```python
alert.send_alert(
    f"⚠️ Google Calendar API error (CalendarFilter query): {e}"
)
```

**Not alerted:** `block_period` and `unblock_period` failures — those are cosmetic (filter correctness is unaffected).

#### `organist_bot/integrations/sheets_logger.py`

Alert site: the `emit()` failure path where a batch `spreadsheets().values().append()` call raises an exception. A failure here causes silent log loss.

At the existing `except Exception` handler, after `logger.warning(...)`, add:

```python
alert.send_alert(
    f"⚠️ Google Sheets API error (batch append failed): {e}"
)
```

#### `organist_bot/filters.py`

Alert site: `PostcodeFilter.is_valid()` when the `googlemaps` API call raises an exception. The current behaviour passes the gig through (returns `True`) so it is not silently rejected — but the travel-time check is skipped entirely, which could allow a gig through that should be rejected.

At the existing `except Exception` handler, after `logger.warning(...)`, add:

```python
alert.send_alert(
    f"⚠️ Google Maps API error (PostcodeFilter): {e}"
)
```

---

## Error Handling

- `send_alert` itself never raises — all exceptions are caught internally.
- Alert failures are logged at WARNING only, never surfaced to the pipeline.
- All three API alert sites continue to log at WARNING as before — the alert call is additive, not a replacement.

---

## Files Changed

| File | Change |
|------|--------|
| `organist_bot/alert.py` | New file — `send_alert(message: str) -> None` |
| `main.py` | Replace `_send_telegram_alert()` with `alert.send_alert`; add parse-error-rate check post-scrape |
| `organist_bot/integrations/calendar_client.py` | Add `alert.send_alert()` in CalendarFilter query exception handler |
| `organist_bot/integrations/sheets_logger.py` | Add `alert.send_alert()` in `emit()` exception handler |
| `organist_bot/filters.py` | Add `alert.send_alert()` in `PostcodeFilter` exception handler |
| `tests/test_alert.py` | Tests for `send_alert` (success, missing config no-op, network failure swallowed) |
| `tests/test_main.py` | Test that parse-error alert fires when `gig_errors >= 2` |
| `tests/test_filters.py` | Test that PostcodeFilter API failure triggers alert |

---

## Out of Scope

- Rate limiting or deduplication of alerts (failures should be rare enough that volume is not a concern).
- Scraper-empty alerts (zero gigs listed) — not requested.
- Dry-spell alerts (N consecutive zero-valid runs) — not requested.
- Alert severity levels or formatting beyond plain text with `⚠️` prefix.
