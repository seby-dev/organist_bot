---
name: trace-gig
description: Use when investigating why a specific gig URL didn't appear, wasn't applied to, or was rejected — replays the full 3-phase pipeline against one listing without writing state.
---

# Trace a Single Gig Through the Pipeline

## Overview

`diagnose-run` reads logs to explain *what happened across a tick*. This skill explains *what would happen to one specific gig right now*: it fetches the URL, builds a `Gig`, runs both filter passes, and reports the verdict per phase. It writes nothing (no `seen_gigs.csv`, no Sheets, no applications).

## When to use

- A user (Telegram or otherwise) says "why didn't gig X get applied to?"
- A new filter was added and you want to confirm it rejects/accepts the right gigs
- The pipeline rejected something that should have passed, and the logs only show a count — you need per-gig detail

## How to invoke

```bash
python -m scripts.trace_gig "https://organistsonline.org/required/..."
```

The script prints a structured report:

```
== Trace: https://organistsonline.org/required/12345 ==
[basic]      header=..., date=..., fee=..., link=...
[pre-filter] SeenFilter            → pass
             FeeFilter(min_fee=100)→ pass
             SundayTimeFilter      → pass
             CalendarFilter        → REJECT (date clash on 2026-06-15)
verdict: REJECTED in pre-filter — would NOT have hit detail-page fetch
```

If pre-filter passes, the script fetches the detail page and runs the full filter chain:

```
[detail]     email=..., postcode=..., contact=...
[full-filter] BlacklistFilter      → pass
             PostcodeFilter(...)   → REJECT (travel 67 min > 45 max)
verdict: REJECTED in full filter — would NOT have been notified or applied to
```

## What it tells you that logs don't

| Question | trace-gig | logs |
|---|---|---|
| Which exact filter rejected this gig? | Yes, per-filter | Only aggregate counts |
| What detail-page fields did the scraper extract? | Yes, prints them | No |
| Would current `runtime_config` overrides (live `min_fee`) accept it? | Yes — uses live values | Yes, but only after next tick |
| Effect of a hypothetical filter toggle change? | Run with `--no-X` flags | Requires editing `.env` + restart |

## Common Diagnoses

| trace output | Meaning |
|---|---|
| `SeenFilter → REJECT` | Gig URL already in `data/seen_gigs.csv` — it was processed in a previous run |
| `CalendarFilter → REJECT (date clash)` | A Google Calendar event blocks the gig date — see `manage_unavailable` to clear |
| `BlacklistFilter → REJECT` | Contact email matches an entry in `filter_store.blacklist_emails()` |
| `PostcodeFilter → REJECT (travel > N)` | Google Maps says > `MAX_TRAVEL_MINUTES`. Either move the postcode bound or lower travel time |
| `Failed to build gig` | Scraper couldn't parse the detail page — check the URL is a `booking noselect` listing, not a removed gig |

## Important constraints

- The script **must not** write to `data/seen_gigs.csv`, `data/applications.json`, the Sheets logger, or the calendar. It only reads.
- It **must** read live `runtime_config` and `filter_store` values so the verdict matches what the scheduler would do *right now*.
- It should construct filters the same way `main.py` does (same toggles, same guards) so reasoning transfers back to production.

## Implementation pointers

The script lives at `scripts/trace_gig.py`. Pipeline logic in `main.py:75-228` is the source of truth — if you change pre-filter vs. full filter membership there, mirror it in `trace_gig.py`.
