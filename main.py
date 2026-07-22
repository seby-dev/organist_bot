import argparse
import fcntl
import hashlib
import html as _html
import logging
import pathlib
import re
import time
import uuid
from collections.abc import Callable
from typing import Any

import schedule

import organist_bot.alert as alert
import organist_bot.application_store as application_store
import organist_bot.filter_store as filter_store
import organist_bot.filter_suspension_store as filter_suspension_store
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
    SuspendableFilter,
    is_negotiable,
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


def _send_neg_alert(gig: Gig, gig_id: str, subject: str, body: str) -> None:
    """Two Telegram messages per NEG draft: gig details first, then the draft."""
    org = f" — {gig.organisation}" if gig.organisation else ""
    contact_line = (
        f"Contact: {gig.contact or '(none)'} <{gig.email}>" if gig.email else "Contact: (none)"
    )
    location_line = f"Location: {gig.postcode}\n" if gig.postcode else ""
    details_msg = (
        f"🟡 NEG gig — {gig.header}{org}\n\n"
        f"Date:     {gig.date} · {gig.time}\n"
        f"Fee:      {gig.fee or 'NEG'}\n"
        f"{location_line}"
        f"{contact_line}\n"
        f"Link:     {gig.link}"
    )
    alert.send_alert(details_msg)

    # Strip HTML tags from the draft body for Telegram display.
    plain = _html.unescape(re.sub(r"<[^>]+>", "", body)).strip()
    plain = re.sub(r"\n{3,}", "\n\n", plain)
    draft_msg = (
        f"Draft email — id: {gig_id}\n\n"
        f"Subject: {subject}\n\n"
        f"{plain}\n\n"
        f"Reply:\n"
        f'  • "approve {gig_id}" to send as-is\n'
        f'  • "edit {gig_id}: <new body>" to send a revised version\n'
        f'  • "reject {gig_id}" to skip'
    )
    alert.send_alert(draft_msg)


def warn_if_gmail_monitoring_unconfigured() -> None:
    """Alert once at startup when Gmail reply/payment monitoring cannot run.

    reply_monitor and invoice_monitor both skip their Gmail checks silently
    when credentials are missing, so without this guard the scheduler looks
    like it monitors replies and invoice payments while doing neither.
    """

    if not settings.gmail_credentials_file:
        logger.warning(
            "Gmail monitoring disabled — GMAIL_CREDENTIALS_FILE not set",
            extra={"reason": "credentials_unset"},
        )
        alert.send_alert(
            "⚠️ Gmail monitoring disabled — GMAIL_CREDENTIALS_FILE is not set in .env. "
            "Gig replies and invoice payments will NOT be detected automatically."
        )
    elif not pathlib.Path(settings.gmail_token_file).exists():
        logger.warning(
            "Gmail monitoring disabled — token file missing",
            extra={"reason": "token_missing", "token_file": settings.gmail_token_file},
        )
        alert.send_alert(
            f"⚠️ Gmail monitoring disabled — token file {settings.gmail_token_file} "
            "is missing. Run scripts/setup_gmail_auth.py once to mint it. "
            "Gig replies and invoice payments will NOT be detected automatically."
        )


def main(
    scraper: Scraper,
    sheets_logger: SheetsLogger | None = None,
    dry_run: bool = False,
    lock_file: str | None = None,
) -> None:
    # Read _LOCK_FILE live (not as a bound default) so tests can override it
    # for every main() call via a single autouse fixture patching the module
    # attribute, rather than threading lock_file= through every call site.
    lock = open(lock_file or _LOCK_FILE, "w")
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

    # Build shared filter instances once — the same object is added to both
    # pre_filter (Phase 1) and filter_chain (Phase 2) to avoid config-drift.
    # Each GigFilterChain keeps its own rejection-count dict keyed by the filter
    # object, and these filters are stateless (config only), so sharing is safe.

    # Suspensions snapshot: loaded once per tick, not per gig — same performance
    # pattern already used for blacklist/availability filter construction below.
    suspension_snapshot = filter_suspension_store.load_active()

    _fee_filter: Callable[[Any], bool] | None = (
        FeeFilter(min_fee=runtime_config.get("min_fee", settings.min_fee))
        if settings.enable_fee_filter
        else None
    )
    if _fee_filter is not None:
        # Wrapped here (not just when added to a chain) so the NEG-drafts fee
        # partition below — which calls _fee_filter(gig) directly — also
        # respects fee suspensions.
        _fee_filter = SuspendableFilter("fee", _fee_filter, suspension_snapshot)
    # When NEG drafting is enabled we remove FeeFilter from BOTH chains so NEG
    # gigs survive past pre_filter (needed for the detail-page fetch that gets
    # us the contact email) and past filter_chain. The explicit partition gate
    # below Phase 2 then sorts them into normal / NEG / drop.
    _include_fee_in_chains = _fee_filter is not None and not settings.enable_neg_drafts

    _sunday_time_filter: Callable[[Any], bool] | None = (
        SundayTimeFilter() if settings.enable_sunday_time_filter else None
    )
    if _sunday_time_filter is not None:
        _sunday_time_filter = SuspendableFilter(
            "sunday_time", _sunday_time_filter, suspension_snapshot
        )
    _avail_filters: list = []
    if settings.enable_availability_filter:
        unavail = filter_store.unavailable_periods()
        avail_only = filter_store.available_only_periods()
        if unavail:
            _avail_filters.append(
                SuspendableFilter(
                    "availability", AvailabilityFilter(unavail, mode="block"), suspension_snapshot
                )
            )
        if avail_only:
            _avail_filters.append(
                SuspendableFilter(
                    "availability",
                    AvailabilityFilter(avail_only, mode="only"),
                    suspension_snapshot,
                )
            )

    # Pre-filter chain: filters that only need basic details (fee, date, time, link).
    # SeenFilter (link in seen_gigs.csv) and CalendarFilter (date in Google Calendar)
    # are both included here so we skip the detail-page HTTP fetch for gigs that
    # would be rejected anyway.  Filters requiring detail-page fields
    # (email → BlacklistFilter, postcode → PostcodeFilter) stay in Phase 2.
    pre_filter = GigFilterChain()
    if settings.enable_seen_filter:
        pre_filter.add(SeenFilter(seen_gigs_set))
    if _fee_filter is not None and _include_fee_in_chains:
        pre_filter.add(_fee_filter)
    if _sunday_time_filter is not None:
        pre_filter.add(_sunday_time_filter)
    if (
        settings.enable_calendar_filter
        and settings.google_calendar_id
        and settings.google_calendar_credentials_file
    ):
        cal_client = GoogleCalendarClient(
            credentials_file=settings.google_calendar_credentials_file,
            calendar_id=settings.google_calendar_id,
        )
        pre_filter.add(
            SuspendableFilter("calendar", CalendarFilter(cal_client), suspension_snapshot)
        )
    elif not settings.enable_calendar_filter:
        logger.info("CalendarFilter disabled")
    else:
        logger.info(
            "CalendarFilter disabled — google_calendar_id or google_calendar_credentials_file not set"
        )
    for _af in _avail_filters:
        pre_filter.add(_af)

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

    if _fee_filter is not None and _include_fee_in_chains:
        filter_chain.add(_fee_filter)
    elif _fee_filter is None:
        logger.info("FeeFilter disabled")
    else:
        logger.info("FeeFilter excluded from chains — NEG drafting active")

    if _sunday_time_filter is not None:
        filter_chain.add(_sunday_time_filter)
    else:
        logger.info("SundayTimeFilter disabled")

    if settings.enable_blacklist_filter:
        filter_chain.add(
            SuspendableFilter(
                "blacklist", BlacklistFilter(filter_store.blacklist_emails()), suspension_snapshot
            )
        )
    else:
        logger.info("BlacklistFilter disabled")

    if _avail_filters:
        for _af in _avail_filters:
            filter_chain.add(_af)
    elif not settings.enable_availability_filter:
        logger.info("AvailabilityFilter disabled")

    if settings.enable_postcode_filter and settings.home_postcode and settings.google_maps_api_key:
        filter_chain.add(
            SuspendableFilter(
                "postcode",
                PostcodeFilter(
                    home_postcode=settings.home_postcode,
                    api_key=settings.google_maps_api_key,
                    max_minutes=runtime_config.get(
                        "max_travel_minutes", settings.max_travel_minutes
                    ),
                ),
                suspension_snapshot,
            )
        )
    elif not settings.enable_postcode_filter:
        logger.info("PostcodeFilter disabled")
    else:
        logger.info("PostcodeFilter disabled — home_postcode or google_maps_api_key not set")

    valid_gigs = filter_chain.apply(gig_list)

    # ── Fee partition (only meaningful when enable_neg_drafts is True) ───────
    neg_gigs: list[Gig] = []

    if settings.enable_neg_drafts and _fee_filter is not None:
        normal_gigs: list[Gig] = []
        fee_dropped = 0
        for gig in valid_gigs:
            if _fee_filter(gig):
                normal_gigs.append(gig)
            elif is_negotiable(gig.fee):
                neg_gigs.append(gig)
            else:
                fee_dropped += 1
        logger.info(
            "Fee partition applied",
            extra={
                "total_in": len(valid_gigs),
                "normal": len(normal_gigs),
                "neg": len(neg_gigs),
                "dropped": fee_dropped,
            },
        )
        valid_gigs = normal_gigs  # Phase 3 + seen-gigs only handle normal gigs
    # When enable_neg_drafts is False, FeeFilter was already in the chain so
    # valid_gigs is correct as-is and neg_gigs stays empty.

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

    # ── NEG drafts: render, persist, alert Telegram ───────────────────────────
    if neg_gigs and not dry_run:
        _neg_notifier = Notifier(settings, SMTPTransport(password=settings.email_password))
        _negotiable_fee = runtime_config.get("negotiable_fee", settings.negotiable_fee)
        _queued_ids: list[str] = []
        for gig in neg_gigs:
            if not gig.email:
                logger.warning(
                    "NEG draft skipped — no contact email",
                    extra={"header": gig.header, "link": gig.link},
                )
                continue
            try:
                subject, body = _neg_notifier.draft_negotiation(gig, negotiable_fee=_negotiable_fee)
                gig_id = application_store.record_neg_pending(
                    gig,
                    draft_subject=subject,
                    draft_body=body,
                    negotiable_fee=_negotiable_fee,
                )
                _queued_ids.append(gig_id)
                _send_neg_alert(gig, gig_id, subject, body)
            except Exception:
                logger.exception(
                    "NEG draft failed for gig — skipping",
                    extra={"link": gig.link},
                )
        logger.info(
            "NEG drafts queued",
            extra={"count": len(_queued_ids), "gig_ids": _queued_ids},
        )
    elif neg_gigs and dry_run:
        logger.info(
            "Phase 3 — DRY-RUN: would draft NEG gigs",
            extra={"count": len(neg_gigs)},
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
    # (gig_list is empty when the listings-hash short-circuit returns early, so
    # this block is only reached on a changed-page run.)
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
        removed_suspensions = filter_suspension_store.purge_past_suspensions()
        if removed_suspensions > 0:
            logger.info("Purged expired filter suspensions", extra={"count": removed_suspensions})
    except Exception:
        logger.warning("filter_suspension_store: purge_past_suspensions failed", exc_info=True)

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
            alert.send_alert(f"⚠️ Sheets flush failed — {exc}")


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
        warn_if_gmail_monitoring_unconfigured()

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
