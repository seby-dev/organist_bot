from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import anthropic

from organist_bot.config import settings
from organist_bot.integrations.calendar_client import GoogleCalendarClient
from organist_bot.models import Gig
from organist_bot.scraper import Scraper

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
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
"""

TOOLS = [
    {
        "name": "fetch_gig_details",
        "description": "Fetch gig details from an organistsonline.org URL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The full gig detail URL."},
            },
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
]


@dataclass
class GigAgentResponse:
    text: str
    done: bool = False


_histories: dict[int, list[dict]] = {}


def _make_calendar_client() -> GoogleCalendarClient | None:
    if settings.google_calendar_id and settings.google_calendar_credentials_file:
        return GoogleCalendarClient(
            credentials_file=settings.google_calendar_credentials_file,
            calendar_id=settings.google_calendar_id,
        )
    return None


async def _execute_tool(name: str, input_data: dict) -> str:
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
            summary = (
                "*Please confirm the following gig:*\n"
                f"• *Title:* {fields['header']}\n"
                f"• *Organisation:* {fields['organisation']}\n"
                f"• *Locality:* {fields['locality']}\n"
                f"• *Date:* {fields['date']}\n"
                f"• *Time:* {fields['time']}\n"
                f"• *Fee:* {fields['fee']}\n\n"
                "Reply *yes* to add to calendar, or tell me what to change."
            )
            return summary

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
            return json.dumps({"result": f"Added to calendar. Event ID: {event_id}"})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    return json.dumps({"error": f"Unknown tool: {name}"})


async def process_message(chat_id: int, text: str) -> GigAgentResponse:
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    if chat_id not in _histories:
        _histories[chat_id] = []

    _histories[chat_id].append({"role": "user", "content": text})

    final_text = ""
    done = False

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
                    final_text = block.text
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            logger.info("Gig agent tool call: %s(%s)", block.name, json.dumps(block.input))
            try:
                result = await _execute_tool(block.name, block.input)
            except Exception as exc:
                result = json.dumps({"error": str(exc)})
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
            if block.name == "add_gig" and '"result"' in result:
                done = True

        _histories[chat_id].append({"role": "user", "content": tool_results})

    return GigAgentResponse(text=final_text, done=done)


def reset_gig_conversation(chat_id: int) -> None:
    _histories.pop(chat_id, None)
