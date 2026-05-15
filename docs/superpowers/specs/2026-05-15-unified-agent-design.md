# Unified Telegram Agent — Design Spec
_Date: 2026-05-15_

## Overview

Replace the two specialised AI agents (`gig_agent.py`, `invoice_agent.py`) and all slash-command handlers with a single unified Claude agent that handles every intent through natural language. Fix Playwright browser cold-start to reduce invoice generation latency.

---

## Goals

1. Remove all Telegram slash commands — every action is driven by free text.
2. Consolidate `gig_agent.py` and `invoice_agent.py` into `unified_agent.py`.
3. Expose all filter management (blacklist, unavailable, available) as agent tools.
4. Expose gig calendar listing and deletion as agent tools.
5. Support natural-language conversation reset ("start over", "forget everything").
6. Fix Playwright cold-start: reuse a persistent browser across invoice generations.

---

## File Changes

| Action | File |
|--------|------|
| **New** | `organist_bot/integrations/unified_agent.py` |
| **New** | `tests/test_unified_agent.py` |
| **Modified** | `organist_bot/integrations/telegram_bot.py` |
| **Modified** | `organist_bot/integrations/invoice_generator.py` |
| **Deleted** | `organist_bot/integrations/gig_agent.py` |
| **Deleted** | `organist_bot/integrations/invoice_agent.py` |
| **Replaced** | `tests/test_telegram_integration.py` — rewritten; all existing handler-specific test classes are deleted and replaced with tests that mock `unified_agent.process_message` |
| **Deleted** | `tests/test_gig_agent.py` — logic absorbed into `tests/test_unified_agent.py` |

---

## Architecture

### `telegram_bot.py`

- Remove `ConversationHandler`, all `CommandHandler`s, and `_gig_listing` cache.
- Remove all handler functions: `addgig_entry`, `gig_chat`, `cancel`, `reset`, `cmd_gigs`, `cmd_deletegig`, `cmd_blacklist`, `cmd_unavailable`, `cmd_available`, `handle_invoice`.
- Register a single `MessageHandler(TEXT & ~COMMAND, handle_message)` that calls `unified_agent.process_message(chat_id, text)`.
- `handle_message` iterates `AgentResponse` objects returned by the agent and sends text or documents accordingly (same pattern as the current `handle_invoice`).
- Auth guard (`_is_authorised`) remains unchanged.
- Keep `/start` as the sole `CommandHandler` — it replies with a short natural-language description of capabilities instead of a command list.

### `unified_agent.py`

Single agent module with:
- One `SYSTEM_PROMPT` covering all three domains (see below).
- 19 tools (see Tools section).
- Per-chat state: `_histories: dict[int, list[dict]]`, `_last_invoice: dict[int, dict]`, `_last_gig_listing: dict[int, list[dict]]`.
- `async process_message(chat_id, text) -> list[AgentResponse]` — agentic loop using `anthropic.AsyncAnthropic` (not the sync client). Loop structure mirrors `gig_agent.process_message`.
- `reset_conversation(chat_id)` — clears `_histories[chat_id]`, `_last_invoice[chat_id]`, and `_last_gig_listing[chat_id]`. Called by the `clear_conversation` tool handler.
- `AgentResponse` dataclass: `text`, `file_path`, `file_caption` (same as today).

### `invoice_generator.py` — Playwright fix

Replace the per-call `async with async_playwright()` context manager with a module-level lazy singleton:

```python
_pw_instance = None
_browser = None

async def _get_browser():
    global _pw_instance, _browser
    if _browser is None or not _browser.is_connected():
        _pw_instance = await async_playwright().start()
        _browser = await _pw_instance.chromium.launch()
    return _browser
```

`generate_invoice` calls `_get_browser()` to get the browser, then opens a fresh page, prints to PDF, and closes the page. The browser process stays alive for the bot's lifetime, eliminating the ~3–5 s Chromium cold-start on every generation after the first.

**Async safety:** `python-telegram-bot` runs on a single asyncio event loop with default concurrency of 1 per chat. Concurrent calls to `_get_browser` from the same chat are not possible. Two simultaneous messages from different chats in theory could race, but in practice the bot processes one update at a time. No asyncio lock is needed, but implementers should note this assumption.

---

## Tools (19 total)

### Gig — scraping & calendar add
| Tool | Description |
|------|-------------|
| `fetch_gig_details(url)` | Scrape an organistsonline.org URL; returns structured gig fields |
| `add_gig(confirmed, header, date, time, organisation?, locality?, fee?)` | Two-phase: `confirmed=false` returns a formatted summary for user review; `confirmed=true` creates the calendar event and marks the date unavailable in `filter_store` |

### Gig — calendar management
| Tool | Description |
|------|-------------|
| `list_upcoming_gigs(max_results?)` | Lists upcoming Google Calendar gigs; stores result in `_last_gig_listing[chat_id]` for subsequent delete. Returns error text if Calendar is not configured. |
| `delete_gig(number)` | Deletes gig by 1-based index from last listing; removes date from unavailable periods. Returns error if Calendar not configured, listing not yet fetched, or index out of range. |

### Invoice — client management
| Tool | Description |
|------|-------------|
| `list_clients` | List all saved clients |
| `get_client(client_key)` | Full details for one client |
| `add_client(key, name, address, email?, cc?)` | Add new client |
| `edit_client(key, name?, address?, email?, cc?)` | Update client fields |
| `delete_client(key)` | Permanently delete a client (requires prior confirmation) |

### Invoice — generation & email
| Tool | Description |
|------|-------------|
| `generate_invoice(client_key, items)` | Generate PDF invoice |
| `duplicate_invoice(invoice_number)` | Clone a past invoice with today's date |
| `send_invoice_email` | Email the last generated invoice |
| `resend_invoice(invoice_number)` | Re-email a past invoice |
| `list_invoices(client_key?, limit?)` | List recent invoices |
| `get_invoice(invoice_number)` | Retrieve and send a past invoice PDF |

### Filter management
| Tool | Description |
|------|-------------|
| `manage_blacklist(action, email?)` | `action` ∈ `list \| add \| remove`. Note: the underlying `filter_store` uses `rm` internally; the tool normalises to `remove` for natural-language clarity. |
| `manage_unavailable(action, period?)` | `action` ∈ `list \| add \| remove`; period formats: `2026-12-25`, `2026-12-20:2027-01-05`, `2026-12` |
| `manage_available(action, period?)` | `action` ∈ `list \| add \| remove` |

### Meta
| Tool | Description |
|------|-------------|
| `clear_conversation` | Clears `_histories`, `_last_invoice`, and `_last_gig_listing` for this chat. |

---

## System Prompt Structure

```
You are an assistant for an organist. You handle three areas:

## Gig calendar
- If the user provides a URL, call fetch_gig_details immediately.
- Gather any missing fields (header, organisation, locality, date, time, fee) one at a time.
- Always call add_gig(confirmed=false) first to show a summary; only call confirmed=true after explicit approval.
- "Show my gigs" / "list gigs" → call list_upcoming_gigs.
- "Delete gig 2" → call delete_gig(2); tell the user to list gigs first if no listing is cached.

## Invoicing
- Confirm before generating, duplicating, sending, or deleting.
- Ask if missing: client, description, quantity, unit price.
- After generating, offer to email.
- Use £ for money.

## Filter management
- "Add Holy Cross to the blacklist" → manage_blacklist(add, email).
- "I'm unavailable in December" → manage_unavailable(add, 2026-12).
- Period formats: YYYY-MM-DD, YYYY-MM-DD:YYYY-MM-DD, YYYY-MM.

## Conversation
- If the user asks to start over, reset, or forget everything → call clear_conversation.

## General
- Keep responses concise — this is a chat interface.
- Use British English.
- Use £ for money.
```

---

## Confirmation Rules (unchanged from today)

These tools require user confirmation before the action:
- `add_gig` — always two-phase (`confirmed=false` summary first)
- `generate_invoice`, `duplicate_invoice`, `send_invoice_email`, `resend_invoice`, `delete_client`

---

## Deliberate Behaviour Changes

- **`/cancel` removed:** Previously `/cancel` aborted only an in-progress gig entry, leaving invoice history intact. Now `clear_conversation` wipes all per-chat state (all three dicts). There is no way to cancel only gig entry. This is an acceptable trade-off given the unified history model.
- **Filter action vocabulary:** The three filter tools accept `remove` (not `rm`). The tool handlers map `remove` → `filter_store.remove_*` internally.

---

## Testing

### `tests/test_unified_agent.py` (new)
Must cover:
- Gig tool dispatch: `fetch_gig_details`, `add_gig(confirmed=false)`, `add_gig(confirmed=true)` (verifies calendar event created and date added to `unavailable_periods`)
- Invoice tool dispatch: `generate_invoice`, `list_clients`, `send_invoice_email`
- Filter tool dispatch: `manage_blacklist` (add, remove, list), `manage_unavailable` (add, remove, list), `manage_available` (add, remove, list)
- Gig listing & delete: `list_upcoming_gigs`, `delete_gig` (happy path and "no listing cached" error)
- `clear_conversation`: verifies all three per-chat dicts are cleared
- `add_gig(confirmed=true)` auto-unavailable: verifies `filter_store.add_period("unavailable_periods", ...)` is called (regression coverage for behaviour from deleted `test_gig_agent.py::TestExecuteToolAddGigAutoUnavailable`)

### `tests/test_telegram_integration.py` (rewritten)
- Delete all existing handler-specific test classes (`TestCmdGigs`, `TestCmdDeleteGig`, `TestAddGigEntry`, `TestHandleInvoice`, etc.).
- Replace with tests that mock `unified_agent.process_message` and verify `handle_message` correctly sends text responses and documents.
- Auth rejection test remains.

### Existing tests unaffected
- `tests/test_filters.py`, `tests/test_scraper.py`, `tests/test_notifier.py`, `tests/test_storage.py`, `tests/test_calendar_client.py`, `tests/test_sheets_logger.py`, `tests/test_filter_store.py`, `tests/test_main.py`.

---

## Out of Scope

- No changes to the scraper pipeline (`main.py`, `filters.py`, `notifier.py`).
- No changes to `calendar_client.py`, `email_sender.py`, or `sheets_logger.py`.
- No changes to `filter_store.py` — the agent tools call it directly.
