# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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

The project has two independent long-running processes that share the `organist_bot` package:

**`main.py` — Gig scraper/scheduler**
Polls `organistsonline.org` every `POLL_MINUTES` and runs a 3-phase pipeline per tick:
1. **Scrape** (`scraper.py`) — fetches the listings page, extracts basic gig data, then fetches each detail page for gigs that survive the pre-filter. The pre-filter deliberately includes `SeenFilter` and `CalendarFilter` to avoid detail-page HTTP fetches for gigs that will be rejected anyway.
2. **Filter** (`filters.py`) — applies the full `GigFilterChain` on detail-enriched `Gig` objects.
3. **Notify** (`notifier.py`) — sends email summaries via SMTP and auto-applies to each gig. Seen gigs are then persisted to `data/seen_gigs.csv`.

After each tick, logs are flushed to Google Sheets via `SheetsLogger` (a `logging.Handler` subclass that buffers records in memory and appends them in batch).

**`telegram_bot.py` — Unified Telegram bot**
A single bot combining two features, both gated by `TELEGRAM_CHAT_ID`:
- **Gig calendar** via `/addgig <url>` (scrapes URL → checks Google Calendar → creates event) or `/addgig` (7-step `ConversationHandler` for manual entry: title → org → locality → date → time → fee → confirm).
- **Invoice AI agent** handles all other free text — a Claude-powered agentic loop (`integrations/invoice_agent.py`) that generates PDF invoices via Playwright, manages a client/invoice JSON store, and sends emails via SMTP.

The `ConversationHandler` is registered before the invoice `MessageHandler` so in-progress manual gig entry intercepts free-text replies before the invoice agent sees them.

**`organist_bot/integrations/`** — external service wrappers:
- `calendar_client.py` — Google Calendar API (service account auth)
- `sheets_logger.py` — Google Sheets API (same service account, same creds file)
- `telegram_bot.py` — unified bot (the entry point above delegates here)
- `invoice_agent.py` — Claude Anthropic SDK agentic loop with tool definitions
- `invoice_generator.py` — Playwright PDF generation from Jinja2 HTML templates
- `email_sender.py` — SMTP invoice email sender

## Configuration

All config lives in `organist_bot/config.py` as a single `Settings` (pydantic-settings) object loaded from `.env`. **Every new env var must be declared as a field in `Settings`** — pydantic rejects unknown keys at startup. Read values from `settings` (not `os.getenv()`) so `.env` is always respected.

Key `.env` sections: scraper/notifier credentials, Google Calendar/Sheets (same service account JSON file for both), Telegram bot token + chat ID, Anthropic API key, SMTP credentials, and invoice payment/from details.

Filter toggles (`ENABLE_FEE_FILTER`, `ENABLE_CALENDAR_FILTER`, etc.) all default to `True` and can be disabled individually in `.env`.

## Data files

- `data/seen_gigs.csv` — dedup store for the scraper; one gig URL per line
- `clients.json` — invoice client database (project root)
- `invoices.json` — invoice history/metadata (project root)
- `output/` — generated PDF invoices (gitignored)
- `organist_bot/templates/` — Jinja2 templates for invoice PDF (`invoice.html`), invoice email (`email.html`), and gig notification email (`email.html` inside `organist_bot/templates/`)

## Filters

`GigFilterChain` composes individual `GigFilter` implementations from `filters.py`. Each filter implements `is_valid(gig) -> bool`. The chain runs two passes in `main.py`:
- **Pre-filter** (basic fields only — fast, no detail-page fetch): `SeenFilter`, `FeeFilter`, `SundayTimeFilter`, `CalendarFilter`, `AvailabilityFilter`
- **Full filter** (after detail-page fetch): all of the above plus `BlacklistFilter` (requires contact email) and `PostcodeFilter` (requires postcode + Google Maps API)

`PostcodeFilter` requires `HOME_POSTCODE` and `GOOGLE_MAPS_API_KEY` to activate. `CalendarFilter` requires `GOOGLE_CALENDAR_ID` and `GOOGLE_CALENDAR_CREDENTIALS_FILE`.

# Development Workflow

## Agent Strategy
- For any task that can be decomposed into independent subtasks, always use subagents.
- **Parallel by default**: if N items can be checked/implemented independently, spawn N subagents simultaneously — never check them one by one in a single agent.
- Delegate exploratory/read-only work (finding files, analysing schemas) to an Explore subagent.
- Delegate planning and design to a Plan subagent before implementation begins.
- Use background agents for long-running tasks (test runs, log analysis) so the main session stays responsive.
- Up to 10 subagents can run in parallel — use this capacity for large implementations.

## Decomposition Pattern
When given an implementation task:
1. Spawn an Explore subagent to map the relevant codebase
2. Spawn a Plan subagent to design the approach
3. Decompose implementation into independent modules and run them in parallel
4. Spawn a review subagent to validate output before committing

## Parallelism Examples
- "Check whether features A, B, C, D are implemented" → spawn 4 Explore subagents in one message, one per feature
- "Implement modules X, Y, Z" → spawn 3 implementer subagents simultaneously if they don't share files
- "Review these 5 files" → spawn 5 reviewer subagents in parallel
- The signal: any time you find yourself writing "check A, then check B, then check C" — stop and parallelize it
