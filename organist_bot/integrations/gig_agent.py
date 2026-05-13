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
You are a gig calendar assistant. You help an organist add gig bookings to their Google Calendar.

When given a URL, fetch the gig details and add it to the calendar.
When in conversation, ask for: title, organisation, locality, date, time, and fee — then add it.

Rules:
- Always confirm details with the user before calling add_gig.
- Set done=true in your final response once the gig is added or the user cancels.
- Keep responses concise — this is a chat interface.
- Use British English and the £ symbol for fees.
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
        "description": "Add a gig to Google Calendar. Call only after confirming details with the user.",
        "input_schema": {
            "type": "object",
            "properties": {
                "header": {"type": "string"},
                "organisation": {"type": "string"},
                "locality": {"type": "string"},
                "date": {"type": "string", "description": "e.g. 'Sunday 1st June 2025'"},
                "time": {"type": "string", "description": "e.g. '10:30am'"},
                "fee": {"type": "string", "description": "e.g. '£150'"},
            },
            "required": ["header", "date", "time"],
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
        try:
            cal = _make_calendar_client()
            if cal is None:
                return json.dumps({"error": "Google Calendar not configured."})
            gig = Gig(
                header=input_data.get("header", "Gig"),
                organisation=input_data.get("organisation") or "",
                locality=input_data.get("locality") or "",
                date=input_data["date"],
                time=input_data["time"],
                fee=input_data.get("fee"),
                link="",
            )
            event_id = cal.add_gig(gig)
            return json.dumps({"result": f"Added to calendar. Event ID: {event_id}"})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    return json.dumps({"error": f"Unknown tool: {name}"})


async def process_message(chat_id: int, text: str) -> GigAgentResponse:
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    if chat_id not in _histories:
        _histories[chat_id] = []

    _histories[chat_id].append({"role": "user", "content": text})

    final_text = ""
    done = False

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
                    final_text = block.text
            # Agent signals completion by including "done=true" or "DONE" in its reply,
            # or when it has successfully added a gig.
            done = "done=true" in final_text.lower() or "added to calendar" in final_text.lower()
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
