# OrganistBot

A personal automation bot that scrapes organ gig listings from [organistsonline.org](https://organistsonline.org/required/), filters them against your preferences, sends email notifications for matching gigs, and manages your calendar, invoices, and filter configuration via Telegram.

## What it does

1. **Scrapes** the listings page every N minutes for new gig postings, reusing a single persistent HTTP session across all poll runs to avoid repeated TCP/TLS handshakes
2. **Pre-filters** each listing using only the data visible on the listings page (fee, date, time, calendar availability) — gigs that fail are discarded without ever fetching their detail page
3. **Fetches** detail pages only for gigs that pass the pre-filter
4. **Filters** the full gig data through a second chain: contact blacklist and travel time
5. **Notifies** you by email with a summary of matching gigs and sends an application email to each contact
6. **Books** confirmed gigs via Telegram — `/addgig <url>` checks your calendar for clashes then creates a timed event; `/addgig` with no arguments walks you through a step-by-step manual entry
7. **Manages invoices** — converse in plain English with an AI agent that generates PDF invoices, manages your client list, and sends invoice emails
8. **Manages filters** at runtime via Telegram — add/remove blacklisted emails, unavailable periods, and available-only periods without editing any files or restarting the bot; marking yourself unavailable also blocks the corresponding dates on Google Calendar automatically

---

## Prerequisites

- Python 3.12+
- A Gmail account with an [App Password](https://myaccount.google.com/apppasswords) enabled
- A [Google Cloud project](https://console.cloud.google.com/) with:
  - Google Calendar API enabled
  - Google Maps Distance Matrix API enabled
  - A service account with a JSON key file (`credentials.json`)
- A Telegram bot token from [@BotFather](https://t.me/botfather)
- An [Anthropic API key](https://console.anthropic.com/) for the invoice AI agent

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/sebby/organist_bot.git
cd organist_bot

uv sync --extra dev
```

### 2. Configure environment

Copy the example and fill in your values:

```bash
cp .env.example .env
```

| Variable | Description |
|---|---|
| `EMAIL_SENDER` | Your Gmail address |
| `EMAIL_PASSWORD` | Gmail App Password (not your account password) |
| `CC_EMAIL` | Address to CC on every application email |
| `MIN_FEE` | Minimum fee to accept (default: 100) |
| `HOME_POSTCODE` | Your home postcode for travel time checks |
| `GOOGLE_MAPS_API_KEY` | Distance Matrix API key |
| `MAX_TRAVEL_MINUTES` | Max travel time in minutes (default: 45) |
| `GOOGLE_CALENDAR_ID` | Your Google Calendar ID (from Settings → Integrate calendar) |
| `GOOGLE_CALENDAR_CREDENTIALS_FILE` | Path to your service account JSON key |
| `GOOGLE_SHEETS_ID` | Spreadsheet ID for run logs (optional) |
| `GOOGLE_SHEETS_CREDENTIALS_FILE` | Path to service account JSON key for Sheets (falls back to calendar key) |
| `TELEGRAM_BOT_TOKEN` | Token from @BotFather |
| `TELEGRAM_CHAT_ID` | Your personal Telegram chat ID |
| `ANTHROPIC_API_KEY` | API key for the invoice AI agent |
| `APPLICANT_NAME` | Your name (used in application emails) |
| `APPLICANT_MOBILE` | Your mobile number |
| `APPLICANT_VIDEO_1` | Optional performance video link |
| `APPLICANT_VIDEO_2` | Optional performance video link |
| `FROM_NAME` | Your name on invoices |
| `FROM_ADDRESS` | Your address on invoices |
| `FROM_EMAIL` | Your email on invoices |
| `PAYMENT_ACCOUNT_NAME` | Bank account name for invoice payment details |
| `PAYMENT_ACCOUNT_NUMBER` | Bank account number |
| `PAYMENT_SORT_CODE` | Sort code |

### 3. Google Calendar setup

1. In [Google Cloud Console](https://console.cloud.google.com/), create a service account
2. Download the JSON key → save it as `credentials.json` in the project root
3. Open Google Calendar → your calendar → **Settings and sharing**
4. Under **Share with specific people**, add the service account email with **Make changes to events** permission
5. Under **Integrate calendar**, copy the **Calendar ID** → set as `GOOGLE_CALENDAR_ID` in `.env`

### 4. Find your Telegram chat ID

1. Start your bot in Telegram (send any message)
2. Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
3. Copy the `chat.id` value → set as `TELEGRAM_CHAT_ID` in `.env`

---

## Running

### Gig scraper + email notifier

```bash
python main.py
```

Runs immediately on startup, then polls every `POLL_MINUTES` minutes.

### Telegram bot

```bash
python telegram_bot.py
```

Run this in a separate terminal alongside `main.py`.

---

## Telegram bot commands

All commands are restricted to `TELEGRAM_CHAT_ID`.

### Gig calendar

| Command | Description |
|---|---|
| `/addgig <url>` | Scrape an organistsonline.org URL and add the gig to Google Calendar |
| `/addgig` | Manually enter gig details step-by-step (title → org → locality → date → time → fee → confirm) |
| `/cancel` | Cancel an in-progress manual entry |

### Filter management

Changes take effect on the **next polling tick** — no restart needed.

| Command | Description |
|---|---|
| `/blacklist` | List blacklisted contact emails |
| `/blacklist add <email>` | Add an email to the blacklist |
| `/blacklist rm <email>` | Remove an email from the blacklist |
| `/unavailable` | List your unavailable periods |
| `/unavailable add <period>` | Block a period (gigs on these dates are rejected; an all-day "Unavailable" event is also created on Google Calendar) |
| `/unavailable rm <period>` | Remove an unavailable period (deletes the corresponding Google Calendar block) |
| `/available` | List your available-only periods |
| `/available add <period>` | Restrict to a period (gigs outside these dates are rejected) |
| `/available rm <period>` | Remove an available-only period |

Period format: `2026-12-25` (single day) · `2026-12-20:2027-01-05` (range) · `2026-12` (whole month)

If both unavailable and available-only periods are set, unavailable takes precedence.

### Invoicing

Just type your request in plain English — the AI agent handles the rest:

```
"Send an invoice to Holy Cross for March Masses, £240"
"List my clients"
"Generate invoice for St Paul's — 3 services at £120 each"
```

| Command | Description |
|---|---|
| `/reset` | Clear the invoice conversation history |

---

## How filtering works

Filtering happens in two stages to minimise unnecessary HTTP requests.

### Phase 1 — Pre-filter (listing page data only)

Applied to every gig before its detail page is fetched. A gig that fails here costs nothing beyond parsing one HTML element.

| Filter | What it checks |
|---|---|
| `SeenFilter` | Link already in `seen_gigs.csv` |
| `FeeFilter` | Fee meets the minimum threshold |
| `SundayTimeFilter` | Service falls within the accepted time window |
| `CalendarFilter` | No existing event on that date in Google Calendar |
| `AvailabilityFilter` | Date not in your unavailable periods / outside your available-only periods |

### Phase 2 — Full filter (detail page data required)

Applied only to gigs that passed Phase 1 and have had their detail page fetched.

| Filter | What it checks |
|---|---|
| `BlacklistFilter` | Contact email not on the blacklist |
| `PostcodeFilter` | Travel time from home within the maximum |
| `AvailabilityFilter` | Same availability check as Phase 1 |

### Filter configuration

The blacklist and availability periods are stored in `data/filter_config.json` and managed entirely via the Telegram bot commands above. The file is read fresh on every polling tick, so changes apply immediately without a restart.

When an unavailable period is added, an all-day **"Unavailable"** event is created on Google Calendar for the same date range. When a period is removed, the event is deleted. On every bot startup, any periods in `filter_config.json` that don't already have a calendar block are synced automatically.

### Filter toggles

Individual filters can be disabled in `.env`:

```env
ENABLE_FEE_FILTER=true
ENABLE_SUNDAY_TIME_FILTER=true
ENABLE_BLACKLIST_FILTER=true
ENABLE_SEEN_FILTER=true
ENABLE_POSTCODE_FILTER=true
ENABLE_CALENDAR_FILTER=true
ENABLE_AVAILABILITY_FILTER=true
```

---

## Google Sheets Log Rotation

Run logs are streamed to a Google Sheet tab (`Logs`). When a tab approaches the 1M-cell limit, the bot automatically creates the next tab (`Logs 2`, `Logs 3`, …) and continues logging there seamlessly.

Set the Sheet ID and credentials in `.env`:

```env
GOOGLE_SHEETS_ID=your_spreadsheet_id
GOOGLE_SHEETS_CREDENTIALS_FILE=credentials.json
```

---

## Logging

Every run produces structured logs in two places:

| Output | Format | Level | Location |
|---|---|---|---|
| Console | Human-readable, colour-coded | INFO+ | stdout |
| File | JSON (one object per line) | DEBUG+ | `logs/gigs.log` |

Every line includes a fixed-width run_id bracket so columns stay aligned throughout.

```
2026-02-27 17:15:30.004 [--------] INFO  __main__                    Scheduler starting  poll_minutes=2
2026-02-27 17:15:30.045 [9f5e3bed] INFO  __main__                    OrganistBot run started
2026-02-27 17:15:30.312 [9f5e3bed] INFO  __main__                    Scraping complete  listed=22  pre_filter_passed=3  scraped=3  elapsed_ms=259
2026-02-27 17:15:30.571 [9f5e3bed] INFO  __main__                    Filtering complete  total_in=3  valid=2  elapsed_ms=4
```

---

## Development

### CI pipeline

Every push triggers two GitHub Actions jobs defined in `.github/workflows/ci.yml`:

| Job | Steps |
|---|---|
| Lint & type-check | `ruff check .` → `ruff format --check .` → `mypy organist_bot/` |
| Tests | `pytest --tb=short -q` |

Both jobs run in parallel on a fresh Ubuntu VM.

### Run tests locally

```bash
EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest
```

### Lint and type-check locally

```bash
ruff check .
mypy organist_bot/
```

### Install pre-commit hooks

```bash
pip install pre-commit
pre-commit install
```

Hooks run automatically on every `git commit`: trailing whitespace, credential detection, ruff lint+format, mypy.

---

## Project structure

```
organist_bot/
├── main.py                  # Scheduler entry point
├── telegram_bot.py          # Telegram bot entry point
├── pyproject.toml           # Dependencies + tool configuration (ruff, mypy, pytest)
├── .pre-commit-config.yaml  # Pre-commit hooks
│
├── .github/
│   └── workflows/
│       └── ci.yml           # CI pipeline (lint + type-check + tests)
│
├── organist_bot/            # Main package
│   ├── config.py            # Pydantic settings (reads .env)
│   ├── models.py            # Gig dataclass
│   ├── scraper.py           # Web scraper with retry logic
│   ├── filters.py           # Filter chain (fee, time, blacklist, postcode, calendar…)
│   ├── filter_store.py      # File-backed store for runtime-editable filter config
│   ├── notifier.py          # Email notifications via SMTP
│   ├── storage.py           # CSV persistence for seen gigs
│   ├── logging_config.py    # Structured logging (console + rotating JSON file)
│   ├── integrations/
│   │   ├── calendar_client.py   # Google Calendar API wrapper
│   │   ├── sheets_logger.py     # Google Sheets run-log writer with auto tab rotation
│   │   ├── telegram_bot.py      # Unified Telegram bot (calendar + invoicing + filters)
│   │   ├── invoice_agent.py     # Claude AI agentic loop for invoice management
│   │   ├── invoice_generator.py # PDF invoice generation via Playwright + Jinja2
│   │   └── email_sender.py      # SMTP invoice email sender
│   └── templates/
│       ├── invoice.html         # Invoice PDF template
│       └── email.html           # Invoice email template
│
├── data/                    # Runtime state (gitignored)
│   ├── seen_gigs.csv        # Dedup store for the scraper
│   └── filter_config.json   # Blacklist and availability periods (managed via Telegram)
├── logs/                    # Log output (gitignored)
│   └── gigs.log
├── clients.json             # Invoice client database
├── invoices.json            # Invoice history
└── tests/
    ├── test_filters.py
    ├── test_scraper.py
    ├── test_storage.py
    ├── test_calendar_client.py
    ├── test_telegram_integration.py
    └── test_main.py
```
