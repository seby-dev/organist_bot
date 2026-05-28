from __future__ import annotations

import datetime
import json
import logging
from dataclasses import dataclass
from typing import cast

from organist_bot import analytics, application_store, filter_store, travel
from organist_bot.config import settings
from organist_bot.filters import normalize_to_yyyymmdd, parse_start_time
from organist_bot.integrations.calendar_client import GoogleCalendarClient
from organist_bot.integrations.email_sender import send_invoice_email
from organist_bot.integrations.invoice_generator import (
    add_client,
    delete_client,
    edit_client,
    generate_invoice,
    load_clients,
    load_invoices,
    mark_invoice_emailed,
    mark_invoice_paid,
)
from organist_bot.models import Gig
from organist_bot.runtime_config_store import runtime_config
from organist_bot.scraper import Scraper

logger = logging.getLogger(__name__)

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
- Confirm before calling generate_invoice, duplicate_invoice, send_invoice_email, resend_invoice, or delete_client. Present a clear summary and ask "Shall I go ahead?"
- If missing required info (client, description, quantity, or unit price), ask for the missing details.
- Invoices can have multiple line items — ask if the user wants to add more items before generating.
- After generating an invoice, ask if the user wants to email it.
- Use list_clients to look up available client keys when the user mentions a client by name.
- Use list_invoices to look up past invoices when the user mentions a client or date.
- "Mark INV-2026-001 as paid" / "invoice has been paid" → mark_invoice_paid.
- Use £ for money.

## Filter management
- "Add <email> to the blacklist" → manage_blacklist(action=add, email=<email>).
- "Remove <email> from the blacklist" → manage_blacklist(action=remove, email=<email>).
- "I'm unavailable in December" → manage_unavailable(action=add, period=2026-12).
- "I'm unavailable on 25 Dec" → manage_unavailable(action=add, period=2026-12-25).
- "Add an available-only period" → manage_available(action=add, period=<period>).
- Period formats: YYYY-MM-DD, YYYY-MM-DD:YYYY-MM-DD, YYYY-MM. Also: today, tomorrow, this/next <weekday>, this weekend, next week, this/next month.

## Pipeline stats
- "Show stats" / "how's the pipeline?" / "gig stats this week" → call get_gig_stats.
- Accept an optional number of days: "stats for the last 30 days" → get_gig_stats(days=30).

## Runtime config
- "What's the current config?" / "show config" → manage_config(action=get).
- "Set min fee to 150" → manage_config(action=set, key=min_fee, value=150).
- "Reset min fee to default" → manage_config(action=reset, key=min_fee).
- Editable keys: min_fee, max_travel_minutes, poll_minutes.

## Application tracking
- "What applications are pending?" / "show my applications" → manage_applications(action=list).
- "Application summary" / "how many gigs have I applied to?" → manage_applications(action=summary).
- "Mark application 2 as declined" → manage_applications(action=update, number=2, status=declined).
- "Show me full details of application 3" / "tell me more about #2" → manage_applications(action=detail, number=3).
- Valid statuses for update: applied, accepted, no_response, declined.
- "What's my acceptance rate?" / "show analytics" → get_application_analytics.
- "Break down my gigs by type" / "which gig types do I win?" → get_gig_breakdown.

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
        "description": "Update one or more fields of an existing client. Only provide the fields to change.",
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
    # ── Meta ────────────────────────────────────────────────────────────────
    {
        "name": "clear_conversation",
        "description": "Clear this chat's conversation history and all cached state.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    # ── Pipeline stats ──────────────────────────────────────────────────────
    {
        "name": "get_gig_stats",
        "description": (
            "Query the Google Sheets log and return pipeline stats. "
            "Shows total runs, gigs listed/filtered/valid, filter rejection breakdown, "
            "and the most recent run. Accepts optional 'days' parameter (default 7, max 90)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Number of days to look back (default 7, max 90).",
                }
            },
            "required": [],
        },
    },
    # ── Runtime config ──────────────────────────────────────────────────────
    {
        "name": "manage_config",
        "description": (
            "Read or update runtime pipeline configuration. "
            "Editable keys: min_fee (int, ≥0), max_travel_minutes (int, 1–300), "
            "poll_minutes (int, 1–60). Changes take effect on the next polling tick. "
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
                    "enum": ["min_fee", "max_travel_minutes", "poll_minutes"],
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


@dataclass
class AgentResponse:
    text: str | None = None
    file_path: str | None = None
    file_caption: str | None = None


# Per-chat state
_histories: dict[int, list[dict]] = {}
_last_invoice: dict[int, dict] = {}
_last_gig_listing: dict[int, list[dict]] = {}
_last_application_listing: dict[int, list[dict]] = {}

_PDF_RESPONSE_TOOLS = {"generate_invoice", "duplicate_invoice", "get_invoice"}
_VERBATIM_RESPONSE_TOOLS = {
    "list_upcoming_gigs",
    "get_gig_stats",
    "manage_config",
    "manage_applications",
    "get_income_forecast",
    "get_application_analytics",
    "get_gig_breakdown",
}


def _make_calendar_client() -> GoogleCalendarClient | None:
    if settings.google_calendar_id and settings.google_calendar_credentials_file:
        return GoogleCalendarClient(
            credentials_file=settings.google_calendar_credentials_file,
            calendar_id=settings.google_calendar_id,
        )
    return None


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


def _make_sheets_logger():
    """Return a SheetsLogger if Sheets is configured, else None."""
    if not settings.google_sheets_id:
        logger.debug("_make_sheets_logger: GOOGLE_SHEETS_ID not set")
        return None
    creds_file = (
        settings.google_sheets_credentials_file or settings.google_calendar_credentials_file
    )
    if not creds_file:
        logger.debug("_make_sheets_logger: no credentials file")
        return None
    try:
        from organist_bot.integrations.sheets_logger import SheetsLogger

        return SheetsLogger(
            spreadsheet_id=settings.google_sheets_id,
            credentials_file=creds_file,
        )
    except Exception as exc:
        logger.warning("_make_sheets_logger: failed — %s", exc)
        return None


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
    # ── fetch_gig_details ───────────────────────────────────────────────────
    if name == "fetch_gig_details":
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

    # ── add_gig ─────────────────────────────────────────────────────────────
    if name == "add_gig":
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

    # ── list_upcoming_gigs ──────────────────────────────────────────────────
    if name == "list_upcoming_gigs":
        cal = _make_calendar_client()
        if cal is None:
            return json.dumps({"error": "Google Calendar not configured."})
        max_results = input_data.get("max_results", 10)
        events = cal.list_upcoming_events(max_results=max_results)
        events = [e for e in events if e["summary"] != "Unavailable"]
        events = sorted(events, key=lambda e: e["start_dt"])
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

    # ── delete_gig ──────────────────────────────────────────────────────────
    if name == "delete_gig":
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

    # ── edit_gig ─────────────────────────────────────────────────────────────
    if name == "edit_gig":
        n = input_data["number"]
        listing = _last_gig_listing.get(chat_id)
        if not listing:
            return json.dumps({"error": "No gig listing cached. Ask me to list your gigs first."})
        if n < 1 or n > len(listing):
            return json.dumps(
                {"error": f"No gig number {n}. There are {len(listing)} gigs listed."}
            )
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

    # ── list_clients ────────────────────────────────────────────────────────
    if name == "list_clients":
        clients = load_clients()
        if not clients:
            return json.dumps(
                {"result": "No clients found. Add one with a natural language request."}
            )
        return json.dumps(clients, indent=2)

    if name == "get_client":
        clients = load_clients()
        key = input_data["client_key"]
        if key not in clients:
            return json.dumps(
                {"error": f"Client '{key}' not found. Available: {', '.join(clients.keys())}"}
            )
        return json.dumps({key: clients[key]}, indent=2)

    if name == "add_client":
        add_client(
            key=input_data["key"],
            name=input_data["name"],
            address=input_data["address"],
            email=input_data.get("email", ""),
            cc=input_data.get("cc", []),
        )
        return json.dumps({"result": f"Client '{input_data['key']}' added successfully."})

    if name == "edit_client":
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

    if name == "delete_client":
        try:
            delete_client(input_data["key"])
            return json.dumps({"result": f"Client '{input_data['key']}' deleted."})
        except ValueError as e:
            return json.dumps({"error": str(e)})

    if name == "generate_invoice":
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

    if name == "duplicate_invoice":
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

    if name == "send_invoice_email":
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

    if name == "resend_invoice":
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

    if name == "mark_invoice_paid":
        inv_num = input_data["invoice_number"]
        ok = mark_invoice_paid(inv_num)
        if not ok:
            return json.dumps({"error": f"Invoice {inv_num} not found."})
        return json.dumps({"result": f"✅ Invoice {inv_num} marked as paid."})

    if name == "list_invoices":
        invoices = load_invoices()
        if not invoices:
            return json.dumps({"result": "No invoices found."})

        import datetime as _dt

        now = _dt.datetime.now(_dt.UTC)

        def _payment_status(r: dict) -> str:
            if r.get("paid_at"):
                return "✅ paid"
            emailed_at_str = r.get("emailed_at")
            if not r.get("emailed") or not emailed_at_str:
                return "not sent"
            try:
                emailed_at = _dt.datetime.fromisoformat(emailed_at_str.replace("Z", "+00:00"))
                days = (now - emailed_at).days
                if days >= 5:
                    return f"⏰ overdue ({days}d)"
                return f"emailed {days}d ago"
            except ValueError:
                return "emailed"

        records = list(invoices.values())
        records.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        lines = ["📄 Invoices (most recent first)", ""]
        for r in records[:20]:
            pay_status = _payment_status(r)
            lines.append(
                f"{r['invoice_number']}  {r.get('client_name', '?'):<20}"
                f"  £{r.get('total', 0):.2f}  {r.get('date', '?')}  {pay_status}"
            )
        return json.dumps({"result": "\n".join(lines)})

    if name == "get_invoice":
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

    # ── manage_blacklist ────────────────────────────────────────────────────
    if name == "manage_blacklist":
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
                f"Added '{email}' to blacklist."
                if added
                else f"'{email}' is already in the blacklist."
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

    # ── manage_unavailable ──────────────────────────────────────────────────
    if name == "manage_unavailable":
        action = input_data["action"]
        if action == "list":
            periods = filter_store.unavailable_periods()
            return (
                json.dumps({"unavailable_periods": periods})
                if periods
                else json.dumps({"result": "No unavailable periods set."})
            )
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
                f"Removed '{period}' from unavailable periods."
                if removed
                else f"'{period}' not found."
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

    # ── manage_available ────────────────────────────────────────────────────
    if name == "manage_available":
        action = input_data["action"]
        if action == "list":
            periods = filter_store.available_only_periods()
            return (
                json.dumps({"available_only_periods": periods})
                if periods
                else json.dumps({"result": "No available-only periods set."})
            )
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

    # ── get_income_forecast ──────────────────────────────────────────────────
    if name == "get_income_forecast":
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

    # ── get_application_analytics ────────────────────────────────────────────
    if name == "get_application_analytics":
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

    # ── get_gig_breakdown ─────────────────────────────────────────────────────
    if name == "get_gig_breakdown":
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

    # ── manage_applications ──────────────────────────────────────────────────
    if name == "manage_applications":
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

    # ── clear_conversation ──────────────────────────────────────────────────
    if name == "clear_conversation":
        reset_conversation(chat_id)
        return json.dumps({"result": "Conversation cleared."})

    # ── get_gig_stats ────────────────────────────────────────────────────────
    if name == "get_gig_stats":
        days = min(max(int(input_data.get("days", 7)), 1), 90)
        sl = _make_sheets_logger()
        if sl is None:
            return json.dumps(
                {"result": "Google Sheets is not configured (GOOGLE_SHEETS_ID missing)."}
            )
        try:
            runs = sl.query_run_stats(days)
        except Exception as exc:
            return json.dumps({"result": f"Could not reach Google Sheets: {exc}"})

        if not runs:
            return json.dumps({"result": f"No pipeline runs logged in the last {days} days."})

        total_runs = len(runs)
        total_listed = sum(r.get("listed", 0) for r in runs)
        total_pre = sum(r.get("pre_filter_passed", 0) for r in runs)
        total_valid = sum(r.get("valid", 0) for r in runs)
        total_errors = sum(r.get("gig_errors", 0) for r in runs)
        avg_listed = round(total_listed / total_runs, 1)
        avg_pre = round(total_pre / total_runs, 1)
        avg_valid = round(total_valid / total_runs, 1)

        # Aggregate filter breakdown, stripping repr params and deduplicating.
        # e.g. "AvailabilityFilter(mode='block', periods=7)" → "AvailabilityFilter"
        name_totals: dict[str, int] = {}
        for r in runs:
            for k, v in r.get("filter_breakdown", {}).items():
                name = k.split("(")[0]
                name_totals[name] = name_totals.get(name, 0) + v
        active_filters = [
            (k, v)
            for k, v in sorted(name_totals.items(), key=lambda x: x[1], reverse=True)
            if v > 0
        ]

        def _fmt_elapsed(ms: int) -> str:
            return f"{ms / 1000:.1f}s" if ms >= 1000 else f"{ms}ms"

        lines = [
            f"📊 *Pipeline stats — last {days} days*",
            "",
            f"*Runs:* {total_runs}",
            f"*Listed:*      {total_listed:>5}  ({avg_listed}/run)",
            f"*Pre-filter:*  {total_pre:>5}  ({avg_pre}/run)",
            f"*Valid:*       {total_valid:>5}  ({avg_valid}/run)",
            f"*Errors:* {total_errors}",
        ]

        if active_filters:
            max_len = max(len(k) for k, _ in active_filters)
            lines += ["", "*🔍 Filter rejections:*"]
            for k, v in active_filters:
                pct = round(v / total_listed * 100) if total_listed else 0
                lines.append(f"`{k:<{max_len}}  {v:>4}  ({pct}%)`")

        lines += ["", "*📅 Recent runs:*"]
        for r in runs[:5]:
            ts = r["timestamp"][:16].replace("T", " ")
            listed = r.get("listed", 0)
            pre = r.get("pre_filter_passed", 0)
            valid = r.get("valid", 0)
            t = _fmt_elapsed(r.get("elapsed_ms", 0))
            lines.append(f"`{ts}  {listed}→{pre}→{valid}  {t}`")

        return json.dumps({"result": "\n".join(lines)})

    # ── manage_config ────────────────────────────────────────────────────────
    if name == "manage_config":
        action = input_data["action"]

        _RANGES: dict[str, tuple[int, int]] = {
            "min_fee": (0, 100_000),
            "max_travel_minutes": (1, 300),
            "poll_minutes": (1, 60),
        }
        _DEFAULTS = {
            "min_fee": settings.min_fee,
            "max_travel_minutes": settings.max_travel_minutes,
            "poll_minutes": settings.poll_minutes,
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
                return json.dumps(
                    {"result": f"Unknown key '{key}'. Valid keys: {', '.join(_RANGES)}."}
                )
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
            return json.dumps(
                {"result": f"{key} was already using the default ({_DEFAULTS[key]})."}
            )

        return json.dumps({"error": f"Unknown action: {action}"})

    return json.dumps({"error": f"Tool not implemented: {name}"})


async def process_message(chat_id: int, text: str) -> list[AgentResponse]:
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    if chat_id not in _histories:
        _histories[chat_id] = []

    _histories[chat_id].append({"role": "user", "content": text})

    responses: list[AgentResponse] = []

    while True:
        response = await client.messages.create(
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

        if response.stop_reason != "tool_use":
            responses.append(AgentResponse(text="(response truncated — please try again)"))
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            logger.info("Unified agent tool call: %s(%s)", block.name, json.dumps(block.input))
            try:
                result = await _execute_tool(block.name, block.input, chat_id)
            except Exception as e:
                logger.error("Tool execution failed: %s", e)
                result = json.dumps({"error": str(e)})

            if block.name in _VERBATIM_RESPONSE_TOOLS:
                try:
                    data = json.loads(result)
                    if "result" in data:
                        responses.append(AgentResponse(text=data["result"]))
                        result = json.dumps({"result": "Listing sent to user."})
                except (json.JSONDecodeError, KeyError):
                    pass

            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

            if block.name in _PDF_RESPONSE_TOOLS and chat_id in _last_invoice:
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

        _histories[chat_id].append({"role": "user", "content": tool_results})

    return responses


def reset_conversation(chat_id: int) -> None:
    _histories.pop(chat_id, None)
    _last_invoice.pop(chat_id, None)
    _last_gig_listing.pop(chat_id, None)
    _last_application_listing.pop(chat_id, None)


class UnifiedAgent:
    """Thin wrapper around the module-level _execute_tool for testability."""

    async def _execute_tool(self, name: str, input_data: dict, chat_id: int) -> str:
        return await _execute_tool(name, input_data, chat_id)
