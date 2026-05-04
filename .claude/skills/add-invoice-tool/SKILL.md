---
name: add-invoice-tool
description: Use when adding a new tool to the invoice AI agent in organist_bot — defining the JSON schema, implementing the handler, and deciding whether it triggers a PDF send.
---

# Add an Invoice Agent Tool

## Overview

The invoice agent (`organist_bot/integrations/invoice_agent.py`) is a Claude tool-use loop. Adding a tool requires three coordinated changes in that file, plus an optional system prompt update.

## Three-Place Pattern

### 1. Add JSON schema to `TOOLS` list

```python
{
    "name": "my_tool",
    "description": "What this tool does and when Claude should call it.",
    "input_schema": {
        "type": "object",
        "properties": {
            "param_name": {
                "type": "string",           # or "integer", "number", "array", "boolean"
                "description": "What this param is, e.g. 'Client key, e.g. holy-cross'",
            },
        },
        "required": ["param_name"],         # omit optional params
    },
},
```

The `description` field is what Claude reads to decide whether to call the tool. Be explicit about preconditions, e.g. "Use list_clients first to find the correct key."

### 2. Add a branch in `_execute_tool()`

```python
elif name == "my_tool":
    # implement here
    # always return json.dumps({...})
    return json.dumps({"result": "..."})
```

**Async vs sync:** `_execute_tool` is `async`. Most tools are synchronous — just implement them normally. Only use `await` if the underlying function is async (currently only `generate_invoice` and `duplicate_invoice` use Playwright and need `await`).

**Error handling:** Return `{"error": "..."}` for expected failures (not found, invalid input). Let unexpected exceptions propagate to the caller's `try/except` in `process_message`.

```python
elif name == "my_tool":
    try:
        result = do_something(input_data["param_name"])
        return json.dumps({"result": result})
    except ValueError as e:
        return json.dumps({"error": str(e)})
```

### 3. Decide: does it send a PDF?

If your tool retrieves or generates a PDF that should be sent back to Telegram, add its name to:

```python
_PDF_RESPONSE_TOOLS = {"generate_invoice", "duplicate_invoice", "get_invoice", "my_tool"}
```

This set is checked in `process_message` after each tool call — if the tool name is in it and `_last_invoice[chat_id]` is set, the PDF at `_last_invoice[chat_id]["pdf_path"]` is queued for sending. Make sure your tool sets `_last_invoice[chat_id]` before returning.

## Optional: Update the System Prompt

If Claude needs to know when to call your tool (or in what order relative to other tools), add a rule to `SYSTEM_PROMPT`:

```python
SYSTEM_PROMPT = """\
...existing rules...
- Use my_tool when the user asks about X. Always call list_clients first to verify the client exists.
"""
```

Keep rules concise — the system prompt is included in every API call.

## Quick Reference

| Location | What to add |
|---|---|
| `TOOLS` list | JSON schema with `name`, `description`, `input_schema` |
| `_execute_tool()` | `elif name == "my_tool":` branch returning `json.dumps({...})` |
| `_PDF_RESPONSE_TOOLS` | Tool name, only if it retrieves/generates a PDF |
| `SYSTEM_PROMPT` | Rule for when/how to call it, only if non-obvious |

## Common Mistakes

| Mistake | Consequence |
|---|---|
| Missing `elif` branch in `_execute_tool` | Returns `{"error": "Unknown tool: my_tool"}` — Claude retries forever |
| Using `async`/`await` for a sync tool | No harm but misleading; only needed for Playwright-based functions |
| Setting `_last_invoice[chat_id]` but not adding to `_PDF_RESPONSE_TOOLS` | PDF generated but never sent to Telegram |
| Vague tool `description` | Claude calls the wrong tool or calls this one at the wrong time |
