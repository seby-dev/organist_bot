#!/usr/bin/env python3
"""One-time OAuth2 setup script for Gmail reply monitoring.

Run once to authorise access to Gmail. Saves a refresh token to the path
configured in GMAIL_TOKEN_FILE (default: data/gmail_token.json).

Requires GMAIL_CREDENTIALS_FILE to point to an OAuth2 credentials.json
downloaded from Google Cloud Console (Desktop app type).

Usage:
    python scripts/setup_gmail_auth.py
"""

import sys
from pathlib import Path

# Allow running from project root without installing the package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from organist_bot.config import settings  # noqa: E402

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def main() -> None:
    if not settings.gmail_credentials_file:
        print("Error: GMAIL_CREDENTIALS_FILE is not set in .env")
        sys.exit(1)

    creds_path = Path(settings.gmail_credentials_file)
    if not creds_path.exists():
        print(f"Error: credentials file not found: {creds_path}")
        sys.exit(1)

    token_path = Path(settings.gmail_token_file)
    token_path.parent.mkdir(parents=True, exist_ok=True)

    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
    creds = flow.run_local_server(port=0)
    token_path.write_text(creds.to_json())
    print(f"Token saved to {token_path}")
    print("Gmail reply monitoring is now authorised.")


if __name__ == "__main__":
    main()
