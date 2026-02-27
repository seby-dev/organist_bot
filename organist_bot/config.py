# organist_bot/config.py

from typing import List
from pydantic_settings import BaseSettings, SettingsConfigDict



class Settings(BaseSettings):
    email_sender: str
    email_password: str
    cc_email: str
    blacklist_emails: List[str] = []
    min_fee: int = 100
    booked_dates: List[str] = []
    poll_minutes: int = 2
    log_file: str = "logs/gigs.log"
    csv_file: str = "data/seen_gigs.csv"
    target_url: str = "https://organistsonline.org/required/"
    base_url: str = "https://organistsonline.org"
    applicant_name: str = ""
    applicant_mobile: str = ""
    applicant_video_1: str = ""
    applicant_video_2: str = ""

    # ── Postcode / distance filter ────────────────────────────────────────────
    home_postcode: str = ""
    google_maps_api_key: str = ""
    max_travel_minutes: int = 45

    # ── Google Calendar ───────────────────────────────────────────────────────
    google_calendar_id: str = ""
    google_calendar_credentials_file: str = ""

    # ── Telegram bot ──────────────────────────────────────────────────────────
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # ── Filter toggles (set to false in .env to disable) ─────────────────────
    enable_fee_filter: bool = True
    enable_sunday_time_filter: bool = True
    enable_blacklist_filter: bool = True
    enable_booked_date_filter: bool = True
    enable_seen_filter: bool = True
    enable_postcode_filter: bool = True
    enable_calendar_filter: bool = True

    model_config = SettingsConfigDict(env_file=".env")

settings = Settings()




