from __future__ import annotations

import datetime
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import cast

from organist_bot import (
    alert,
    analytics,
    application_store,
    filter_store,
    filter_suspension_store,
    llm_usage_store,
    travel,
)
from organist_bot.config import settings
from organist_bot.filters import normalize_to_yyyymmdd, parse_start_time
from organist_bot.integrations import agent_state
from organist_bot.integrations.calendar_client import (
    GoogleCalendarClient,
)
from organist_bot.integrations.calendar_client import (
    make_calendar_client as _make_calendar_client,
)
from organist_bot.integrations.email_sender import send_invoice_email
from organist_bot.integrations.invoice_generator import (
    add_client,
    delete_client,
    delete_invoice,
    edit_client,
    generate_invoice,
    load_clients,
    load_invoices,
    mark_invoice_emailed,
    mark_invoice_paid,
    unmark_invoice_paid,
)
from organist_bot.models import Gig
from organist_bot.notifier import Notifier, SMTPTransport, send_application_email
from organist_bot.runtime_config_store import runtime_config
from organist_bot.scraper import Scraper

logger = logging.getLogger(__name__)

_PROVIDER_MODELS: dict[str, dict[str, str]] = {
    "anthropic": {
        "sonnet": "anthropic/claude-sonnet-4-6",
        "opus": "anthropic/claude-opus-4-6",
        "haiku": "anthropic/claude-haiku-4-5-20251001",
    },
    "openai": {
        "gpt-6-astra": "openai/gpt-6-astra",
        "gpt-5.6-luna": "openai/gpt-5.6-luna",
    },
    "gemini": {
        "gemini-pro": "gemini/gemini-3.1-pro-preview",
        "gemini-3.8-flash": "gemini/gemini-3.8-flash",
    },
}
_DEFAULT_PROVIDER = "anthropic"
_DEFAULT_MODEL_KEY = "sonnet"
_PROVIDER_API_KEY_FIELD = {
    "anthropic": "anthropic_api_key",
    "openai": "openai_api_key",
    "gemini": "gemini_api_key",
}
# Fixed walk order for automatic failover -- deliberately not derived from
# _PROVIDER_MODELS (dict insertion order is an implementation detail, not a
# contract) and not configurable, matching this being a single global
# default rather than a per-chat/per-tier setting.
_FAILOVER_ORDER = ["anthropic", "openai", "gemini"]
# Which curated model a provider fails over TO, since the currently-active
# model key may not exist for that provider (e.g. "opus" has no OpenAI
# equivalent key). Deliberately NOT each provider's flagship: OpenAI's
# flagship "gpt-6-astra" is a reasoning model that OpenAI's own API refuses
# to run with function tools at all on the chat-completions endpoint this
# codebase uses (needs /v1/responses instead, and separately rejects
# reasoning_effort="none" as a workaround) -- confirmed live against the real
# API. Every conversation here always sends tools, so gpt-6-astra can never
# actually serve a failover call; "gpt-5.6-luna" is openai's other curated
# option and works fine with tools.
_DEFAULT_MODEL_KEY_PER_PROVIDER = {
    "anthropic": "sonnet",
    "openai": "gpt-5.6-luna",
    "gemini": "gemini-pro",
}

# Models that reject function tools on /v1/chat/completions and must go through
# /v1/responses instead (litellm.aresponses()) -- see
# docs/superpowers/specs/2026-09-07-gpt6-astra-responses-api-design.md. Currently
# just gpt-6-astra; every other curated model keeps using acompletion().
_RESPONSES_API_MODELS = {"openai/gpt-6-astra"}


def _default_model_string() -> str:
    return _PROVIDER_MODELS[_DEFAULT_PROVIDER][_DEFAULT_MODEL_KEY]


def _configured_providers() -> list[str]:
    """Providers with a non-empty API key, in the fixed failover order."""
    return [p for p in _FAILOVER_ORDER if getattr(settings, _PROVIDER_API_KEY_FIELD[p])]


def _record_llm_usage(provider: str, model: str, response) -> None:
    """Best-effort usage tracking -- a logging/storage failure must never
    break the chat turn that triggered the real LLM call it's recording."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    try:
        llm_usage_store.record_call(
            provider,
            model,
            getattr(usage, "prompt_tokens", 0) or 0,
            getattr(usage, "completion_tokens", 0) or 0,
        )
    except Exception:
        logger.warning("llm_usage_store: failed to record call", exc_info=True)


_REASON_TRUNCATE_LEN = 300


def _truncate_reason(exc: Exception) -> str:
    """Cap a provider's exception text so a verbose provider error (looking
    at you, Gemini's multi-KB quota-violation JSON) can't blow past Telegram's
    4096-char message limit once several are joined into one alert/message."""
    text = str(exc)
    return text if len(text) <= _REASON_TRUNCATE_LEN else text[: _REASON_TRUNCATE_LEN - 1] + "…"


def _item_get(item, key, default=None):
    """Accessor tolerant of both a pydantic response-item object (attribute
    access) and a plain dict -- litellm's Responses API objects are pydantic
    models, but this keeps the translators trivially unit-testable with dicts."""
    return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)


def _chat_message_from_responses_output(output_items) -> SimpleNamespace:
    """Build an object exposing .content / .tool_calls / .model_dump() shaped
    exactly like litellm.acompletion()'s response.choices[0].message, from a
    Responses API response's `.output` list."""
    text_parts: list[str] = []
    tool_calls: list[SimpleNamespace] = []
    for item in output_items:
        item_type = _item_get(item, "type")
        if item_type == "message":
            for content_item in _item_get(item, "content", []) or []:
                if _item_get(content_item, "type") == "output_text":
                    text_parts.append(_item_get(content_item, "text", "") or "")
        elif item_type == "function_call":
            tool_calls.append(
                SimpleNamespace(
                    id=_item_get(item, "call_id"),
                    function=SimpleNamespace(
                        name=_item_get(item, "name"),
                        arguments=_item_get(item, "arguments", "{}"),
                    ),
                )
            )
        # "reasoning" items (and any other type) are intentionally not surfaced
        # here -- they're not replayed manually; see previous_response_id
        # chaining in _call_openai_responses_api.

    tool_calls_out = tool_calls or None
    content = ("".join(text_parts) or None) if not tool_calls_out else None

    def _model_dump() -> dict:
        return {
            "role": "assistant",
            "content": content,
            "tool_calls": (
                [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls_out
                ]
                if tool_calls_out
                else None
            ),
        }

    return SimpleNamespace(content=content, tool_calls=tool_calls_out, model_dump=_model_dump)


def _chat_response_from_responses_api(response) -> SimpleNamespace:
    """Wrap a Responses API result as a chat-completions-shaped response object
    (`.choices[0].message`, `.usage.prompt_tokens/.completion_tokens`) -- same
    field names _record_llm_usage() already reads, translated from the
    Responses API's own usage.input_tokens/output_tokens."""
    message = _chat_message_from_responses_output(response.output)
    usage = _item_get(response, "usage")
    chat_usage = (
        SimpleNamespace(
            prompt_tokens=_item_get(usage, "input_tokens", 0) or 0,
            completion_tokens=_item_get(usage, "output_tokens", 0) or 0,
        )
        if usage is not None
        else None
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=chat_usage)


async def _call_llm_with_failover(
    provider: str, model: str, messages: list[dict], tools: list[dict]
):
    """Call litellm.acompletion against `provider`/`model`; on failure, try
    the other configured providers (fixed order, skipping any without an API
    key and whichever was just attempted) until one succeeds. Records usage
    for whichever call actually succeeded.

    On the first success after at least one failure, persists the working
    provider/model as the new default via runtime_config and fires a Telegram
    alert -- so a provider outage self-heals instead of silently breaking
    every message until a human notices and switches manually. If EVERY
    configured provider fails, fires a Telegram alert listing every provider
    tried and its own error, then raises a RuntimeError whose message is that
    same summary (chained onto the last provider's exception) -- so the
    generic "Unexpected error" text the caller falls back to (see
    telegram_bot.handle_message) shows what was actually tried instead of
    just whichever provider happened to be attempted last.

    Returns (response, provider_used, model_used) so the caller's own
    provider/model tracking stays in sync for the rest of that conversation
    turn, not just for this one call.
    """
    import litellm

    candidates = [(provider, model)]
    for p in _configured_providers():
        if p == provider:
            continue
        candidates.append((p, _PROVIDER_MODELS[p][_DEFAULT_MODEL_KEY_PER_PROVIDER[p]]))

    attempts: list[tuple[str, Exception]] = []
    for i, (p, m) in enumerate(candidates):
        try:
            response = await litellm.acompletion(
                model=m,
                # Not max_tokens: some providers' newer models (e.g. OpenAI's
                # reasoning-style models) reject it outright and require
                # max_completion_tokens instead. litellm's per-provider
                # transformation layer accepts this generic name for every
                # provider here and maps it to that provider's native param
                # (Anthropic/Gemini included), so this one name is safe
                # everywhere -- unlike max_tokens, which isn't for all of them.
                max_completion_tokens=4096,
                api_key=getattr(settings, _PROVIDER_API_KEY_FIELD[p]),
                messages=messages,
                tools=tools,
            )
        except Exception as exc:
            logger.warning("LLM call failed for provider %s: %s", p, exc)
            attempts.append((p, exc))
            continue

        if i > 0:
            runtime_config.set("llm_provider", p)
            runtime_config.set("llm_model", m)
            reason = alert.escape_markdown_v2(_truncate_reason(attempts[-1][1]))
            alert.send_alert(
                f"🔀 *AI provider auto\\-switched*\n{provider} → {p}\n\nReason: {reason}",
                parse_mode="MarkdownV2",
            )
        _record_llm_usage(p, m, response)
        return response, p, m

    plain_summary = "\n".join(f"{p}: {_truncate_reason(exc)}" for p, exc in attempts)
    escaped_summary = "\n".join(
        f"{p}: {alert.escape_markdown_v2(_truncate_reason(exc))}" for p, exc in attempts
    )
    alert.send_alert(
        f"🔴 *All configured AI providers failed*\n{escaped_summary}",
        parse_mode="MarkdownV2",
    )
    raise RuntimeError(f"All configured providers failed:\n{plain_summary}") from attempts[-1][1]


SYSTEM_PROMPT = """\
You are an assistant for an organist. You handle three areas:

## Gig calendar
- If the user provides a URL, call fetch_gig_details immediately.
- If fetch_gig_details returns an error, tell the user plainly and ask them to enter the details manually.
- Gather any missing fields (header, organisation, locality, date, time, fee) one at a time.
- Always call add_gig(confirmed=false) first to show a summary; only call confirmed=true after explicit approval.
- "Show my gigs" / "list gigs" → call list_upcoming_gigs.
- "Delete gig 2" → call delete_gig(2). Tell the user to list gigs first if no listing is cached.
- "Change gig 2 to 11am" / "Rename gig 1" → call edit_gig. Tell the user to list gigs first if no listing is cached.

## Invoicing
- Confirm before calling generate_invoice, duplicate_invoice, send_invoice_email, resend_invoice, delete_client, or delete_invoice. Present a clear summary and ask "Shall I go ahead?"
- If missing required info (client, description, quantity, or unit price), ask for the missing details.
- Invoices can have multiple line items — ask if the user wants to add more items before generating.
- After generating an invoice, ask if the user wants to email it.
- Use list_clients to look up available client keys when the user mentions a client by name.
- Use list_invoices to look up past invoices when the user mentions a client or date.
- "Mark INV-2026-001 as paid" / "invoice has been paid" → mark_invoice_paid.
- "Unmark INV-2026-001 as paid" / "that invoice isn't actually paid" → unmark_invoice_paid.
- "Delete INV-2026-001" / "remove that invoice" → delete_invoice (confirm first).
- Use £ for money.

## Filter management
- "Add <email> to the blacklist" → manage_blacklist(action=add, email=<email>).
- "Remove <email> from the blacklist" → manage_blacklist(action=remove, email=<email>).
- "I'm unavailable in December" → manage_unavailable(action=add, period=2026-12).
- "I'm unavailable on 25 Dec" → manage_unavailable(action=add, period=2026-12-25).
- "Add an available-only period" → manage_available(action=add, period=<period>).
- Period formats: YYYY-MM-DD, YYYY-MM-DD:YYYY-MM-DD, YYYY-MM. Also: today, tomorrow, this/next <weekday>, this weekend, next week, this/next month.
- "Turn off the postcode filter for all of December" → manage_filter_suspensions(action=add, filter=postcode, period=2026-12).
- "Ignore the fee filter from August 1st onward" → manage_filter_suspensions(action=add, filter=fee, period=2026-08-01:).
- "Disable every filter until the 5th of January" → manage_filter_suspensions(action=add, filter=all, period=:2026-01-05).
- "What filters are currently suspended?" → manage_filter_suspensions(action=list).
- "Resume the postcode filter" → manage_filter_suspensions(action=remove, filter=postcode, period=<period from the last list>).
- The 'seen' filter cannot be suspended — if asked, explain that suspending it would just resend the same application every poll tick instead of exempting a category of gig.

## Runtime config
- "What's the current config?" / "show config" → manage_config(action=get).
- "Set min fee to 150" → manage_config(action=set, key=min_fee, value=150).
- "Reset min fee to default" → manage_config(action=reset, key=min_fee).
- Editable keys: min_fee, max_travel_minutes, poll_minutes, negotiable_fee.

## LLM provider
- "Switch to GPT-6 Astra" / "use Gemini" / "what model are we using?" → manage_llm_provider.
- If you say a provider without a model, I'll list that provider's options and ask which one.
- A requested switch isn't immediate — I'll show a Confirm/Cancel button first.
- If a provider errors mid-conversation, I automatically fail over to another configured
  provider and let you know — you don't need to switch manually when one goes down.
- "How much have I used?" / "usage summary" / "which provider costs more?" → get_llm_usage_summary.

## Application tracking
- "What applications are pending?" / "show my applications" → manage_applications(action=list).
- "Application summary" / "how many gigs have I applied to?" → manage_applications(action=summary).
- "Mark application 2 as declined" → manage_applications(action=update, number=2, status=declined).
- "Show me full details of application 3" / "tell me more about #2" → manage_applications(action=detail, number=3).
- Valid statuses for update: applied, accepted, no_response, declined.
- "What's my acceptance rate?" / "show analytics" → get_application_analytics.
- "Break down my gigs by type" / "which gig types do I win?" → get_gig_breakdown.

## NEG-fee drafts
- "What NEG drafts are pending?" → list_neg_pending.
- "Approve <id>" / "approve it" / "send it" → approve_neg_application(gig_id if the user gave one, otherwise omit it).
- "Edit <id>: <new text>" / "raise the fee to 150" → edit_neg_application(gig_id if given, new_body or new_fee).
- "Reject <id>" / "reject it" → reject_neg_application(gig_id if given, otherwise omit it).
- Every one of these tools returns tappable buttons for the user to actually confirm sending or rejecting — never ask the user to reply "confirmed" yourself, and never call any of these tools a second time to "confirm" something. The buttons handle that.

## Conversation
- If the user asks to start over, reset, or forget everything → call clear_conversation.

## General
- Keep responses concise — this is a chat interface.
- Use British English.
- Use £ for money.
- This is Telegram: NEVER use markdown tables, headers, or other markdown formatting — they render as raw text. Use plain text with simple bullet lines (•) only.
- When a tool returns a pre-formatted list (e.g. availability periods, invoices), relay it VERBATIM — do not reformat, renumber, or convert it into a table. You may append a short follow-up note after the list.
"""

_TOOLS_SCHEMA: list[dict] = [
    # ── Gig — scraping & calendar add ──────────────────────────────────────
    {
        "name": "fetch_gig_details",
        "description": "Fetch gig details from an organistsonline.org URL.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "The full gig detail URL."}},
            "required": ["url"],
        },
    },
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
                "confirmed": {"type": "boolean"},
                "header": {"type": "string"},
                "organisation": {"type": "string"},
                "locality": {"type": "string"},
                "date": {"type": "string", "description": "e.g. 'Sunday 1st June 2025'"},
                "time": {"type": "string", "description": "e.g. '10:30am'"},
                "fee": {"type": "string", "description": "e.g. '£150'"},
                "url": {
                    "type": "string",
                    "description": "Source gig URL from fetch_gig_details. Omit for manual entries.",
                },
                "postcode": {
                    "type": "string",
                    "description": "Gig venue postcode for travel buffer calculation (e.g. CM1 1AA)",
                },
            },
            "required": ["confirmed", "header", "date", "time"],
        },
    },
    # ── Gig — calendar management ───────────────────────────────────────────
    {
        "name": "list_upcoming_gigs",
        "description": "List upcoming gigs from Google Calendar. Returns a numbered list.",
        "input_schema": {
            "type": "object",
            "properties": {
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of gigs to return (default 10).",
                }
            },
            "required": [],
        },
    },
    {
        "name": "delete_gig",
        "description": (
            "Delete a gig from Google Calendar by its 1-based position from the last list_upcoming_gigs call. "
            "Also removes the date from unavailable periods."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "number": {
                    "type": "integer",
                    "description": "1-based position from the last gig listing.",
                }
            },
            "required": ["number"],
        },
    },
    {
        "name": "edit_gig",
        "description": (
            "Edit an upcoming gig by its 1-based position from the last list_upcoming_gigs call. "
            "Provide only the fields to change (summary, date, time). "
            "Requires a prior list_upcoming_gigs call."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "number": {
                    "type": "integer",
                    "description": "1-based position from the last gig listing.",
                },
                "summary": {
                    "type": "string",
                    "description": "New event title, e.g. 'Sunday Service — St Paul's'",
                },
                "date": {
                    "type": "string",
                    "description": "New date, e.g. 'Sunday 1st June 2026'",
                },
                "time": {
                    "type": "string",
                    "description": "New start time, e.g. '11:00am'",
                },
            },
            "required": ["number"],
        },
    },
    # ── Invoice — client management ─────────────────────────────────────────
    {
        "name": "list_clients",
        "description": "List all saved clients with their keys, names, emails, and addresses.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_client",
        "description": "Get full details for a single client by their key.",
        "input_schema": {
            "type": "object",
            "properties": {
                "client_key": {"type": "string", "description": "The client key, e.g. 'holy-cross'"}
            },
            "required": ["client_key"],
        },
    },
    {
        "name": "add_client",
        "description": "Add a new client to the client database.",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Unique client key, e.g. 'st-marys'"},
                "name": {"type": "string", "description": "Contact name, e.g. 'The Secretary'"},
                "address": {
                    "type": "string",
                    "description": "Full address as plain text (use newlines for line breaks, not HTML)",
                },
                "email": {"type": "string", "description": "Client email address"},
                "cc": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "CC email addresses",
                },
            },
            "required": ["key", "name", "address"],
        },
    },
    {
        "name": "edit_client",
        "description": "Update one or more fields of an existing client. Only provide the fields to change.",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "The client key to update"},
                "name": {"type": "string", "description": "New contact name"},
                "address": {
                    "type": "string",
                    "description": "New address as plain text (use newlines for line breaks, not HTML)",
                },
                "email": {"type": "string", "description": "New email address"},
                "cc": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "New CC list (replaces existing)",
                },
            },
            "required": ["key"],
        },
    },
    {
        "name": "delete_client",
        "description": "Permanently delete a client from the database. Cannot be undone.",
        "input_schema": {
            "type": "object",
            "properties": {"key": {"type": "string", "description": "The client key to delete"}},
            "required": ["key"],
        },
    },
    # ── Invoice — generation & email ────────────────────────────────────────
    {
        "name": "generate_invoice",
        "description": "Generate a PDF invoice for a client with one or more line items. Returns the PDF.",
        "input_schema": {
            "type": "object",
            "properties": {
                "client_key": {"type": "string"},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "description": {"type": "string"},
                            "quantity": {"type": "integer"},
                            "unit_price": {"type": "number"},
                        },
                        "required": ["description", "quantity", "unit_price"],
                    },
                },
            },
            "required": ["client_key", "items"],
        },
    },
    {
        "name": "duplicate_invoice",
        "description": "Create a new invoice identical to a previous one with today's date and a new number.",
        "input_schema": {
            "type": "object",
            "properties": {"invoice_number": {"type": "string"}},
            "required": ["invoice_number"],
        },
    },
    {
        "name": "send_invoice_email",
        "description": "Email the most recently generated invoice to the client.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "resend_invoice",
        "description": "Re-email a previously generated invoice by invoice number.",
        "input_schema": {
            "type": "object",
            "properties": {"invoice_number": {"type": "string"}},
            "required": ["invoice_number"],
        },
    },
    {
        "name": "list_invoices",
        "description": "List recent invoices, showing invoice number, client, amount, date, and whether they were emailed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "client_key": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": [],
        },
    },
    {
        "name": "mark_invoice_paid",
        "description": "Mark an invoice as paid. Use when the user says an invoice has been paid or confirms payment.",
        "input_schema": {
            "type": "object",
            "properties": {
                "invoice_number": {
                    "type": "string",
                    "description": "The invoice number, e.g. INV-2026-001",
                }
            },
            "required": ["invoice_number"],
        },
    },
    {
        "name": "unmark_invoice_paid",
        "description": "Clear the paid status on an invoice. Use when the user says an invoice was marked paid by mistake or the payment was reversed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "invoice_number": {
                    "type": "string",
                    "description": "The invoice number, e.g. INV-2026-001",
                }
            },
            "required": ["invoice_number"],
        },
    },
    {
        "name": "delete_invoice",
        "description": "Delete an invoice record and its PDF file. Destructive — always confirm with the user first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "invoice_number": {
                    "type": "string",
                    "description": "The invoice number, e.g. INV-2026-001",
                }
            },
            "required": ["invoice_number"],
        },
    },
    {
        "name": "get_invoice",
        "description": "Retrieve a specific invoice by number and send it as a PDF.",
        "input_schema": {
            "type": "object",
            "properties": {"invoice_number": {"type": "string"}},
            "required": ["invoice_number"],
        },
    },
    # ── Filter management ───────────────────────────────────────────────────
    {
        "name": "manage_blacklist",
        "description": "Manage the organist blacklist. action: list, add, or remove.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "add", "remove"]},
                "email": {"type": "string"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "manage_unavailable",
        "description": "Manage unavailable periods. action: list, add, or remove. period formats: YYYY-MM-DD, YYYY-MM-DD:YYYY-MM-DD, YYYY-MM. Also accepts: today, tomorrow, this/next <weekday>, this weekend, next week, this/next month.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "add", "remove"]},
                "period": {"type": "string"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "manage_available",
        "description": "Manage available-only periods. action: list, add, or remove.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "add", "remove"]},
                "period": {"type": "string"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "manage_filter_suspensions",
        "description": (
            "Suspend or resume gig filters for a date range, keyed by the GIG's own date "
            "(not today's date) — e.g. 'ignore the postcode filter for gigs in December'. "
            "action: list, add, or remove. filter: fee, sunday_time, blacklist, postcode, "
            "calendar, availability, or all. The 'seen' filter cannot be suspended — doing so "
            "would just re-send the same application every poll tick instead of exempting a "
            "category of gig. period formats: YYYY-MM-DD, YYYY-MM-DD:YYYY-MM-DD, YYYY-MM, "
            "YYYY-MM-DD: (from that date onward, open-ended), :YYYY-MM-DD (up to and including "
            "that date). Also accepts the same relative phrases as manage_unavailable: today, "
            "tomorrow, this/next <weekday>, this weekend, next week, this/next month."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "add", "remove"]},
                "filter": {
                    "type": "string",
                    "enum": [
                        "fee",
                        "sunday_time",
                        "blacklist",
                        "postcode",
                        "calendar",
                        "availability",
                        "all",
                    ],
                },
                "period": {"type": "string"},
            },
            "required": ["action"],
        },
    },
    # ── Meta ────────────────────────────────────────────────────────────────
    {
        "name": "clear_conversation",
        "description": "Clear this chat's conversation history and all cached state.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    # ── Runtime config ──────────────────────────────────────────────────────
    {
        "name": "manage_config",
        "description": (
            "Read or update runtime pipeline configuration. "
            "Editable keys: min_fee (int, ≥0), max_travel_minutes (int, 1–300), "
            "poll_minutes (int, 1–60), negotiable_fee (int, 0–100000). "
            "Changes take effect on the next polling tick. "
            "Use action='reset' to restore the .env default for a key."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["get", "set", "reset"],
                    "description": (
                        "get=show all values, set=update one value, "
                        "reset=restore .env default for one key"
                    ),
                },
                "key": {
                    "type": "string",
                    "enum": ["min_fee", "max_travel_minutes", "poll_minutes", "negotiable_fee"],
                    "description": "Required for set and reset actions.",
                },
                "value": {
                    "type": "integer",
                    "description": "New value. Required for set.",
                },
            },
            "required": ["action"],
        },
    },
    # ── LLM provider ─────────────────────────────────────────────────────────
    {
        "name": "manage_llm_provider",
        "description": (
            "Read or switch which LLM provider/model powers this conversation. "
            "Providers: anthropic (sonnet/opus/haiku), openai (gpt-6-astra/gpt-5.6-luna), "
            "gemini (gemini-pro/gemini-3.8-flash). "
            "Use action='get' to show the current provider/model. "
            "Use action='set' with 'provider' to switch — if 'model' is omitted, list "
            "that provider's options and ask the user to pick one before calling set "
            "again. A valid set does NOT switch immediately — it returns a Confirm/Cancel "
            "button; the switch only applies once the user taps Confirm. "
            "Use action='reset' to restore the default (anthropic/sonnet). "
            "Checks the provider's API key is configured before doing anything else — "
            "refuses immediately (even before listing model options) if it isn't. "
            "If the active provider errors mid-conversation, this bot automatically fails "
            "over to another configured provider on its own — no tool call needed for that."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["get", "set", "reset"]},
                "provider": {
                    "type": "string",
                    "enum": ["anthropic", "openai", "gemini"],
                    "description": "Required for set.",
                },
                "model": {
                    "type": "string",
                    "description": (
                        "One of that provider's curated model keys (e.g. 'sonnet', "
                        "'gpt-5.6-luna'). Optional for set — omit to see the options."
                    ),
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "get_llm_usage_summary",
        "description": (
            "Report LLM call counts and token usage per provider, for today and all-time. "
            "Reflects real usage including any automatic failover, so it shows which "
            "provider actually served requests, not just whichever was nominally active."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    # ── Application tracking ────────────────────────────────────────────────
    {
        "name": "manage_applications",
        "description": (
            "Query or update gig application tracking. "
            "'summary' returns status counts for the last N days. "
            "'list' returns a numbered listing (most recent first). "
            "'update' changes the status of an application by its number from the last list call. "
            "Valid statuses: applied, accepted, no_response, declined."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["summary", "list", "update", "detail"],
                    "description": "summary=status counts, list=numbered listing, update=change status, detail=full fields for one record",
                },
                "days": {
                    "type": "integer",
                    "description": "Lookback window in days for summary/list (default 30).",
                },
                "number": {
                    "type": "integer",
                    "description": "1-based position from the last list call. Required for update.",
                },
                "status": {
                    "type": "string",
                    "enum": ["applied", "accepted", "no_response", "declined"],
                    "description": "New status. Required for update.",
                },
            },
            "required": ["action"],
        },
    },
    # ── NEG-fee drafts ──────────────────────────────────────────────────────
    {
        "name": "list_neg_pending",
        "description": (
            "List all NEG-fee application drafts awaiting user review. "
            "Returns gig_id, gig summary, and a draft-body preview for each. "
            "Use when the user asks about pending NEG drafts or what is "
            "awaiting their approval."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "approve_neg_application",
        "description": (
            "Show a Confirm/Cancel prompt to send a NEG-fee application draft "
            "as-is to the gig contact. Omit gig_id if the user didn't specify "
            "one — it resolves automatically, or the user is shown a picker "
            "if more than one draft is pending. The actual send only happens "
            "when the user taps Confirm — never call this a second time to "
            "'confirm' it yourself."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "gig_id": {
                    "type": "string",
                    "description": "12-char id from the Telegram alert or list_neg_pending. Omit if not specified by the user.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "edit_neg_application",
        "description": (
            "Revise a NEG-fee application draft. Provide either new_body "
            "(replace the whole HTML body) OR new_fee (re-render the "
            "template with a different £ amount). The revision is saved "
            "immediately and shown back with Accept/Edit/Reject buttons — "
            "it is not sent until the user taps Accept then Confirm. Omit "
            "gig_id if the user didn't specify one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "gig_id": {"type": "string"},
                "new_body": {"type": "string", "description": "Replacement HTML body."},
                "new_fee": {
                    "type": "integer",
                    "description": "Re-render the negotiation template with this fee.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "reject_neg_application",
        "description": (
            "Show a Confirm/Cancel prompt to reject a NEG-fee application "
            "draft without sending any email. Omit gig_id if the user "
            "didn't specify one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "gig_id": {"type": "string"},
            },
            "required": [],
        },
    },
    # ── Income forecast ─────────────────────────────────────────────────────
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
                "from_date": {
                    "type": "string",
                    "description": "Start date ISO format YYYY-MM-DD (inclusive)",
                },
                "to_date": {
                    "type": "string",
                    "description": "End date ISO format YYYY-MM-DD (inclusive)",
                },
            },
            "required": ["from_date", "to_date"],
        },
    },
    # ── Application analytics ────────────────────────────────────────────────
    {
        "name": "get_application_analytics",
        "description": (
            "Return application success metrics: total applications, acceptance rate, "
            "response rate, and average response time. "
            "Optional days parameter (default 365)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Lookback window in days. Default 365.",
                }
            },
        },
    },
    {
        "name": "get_gig_breakdown",
        "description": (
            "Return breakdown of applications and acceptance rates by gig type "
            "(wedding, funeral, service, etc). "
            "acceptance_rate is accepted / total-including-pending for each type. "
            "Optional days parameter (default 365)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Lookback window in days. Default 365.",
                }
            },
        },
    },
]


def _to_function_tool(tool: dict) -> dict:
    """Wrap one Anthropic-shaped tool schema into OpenAI's function-calling shape —
    LiteLLM's canonical `tools=` input format regardless of which backend provider
    actually handles the request."""
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["input_schema"],
        },
    }


TOOLS: list[dict] = [_to_function_tool(t) for t in _TOOLS_SCHEMA]


def _to_responses_tool(tool: dict) -> dict:
    """Flat function-tool shape required by the Responses API (no nested
    "function" key, unlike Chat Completions' TOOLS)."""
    return {
        "type": "function",
        "name": tool["name"],
        "description": tool["description"],
        "parameters": tool["input_schema"],
    }


RESPONSES_TOOLS: list[dict] = [_to_responses_tool(t) for t in _TOOLS_SCHEMA]


@dataclass
class AgentResponse:
    text: str | None = None
    file_path: str | None = None
    file_caption: str | None = None
    buttons: list[list[dict]] | None = None


# Per-chat state
_histories: dict[int, list[dict]] = {}
# Caps a chat's in-memory history so a long-lived conversation can't grow
# without bound and eventually exceed the model's context window.
_MAX_HISTORY_MESSAGES = 60
_last_invoice: dict[int, dict] = {}
_last_gig_listing: dict[int, list[dict]] = {}
_last_application_listing: dict[int, list[dict]] = {}
_active_neg_draft: dict[int, str] = {}
_pending_neg_instruction: dict[int, str] = {}
# A provider/model switch awaiting Telegram confirm/cancel -- see
# manage_llm_provider's "set" action and llm_confirm_switch/llm_cancel_switch
# below. Not persisted across a restart (same treatment as
# _pending_neg_instruction): losing an unconfirmed switch on restart is an
# acceptable edge case for a short-lived confirmation prompt.
_pending_llm_switch: dict[int, tuple[str, str]] = {}

# Chats whose persisted reference-context has been loaded this process.
_hydrated: set[int] = set()


def _trim_history(chat_id: int) -> None:
    """Drop the oldest turns once a chat's history exceeds _MAX_HISTORY_MESSAGES.

    Only cuts at a real user-text turn (role "user" with plain string content),
    never inside a tool_use/tool_result pair — cutting mid-pair would leave a
    dangling tool_use with no matching tool_result, which the API rejects.
    """
    history = _histories.get(chat_id)
    if history is None or len(history) <= _MAX_HISTORY_MESSAGES:
        return
    cut = len(history) - _MAX_HISTORY_MESSAGES
    while cut < len(history) and not (
        history[cut]["role"] == "user" and isinstance(history[cut]["content"], str)
    ):
        cut += 1
    _histories[chat_id] = history[cut:]


def _hydrate_chat(chat_id: int) -> None:
    """Load persisted last_* context for chat_id into the in-memory dicts, once
    per process, so a bot restart doesn't drop the user's reference context
    (e.g. "email that invoice" after a restart)."""
    if chat_id in _hydrated:
        return
    _hydrated.add(chat_id)
    try:
        persisted = agent_state.load_chat(chat_id)
    except Exception:
        logger.warning("agent_state: failed to load chat %s context", chat_id, exc_info=True)
        return
    if chat_id not in _last_invoice and persisted.get("last_invoice") is not None:
        _last_invoice[chat_id] = persisted["last_invoice"]
    if chat_id not in _last_gig_listing and persisted.get("last_gig_listing") is not None:
        _last_gig_listing[chat_id] = persisted["last_gig_listing"]
    if (
        chat_id not in _last_application_listing
        and persisted.get("last_application_listing") is not None
    ):
        _last_application_listing[chat_id] = persisted["last_application_listing"]
    if chat_id not in _active_neg_draft and persisted.get("active_neg_draft") is not None:
        _active_neg_draft[chat_id] = persisted["active_neg_draft"]


def _persist_chat(chat_id: int) -> None:
    """Write the chat's current last_* reference-context to disk (best-effort —
    a persistence failure must never break the user's reply)."""
    try:
        agent_state.save_chat(
            chat_id,
            {
                "last_invoice": _last_invoice.get(chat_id),
                "last_gig_listing": _last_gig_listing.get(chat_id),
                "last_application_listing": _last_application_listing.get(chat_id),
                "active_neg_draft": _active_neg_draft.get(chat_id),
            },
        )
    except Exception:
        logger.warning("agent_state: failed to persist chat %s context", chat_id, exc_info=True)


_TOOL_HANDLERS: dict[str, Callable[[dict, int], Awaitable[str]]] = {}


def _handler(name: str) -> Callable[[Callable], Callable]:
    """Register an async tool handler under `name` in _TOOL_HANDLERS."""

    def deco(fn: Callable) -> Callable:
        _TOOL_HANDLERS[name] = fn
        return fn

    return deco


_PDF_RESPONSE_TOOLS = {"generate_invoice", "duplicate_invoice", "get_invoice"}
_VERBATIM_RESPONSE_TOOLS = {
    "list_upcoming_gigs",
    "manage_config",
    "manage_llm_provider",
    "get_llm_usage_summary",
    "manage_applications",
    "get_income_forecast",
    "get_application_analytics",
    "get_gig_breakdown",
    "list_neg_pending",
    "approve_neg_application",
    "edit_neg_application",
    "reject_neg_application",
}


def sync_calendar_blocks(cal: GoogleCalendarClient) -> None:
    """Create calendar blocks for all current unavailable periods not already blocked.

    Idempotent — safe to call at every startup.
    """
    periods = filter_store.unavailable_periods()
    for period in periods:
        try:
            cal.block_period(period)
        except Exception:
            logger.warning("sync_calendar_blocks: failed for %r", period, exc_info=True)
    logger.info("sync_calendar_blocks: synced %d period(s)", len(periods))


def _resolve_period(text: str) -> str:
    """Resolve relative date expressions to period token format.

    Handles: today, tomorrow, this/next month, next week, this weekend,
    this/next <weekday>. Unrecognised text is returned unchanged.
    """
    import datetime as _dt

    t = text.strip().lower()
    today = _dt.date.today()

    if t == "today":
        return today.isoformat()

    if t == "tomorrow":
        return (today + _dt.timedelta(days=1)).isoformat()

    if t in ("this month", "this-month"):
        return today.strftime("%Y-%m")

    if t in ("next month", "next-month"):
        if today.month == 12:
            return f"{today.year + 1}-01"
        return f"{today.year}-{today.month + 1:02d}"

    if t in ("next week", "next-week"):
        days_until_monday = (7 - today.weekday()) % 7
        if days_until_monday == 0:
            days_until_monday = 7
        next_mon = today + _dt.timedelta(days=days_until_monday)
        next_sun = next_mon + _dt.timedelta(days=6)
        return f"{next_mon.isoformat()}:{next_sun.isoformat()}"

    if t in ("this weekend", "this-weekend", "next weekend", "next-weekend"):
        if today.weekday() == 6:  # Sunday — today is already the weekend
            return today.isoformat()
        if today.weekday() == 5:  # Saturday — today and tomorrow
            return f"{today.isoformat()}:{(today + _dt.timedelta(days=1)).isoformat()}"
        days_until_sat = (5 - today.weekday()) % 7
        if days_until_sat == 0:
            days_until_sat = 7
        sat = today + _dt.timedelta(days=days_until_sat)
        sun = sat + _dt.timedelta(days=1)
        return f"{sat.isoformat()}:{sun.isoformat()}"

    _WEEKDAYS = {
        "monday": 0,
        "tuesday": 1,
        "wednesday": 2,
        "thursday": 3,
        "friday": 4,
        "saturday": 5,
        "sunday": 6,
    }
    for prefix in ("this ", "next "):
        if t.startswith(prefix):
            day_name = t[len(prefix) :]
            if day_name in _WEEKDAYS:
                target = _WEEKDAYS[day_name]
                days_ahead = (target - today.weekday()) % 7
                if days_ahead == 0:
                    days_ahead = 7
                return (today + _dt.timedelta(days=days_ahead)).isoformat()

    return text


def _fmt_application_date(date_str: str) -> str:
    """Format a gig date string as 'D Mon' (e.g. '15 Jun') for application listings."""
    yyyymmdd = normalize_to_yyyymmdd(date_str)
    if yyyymmdd:
        try:
            dt = datetime.datetime.strptime(yyyymmdd, "%Y%m%d")
            return f"{dt.day} {dt.strftime('%b')}"
        except ValueError:
            pass
    return date_str


async def _execute_tool(name: str, input_data: dict, chat_id: int) -> str:
    handler = _TOOL_HANDLERS.get(name)
    if handler is not None:
        return await handler(input_data, chat_id)

    return json.dumps({"error": f"Tool not implemented: {name}"})


# ── Gig tools ──────────────────────────────────────────────────────────────


@_handler("fetch_gig_details")
async def _handle_fetch_gig_details(input_data: dict, chat_id: int) -> str:
    try:
        scraper = Scraper()
        html = scraper.fetch(input_data["url"])
        basic = scraper.extract_basic_from_detail(html, input_data["url"])
        full = scraper.extract_full_details(html)
        details = {**basic, **full}
        scraper.session.close()
        return json.dumps({k: v for k, v in details.items() if v is not None})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@_handler("add_gig")
async def _handle_add_gig(input_data: dict, chat_id: int) -> str:
    confirmed = input_data.get("confirmed", False)
    fields = {
        "header": input_data.get("header", ""),
        "organisation": input_data.get("organisation") or "",
        "locality": input_data.get("locality") or "",
        "date": input_data.get("date", ""),
        "time": input_data.get("time", ""),
        "fee": input_data.get("fee") or "not specified",
    }
    if not confirmed:
        return (
            "*Please confirm the following gig:*\n"
            f"• *Title:* {fields['header']}\n"
            f"• *Organisation:* {fields['organisation']}\n"
            f"• *Locality:* {fields['locality']}\n"
            f"• *Date:* {fields['date']}\n"
            f"• *Time:* {fields['time']}\n"
            f"• *Fee:* {fields['fee']}\n\n"
            "Reply *yes* to add to calendar, or tell me what to change."
        )
    try:
        cal = _make_calendar_client()
        if cal is None:
            return json.dumps({"error": "Google Calendar not configured."})
        gig = Gig(
            header=fields["header"],
            organisation=fields["organisation"],
            locality=fields["locality"],
            date=fields["date"],
            time=fields["time"],
            fee=fields["fee"] if fields["fee"] != "not specified" else None,
            link="",
        )
        event_id = cal.add_gig(gig)
        url = input_data.get("url") or None
        # Travel buffers (non-fatal — gig event already created)
        try:
            postcode = input_data.get("postcode", "")
            yyyymmdd_buf = normalize_to_yyyymmdd(fields["date"])
            start_time_buf = parse_start_time(fields["time"])
            if yyyymmdd_buf and start_time_buf:
                buf_date = datetime.datetime.strptime(yyyymmdd_buf, "%Y%m%d").date()
                buf_start = datetime.datetime.combine(buf_date, start_time_buf)
                buf_end = buf_start + datetime.timedelta(hours=1)
                travel_mins = travel.get_travel_minutes(postcode) or settings.max_travel_minutes
                before_id, after_id = cal.add_travel_buffers(
                    gig_summary=f"{fields['header']} — {fields['organisation']}",
                    start_dt=buf_start,
                    end_dt=buf_end,
                    travel_minutes=travel_mins,
                )
                if url:
                    application_store.update_travel_buffer_ids(url, before_id, after_id)
        except Exception as buf_exc:
            logger.warning("add_gig: travel buffer creation failed: %s", buf_exc)
        yyyymmdd = normalize_to_yyyymmdd(fields["date"])
        if yyyymmdd:
            try:
                date_str = datetime.datetime.strptime(yyyymmdd, "%Y%m%d").strftime("%Y-%m-%d")
                filter_store.add_period("unavailable_periods", date_str)
            except Exception:
                logger.warning(
                    "Failed to add gig date to unavailable periods",
                    extra={"date": fields["date"]},
                )
        try:
            application_store.upsert_accepted(
                url=url,
                header=fields["header"],
                organisation=fields.get("organisation", ""),
                date=fields["date"],
                fee=fields["fee"] if fields["fee"] != "not specified" else "",
                postcode=input_data.get("postcode", ""),
                time=fields.get("time", ""),
            )
        except Exception:
            logger.warning(
                "add_gig: upsert_accepted failed",
                extra={"url": url},
                exc_info=True,
            )
        return json.dumps({"result": f"Added to calendar. Event ID: {event_id}"})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@_handler("list_upcoming_gigs")
async def _handle_list_upcoming_gigs(input_data: dict, chat_id: int) -> str:
    cal = _make_calendar_client()
    if cal is None:
        return json.dumps({"error": "Google Calendar not configured."})
    max_results = input_data.get("max_results", 10)
    # Each booked gig typically generates ~3 non-gig events (1 Unavailable block
    # + 2 travel buffers), so over-fetch to keep the post-filter list at the
    # requested size.
    events = cal.list_upcoming_events(max_results=max_results * 4)
    events = [
        e
        for e in events
        if e.get("summary", "").strip() != "Unavailable"
        and not e.get("summary", "").startswith("🚗 Travel ")
    ]
    events = sorted(events, key=lambda e: e["start_dt"])[:max_results]
    _last_gig_listing[chat_id] = events
    if not events:
        return json.dumps({"result": "No upcoming gigs found."})
    lines = [f"🎵 *Upcoming Gigs* ({len(events)})"]
    for i, ev in enumerate(events, start=1):
        start_dt = ev["start_dt"]
        time_str = start_dt.strftime("%I:%M%p").lstrip("0").lower()
        date_str = start_dt.strftime("%a %d %b %Y").replace(" 0", " ")
        lines.append(f"{i}. *{ev['summary']}*\n   {date_str} · {time_str}")
    return json.dumps({"result": "\n\n".join(lines)})


@_handler("delete_gig")
async def _handle_delete_gig(input_data: dict, chat_id: int) -> str:
    n = input_data["number"]
    listing = _last_gig_listing.get(chat_id)
    if not listing:
        return json.dumps({"error": "No gig listing cached. Ask me to list your gigs first."})
    if n < 1 or n > len(listing):
        return json.dumps(
            {"error": f"No gig number {n}. There are {len(listing)} gigs in the last listing."}
        )
    cal = _make_calendar_client()
    if cal is None:
        return json.dumps({"error": "Google Calendar not configured."})
    event = listing[n - 1]
    try:
        cal.delete_event(event["id"])
    except Exception as exc:
        return json.dumps({"error": str(exc)})
    filter_store.remove_period("unavailable_periods", event["date_str"])
    _last_gig_listing[chat_id] = [e for i, e in enumerate(listing) if i != n - 1]
    return json.dumps(
        {"result": f"Deleted {event['summary']}. Date removed from unavailable if present."}
    )


@_handler("edit_gig")
async def _handle_edit_gig(input_data: dict, chat_id: int) -> str:
    n = input_data["number"]
    listing = _last_gig_listing.get(chat_id)
    if not listing:
        return json.dumps({"error": "No gig listing cached. Ask me to list your gigs first."})
    if n < 1 or n > len(listing):
        return json.dumps({"error": f"No gig number {n}. There are {len(listing)} gigs listed."})
    event = listing[n - 1]
    cal = _make_calendar_client()
    if cal is None:
        return json.dumps({"error": "Google Calendar not configured."})

    new_summary = input_data.get("summary")
    new_date_str = input_data.get("date")
    new_time_str = input_data.get("time")

    new_start_dt = None
    old_date_str = event["date_str"]

    if new_date_str or new_time_str:
        if new_date_str:
            normalized = normalize_to_yyyymmdd(new_date_str)
            if not normalized:
                return json.dumps({"error": f"Cannot parse date: {new_date_str!r}"})
            base_date = datetime.datetime.strptime(normalized, "%Y%m%d").date()
        else:
            base_date = event["start_dt"].date()

        if new_time_str:
            parsed_time = parse_start_time(new_time_str)
            if not parsed_time:
                return json.dumps({"error": f"Cannot parse time: {new_time_str!r}"})
        else:
            parsed_time = event["start_dt"].time()

        assert parsed_time is not None  # guarded by early return above
        new_start_dt = datetime.datetime.combine(base_date, parsed_time, tzinfo=datetime.UTC)

    try:
        cal.update_event(event["id"], summary=new_summary, start_dt=new_start_dt)
    except Exception as exc:
        return json.dumps({"error": str(exc)})

    if new_start_dt:
        new_date_iso = new_start_dt.date().isoformat()
        if new_date_iso != old_date_str:
            filter_store.remove_period("unavailable_periods", old_date_str)
            filter_store.add_period("unavailable_periods", new_date_iso)
        updated = {**event, "start_dt": new_start_dt, "date_str": new_date_iso}
    else:
        updated = {**event}
    if new_summary:
        updated["summary"] = new_summary
    listing[n - 1] = updated

    return json.dumps({"result": "✓ Gig updated."})


# ── Client tools ────────────────────────────────────────────────────────────


@_handler("list_clients")
async def _handle_list_clients(input_data: dict, chat_id: int) -> str:
    clients = load_clients()
    if not clients:
        return json.dumps({"result": "No clients found. Add one with a natural language request."})
    return json.dumps(clients, indent=2)


@_handler("get_client")
async def _handle_get_client(input_data: dict, chat_id: int) -> str:
    clients = load_clients()
    key = input_data["client_key"]
    if key not in clients:
        return json.dumps(
            {"error": f"Client '{key}' not found. Available: {', '.join(clients.keys())}"}
        )
    return json.dumps({key: clients[key]}, indent=2)


@_handler("add_client")
async def _handle_add_client(input_data: dict, chat_id: int) -> str:
    add_client(
        key=input_data["key"],
        name=input_data["name"],
        address=input_data["address"],
        email=input_data.get("email", ""),
        cc=input_data.get("cc", []),
    )
    return json.dumps({"result": f"Client '{input_data['key']}' added successfully."})


@_handler("edit_client")
async def _handle_edit_client(input_data: dict, chat_id: int) -> str:
    try:
        edit_client(
            key=input_data["key"],
            name=input_data.get("name"),
            address=input_data.get("address"),
            email=input_data.get("email"),
            cc=input_data.get("cc"),
        )
        return json.dumps({"result": f"Client '{input_data['key']}' updated successfully."})
    except ValueError as e:
        return json.dumps({"error": str(e)})


@_handler("delete_client")
async def _handle_delete_client(input_data: dict, chat_id: int) -> str:
    try:
        delete_client(input_data["key"])
        return json.dumps({"result": f"Client '{input_data['key']}' deleted."})
    except ValueError as e:
        return json.dumps({"error": str(e)})


# ── Invoice tools ────────────────────────────────────────────────────────────


@_handler("generate_invoice")
async def _handle_generate_invoice(input_data: dict, chat_id: int) -> str:
    try:
        result = await generate_invoice(
            client_key=input_data["client_key"],
            items=input_data["items"],
        )
    except (ValueError, KeyError) as e:
        return json.dumps({"error": str(e)})
    _last_invoice[chat_id] = result
    return json.dumps(
        {
            "result": "Invoice generated successfully.",
            "pdf_path": str(result["pdf_path"]),
            "client_name": result["client_name"],
            "client_email": result["client_email"],
            "invoice_number": result["invoice_number"],
            "total": result["total"],
            "currency": result["currency"],
        }
    )


@_handler("duplicate_invoice")
async def _handle_duplicate_invoice(input_data: dict, chat_id: int) -> str:
    invoices = load_invoices()
    inv_num = input_data["invoice_number"]
    if inv_num not in invoices:
        return json.dumps({"error": f"Invoice '{inv_num}' not found in history."})
    original = invoices[inv_num]
    result = await generate_invoice(
        client_key=original["client_key"],
        items=original["items"],
    )
    _last_invoice[chat_id] = result
    return json.dumps(
        {
            "result": f"Duplicate invoice created (original: {inv_num}).",
            "pdf_path": str(result["pdf_path"]),
            "invoice_number": result["invoice_number"],
            "client_name": result["client_name"],
            "total": result["total"],
            "currency": result["currency"],
        }
    )


@_handler("send_invoice_email")
async def _handle_send_invoice_email(input_data: dict, chat_id: int) -> str:
    inv = _last_invoice.get(chat_id)
    if not inv:
        return json.dumps({"error": "No invoice has been generated yet in this session."})
    email_result = send_invoice_email(inv)
    if email_result["success"]:
        mark_invoice_emailed(inv["invoice_number"])
        cc_list = inv.get("client_cc", [])
        cc_msg = f" (CC: {', '.join(cc_list)})" if cc_list else ""
        return json.dumps({"result": f"Invoice emailed to {inv['client_email']}{cc_msg}."})
    return json.dumps({"error": email_result["error"]})


@_handler("resend_invoice")
async def _handle_resend_invoice(input_data: dict, chat_id: int) -> str:
    invoices = load_invoices()
    inv_num = input_data["invoice_number"]
    if inv_num not in invoices:
        return json.dumps({"error": f"Invoice '{inv_num}' not found in history."})
    inv = cast(dict, invoices[inv_num])
    email_result = send_invoice_email(inv)
    if email_result["success"]:
        mark_invoice_emailed(inv_num)
        return json.dumps({"result": f"Invoice {inv_num} re-sent to {inv['client_email']}."})
    return json.dumps({"error": email_result["error"]})


@_handler("mark_invoice_paid")
async def _handle_mark_invoice_paid(input_data: dict, chat_id: int) -> str:
    inv_num = input_data["invoice_number"]
    ok = mark_invoice_paid(inv_num)
    if not ok:
        return json.dumps({"error": f"Invoice {inv_num} not found."})
    return json.dumps({"result": f"✅ Invoice {inv_num} marked as paid."})


@_handler("unmark_invoice_paid")
async def _handle_unmark_invoice_paid(input_data: dict, chat_id: int) -> str:
    inv_num = input_data["invoice_number"]
    ok = unmark_invoice_paid(inv_num)
    if not ok:
        return json.dumps({"error": f"Invoice {inv_num} not found."})
    return json.dumps({"result": f"Invoice {inv_num} is no longer marked as paid."})


@_handler("delete_invoice")
async def _handle_delete_invoice(input_data: dict, chat_id: int) -> str:
    inv_num = input_data["invoice_number"]
    ok = delete_invoice(inv_num)
    if not ok:
        return json.dumps({"error": f"Invoice {inv_num} not found."})
    cached = _last_invoice.get(chat_id)
    if cached and cached.get("invoice_number") == inv_num:
        _last_invoice.pop(chat_id, None)
    return json.dumps({"result": f"🗑️ Invoice {inv_num} deleted."})


@_handler("list_invoices")
async def _handle_list_invoices(input_data: dict, chat_id: int) -> str:
    invoices = load_invoices()
    if not invoices:
        return "No invoices found."

    import datetime as _dt

    now = _dt.datetime.now(_dt.UTC)

    def _payment_status(r: dict) -> str:
        if r.get("paid_at"):
            return "✅ paid"
        if not r.get("emailed"):
            return "📝 not sent"
        # Older records have `emailed: true` but no `emailed_at`; fall back to
        # `created_at` so the days-since calculation still works.
        sent_at_str = r.get("emailed_at") or r.get("created_at")
        if not sent_at_str:
            return "📤 emailed"
        try:
            sent_at = _dt.datetime.fromisoformat(sent_at_str.replace("Z", "+00:00"))
            if sent_at.tzinfo is None:
                sent_at = sent_at.replace(tzinfo=_dt.UTC)
            days = (now - sent_at).days
            if days >= 5:
                return f"⏰ overdue ({days}d)"
            return f"📤 emailed {days}d ago"
        except ValueError:
            return "📤 emailed"

    records = list(invoices.values())
    records.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    records = records[:20]

    lines = [f"📄 *Invoices* ({len(records)}, most recent first)"]
    for i, r in enumerate(records, start=1):
        lines.append(
            f"{i}. {_payment_status(r)} — *{r['invoice_number']}*\n"
            f"   {r.get('client_name', '?')} · £{r.get('total', 0):.2f} · {r.get('date', '?')}"
        )
    return "\n\n".join(lines)


@_handler("get_invoice")
async def _handle_get_invoice(input_data: dict, chat_id: int) -> str:
    invoices = load_invoices()
    inv_num = input_data["invoice_number"]
    if inv_num not in invoices:
        return json.dumps({"error": f"Invoice '{inv_num}' not found in history."})
    inv = cast(dict, invoices[inv_num])
    _last_invoice[chat_id] = inv
    return json.dumps(
        {
            "result": "Invoice found.",
            "invoice_number": inv["invoice_number"],
            "client_name": inv["client_name"],
            "date": inv["date"],
            "total": inv["total"],
            "currency": inv["currency"],
            "emailed": inv.get("emailed", False),
            "pdf_path": inv["pdf_path"],
        }
    )


# ── Filter tools ────────────────────────────────────────────────────────────


@_handler("manage_blacklist")
async def _handle_manage_blacklist(input_data: dict, chat_id: int) -> str:
    action = input_data["action"]
    if action == "list":
        emails = filter_store.blacklist_emails()
        return (
            json.dumps({"blacklist": emails})
            if emails
            else json.dumps({"result": "Blacklist is empty."})
        )
    email = input_data.get("email", "")
    if action == "add":
        added = filter_store.add_blacklist_email(email)
        msg = (
            f"Added '{email}' to blacklist." if added else f"'{email}' is already in the blacklist."
        )
        return json.dumps({"result": msg})
    if action == "remove":
        removed = filter_store.remove_blacklist_email(email)
        msg = (
            f"Removed '{email}' from blacklist."
            if removed
            else f"'{email}' not found in blacklist."
        )
        return json.dumps({"result": msg})
    return json.dumps({"error": f"Unknown action: {action}"})


def _format_period(token: str) -> str:
    """Format a YYYY-MM-DD, YYYY-MM, or YYYY-MM-DD:YYYY-MM-DD token into a readable label."""
    fmt = "%d %b %Y"

    def _parse(s: str) -> datetime.date:
        if len(s) == 7:  # YYYY-MM
            return datetime.date.fromisoformat(s + "-01")
        return datetime.date.fromisoformat(s)

    try:
        if ":" in token:
            start_s, end_s = token.split(":", 1)
            start = _parse(start_s)
            end = _parse(end_s)
            if start.year == end.year:
                return f"{start.strftime('%d %b')} – {end.strftime('%d %b %Y')}"
            return f"{start.strftime(fmt)} – {end.strftime(fmt)}"
        return _parse(token).strftime(fmt)
    except ValueError:
        return token


def _format_periods_list(periods: list[str], label: str) -> str:
    if not periods:
        return f"No {label} set."
    lines = "\n".join(f"  • {_format_period(p)}" for p in periods)
    return f"{label.capitalize()} ({len(periods)}):\n{lines}"


def _format_suspension(entry: dict) -> str:
    """Format a suspension entry (filter + period) into a readable label,
    including open-ended bounds."""
    period = entry.get("period", "")
    filter_name = entry.get("filter", "")
    if period.startswith(":"):
        label = f"through {_format_period(period[1:])}"
    elif period.endswith(":") and period.count(":") == 1:
        label = f"from {_format_period(period[:-1])} onward"
    else:
        label = _format_period(period)
    return f"{filter_name}: {label}"


def _format_suspensions_list(suspensions: list[dict]) -> str:
    if not suspensions:
        return "No filter suspensions set."
    lines = "\n".join(f"  • {_format_suspension(e)}" for e in suspensions)
    return f"Filter suspensions ({len(suspensions)}):\n{lines}"


@_handler("manage_filter_suspensions")
async def _handle_manage_filter_suspensions(input_data: dict, chat_id: int) -> str:
    action = input_data["action"]
    if action == "list":
        suspensions = filter_suspension_store.list_suspensions()
        return _format_suspensions_list(suspensions)

    filter_name = input_data.get("filter", "")
    period = _resolve_period(input_data.get("period", ""))

    if action == "add":
        try:
            added = filter_suspension_store.add_suspension(filter_name, period)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        msg = (
            f"Suspended '{filter_name}' for '{period}'."
            if added
            else f"'{filter_name}' is already suspended for '{period}'."
        )
        return json.dumps({"result": msg})

    if action == "remove":
        removed = filter_suspension_store.remove_suspension(filter_name, period)
        msg = (
            f"Resumed '{filter_name}' for '{period}'."
            if removed
            else f"No matching suspension for '{filter_name}' / '{period}'."
        )
        return json.dumps({"result": msg})

    return json.dumps({"error": f"Unknown action: {action}"})


@_handler("manage_unavailable")
async def _handle_manage_unavailable(input_data: dict, chat_id: int) -> str:
    action = input_data["action"]
    if action == "list":
        periods = filter_store.unavailable_periods()
        return _format_periods_list(periods, "unavailable periods")
    period = _resolve_period(input_data.get("period", ""))
    if action == "add":
        added = filter_store.add_period("unavailable_periods", period)
        msg = (
            f"Marked '{period}' as unavailable."
            if added
            else f"'{period}' already in unavailable list."
        )
        cal = _make_calendar_client()
        if cal:
            try:
                cal.block_period(period)
            except Exception:
                logger.warning(
                    "manage_unavailable: failed to block calendar for %r", period, exc_info=True
                )
        return json.dumps({"result": msg})
    if action == "remove":
        removed = filter_store.remove_period("unavailable_periods", period)
        msg = (
            f"Removed '{period}' from unavailable periods." if removed else f"'{period}' not found."
        )
        cal = _make_calendar_client()
        if cal:
            try:
                cal.unblock_period(period)
            except Exception:
                logger.warning(
                    "manage_unavailable: failed to unblock calendar for %r",
                    period,
                    exc_info=True,
                )
        return json.dumps({"result": msg})
    return json.dumps({"error": f"Unknown action: {action}"})


@_handler("manage_available")
async def _handle_manage_available(input_data: dict, chat_id: int) -> str:
    action = input_data["action"]
    if action == "list":
        periods = filter_store.available_only_periods()
        return _format_periods_list(periods, "available-only periods")
    period = input_data.get("period", "")
    if action == "add":
        added = filter_store.add_period("available_only_periods", period)
        msg = (
            f"Added '{period}' to available-only periods."
            if added
            else f"'{period}' already present."
        )
        return json.dumps({"result": msg})
    if action == "remove":
        removed = filter_store.remove_period("available_only_periods", period)
        msg = (
            f"Removed '{period}' from available-only periods."
            if removed
            else f"'{period}' not found."
        )
        return json.dumps({"result": msg})
    return json.dumps({"error": f"Unknown action: {action}"})


# ── Analytics tools ──────────────────────────────────────────────────────────


@_handler("get_income_forecast")
async def _handle_get_income_forecast(input_data: dict, chat_id: int) -> str:
    from_date = input_data.get("from_date", "")
    to_date = input_data.get("to_date", "")
    try:
        income_summary: dict = application_store.get_income(from_date, to_date)
    except Exception as exc:
        return json.dumps({"error": f"Failed to retrieve income: {exc}"})

    try:
        from_dt = datetime.date.fromisoformat(from_date)
        to_dt = datetime.date.fromisoformat(to_date)
        header = f"💰 Income — {from_dt.day} {from_dt.strftime('%b')} to {to_dt.day} {to_dt.strftime('%b %Y')}"
    except ValueError:
        header = f"💰 Income — {from_date} to {to_date}"

    if income_summary["count"] == 0:
        return json.dumps({"result": f"{header}\n\nNo accepted gigs in this period."})

    lines = [
        header,
        "",
        f"Confirmed gigs:   {income_summary['count']}",
        f"Total income:     £{income_summary['total']:.2f}",
    ]
    if income_summary["no_fee_count"] > 0:
        lines.append(
            f"No fee recorded:  {income_summary['no_fee_count']} gig(s) (not included in total)"
        )

    lines.append("")
    for i, r in enumerate(income_summary["records"], start=1):
        org = r.get("organisation") or r.get("header") or "Unknown"
        try:
            d = datetime.date.fromisoformat(r.get("date", ""))
            date_str = f"{d.day} {d.strftime('%b')}"
        except ValueError:
            date_str = r.get("date", "")
        fee_str = r.get("fee", "").strip()
        fee_display = fee_str if fee_str else "(no fee)"
        lines.append(f"{i}. {org} — {date_str}  {fee_display}")

    return json.dumps({"result": "\n".join(lines)})


@_handler("get_application_analytics")
async def _handle_get_application_analytics(input_data: dict, chat_id: int) -> str:
    days = min(max(int(input_data.get("days", 365)), 1), 730)
    m = analytics.get_success_metrics(days)
    lines = [
        f"📊 Application Analytics (last {days} days)",
        "",
        f"Total applications: {m['total']}",
        f"✅ Accepted:      {m['accepted']:>4}",
        f"❌ Rejected/declined: {m['rejected']:>4}",
        f"💤 No response:   {m['no_response']:>4}",
        f"⏳ Still pending: {m['applied']:>4}",
        "",
        f"Acceptance rate: {m['acceptance_rate']}% (of resolved)",
        f"Response rate:   {m['response_rate']}% (of resolved)",
    ]
    if m["avg_response_days"] is not None:
        lines.append(f"Avg response time: {m['avg_response_days']} days")
    else:
        lines.append("Avg response time: not enough data")
    return json.dumps({"result": "\n".join(lines)})


@_handler("get_gig_breakdown")
async def _handle_get_gig_breakdown(input_data: dict, chat_id: int) -> str:
    days = min(max(int(input_data.get("days", 365)), 1), 730)
    breakdown = analytics.get_gig_type_breakdown(days)
    if not breakdown:
        return json.dumps({"result": f"No applications in the last {days} days."})
    sorted_types = sorted(breakdown.items(), key=lambda kv: kv[1]["count"], reverse=True)
    max_label = max(len(t) for t, _ in sorted_types)
    lines = [f"🎹 Gig Type Breakdown (last {days} days)", ""]
    for gig_type, data in sorted_types:
        label = f"{gig_type}:"
        lines.append(
            f"{label:<{max_label + 1}}  {data['count']:>3} applied"
            f" | {data['accepted']:>2} accepted ({data['acceptance_rate']:.0f}%)"
        )
    return json.dumps({"result": "\n".join(lines)})


@_handler("manage_applications")
async def _handle_manage_applications(input_data: dict, chat_id: int) -> str:
    action = input_data.get("action", "summary")
    days = input_data.get("days", 30)
    records = application_store.list_applications(days)
    if action in ("summary", "list"):
        _last_application_listing[chat_id] = records

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
        today = datetime.date.today()
        from_date = (today - datetime.timedelta(days=days)).isoformat()
        to_date = today.isoformat()
        income = application_store.get_income(from_date, to_date)
        income_line = f"Income (accepted):  £{income['total']:.2f}"
        if income["no_fee_count"] > 0:
            if income["no_fee_count"] == income["count"] and income["count"] > 0:
                income_line += f"  · all {income['count']} gig(s) have no fee recorded"
            else:
                n = income["no_fee_count"]
                income_line += f"  · {n} gig{'s' if n != 1 else ''} have no fee recorded"
        lines.append("")
        lines.append(income_line)
        return json.dumps({"result": "\n".join(lines)})

    if action == "list":
        if not records:
            return json.dumps({"result": f"No applications in the last {days} days."})
        _last_application_listing[chat_id] = records
        _status_emoji = {
            "accepted": "✅",
            "applied": "⏳",
            "no_response": "🔕",
            "declined": "❌",
            "rejected": "🚫",
        }
        lines = [f"📋 Applications — last {days} days", ""]
        for i, r in enumerate(records, start=1):
            emoji = _status_emoji.get(r["status"], "❓")
            org_part = f" — {r['organisation']}" if r.get("organisation") else ""
            date_part = _fmt_application_date(r.get("date", ""))
            fee_part = f"  {r['fee']}" if r.get("fee") else ""
            lines.append(f"{i}. {emoji} {r['header']}{org_part}  ({date_part}){fee_part}")
        return json.dumps({"result": "\n".join(lines)}, ensure_ascii=False)

    if action == "update":
        n = input_data.get("number")
        status: str = input_data.get("status") or ""
        listing = _last_application_listing.get(chat_id)
        if not listing:
            return json.dumps(
                {"error": "No application listing cached. Ask to list applications first."}
            )
        if n is None or n < 1 or n > len(listing):
            return json.dumps({"error": f"No application number {n}."})
        record = listing[n - 1]
        url = record.get("url", "")
        if not url:
            return json.dumps({"error": "Cannot update a manual entry with no URL."})
        original_status = record.get("status", "")
        ok = application_store.update_status(url, status)
        if ok:
            listing[n - 1]["status"] = status
            msg = f"Updated application {n} to '{status}'."
            if original_status == "accepted" and status == "declined":
                org = record.get("organisation") or record.get("header", "")
                date = record.get("date", "")
                # Delete travel buffer events
                cal = _make_calendar_client()
                if cal:
                    for field in ("travel_before_event_id", "travel_after_event_id"):
                        evt_id = record.get(field)
                        if evt_id:
                            try:
                                cal.delete_event(evt_id)
                            except Exception as del_exc:
                                logger.warning(
                                    "manage_applications: failed to delete travel buffer %s: %s",
                                    evt_id,
                                    del_exc,
                                )
                msg += (
                    f"\n\nThis was a confirmed booking ({org} on {date}). "
                    "Do you want to delete the calendar event?"
                )
            return json.dumps({"result": msg})
        return json.dumps({"error": "Application not found in store."})

    if action == "detail":
        n = input_data.get("number")
        listing = _last_application_listing.get(chat_id)
        if not listing:
            return json.dumps(
                {
                    "error": "No application listing cached. Ask to list or summarise applications first."
                }
            )
        if n is None or n < 1 or n > len(listing):
            return json.dumps({"error": f"No application number {n}."})
        r = listing[n - 1]
        lines = [
            f"📋 Application {n} — full details",
            "",
            f"Header:        {r.get('header') or '—'}",
            f"Organisation:  {r.get('organisation') or '—'}",
            f"Date:          {r.get('date') or '—'}",
            f"Fee:           {r.get('fee') or '—'}",
            f"Status:        {r.get('status') or '—'}",
            f"Email:         {r.get('email') or '—'}",
            f"URL:           {r.get('url') or '—'}",
            f"Applied at:    {r.get('applied_at') or '—'}",
            f"Updated at:    {r.get('updated_at') or '—'}",
        ]
        return json.dumps({"result": "\n".join(lines)})

    return json.dumps({"error": f"Unknown action: {action}"})


# ── NEG-application tools ────────────────────────────────────────────────────


def _neg_body_as_text(body: str) -> str:
    """Plain-text rendering of a draft HTML body for Telegram previews.

    Same crude tag-strip as main._send_neg_alert — the body is our own
    hand-written Jinja2 template, so there are no scripts/styles to worry about.
    """
    import html as _html
    import re as _re

    plain = _html.unescape(_re.sub(r"<[^>]+>", "", body)).strip()
    return _re.sub(r"\n{3,}", "\n\n", plain)


def _find_neg_row(gig_id: str) -> dict | None:
    for r in application_store.list_neg_pending():
        if r.get("gig_id") == gig_id:
            return r
    return None


def _neg_row_lookup_error(gig_id: str) -> str:
    existing = application_store.get_by_gig_id(gig_id)
    if existing is None:
        return f"No draft found with id {gig_id}."
    decided = existing.get("decided_at") or existing.get("updated_at") or "unknown time"
    return f"Already {existing.get('status')} at {decided}."


def set_active_neg_draft(chat_id: int, gig_id: str) -> None:
    _active_neg_draft[chat_id] = gig_id


def get_active_neg_draft(chat_id: int) -> str | None:
    return _active_neg_draft.get(chat_id)


def stash_pending_neg_instruction(chat_id: int, text: str) -> None:
    _pending_neg_instruction[chat_id] = text


def pop_pending_neg_instruction(chat_id: int) -> str | None:
    return _pending_neg_instruction.pop(chat_id, None)


def _draft_buttons(gig_id: str) -> list[list[dict]]:
    return [
        [
            {"text": "✅ Accept", "callback_data": f"neg:accept:{gig_id}"},
            {"text": "✏️ Edit", "callback_data": f"neg:edit:{gig_id}"},
            {"text": "❌ Reject", "callback_data": f"neg:reject:{gig_id}"},
        ]
    ]


def neg_confirm_buttons(gig_id: str, *, send: bool) -> list[list[dict]]:
    confirm_action = "confirm_send" if send else "confirm_reject"
    return [
        [
            {"text": "Confirm", "callback_data": f"neg:{confirm_action}:{gig_id}"},
            {"text": "Cancel", "callback_data": f"neg:cancel:{gig_id}"},
        ]
    ]


async def neg_confirm_send(gig_id: str) -> tuple[bool, str]:
    """Send the current draft for gig_id and transition it to applied.

    Called by the deterministic Telegram button handler, never by the LLM —
    this is the one place a NEG email actually gets sent.
    """
    row = _find_neg_row(gig_id)
    if row is None:
        return False, _neg_row_lookup_error(gig_id)
    try:
        send_application_email(
            transport=SMTPTransport(password=settings.email_password),
            settings=settings,
            subject=row["draft_subject"],
            body=row["draft_body"],
            recipient=row["email"],
            cc=[settings.cc_email] if settings.cc_email else None,
        )
    except Exception as exc:
        logger.exception("NEG confirm_send: send failed", extra={"gig_id": gig_id})
        return False, f"Send failed: {exc}"
    ok = application_store.transition_neg_pending(gig_id, to="applied", sent_body=row["draft_body"])
    if not ok:
        # The email above was sent successfully — this call just lost the
        # race to record it (a concurrent tap got there first, or the write
        # itself failed). Either way the send already happened; say so.
        logger.warning(
            "NEG confirm_send: email sent but transition failed", extra={"gig_id": gig_id}
        )
        return False, f"Sent to {row.get('email')}, but failed to record — check applications.json."
    logger.info("NEG application sent", extra={"gig_id": gig_id})
    return True, f"Sent to {row.get('email')}."


def neg_confirm_reject(gig_id: str) -> tuple[bool, str]:
    """Transition gig_id to rejected. Called by the deterministic button handler."""
    row = _find_neg_row(gig_id)
    if row is None:
        return False, _neg_row_lookup_error(gig_id)
    ok = application_store.transition_neg_pending(gig_id, to="rejected")
    if not ok:
        return False, "Already decided."
    logger.info("NEG application rejected", extra={"gig_id": gig_id})
    return True, "Draft rejected — no email sent."


def neg_draft_view(gig_id: str) -> tuple[str, list[list[dict]]] | None:
    """Current draft text + Accept/Edit/Reject buttons for gig_id, or None if
    it's not a pending draft (already decided or unknown id)."""
    row = _find_neg_row(gig_id)
    if row is None:
        return None
    text = (
        f"Draft email — id: {gig_id}\n\n"
        f"Subject: {row.get('draft_subject')}\n\n"
        f"{_neg_body_as_text(row.get('draft_body') or '')}"
    )
    return text, _draft_buttons(gig_id)


def _resolve_neg_gig_id(chat_id: int, gig_id: str | None) -> str | None:
    """Resolve which draft a NEG tool call targets when gig_id is omitted.

    Returns the resolved gig_id, or None if ambiguous — caller must show a
    picker (via _neg_picker_response) in that case.
    """
    if gig_id:
        if _find_neg_row(gig_id) is not None:
            set_active_neg_draft(chat_id, gig_id)
        return gig_id
    pending = application_store.list_neg_pending()
    if len(pending) == 1:
        return pending[0]["gig_id"]
    active = get_active_neg_draft(chat_id)
    if active and any(r["gig_id"] == active for r in pending):
        return active
    return None


def _neg_picker_response(pending: list[dict]) -> str:
    if not pending:
        return json.dumps({"result": "No NEG drafts pending."})
    buttons = [
        [
            {
                "text": f"{r.get('header', '?')[:30]} ({r['gig_id']})",
                "callback_data": f"neg:pick:{r['gig_id']}",
            }
        ]
        for r in pending
    ]
    return json.dumps(
        {"result": "Which draft did you mean?", "buttons": buttons, "needs_pick": True}
    )


@_handler("list_neg_pending")
async def _handle_list_neg_pending(input_data: dict, chat_id: int) -> str:
    rows = application_store.list_neg_pending()
    if not rows:
        return json.dumps({"result": "No NEG drafts pending."})
    lines = [f"{len(rows)} NEG draft(s) pending:"]
    for r in rows:
        preview = _neg_body_as_text(r.get("draft_body") or "")[:120].replace("\n", " ")
        lines.append(
            f"  • {r['gig_id']}  {r.get('date', '?')}  {r.get('header', '?')[:50]}\n"
            f"      → £{r.get('negotiable_fee')}  preview: {preview}..."
        )
    return json.dumps({"result": "\n".join(lines)})


@_handler("approve_neg_application")
async def _handle_approve_neg(input_data: dict, chat_id: int) -> str:
    gig_id = _resolve_neg_gig_id(chat_id, input_data.get("gig_id") or None)
    if gig_id is None:
        return _neg_picker_response(application_store.list_neg_pending())

    row = _find_neg_row(gig_id)
    if row is None:
        return json.dumps({"result": _neg_row_lookup_error(gig_id)})

    return json.dumps(
        {
            "result": (
                f"Will send this draft to {row.get('email')}.\n\n"
                f"Subject: {row.get('draft_subject')}\n\n"
                f"{_neg_body_as_text(row.get('draft_body') or '')}"
            ),
            "buttons": neg_confirm_buttons(gig_id, send=True),
        }
    )


@_handler("edit_neg_application")
async def _handle_edit_neg(input_data: dict, chat_id: int) -> str:
    gig_id = _resolve_neg_gig_id(chat_id, input_data.get("gig_id") or None)
    if gig_id is None:
        return _neg_picker_response(application_store.list_neg_pending())

    new_body: str | None = input_data.get("new_body")
    new_fee: int | None = input_data.get("new_fee")

    row = _find_neg_row(gig_id)
    if row is None:
        return json.dumps({"result": _neg_row_lookup_error(gig_id)})

    if new_body is None and new_fee is None:
        return json.dumps({"result": "Provide either new_body or new_fee."})

    if new_fee is not None:
        gig = Gig(
            header=row.get("header", ""),
            organisation=row.get("organisation", ""),
            locality="",
            date=row.get("date", ""),
            time=row.get("time", ""),
            fee=row.get("fee", ""),
            link=row.get("url", ""),
            contact=row.get("contact") or None,
            email=row.get("email", ""),
            postcode=row.get("postcode", ""),
        )
        notifier = Notifier(settings, SMTPTransport(password=settings.email_password))
        _, new_body = notifier.draft_negotiation(gig, negotiable_fee=int(new_fee))

    assert new_body is not None
    application_store.update_neg_draft(
        gig_id,
        draft_body=new_body,
        negotiable_fee=int(new_fee) if new_fee is not None else None,
    )
    set_active_neg_draft(chat_id, gig_id)

    return json.dumps(
        {
            "result": (
                f"Revised draft for '{row.get('header')}':\n\n{_neg_body_as_text(new_body)}"
            ),
            "buttons": _draft_buttons(gig_id),
        }
    )


@_handler("reject_neg_application")
async def _handle_reject_neg(input_data: dict, chat_id: int) -> str:
    gig_id = _resolve_neg_gig_id(chat_id, input_data.get("gig_id") or None)
    if gig_id is None:
        return _neg_picker_response(application_store.list_neg_pending())

    row = _find_neg_row(gig_id)
    if row is None:
        return json.dumps({"result": _neg_row_lookup_error(gig_id)})

    return json.dumps(
        {
            "result": f"Reject NEG draft for '{row.get('header')}'?",
            "buttons": neg_confirm_buttons(gig_id, send=False),
        }
    )


# ── Config tools ─────────────────────────────────────────────────────────────


@_handler("clear_conversation")
async def _handle_clear_conversation(input_data: dict, chat_id: int) -> str:
    reset_conversation(chat_id)
    return json.dumps({"result": "Conversation cleared."})


@_handler("manage_config")
async def _handle_manage_config(input_data: dict, chat_id: int) -> str:
    action = input_data["action"]

    _RANGES: dict[str, tuple[int, int]] = {
        "min_fee": (0, 100_000),
        "max_travel_minutes": (1, 300),
        "poll_minutes": (1, 60),
        "negotiable_fee": (0, 100_000),
    }
    _DEFAULTS = {
        "min_fee": settings.min_fee,
        "max_travel_minutes": settings.max_travel_minutes,
        "poll_minutes": settings.poll_minutes,
        "negotiable_fee": settings.negotiable_fee,
    }

    if action == "get":
        overrides = runtime_config.all()
        lines = []
        for key, default in _DEFAULTS.items():
            if key in overrides:
                lines.append(f"{key:<20} {overrides[key]}  (override, default: {default})")
            else:
                lines.append(f"{key:<20} {default}  (default)")
        return json.dumps({"result": "\n".join(lines)})

    if action == "set":
        key = input_data.get("key", "")
        value = input_data.get("value")
        if key not in _RANGES:
            return json.dumps({"result": f"Unknown key '{key}'. Valid keys: {', '.join(_RANGES)}."})
        if value is None:
            return json.dumps({"result": "value is required for set."})
        lo, hi = _RANGES[key]
        if not (lo <= int(value) <= hi):
            return json.dumps(
                {"result": f"Invalid value {value} for {key}. Must be between {lo} and {hi}."}
            )
        runtime_config.set(key, int(value))
        return json.dumps(
            {"result": f"{key} set to {value}. Takes effect on the next polling tick."}
        )

    if action == "reset":
        key = input_data.get("key", "")
        if key not in _DEFAULTS:
            return json.dumps(
                {"result": f"Unknown key '{key}'. Valid keys: {', '.join(_DEFAULTS)}."}
            )
        existed = runtime_config.reset(key)
        if existed:
            return json.dumps({"result": f"{key} reset to default ({_DEFAULTS[key]})."})
        return json.dumps({"result": f"{key} was already using the default ({_DEFAULTS[key]})."})

    return json.dumps({"error": f"Unknown action: {action}"})


@_handler("manage_llm_provider")
async def _handle_manage_llm_provider(input_data: dict, chat_id: int) -> str:
    action = input_data.get("action", "")

    if action == "get":
        provider = runtime_config.get("llm_provider", _DEFAULT_PROVIDER)
        model = runtime_config.get("llm_model", _default_model_string())
        return json.dumps({"result": f"Current provider: {provider}\nCurrent model: {model}"})

    if action == "set":
        provider = input_data.get("provider", "")
        if not provider:
            return json.dumps({"result": "provider is required for action='set'."})
        if provider not in _PROVIDER_MODELS:
            valid = ", ".join(_PROVIDER_MODELS)
            return json.dumps({"result": f"Unknown provider '{provider}'. Valid: {valid}."})

        api_key_field = _PROVIDER_API_KEY_FIELD[provider]
        if not getattr(settings, api_key_field):
            return json.dumps(
                {"result": f"Can't switch to {provider} — {api_key_field.upper()} isn't set."}
            )

        model_key = input_data.get("model", "")
        provider_models = _PROVIDER_MODELS[provider]
        if not model_key:
            options = ", ".join(provider_models)
            return json.dumps({"result": f"Which {provider} model? Options: {options}."})
        if model_key not in provider_models:
            valid = ", ".join(provider_models)
            return json.dumps(
                {"result": f"Unknown model '{model_key}' for {provider}. Valid: {valid}."}
            )

        _pending_llm_switch[chat_id] = (provider, model_key)
        return json.dumps(
            {
                "result": f"Switch to {provider}/{model_key}?",
                "buttons": _llm_switch_buttons(provider, model_key),
            }
        )

    if action == "reset":
        runtime_config.reset("llm_provider")
        runtime_config.reset("llm_model")
        return json.dumps({"result": "Reset to default (anthropic/sonnet)."})

    return json.dumps({"error": f"Unknown action: {action}"})


def _llm_switch_buttons(provider: str, model_key: str) -> list[list[dict]]:
    target = f"{provider}/{model_key}"
    return [
        [
            {"text": "Confirm", "callback_data": f"llm:confirm:{target}"},
            {"text": "Cancel", "callback_data": f"llm:cancel:{target}"},
        ]
    ]


def llm_confirm_switch(chat_id: int, provider: str, model_key: str) -> tuple[bool, str]:
    """Apply a pending provider/model switch. Called by the deterministic
    Telegram button handler, never by the LLM — same two-step pattern as
    neg_confirm_send. Requires the (provider, model_key) target to still
    match what's pending for this chat, so a stale or double-tapped button
    can't apply a switch that a newer request already replaced."""
    if _pending_llm_switch.get(chat_id) != (provider, model_key):
        return False, "This switch is no longer pending — it may have been replaced or cancelled."
    del _pending_llm_switch[chat_id]
    runtime_config.set("llm_provider", provider)
    runtime_config.set("llm_model", _PROVIDER_MODELS[provider][model_key])
    return True, f"Switched to {provider}/{model_key}."


def llm_cancel_switch(chat_id: int, provider: str, model_key: str) -> tuple[bool, str]:
    """Discard a pending provider/model switch without applying it."""
    if _pending_llm_switch.get(chat_id) != (provider, model_key):
        return False, "This switch is no longer pending."
    del _pending_llm_switch[chat_id]
    return True, "Switch cancelled."


@_handler("get_llm_usage_summary")
async def _handle_get_llm_usage_summary(input_data: dict, chat_id: int) -> str:
    now = datetime.datetime.now(datetime.UTC)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today = llm_usage_store.summary(since=today_start)
    all_time = llm_usage_store.summary()

    if not all_time:
        return json.dumps({"result": "No LLM usage recorded yet."})

    lines = ["LLM usage by provider:"]
    for provider in sorted(all_time):
        t = today.get(provider, {"call_count": 0, "total_tokens": 0})
        a = all_time[provider]
        lines.append(
            f"- {provider}: today {t['call_count']} calls / {t['total_tokens']} tokens "
            f"— all-time {a['call_count']} calls / {a['total_tokens']} tokens"
        )
    return json.dumps({"result": "\n".join(lines)})


async def process_message(
    chat_id: int,
    text: str,
    on_step: Callable[[str], Awaitable[None]] | None = None,
) -> list[AgentResponse]:
    provider = runtime_config.get("llm_provider", _DEFAULT_PROVIDER)
    if provider not in _PROVIDER_MODELS:
        logger.warning("Unknown stored llm_provider %r, resetting to default", provider)
        runtime_config.reset("llm_provider")
        runtime_config.reset("llm_model")
        provider = _DEFAULT_PROVIDER
    model = runtime_config.get("llm_model", _default_model_string())

    _hydrate_chat(chat_id)

    if chat_id not in _histories:
        _histories[chat_id] = []

    _histories[chat_id].append({"role": "user", "content": text})

    responses: list[AgentResponse] = []
    steps: list[str] = []

    while True:
        # provider/model are updated from what actually served the call, so a
        # mid-conversation failover (see _call_llm_with_failover) sticks for
        # the rest of this turn's tool-calling loop instead of retrying the
        # already-failed provider again on every iteration.
        response, provider, model = await _call_llm_with_failover(
            provider,
            model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                *_histories[chat_id],
            ],
            tools=TOOLS,
        )

        msg = response.choices[0].message
        # NOT exclude_none=True — see Global Constraints. A tool-call turn has
        # content: None alongside tool_calls; dropping that key entirely (rather
        # than keeping it as an explicit null) breaks the Anthropic backend on the
        # NEXT turn, since LiteLLM's Anthropic translation requires `content` to be
        # present on every message.
        _histories[chat_id].append(msg.model_dump())

        if not msg.tool_calls:
            if msg.content:
                responses.append(AgentResponse(text=msg.content))
            break

        tool_results = []
        for tc in msg.tool_calls:
            name = tc.function.name
            args = json.loads(tc.function.arguments)
            logger.info("Unified agent tool call: %s(%s)", name, json.dumps(args))

            steps.append(f"🔧 {name}")
            if on_step is not None:
                await on_step("\n".join(steps))

            try:
                result = await _execute_tool(name, args, chat_id)
            except Exception as e:
                logger.error("Tool execution failed: %s", e)
                result = json.dumps({"error": str(e)})

            steps[-1] = f"✅ {name}"
            if on_step is not None:
                await on_step("\n".join(steps))

            if name in _VERBATIM_RESPONSE_TOOLS:
                try:
                    data = json.loads(result)
                    if "result" in data:
                        responses.append(
                            AgentResponse(text=data["result"], buttons=data.get("buttons"))
                        )
                        if data.get("needs_pick"):
                            stash_pending_neg_instruction(chat_id, text)
                        result = json.dumps({"result": "Listing sent to user."})
                except (json.JSONDecodeError, KeyError):
                    pass

            tool_results.append(
                {"role": "tool", "tool_call_id": tc.id, "name": name, "content": result}
            )

            if name in _PDF_RESPONSE_TOOLS and chat_id in _last_invoice:
                pdf_path = _last_invoice[chat_id].get("pdf_path")
                if pdf_path:
                    inv_num = _last_invoice[chat_id].get("invoice_number", "")
                    responses.append(
                        AgentResponse(file_path=str(pdf_path), file_caption=f"Invoice {inv_num}")
                    )

        if not tool_results:
            responses.append(
                AgentResponse(text="(unexpected empty tool response — please try again)")
            )
            break

        _histories[chat_id].extend(tool_results)

    _trim_history(chat_id)
    _persist_chat(chat_id)
    return responses


def reset_conversation(chat_id: int) -> None:
    _histories.pop(chat_id, None)
    _last_invoice.pop(chat_id, None)
    _last_gig_listing.pop(chat_id, None)
    _last_application_listing.pop(chat_id, None)
    _active_neg_draft.pop(chat_id, None)
    _pending_neg_instruction.pop(chat_id, None)
    _pending_llm_switch.pop(chat_id, None)
    _hydrated.discard(chat_id)
    agent_state.save_chat(chat_id, {})


class UnifiedAgent:
    """Thin wrapper around the module-level _execute_tool for testability."""

    async def _execute_tool(self, name: str, input_data: dict, chat_id: int) -> str:
        return await _execute_tool(name, input_data, chat_id)
