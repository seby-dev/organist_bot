# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Pull request workflow

After pushing a branch and creating a PR:
1. Always create PRs as **ready for review** (never as draft).
2. Immediately enable **auto-merge with squash** on the PR (`mcp__github__enable_pr_auto_merge` with `mergeMethod: SQUASH`).
3. Call `subscribe_pr_activity` for the PR to monitor CI and review events.
4. Run `/code-review --comment` to review the diff and post any findings as inline PR comments.
5. Once CI passes and there are no blocking issues from the review, merge the PR using `mcp__github__merge_pull_request` with `mergeMethod: squash`.

## Commands

```bash
# Install dependencies
uv sync

# Run all tests
pytest --tb=short -q

# Run a single test file
pytest tests/test_filters.py --tb=short -q

# Run a single test by name
pytest tests/test_filters.py::test_fee_filter_rejects_low -q

# Lint
ruff check .

# Format
ruff format .

# Type-check
mypy organist_bot/

# Run the gig scraper/scheduler
python main.py

# Run the Telegram bot (separate process)
python telegram_bot.py
```

Tests require dummy env vars at import time (Pydantic validates on `Settings()` instantiation):
```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest
```

After adding new dependencies, run `playwright install chromium` if Playwright is involved.

## Architecture

The project has two long-running processes that share the `organist_bot` package. Both run under launchd (see `scripts/install-launchagent.sh`) and are auto-redeployed by `scripts/auto_deploy.py` on every push to `main`.

### `main.py` — Gig scraper/scheduler

Polls `organistsonline.org` every `POLL_MINUTES` and runs a 3-phase pipeline per tick. A `fcntl` exclusive lock on `/tmp/organistbot_scheduler.lock` prevents overlapping ticks (e.g. after an auto-deploy restart).

**Short-circuit**: each tick first hashes the listings HTML and compares it against `data/listings_hash.txt`. If unchanged, the run skips the rest of the pipeline (Sheets buffer drains on the next changed run, so skipped ticks have a slight timestamp lag).

1. **Scrape** (`scraper.py`) — fetches the listings page, extracts basic fields, then fetches each detail page for gigs that survive the pre-filter. The pre-filter (Phase 1) deliberately includes `SeenFilter` and `CalendarFilter` to avoid the detail-page HTTP fetch for gigs that would be rejected anyway.
2. **Filter** (`filters.py`) — applies the full `GigFilterChain` on detail-enriched `Gig` objects (Phase 2). Per-filter rejection counts are logged structured to Sheets.
3. **Notify** (`notifier.py`) — sends an email summary via SMTP and auto-applies to each gig via a Jinja2 template. Each application is recorded in `data/applications.json` via `application_store.record_application`. Seen gigs are then persisted to `data/seen_gigs.csv`.

**Post-pipeline steps** (run every tick, even when no new gigs):
- `application_store.expire_past_applied()` — flips `applied` rows whose gig date is in the past to `no_response`.
- `reply_monitor.check_replies()` — polls Gmail for replies to active applications and classifies each with Claude Haiku (`accepted` / `rejected` / `cancellation` / `unclear`). On `accepted` it upserts the application as accepted, creates a Google Calendar event, and pings Telegram.

Finally, `SheetsLogger` (a buffering `logging.Handler` subclass) drains its in-memory record buffer into the Google Sheet in one batch.

### `telegram_bot.py` — Unified Telegram bot

A single python-telegram-bot polling bot, gated by `TELEGRAM_CHAT_ID`. **Every free-text message is forwarded to `unified_agent.process_message`** (`integrations/unified_agent.py`) — a multi-domain Claude Sonnet 4.6 agent with ~26 tools spanning:
- **Gig calendar** — `add_gig` (from URL or fields), `list_upcoming_gigs`, `manage_competing_gigs`
- **Invoicing** — `generate_invoice`, `email_invoice`, `list_clients`, `list_invoices`
- **Filter management** — `manage_blacklist`, `manage_unavailable`, `manage_available` (writes to `filter_store`)
- **Runtime config** — `manage_config` (writes to `runtime_config_store`: `min_fee`, `max_travel_minutes`, `poll_minutes`)
- **Pipeline observability** — `get_gig_stats` (queries Sheets via `SheetsLogger.query_run_stats`)
- **Applications & income** — `manage_applications`, `get_income_forecast` (reads from `application_store`)

Per-chat history, last-invoice context, and last-gig-listing context live in process memory keyed by `chat_id`. On startup the bot calls `sync_calendar_blocks` (mirrors `filter_store.unavailable_periods()` into Google Calendar) and fires `alert.send_alert("🤖 Telegram bot started")`. The old 7-step `ConversationHandler` and the separate `invoice_agent.py` no longer exist — all interactions go through the unified agent.

### `organist_bot/` — top-level modules

- `config.py` — single `Settings` pydantic-settings instance loaded from `.env`
- `models.py` — `Gig` dataclass (the only shared model)
- `scraper.py` — `requests.Session` + BeautifulSoup, with `tenacity` retry on 5xx
- `filters.py` — all `GigFilter` classes and `GigFilterChain` (see "Filters" below)
- `notifier.py` — `Notifier` + `Transport` protocol (production: `SMTPTransport`, tests: `FakeTransport`)
- `storage.py` — `seen_gigs.csv` and `listings_hash.txt` I/O
- `application_store.py` — JSON-backed application lifecycle (`applied → accepted/no_response/declined/rejected`)
- `filter_store.py` — JSON-backed runtime filter values (blacklist, unavail/avail periods); read fresh each tick
- `runtime_config_store.py` — JSON-backed pipeline overrides (`min_fee`, `max_travel_minutes`, `poll_minutes`)
- `reply_monitor.py` — Gmail → Claude-classifier → application_store + calendar + Telegram
- `alert.py` — fire-and-forget Telegram alert (`send_alert(message)`); silently no-ops if unconfigured
- `logging_config.py` — dual handler (ANSI console + rotating JSON file), `run_id` correlation

### `organist_bot/integrations/`

- `calendar_client.py` — `GoogleCalendarClient` (service account; `has_event_on_date`, `add_gig`, `block_period`, `unblock_period`)
- `sheets_logger.py` — buffering `logging.Handler` + `query_run_stats` for the dashboard
- `gmail_client.py` — OAuth2 Gmail read-only; refreshes token + atomic write with `0o600`
- `telegram_bot.py` — the bot module the entry point delegates to
- `unified_agent.py` — Claude SDK agentic loop, ~26 tools, per-chat state
- `invoice_generator.py` — Playwright headless Chromium → PDF from Jinja2 `invoice.html`
- `email_sender.py` — SMTP invoice email sender

## Configuration

All config lives in `organist_bot/config.py` as a single `Settings` (pydantic-settings) object loaded from `.env`. **Every new env var must be declared as a field on `Settings`** — pydantic rejects unknown keys at startup. Read values from `settings` (not `os.getenv()`).

Required fields (no defaults; pydantic raises on import if unset): `EMAIL_SENDER`, `EMAIL_PASSWORD`, `CC_EMAIL`.

Optional sections in `.env`:
- **Scraper** — `MIN_FEE` (default 100), `POLL_MINUTES` (default 2), `TARGET_URL`, applicant fields (`APPLICANT_NAME`, `APPLICANT_MOBILE`, `APPLICANT_VIDEO_1/2`)
- **Postcode / distance** — `HOME_POSTCODE`, `GOOGLE_MAPS_API_KEY`, `MAX_TRAVEL_MINUTES` (default 45)
- **Google Calendar** — `GOOGLE_CALENDAR_ID`, `GOOGLE_CALENDAR_CREDENTIALS_FILE`
- **Google Sheets** — `GOOGLE_SHEETS_ID`, `GOOGLE_SHEETS_CREDENTIALS_FILE` (falls back to the calendar creds file)
- **Telegram** — `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- **Gmail reply monitor** — `GMAIL_CREDENTIALS_FILE`, `GMAIL_TOKEN_FILE` (default `data/gmail_token.json`); run `scripts/setup_gmail_auth.py` once to mint the token
- **Anthropic** — `ANTHROPIC_API_KEY`
- **Invoice / SMTP** — `FROM_NAME`, `FROM_ADDRESS`, `CURRENCY`, payment fields, `SMTP_HOST/PORT/USER/PASSWORD`
- **Filter toggles** — `ENABLE_FEE_FILTER`, `ENABLE_SUNDAY_TIME_FILTER`, `ENABLE_BLACKLIST_FILTER`, `ENABLE_SEEN_FILTER`, `ENABLE_POSTCODE_FILTER`, `ENABLE_CALENDAR_FILTER`, `ENABLE_AVAILABILITY_FILTER` (all default `True`)

`runtime_config_store` overrides `MIN_FEE`, `MAX_TRAVEL_MINUTES`, and `POLL_MINUTES` at runtime — the scheduler reads via `runtime_config.get(key, settings.foo)` so `.env` values are the fallback.

## Data files

| File | Purpose |
|---|---|
| `data/seen_gigs.csv` | Dedup store for the scraper; one gig URL per line |
| `data/applications.json` | Application lifecycle store (written by `application_store`) |
| `data/filter_config.json` | Runtime filter values: blacklist, unavail/avail periods |
| `data/runtime_config.json` | Runtime pipeline overrides: min_fee, max_travel_minutes, poll_minutes |
| `data/listings_hash.txt` | Hash of last-seen listings HTML for short-circuit detection |
| `data/gmail_token.json` | OAuth2 token for Gmail reply monitoring (gitignored) |
| `clients.json` | Invoice client database (project root) |
| `invoices.json` | Invoice history/metadata (project root) |
| `output/` | Generated PDF invoices (gitignored) |
| `organist_bot/templates/invoice.html` | Jinja2 template for PDF invoices |

## Filters

`GigFilterChain` composes individual `GigFilter` implementations from `filters.py`. Each filter implements `is_valid(gig) -> bool`. The chain runs two passes in `main.py`:
- **Pre-filter** (basic fields only — fast, no detail-page fetch): `SeenFilter`, `FeeFilter`, `SundayTimeFilter`, `CalendarFilter`, `AvailabilityFilter`
- **Full filter** (after detail-page fetch): all of the above plus `BlacklistFilter` (requires contact email) and `PostcodeFilter` (requires postcode + Google Maps API)

`PostcodeFilter` requires `HOME_POSTCODE` and `GOOGLE_MAPS_API_KEY` to activate. `CalendarFilter` requires `GOOGLE_CALENDAR_ID` and `GOOGLE_CALENDAR_CREDENTIALS_FILE`.
