import argparse
import fcntl
import hashlib
import logging
import time
import uuid

import schedule

import organist_bot.alert as alert
import organist_bot.application_store as application_store
import organist_bot.filter_store as filter_store
from organist_bot.config import settings
from organist_bot.filters import (
    AvailabilityFilter,
    BlacklistFilter,
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
from organist_bot.runtime_config_store import runtime_config
from organist_bot.scraper import Scraper
from organist_bot.storage import (
    load_listings_hash,
    load_seen_gigs,
    save_listings_hash,
    save_seen_gigs,
)

logger = logging.getLogger(__name__)


_LOCK_FILE = "/tmp/organistbot_scheduler.lock"


def main(
    scraper: Scraper,
    sheets_logger: SheetsLogger | None = None,
    dry_run: bool = False,
) -> None:
    lock = open(_LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.warning("Previous run still in progress — skipping this tick")
        lock.close()
        return

    try:
        _run(scraper, sheets_logger, dry_run=dry_run)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()

    try:
        from organist_bot.weekly_summary import check_and_send

        check_and_send()
    except Exception:
        logger.warning("weekly_summary: check_and_send failed", exc_info=True)


def _run(
    scraper: Scraper,
    sheets_logger: SheetsLogger | None = None,
    dry_run: bool = False,
) -> None:
    run_id = uuid.uuid4().hex[:8]
    set_run_id(run_id)

    if dry_run:
        logger.info("╔═══════════════════════════════════════╗")
        logger.info("║  DRY-RUN mode — no writes or emails  ║")
        logger.info("╚═══════════════════════════════════════╝")

    logger.info(
        "OrganistBot run started",
        extra={
            "run_id": run_id,
            "target_url": settings.target_url,
            "min_fee": settings.min_fee,
            "poll_minutes": settings.poll_minutes,
            "dry_run": dry_run,
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
        pre_filter.add(FeeFilter(min_fee=runtime_config.get("min_fee", settings.min_fee)))
    if settings.enable_sunday_time_filter:
        pre_filter.add(SundayTimeFilter())
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
    if settings.enable_availability_filter:
        unavail = filter_store.unavailable_periods()
        avail_only = filter_store.available_only_periods()
        if unavail:
            pre_filter.add(AvailabilityFilter(unavail, mode="block"))
        if avail_only:
            pre_filter.add(AvailabilityFilter(avail_only, mode="only"))

    gigs_div: list = []
    pre_filter_passed: int = 0

    response = scraper.fetch(settings.target_url)
    gigs_div = scraper.parse_gig_listings(response, "booking noselect")

    # Skip the full pipeline if the gig listings haven't changed since last run.
    # We hash the serialised gig elements rather than the full HTML to ignore
    # dynamic page content (e.g. ASP.NET __VIEWSTATE) that rotates every request.
    # Note: buffered SheetsLogger records from this run are not drained here — they
    # flush with the next changed-page run. Skipped runs therefore appear in Sheets
    # with a slight timestamp lag, not in real time.
    listings_content = "".join(str(el) for el in gigs_div)
    current_hash = hashlib.sha256(listings_content.encode()).hexdigest()
    if load_listings_hash() == current_hash:
        logger.info("Listings page unchanged — skipping run", extra={"hash": current_hash[:12]})
        return

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
    if gig_errors >= 2:
        alert.send_alert(
            f"⚠️ Parse errors in run {run_id}: {gig_errors} gig(s) failed to parse "
            f"out of {len(gigs_div)} listed. Check logs for detail."
        )

    # Emit Phase-1 pre-filter breakdown so the dashboard counts which filters
    # rejected gigs before the detail-page fetch.
    pre_filter.log_and_reset_counts(total_in=len(gigs_div), passed=pre_filter_passed)

    # ── Phase 2: Filter ───────────────────────────────────────────────────────
    logger.info("Phase 2 — applying filters")
    t0 = time.perf_counter()

    filter_chain = GigFilterChain()

    if settings.enable_fee_filter:
        filter_chain.add(FeeFilter(min_fee=runtime_config.get("min_fee", settings.min_fee)))
    else:
        logger.info("FeeFilter disabled")

    if settings.enable_sunday_time_filter:
        filter_chain.add(SundayTimeFilter())
    else:
        logger.info("SundayTimeFilter disabled")

    if settings.enable_blacklist_filter:
        filter_chain.add(BlacklistFilter(filter_store.blacklist_emails()))
    else:
        logger.info("BlacklistFilter disabled")

    if settings.enable_availability_filter:
        unavail = filter_store.unavailable_periods()
        avail_only = filter_store.available_only_periods()
        if unavail:
            filter_chain.add(AvailabilityFilter(unavail, mode="block"))
        if avail_only:
            filter_chain.add(AvailabilityFilter(avail_only, mode="only"))
    else:
        logger.info("AvailabilityFilter disabled")

    if settings.enable_postcode_filter and settings.home_postcode and settings.google_maps_api_key:
        filter_chain.add(
            PostcodeFilter(
                home_postcode=settings.home_postcode,
                api_key=settings.google_maps_api_key,
                max_minutes=runtime_config.get("max_travel_minutes", settings.max_travel_minutes),
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
        if dry_run:
            logger.info(
                "Phase 3 — DRY-RUN: would send notifications",
                extra={"gig_count": len(valid_gigs)},
            )
            for gig in valid_gigs:
                logger.info(
                    "Would notify: %s (%s) — %s",
                    gig.header,
                    gig.organisation or "",
                    gig.fee or "",
                )
        else:
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
    else:
        logger.info("No new gigs passed the filters — notifications skipped")

    # Record every gig we evaluated at detail level (valid OR rejected by a
    # Phase-2-only filter) so blacklist/postcode rejections are not re-fetched on
    # the next listings change. Trade-off (accepted): un-blacklisting an email or
    # raising max_travel_minutes will not re-surface a gig already marked seen.
    if not dry_run:
        newly_seen = {g.link for g in gig_list if g.link}
        if newly_seen:
            save_seen_gigs(seen=seen_gigs_set | newly_seen)

    try:
        expired = application_store.expire_past_applied()
        if expired > 0:
            logger.info("Expired past applications as no_response", extra={"count": expired})
    except Exception:
        logger.warning("application_store: expire_past_applied failed", exc_info=True)

    try:
        import organist_bot.reply_monitor as reply_monitor

        reply_monitor.check_replies()
    except Exception as exc:
        alert.send_alert(f"⚠️ reply_monitor: check_replies failed — {exc}")
        logger.warning("reply_monitor: check_replies failed", exc_info=True)

    try:
        import organist_bot.invoice_monitor as invoice_monitor

        invoice_monitor.check_invoice_reminders_and_replies()
    except Exception as exc:
        alert.send_alert(f"⚠️ invoice_monitor: check failed — {exc}")
        logger.warning("invoice_monitor: check_invoice_reminders_and_replies failed", exc_info=True)

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

    if dry_run:
        logger.info("DRY-RUN complete — listings hash and seen-gigs NOT updated")
    else:
        save_listings_hash(current_hash)

    # ── Flush logs to Google Sheets ───────────────────────────────────────────
    if sheets_logger is not None and not dry_run:
        try:
            rows = sheets_logger.drain()
            logger.info("Sheets flush complete", extra={"rows_written": rows})
        except Exception as exc:
            logger.warning(
                "Sheets flush failed — rows queued for next run",
                exc_info=True,
                extra={"error": str(exc)},
            )


if __name__ == "__main__":
    _parser = argparse.ArgumentParser(description="OrganistBot scheduler")
    _parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Run the full pipeline without sending emails or writing state.",
    )
    _args = _parser.parse_args()
    _dry_run = _args.dry_run or settings.dry_run

    setup_logging(settings.log_file)
    logger.info(
        "Scheduler starting",
        extra={"poll_minutes": settings.poll_minutes, "dry_run": _dry_run},
    )
    if _dry_run:
        logger.info("DRY-RUN mode active — no emails, no state writes, no Sheets drain")
    else:
        alert.send_alert(f"🔄 Scheduler started (polling every {settings.poll_minutes} min)")

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
                logging.getLogger().addHandler(sheets_logger)
            except Exception:
                logger.warning("SheetsLogger init failed — Sheets logging disabled")
        else:
            logger.info(
                "SheetsLogger disabled — no credentials file configured "
                "(set GOOGLE_SHEETS_CREDENTIALS_FILE or GOOGLE_CALENDAR_CREDENTIALS_FILE)"
            )

    scraper = Scraper()
    try:
        main(
            scraper, sheets_logger, dry_run=_dry_run
        )  # run immediately on startup, then on schedule
        current_poll = runtime_config.get("poll_minutes", settings.poll_minutes)
        job = schedule.every(current_poll).minutes.do(main, scraper, sheets_logger, _dry_run)

        _tick = 0
        while True:
            try:
                schedule.run_pending()
            except Exception:
                logger.exception("Unhandled exception in scheduled run")
                alert.send_alert("❌ OrganistBot crashed — check logs.")

            _tick += 1
            if _tick % 10 == 0:
                desired_poll = runtime_config.get("poll_minutes", settings.poll_minutes)
                if desired_poll != current_poll:
                    schedule.cancel_job(job)
                    job = schedule.every(desired_poll).minutes.do(
                        main, scraper, sheets_logger, _dry_run
                    )
                    current_poll = desired_poll
                    logger.info(
                        "Poll interval updated",
                        extra={"poll_minutes": desired_poll},
                    )

            time.sleep(1)
    finally:
        scraper.session.close()
        logger.info("Scraper session closed — bot shutting down")
