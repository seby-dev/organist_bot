# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Pull request workflow

After pushing a branch and creating a PR:
1. Always create PRs as **ready for review** (never as draft).
2. Immediately enable **auto-merge with squash** on the PR (`mcp__github__enable_pr_auto_merge` with `mergeMethod: SQUASH`).

## Ship workflow

Never commit directly to `main`. For any change:

```bash
git checkout -b <descriptive-branch-name>
# ... make changes, commit ...
make ship
```

`make ship` runs the full local quality gate (`make pre-push`: ruff lint,
ruff format --check, mypy, bandit + semgrep, pytest) — refusing to run at
all if you're on `main` — then pushes the branch, opens a PR (ready for
review, matching the workflow above), and enables squash auto-merge.
`core.hooksPath` is set to `.githooks` automatically the first time `make
pre-push` or `make ship` runs, so the same checks also run as a real `git
push` hook — a push that skips `make ship` entirely still can't skip the
gate.

`main` requires the `Lint & type-check` and `Tests` CI checks to pass
before any PR can merge — auto-merge genuinely waits for green CI rather
than merging immediately.

Separately, `scripts/auto_deploy.py` re-runs the same checks locally
(ruff/mypy/pytest) immediately before restarting the live bots, as a
backstop that doesn't depend on GitHub Actions or `gh` auth being
reachable from a background launchd process. See its module docstring for
the exact failure-handling behavior (alert-once-per-SHA, conditional safe
rollback).

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

The project has two long-running processes that share the `organist_bot` package. Both run under launchd (see `scripts/install-launchagent.sh`) and are auto-redeployed by `scripts/auto_deploy.py` on every push to `main` — but only after `auto_deploy.py` re-verifies lint/type/tests locally; see "Ship workflow" above for the full picture.

### `main.py` — Gig scraper/scheduler

Polls `organistsonline.org` every `POLL_MINUTES` and runs a 3-phase pipeline per tick. A `fcntl` exclusive lock on `/tmp/organistbot_scheduler.lock` prevents overlapping ticks (e.g. after an auto-deploy restart).

**Short-circuit**: each tick first hashes the listings HTML and compares it against `data/listings_hash.txt`. If unchanged, the run skips the rest of the pipeline (Sheets buffer drains on the next changed run, so skipped ticks have a slight timestamp lag).

1. **Scrape** (`scraper.py`) — fetches the listings page, extracts basic fields, then fetches each detail page for gigs that survive the pre-filter. The pre-filter (Phase 1) deliberately includes `SeenFilter` and `CalendarFilter` to avoid the detail-page HTTP fetch for gigs that would be rejected anyway.
2. **Filter** (`filters.py`) — applies the full `GigFilterChain` on detail-enriched `Gig` objects (Phase 2). Per-filter rejection counts are logged structured to Sheets.
3. **Notify** (`notifier.py`) — sends an email summary via SMTP and auto-applies to each gig via a Jinja2 template. Each application is recorded in `data/applications.json` via `application_store.record_application`. Seen gigs are then persisted to `data/seen_gigs.csv`.

**Post-pipeline steps** (run every tick, even when no new gigs):
- `application_store.expire_past_applied()` — flips `applied` rows whose gig date is in the past to `no_response`.
- `reply_monitor.check_replies()` — polls Gmail for replies to active applications and classifies each with Claude Haiku (`accepted` / `rejected` / `cancellation` / `unclear`). On `accepted` it upserts the application as accepted, creates a Google Calendar event, and pings Telegram.

Finally, `SheetsLogger` (a buffering `logging.Handler` subclass) drains its in-memory record buffer into the Google Sheet in one batch. It rotates to a new `Logs N` tab when the active tab nears Sheets' per-sheet cell limit, and — since Sheets also enforces a hard 10M-cell-per-workbook cap — prunes (deletes) any `Logs N` tab whose newest row is older than `_RETENTION_DAYS` (60 days) each time it rotates, so historical tabs don't accumulate forever.

### `telegram_bot.py` — Unified Telegram bot

A single python-telegram-bot polling bot, gated by `TELEGRAM_CHAT_ID`. **Every free-text message is forwarded to `unified_agent.process_message`** (`integrations/unified_agent.py`) — a multi-domain Claude Sonnet 4.6 agent with ~27 tools spanning:
- **Gig calendar** — `add_gig` (from URL or fields), `list_upcoming_gigs`, `manage_competing_gigs`
- **Invoicing** — `generate_invoice`, `email_invoice`, `list_clients`, `list_invoices`
- **Filter management** — `manage_blacklist`, `manage_unavailable`, `manage_available` (writes to `filter_store`), `manage_filter_suspensions` (writes to `filter_suspension_store`)
- **Runtime config** — `manage_config` (writes to `runtime_config_store`: `min_fee`, `max_travel_minutes`, `poll_minutes`)
- **Pipeline observability** — `get_gig_stats` (queries Sheets via `SheetsLogger.query_run_stats`)
- **Applications & income** — `manage_applications`, `get_income_forecast` (reads from `application_store`)

Per-chat history, last-invoice context, and last-gig-listing context live in process memory keyed by `chat_id`. The reference-context fields (last invoice / gig-listing / application-listing — but **not** history) are also persisted to `data/agent_state.json` via `integrations/agent_state.py`: `process_message` lazily hydrates a chat's context on its first message (so it survives a bot restart) and saves it after each turn. On startup the bot calls `sync_calendar_blocks` (mirrors `filter_store.unavailable_periods()` into Google Calendar) and fires `alert.send_alert("🤖 Telegram bot started")`. The old 7-step `ConversationHandler` and the separate `invoice_agent.py` no longer exist — all interactions go through the unified agent.

### `organist_bot/` — top-level modules

- `config.py` — single `Settings` pydantic-settings instance loaded from `.env`
- `models.py` — `Gig` dataclass (the only shared model)
- `scraper.py` — `requests.Session` + BeautifulSoup, with `tenacity` retry on 5xx
- `filters.py` — all `GigFilter` classes and `GigFilterChain` (see "Filters" below)
- `notifier.py` — `Notifier` + `Transport` protocol (production: `SMTPTransport`, tests: `FakeTransport`)
- `storage.py` — `seen_gigs.csv` and `listings_hash.txt` I/O
- `application_store.py` — JSON-backed application lifecycle (`applied → accepted/no_response/declined/rejected`)
- `filter_store.py` — JSON-backed runtime filter values (blacklist, unavail/avail periods); read fresh each tick
- `filter_suspension_store.py` — JSON-backed store for date-ranged filter suspensions (temporarily exempt gigs, by their own date, from a named filter or all filters except `seen`); read fresh each tick
- `runtime_config_store.py` — JSON-backed pipeline overrides (`min_fee`, `max_travel_minutes`, `poll_minutes`)
- `reply_monitor.py` — Gmail → Claude-classifier → application_store + calendar + Telegram
- `alert.py` — fire-and-forget Telegram alert (`send_alert(message)`); silently no-ops if unconfigured
- `logging_config.py` — dual handler (ANSI console + rotating JSON file), `run_id` correlation

### `organist_bot/integrations/`

- `calendar_client.py` — `GoogleCalendarClient` (service account; `has_event_on_date`, `add_gig`, `block_period`, `unblock_period`)
- `sheets_logger.py` — buffering `logging.Handler` + `query_run_stats` for the dashboard
- `gmail_client.py` — OAuth2 Gmail read-only; refreshes token + atomic write with `0o600`
- `telegram_bot.py` — the bot module the entry point delegates to
- `unified_agent.py` — Claude SDK agentic loop, ~27 tools, per-chat state
- `invoice_generator.py` — Playwright headless Chromium → PDF from Jinja2 `invoice.html`
- `email_sender.py` — SMTP invoice email sender

## Configuration

All config lives in `organist_bot/config.py` as a single `Settings` (pydantic-settings) object loaded from `.env`. **Every new env var must be declared as a field on `Settings`** — pydantic rejects unknown keys at startup. Read values from `settings` (not `os.getenv()`).

Required fields (no defaults; pydantic raises on import if unset): `EMAIL_SENDER`, `EMAIL_PASSWORD`, `CC_EMAIL`.

Optional sections in `.env`:
- **Scraper** — `MIN_FEE` (default 100), `NEGOTIABLE_FEE` (default 120; proposed fee for NEG-flagged gigs), `POLL_MINUTES` (default 2), `TARGET_URL`, applicant fields (`APPLICANT_NAME`, `APPLICANT_MOBILE`, `APPLICANT_VIDEO_1/2`)
- **Postcode / distance** — `HOME_POSTCODE`, `GOOGLE_MAPS_API_KEY`, `MAX_TRAVEL_MINUTES` (default 45)
- **Google Calendar** — `GOOGLE_CALENDAR_ID`, `GOOGLE_CALENDAR_CREDENTIALS_FILE`
- **Google Sheets** — `GOOGLE_SHEETS_ID`, `GOOGLE_SHEETS_CREDENTIALS_FILE` (falls back to the calendar creds file)
- **Telegram** — `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- **Gmail reply monitor** — `GMAIL_CREDENTIALS_FILE`, `GMAIL_TOKEN_FILE` (default `data/gmail_token.json`); run `scripts/setup_gmail_auth.py` once to mint the token
- **Anthropic** — `ANTHROPIC_API_KEY`
- **Invoice / SMTP** — `FROM_NAME`, `FROM_ADDRESS`, `CURRENCY`, payment fields, `SMTP_HOST/PORT/USER/PASSWORD`
- **Filter toggles** — `ENABLE_FEE_FILTER`, `ENABLE_SUNDAY_TIME_FILTER`, `ENABLE_BLACKLIST_FILTER`, `ENABLE_SEEN_FILTER`, `ENABLE_POSTCODE_FILTER`, `ENABLE_CALENDAR_FILTER`, `ENABLE_AVAILABILITY_FILTER`, `ENABLE_NEG_DRAFTS` (all default `True`)

`runtime_config_store` overrides `MIN_FEE`, `MAX_TRAVEL_MINUTES`, `POLL_MINUTES`, and `NEGOTIABLE_FEE` at runtime — the scheduler reads via `runtime_config.get(key, settings.foo)` so `.env` values are the fallback.

## Data files

| File | Purpose |
|---|---|
| `data/seen_gigs.csv` | Dedup store for the scraper; one gig URL per line |
| `data/applications.json` | Application lifecycle store (written by `application_store`); `neg_pending` rows hold unsent NEG drafts awaiting Telegram approval |
| `data/filter_config.json` | Runtime filter values: blacklist, unavail/avail periods |
| `data/filter_suspensions.json` | Runtime filter suspensions (written by `filter_suspension_store`): which filter (or `all`) is exempted for which date range |
| `data/runtime_config.json` | Runtime pipeline overrides: min_fee, max_travel_minutes, poll_minutes |
| `data/agent_state.json` | Per-chat agent reference-context (last invoice/gig-listing/application-listing) persisted across restarts by `integrations/agent_state.py` |
| `data/listings_hash.txt` | Hash of last-seen listings HTML for short-circuit detection |
| `data/last_deployed_sha.txt` | SHA of the last successfully deployed commit; written by `scripts/auto_deploy.py` after each restart (gitignored) |
| `data/last_failed_deploy_sha.txt` | SHA of the last commit that failed `auto_deploy.py`'s local re-run gate (ruff/mypy/pytest); prevents re-alerting every 60s for the same stuck failure (gitignored) |
| `data/gmail_token.json` | OAuth2 token for Gmail reply monitoring (gitignored) |
| `data/reply_monitor_since_floor.txt` | Earliest date `reply_monitor.check_replies` will ever search Gmail for; set to "today" on first use and never moves backward, so replies to applications made before it was introduced aren't retroactively surfaced |
| `clients.json` | Invoice client database (project root) |
| `invoices.json` | Invoice history/metadata (project root) |
| `output/` | Generated PDF invoices (gitignored) |
| `organist_bot/templates/invoice.html` | Jinja2 template for PDF invoices |

## Filters

`GigFilterChain` composes individual `GigFilter` implementations from `filters.py`. Each filter implements `is_valid(gig) -> bool`. The chain runs two passes in `main.py`:
- **Pre-filter** (basic fields only — fast, no detail-page fetch): `SeenFilter`, `FeeFilter`, `SundayTimeFilter`, `CalendarFilter`, `AvailabilityFilter`
- **Full filter** (after detail-page fetch): all of the above plus `BlacklistFilter` (requires contact email) and `PostcodeFilter` (requires postcode + Google Maps API)

`PostcodeFilter` requires `HOME_POSTCODE` and `GOOGLE_MAPS_API_KEY` to activate. `CalendarFilter` requires `GOOGLE_CALENDAR_ID` and `GOOGLE_CALENDAR_CREDENTIALS_FILE`.

### NEG-fee drafts

When `ENABLE_NEG_DRAFTS=true` (default), gigs whose fee is `"NEG"` or `"Negotiable"` are NOT rejected by `FeeFilter` — `FeeFilter` is excluded from both chains and an explicit fee partition runs after Phase 2. Gigs passing every *other* filter get a draft email proposing `NEGOTIABLE_FEE` (default 120, runtime-overridable via the agent's `manage_config`) rendered from `templates/negotiation.html.j2`, persisted to `applications.json` as `status: "neg_pending"`, and a Telegram alert with the plain-text draft + a 12-char `gig_id`.

The user approves/edits/rejects via Telegram chat (unified-agent tools, two-step `confirmed` pattern):
- `approve <gig_id>` → `approve_neg_application` sends the stored draft verbatim and transitions the row to `applied`.
- `edit <gig_id>: <new body>` or `edit <gig_id> fee 150` → `edit_neg_application` (replaces the body or re-renders with `new_fee`) then sends.
- `reject <gig_id>` → `reject_neg_application` transitions to `rejected`; no email.

Past-date `neg_pending` rows auto-flip to `expired` via `expire_past_applied`. `ENABLE_NEG_DRAFTS=false` reverts to the old behavior (NEG gigs rejected by `FeeFilter`).

Two intentional visibility caveats: (1) `neg_pending`/`rejected`/`expired` NEG rows have no `applied_at`, so they never appear in `manage_applications` summaries or analytics — only `list_neg_pending` shows drafts, and approved drafts become normal `applied` rows; (2) with NEG drafting active, `FeeFilter` disappears from the dashboard's `filter_breakdown` metric — the "Fee partition applied" log (normal/neg/dropped counts) replaces it.

### Filter suspensions

Any filter except `SeenFilter` can be temporarily suspended for a date range via the Telegram agent's `manage_filter_suspensions` tool, backed by `filter_suspension_store.py` (`data/filter_suspensions.json`). Suspension containment is keyed by the **gig's own date** (same model as `unavailable_periods`/`available_only_periods`), not the date the suspension was created. Period tokens support the existing formats (`YYYY-MM-DD`, `YYYY-MM-DD:YYYY-MM-DD`, `YYYY-MM`) plus two open-ended forms: `YYYY-MM-DD:` (from that date onward, never auto-expires) and `:YYYY-MM-DD` (up to and including that date, auto-expires like any closed range).

In `main.py`, each suspendable filter instance is wrapped in a `SuspendableFilter` (`filters.py`) at construction time, using a suspension snapshot loaded once per tick via `filter_suspension_store.load_active()`. Wrapping happens before the instance is used anywhere — including the direct `_fee_filter(gig)` call inside the NEG-drafts fee-partition block — so a fee suspension takes effect there too, not only inside `GigFilterChain`. `filter="all"` suspends every wrapped filter but never reaches `SeenFilter`, since it's never wrapped in the first place.
