# organist_bot/integrations/sheets_logger.py
"""Buffer structured log records in memory and flush them to a Google Sheets spreadsheet.

SheetsLogger is a logging.Handler subclass.  Attach it to the root logger so it
receives every log record emitted during a scheduler run:

    sheets_logger = SheetsLogger(spreadsheet_id=..., credentials_file=...)
    logging.getLogger().addHandler(sheets_logger)

After every scheduler tick, main() calls SheetsLogger.flush():
  1. Drain the in-memory record buffer (accumulated via emit()).
  2. Append each record as a row in the "Logs" sheet.
  3. Clear the buffer so the next run starts clean.
     If the Sheets API call fails the rows are restored to the buffer and will
     be retried on the next flush().

This approach is immune to RotatingFileHandler rotation because it never reads
the log file — records are captured directly from the logging system in-memory.

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

import datetime
import json
import logging
import threading

from google.oauth2 import service_account
from googleapiclient.discovery import build

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

# Log records whose logger name starts with any of these prefixes are skipped
# during emit — they are internal library chatter from python-telegram-bot
# and its HTTP stack, not scheduler run events.
_EXCLUDED_LOGGER_PREFIXES: tuple[str, ...] = ("telegram", "httpx", "httpcore")

# Standard LogRecord attributes that should NOT be treated as extra "details" fields.
_STANDARD_RECORD_KEYS: frozenset[str] = frozenset(
    {
        "args",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


def _record_to_row(record: logging.LogRecord) -> list:
    """Convert a LogRecord to a fixed-column + details row."""
    dt = datetime.datetime.fromtimestamp(record.created, tz=datetime.UTC)
    timestamp = dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"

    doc = {
        "timestamp": timestamp,
        "run_id": getattr(record, "run_id", ""),
        "level": record.levelname,
        "logger": record.name,
        "message": record.getMessage(),
        "module": record.module,
        "function": record.funcName,
        "line": record.lineno,
    }

    # Collect any extra fields attached by the caller (e.g. elapsed_ms, gig_count).
    details = {
        k: v
        for k, v in record.__dict__.items()
        if k not in _STANDARD_RECORD_KEYS and k not in _FIXED_SET and not k.startswith("_")
    }

    fixed_vals = [doc.get(col, "") for col in _FIXED_COLUMNS]
    return fixed_vals + [json.dumps(details, default=str) if details else ""]


class SheetsLogger(logging.Handler):
    """Buffers log records in memory and appends them to a Google Sheets spreadsheet.

    Attach this handler to the root logger so it receives every log record emitted
    during a scheduler run.  Call flush() at the end of each run to drain the buffer
    and write all rows to the configured sheet in a single API call.

    Args:
        spreadsheet_id:   The Google Sheets spreadsheet ID (from the URL).
        credentials_file: Path to a service account JSON key file.
    """

    def __init__(self, spreadsheet_id: str, credentials_file: str) -> None:
        super().__init__()
        self._spreadsheet_id = spreadsheet_id
        creds = service_account.Credentials.from_service_account_file(
            credentials_file, scopes=_SCOPES
        )
        self._service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        self._buffer: list[list] = []
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        """Buffer a single log record.  Called automatically by the logging system."""
        if any(record.name.startswith(p) for p in _EXCLUDED_LOGGER_PREFIXES):
            return
        try:
            row = _record_to_row(record)
            with self._lock:
                self._buffer.append(row)
        except Exception:
            self.handleError(record)

    def flush(self) -> None:
        """logging.Handler.flush() override — no-op (use drain() to send to Sheets)."""

    def drain(self) -> int:
        """Drain the in-memory buffer and append all rows to the Sheets tab.

        If the Sheets API call fails the drained rows are restored to the front of
        the buffer so they will be included on the next drain() call.

        Returns:
            Number of rows appended to the spreadsheet.
        """
        with self._lock:
            rows, self._buffer = self._buffer, []

        if not rows:
            return 0

        try:
            # Check whether the "Logs" sheet already has a header row.
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

            # Write the header row on first use.
            if not has_header:
                self._service.spreadsheets().values().update(
                    spreadsheetId=self._spreadsheet_id,
                    range=f"{_SHEET_NAME}!A1",
                    valueInputOption="RAW",
                    body={"values": [_HEADERS]},
                ).execute()

            # Append all data rows in a single API call.
            self._service.spreadsheets().values().append(
                spreadsheetId=self._spreadsheet_id,
                range=f"{_SHEET_NAME}!A:A",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": rows},
            ).execute()

        except Exception:
            # Restore rows to the buffer so they are retried on the next drain().
            with self._lock:
                self._buffer = rows + self._buffer
            raise

        return len(rows)
