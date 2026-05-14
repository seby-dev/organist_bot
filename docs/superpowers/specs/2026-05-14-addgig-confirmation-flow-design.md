# /addgig Confirmation Flow — Design Spec

**Date:** 2026-05-14
**Status:** Approved

## Problem

The current `/addgig` implementation has three fragilities:

1. **Fragile `done` detection** — completion is inferred by string-matching `"done=true"` or `"added to calendar"` in the agent's reply text. The model can phrase things differently and the `ConversationHandler` stays open indefinitely.
2. **No reliable confirmation step** — the system prompt asks the agent to confirm before adding, but this is a soft convention. The agent can skip it.
3. **Sync client in async context** — `anthropic.Anthropic` (blocking) is called inside an `async` function, blocking the bot's event loop on every API call.

Additionally, the URL regex gate in `telegram_bot.py` rejects non-`organistsonline.org` URLs before the agent sees them, preventing the agent from handling the validation gracefully.

## Goal

- The agent handles both URL-provided and conversational gig entry uniformly — no special-casing in `telegram_bot.py`.
- The agent always asks for any missing fields before confirming.
- The agent always presents a formatted confirmation summary before writing to calendar.
- The user can edit fields in plain English; the agent re-presents the updated summary.
- `done` is tied to a real calendar write, not text parsing.

## Approach: Two-phase `add_gig` tool (Approach C)

Replace the current `add_gig` tool with a version that has a `confirmed: bool` parameter.

- `confirmed=false` — formats and returns a markdown summary of the gig details. No calendar write. Used for preview and re-preview after edits.
- `confirmed=true` — writes to Google Calendar. Sets `done=True` on the `GigAgentResponse`. Only reachable after the user explicitly approves.

This makes `done` an event-driven signal (calendar write succeeded) rather than text parsing.

## Changes

### `organist_bot/integrations/gig_agent.py`

**Tool schema** — `add_gig` gains a required `confirmed: bool` field. All six gig fields are listed as properties; `confirmed`, `header`, `date`, and `time` are required. The system prompt (see below) instructs the agent to always pass all fields when calling `confirmed=true`, preventing stale/dropped values between an edit preview and the final commit:

```python
{
    "name": "add_gig",
    "description": (
        "Two-phase gig calendar tool. "
        "Call with confirmed=false to generate a confirmation summary for the user. "
        "Call with confirmed=true only after the user has explicitly approved. "
        "When calling with confirmed=true, always include all fields shown in the last summary."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "confirmed":    {"type": "boolean"},
            "header":       {"type": "string"},
            "organisation": {"type": "string"},
            "locality":     {"type": "string"},
            "date":         {"type": "string", "description": "e.g. 'Sunday 1st June 2025'"},
            "time":         {"type": "string", "description": "e.g. '10:30am'"},
            "fee":          {"type": "string", "description": "e.g. '£150'"},
        },
        "required": ["confirmed", "header", "date", "time"],
    },
}
```

**`_execute_tool`** — `add_gig` branch splits on `confirmed`:

- `confirmed=false`: returns a formatted markdown summary string (no side-effect). Example:
  ```
  *Please confirm the following gig:*
  • *Title:* Eucharist Service
  • *Organisation:* St Mary's Church
  • *Locality:* Oxford
  • *Date:* Sunday 1st June 2025
  • *Time:* 10:30am
  • *Fee:* £150

  Reply *yes* to add to calendar, or tell me what to change.
  ```
- `confirmed=true`: writes to Google Calendar, returns `{"result": "Added. Event ID: ..."}`. Sets `done=True`.

**`done` detection** — removed from text parsing entirely. `done=True` is set only when `add_gig` is called with `confirmed=true` and returns a `"result"` key.

**Async client** — swap `anthropic.Anthropic` → `anthropic.AsyncAnthropic`, `client.messages.create` → `await client.messages.create`.

**System prompt** — rewritten to enforce two-phase flow, handle `fetch_gig_details` errors, define ambiguous-approval behaviour, and require full fields on confirm:

```
You are a gig calendar assistant for an organist. Your job is to add gig bookings to Google Calendar.

## Gathering details
- If the user provides a URL, call fetch_gig_details immediately. Use the returned fields to pre-fill as much as possible.
- If fetch_gig_details returns an error, tell the user plainly and ask them to enter the details manually.
- If any of header, organisation, locality, date, time, or fee are missing after fetching (or no URL was given), ask for them one at a time in a natural conversational way.
- Do not ask for a field you already have.

## Confirmation (always required)
- Once you have all fields, call add_gig with confirmed=false. This returns a formatted summary — relay it to the user verbatim.
- Wait for the user's reply before doing anything else.
- If the user approves (e.g. "yes", "looks good", "confirm"), call add_gig with confirmed=true. Always include all fields from the last summary in the confirmed=true call.
- If the user requests a change (e.g. "change the fee to £200"), update the relevant field(s) and call add_gig with confirmed=false again with the full corrected fields. Repeat until confirmed.
- If the user's reply is ambiguous (a question, "maybe", etc.), treat it as a change request and re-present the summary with a prompt to confirm or edit.

## Rules
- Never call add_gig with confirmed=true unless the user has explicitly approved the summary in this conversation.
- Keep responses concise — this is a chat interface.
- Use British English and £ for fees.
- Once add_gig(confirmed=true) succeeds, your job is done.
```

### `organist_bot/integrations/telegram_bot.py`

**Remove URL regex gate** — delete the `_GIG_URL_RE` check in `addgig_entry` that rejects non-`organistsonline.org` URLs. Forward all input to the agent.

**Simplify entry message** — pass the user's raw input to the agent:
- With args: send `context.args[0]` (the URL or whatever the user typed) as the first message
- Without args: send an empty string (the agent opens the conversation naturally)

All other handlers (`gig_chat`, `cancel`, `ConversationHandler` wiring) are unchanged.

**Conversation history** — the existing `_histories` dict in `gig_agent.py` stores every turn as raw message dicts, including tool-use and tool-result turns. This is unchanged; the agent's context across `gig_chat` calls already includes the full `add_gig(confirmed=false)` exchange, so it will not re-ask for fields it already has.

**URL gate removal and cost** — removing `_GIG_URL_RE` means any free text passed as an `/addgig` argument is forwarded to the Claude API. This is acceptable because `addgig_entry` is already gated by `_is_authorised` (matching `TELEGRAM_CHAT_ID`), limiting exposure to a single trusted chat.

## Data flow

```
/addgig [url?]
  → addgig_entry: reset history, send raw input to agent
  → gig_agent.process_message:
      Claude loop:
        if URL → fetch_gig_details → fill fields
        ask for missing fields one at a time
        add_gig(confirmed=false) → return summary to user
      → CHATTING state
  → user replies (confirm or edit)
  → gig_chat → gig_agent.process_message:
      if edit → update fields → add_gig(confirmed=false) → re-present summary
      if confirm → add_gig(confirmed=true) → done=True
  → ConversationHandler.END
```

## Out of scope

- Changing the invoice agent
- Filter management commands
- Any changes to the scraper or calendar client
