# organist_bot/config.py


from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    email_sender: str
    email_password: str
    cc_email: str
    min_fee: int = 100
    negotiable_fee: int = 120
    enable_neg_drafts: bool = True
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
    travel_home_postcode: str = ""  # origin for travel buffer lookups; falls back to home_postcode

    # ── Google Calendar ───────────────────────────────────────────────────────
    google_calendar_id: str = ""
    google_calendar_credentials_file: str = ""

    # ── Google Sheets (run log) ───────────────────────────────────────────────
    google_sheets_id: str = ""
    google_sheets_credentials_file: str = ""  # falls back to google_calendar_credentials_file

    # ── Telegram bot ──────────────────────────────────────────────────────────
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # ── Gmail (reply monitoring) ───────────────────────────────────────────────
    gmail_credentials_file: str = ""
    gmail_token_file: str = "data/gmail_token.json"

    # ── Invoice agent ─────────────────────────────────────────────────────────
    anthropic_api_key: str = ""
    from_name: str = ""
    from_address: str = ""
    from_email: str = ""
    currency: str = "£"
    payment_account_name: str = ""
    payment_account_number: str = ""
    payment_sort_code: str = ""
    payment_note: str = ""

    # ── SMTP ──────────────────────────────────────────────────────────────────
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""

    # ── Filter toggles (set to false in .env to disable) ─────────────────────
    enable_fee_filter: bool = True
    enable_sunday_time_filter: bool = True
    enable_blacklist_filter: bool = True
    enable_seen_filter: bool = True
    enable_postcode_filter: bool = True
    enable_calendar_filter: bool = True

    # ── Availability filter ───────────────────────────────────────────────────
    enable_availability_filter: bool = True

    # ── Dry-run mode ──────────────────────────────────────────────────────────
    dry_run: bool = False

    # ── Weekly summary ────────────────────────────────────────────────────────
    weekly_summary_time: str = "09:00"

    model_config = SettingsConfigDict(env_file=".env")


settings = Settings()  # type: ignore[call-arg]
