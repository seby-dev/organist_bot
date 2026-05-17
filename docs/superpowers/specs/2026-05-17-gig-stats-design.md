# Gig Stats via Telegram

**Date:** 2026-05-17

## Problem

The pipeline logs rich per-run metrics (gigs listed, filtered, valid, rejection counts by filter) to Google Sheets, but there is no way to query those metrics from Telegram. The only way to review pipeline performance is to open the spreadsheet manually.

## Goal

Add a `get_gig_stats` tool to the unified agent that queries the Google Sheets log and returns an aggregated pipeline summary covering a configurable time window (default: last 7 days).

## Data Sources

The Sheets log (tab `Logs`, or the latest auto-rotated tab) has one row per log record with columns:

| Column | Content |
|--------|---------|
| `timestamp` | ISO-8601 datetime string |
| `run_id` | 8-char hex shared across all records in a run |
| `level` | `INFO`, `DEBUG`, etc. |
| `message` | Human-readable label |
| `details` | JSON blob with structured fields |

Three message types are relevant:

- **`"Scraping complete"`** — details: `listed`, `pre_filter_passed`, `scraped`, `gig_errors`, `elapsed_ms`
- **`"Run summary"`** — details: `scraped`, `valid`, `notified`, `gig_errors`, `elapsed_ms` (total run)
- **`"Filter chain applied"`** — details: `filter_breakdown` (object mapping filter repr string → rejection count)

Rows are grouped by `run_id` to reconstruct per-run stats.

---

## Design

### 1. `SheetsLogger` — new method (`sheets_logger.py`)

**`query_run_stats(days: int = 7) -> list[dict]`**

1. Calls `spreadsheets().values().get()` on the active log tab (uses the same tab-detection logic already in the class to find the latest `Logs N` tab).
2. Parses each row into a dict; skips rows with missing or malformed `details` JSON.
3. Filters to rows whose `timestamp` falls within the last `days` days.
4. Groups by `run_id`:
   - Merges `"Scraping complete"` fields: `listed`, `pre_filter_passed`
   - Merges `"Run summary"` fields: `valid`, `gig_errors`, `elapsed_ms`
   - Merges `"Filter chain applied"` field: `filter_breakdown` (dict)
5. Returns a list of per-run dicts sorted by timestamp descending:
   ```python
   {
       "run_id": "9f5e3bed",
       "timestamp": "2026-05-17T14:23:01",
       "listed": 22,
       "pre_filter_passed": 3,
       "valid": 1,
       "gig_errors": 0,
       "elapsed_ms": 891,
       "filter_breakdown": {"SeenFilter": 12, "FeeFilter": 4, ...},
   }
   ```
6. Runs that are missing a `"Run summary"` row (e.g. still in progress) are excluded.

Returns an empty list if no rows match. Raises `RuntimeError` if the Sheets API call fails (the tool handler catches this and returns an error message to the user).

---

### 2. `unified_agent.py` — new tool

**Tool schema** (added to `TOOLS` list):

```python
{
    "name": "get_gig_stats",
    "description": "Query the Google Sheets log and return pipeline stats for the last N days. Shows total runs, gigs listed/filtered/valid, filter rejection breakdown, and the most recent run summary.",
    "input_schema": {
        "type": "object",
        "properties": {
            "days": {
                "type": "integer",
                "description": "Number of days to look back (default 7, max 90).",
                "default": 7,
            }
        },
        "required": [],
    },
}
```

**`_execute_tool` branch** for `get_gig_stats`:

1. Validates `days` (clamp to 1–90).
2. Constructs `SheetsLogger` using credentials from `settings` (same pattern as `_make_calendar_client`). Returns an error message if Sheets is not configured (`GOOGLE_SHEETS_ID` not set).
3. Calls `sheets_logger.query_run_stats(days)`.
4. Aggregates:
   - `total_runs` — count of run dicts
   - `total_listed`, `total_valid`, `total_gig_errors` — sums
   - `avg_listed`, `avg_valid` — means rounded to 1 dp
   - `combined_filter_breakdown` — summed rejection counts across all runs, sorted descending
   - `last_run` — the first (most recent) run dict
5. Returns formatted text (no JSON — the AI formats this as a direct response):

```
Pipeline stats — last 7 days

Runs: 84
Listed: 1,680 total · 20.0 avg/run
Valid:  12 total · 0.1 avg/run
Errors: 0

Filter rejections:
  SeenFilter:       847
  CalendarFilter:    23
  FeeFilter:         12
  PostcodeFilter:     8
  BlacklistFilter:    2

Last run: 2026-05-17 14:23 — 18 listed · 0 valid · 891ms
```

If no data is found for the requested window, returns: `"No pipeline runs logged in the last {days} days."`

---

### 3. Helper: `_make_sheets_logger() -> SheetsLogger | None`

Mirrors `_make_calendar_client()`. Returns `None` (with a `logger.debug`) if `GOOGLE_SHEETS_ID` is not configured, otherwise constructs and returns a `SheetsLogger` instance.

---

## Error Handling

- Sheets API failure → caught in tool handler → returns `"Could not reach Google Sheets: {error}"` to the user.
- Malformed `details` JSON in a row → skip that row silently (logged at DEBUG).
- `days` out of range → clamped to 1–90 silently.
- Sheets not configured → `"Google Sheets is not configured (GOOGLE_SHEETS_ID missing)."`.

---

## Files Changed

| File | Change |
|------|--------|
| `organist_bot/integrations/sheets_logger.py` | Add `query_run_stats(days)` method |
| `organist_bot/integrations/unified_agent.py` | Add `get_gig_stats` tool schema + `_execute_tool` branch + `_make_sheets_logger` helper |
| `tests/test_sheets_logger.py` | Tests for `query_run_stats` |
| `tests/test_unified_agent.py` | Tests for `get_gig_stats` tool |

---

## Out of Scope

- Stats on individual gig details (fees, localities) — seen_gigs.csv stores only URLs.
- Push-based stats (periodic summaries sent automatically) — covered by the Alerts spec.
- Caching query results locally — the Sheets tab is small enough that a full read is fast.
