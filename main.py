import logging
import time
import uuid

import requests as _requests
import schedule

from organist_bot.config import settings
from organist_bot.filters import (
    BlacklistFilter,
    BookedDateFilter,
    CalendarFilter,
    FeeFilter,
    GigFilterChain,
    PostcodeFilter,
    SeenFilter,
    SundayTimeFilter,
)
from organist_bot.integrations.calendar_client import GoogleCalendarClient
from organist_bot.integrations.sheets_logger import SheetsLogger
from organist_bot.logging_config import set_run_id, setup_logging
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
    t0 = time.perf_counter()
    try:
        _requests.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json={"chat_id": settings.telegram_chat_id, "text": message},
            timeout=10,
        )
        logger.info(
            "Telegram alert sent",
            extra={"elapsed_ms": int((time.perf_counter() - t0) * 1000)},
        )
    except Exception:
        logger.warning(
            "Telegram alert failed",
            extra={"elapsed_ms": int((time.perf_counter() - t0) * 1000)},
        )


def main(scraper: Scraper, sheets_logger: SheetsLogger | None = None) -> None:
    run_id = uuid.uuid4().hex[:8]
    set_run_id(run_id)

    logger.info(
        "OrganistBot run started",
        extra={
            "run_id": run_id,
            "target_url": settings.target_url,
            "min_fee": settings.min_fee,
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

    # Pre-filter chain: filters that only need basic details (fee, date, time, link).
    # SeenFilter (link in seen_gigs.csv) and CalendarFilter (date in Google Calendar)
    # are both included here so we skip the detail-page HTTP fetch for gigs that
    # would be rejected anyway.  Filters requiring detail-page fields
    # (email → BlacklistFilter, postcode → PostcodeFilter) stay in Phase 2.
    pre_filter = GigFilterChain()
    if settings.enable_seen_filter:
        pre_filter.add(SeenFilter(seen_gigs_set))
    if settings.enable_fee_filter:
        pre_filter.add(FeeFilter(min_fee=settings.min_fee))
    if settings.enable_sunday_time_filter:
        pre_filter.add(SundayTimeFilter())
    if settings.enable_booked_date_filter:
        pre_filter.add(BookedDateFilter(settings.booked_dates))
    if (
        settings.enable_calendar_filter
        and settings.google_calendar_id
        and settings.google_calendar_credentials_file
    ):
        cal_client = GoogleCalendarClient(
            credentials_file=settings.google_calendar_credentials_file,
            calendar_id=settings.google_calendar_id,
        )
        pre_filter.add(CalendarFilter(cal_client))
    elif not settings.enable_calendar_filter:
        logger.info("CalendarFilter disabled")
    else:
        logger.info(
            "CalendarFilter disabled — google_calendar_id or google_calendar_credentials_file not set"
        )

    gigs_div: list = []
    pre_filter_passed: int = 0

    response = scraper.fetch(settings.target_url)
    gigs_div = scraper.parse_gig_listings(response, "booking noselect")

    for gig_el in gigs_div:
        basic: dict = {}
        try:
            basic = scraper.extract_basic_details(gig_el)

            # Skip gigs that fail cheap filters (incl. seen + calendar) — avoids the detail-page fetch.
            if not pre_filter.is_valid(Gig(**basic)):
                continue

            pre_filter_passed += 1

            # Passed all cheap filters — now fetch the detail page.
            extra = (
                scraper.extract_full_details(scraper.fetch(basic["link"]))
                if basic.get("link")
                else {}
            )
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
            "listed": len(gigs_div),
            "pre_filter_passed": pre_filter_passed,
            "scraped": len(gig_list),
            "gig_errors": gig_errors,
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
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

    if settings.enable_postcode_filter and settings.home_postcode and settings.google_maps_api_key:
        filter_chain.add(
            PostcodeFilter(
                home_postcode=settings.home_postcode,
                api_key=settings.google_maps_api_key,
                max_minutes=settings.max_travel_minutes,
            )
        )
    elif not settings.enable_postcode_filter:
        logger.info("PostcodeFilter disabled")
    else:
        logger.info("PostcodeFilter disabled — home_postcode or google_maps_api_key not set")

    valid_gigs = filter_chain.apply(gig_list)

    logger.info(
        "Filtering complete",
        extra={
            "total_in": len(gig_list),
            "valid": len(valid_gigs),
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        },
    )

    for gig in valid_gigs:
        logger.info(
            "Gig passed all filters",
            extra={
                "header": gig.header,
                "date": gig.date,
                "fee": gig.fee,
                "organisation": gig.organisation or "",
                "postcode": gig.postcode or "",
                "contact_email": gig.email or "",
                "link": gig.link,
            },
        )

    # ── Phase 3: Notify ───────────────────────────────────────────────────────
    if valid_gigs:
        logger.info("Phase 3 — sending notifications", extra={"gig_count": len(valid_gigs)})
        t0 = time.perf_counter()

        transport = SMTPTransport(password=settings.email_password)
        notifier = Notifier(settings, transport)
        notifier.send_summary(valid_gigs)
        for gig in valid_gigs:
            notifier.apply_to_gig(gig)

        logger.info(
            "Notifications sent",
            extra={
                "gig_count": len(valid_gigs),
                "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            },
        )
        save_seen_gigs(seen=seen_gigs_set | set(gig.link for gig in valid_gigs))
    else:
        logger.info("No new gigs passed the filters — notifications skipped")

    # ── Run summary ───────────────────────────────────────────────────────────
    logger.info(
        "Run summary",
        extra={
            "run_id": run_id,
            "scraped": len(gig_list),
            "valid": len(valid_gigs),
            "notified": len(valid_gigs),
            "gig_errors": gig_errors,
            "elapsed_ms": int((time.perf_counter() - run_start) * 1000),
        },
    )

    # ── Flush logs to Google Sheets ───────────────────────────────────────────
    if sheets_logger is not None:
        try:
            rows = sheets_logger.flush(settings.log_file)
            logger.info("Sheets flush complete", extra={"rows_written": rows})
        except Exception:
            logger.warning(
                "Sheets flush failed — logs retained in file",
                extra={"log_file": settings.log_file},
            )


if __name__ == "__main__":
    setup_logging(settings.log_file)
    logger.info(
        "Scheduler starting",
        extra={"poll_minutes": settings.poll_minutes},
    )

    # ── Google Sheets logger (optional) ───────────────────────────────────────
    sheets_logger: SheetsLogger | None = None
    if settings.google_sheets_id:
        creds_file = (
            settings.google_sheets_credentials_file or settings.google_calendar_credentials_file
        )
        if creds_file:
            try:
                sheets_logger = SheetsLogger(
                    spreadsheet_id=settings.google_sheets_id,
                    credentials_file=creds_file,
                )
            except Exception:
                logger.warning("SheetsLogger init failed — Sheets logging disabled")
        else:
            logger.info(
                "SheetsLogger disabled — no credentials file configured "
                "(set GOOGLE_SHEETS_CREDENTIALS_FILE or GOOGLE_CALENDAR_CREDENTIALS_FILE)"
            )

    scraper = Scraper()
    try:
        main(scraper, sheets_logger)  # run immediately on startup, then on schedule
        schedule.every(settings.poll_minutes).minutes.do(main, scraper, sheets_logger)

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
