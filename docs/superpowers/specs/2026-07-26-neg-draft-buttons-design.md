# NEG draft Accept/Edit/Reject buttons

## Problem

NEG-fee draft alerts (`main._send_neg_alert`) are plain Telegram text
messages. Acting on them requires typing exact commands (`approve <id>`,
`edit <id>: <text>`, `reject <id>`) with a 12-char gig id copied from the
message. There's no tap-to-act path, no way to say "raise the fee" without
knowing which draft you mean when more than one is pending, and an edit's
revised draft is never re-shown for confirmation — it's just inline text in
the chat.

## Goals

- Every NEG draft alert carries `Accept | Edit | Reject` buttons.
- Free text keeps working exactly as it partially does today (`approve
  <id>`, `edit <id>: ...`, `reject <id>`), but a `gig_id` becomes optional:
  the bot resolves it from context when unambiguous, and shows a tappable
  picker when it isn't.
- Every path that ends in "send an email" or "mark rejected" ends at the
  same tappable `Confirm | Cancel` state, regardless of whether it started
  from a button or free text.
- An edit (via button or free text) immediately becomes the new live draft
  and is re-shown with fresh `Accept | Edit | Reject` buttons.

## Non-goals

- Preset/quick-edit buttons (e.g. "+£20") — free text covers this.
- Persisting `_pending_neg_instruction` (the picker's in-flight
  instruction) across a bot restart — see Error Handling.
- Changing anything about how `main.py`'s scraper/filter pipeline decides
  which gigs get a NEG draft in the first place.

## Design

### New per-chat state (`unified_agent.py`)

```python
_active_neg_draft: dict[int, str] = {}       # chat_id -> gig_id
_pending_neg_instruction: dict[int, str] = {} # chat_id -> raw free text, picker-only
```

`_active_neg_draft` follows the exact pattern of `_last_invoice` /
`_last_gig_listing`: set whenever a draft is touched (Edit tap, picker
resolution, or a tool call that resolved an explicit `gig_id`), persisted
via `agent_state.py` (add `"active_neg_draft"` to `_KEYS`) so it survives a
restart. `_pending_neg_instruction` is in-memory only.

### `AgentResponse.buttons`

```python
@dataclass
class AgentResponse:
    text: str | None = None
    file_path: str | None = None
    file_caption: str | None = None
    buttons: list[list[dict]] | None = None  # rows of {"text": ..., "callback_data": ...}
```

Same shape python-telegram-bot's `InlineKeyboardMarkup` needs — `handle_message`
converts each row into `InlineKeyboardButton(**cell)` and passes the result
as `reply_markup` when sending.

### Callback data namespace

One prefix, one `CallbackQueryHandler`:

```
neg:accept:<gig_id>        neg:edit:<gig_id>          neg:reject:<gig_id>
neg:confirm_send:<gig_id>  neg:confirm_reject:<gig_id> neg:cancel:<gig_id>
neg:pick:<gig_id>
```

### Flow 1 — new draft alert (`main.py`, unchanged process)

`alert.send_alert` gains an optional `reply_markup: dict | None` param —
just another key in the existing raw HTTP POST, no new dependency.
`_send_neg_alert` attaches `Accept | Edit | Reject`
(`neg:accept:<id>` / `neg:edit:<id>` / `neg:reject:<id>`) to the draft
message (not the gig-details message).

### Flow 2 — button taps (`telegram_bot.py`, deterministic, no LLM call)

One `CallbackQueryHandler`, gated by the same `_is_authorised` check
`handle_message` uses. Every branch calls `update.callback_query.answer()`
immediately, then edits `update.callback_query.message` in place:

| Tap | Effect |
|---|---|
| `accept` / `reject` | Edit message to `Confirm \| Cancel` (`neg:confirm_send:<id>` / `neg:confirm_reject:<id>` / `neg:cancel:<id>`) |
| `edit` | Set `_active_neg_draft[chat_id] = gig_id`; edit message to "✏️ What would you like to change?", remove buttons |
| `confirm_send` | Send the email, `transition_neg_pending(to="applied")`, edit message to "✅ Sent" |
| `confirm_reject` | `transition_neg_pending(to="rejected")`, edit message to "❌ Rejected" |
| `cancel` | Re-render the message from the row's *current* stored draft with `Accept \| Edit \| Reject` restored |
| `pick:<gig_id>` | Read `_pending_neg_instruction[chat_id]`, set it active, replay via `process_message(chat_id, f"For gig {gig_id}: {instruction}")` — the one tap that does call the LLM, since the instruction text needs interpretation |

Every branch re-checks the row's actual status before acting (see Error
Handling) — a tap is a claim, not a guarantee the row is still `neg_pending`.

### Flow 3 — free text through the agent

`approve_neg_application` / `edit_neg_application` / `reject_neg_application`
lose `gig_id` from their schema's `required` list. Resolution order when
omitted:

1. Exactly one `neg_pending` row exists → use it.
2. `_active_neg_draft[chat_id]` is set → use it.
3. Otherwise → return a disambiguation result. `process_message` recognizes
   this shape (a new sentinel key, e.g. `{"needs_pick": [...]}`), stashes
   the raw user message in `_pending_neg_instruction[chat_id]`, and emits
   an `AgentResponse` with one `neg:pick:<id>` button per pending draft —
   reusing the same `buttons` field Flow 1/2 use.

### Flow 4 — edit resend

Once `edit_neg_application` resolves a target (via any of the three tools
above) and produces a revised subject/body (from `new_body`, or a
re-rendered `new_fee`), it immediately persists that as the row's current
draft via a new `application_store.update_neg_draft(gig_id, *,
draft_subject=None, draft_body=None, negotiable_fee=None)` — **before**
any confirmation — then returns a result carrying `Accept | Edit | Reject`
buttons (a fresh draft state, not `Confirm/Cancel`) attached to a new
message. `approve`/`reject` via free text resolve the same way and land on
`Confirm | Cancel`, converging with Flow 2.

## Error Handling

- **Stale/already-decided taps** (double-tap, or decided via a different
  path first): reuse `_find_neg_row` / `_neg_row_lookup_error`; edit the
  message to show the real state ("Already sent at …") instead of
  erroring or re-sending.
- **Race on `confirm_send`/`confirm_reject`**: `transition_neg_pending` is
  already file-locked and returns `False` if the row is no longer
  `neg_pending` — the handler must check this return value before editing
  the message to "✅ Sent", so two near-simultaneous taps can't
  double-send.
- **Callback acknowledgement**: `update.callback_query.answer()` is called
  first in every branch, wrapped so a bad/expired callback can't crash the
  polling loop.
- **Edit failures**: `edit_message_text` calls reuse the
  `BadRequest`-swallowing helper pattern already established for the
  progress-indicator message (`telegram_bot.py`).
- **Send failure on `confirm_send`**: message shows "❌ Send failed: …",
  row stays `neg_pending` (retryable) — matches today's
  `approve_neg_application` behavior.
- **Auth**: the callback handler runs the same `_is_authorised` check
  `handle_message` does, using `update.effective_chat`.
- **Restart mid-picker**: `_pending_neg_instruction` is in-memory only. A
  restart between showing the picker and a tap means the tap finds no
  stashed instruction — the handler sets `_active_neg_draft` anyway and
  replies "I lost track of what you asked — please repeat it for this
  draft" instead of crashing or silently no-op'ing.

## Testing

- `application_store.update_neg_draft`: persists subject/body/fee without
  changing `status`, no-ops gracefully on an unknown `gig_id`.
- Resolution-order tests on all three tools: single-pending shortcut,
  active-draft shortcut, ambiguous → disambiguation result shape.
- `CallbackQueryHandler` behavior for every `neg:*` pattern: correct
  deterministic action, correct edited message, auth rejection, stale-draft
  handling, double-tap race safety.
- `alert.send_alert` includes `reply_markup` in the POST body when given,
  omits the key when not.
- `AgentResponse.buttons` flows correctly through `process_message` for
  both the picker path and the edit-resend path.
- End-to-end-ish: ambiguous free text → picker buttons → tap → original
  instruction replayed against the picked draft.
