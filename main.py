import logging
import time
import uuid
import schedule
import requests as _requests

from organist_bot.integrations.calendar_client import GoogleCalendarClient
from organist_bot.config import settings
from organist_bot.filters import (
    BlacklistFilter, BookedDateFilter, CalendarFilter, FeeFilter,
    GigFilterChain, PostcodeFilter, SeenFilter, SundayTimeFilter,
)
from organist_bot.logging_config import setup_logging, set_run_id
from organist_bot.models import Gig
from organist_bot.notifier import Notifier, SMTPTransport
from organist_bot.scraper import Scraper
from organist_bot.storage import load_seen_gigs, save_seen_gigs

logger = logging.getLogger(__name__)


def _send_telegram_alert(message: str) -> None:
    """Post a plain-text alert to the configured Telegram chat.

    Called only on unhandled scheduler crashes — failure here must never
    propagate, so the scheduler loop can continue.
    """
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return
    try:
        _requests.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json={"chat_id": settings.telegram_chat_id, "text": message},
            timeout=10,
        )
    except Exception:
        logger.warning("Failed to send Telegram crash alert")


def main(scraper: Scraper) -> None:
    run_id = uuid.uuid4().hex[:8]
    set_run_id(run_id)

    logger.info(
        "OrganistBot run started",
        extra={
            "run_id":       run_id,
            "target_url":   settings.target_url,
            "min_fee":      settings.min_fee,
            "poll_minutes": settings.poll_minutes,
        },
    )
    run_start = time.perf_counter()
    gig_errors = 0

    # ── Phase 1: Scrape ───────────────────────────────────────────────────────
    logger.info("Phase 1 — scraping listings")
    t0 = time.perf_counter()
    gig_list: list[Gig] = []

    # Load seen gigs upfront so we can skip detail-page fetches for known gigs.
    # On a typical run (polling every 2 min) nearly every gig is already seen,
    # so this cuts Phase 1 from N+1 HTTP requests down to just 1.
    seen_gigs_set = load_seen_gigs() if settings.enable_seen_filter else set()

    # Pre-filter chain: filters that only need basic details (fee, date, time).
    # Applied before fetching the detail page so we skip the HTTP request for
    # gigs that would be rejected anyway.  Filters requiring detail-page fields
    # (email → BlacklistFilter, postcode → PostcodeFilter) stay in Phase 2.
    pre_filter = GigFilterChain()
    if settings.enable_fee_filter:
        pre_filter.add(FeeFilter(min_fee=settings.min_fee))
    if settings.enable_sunday_time_filter:
        pre_filter.add(SundayTimeFilter())
    if settings.enable_booked_date_filter:
        pre_filter.add(BookedDateFilter(settings.booked_dates))
    if settings.enable_calendar_filter and settings.google_calendar_id and settings.google_calendar_credentials_file:
        cal_client = GoogleCalendarClient(
            credentials_file=settings.google_calendar_credentials_file,
            calendar_id=settings.google_calendar_id,
        )
        pre_filter.add(CalendarFilter(cal_client))
    elif not settings.enable_calendar_filter:
        logger.info("CalendarFilter disabled")
    else:
        logger.info("CalendarFilter disabled — google_calendar_id or google_calendar_credentials_file not set")

    gigs_div: list         = []
    pre_filter_passed: int = 0

    response = scraper.fetch(settings.target_url)
    gigs_div = scraper.parse_gig_listings(response, "booking noselect")

    for gig_el in gigs_div:
        basic: dict = {}
        try:
            basic = scraper.extract_basic_details(gig_el)

            # Skip already-seen gigs.
            if settings.enable_seen_filter and basic.get("link") in seen_gigs_set:
                continue

            # Skip gigs that fail cheap filters — avoids the detail-page fetch.
            if not pre_filter.is_valid(Gig(**basic)):
                continue

            pre_filter_passed += 1

            # Passed all cheap filters — now fetch the detail page.
            extra = scraper.extract_full_details(scraper.fetch(basic["link"])) if basic.get("link") else {}
            gig_list.append(Gig(**{**basic, **extra}))
        except Exception:
            gig_errors += 1
            logger.exception(
                "Failed to build gig — skipping",
                extra={"link": basic.get("link")},
            )

    logger.info(
        "Scraping complete",
        extra={
            "listed":             len(gigs_div),
            "pre_filter_passed":  pre_filter_passed,
            "scraped":            len(gig_list),
            "gig_errors":         gig_errors,
            "elapsed_ms":         int((time.perf_counter() - t0) * 1000),
        },
    )

    # ── Phase 2: Filter ───────────────────────────────────────────────────────
    logger.info("Phase 2 — applying filters")
    t0 = time.perf_counter()

    filter_chain = GigFilterChain()

    if settings.enable_fee_filter:
        filter_chain.add(FeeFilter(min_fee=settings.min_fee))
    else:
        logger.info("FeeFilter disabled")

    if settings.enable_sunday_time_filter:
        filter_chain.add(SundayTimeFilter())
    else:
        logger.info("SundayTimeFilter disabled")

    if settings.enable_blacklist_filter:
        filter_chain.add(BlacklistFilter(settings.blacklist_emails))
    else:
        logger.info("BlacklistFilter disabled")

    if settings.enable_booked_date_filter:
        filter_chain.add(BookedDateFilter(settings.booked_dates))
    else:
        logger.info("BookedDateFilter disabled")

    if settings.enable_seen_filter:
        filter_chain.add(SeenFilter(seen_gigs_set))  # reuse set loaded in Phase 1
    else:
        logger.info("SeenFilter disabled")

    if settings.enable_postcode_filter and settings.home_postcode and settings.google_maps_api_key:
        filter_chain.add(PostcodeFilter(
            home_postcode=settings.home_postcode,
            api_key=settings.google_maps_api_key,
            max_minutes=settings.max_travel_minutes,
        ))
    elif not settings.enable_postcode_filter:
        logger.info("PostcodeFilter disabled")
    else:
        logger.info("PostcodeFilter disabled — home_postcode or google_maps_api_key not set")

    valid_gigs = filter_chain.apply(gig_list)

    logger.info(
        "Filtering complete",
        extra={
            "total_in":   len(gig_list),
            "valid":      len(valid_gigs),
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        },
    )

    # ── Phase 3: Notify ───────────────────────────────────────────────────────
    if valid_gigs:
        logger.info("Phase 3 — sending notifications", extra={"gig_count": len(valid_gigs)})
        t0 = time.perf_counter()

        transport = SMTPTransport(password=settings.email_password)
        notifier  = Notifier(settings, transport)
        notifier.send_summary(valid_gigs)
        for gig in valid_gigs:
            notifier.apply_to_gig(gig)

        logger.info(
            "Notifications sent",
            extra={
                "gig_count":  len(valid_gigs),
                "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            },
        )
        save_seen_gigs(seen=set(gig.link for gig in valid_gigs))
    else:
        logger.info("No new gigs passed the filters — notifications skipped")

    # ── Run summary ───────────────────────────────────────────────────────────
    logger.info(
        "Run summary",
        extra={
            "run_id":     run_id,
            "scraped":    len(gig_list),
            "valid":      len(valid_gigs),
            "notified":   len(valid_gigs),
            "gig_errors": gig_errors,
            "elapsed_ms": int((time.perf_counter() - run_start) * 1000),
        },
    )


if __name__ == "__main__":
    setup_logging(settings.log_file)
    logger.info(
        "Scheduler starting",
        extra={"poll_minutes": settings.poll_minutes},
    )

    scraper = Scraper()
    try:
        main(scraper)  # run immediately on startup, then on schedule
        schedule.every(settings.poll_minutes).minutes.do(main, scraper)

        while True:
            try:
                schedule.run_pending()
            except Exception:
                logger.exception("Unhandled exception in scheduled run")
                _send_telegram_alert("❌ OrganistBot crashed — check logs.")
            time.sleep(1)
    finally:
        scraper.session.close()
        logger.info("Scraper session closed — bot shutting down")
