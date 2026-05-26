"""
organist_bot/calendar_client.py
────────────────────────────────
Google Calendar integration for OrganistBot.

GoogleCalendarClient wraps the Google Calendar API with the following operations:

  has_event_on_date(date_str)
    Returns True if the calendar already has at least one event on the given
    date (YYYYMMDD).  Used by CalendarFilter to skip already-busy dates.
    Fails open — if the API call errors, returns False so the gig is not
    silently dropped.

  add_gig(gig)
    Creates a timed calendar event for a confirmed gig and returns the event ID.
    Used by add_booking.py after the user has manually secured a booking.

  block_period(period)
    Creates an all-day 'Unavailable' blocking event for the given period token
    (YYYY-MM-DD, YYYY-MM-DD:YYYY-MM-DD, or YYYY-MM). Idempotent: returns the
    existing event ID if a block already exists. Returns None on parse failure
    or API error.

  unblock_period(period)
    Deletes the blocking event for the given period token if one exists.
    Returns True on success, False if no block found or API error.

Authentication uses a Google service account JSON key file.  To set up:
  1. Create a service account in Google Cloud Console.
  2. Enable the Google Calendar API for the project.
  3. Share your calendar with the service account's email address.
  4. Download the JSON key and set GOOGLE_CALENDAR_CREDENTIALS_FILE in .env.
"""

import calendar as _cal_mod
import datetime
import logging
import re as _re
import time

from google.oauth2 import service_account
from googleapiclient.discovery import build

from organist_bot import alert
from organist_bot.filters import normalize_to_yyyymmdd, parse_start_time
from organist_bot.models import Gig

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/calendar"]


def _parse_period_dates(period: str) -> tuple[datetime.date, datetime.date] | None:
    """Parse a period token into an inclusive (start, end) date pair.

    Accepts: YYYY-MM-DD, YYYY-MM-DD:YYYY-MM-DD, YYYY-MM.
    Returns None on any parse failure.
    """
    try:
        if ":" in period:
            start_str, end_str = period.split(":", 1)
            return datetime.date.fromisoformat(start_str), datetime.date.fromisoformat(end_str)
        if _re.fullmatch(r"\d{4}-\d{2}", period):
            year, month = int(period[:4]), int(period[5:])
            last_day = _cal_mod.monthrange(year, month)[1]
            return datetime.date(year, month, 1), datetime.date(year, month, last_day)
        d = datetime.date.fromisoformat(period)
        return d, d
    except Exception:
        return None


class GoogleCalendarClient:
    """Thin wrapper around the Google Calendar v3 API."""

    def __init__(self, credentials_file: str, calendar_id: str):
        self.calendar_id = calendar_id
        creds = service_account.Credentials.from_service_account_file(
            credentials_file, scopes=_SCOPES
        )
        self._service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        logger.debug("GoogleCalendarClient initialised", extra={"calendar_id": calendar_id})

    def get_events_on_date(self, date_str: str) -> list[dict]:
        """Return events on the given date (YYYYMMDD) as [{id, summary}] dicts.

        Returns [] on any API error (fail-open — don't silently drop gigs).
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

            events = [
                {"id": item.get("id", ""), "summary": item.get("summary", "(No title)")}
                for item in result.get("items", [])
            ]
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            logger.debug(
                "Calendar check complete",
                extra={"date": date_str, "event_count": len(events), "elapsed_ms": elapsed_ms},
            )
            return events

        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            logger.warning(
                "Calendar check failed — failing open",
                extra={"date": date_str, "error": str(exc), "elapsed_ms": elapsed_ms},
            )
            alert.send_alert(f"⚠️ Google Calendar API error (CalendarFilter query): {exc}")
            return []

    def has_event_on_date(self, date_str: str) -> bool:
        """Return True if there is at least one event on the given date (YYYYMMDD).

        Fails open — returns False (don't block the gig) if the API call fails.
        """
        return bool(self.get_events_on_date(date_str))

    def list_upcoming_events(self, max_results: int = 10) -> list[dict]:
        """Return upcoming events from now, ordered by start time ascending.

        Each dict: {id, summary, start_dt (timezone-aware datetime), date_str (YYYY-MM-DD)}.
        Fails open — returns [] on any API error.
        """
        t0 = time.perf_counter()
        try:
            now = datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z")
            result = (
                self._service.events()
                .list(
                    calendarId=self.calendar_id,
                    timeMin=now,
                    maxResults=max_results,
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
            )
            events = []
            for item in result.get("items", []):
                start = item.get("start", {})
                if "dateTime" in start:
                    start_dt = datetime.datetime.fromisoformat(
                        start["dateTime"].replace("Z", "+00:00")
                    )
                    date_str = start_dt.date().isoformat()
                else:
                    date_str = start.get("date", "")
                    year, month, day = (int(p) for p in date_str.split("-"))
                    start_dt = datetime.datetime(year, month, day, tzinfo=datetime.UTC)
                event_id = item.get("id")
                if not event_id:
                    continue
                events.append(
                    {
                        "id": event_id,
                        "summary": item.get("summary", "(No title)"),
                        "start_dt": start_dt,
                        "date_str": date_str,
                    }
                )
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            logger.debug(
                "list_upcoming_events complete",
                extra={"count": len(events), "elapsed_ms": elapsed_ms},
            )
            return events
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            logger.warning(
                "list_upcoming_events failed — returning []",
                extra={"error": str(exc), "elapsed_ms": elapsed_ms},
            )
            return []

    def delete_event(self, event_id: str) -> None:
        """Delete a calendar event by ID. Raises on failure."""
        t0 = time.perf_counter()
        try:
            self._service.events().delete(calendarId=self.calendar_id, eventId=event_id).execute()
        except Exception:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            logger.exception(
                "Failed to delete calendar event",
                extra={
                    "event_id": event_id,
                    "calendar_id": self.calendar_id,
                    "elapsed_ms": elapsed_ms,
                },
            )
            raise
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        logger.info(
            "Calendar event deleted",
            extra={"event_id": event_id, "calendar_id": self.calendar_id, "elapsed_ms": elapsed_ms},
        )

    def update_event(
        self,
        event_id: str,
        *,
        summary: str | None = None,
        start_dt: datetime.datetime | None = None,
    ) -> None:
        """Patch a calendar event. Only provided fields are changed.

        Raises on API failure.
        """
        body: dict = {}
        if summary is not None:
            body["summary"] = summary
        if start_dt is not None:
            end_dt = start_dt + datetime.timedelta(hours=1)
            body["start"] = {"dateTime": start_dt.isoformat(), "timeZone": "Europe/London"}
            body["end"] = {"dateTime": end_dt.isoformat(), "timeZone": "Europe/London"}
        if not body:
            return
        t0 = time.perf_counter()
        try:
            self._service.events().patch(
                calendarId=self.calendar_id, eventId=event_id, body=body
            ).execute()
        except Exception:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            logger.exception(
                "Failed to update calendar event",
                extra={
                    "event_id": event_id,
                    "calendar_id": self.calendar_id,
                    "elapsed_ms": elapsed_ms,
                },
            )
            raise
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        logger.info(
            "Calendar event updated",
            extra={
                "event_id": event_id,
                "calendar_id": self.calendar_id,
                "elapsed_ms": elapsed_ms,
            },
        )

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

    def block_period(self, period: str) -> str | None:
        """Create an all-day 'Unavailable' blocking event for the given period token.

        Idempotent: returns the existing event ID if a block already exists.
        Returns None on parse failure or API error.
        """
        dates = _parse_period_dates(period)
        if dates is None:
            logger.warning("block_period: cannot parse period %r — skipping", period)
            return None
        start, end = dates
        end_exclusive = end + datetime.timedelta(days=1)
        time_min = datetime.datetime.combine(start, datetime.time.min).isoformat() + "Z"
        time_max = datetime.datetime.combine(end_exclusive, datetime.time.min).isoformat() + "Z"
        t0 = time.perf_counter()
        try:
            existing = (
                self._service.events()
                .list(
                    calendarId=self.calendar_id,
                    timeMin=time_min,
                    timeMax=time_max,
                    privateExtendedProperty="organist_bot_block=1",
                    singleEvents=True,
                )
                .execute()
            )
            if existing.get("items"):
                return existing["items"][0]["id"]
            event = {
                "summary": "Unavailable",
                "start": {"date": start.isoformat()},
                "end": {"date": end_exclusive.isoformat()},
                "extendedProperties": {"private": {"organist_bot_block": "1"}},
            }
            created = (
                self._service.events().insert(calendarId=self.calendar_id, body=event).execute()
            )
            event_id = created["id"]
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            logger.info(
                "Calendar block created",
                extra={"period": period, "event_id": event_id, "elapsed_ms": elapsed_ms},
            )
            return event_id
        except Exception:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            logger.warning(
                "block_period: failed for %r",
                period,
                extra={"elapsed_ms": elapsed_ms},
                exc_info=True,
            )
            return None

    def unblock_period(self, period: str) -> bool:
        """Delete all calendar blocking events for the given period token.

        Returns True if any blocks were deleted. Returns False on parse failure,
        no blocks found, or API error.
        """
        dates = _parse_period_dates(period)
        if dates is None:
            logger.warning("unblock_period: cannot parse period %r — skipping", period)
            return False
        start, end = dates
        end_exclusive = end + datetime.timedelta(days=1)
        time_min = datetime.datetime.combine(start, datetime.time.min).isoformat() + "Z"
        time_max = datetime.datetime.combine(end_exclusive, datetime.time.min).isoformat() + "Z"
        t0 = time.perf_counter()
        try:
            result = (
                self._service.events()
                .list(
                    calendarId=self.calendar_id,
                    timeMin=time_min,
                    timeMax=time_max,
                    privateExtendedProperty="organist_bot_block=1",
                    singleEvents=True,
                )
                .execute()
            )
            events = result.get("items", [])
            for ev in events:
                self._service.events().delete(
                    calendarId=self.calendar_id, eventId=ev["id"]
                ).execute()
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            if events:
                logger.info(
                    "Calendar blocks removed",
                    extra={"period": period, "count": len(events), "elapsed_ms": elapsed_ms},
                )
            else:
                logger.debug(
                    "unblock_period: no blocks found",
                    extra={"period": period, "elapsed_ms": elapsed_ms},
                )
            return bool(events)
        except Exception:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            logger.warning(
                "unblock_period: failed for %r",
                period,
                extra={"elapsed_ms": elapsed_ms},
                exc_info=True,
            )
            return False
