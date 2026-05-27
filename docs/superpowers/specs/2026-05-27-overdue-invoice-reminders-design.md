# Overdue Invoice Reminders Design

> **For agentic workers:** After approval, use `superpowers:writing-plans` to create the implementation plan.

**Goal:** Automatically remind via Telegram when an emailed invoice hasn't been paid after 5 days, and monitor the client's Gmail reply thread to auto-detect payment confirmations. Also expose a `mark_invoice_paid` tool in the unified agent for manual marking.

---

## Section 1: Invoice Schema Changes

Three new fields added to every invoice record in `invoices.json`:

| Field | Type | Set by |
|---|---|---|
| `emailed_at` | `str \| None` | `mark_invoice_emailed()` — ISO timestamp alongside existing `emailed: True` |
| `paid_at` | `str \| None` | `mark_invoice_paid()` — ISO timestamp when invoice marked paid |
| `reminder_sent` | `bool` | `invoice_monitor` — set to `True` after the one-time overdue Telegram ping |
| `checked_reply_ids` | `list[str]` | `invoice_monitor` — Gmail message IDs already classified (dedup) |

Existing records without these fields degrade gracefully — all treated as absent/None/False/[].

### Modified: `mark_invoice_emailed(invoice_number: str) -> None`

In addition to setting `emailed = True`, also writes:
```python
invoices[invoice_number]["emailed_at"] = now_iso()
```

Where `now_iso()` returns a UTC ISO-8601 string (`YYYY-MM-DDTHH:MM:SSZ`).

### New: `mark_invoice_paid(invoice_number: str) -> bool`

In `invoice_generator.py`:
```python
def mark_invoice_paid(invoice_number: str) -> bool:
    """Set paid_at on the matching invoice record. Returns False if not found."""
```

Sets `paid_at = now_iso()`. Returns `True` on success, `False` if invoice number not found.

### Modified: `save_invoice(invoice_data: dict) -> None`

Initialise new fields on creation:
```python
"emailed_at": None,
"paid_at": None,
"reminder_sent": False,
"checked_reply_ids": [],
```

---

## Section 2: Gmail Reply Monitoring

### New method: `GmailClient.fetch_invoice_replies`

```python
def fetch_invoice_replies(
    self,
    invoice_number: str,
    client_email: str,
    since_date: str | None = None,
) -> list[dict]:
    """Search inbox for replies to a sent invoice.

    Searches: from:{client_email} subject:{invoice_number} in:inbox
    since_date: optional YYYY/MM/DD bound (use emailed_at date to limit scan).
    Returns list of {message_id, sender, body, ...} dicts.
    Fails open — returns [] on any error.
    """
```

Uses existing `_search_messages` and `_get_message_details` infrastructure. Same return shape as `fetch_reply_messages`.

### Classification

Uses Claude Haiku (same pattern as `reply_monitor.classify_reply`):

```
Does this email indicate that invoice {invoice_number} has been paid?
Reply with exactly one word: paid / unclear
```

Model: `claude-haiku-4-5-20251001`. Temperature 0. Fails open — treats any non-`"paid"` response (including API errors) as `"unclear"`.

---

## Section 3: Overdue Check & Notifications

### New module: `organist_bot/invoice_monitor.py`

Single public function:

```python
def check_invoice_reminders_and_replies() -> None:
    """Check all emailed-but-unpaid invoices for payment replies and overdue status.

    Called in main.py post-pipeline steps on every scheduler tick.
    Fails open — logs warnings on any per-invoice error and continues.
    """
```

**Algorithm per tick:**

1. Load all invoices (`load_invoices()`).
2. Filter to candidates: `emailed=True` and `paid_at` absent or None.
3. For each candidate:

   **Reply check:**
   - Call `gmail_client.fetch_invoice_replies(invoice_number, client_email, since_date=emailed_at[:10])`.
   - For each returned message, skip if `message_id` already in `checked_reply_ids`.
   - Classify new messages with Haiku.
   - If classification is `"paid"`:
     - Call `mark_invoice_paid(invoice_number)`.
     - Send Telegram alert: `"✅ Invoice {number} ({client_name}, £{total:.2f}) marked as paid — reply received."`
     - Skip overdue check for this invoice (just paid).
   - Append all new message IDs to `checked_reply_ids` regardless of classification.
   - Persist updated `checked_reply_ids` via `save_invoice_field(invoice_number, "checked_reply_ids", [...])`.

   **Overdue check** (only if not just marked paid):
   - If `emailed_at` is 5+ days ago and `reminder_sent=False`:
     - Send Telegram alert: `"⏰ Invoice {number} ({client_name}, £{total:.2f}) was sent {N} days ago and hasn't been paid."`
     - Set `reminder_sent=True` via `save_invoice_field(invoice_number, "reminder_sent", True)`.

### New helper: `save_invoice_field`

```python
def save_invoice_field(invoice_number: str, field: str, value: object) -> None:
    """Update a single field on an invoice record atomically."""
```

In `invoice_generator.py`. Used by `invoice_monitor` to persist `checked_reply_ids` and `reminder_sent` without rewriting the whole invoice structure.

### Integration in `main.py`

Added to post-pipeline steps (after `reply_monitor.check_replies()`):

```python
from organist_bot import invoice_monitor
invoice_monitor.check_invoice_reminders_and_replies()
```

---

## Section 4: Unified Agent Tool

### New tool: `mark_invoice_paid`

Added to `unified_agent.py` TOOLS list:

```json
{
    "name": "mark_invoice_paid",
    "description": "Mark an invoice as paid. Use when the user says an invoice has been paid or confirms payment.",
    "parameters": {
        "type": "object",
        "properties": {
            "invoice_number": {
                "type": "string",
                "description": "The invoice number, e.g. INV-2026-001"
            }
        },
        "required": ["invoice_number"]
    }
}
```

**Handler:**
```python
if name == "mark_invoice_paid":
    inv_num = input_data["invoice_number"]
    ok = mark_invoice_paid(inv_num)
    if not ok:
        return json.dumps({"error": f"Invoice {inv_num} not found."})
    return json.dumps({"result": f"✅ Invoice {inv_num} marked as paid."})
```

**System prompt bullet** (after invoice section):
```
- "Mark INV-2026-001 as paid" / "invoice has been paid" → mark_invoice_paid.
```

### Updated: `list_invoices` display

Replace bare `emailed: yes/no` with a richer payment status column:

| State | Display |
|---|---|
| Not emailed | `not sent` |
| Emailed, unpaid, < 5 days | `emailed {N}d ago` |
| Emailed, unpaid, ≥ 5 days | `⏰ overdue ({N}d)` |
| Paid | `✅ paid` |

---

## Error Handling

| Failure | Behaviour |
|---|---|
| Gmail API unavailable | `fetch_invoice_replies` returns `[]`, skip reply check for this tick |
| Haiku classification error | Treat as `"unclear"`, log warning |
| `mark_invoice_paid` not found | Log warning, continue to next invoice |
| Telegram alert fails | Log warning, do not set `reminder_sent=True` (retry next tick) |
| Invoice has no `client_email` | Skip reply check, still send overdue reminder |

---

## Testing

- `tests/test_invoice_monitor.py` (new) — unit tests for `check_invoice_reminders_and_replies`:
  - Overdue invoice sends reminder and sets `reminder_sent=True`
  - Invoice < 5 days old does not send reminder
  - Already `reminder_sent=True` does not send again
  - Reply classified as `"paid"` marks invoice paid and sends Telegram confirmation
  - Reply classified as `"unclear"` does not mark paid
  - Already-seen message IDs are skipped
  - Gmail failure does not crash the function
- `tests/test_invoice_generator.py` (extend):
  - `mark_invoice_emailed` sets both `emailed=True` and `emailed_at`
  - `mark_invoice_paid` sets `paid_at`, returns True; returns False for unknown number
  - `save_invoice` initialises all four new fields
- `tests/test_unified_agent.py` (extend):
  - `mark_invoice_paid` tool returns success message
  - `mark_invoice_paid` tool returns error for unknown invoice
  - `list_invoices` shows correct payment status labels
