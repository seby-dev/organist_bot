from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import cast

import anthropic

from organist_bot.config import settings
from organist_bot.integrations.email_sender import send_invoice_email
from organist_bot.integrations.invoice_generator import (
    add_client,
    delete_client,
    edit_client,
    generate_invoice,
    load_clients,
    load_invoices,
    mark_invoice_emailed,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are an invoice assistant for a Telegram bot. You help users generate PDF invoices, manage clients, and track invoice history.

Rules:
- Always confirm with the user before calling generate_invoice, duplicate_invoice, send_invoice_email, resend_invoice, or delete_client. Present a clear summary and ask "Shall I go ahead?"
- If the user's request is missing required info (client, description, quantity, or unit price), ask for the missing details.
- Invoices can have multiple line items — ask if the user wants to add more items before generating.
- Use list_clients to look up available client keys when the user mentions a client by name.
- When referring to money, use the £ symbol.
- After generating an invoice, ask if the user wants to email it to the client.
- When adding or editing a client, confirm the details before saving.
- Use list_invoices to look up past invoices when the user mentions a client or date.
- Keep responses concise — this is a chat interface.
"""

TOOLS = [
    # ── Client management ──────────────────────────────────────────────────
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
                "client_key": {
                    "type": "string",
                    "description": "The client key, e.g. 'holy-cross'",
                },
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
                    "description": "Full address (use <br> for line breaks)",
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
        "description": "Update one or more fields of an existing client. Only provide the fields you want to change.",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "The client key to update"},
                "name": {"type": "string", "description": "New contact name"},
                "address": {
                    "type": "string",
                    "description": "New address (use <br> for line breaks)",
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
            "properties": {
                "key": {"type": "string", "description": "The client key to delete"},
            },
            "required": ["key"],
        },
    },
    # ── Invoice generation ─────────────────────────────────────────────────
    {
        "name": "generate_invoice",
        "description": "Generate a PDF invoice for a client with one or more line items. Returns the PDF.",
        "input_schema": {
            "type": "object",
            "properties": {
                "client_key": {"type": "string", "description": "The client key"},
                "items": {
                    "type": "array",
                    "description": "List of line items",
                    "items": {
                        "type": "object",
                        "properties": {
                            "description": {"type": "string", "description": "Service description"},
                            "quantity": {"type": "integer", "description": "Number of units"},
                            "unit_price": {"type": "number", "description": "Price per unit in £"},
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
        "description": "Create a new invoice identical to a previous one (same client and items) with today's date and a new invoice number.",
        "input_schema": {
            "type": "object",
            "properties": {
                "invoice_number": {
                    "type": "string",
                    "description": "Invoice number to duplicate, e.g. 'INV-2026-003'",
                },
            },
            "required": ["invoice_number"],
        },
    },
    # ── Email ──────────────────────────────────────────────────────────────
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
            "properties": {
                "invoice_number": {
                    "type": "string",
                    "description": "Invoice number to resend, e.g. 'INV-2026-003'",
                },
            },
            "required": ["invoice_number"],
        },
    },
    # ── Invoice history ────────────────────────────────────────────────────
    {
        "name": "list_invoices",
        "description": "List recent invoices, showing invoice number, client, amount, date, and whether they were emailed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "client_key": {"type": "string", "description": "Optional: filter by client key"},
                "limit": {
                    "type": "integer",
                    "description": "Max number of invoices to return (default 10)",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_invoice",
        "description": "Retrieve a specific invoice by number and send it as a PDF to the chat.",
        "input_schema": {
            "type": "object",
            "properties": {
                "invoice_number": {
                    "type": "string",
                    "description": "Invoice number, e.g. 'INV-2026-003'",
                },
            },
            "required": ["invoice_number"],
        },
    },
]


@dataclass
class AgentResponse:
    text: str | None = None
    file_path: str | None = None
    file_caption: str | None = None


# Per-chat conversation histories and last-generated invoice cache
_histories: dict[int, list[dict]] = {}
_last_invoice: dict[int, dict] = {}


async def _execute_tool(name: str, input_data: dict, chat_id: int) -> str:
    if name == "list_clients":
        clients = load_clients()
        if not clients:
            return json.dumps({"result": "No clients found. Add one with add_client."})
        return json.dumps(clients, indent=2)

    elif name == "get_client":
        clients = load_clients()
        key = input_data["client_key"]
        if key not in clients:
            return json.dumps(
                {"error": f"Client '{key}' not found. Available: {', '.join(clients.keys())}"}
            )
        return json.dumps({key: clients[key]}, indent=2)

    elif name == "add_client":
        add_client(
            key=input_data["key"],
            name=input_data["name"],
            address=input_data["address"],
            email=input_data.get("email", ""),
            cc=input_data.get("cc", []),
        )
        return json.dumps({"result": f"Client '{input_data['key']}' added successfully."})

    elif name == "edit_client":
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

    elif name == "delete_client":
        try:
            delete_client(input_data["key"])
            return json.dumps({"result": f"Client '{input_data['key']}' deleted."})
        except ValueError as e:
            return json.dumps({"error": str(e)})

    elif name == "generate_invoice":
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

    elif name == "duplicate_invoice":
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

    elif name == "send_invoice_email":
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

    elif name == "resend_invoice":
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

    elif name == "list_invoices":
        invoices = load_invoices()
        if not invoices:
            return json.dumps({"result": "No invoices found."})

        client_filter = input_data.get("client_key")
        limit = input_data.get("limit", 10)

        records = list(invoices.values())
        if client_filter:
            records = [r for r in records if r.get("client_key") == client_filter]

        records.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        records = records[:limit]

        summary = [
            {
                "invoice_number": r["invoice_number"],
                "client": r["client_name"],
                "total": f"{r['currency']}{r['total']:.2f}",
                "date": r["date"],
                "emailed": r.get("emailed", False),
            }
            for r in records
        ]
        return json.dumps(summary, indent=2)

    elif name == "get_invoice":
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

    return json.dumps({"error": f"Unknown tool: {name}"})


_PDF_RESPONSE_TOOLS = {"generate_invoice", "duplicate_invoice", "get_invoice"}


async def process_message(chat_id: int, text: str) -> list[AgentResponse]:
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    if chat_id not in _histories:
        _histories[chat_id] = []

    _histories[chat_id].append({"role": "user", "content": text})

    responses: list[AgentResponse] = []

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=TOOLS,  # type: ignore[arg-type]
            messages=_histories[chat_id],  # type: ignore[arg-type]
        )

        _histories[chat_id].append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    responses.append(AgentResponse(text=block.text))
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            logger.info("Invoice tool call: %s(%s)", block.name, json.dumps(block.input))
            try:
                result = await _execute_tool(block.name, block.input, chat_id)
            except Exception as e:
                logger.error("Tool execution failed: %s", e)
                result = json.dumps({"error": str(e)})

            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                }
            )

            if block.name in _PDF_RESPONSE_TOOLS and chat_id in _last_invoice:
                pdf_path = str(_last_invoice[chat_id]["pdf_path"])
                inv_num = _last_invoice[chat_id].get("invoice_number", "")
                responses.append(
                    AgentResponse(
                        file_path=pdf_path,
                        file_caption=f"Invoice {inv_num}",
                    )
                )

        _histories[chat_id].append({"role": "user", "content": tool_results})

    return responses


def reset_conversation(chat_id: int) -> None:
    _histories.pop(chat_id, None)
    _last_invoice.pop(chat_id, None)
