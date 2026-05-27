# Income Forecasting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface income totals for any user-specified period via a `get_income_forecast` Telegram tool and as a summary line in `manage_applications`.

**Architecture:** `get_income(from_date, to_date)` added to `application_store.py` filters `accepted` records by gig `date`, parses fees (strip £/$,), and returns a summary dict. The `get_income_forecast` tool in `unified_agent.py` formats this for display. The `manage_applications` summary action gains an income line using the same `days` window.

**Tech Stack:** Python 3.13, `json`, `datetime`, `re`, `pytest`, `unittest.mock`.

---

## File Structure

| File | Change |
|------|--------|
| `organist_bot/application_store.py` | Add `get_income(from_date, to_date) -> dict` |
| `organist_bot/integrations/unified_agent.py` | Add `get_income_forecast` tool + handler; update `manage_applications` summary |
| `tests/test_application_store.py` | Add income tests |
| `tests/test_unified_agent.py` | Add income forecast and summary tests |

---

### Task 1: `get_income` in `application_store.py`

**Files:**
- Modify: `organist_bot/application_store.py`
- Test: `tests/test_application_store.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_application_store.py`:

```python
import datetime
from pathlib import Path
import json, pytest
# (existing imports already present)

class TestGetIncome:
    def _write_records(self, tmp_path, records):
        p = tmp_path / "applications.json"
        p.write_text(json.dumps(records))
        return p

    def _make_accepted(self, date, fee, url="http://example.com/1"):
        return {
            "url": url, "header": "Test", "organisation": "St John",
            "date": date, "fee": fee, "email": "",
            "status": "accepted",
            "applied_at": "2026-06-01T10:00:00Z",
            "updated_at": "2026-06-01T10:00:00Z",
        }

    def test_sums_accepted_fees_in_range(self, tmp_path, monkeypatch):
        import organist_bot.application_store as store
        monkeypatch.setattr(store, "_PATH", tmp_path / "applications.json")
        records = [
            self._make_accepted("2026-06-10", "£140.00", "http://a.com/1"),
            self._make_accepted("2026-06-15", "£150.00", "http://a.com/2"),
        ]
        (tmp_path / "applications.json").write_text(json.dumps(records))
        result = store.get_income("2026-06-01", "2026-06-30")
        assert result["total"] == pytest.approx(290.0)
        assert result["count"] == 2
        assert result["no_fee_count"] == 0

    def test_excludes_non_accepted_statuses(self, tmp_path, monkeypatch):
        import organist_bot.application_store as store
        monkeypatch.setattr(store, "_PATH", tmp_path / "applications.json")
        records = [
            {**self._make_accepted("2026-06-10", "£100.00"), "status": "applied", "url": "http://a.com/1"},
            {**self._make_accepted("2026-06-10", "£100.00"), "status": "rejected", "url": "http://a.com/2"},
            {**self._make_accepted("2026-06-10", "£100.00"), "status": "declined", "url": "http://a.com/3"},
        ]
        (tmp_path / "applications.json").write_text(json.dumps(records))
        result = store.get_income("2026-06-01", "2026-06-30")
        assert result["total"] == 0.0
        assert result["count"] == 0

    def test_excludes_records_outside_date_range(self, tmp_path, monkeypatch):
        import organist_bot.application_store as store
        monkeypatch.setattr(store, "_PATH", tmp_path / "applications.json")
        records = [
            self._make_accepted("2026-05-31", "£100.00", "http://a.com/1"),  # before
            self._make_accepted("2026-06-15", "£140.00", "http://a.com/2"),  # in range
            self._make_accepted("2026-07-01", "£100.00", "http://a.com/3"),  # after
        ]
        (tmp_path / "applications.json").write_text(json.dumps(records))
        result = store.get_income("2026-06-01", "2026-06-30")
        assert result["count"] == 1
        assert result["total"] == pytest.approx(140.0)

    def test_empty_fee_counted_as_no_fee(self, tmp_path, monkeypatch):
        import organist_bot.application_store as store
        monkeypatch.setattr(store, "_PATH", tmp_path / "applications.json")
        records = [self._make_accepted("2026-06-10", "")]
        (tmp_path / "applications.json").write_text(json.dumps(records))
        result = store.get_income("2026-06-01", "2026-06-30")
        assert result["count"] == 1
        assert result["no_fee_count"] == 1
        assert result["total"] == 0.0

    def test_parses_pound_and_dollar(self, tmp_path, monkeypatch):
        import organist_bot.application_store as store
        monkeypatch.setattr(store, "_PATH", tmp_path / "applications.json")
        records = [
            self._make_accepted("2026-06-10", "£140.00", "http://a.com/1"),
            self._make_accepted("2026-06-15", "$500.00", "http://a.com/2"),
        ]
        (tmp_path / "applications.json").write_text(json.dumps(records))
        result = store.get_income("2026-06-01", "2026-06-30")
        assert result["total"] == pytest.approx(640.0)

    def test_fails_open_on_corrupt_json(self, tmp_path, monkeypatch):
        import organist_bot.application_store as store
        monkeypatch.setattr(store, "_PATH", tmp_path / "applications.json")
        (tmp_path / "applications.json").write_text("not json")
        result = store.get_income("2026-06-01", "2026-06-30")
        assert result == {"total": 0.0, "count": 0, "no_fee_count": 0, "records": []}
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_application_store.py::TestGetIncome -v
```
Expected: AttributeError or FAILED (get_income not yet defined)

- [ ] **Step 3: Implement `get_income` in `organist_bot/application_store.py`**

Add after `list_applications`:

```python
def _parse_fee(fee_str: str) -> float | None:
    """Strip currency symbols/commas, return float or None if unparseable/empty."""
    if not fee_str or not fee_str.strip():
        return None
    cleaned = fee_str.strip().lstrip("£$").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def get_income(from_date: str, to_date: str) -> dict:
    """Return income summary for accepted records where gig date falls in [from_date, to_date] inclusive.

    Returns:
      {
        "total": float,          # sum of parsed fees (excludes no-fee records)
        "count": int,            # total accepted gigs in range
        "no_fee_count": int,     # gigs with empty/unparseable fee
        "records": list[dict],   # matching records, sorted by date ascending
      }
    Fails open — returns zero summary on any error.
    """
    _zero = {"total": 0.0, "count": 0, "no_fee_count": 0, "records": []}
    try:
        records = _read()
        matched = []
        for r in records:
            if r.get("status") != "accepted":
                continue
            date_str = r.get("date", "")
            if not date_str:
                continue
            try:
                gig_date = datetime.date.fromisoformat(date_str)
            except ValueError:
                continue
            if datetime.date.fromisoformat(from_date) <= gig_date <= datetime.date.fromisoformat(to_date):
                matched.append(r)
        matched.sort(key=lambda r: r.get("date", ""))
        total = 0.0
        no_fee_count = 0
        for r in matched:
            parsed = _parse_fee(r.get("fee", ""))
            if parsed is None:
                no_fee_count += 1
            else:
                total += parsed
        return {
            "total": total,
            "count": len(matched),
            "no_fee_count": no_fee_count,
            "records": matched,
        }
    except Exception:
        logger.exception("application_store: get_income failed")
        return _zero
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_application_store.py::TestGetIncome -v
```
Expected: 6 PASSED

- [ ] **Step 5: Run full test suite**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest --tb=short -q
```
Expected: all passing

- [ ] **Step 6: Commit**

```bash
git add organist_bot/application_store.py tests/test_application_store.py
git commit -m "feat: add get_income to application_store"
```

---

### Task 2: `get_income_forecast` tool + `manage_applications` summary income line

**Files:**
- Modify: `organist_bot/integrations/unified_agent.py`
- Test: `tests/test_unified_agent.py`

- [ ] **Step 1: Write failing tests for `get_income_forecast`**

Add to `tests/test_unified_agent.py`:

```python
class TestGetIncomeForecast:
    @pytest.mark.asyncio
    async def test_formats_output_with_records(self):
        summary = {
            "total": 290.0,
            "count": 2,
            "no_fee_count": 0,
            "records": [
                {"organisation": "St John", "date": "2026-06-10", "fee": "£140.00"},
                {"organisation": "St Leonard's", "date": "2026-06-22", "fee": "£150.00"},
            ],
        }
        with patch("organist_bot.integrations.unified_agent.application_store.get_income", return_value=summary):
            result = await _execute_tool(
                "get_income_forecast",
                {"from_date": "2026-06-01", "to_date": "2026-06-30"},
                CHAT_ID,
            )
        assert "💰" in result
        assert "£290.00" in result
        assert "St John" in result
        assert "St Leonard" in result

    @pytest.mark.asyncio
    async def test_no_gigs_message(self):
        summary = {"total": 0.0, "count": 0, "no_fee_count": 0, "records": []}
        with patch("organist_bot.integrations.unified_agent.application_store.get_income", return_value=summary):
            result = await _execute_tool(
                "get_income_forecast",
                {"from_date": "2026-06-01", "to_date": "2026-06-30"},
                CHAT_ID,
            )
        assert "No accepted gigs" in result

    @pytest.mark.asyncio
    async def test_shows_no_fee_note(self):
        summary = {
            "total": 140.0,
            "count": 2,
            "no_fee_count": 1,
            "records": [
                {"organisation": "St John", "date": "2026-06-10", "fee": "£140.00"},
                {"organisation": "All Saints", "date": "2026-06-15", "fee": ""},
            ],
        }
        with patch("organist_bot.integrations.unified_agent.application_store.get_income", return_value=summary):
            result = await _execute_tool(
                "get_income_forecast",
                {"from_date": "2026-06-01", "to_date": "2026-06-30"},
                CHAT_ID,
            )
        assert "no fee" in result.lower() or "(no fee)" in result.lower()

class TestManageApplicationsSummaryIncome:
    @pytest.mark.asyncio
    async def test_summary_includes_income_line(self):
        records = [
            {
                "url": "http://a.com/1", "header": "Service", "organisation": "St John",
                "date": "2026-06-10", "fee": "£140.00", "email": "", "status": "accepted",
                "applied_at": "2026-06-01T10:00:00Z", "updated_at": "2026-06-01T10:00:00Z",
            }
        ]
        income = {"total": 140.0, "count": 1, "no_fee_count": 0, "records": records}
        with patch("organist_bot.integrations.unified_agent.application_store.list_applications", return_value=records), \
             patch("organist_bot.integrations.unified_agent.application_store.get_income", return_value=income):
            result = await _execute_tool("manage_applications", {"action": "summary"}, CHAT_ID)
        assert "Income" in result
        assert "£140.00" in result
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_unified_agent.py::TestGetIncomeForecast \
         tests/test_unified_agent.py::TestManageApplicationsSummaryIncome -v
```
Expected: FAILED (tool not defined)

- [ ] **Step 3: Add `get_income_forecast` tool schema to `TOOLS` list in `unified_agent.py`**

```python
{
    "name": "get_income_forecast",
    "description": (
        "Show total income from accepted gigs for any period. "
        "Convert natural language to ISO dates before calling: "
        "'June' → from_date='2026-06-01', to_date='2026-06-30'; "
        "'this year' → from_date='2026-01-01', to_date='2026-12-31'; "
        "'last 3 months' → compute relative to today."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "from_date": {"type": "string", "description": "Start date ISO format YYYY-MM-DD (inclusive)"},
            "to_date": {"type": "string", "description": "End date ISO format YYYY-MM-DD (inclusive)"},
        },
        "required": ["from_date", "to_date"],
    },
},
```

- [ ] **Step 4: Add `get_income_forecast` handler in `_execute_tool`**

Add before the `manage_applications` block:

```python
# ── get_income_forecast ──────────────────────────────────────────────────
if name == "get_income_forecast":
    from_date = input_data.get("from_date", "")
    to_date = input_data.get("to_date", "")
    try:
        summary = application_store.get_income(from_date, to_date)
    except Exception as exc:
        return json.dumps({"error": f"Failed to retrieve income: {exc}"})

    # Format the date range header
    try:
        from_dt = datetime.date.fromisoformat(from_date)
        to_dt = datetime.date.fromisoformat(to_date)
        header = f"💰 Income — {from_dt.strftime('%-d %b')} to {to_dt.strftime('%-d %b %Y')}"
    except ValueError:
        header = f"💰 Income — {from_date} to {to_date}"

    if summary["count"] == 0:
        return json.dumps({"result": f"{header}\n\nNo accepted gigs in this period."})

    lines = [
        header,
        "",
        f"Confirmed gigs:   {summary['count']}",
        f"Total income:     £{summary['total']:.2f}",
    ]
    if summary["no_fee_count"] > 0:
        lines.append(f"No fee recorded:  {summary['no_fee_count']} gig(s) (not included in total)")

    lines.append("")
    for i, r in enumerate(summary["records"], start=1):
        org = r.get("organisation") or r.get("header") or "Unknown"
        try:
            d = datetime.date.fromisoformat(r.get("date", ""))
            date_str = d.strftime("%-d %b")
        except ValueError:
            date_str = r.get("date", "")
        fee_str = r.get("fee", "").strip()
        fee_display = fee_str if fee_str else "(no fee)"
        lines.append(f"{i}. {org} — {date_str}  {fee_display}")

    return json.dumps({"result": "\n".join(lines)})
```

- [ ] **Step 5: Add `get_income_forecast` to `_VERBATIM_RESPONSE_TOOLS`**

```python
_VERBATIM_RESPONSE_TOOLS = {
    "list_upcoming_gigs", "get_gig_stats", "manage_config",
    "manage_applications", "get_income_forecast",
}
```

- [ ] **Step 6: Update `manage_applications` summary action to include income line and `rejected` count**

Replace the summary block (lines ~1056–1073) with:

```python
if action == "summary":
    counts = {
        "accepted": sum(1 for r in records if r["status"] == "accepted"),
        "applied": sum(1 for r in records if r["status"] == "applied"),
        "no_response": sum(1 for r in records if r["status"] == "no_response"),
        "declined": sum(1 for r in records if r["status"] == "declined"),
        "rejected": sum(1 for r in records if r["status"] == "rejected"),
    }
    total = len(records)
    lines = [
        f"📋 Applications — last {days} days",
        "",
        f"Applied:      {total}",
        f"Accepted:     {counts['accepted']}",
        f"No response:  {counts['no_response']}",
        f"Declined:     {counts['declined']}",
        f"Rejected:     {counts['rejected']}",
        f"Pending:      {counts['applied']}",
    ]
    # Income line: use gig dates within the same window (approximated as from days ago to today)
    today = datetime.date.today()
    from_date = (today - datetime.timedelta(days=days)).isoformat()
    to_date = today.isoformat()
    income = application_store.get_income(from_date, to_date)
    income_line = f"Income (accepted):  £{income['total']:.2f}"
    if income["no_fee_count"] > 0:
        if income["no_fee_count"] == income["count"]:
            income_line += f"  · all {income['count']} gig(s) have no fee recorded"
        else:
            income_line += f"  · {income['no_fee_count']} gig has no fee recorded"
    lines.append("")
    lines.append(income_line)
    return json.dumps({"result": "\n".join(lines)})
```

Note: `import datetime` is already present in `unified_agent.py`. Verify before adding.

- [ ] **Step 7: Run tests to confirm they pass**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com \
  pytest tests/test_unified_agent.py::TestGetIncomeForecast \
         tests/test_unified_agent.py::TestManageApplicationsSummaryIncome -v
```
Expected: all PASSED

- [ ] **Step 8: Run full test suite**

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest --tb=short -q
```
Expected: all passing

- [ ] **Step 9: Commit**

```bash
git add organist_bot/integrations/unified_agent.py tests/test_unified_agent.py
git commit -m "feat: add get_income_forecast tool and income line to applications summary"
```
