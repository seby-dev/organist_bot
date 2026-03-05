# organist_bot/integrations/sheets_logger.py
"""Flush structured run logs from gigs.log to a Google Sheets spreadsheet.

After every scheduler tick the bot calls SheetsLogger.flush(log_file):
  1. Read every JSON line written to log_file during that run.
  2. Append each line as a row in the "Logs" sheet.
  3. Truncate log_file so the next run starts with a clean file.

Authentication uses a service account JSON key — the same credential file
already used for Google Calendar.  The required scope is:
  https://www.googleapis.com/auth/spreadsheets

One-time manual setup
---------------------
1. Enable the Google Sheets API in your Google Cloud project.
2. Share the target spreadsheet with the service account's email (Editor).
3. Ensure a sheet tab named "Logs" exists inside the spreadsheet.
4. Add to .env:
     GOOGLE_SHEETS_ID=<spreadsheet_id>
     # GOOGLE_SHEETS_CREDENTIALS_FILE=  (omit to reuse calendar credentials)
"""

import json
import logging
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_SHEET_NAME = "Logs"

# Columns written in a fixed order — all other keys go into the "details" column.
_FIXED_COLUMNS = [
    "timestamp",
    "run_id",
    "level",
    "logger",
    "message",
    "module",
    "function",
    "line",
]
_HEADERS = _FIXED_COLUMNS + ["details"]
_FIXED_SET = set(_FIXED_COLUMNS)

# Log lines whose logger name starts with any of these prefixes are skipped
# during flush — they are internal library chatter from python-telegram-bot
# and its HTTP stack, not scheduler run events.
_EXCLUDED_LOGGER_PREFIXES: tuple[str, ...] = ("telegram", "httpx", "httpcore")


class SheetsLogger:
    """Appends structured log lines to a Google Sheets spreadsheet.

    Args:
        spreadsheet_id:   The Google Sheets spreadsheet ID (from the URL).
        credentials_file: Path to a service account JSON key file.
    """

    def __init__(self, spreadsheet_id: str, credentials_file: str) -> None:
        self._spreadsheet_id = spreadsheet_id
        creds = service_account.Credentials.from_service_account_file(
            credentials_file, scopes=_SCOPES
        )
        self._service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        logger.debug(
            "SheetsLogger initialised",
            extra={"spreadsheet_id": spreadsheet_id},
        )

    def flush(self, log_file: str) -> int:
        """Read log_file, append every JSON line to the sheet, then truncate.

        Args:
            log_file: Path to the JSON log file (e.g. settings.log_file).

        Returns:
            Number of rows appended to the spreadsheet.
        """
        # 1. Read the log file.
        try:
            text = Path(log_file).read_text(encoding="utf-8")
        except FileNotFoundError:
            return 0

        # 2. Parse each non-empty line as JSON.
        rows: list[list] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                doc: dict = json.loads(line)
            except json.JSONDecodeError:
                logger.warning(
                    "SheetsLogger: skipping malformed log line",
                    extra={"line_preview": line[:120]},
                )
                continue

            logger_name = doc.get("logger", "")
            if any(logger_name.startswith(p) for p in _EXCLUDED_LOGGER_PREFIXES):
                continue

            fixed_vals = [doc.get(col, "") for col in _FIXED_COLUMNS]
            details = {k: v for k, v in doc.items() if k not in _FIXED_SET}
            rows.append(fixed_vals + [json.dumps(details, default=str) if details else ""])

        # Always truncate even if there were no parseable rows.
        if not rows:
            Path(log_file).open("w", encoding="utf-8").close()
            return 0

        # 3. Check whether the "Logs" sheet already has a header row.
        result = (
            self._service.spreadsheets()
            .values()
            .get(
                spreadsheetId=self._spreadsheet_id,
                range=f"{_SHEET_NAME}!A1:A1",
            )
            .execute()
        )
        has_header = bool(result.get("values"))

        # 4. Write the header row on first use.
        if not has_header:
            self._service.spreadsheets().values().update(
                spreadsheetId=self._spreadsheet_id,
                range=f"{_SHEET_NAME}!A1",
                valueInputOption="RAW",
                body={"values": [_HEADERS]},
            ).execute()

        # 5. Append all data rows in a single API call.
        self._service.spreadsheets().values().append(
            spreadsheetId=self._spreadsheet_id,
            range=f"{_SHEET_NAME}!A:A",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": rows},
        ).execute()

        # 6. Truncate the log file — open("w") preserves the inode so the
        #    rotating file handler can keep writing without interruption.
        with Path(log_file).open("w", encoding="utf-8"):
            pass  # intentional no-op: truncates the file

        return len(rows)
