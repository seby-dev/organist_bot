from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import cast  # noqa: F401

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are an assistant for an organist. You handle three areas:

## Gig calendar
- If the user provides a URL, call fetch_gig_details immediately.
- Gather any missing fields (header, organisation, locality, date, time, fee) one at a time.
- Always call add_gig(confirmed=false) first to show a summary; only call confirmed=true after explicit approval.
- "Show my gigs" / "list gigs" → call list_upcoming_gigs.
- "Delete gig 2" → call delete_gig(2). Tell the user to list gigs first if no listing is cached.

## Invoicing
- Confirm before calling generate_invoice, duplicate_invoice, send_invoice_email, resend_invoice, or delete_client. Present a clear summary and ask "Shall I go ahead?"
- If missing required info (client, description, quantity, or unit price), ask for the missing details.
- After generating an invoice, ask if the user wants to email it.
- Use list_clients to look up available client keys when the user mentions a client by name.
- Use list_invoices to look up past invoices when the user mentions a client or date.
- Use £ for money.

## Filter management
- "Add <email> to the blacklist" → manage_blacklist(action=add, email=<email>).
- "Remove <email> from the blacklist" → manage_blacklist(action=remove, email=<email>).
- "I'm unavailable in December" → manage_unavailable(action=add, period=2026-12).
- "I'm unavailable on 25 Dec" → manage_unavailable(action=add, period=2026-12-25).
- "Add an available-only period" → manage_available(action=add, period=<period>).
- Period formats: YYYY-MM-DD, YYYY-MM-DD:YYYY-MM-DD, YYYY-MM.

## Conversation
- If the user asks to start over, reset, or forget everything → call clear_conversation.

## General
- Keep responses concise — this is a chat interface.
- Use British English.
- Use £ for money.
"""

TOOLS: list[dict] = [
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
                "key": {"type": "string"},
                "name": {"type": "string"},
                "address": {
                    "type": "string",
                    "description": "Full address (use <br> for line breaks)",
                },
                "email": {"type": "string"},
                "cc": {"type": "array", "items": {"type": "string"}},
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
                "key": {"type": "string"},
                "name": {"type": "string"},
                "address": {"type": "string"},
                "email": {"type": "string"},
                "cc": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["key"],
        },
    },
    {
        "name": "delete_client",
        "description": "Permanently delete a client from the database. Cannot be undone.",
        "input_schema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    },
    # ── Invoice — generation & email ────────────────────────────────────────
    {
        "name": "generate_invoice",
        "description": "Generate a PDF invoice for a client with one or more line items.",
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
        "description": "List recent invoices.",
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
        "description": "Manage unavailable periods. action: list, add, or remove. period formats: YYYY-MM-DD, YYYY-MM-DD:YYYY-MM-DD, YYYY-MM.",
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
    # ── Meta ────────────────────────────────────────────────────────────────
    {
        "name": "clear_conversation",
        "description": "Clear this chat's conversation history and all cached state.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]


@dataclass
class AgentResponse:
    text: str | None = None
    file_path: str | None = None
    file_caption: str | None = None


# Per-chat state
_histories: dict[int, list[dict]] = {}
_last_invoice: dict[int, dict] = {}
_last_gig_listing: dict[int, list[dict]] = {}

_PDF_RESPONSE_TOOLS = {"generate_invoice", "duplicate_invoice", "get_invoice"}


async def _execute_tool(name: str, input_data: dict, chat_id: int) -> str:
    return json.dumps({"error": f"Tool not implemented: {name}"})


async def process_message(chat_id: int, text: str) -> list[AgentResponse]:
    return [AgentResponse(text="(not implemented)")]


def reset_conversation(chat_id: int) -> None:
    _histories.pop(chat_id, None)
    _last_invoice.pop(chat_id, None)
    _last_gig_listing.pop(chat_id, None)
