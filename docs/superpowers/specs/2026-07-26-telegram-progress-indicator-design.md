# Telegram progress indicator

## Problem

`unified_agent.process_message` runs a multi-turn agentic loop — each turn is a
Claude API call, and a turn that requests a tool triggers another turn after
the tool result comes back. For requests that need several tool calls this can
take many seconds, during which `telegram_bot.py` gives the user no feedback
at all. The user can't tell whether the bot is working or has hung.

## Goals

- Show live, in-place progress in Telegram while `process_message` runs,
  updating as each tool call starts and finishes.
- Leave the final chat output unchanged: once the agent finishes, the
  progress message disappears and the real reply/replies are sent exactly as
  today.
- No behavior change for any caller that doesn't opt in (tests, future
  callers).

## Non-goals

- A human-readable label per tool (27 tools) — raw tool names are shown as-is.
- Persisting progress state across a bot restart — a progress message only
  needs to survive the single request it belongs to.
- Debouncing/rate-limiting edits — the loop's tool-call count is small enough
  in practice that Telegram's edit rate limits aren't a concern.

## Design

### `unified_agent.process_message`

Add an optional parameter:

```python
async def process_message(
    chat_id: int,
    text: str,
    on_step: Callable[[str], Awaitable[None]] | None = None,
) -> list[AgentResponse]:
```

Default `None` — every existing call site and test is unaffected.

Inside the `while True` loop, maintain a local `steps: list[str]`. For each
`tool_use` block, immediately before calling `_execute_tool`:

1. Append `f"🔧 {block.name}"` to `steps`.
2. If `on_step` is set, `await on_step("\n".join(steps))`.

Immediately after `_execute_tool` returns (success or caught exception):

3. Replace that last entry with `f"✅ {block.name}"`.
4. If `on_step` is set, `await on_step("\n".join(steps))` again.

No step is emitted for the plain "thinking" API call itself — the placeholder
message's initial text (see below) covers that, and there's no meaningful
sub-progress to report while waiting on the model.

### `telegram_bot.py handle_message`

1. Before calling `process_message`, send a placeholder: `status_msg =
   await update.message.reply_text("🤔 Thinking…")`.
2. Build a closure:

   ```python
   async def on_step(status_text: str) -> None:
       try:
           await context.bot.edit_message_text(
               chat_id=chat_id, message_id=status_msg.message_id, text=status_text
           )
       except BadRequest as exc:
           if "message is not modified" not in str(exc).lower():
               logger.debug("Telegram: progress edit failed: %s", exc)
   ```

   `"message is not modified"` is Telegram's expected response when two
   consecutive edits produce identical text (e.g. a fast tool call whose
   🔧→✅ transition lands between polls) — silently ignored, not logged.
3. Call `responses = await unified_agent.process_message(chat_id, text,
   on_step=on_step)`.
4. Whether that call succeeds or raises, delete the placeholder via a small
   `_delete_quietly(context, chat_id, message_id)` helper (wraps
   `context.bot.delete_message` in the same try/except-BadRequest pattern).
   On the success path this happens right before the existing response-send
   loop; on the exception path it happens right before the existing
   `❌ Unexpected error` reply.

No other part of `handle_message`'s existing control flow changes.

## Testing

- Update `tests/test_telegram_integration.py`: every test that currently
  asserts `reply_text.assert_called_once_with(...)` now needs to account for
  the leading placeholder `reply_text` call, plus mock
  `context.bot.edit_message_text` and `context.bot.delete_message`.
- Add a test asserting the placeholder is deleted on both the success path
  and the exception path.
- Add a test asserting a `BadRequest("message is not modified")` from
  `edit_message_text` doesn't propagate.
- Add a new test in `tests/test_unified_agent.py` for `process_message`'s
  `on_step` sequencing: monkeypatch `anthropic.AsyncAnthropic` (referenced as
  a local import inside `process_message`) with a fake client whose
  `messages.create` returns a canned `tool_use` turn followed by an
  `end_turn` turn, and assert `on_step` is called with the expected
  `🔧 tool_name` then `✅ tool_name` strings in order.
