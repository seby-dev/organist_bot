"""
telegram_bot.py — entry point for the OrganistBot Telegram bot.

Usage:
    python telegram_bot.py

Send a gig URL from organistsonline.org to the bot in Telegram and it will
add it to your Google Calendar (after checking for clashes).

Prerequisites (set in .env):
    TELEGRAM_BOT_TOKEN                — from @BotFather
    TELEGRAM_CHAT_ID                  — your personal chat ID (restricts access to you only)
    GOOGLE_CALENDAR_ID                — the calendar to write to
    GOOGLE_CALENDAR_CREDENTIALS_FILE  — path to the service account JSON key
"""

import sys

from organist_bot.config import settings
from organist_bot.logging_config import setup_logging
from organist_bot.integrations.telegram_bot import run

if __name__ == "__main__":
    setup_logging(settings.log_file)

    missing = [
        name for name, val in [
            ("TELEGRAM_BOT_TOKEN",               settings.telegram_bot_token),
            ("TELEGRAM_CHAT_ID",                 settings.telegram_chat_id),
            ("GOOGLE_CALENDAR_ID",               settings.google_calendar_id),
            ("GOOGLE_CALENDAR_CREDENTIALS_FILE", settings.google_calendar_credentials_file),
        ]
        if not val
    ]
    if missing:
        print(f"Error: the following must be set in .env: {', '.join(missing)}")
        sys.exit(1)

    run(settings.telegram_bot_token)
