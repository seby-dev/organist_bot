"""
organist_bot/calendar_client.py
────────────────────────────────
Google Calendar integration for OrganistBot.

GoogleCalendarClient wraps the Google Calendar API with two operations:

  has_event_on_date(date_str)
    Returns True if the calendar already has at least one event on the given
    date (YYYYMMDD).  Used by CalendarFilter to skip already-busy dates.
    Fails open — if the API call errors, returns False so the gig is not
    silently dropped.

  add_gig(gig)
    Creates an all-day event for a confirmed gig and returns the event ID.
    Used by add_booking.py after the user has manually secured a booking.

Authentication uses a Google service account JSON key file.  To set up:
  1. Create a service account in Google Cloud Console.
  2. Enable the Google Calendar API for the project.
  3. Share your calendar with the service account's email address.
  4. Download the JSON key and set GOOGLE_CALENDAR_CREDENTIALS_FILE in .env.
"""

import datetime
import logging
import time

from google.oauth2 import service_account
from googleapiclient.discovery import build

from organist_bot.filters import normalize_to_yyyymmdd, parse_start_time
from organist_bot.models import Gig

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/calendar"]


class GoogleCalendarClient:
    """Thin wrapper around the Google Calendar v3 API."""

    def __init__(self, credentials_file: str, calendar_id: str):
        self.calendar_id = calendar_id
        creds = service_account.Credentials.from_service_account_file(
            credentials_file, scopes=_SCOPES
        )
        self._service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        logger.debug("GoogleCalendarClient initialised", extra={"calendar_id": calendar_id})

    def has_event_on_date(self, date_str: str) -> bool:
        """Return True if there is at least one event on the given date (YYYYMMDD).

        Fails open — returns False (don't block the gig) if the API call fails.
        """
        t0 = time.perf_counter()
        try:
            dt = datetime.datetime.strptime(date_str, "%Y%m%d").date()
            time_min = datetime.datetime.combine(dt, datetime.time.min).isoformat() + "Z"
            time_max = datetime.datetime.combine(dt, datetime.time.max).isoformat() + "Z"

            result = (
                self._service.events()
                .list(
                    calendarId=self.calendar_id,
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                )
                .execute()
            )

            events = result.get("items", [])
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            logger.debug(
                "Calendar check complete",
                extra={"date": date_str, "event_count": len(events), "elapsed_ms": elapsed_ms},
            )
            return len(events) > 0

        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            logger.warning(
                "Calendar check failed — failing open",
                extra={"date": date_str, "error": str(exc), "elapsed_ms": elapsed_ms},
            )
            return False

    def add_gig(self, gig: Gig) -> str:
        """Create a timed calendar event for a confirmed gig.

        The event starts at the gig's start time and ends one hour later.
        Timezone is set to Europe/London.

        Returns the Google Calendar event ID.
        Raises ValueError if the gig's date or time cannot be parsed.
        """
        date_str = normalize_to_yyyymmdd(gig.date)
        if not date_str:
            raise ValueError(f"Cannot parse gig date: {gig.date!r}")

        start_time = parse_start_time(gig.time)
        if not start_time:
            raise ValueError(f"Cannot parse gig time: {gig.time!r}")

        date = datetime.datetime.strptime(date_str, "%Y%m%d").date()
        start_dt = datetime.datetime.combine(date, start_time)
        end_dt = start_dt + datetime.timedelta(hours=1)

        description = "\n".join(
            [
                f"Fee:                   {gig.fee or '—'}",
                f"Contact:               {gig.contact or '—'}",
                f"Email:                 {gig.email or '—'}",
                f"Phone:                 {gig.phone or '—'}",
                f"Address:               {gig.address or '—'}",
                f"Musical requirements:  {gig.musical_requirements or '—'}",
                f"Link:                  {gig.link or '—'}",
            ]
        )

        event = {
            "summary": f"{gig.header} — {gig.organisation}",
            "location": gig.address or gig.locality or "",
            "description": description,
            "start": {"dateTime": start_dt.isoformat(), "timeZone": "Europe/London"},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": "Europe/London"},
        }

        t0 = time.perf_counter()
        try:
            created = (
                self._service.events().insert(calendarId=self.calendar_id, body=event).execute()
            )
        except Exception:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            logger.exception(
                "Failed to insert calendar event",
                extra={
                    "summary": event["summary"],
                    "calendar_id": self.calendar_id,
                    "elapsed_ms": elapsed_ms,
                },
            )
            raise
        event_id = created["id"]
        elapsed_ms = int((time.perf_counter() - t0) * 1000)

        logger.info(
            "Gig added to Google Calendar",
            extra={
                "event_id": event_id,
                "summary": event["summary"],
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat(),
                "calendar_id": self.calendar_id,
                "elapsed_ms": elapsed_ms,
            },
        )
        return event_id
