# organist_bot/parsing.py
"""Pure parsing helpers for fee, date, and time strings.

These functions have no organist_bot dependencies so they can be safely
imported by both models.py and filters.py without creating circular imports.
"""

import datetime
import logging
import re

logger = logging.getLogger(__name__)


def parse_min_fee(fee_str: str | None) -> float | None:
    """Extract the minimum numeric fee from a fee string.

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
    """Extract the start time from a time string, returning a datetime.time.

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
    """Try to determine the weekday from a date string.

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
    """Attempt to parse a human-readable date string into YYYYMMDD format.

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
