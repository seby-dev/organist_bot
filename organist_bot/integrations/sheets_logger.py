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
import re
import threading

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from organist_bot import alert

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_SHEET_NAME = "Logs"

# Rotate to a new sheet tab when the active sheet reaches this many cells.
# Google Sheets hard limit is 1 000 000 cells per sheet; we rotate at 900 000
# to leave a comfortable margin and avoid hitting the wall mid-run.
_CELL_THRESHOLD = 900_000
_NUM_COLS = 9  # len(_HEADERS) — kept as a literal to avoid a forward reference

# Cap the in-memory buffer so a prolonged Sheets outage can't grow it without
# bound. On overflow the OLDEST rows are dropped (recent observability is more
# useful than stale) and the dropped count is surfaced via alert on the next
# drain(). ~50k rows is generous headroom (days of buffering at a 2-min poll).
_MAX_BUFFER_ROWS = 50_000

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


def _latest_log_sheet(titles: list[str]) -> str:
    """Return the name of the highest-numbered 'Logs' or 'Logs N' sheet.

    Falls back to ``_SHEET_NAME`` ("Logs") if none are found — e.g. on a brand-new
    spreadsheet where the tab hasn't been created yet.
    """
    pattern = re.compile(r"^Logs(?: (\d+))?$")
    candidates: list[tuple[int, str]] = []
    for title in titles:
        m = pattern.match(title)
        if m:
            n = int(m.group(1)) if m.group(1) else 1
            candidates.append((n, title))
    if not candidates:
        return _SHEET_NAME
    return max(candidates)[1]


def _parse_last_row(response: dict) -> int | None:
    """Extract the last written row number from a Sheets ``append()`` response.

    The response contains an ``updates.updatedRange`` string such as
    ``"Logs!A1001:I1010"``; we parse the trailing row number (1010 here).
    Returns ``None`` if the range is absent or unparseable.
    """
    updated_range = response.get("updates", {}).get("updatedRange", "")
    m = re.search(r":(?:[A-Z]+)(\d+)$", updated_range)
    return int(m.group(1)) if m else None


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
        self._dropped = 0  # rows discarded due to the buffer cap (reported on drain)
        self._lock = threading.Lock()
        # Resolved lazily on the first drain() so we always target the latest
        # Logs sheet even after a process restart.
        self._active_sheet: str | None = None

    def emit(self, record: logging.LogRecord) -> None:
        """Buffer a single log record.  Called automatically by the logging system."""
        if any(record.name.startswith(p) for p in _EXCLUDED_LOGGER_PREFIXES):
            return
        try:
            row = _record_to_row(record)
            with self._lock:
                self._buffer.append(row)
                if len(self._buffer) > _MAX_BUFFER_ROWS:
                    overflow = len(self._buffer) - _MAX_BUFFER_ROWS
                    del self._buffer[:overflow]  # drop oldest
                    self._dropped += overflow
        except Exception:
            self.handleError(record)

    def flush(self) -> None:
        """logging.Handler.flush() override — no-op (use drain() to send to Sheets)."""

    def drain(self) -> int:
        """Drain the in-memory buffer and append all rows to the active Sheets tab.

        Automatically rotates to a new sheet tab (e.g. "Logs 2", "Logs 3") when
        the active sheet reaches ``_CELL_THRESHOLD`` cells.  The first call after
        startup queries the spreadsheet to find the latest existing Logs sheet so
        the correct tab is targeted even after a process restart.

        If the Sheets API call fails the drained rows are restored to the front of
        the buffer so they will be included on the next drain() call.

        Returns:
            Number of rows appended to the spreadsheet.
        """
        with self._lock:
            rows, self._buffer = self._buffer, []
            dropped, self._dropped = self._dropped, 0

        if dropped:
            alert.send_alert(
                f"⚠️ SheetsLogger dropped {dropped} buffered log row(s) — buffer "
                f"cap {_MAX_BUFFER_ROWS} reached (Sheets unavailable?)."
            )

        if not rows:
            return 0

        try:
            # Resolve the active sheet on first call after startup.
            if self._active_sheet is None:
                self._active_sheet = self._resolve_active_sheet()

            sheet = self._active_sheet

            # Ensure the sheet tab exists (auto-create if missing — e.g. after
            # the workbook was cleared and the tab was deleted).
            existing_titles = self._get_sheet_titles()
            if sheet not in existing_titles:
                self._ensure_sheet_exists(sheet)

            # Check whether the sheet already has a header row.
            result = (
                self._service.spreadsheets()
                .values()
                .get(
                    spreadsheetId=self._spreadsheet_id,
                    range=f"{sheet}!A1:A1",
                )
                .execute()
            )
            has_header = bool(result.get("values"))

            # Write the header row on first use.
            if not has_header:
                self._service.spreadsheets().values().update(
                    spreadsheetId=self._spreadsheet_id,
                    range=f"{sheet}!A1",
                    valueInputOption="RAW",
                    body={"values": [_HEADERS]},
                ).execute()

            # Append all data rows in a single API call.
            try:
                response = (
                    self._service.spreadsheets()
                    .values()
                    .append(
                        spreadsheetId=self._spreadsheet_id,
                        range=f"{sheet}!A:A",
                        valueInputOption="RAW",
                        insertDataOption="INSERT_ROWS",
                        body={"values": rows},
                    )
                    .execute()
                )
            except HttpError as exc:
                if exc.resp.status == 400 and "cells" in str(exc).lower():
                    # Workbook-level cell budget exhausted before our per-sheet
                    # threshold fired.  Rotate now and retry on the new sheet.
                    self._active_sheet = self._create_next_sheet()
                    sheet = self._active_sheet
                    self._service.spreadsheets().values().update(
                        spreadsheetId=self._spreadsheet_id,
                        range=f"{sheet}!A1",
                        valueInputOption="RAW",
                        body={"values": [_HEADERS]},
                    ).execute()
                    try:
                        response = (
                            self._service.spreadsheets()
                            .values()
                            .append(
                                spreadsheetId=self._spreadsheet_id,
                                range=f"{sheet}!A:A",
                                valueInputOption="RAW",
                                insertDataOption="INSERT_ROWS",
                                body={"values": rows},
                            )
                            .execute()
                        )
                    except HttpError as retry_exc:
                        if retry_exc.resp.status == 400 and "cells" in str(retry_exc).lower():
                            raise RuntimeError(
                                "Google Sheets workbook cell budget exhausted and cannot be "
                                "recovered automatically. Delete old sheet tabs in the "
                                "spreadsheet to free up cell quota, then restart the bot."
                            ) from retry_exc
                        raise
                else:
                    raise

            # Rotate to a new sheet if we've reached the per-sheet cell threshold.
            last_row = _parse_last_row(response)
            if last_row is not None and last_row * _NUM_COLS >= _CELL_THRESHOLD:
                self._active_sheet = self._create_next_sheet()

        except Exception as exc:
            # Restore rows to the buffer so they are retried on the next drain(),
            # but keep the buffer bounded if the outage persists (drop oldest).
            with self._lock:
                self._buffer = rows + self._buffer
                if len(self._buffer) > _MAX_BUFFER_ROWS:
                    overflow = len(self._buffer) - _MAX_BUFFER_ROWS
                    del self._buffer[:overflow]
                    self._dropped += overflow
            alert.send_alert(f"⚠️ Google Sheets API error (batch append failed): {exc}")
            raise

        return len(rows)

    def query_run_stats(self, days: int = 7) -> list[dict]:
        """Fetch per-run pipeline stats from the active log tab.

        Reads all rows from the active Sheets tab, filters to the last ``days``
        days, groups by run_id, and returns one dict per complete run (i.e. runs
        that have a "Run summary" log row).

        Returns:
            List of run dicts sorted by timestamp descending.  Each dict has:
            run_id, timestamp, listed, pre_filter_passed, valid, gig_errors,
            elapsed_ms, filter_breakdown (dict, may be empty).

        Raises:
            Any exception from the Sheets API propagates — the caller is
            responsible for catching and surfacing it to the user.
        """
        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days)

        sheet = self._resolve_active_sheet()
        result = (
            self._service.spreadsheets()
            .values()
            .get(spreadsheetId=self._spreadsheet_id, range=f"{sheet}!A:I")
            .execute()
        )

        all_rows = result.get("values", [])
        if not all_rows:
            return []

        headers = all_rows[0]
        data_rows = all_rows[1:]

        runs: dict[str, dict] = {}

        for row in data_rows:
            padded = row + [""] * (len(headers) - len(row))
            record = dict(zip(headers, padded, strict=False))

            ts_str = record.get("timestamp", "")
            if not ts_str:
                continue
            try:
                ts = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except ValueError:
                continue

            if ts < cutoff.replace(tzinfo=datetime.UTC):
                continue

            run_id = record.get("run_id", "")
            if not run_id:
                continue

            message = record.get("message", "")
            details_str = record.get("details", "")
            try:
                details: dict = json.loads(details_str) if details_str else {}
            except json.JSONDecodeError:
                details = {}

            if run_id not in runs:
                runs[run_id] = {
                    "run_id": run_id,
                    "timestamp": ts_str,
                    "listed": 0,
                    "pre_filter_passed": 0,
                    "valid": 0,
                    "gig_errors": 0,
                    "elapsed_ms": 0,
                    "filter_breakdown": {},
                }

            if message == "Scraping complete":
                runs[run_id]["listed"] = details.get("listed", 0)
                runs[run_id]["pre_filter_passed"] = details.get("pre_filter_passed", 0)
            elif message == "Run summary":
                runs[run_id]["valid"] = details.get("valid", 0)
                runs[run_id]["gig_errors"] = details.get("gig_errors", 0)
                runs[run_id]["elapsed_ms"] = details.get("elapsed_ms", 0)
                runs[run_id]["_complete"] = True
            elif message == "Filter chain applied":
                fb = details.get("filter_breakdown", {})
                for k, v in fb.items():
                    runs[run_id]["filter_breakdown"][k] = (
                        runs[run_id]["filter_breakdown"].get(k, 0) + v
                    )

        complete = [r for r in runs.values() if r.pop("_complete", False)]
        return sorted(complete, key=lambda r: r["timestamp"], reverse=True)

    # ── Sheet management ──────────────────────────────────────────────────────

    def _get_sheet_titles(self) -> list[str]:
        meta = (
            self._service.spreadsheets()
            .get(spreadsheetId=self._spreadsheet_id, fields="sheets.properties.title")
            .execute()
        )
        return [s["properties"]["title"] for s in meta.get("sheets", [])]

    def _resolve_active_sheet(self) -> str:
        """Query the spreadsheet and return the latest Logs sheet name."""
        return _latest_log_sheet(self._get_sheet_titles())

    def _ensure_sheet_exists(self, sheet_name: str) -> None:
        """Create sheet_name if it doesn't already exist in the spreadsheet."""
        self._service.spreadsheets().batchUpdate(
            spreadsheetId=self._spreadsheet_id,
            body={
                "requests": [
                    {
                        "addSheet": {
                            "properties": {
                                "title": sheet_name,
                                "gridProperties": {"rowCount": 1, "columnCount": _NUM_COLS},
                            }
                        }
                    }
                ]
            },
        ).execute()

    def _create_next_sheet(self) -> str:
        """Create the next sequential Logs sheet tab and return its name."""
        titles = self._get_sheet_titles()
        pattern = re.compile(r"^Logs(?: (\d+))?$")
        max_n = 0
        for title in titles:
            m = pattern.match(title)
            if m:
                max_n = max(max_n, int(m.group(1)) if m.group(1) else 1)
        new_name = f"Logs {max_n + 1}"
        # Start the new sheet with the minimum grid size (1 row × 9 cols = 9 cells)
        # rather than the default 1000×26 = 26 000 cells, to preserve the 10M
        # workbook-level cell budget as much as possible.
        self._service.spreadsheets().batchUpdate(
            spreadsheetId=self._spreadsheet_id,
            body={
                "requests": [
                    {
                        "addSheet": {
                            "properties": {
                                "title": new_name,
                                "gridProperties": {"rowCount": 1, "columnCount": _NUM_COLS},
                            }
                        }
                    }
                ]
            },
        ).execute()
        return new_name
