# Email Reply Monitoring

**Date:** 2026-05-26
**Status:** Approved

## Problem

The bot auto-applies to gigs but has no awareness of replies. Churches confirm, decline, or cancel via email. Currently these replies go unread by the bot — status transitions must be done manually.

## Goal

Monitor the Gmail inbox and sent folder for replies related to active applications, classify them with Claude, and automatically transition application statuses with appropriate follow-up actions and Telegram notifications.

## Scope

- Spec 2 of 3 in the application tracking series
- Depends on `data/applications.json` from Spec 1 (application_store)
- Spec 3 (income forecasting) consumes data produced here

## Statuses

```
applied → accepted       (reply classified as acceptance → auto calendar event)
applied → rejected       (reply classified as church declining)
applied → no_response    (existing: gig date passed, scheduler tick)
applied → declined       (existing: manual via Telegram)
accepted → declined      (cancellation confirmed via Telegram — either side)
```

New status: `rejected` (church declined via email, distinct from `declined` which is user-initiated).

## Classification

Each reply is passed to Claude with a prompt classifying it into one of four categories:

| Classification | Trigger | Action |
|---|---|---|
| `accepted` | Church confirms booking | `upsert_accepted()` + create calendar event + Telegram notify |
| `rejected` | Church says they've moved on / filled position | `update_status('rejected')` + Telegram notify |
| `cancellation` | Either party signalling cancellation of an accepted booking | Telegram prompt: "Booking at [org] on [date] may be cancelled. Delete calendar event or ignore?" |
| `unclear` | Anything else — questions, ambiguous replies, requests for more info | Telegram notify with reply excerpt, no status change |

`cancellation` applies to both `applied` and `accepted` records. For `applied` records it surfaces as `unclear` unless the language is unambiguously a withdrawal.

## Data Model

Add one field to each record in `data/applications.json`:

```json
{
  "reply_message_id": "18abc123def456"
}
```

`null` until a reply is processed. Used for deduplication — a message is only classified once.

## Architecture

### 1. `organist_bot/integrations/gmail_client.py` (new)

OAuth2 Gmail API wrapper. Authenticates via `credentials.json` + `token.json`.

```python
def fetch_reply_messages(
    applied_emails: list[str],
    accepted_emails: list[str],
) -> list[dict]:
    """
    Search inbox for messages FROM church emails (applied + accepted records).
    Search sent folder for messages TO church emails (accepted records only).
    Returns list of dicts: message_id, sender, recipient, body, direction ('incoming'|'outgoing').
    """
```

Fails open — returns `[]` on API errors.

### 2. `organist_bot/reply_monitor.py` (new)

Orchestrates the check each scheduler tick:

```python
def check_replies() -> None:
    """Load applied + accepted records, fetch reply messages, classify each, dispatch actions."""
```

Per message:
1. Skip if `reply_message_id` already set on the matching record (dedup)
2. Match message to application record by sender/recipient email
3. Call Claude to classify: `accepted` / `rejected` / `cancellation` / `unclear`
4. Dispatch action per table above
5. Store `reply_message_id` on record via `application_store.update_reply_message_id(url, message_id)`

### 3. `organist_bot/application_store.py` — additions

```python
def update_reply_message_id(url: str, message_id: str) -> bool:
    """Set reply_message_id on the record with the given URL."""
```

### 4. `organist_bot/integrations/unified_agent.py` — `manage_applications` update

When `action=update` transitions an `accepted` record to `declined`, follow up via Telegram:

> "Do you want to delete the calendar event for [org] on [date]?"

User responds naturally; the unified agent calls `GoogleCalendarClient.delete_event()` if confirmed.

### 5. `main.py` — `_run`

After the notify phase, call `reply_monitor.check_replies()`. Wrapped in try/except, failure logged at WARNING + Telegram alert.

### 6. `scripts/setup_gmail_auth.py` (new)

One-time OAuth2 setup script. Opens browser for authentication, writes refresh token to `data/gmail_token.json`. Run once on any machine with a browser, then copy token to the server.

## Configuration

Two new fields in `organist_bot/config.py`:

| Field | Default | Purpose |
|---|---|---|
| `GMAIL_CREDENTIALS_FILE` | `""` | Path to OAuth2 `credentials.json` from Google Cloud Console |
| `GMAIL_TOKEN_FILE` | `"data/gmail_token.json"` | Path to stored OAuth2 token (auto-refreshed) |

Both default to disabled — `check_replies()` is a no-op if `GMAIL_CREDENTIALS_FILE` is empty.

## Files Changed

| File | Change |
|---|---|
| `organist_bot/integrations/gmail_client.py` | New module |
| `organist_bot/reply_monitor.py` | New module |
| `organist_bot/application_store.py` | Add `update_reply_message_id`; add `rejected` to valid statuses |
| `organist_bot/integrations/unified_agent.py` | Enhance `manage_applications` update action; add `rejected` status handling |
| `organist_bot/config.py` | Add `GMAIL_CREDENTIALS_FILE`, `GMAIL_TOKEN_FILE` |
| `main.py` | Call `reply_monitor.check_replies()` each tick |
| `scripts/setup_gmail_auth.py` | New one-time auth setup script |
| `tests/test_reply_monitor.py` | New test file |
| `tests/test_gmail_client.py` | New test file |
| `tests/test_application_store.py` | Tests for `update_reply_message_id` |

## Tests

### `test_gmail_client.py`

| Test | Scenario |
|---|---|
| `test_fetch_reply_messages_returns_inbox_messages` | Inbox message FROM church email → returned |
| `test_fetch_reply_messages_returns_sent_messages_for_accepted` | Sent message TO accepted-record email → returned |
| `test_fetch_reply_messages_skips_sent_for_applied` | Sent message TO applied-only email → not returned |
| `test_fetch_reply_messages_fails_open` | API error → returns `[]` |

### `test_reply_monitor.py`

| Test | Scenario |
|---|---|
| `test_accepted_reply_creates_calendar_event` | Classified `accepted` → `upsert_accepted` + calendar event + notify |
| `test_rejected_reply_updates_status` | Classified `rejected` → status `rejected` + notify |
| `test_cancellation_reply_sends_telegram_prompt` | Classified `cancellation` → Telegram prompt, no auto status change |
| `test_unclear_reply_sends_telegram_excerpt` | Classified `unclear` → Telegram notify, no status change |
| `test_dedup_skips_processed_message` | `reply_message_id` already set → skipped |
| `test_no_matching_record_skips_message` | Message from unknown email → skipped |
| `test_check_replies_disabled_when_no_credentials` | `GMAIL_CREDENTIALS_FILE` empty → no-op |
| `test_check_replies_fails_open` | API error → logged at WARNING, no exception raised |

## Error Handling

- `check_replies()` failure must not affect the scheduler tick — wrapped in try/except in `main.py`, logged at WARNING + Telegram alert
- `gmail_client` fails open on API errors (returns `[]`)
- Classification failure for a single message is caught, logged, and skipped — does not abort remaining messages
- OAuth token refresh failure → logged at WARNING, `check_replies()` skips for that tick

## Out of Scope

- Sending replies from the bot (read-only monitoring)
- Monitoring non-application email threads
- Multi-message thread summarisation
- Income forecasting (Spec 3)
