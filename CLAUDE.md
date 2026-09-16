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
gate. `.githooks/pre-commit` also chains the pre-commit framework's
commit-stage hooks (trailing-whitespace, detect-private-key, ruff, mypy,
etc. — see `.pre-commit-config.yaml`), so nothing from the pre-existing
`pre-commit install` setup was lost when `core.hooksPath` took effect.

`main` requires the `Lint & type-check` and `Tests` CI checks to pass
before any PR can merge — auto-merge genuinely waits for green CI rather
than merging immediately.

Separately, `scripts/auto_deploy.py` polls its own checkout
(`~/Developer/organist_bot`) every 60 seconds and deploys whenever local
`main`'s own `HEAD` differs from the last deployed SHA. It never fetches
or merges from origin itself, so a deploy only ever follows something else
already advancing local main (a manual `git pull`/`git merge`, `gh pr
merge` run directly in that checkout, and so on). A squash-merge landing
on `origin/main` alone does **not** reach production until something
advances this checkout's local main too — `_check_stale_origin` sends one
read-only Telegram alert per such gap (comparing against the cached
`refs/remotes/origin/main`, never fetching) so that stall isn't silent,
without fetching, merging, or deploying anything itself.

It re-runs the same checks locally (ruff/mypy/pytest) immediately before
restarting the live bots, as a backstop that doesn't depend on GitHub
Actions or `gh` auth being reachable from a background launchd process —
this alone is minutes-long. The installed launchd job already serializes
its own ticks (a `StartInterval` firing is skipped outright if the
previous one is still running), so a `fcntl` exclusive lock on
`/tmp/organistbot_autodeploy.lock` (the same pattern as `main.py`'s own
scheduler lock, which does guard a real launchd race there) instead
guards the other ways two ticks could overlap — a manual run, `launchctl
kickstart -k`, a bootout/bootstrap reinstall landing mid-tick; an overlap
from any of those is expected and harmless, so it's skipped with a log
line, not an alert. It also refuses to deploy at all unless its own
checkout has `HEAD` on `main` and no
uncommitted changes — a checkout left on another branch, or mid-edit (e.g.
mid-feature-work in the same directory instead of a worktree), silently
blocks every subsequent deploy until fixed, and sends one Telegram alert
per stuck commit for exactly this reason.

See its module docstring for the exact failure-handling behavior —
alert-once-per-SHA throughout. A commit that fails the ruff/mypy/pytest
check gate is first saved to a rescue branch (`autodeploy-failed-<sha>`),
then rolled back via `git reset --hard` to the last good deploy, since
both bot launchd jobs set `KeepAlive` and an unrelated restart would
otherwise load the failed commit anyway with no gate at all; that same
rollback re-applies if the exact commit is ever checked out again (for
example, re-pulled after a previous rollback moved local main away from
it). A `uv sync` failure is handled separately and never rolled back,
since it's usually environmental rather than a property of the commit's
code, so it's retried on the next tick instead.

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

Tests marked `live` (currently just the real multi-turn Anthropic round trip in
`tests/test_live_anthropic_multiturn.py`) make a genuine network call to an
LLM provider and are deselected by default (`addopts = "-m 'not live'"` in
`pyproject.toml`). Run them explicitly, with a real `ANTHROPIC_API_KEY`
configured:
```bash
pytest -m live
```

After adding new dependencies, run `playwright install chromium` if Playwright is involved.

## Architecture

The project has two long-running processes that share the `organist_bot` package. Both run under launchd (see `scripts/install-launchagent.sh`) and are auto-redeployed by `scripts/auto_deploy.py` whenever local `main` advances — not on every push to `origin/main` by itself, since the script never fetches or merges from origin; see "Ship workflow" above for the full picture.

### `main.py` — Gig scraper/scheduler

Polls `organistsonline.org` every `POLL_MINUTES` and runs a 3-phase pipeline per tick. A `fcntl` exclusive lock on `/tmp/organistbot_scheduler.lock` prevents overlapping ticks (e.g. after an auto-deploy restart).

**Short-circuit**: each tick first hashes the listings HTML and compares it against `data/listings_hash.txt`. If unchanged, the run skips the rest of the pipeline.

1. **Scrape** (`scraper.py`) — fetches the listings page, extracts basic fields, then fetches each detail page for gigs that survive the pre-filter. The pre-filter (Phase 1) deliberately includes `SeenFilter` and `CalendarFilter` to avoid the detail-page HTTP fetch for gigs that would be rejected anyway.
2. **Filter** (`filters.py`) — applies the full `GigFilterChain` on detail-enriched `Gig` objects (Phase 2). Per-filter rejection counts are logged.
3. **Notify** (`notifier.py`) — sends an email summary via SMTP and auto-applies to each gig via a Jinja2 template. Each application is recorded in `data/applications.json` via `application_store.record_application`. Seen gigs are then persisted to `data/seen_gigs.csv`.

**Post-pipeline steps** (run every tick, even when no new gigs):
- `application_store.expire_past_applied()` — flips `applied` rows whose gig date is in the past to `no_response`.
- `reply_monitor.check_replies()` — polls Gmail for replies to active applications and classifies each with Claude Haiku (`accepted` / `rejected` / `cancellation` / `unclear`). On `accepted` it upserts the application as accepted, creates a Google Calendar event, and pings Telegram.
- Every gig that passes the filter chain and isn't fee-negotiable also runs through `gig_classifier.classify_gig` (Saturday/Sunday only — weekdays are held on a pure date check) before auto-applying — see "Held-for-review drafts" below.

### `telegram_bot.py` — Unified Telegram bot

A single python-telegram-bot polling bot, gated by `TELEGRAM_CHAT_ID`. **Every free-text message is forwarded to `unified_agent.process_message`** (`integrations/unified_agent.py`) — a multi-domain agent with ~35 tools spanning:
- **Gig calendar** — `add_gig` (from URL or fields), `list_upcoming_gigs`, `manage_competing_gigs`
- **Invoicing** — `generate_invoice`, `email_invoice`, `list_clients`, `list_invoices`
- **Filter management** — `manage_blacklist`, `manage_unavailable`, `manage_available` (writes to `filter_store`), `manage_filter_suspensions` (writes to `filter_suspension_store`)
- **Runtime config** — `manage_config` (writes to `runtime_config_store`: `min_fee`, `max_travel_minutes`, `poll_minutes`)
- **LLM provider** — `manage_llm_provider` (switches between Claude/OpenAI/Gemini via `litellm.acompletion`, backed by `runtime_config_store`'s `llm_provider`/`llm_model` keys), `get_llm_usage_summary` (per-provider call/token totals from `llm_usage_store`)
- **Applications & income** — `manage_applications`, `get_income_forecast` (reads from `application_store`)

The agent runs on whichever provider/model `runtime_config_store` currently holds (default: Anthropic Claude Sonnet, `anthropic/claude-sonnet-4-6`) — see "LLM providers" below.

**Provider switching, failover, and usage tracking.** `manage_llm_provider`'s `set` action never switches immediately — it stashes the requested `(provider, model_key)` in `unified_agent._pending_llm_switch` and returns a Confirm/Cancel button (`llm:confirm:<provider>/<model_key>` / `llm:cancel:<provider>/<model_key>`), handled by `telegram_bot.handle_llm_callback` exactly like the existing NEG-draft confirm flow; the switch only takes effect via `llm_confirm_switch`, and a stale or double-tapped button is a no-op once a newer request has replaced it. Independently, if the currently active provider's call fails mid-conversation, `_call_llm_with_failover` retries the other configured providers in a fixed order (`anthropic → openai → gemini`, skipping any without an API key); the first provider to succeed after a failure is persisted as the new default and reported via `alert.send_alert`, so an outage self-heals without a manual switch. Every successful call (first-try or failed-over) is recorded to `llm_usage_store` (`data/llm_usage.json`); `get_llm_usage_summary` reports today/all-time call counts, token totals, and prompt-cache read/write totals per provider. One model, `openai/gpt-6-astra`, can't serve tool calls through the Chat Completions endpoint at all, so `_call_llm_with_failover` routes it through OpenAI's Responses API (`litellm.aresponses()`, via `_call_openai_responses_api`) instead — every other model keeps using `acompletion()`.

**Prompt caching.** Anthropic candidates only: `_call_llm_with_failover` attaches an `ephemeral` `cache_control` breakpoint to the system prompt and to the last tool in `TOOLS` (via `_with_anthropic_cache_control`, applied fresh per candidate inside the failover loop, never mutating the shared `SYSTEM_PROMPT`/`TOOLS` constants) before calling `litellm.acompletion`. This caches the roughly six-hundred-line static system-prompt-plus-tools prefix that's otherwise resent on every call, including every iteration of one turn's tool-calling loop. OpenAI and Gemini candidates — and the Responses API path for `openai/gpt-6-astra` — get the unmodified messages and tools; both providers apply automatic prompt-prefix caching on their own, with no request-side code needed. Whatever cache-hit/cache-write token counts a response reports (`usage.prompt_tokens_details.cached_tokens`/`.cache_write_tokens`, litellm's normalized name regardless of provider) flow into `llm_usage_store` alongside the ordinary token counts.

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
- `llm_usage_store.py` — JSON-backed per-call LLM usage log (provider, model, token counts, prompt-cache read/write token counts); `record_call` / `summary(since=...)`
- `reply_monitor.py` — Gmail → Claude-classifier → application_store + calendar + Telegram
- `alert.py` — fire-and-forget Telegram alert (`send_alert(message)`); silently no-ops if unconfigured
- `logging_config.py` — dual handler (ANSI console + rotating JSON file), `run_id` correlation

### `organist_bot/integrations/`

- `calendar_client.py` — `GoogleCalendarClient` (service account; `has_event_on_date`, `add_gig`, `block_period`, `unblock_period`)
- `gmail_client.py` — OAuth2 Gmail read-only; refreshes token + atomic write with `0o600`
- `telegram_bot.py` — the bot module the entry point delegates to
- `unified_agent.py` — litellm-backed agentic loop (Claude/OpenAI/Gemini), ~35 tools, per-chat state
- `invoice_generator.py` — Playwright headless Chromium → PDF from Jinja2 `invoice.html`
- `email_sender.py` — SMTP invoice email sender

## Configuration

All config lives in `organist_bot/config.py` as a single `Settings` (pydantic-settings) object loaded from `.env`. **Every new env var must be declared as a field on `Settings`** — pydantic rejects unknown keys at startup. Read values from `settings` (not `os.getenv()`).

Required fields (no defaults; pydantic raises on import if unset): `EMAIL_SENDER`, `EMAIL_PASSWORD`, `CC_EMAIL`.

Optional sections in `.env`:
- **Scraper** — `MIN_FEE` (default 100), `NEGOTIABLE_FEE` (default 120; proposed fee for NEG-flagged gigs), `POLL_MINUTES` (default 2), `TARGET_URL`, applicant fields (`APPLICANT_NAME`, `APPLICANT_MOBILE`, `APPLICANT_VIDEO_1/2`)
- **Postcode / distance** — `HOME_POSTCODE`, `GOOGLE_MAPS_API_KEY`, `MAX_TRAVEL_MINUTES` (default 45)
- **Google Calendar** — `GOOGLE_CALENDAR_ID`, `GOOGLE_CALENDAR_CREDENTIALS_FILE`
- **Telegram** — `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- **Gmail reply monitor** — `GMAIL_CREDENTIALS_FILE`, `GMAIL_TOKEN_FILE` (default `data/gmail_token.json`); run `scripts/setup_gmail_auth.py` once to mint the token
- **LLM providers** — `ANTHROPIC_API_KEY` (default provider), `OPENAI_API_KEY`, `GEMINI_API_KEY` (all optional; `manage_llm_provider` refuses to switch to a provider whose key isn't set)
- **Invoice / SMTP** — `FROM_NAME`, `FROM_ADDRESS`, `CURRENCY`, payment fields, `SMTP_HOST/PORT/USER/PASSWORD`
- **Filter toggles** — `ENABLE_FEE_FILTER`, `ENABLE_SUNDAY_TIME_FILTER`, `ENABLE_BLACKLIST_FILTER`, `ENABLE_SEEN_FILTER`, `ENABLE_POSTCODE_FILTER`, `ENABLE_CALENDAR_FILTER`, `ENABLE_AVAILABILITY_FILTER`, `ENABLE_NEG_DRAFTS` (all default `True`)

`runtime_config_store` overrides `MIN_FEE`, `MAX_TRAVEL_MINUTES`, `POLL_MINUTES`, and `NEGOTIABLE_FEE` at runtime — the scheduler reads via `runtime_config.get(key, settings.foo)` so `.env` values are the fallback.

## Data files

| File | Purpose |
|---|---|
| `data/seen_gigs.csv` | Dedup store for the scraper; one gig URL per line |
| `data/applications.json` | Application lifecycle store (written by `application_store`); `neg_pending`/`review_pending` rows hold a real Gmail draft (`draft_id`) awaiting Telegram Accept/Decline |
| `data/filter_config.json` | Runtime filter values: blacklist, unavail/avail periods |
| `data/filter_suspensions.json` | Runtime filter suspensions (written by `filter_suspension_store`): which filter (or `all`) is exempted for which date range |
| `data/runtime_config.json` | Runtime pipeline overrides: min_fee, max_travel_minutes, poll_minutes |
| `data/llm_usage.json` | Per-call LLM usage log (provider, model, token counts, prompt-cache read/write token counts) written by `llm_usage_store`; read by `get_llm_usage_summary` |
| `data/agent_state.json` | Per-chat agent reference-context (last invoice/gig-listing/application-listing) persisted across restarts by `integrations/agent_state.py` |
| `data/listings_hash.txt` | Hash of last-seen listings HTML for short-circuit detection |
| `data/last_deployed_sha.txt` | SHA of the last successfully deployed commit; written by `scripts/auto_deploy.py` after each restart (gitignored) |
| `data/last_failed_deploy_sha.txt` | SHA of the last commit that failed `auto_deploy.py`'s local re-run gate (ruff/mypy/pytest); prevents re-alerting every 60s for the same stuck failure (gitignored) |
| `data/last_wrong_branch_alert_sha.txt` | SHA of the last commit `auto_deploy.py` couldn't deploy because its checkout wasn't on `main`; prevents re-alerting every 60s while the checkout stays on another branch (gitignored) |
| `data/last_dirty_tree_alert_sha.txt` | SHA of the last commit `auto_deploy.py` couldn't deploy because its checkout had uncommitted changes; prevents re-alerting every 60s while the tree stays dirty (gitignored) |
| `data/last_stale_origin_alert_sha.txt` | `origin/main` SHA last flagged by `auto_deploy.py`'s `_check_stale_origin` as ahead of local main; prevents re-alerting every 60s for the same unpulled gap (gitignored) |
| `data/last_uv_sync_failed_alert_sha.txt` | SHA of the last commit whose `uv sync` failed during `auto_deploy.py`'s deploy attempt; dedups that alert only — unlike the check-gate failure marker, this never blocks a retry, since a `uv sync` failure is usually environmental (gitignored) |
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

### Held-for-review drafts (NEG-fee negotiations + AI-flagged review gigs)

Two independent mechanisms hold a gig for the user's review instead of auto-applying, and both converge on the same real-Gmail-draft + Telegram-button flow:

- **NEG-fee**: when `ENABLE_NEG_DRAFTS=true` (default), gigs whose fee is `"NEG"` or `"Negotiable"` are NOT rejected by `FeeFilter` — `FeeFilter` is excluded from both chains and an explicit fee partition runs after Phase 2, proposing `NEGOTIABLE_FEE` (default 120, runtime-overridable via the agent's `manage_config`) via `templates/negotiation.html.j2`. `ENABLE_NEG_DRAFTS=false` reverts to the old behavior (NEG gigs rejected by `FeeFilter`).
- **Non-NEG hold classifier** (`gig_classifier.classify_gig`, Claude Haiku): runs on every remaining gig that passed the filter chain. Monday–Friday gigs are always held (`hold_reason="weekday"`), no LLM call. Saturday and Sunday gigs go through the classifier: a single Funeral, Wedding, or plain Sunday/Saturday service auto-sends as before; anything bundling two or more services (`hold_reason="multi_service"`) or any other single service type (`hold_reason="other_service_type"`, e.g. Evensong) is held instead. `ANTHROPIC_API_KEY` unset holds every Saturday/Sunday gig (fail-safe) and alerts once at startup (`warn_if_gig_classifier_unconfigured`).

Either path renders the standard `application.html.j2` (non-NEG) or `negotiation.html.j2` (NEG) email, creates a **real Gmail draft** via `GmailClient.create_draft` (requires the `gmail.compose` OAuth scope — `warn_if_gmail_write_scope_missing` alerts once at startup if the token lacks it), persists a row to `applications.json` as `status: "neg_pending"` or `"review_pending"` (`application_store.record_held_draft`), and sends one Telegram alert (`main._send_review_alert`) with the gig's own scraped details and two buttons: **Accept** / **Decline**.

- **Accept** → `Confirm`/`Cancel` re-confirmation → `Confirm` sends the actual Gmail draft (`GmailClient.send_draft` — so a hand-edit made directly in Gmail before confirming goes out as edited) and transitions the row to `applied`.
- **Decline** → immediately deletes the Gmail draft (`GmailClient.delete_draft`) and transitions the row to `rejected`. No confirmation step.

There is no in-Telegram way to edit a draft or act on one via typed chat commands — editing happens directly in Gmail, and the only surviving chat tool is the read-only `list_pending_drafts` (what's pending, and why it's held).

Past-date `neg_pending`/`review_pending` rows auto-flip to `expired` via `expire_past_applied`, which also deletes each row's now-orphaned Gmail draft.

One intentional visibility caveat: `neg_pending`/`review_pending`/`rejected`/`expired` rows have no `applied_at`, so they never appear in `manage_applications` summaries or analytics — only `list_pending_drafts` shows them, and accepted drafts become normal `applied` rows.

**Legacy rows from before this feature.** Any `neg_pending` row written before real Gmail drafts existed has `draft_body`/`draft_subject` but no `draft_id`. Such a row is inert under the current code: its Telegram Accept/Decline buttons carry `neg:*` callback data that no handler matches anymore, so tapping one does nothing, and `review_confirm_send`/`review_decline` return a legacy-row message instead of acting when called against it (checked via `row.get("draft_id")`, never a plain `row["draft_id"]` index). It still auto-expires via `expire_past_applied` once its gig date passes.

### Filter suspensions

Any filter except `SeenFilter` can be temporarily suspended for a date range via the Telegram agent's `manage_filter_suspensions` tool, backed by `filter_suspension_store.py` (`data/filter_suspensions.json`). Suspension containment is keyed by the **gig's own date** (same model as `unavailable_periods`/`available_only_periods`), not the date the suspension was created. Period tokens support the existing formats (`YYYY-MM-DD`, `YYYY-MM-DD:YYYY-MM-DD`, `YYYY-MM`) plus two open-ended forms: `YYYY-MM-DD:` (from that date onward, never auto-expires) and `:YYYY-MM-DD` (up to and including that date, auto-expires like any closed range).

In `main.py`, each suspendable filter instance is wrapped in a `SuspendableFilter` (`filters.py`) at construction time, using a suspension snapshot loaded once per tick via `filter_suspension_store.load_active()`. Wrapping happens before the instance is used anywhere — including the direct `_fee_filter(gig)` call inside the NEG-drafts fee-partition block — so a fee suspension takes effect there too, not only inside `GigFilterChain`. `filter="all"` suspends every wrapped filter but never reaches `SeenFilter`, since it's never wrapped in the first place.
