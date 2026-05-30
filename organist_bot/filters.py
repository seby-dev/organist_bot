import calendar as _calendar
import datetime
import logging
import re
import time
from collections.abc import Callable
from typing import Any

import googlemaps

from organist_bot import alert
from organist_bot.models import Gig

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Parsing helpers (pure functions, no state)
# ──────────────────────────────────────────────


def parse_min_fee(fee_str: str | None) -> float | None:
    """
    Extract the minimum numeric fee from a fee string.
    Examples:
    - "£80 - £120" -> 80.0
    - "£100+" -> 100.0
    - "From £90" -> 90.0
    - "£120" -> 120.0
    Returns None if no valid amount found or if marked negotiable.
    """
    if not fee_str:
        return None

    s = fee_str.strip()
    if re.search(r"neg|negotiable|expenses", s, re.IGNORECASE):
        return None

    amounts = re.findall(r"£?\s*([0-9]+(?:\.[0-9]{1,2})?)", s)
    if not amounts:
        return None

    try:
        numbers = [float(a) for a in amounts]
        return min(numbers) if numbers else None
    except ValueError:
        return None


def parse_start_time(time_str: str) -> datetime.time | None:
    """
    Extract the start time from a time string, returning a datetime.time.
    Accepts formats like "9:00 AM", "9am", "09:30 am".
    Trims trailing timezone text like GMT/BST.
    Returns None if it cannot be parsed.
    """
    if not time_str:
        return None

    base = re.split(r"\b(GMT|BST)\b", time_str, flags=re.IGNORECASE)[0].strip()

    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap]m)\b", base, re.IGNORECASE)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2)) if m.group(2) else 0
        ampm = m.group(3).lower()
        if hour == 12:
            hour = 0
        if ampm == "pm":
            hour += 12
        try:
            return datetime.time(hour, minute)
        except ValueError:
            return None

    for fmt in ("%I:%M %p", "%I %p"):
        try:
            return datetime.datetime.strptime(base, fmt).time()
        except (ValueError, AttributeError):
            continue

    logger.debug("parse_start_time: no format matched", extra={"input": time_str})
    return None


def parse_weekday(date_str: str) -> int | None:
    """
    Try to determine the weekday from a date string.
    Returns an integer Monday=0 ... Sunday=6, or None if unknown.
    """
    if not date_str:
        return None

    s = date_str.strip()
    s = re.sub(r"\b(\d{1,2})(st|nd|rd|th)\b", r"\1", s, flags=re.IGNORECASE)

    fmts = [
        "%A, %B %d, %Y",
        "%A %B %d, %Y",
        "%A, %d %B %Y",
        "%A %d %B %Y",
        "%a, %b %d, %Y",
        "%a %b %d, %Y",
        "%a, %d %b %Y",
        "%a %d %b %Y",
        "%A %d %B, %Y",
        "%A, %B %d %Y",
    ]
    for fmt in fmts:
        try:
            return datetime.datetime.strptime(s, fmt).weekday()
        except (ValueError, AttributeError):
            pass

    weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    m = re.search(r"\b(" + "|".join(weekdays) + r")\b", s, re.IGNORECASE)
    if m:
        return weekdays.index(m.group(1).lower())

    return None


def normalize_to_yyyymmdd(date_str: str) -> str | None:
    """
    Attempt to parse a human-readable date string into YYYYMMDD format.
    Returns the formatted string or None if parsing fails.
    """
    if not date_str:
        return None

    s = date_str.strip()
    s2 = re.sub(r"\b(\d{1,2})(st|nd|rd|th)\b", r"\1", s, flags=re.IGNORECASE)

    fmts = [
        "%A, %B %d, %Y",
        "%A %B %d, %Y",
        "%A, %d %B %Y",
        "%A %d %B %Y",
        "%a, %b %d, %Y",
        "%a %b %d, %Y",
        "%a, %d %b %Y",
        "%a %d %b %Y",
        "%d %B %Y",
        "%d %b %Y",
        "%B %d, %Y",
        "%b %d, %Y",
        "%Y-%m-%d",
    ]
    for fmt in fmts:
        try:
            dt = datetime.datetime.strptime(s2, fmt).date()
            return dt.strftime("%Y%m%d")
        except (ValueError, AttributeError):
            pass

    fmts_no_year = ["%d %B", "%d %b", "%B %d", "%b %d"]
    today = datetime.date.today()
    for fmt in fmts_no_year:
        try:
            partial = datetime.datetime.strptime(s2, fmt)
            dt = datetime.date(today.year, partial.month, partial.day)
            if dt < today:
                dt = datetime.date(today.year + 1, partial.month, partial.day)
            return dt.strftime("%Y%m%d")
        except (ValueError, AttributeError):
            pass

    logger.debug("normalize_to_yyyymmdd: no format matched", extra={"input": date_str})
    return None


# ──────────────────────────────────────────────
# Individual filter callables
# ──────────────────────────────────────────────


class FeeFilter:
    """Reject gigs whose minimum fee is below the required threshold.

    Weekday gigs (Mon-Fri) enforce a separate weekday_min_fee (default 120).
    Weekend/unknown-day gigs use the provided min_fee.
    """

    def __init__(self, min_fee: float, weekday_min_fee: float = 100):
        self.min_fee = min_fee
        self.weekday_min_fee = weekday_min_fee

    def __call__(self, gig: Gig) -> bool:
        weekday = parse_weekday(gig.date)
        is_weekday = weekday in (0, 1, 2, 3, 4)
        required = self.weekday_min_fee if is_weekday else self.min_fee

        amount = parse_min_fee(gig.fee)
        return amount is not None and amount >= required

    def __repr__(self):
        return f"FeeFilter(min_fee={self.min_fee}, weekday_min_fee={self.weekday_min_fee})"


class SundayTimeFilter:
    """For Sunday gigs, enforce that the start time falls within a time window.

    Non-Sunday gigs always pass this filter.
    """

    def __init__(
        self,
        earliest: datetime.time = datetime.time(9, 0),
        latest: datetime.time = datetime.time(10, 0),
    ):
        self.earliest = earliest
        self.latest = latest

    def __call__(self, gig: Gig) -> bool:
        weekday = parse_weekday(gig.date)
        if weekday != 6:  # Not Sunday
            return True

        start_time = parse_start_time(gig.time)
        if start_time is None:
            return False

        return self.earliest <= start_time <= self.latest

    def __repr__(self):
        return f"SundayTimeFilter(earliest={self.earliest}, latest={self.latest})"


class BlacklistFilter:
    """Reject gigs whose contact email is in a blacklist."""

    def __init__(self, blacklist_emails: list[str]):
        self.blacklist_emails = {e.lower().strip() for e in blacklist_emails}

    def __call__(self, gig: Gig) -> bool:
        email = gig.email
        if not email:
            return True  # No email to check — allow through
        return email.lower().strip() not in self.blacklist_emails

    def __repr__(self):
        return f"BlacklistFilter(count={len(self.blacklist_emails)})"


class BookedDateFilter:
    """Reject gigs that fall on already-booked dates (YYYYMMDD strings)."""

    def __init__(self, booked_dates: list[str]):
        self.booked_dates = set(booked_dates)

    def __call__(self, gig: Gig) -> bool:
        normalized = normalize_to_yyyymmdd(gig.date)
        if normalized is None:
            return True  # Can't determine date — allow through
        return normalized not in self.booked_dates

    def __repr__(self):
        return f"BookedDateFilter(count={len(self.booked_dates)})"


class PostcodeFilter:
    """Reject gigs that are too far away from a home postcode.

    Queries the Google Maps Distance Matrix API for three travel modes —
    transit (train/bus), bicycling, and walking — between the user's home
    postcode and the gig's postcode.  The gig passes if ANY mode arrives
    within max_minutes.

    Fail-open behaviour:
      - Gig has no postcode → pass (can't judge distance).
      - API call fails or returns a non-OK status for a mode → treat that
        mode as unreachable (None), but still check the other modes.
      - All modes return None → pass (rather than silently drop a gig).

    Results are cached in-memory per filter instance so the same postcode
    is never queried more than once per bot run.

    Args:
        home_postcode:  Origin postcode, e.g. "SW1A 1AA".
        api_key:        Google Maps Distance Matrix API key.
        max_minutes:    Maximum acceptable one-way travel time (default 45).
        _client:        Optional pre-built googlemaps.Client for testing.
    """

    MODES: tuple[str, ...] = ("transit", "bicycling", "walking")

    def __init__(
        self,
        home_postcode: str,
        api_key: str,
        max_minutes: int = 45,
        _client=None,  # injectable for testing — pass a mock here
    ):
        self.home_postcode = home_postcode
        self.max_minutes = max_minutes
        self._client = _client or googlemaps.Client(key=api_key)
        self._cache: dict[str, dict[str, int | None]] = {}

    def __call__(self, gig: Gig) -> bool:
        weekday = parse_weekday(gig.date)
        if weekday != 6:  # Not Sunday — distance irrelevant
            return True

        if not gig.postcode:
            logger.debug(
                "PostcodeFilter: no postcode — passing through",
                extra={"header": gig.header},
            )
            return True

        times = self._travel_times(gig.postcode)

        # If every mode failed (API error or no-route) we have no data to
        # judge distance, so fail open rather than silently drop the gig.
        if all(t is None for t in times.values()):
            logger.debug(
                "PostcodeFilter: all modes returned None — failing open",
                extra={"header": gig.header, "postcode": gig.postcode},
            )
            return True

        passed = any(t is not None and t <= self.max_minutes for t in times.values())

        if not passed:
            logger.info(
                "PostcodeFilter: gig rejected — too far",
                extra={
                    "header": gig.header,
                    "organisation": gig.organisation,
                    "date": gig.date,
                    "fee": gig.fee,
                    "locality": gig.locality,
                    "postcode": gig.postcode,
                    "times_min": times,
                    "max_minutes": self.max_minutes,
                },
            )
        else:
            logger.debug(
                "PostcodeFilter: gig passed",
                extra={
                    "header": gig.header,
                    "postcode": gig.postcode,
                    "times_min": times,
                },
            )
        return passed

    def _travel_times(self, postcode: str) -> dict[str, int | None]:
        """Return cached travel times (minutes) for each mode, querying if needed."""
        if postcode not in self._cache:
            times: dict[str, int | None] = {}
            alerted = False
            for mode in self.MODES:
                result, exc = self._query(postcode, mode)
                if exc is not None and not alerted:
                    alert.send_alert(f"⚠️ Google Maps API error (PostcodeFilter): {exc}")
                    alerted = True
                times[mode] = result
            self._cache[postcode] = times
        return self._cache[postcode]

    def _query(self, postcode: str, mode: str) -> tuple[int | None, Exception | None]:
        """Call the Distance Matrix API for a single mode.

        Returns (minutes, None) on success or non-OK status, or (None, exc) on exception.
        """
        t0 = time.perf_counter()
        try:
            result = self._client.distance_matrix(
                origins=[postcode],
                destinations=[self.home_postcode],
                mode=mode,
                units="metric",
            )
            element = result["rows"][0]["elements"][0]
            elapsed_ms = int((time.perf_counter() - t0) * 1000)

            if element["status"] != "OK":
                logger.debug(
                    "Distance Matrix non-OK status",
                    extra={
                        "postcode": postcode,
                        "mode": mode,
                        "status": element["status"],
                        "elapsed_ms": elapsed_ms,
                    },
                )
                return None, None

            minutes = element["duration"]["value"] // 60  # seconds → whole minutes
            logger.debug(
                "Distance Matrix OK",
                extra={
                    "postcode": postcode,
                    "mode": mode,
                    "minutes": minutes,
                    "elapsed_ms": elapsed_ms,
                },
            )
            return minutes, None

        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            logger.warning(
                "Distance Matrix query failed — failing open",
                extra={
                    "postcode": postcode,
                    "mode": mode,
                    "error": str(exc),
                    "elapsed_ms": elapsed_ms,
                },
            )
            return None, exc

    def __repr__(self):
        return f"PostcodeFilter(home={self.home_postcode!r}, max_minutes={self.max_minutes})"


class SeenFilter:
    """Reject gigs that have already been seen by the bot."""

    def __init__(self, seen_gigs: set[str]):
        self.seen_gigs = seen_gigs

    def __call__(self, gig: Gig) -> bool:
        # A gig with no link can't be deduplicated, detail-fetched, or applied
        # to, and is never recorded in seen_gigs.csv — so it would be re-notified
        # on every tick. Reject linkless gigs outright.
        if not gig.link:
            return False
        return gig.link not in self.seen_gigs

    def __repr__(self):
        return f"SeenFilter(count={len(self.seen_gigs)})"


class CalendarFilter:
    """Reject gigs whose date already has an event in Google Calendar.

    Distinguishes between 'Unavailable' blocking events (silent reject) and
    real confirmed gigs (reject + Telegram alert with competing gig details).

    Fails open in two cases:
      - The gig date cannot be parsed  → pass (can't judge)
      - The calendar API call fails    → pass (don't silently drop gigs)

    Args:
        client: A GoogleCalendarClient instance.
    """

    def __init__(self, client):
        self._client = client

    def __call__(self, gig: Gig) -> bool:
        normalized = normalize_to_yyyymmdd(gig.date)
        if normalized is None:
            return True  # Can't determine date — allow through

        events = self._client.get_events_on_date(normalized)
        if not events:
            return True

        competing = [e for e in events if e.get("summary", "").strip() != "Unavailable"]
        if competing:
            self._alert_competing(gig, competing)
        else:
            logger.debug(
                "CalendarFilter: date already busy — rejecting",
                extra={"header": gig.header, "date": gig.date},
            )
        return False

    def _alert_competing(self, gig: Gig, competing: list[dict]) -> None:
        org_part = f" — {gig.organisation}" if gig.organisation else ""
        fee_part = f"\nFee:      {gig.fee}" if gig.fee else ""
        conflicts = "\n".join(f"  • {e.get('summary', '(untitled)')}" for e in competing)
        msg = (
            "⚠️ Competing gig — date already booked\n\n"
            f"New gig:  {gig.header}{org_part}\n"
            f"Date:     {gig.date}"
            f"{fee_part}\n"
            f"URL:      {gig.link}\n\n"
            f"Conflicts with:\n{conflicts}"
        )
        logger.info(
            "CalendarFilter: competing gig detected — alerting",
            extra={
                "header": gig.header,
                "date": gig.date,
                "competing": [e.get("summary", "(untitled)") for e in competing],
            },
        )
        alert.send_alert(msg)

    def __repr__(self):
        return "CalendarFilter()"


# ──────────────────────────────────────────────
# Availability period helpers
# ──────────────────────────────────────────────


def _parse_periods(
    raw: list[str],
) -> list[tuple[datetime.date, datetime.date]]:
    """Parse period tokens into inclusive (start, end) date tuples.

    Accepted formats:
      "2026-12-25"            – single day (use a list of these for specific days)
      "2026-12-15:2027-01-05" – inclusive date range
      "2026-12"               – full calendar month
    Invalid tokens are skipped with a warning.
    """
    parsed = []
    for token in raw:
        token = token.strip()
        try:
            if ":" in token:
                # Date range
                start_str, end_str = token.split(":", 1)
                start = datetime.date.fromisoformat(start_str.strip())
                end = datetime.date.fromisoformat(end_str.strip())
                parsed.append((start, end))
            elif re.fullmatch(r"\d{4}-\d{2}", token):
                # Year-month (e.g. "2026-12")
                year, month = int(token[:4]), int(token[5:])
                last_day = _calendar.monthrange(year, month)[1]
                parsed.append(
                    (
                        datetime.date(year, month, 1),
                        datetime.date(year, month, last_day),
                    )
                )
            else:
                # Specific date (e.g. "2026-12-25")
                d = datetime.date.fromisoformat(token)
                parsed.append((d, d))
        except (ValueError, AttributeError):
            logger.warning("AvailabilityFilter: could not parse period %r — skipping", token)
    return parsed


def _date_in_periods(
    d: datetime.date,
    periods: list[tuple[datetime.date, datetime.date]],
) -> bool:
    """Return True if `d` falls within any of the inclusive (start, end) ranges."""
    return any(start <= d <= end for start, end in periods)


class AvailabilityFilter:
    """Reject gigs based on personal availability periods.

    mode="block"  – reject if gig date falls IN any period (not available).
    mode="only"   – reject if gig date falls OUTSIDE all periods (only available then).

    Fails open: if the gig date cannot be parsed, the gig is allowed through.
    If periods is empty, the filter is a no-op (all gigs pass).
    """

    def __init__(self, periods: list[str], mode: str) -> None:
        if mode not in ("block", "only"):
            raise ValueError(f"AvailabilityFilter mode must be 'block' or 'only', got {mode!r}")
        self.mode = mode
        self._raw = periods
        self._periods = _parse_periods(periods)

    def __call__(self, gig: Gig) -> bool:
        if not self._periods:
            return True  # no periods configured — no-op

        normalized = normalize_to_yyyymmdd(gig.date)
        if normalized is None:
            return True  # fail-open

        try:
            d = datetime.datetime.strptime(normalized, "%Y%m%d").date()
        except ValueError:
            return True  # fail-open

        hit = _date_in_periods(d, self._periods)
        if self.mode == "block":
            if hit:
                logger.debug(
                    "AvailabilityFilter(block): date in unavailable period — rejecting",
                    extra={"header": gig.header, "date": gig.date},
                )
            return not hit
        else:  # only
            if not hit:
                logger.debug(
                    "AvailabilityFilter(only): date outside available periods — rejecting",
                    extra={"header": gig.header, "date": gig.date},
                )
            return hit

    def __repr__(self) -> str:
        return f"AvailabilityFilter(mode={self.mode!r}, periods={len(self._periods)})"


# ──────────────────────────────────────────────
# Composable filter chain
# ──────────────────────────────────────────────


class GigFilterChain:
    """Composable chain of gig filters.

    Each filter is a callable(dict) -> bool.
    A gig passes only if ALL filters return True.

    Usage:
        chain = (
            GigFilterChain()
            .add(FeeFilter(min_fee=50))
            .add(SundayTimeFilter())
            .add(BlacklistFilter(['bad@example.com']))
            .add(BookedDateFilter(['20260315']))
        )

        valid_gigs = chain.apply(gig_list)
    """

    def __init__(self):
        self._filters: list[Callable[[Any], bool]] = []
        # Cumulative rejection counts accumulated by is_valid() calls.
        # Keyed by repr(filter); reset by log_and_reset_counts().
        self._rejection_counts: dict[str, int] = {}

    def add(self, filter_fn: Callable[[Any], bool]) -> "GigFilterChain":
        """Add a filter to the chain. Returns self for fluent chaining."""
        self._filters.append(filter_fn)
        self._rejection_counts.setdefault(repr(filter_fn), 0)
        return self

    def is_valid(self, gig: Gig) -> bool:
        """Check if a single gig passes all filters.

        Stops at the first failing filter, increments its rejection counter,
        and logs the reason at DEBUG level.
        """
        for f in self._filters:
            if not f(gig):
                key = repr(f)
                self._rejection_counts[key] = self._rejection_counts.get(key, 0) + 1
                logger.debug(
                    "Gig rejected",
                    extra={
                        "filter": repr(f),
                        "header": gig.header,
                        "date": gig.date,
                        "fee": gig.fee,
                        "org": gig.organisation,
                    },
                )
                return False
        return True

    def log_and_reset_counts(self, total_in: int, passed: int) -> None:
        """Emit a 'Filter chain applied' log for accumulated is_valid() rejections.

        Logs the same structured event as apply() so the dashboard aggregates
        Phase-1 pre-filter rejections alongside Phase-2 rejections.
        Resets the counters afterwards so the next run starts clean.

        Args:
            total_in: Total number of gigs tested via is_valid() since last reset.
            passed:   Number that passed (i.e. is_valid() returned True).
        """
        counts = dict(self._rejection_counts)
        self._rejection_counts = {repr(f): 0 for f in self._filters}
        logger.info(
            "Filter chain applied",
            extra={
                "total_in": total_in,
                "passed": passed,
                "rejected": total_in - passed,
                "filters": [repr(f) for f in self._filters],
                "filter_breakdown": counts,
            },
        )

    def apply(self, gigs: list[Gig]) -> list[Gig]:
        """Return only the gigs that pass all filters."""
        rejection_counts: dict[str, int] = {repr(f): 0 for f in self._filters}
        valid: list[Gig] = []

        for gig in gigs:
            passed = True
            for f in self._filters:
                if not f(gig):
                    rejection_counts[repr(f)] += 1
                    passed = False
                    break  # short-circuit: first failing filter wins
            if passed:
                valid.append(gig)

        logger.info(
            "Filter chain applied",
            extra={
                "total_in": len(gigs),
                "passed": len(valid),
                "rejected": len(gigs) - len(valid),
                "filters": [repr(f) for f in self._filters],
                "filter_breakdown": rejection_counts,
            },
        )
        return valid

    def __repr__(self):
        names = [repr(f) for f in self._filters]
        return f"GigFilterChain([{', '.join(names)}])"
