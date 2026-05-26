# Income Forecasting

**Date:** 2026-05-26
**Status:** Approved

## Problem

The bot tracks accepted gigs in `data/applications.json` but there is no way to query expected or earned income for a given period. Calculating income requires manually scanning the file.

## Goal

Surface income totals for any user-specified period via a dedicated Telegram tool and as a summary line in `manage_applications`.

## Scope

- Spec 3 of 3 in the application tracking series
- Depends on `data/applications.json` from Spec 1 (application_store)
- Consumes `accepted` records only — no speculation on pending applications

## Data

Income is calculated from `accepted` records where the gig `date` falls within the requested period.

**Fee parsing:** strip `£`, `$`, `,` and convert to float. Empty fee → `0.0`, counted separately as `no_fee_count`. Records with unparseable fee strings are treated as no-fee.

**Date field used:** `date` (the gig date), not `applied_at`.

## Architecture

### 1. `organist_bot/application_store.py` — addition

```python
def get_income(from_date: str, to_date: str) -> dict:
    """
    Return income summary for accepted records where gig date falls in [from_date, to_date] inclusive.
    Returns:
      {
        "total": float,          # sum of parsed fees (excludes no-fee records)
        "count": int,            # total accepted gigs in range
        "no_fee_count": int,     # gigs with empty/unparseable fee
        "records": list[dict],   # matching records, sorted by date ascending
      }
    Fails open — returns {"total": 0.0, "count": 0, "no_fee_count": 0, "records": []} on error.
    """
```

### 2. `unified_agent.py` — new `get_income_forecast` tool

Tool parameters: `from_date: str` (ISO date), `to_date: str` (ISO date).

The LLM converts natural language from the user ("June", "this year", "last 3 months", "Q1 2026") to ISO dates before calling the tool.

Output format:

```
💰 Income — 1 Jun to 30 Jun 2026

Confirmed gigs:   4
Total income:     £420.00
No fee recorded:  1 gig (not included in total)

1. St John the Evangelist — 18 Jun  £140.00
2. St Leonard's Church — 22 Jun     £150.00
3. St Peter's Church — 28 Jun       £130.00
4. All Saints Church — 15 Jun       (no fee)
```

If no accepted gigs in the period:
```
💰 Income — 1 Jun to 30 Jun 2026

No accepted gigs in this period.
```

The tool description routes:
- "what's my income for June?" → `get_income_forecast(from_date="2026-06-01", to_date="2026-06-30")`
- "how much have I earned this year?" → `get_income_forecast(from_date="2026-01-01", to_date="2026-12-31")`
- "income last 3 months" → LLM computes dates relative to today

### 3. `unified_agent.py` — `manage_applications(action=summary)` update

Append one income line at the bottom of the summary, using the same `days` window:

```
📋 Applications — last 30 days

Applied:      12
Accepted:      3
No response:   2
Declined:      1
Rejected:      1
Pending:       6

Income (accepted):  £420.00  · 1 gig has no fee recorded
```

If all accepted gigs have empty fees:
```
Income (accepted):  £0.00  · all 3 gigs have no fee recorded
```

## Files Changed

| File | Change |
|---|---|
| `organist_bot/application_store.py` | Add `get_income(from_date, to_date)` |
| `organist_bot/integrations/unified_agent.py` | Add `get_income_forecast` tool; update `manage_applications` summary |
| `tests/test_application_store.py` | Tests for `get_income` |
| `tests/test_unified_agent.py` | Tests for `get_income_forecast` tool and summary update |

## Tests

### `test_application_store.py`

| Test | Scenario |
|---|---|
| `test_get_income_sums_accepted_fees_in_range` | Two accepted records in range → correct total |
| `test_get_income_excludes_non_accepted` | `applied`/`rejected`/`declined` records → not counted |
| `test_get_income_excludes_outside_range` | Record with date outside window → not counted |
| `test_get_income_empty_fee_counted_as_no_fee` | Record with empty fee → count=1, total unchanged |
| `test_get_income_parses_pound_and_dollar` | `£140.00` and `$500.00` both parse correctly |
| `test_get_income_fails_open` | Corrupt JSON → returns zero summary |

### `test_unified_agent.py`

| Test | Scenario |
|---|---|
| `test_get_income_forecast_formats_output` | Records in range → correct formatted output |
| `test_get_income_forecast_no_gigs` | Empty range → "No accepted gigs in this period" |
| `test_get_income_forecast_shows_no_fee_note` | Record with no fee → note appended |
| `test_manage_applications_summary_includes_income` | Summary action → income line present |

## Error Handling

- `get_income` fails open — returns zero summary on any read or parse error
- Tool failure returns a graceful error message to the user, never raises

## Out of Scope

- Projecting income from `applied` (pending) records
- Invoicing or payment tracking
- Multi-currency conversion (fees displayed as-is)
- Export to spreadsheet
