# OrganistBot

A personal automation bot that scrapes organ gig listings from [organistsonline.org](https://organistsonline.org/required/), filters them against your preferences, sends email notifications for matching gigs, and lets you book confirmed gigs directly into Google Calendar via Telegram.

## What it does

1. **Scrapes** the listings page every N minutes for new gig postings, reusing a single persistent HTTP session across all poll runs to avoid repeated TCP/TLS handshakes
2. **Pre-filters** each listing using only the data visible on the listings page (fee, date, time, calendar availability) — gigs that fail are discarded without ever fetching their detail page
3. **Fetches** detail pages only for gigs that pass the pre-filter
4. **Filters** the full gig data through a second chain: contact blacklist and travel time
5. **Notifies** you by email with a summary of matching gigs and sends an application email to each contact
6. **Books** confirmed gigs — send a gig URL to the Telegram bot and it checks your calendar for clashes, then creates a timed event

---

## Prerequisites

- Python 3.12+
- A Gmail account with an [App Password](https://myaccount.google.com/apppasswords) enabled
- A [Google Cloud project](https://console.cloud.google.com/) with:
  - Google Calendar API enabled
  - Google Maps Distance Matrix API enabled
  - A service account with a JSON key file (`credentials.json`)
- A Telegram bot token from [@BotFather](https://t.me/botfather)

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/sebby/organist_bot.git
cd organist_bot

# Runtime only
pip install -r requirements.txt

# Development (includes pytest, mypy, ruff)
pip install -r requirements-dev.txt
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
| `BLACKLIST_EMAILS` | JSON array of contact emails to ignore |
| `BOOKED_DATES` | JSON array of already-booked dates as `YYYYMMDD` |
| `HOME_POSTCODE` | Your home postcode for travel time checks |
| `GOOGLE_MAPS_API_KEY` | Distance Matrix API key |
| `MAX_TRAVEL_MINUTES` | Max travel time in minutes (default: 45) |
| `GOOGLE_CALENDAR_ID` | Your Google Calendar ID (from Settings → Integrate calendar) |
| `GOOGLE_CALENDAR_CREDENTIALS_FILE` | Path to your service account JSON key |
| `TELEGRAM_BOT_TOKEN` | Token from @BotFather |
| `TELEGRAM_CHAT_ID` | Your personal Telegram chat ID |
| `APPLICANT_NAME` | Your name (used in application emails) |
| `APPLICANT_MOBILE` | Your mobile number |
| `APPLICANT_VIDEO_1` | Optional performance video link |
| `APPLICANT_VIDEO_2` | Optional performance video link |

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

### Telegram booking bot

```bash
python telegram_bot.py
```

Run this in a separate terminal alongside `main.py`. Send a gig URL from organistsonline.org to your bot in Telegram to add it to your calendar.

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
| `BookedDateFilter` | Date not already in your booked dates list |
| `CalendarFilter` | No existing event on that date in Google Calendar |

### Phase 2 — Full filter (detail page data required)

Applied only to gigs that passed Phase 1 and have had their detail page fetched.

| Filter | What it checks |
|---|---|
| `BlacklistFilter` | Contact email not on the blacklist |
| `PostcodeFilter` | Travel time from home within the maximum |

### Filter toggles

All filters can be enabled/disabled in `.env`:

```env
ENABLE_FEE_FILTER=true
ENABLE_SUNDAY_TIME_FILTER=true
ENABLE_BLACKLIST_FILTER=true
ENABLE_BOOKED_DATE_FILTER=true
ENABLE_SEEN_FILTER=true
ENABLE_POSTCODE_FILTER=true
ENABLE_CALENDAR_FILTER=true
```

---

## Logging

Every run produces structured logs in two places:

| Output | Format | Level | Location |
|---|---|---|---|
| Console | Human-readable, colour-coded | INFO+ | stdout |
| File | JSON (one object per line) | DEBUG+ | `logs/gigs.log` |

Every line includes a fixed-width run_id bracket so columns stay aligned throughout. Pre-run messages use `[--------]` as a placeholder; once `main()` starts a real 8-character ID is stamped on every subsequent line.

```
2026-02-27 17:15:30.004 [--------] INFO  __main__                    Scheduler starting  poll_minutes=2
2026-02-27 17:15:30.021 [--------] INFO  organist_bot.logging_config Logging initialised  log_file='...'
2026-02-27 17:15:30.045 [9f5e3bed] INFO  __main__                    OrganistBot run started
2026-02-27 17:15:30.312 [9f5e3bed] INFO  __main__                    Scraping complete  listed=22  pre_filter_passed=3  scraped=3  elapsed_ms=259
2026-02-27 17:15:30.571 [9f5e3bed] INFO  __main__                    Filtering complete  total_in=3  valid=2  elapsed_ms=4
```

The HTTP session is created once when the bot starts and shared across all poll runs. "Scraper initialised" therefore appears only once in the logs, not once per poll. When the bot exits (Ctrl-C or SIGTERM) the session is closed cleanly.

---

## Development

### CI pipeline

Every push triggers two GitHub Actions jobs defined in `.github/workflows/ci.yml`:

| Job | Steps |
|---|---|
| Lint & type-check | `ruff check .` → `ruff format --check .` → `mypy organist_bot/` |
| Tests | `pytest --tb=short -q` |

Both jobs run in parallel on a fresh Ubuntu VM. The commit is marked ✅ or ❌ on GitHub.

### Run tests locally

```bash
pytest
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

Hooks run automatically on every `git commit`: trailing whitespace, credential detection, ruff lint+format, mypy. CI runs the same checks on every push as a guarantee.

---

## Project structure

```
organist_bot/
├── main.py                  # Scheduler entry point
├── telegram_bot.py          # Telegram bot entry point
├── requirements.txt         # Runtime dependencies
├── requirements-dev.txt     # Development dependencies
├── pyproject.toml           # Tool configuration (ruff, mypy, pytest)
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
│   ├── notifier.py          # Email notifications via SMTP
│   ├── storage.py           # CSV persistence for seen gigs
│   ├── logging_config.py    # Structured logging (console + rotating JSON file)
│   ├── integrations/
│   │   ├── calendar_client.py  # Google Calendar API wrapper
│   │   └── telegram_bot.py     # Telegram bot handler
│   └── templates/
│       ├── summary.html.j2     # Summary email template
│       └── application.html.j2 # Application email template
│
├── data/                    # Runtime state (gitignored)
│   └── seen_gigs.csv
├── logs/                    # Log output (gitignored)
│   └── gigs.log
└── tests/
    ├── test_filters.py
    ├── test_scraper.py
    ├── test_storage.py
    ├── test_calendar_client.py
    ├── test_telegram_integration.py
    └── test_main.py
```
