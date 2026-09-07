"""Trace a single gig URL through the OrganistBot pipeline without writing state.

Usage:
    python -m scripts.trace_gig "https://organistsonline.org/required/12345"

Mirrors the construction in main.py (same filters, same toggles, same runtime_config
and filter_store values), but iterates filters one-by-one so the output reports
exactly which filter rejected the gig — something the production logs only
aggregate in counts.

Writes nothing: no seen_gigs.csv, no applications.json, no Google Sheets,
no calendar events.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from organist_bot import filter_store
from organist_bot.config import settings
from organist_bot.filters import (
    AvailabilityFilter,
    BlacklistFilter,
    CalendarFilter,
    FeeFilter,
    PostcodeFilter,
    SeenFilter,
    SundayTimeFilter,
)
from organist_bot.integrations.calendar_client import GoogleCalendarClient
from organist_bot.models import Gig
from organist_bot.runtime_config_store import runtime_config
from organist_bot.scraper import Scraper
from organist_bot.storage import load_seen_gigs

LabeledFilter = tuple[str, Callable[[Gig], bool]]


def _build_pre_filter() -> list[LabeledFilter]:
    """Build the Phase-1 pre-filter — mirrors main.py:90-119."""
    filters: list[LabeledFilter] = []

    if settings.enable_seen_filter:
        filters.append(("SeenFilter", SeenFilter(load_seen_gigs())))

    if settings.enable_fee_filter:
        fee = FeeFilter(min_fee=runtime_config.get("min_fee", settings.min_fee))
        filters.append((repr(fee), fee))

    if settings.enable_sunday_time_filter:
        filters.append(("SundayTimeFilter", SundayTimeFilter()))

    if (
        settings.enable_calendar_filter
        and settings.google_calendar_id
        and settings.google_calendar_credentials_file
    ):
        cal = GoogleCalendarClient(
            credentials_file=settings.google_calendar_credentials_file,
            calendar_id=settings.google_calendar_id,
        )
        filters.append(("CalendarFilter", CalendarFilter(cal)))

    if settings.enable_availability_filter:
        unavail = filter_store.unavailable_periods()
        avail_only = filter_store.available_only_periods()
        if unavail:
            filters.append(("AvailabilityFilter(block)", AvailabilityFilter(unavail, mode="block")))
        if avail_only:
            filters.append(
                ("AvailabilityFilter(only)", AvailabilityFilter(avail_only, mode="only"))
            )

    return filters


def _build_full_filter() -> list[LabeledFilter]:
    """Build the Phase-2 full filter — mirrors main.py:188-227."""
    filters: list[LabeledFilter] = []

    if settings.enable_fee_filter:
        fee = FeeFilter(min_fee=runtime_config.get("min_fee", settings.min_fee))
        filters.append((repr(fee), fee))

    if settings.enable_sunday_time_filter:
        filters.append(("SundayTimeFilter", SundayTimeFilter()))

    if settings.enable_blacklist_filter:
        filters.append(("BlacklistFilter", BlacklistFilter(filter_store.blacklist_emails())))

    if settings.enable_availability_filter:
        unavail = filter_store.unavailable_periods()
        avail_only = filter_store.available_only_periods()
        if unavail:
            filters.append(("AvailabilityFilter(block)", AvailabilityFilter(unavail, mode="block")))
        if avail_only:
            filters.append(
                ("AvailabilityFilter(only)", AvailabilityFilter(avail_only, mode="only"))
            )

    if settings.enable_postcode_filter and settings.home_postcode and settings.google_maps_api_key:
        pf = PostcodeFilter(
            home_postcode=settings.home_postcode,
            api_key=settings.google_maps_api_key,
            max_minutes=runtime_config.get("max_travel_minutes", settings.max_travel_minutes),
        )
        filters.append((repr(pf), pf))

    return filters


def _run_chain(name: str, gig: Gig, chain: list[LabeledFilter]) -> bool:
    """Iterate filters with verbose per-filter output. Returns overall pass/fail."""
    print(f"[{name}]")
    if not chain:
        print("             (no filters active)")
        return True

    overall_pass = True
    for label, fn in chain:
        try:
            verdict = fn(gig)
        except Exception as exc:
            print(f"  {label:<40s} → ERROR ({type(exc).__name__}: {exc})")
            overall_pass = False
            continue

        marker = "pass  " if verdict else "REJECT"
        print(f"  {label:<40s} → {marker}")
        if not verdict and overall_pass:
            # The real pipeline short-circuits at first failure; mark overall failure
            # but keep going so the user sees every filter's verdict for full context.
            overall_pass = False
    return overall_pass


def _print_fields(label: str, fields: dict) -> None:
    print(f"[{label}]")
    for k, v in fields.items():
        if v is not None:
            print(f"  {k:<24s} {v}")


def trace(url: str) -> None:
    print(f"== Trace: {url} ==\n")

    scraper = Scraper()
    try:
        detail_html = scraper.fetch(url)
    except Exception as exc:
        print(f"FATAL: could not fetch URL — {type(exc).__name__}: {exc}")
        sys.exit(1)

    basic = scraper.extract_basic_from_detail(detail_html, url)
    _print_fields("basic", basic)
    print()

    try:
        gig = Gig(**basic)
    except Exception as exc:
        print(f"FATAL: detail page is not a parseable gig listing — {exc}")
        sys.exit(1)

    pre_chain = _build_pre_filter()
    pre_pass = _run_chain("pre-filter", gig, pre_chain)
    print()

    if not pre_pass:
        print(
            "verdict: REJECTED in pre-filter — would NOT have fetched the detail page in production"
        )
        return

    extra = scraper.extract_full_details(detail_html)
    enriched = Gig(**{**basic, **extra})
    _print_fields("detail", extra)
    print()

    full_chain = _build_full_filter()
    full_pass = _run_chain("full filter", enriched, full_chain)
    print()

    if full_pass:
        print("verdict: PASSED all filters — would have been notified and applied to")
    else:
        print("verdict: REJECTED in full filter — would NOT have been notified or applied to")


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: python -m scripts.trace_gig <gig-detail-url>", file=sys.stderr)
        sys.exit(2)
    trace(sys.argv[1])


if __name__ == "__main__":
    main()
