# tests/test_sheets_logger.py
"""Tests for SheetsLogger (in-memory logging.Handler — emit + drain)."""

import datetime
import json
import logging
from unittest.mock import MagicMock, patch

import pytest

from organist_bot.integrations.sheets_logger import (
    _EXCLUDED_LOGGER_PREFIXES,
    _HEADERS,
    SheetsLogger,
)

# ── Fixtures ───────────────────────────────────────────────────────────────────

# A known UTC instant used to verify timestamp formatting in row assertions.
_KNOWN_TS = datetime.datetime(2026, 3, 3, 10, 0, 1, 3000, tzinfo=datetime.UTC)
_KNOWN_TS_STR = "2026-03-03T10:00:01.003Z"


@pytest.fixture
def mock_service():
    svc = MagicMock()
    # Default: sheet has a header row (A1:A1 returns a value).
    svc.spreadsheets().values().get().execute.return_value = {"values": [["timestamp"]]}
    return svc


@pytest.fixture
def sheets_logger(mock_service):
    """SheetsLogger with all Google API calls mocked out."""
    with (
        patch(
            "organist_bot.integrations.sheets_logger"
            ".service_account.Credentials.from_service_account_file"
        ),
        patch(
            "organist_bot.integrations.sheets_logger.build",
            return_value=mock_service,
        ),
    ):
        return SheetsLogger(
            spreadsheet_id="fake_sheet_id",
            credentials_file="fake_creds.json",
        )


def _make_record(
    name: str = "__main__",
    level: int = logging.INFO,
    msg: str = "Test log line",
    **extra,
) -> logging.LogRecord:
    """Build a LogRecord for testing emit().

    Sets a fixed created timestamp so timestamp-column assertions are deterministic.
    """
    record = logging.LogRecord(
        name=name,
        level=level,
        pathname="main.py",
        lineno=51,
        msg=msg,
        args=(),
        exc_info=None,
    )
    record.funcName = "main"
    record.module = "main"
    # run_id is normally injected by the logging filter in logging_config.py.
    record.run_id = "a1b2c3d4"
    record.created = _KNOWN_TS.timestamp()
    for k, v in extra.items():
        setattr(record, k, v)
    return record


# ── Buffer / drain mechanics ───────────────────────────────────────────────────


class TestDrainMechanics:
    def test_empty_buffer_returns_zero(self, sheets_logger, mock_service):
        result = sheets_logger.drain()
        assert result == 0
        mock_service.spreadsheets().values().append.assert_not_called()

    def test_returns_correct_row_count(self, sheets_logger):
        for _ in range(5):
            sheets_logger.emit(_make_record())
        assert sheets_logger.drain() == 5

    def test_single_record_returns_one(self, sheets_logger):
        sheets_logger.emit(_make_record())
        assert sheets_logger.drain() == 1

    def test_buffer_cleared_after_drain(self, sheets_logger):
        sheets_logger.emit(_make_record())
        sheets_logger.drain()
        assert sheets_logger.drain() == 0

    def test_rows_restored_to_buffer_on_api_failure(self, sheets_logger, mock_service):
        """If the Sheets API raises, rows must be put back so the next drain retries."""
        mock_service.spreadsheets().values().get().execute.side_effect = RuntimeError("boom")
        sheets_logger.emit(_make_record())
        with pytest.raises(RuntimeError):
            sheets_logger.drain()
        # Buffer restored — second drain should attempt to send the row again.
        mock_service.spreadsheets().values().get().execute.side_effect = None
        assert sheets_logger.drain() == 1


# ── Header row ────────────────────────────────────────────────────────────────


class TestDrainHeaderRow:
    def test_writes_header_when_sheet_empty(self, sheets_logger, mock_service):
        mock_service.spreadsheets().values().get().execute.return_value = {}
        sheets_logger.emit(_make_record())
        sheets_logger.drain()
        update_call = mock_service.spreadsheets().values().update
        update_call.assert_called_once()
        body = update_call.call_args[1]["body"]
        assert body["values"] == [_HEADERS]

    def test_skips_header_when_sheet_has_data(self, sheets_logger, mock_service):
        # Default fixture: get() returns {"values": [["timestamp"]]}.
        sheets_logger.emit(_make_record())
        sheets_logger.drain()
        mock_service.spreadsheets().values().update.assert_not_called()


# ── Batching ──────────────────────────────────────────────────────────────────


class TestDrainBatching:
    def test_single_api_call_for_multiple_records(self, sheets_logger, mock_service):
        for _ in range(10):
            sheets_logger.emit(_make_record())
        sheets_logger.drain()
        assert mock_service.spreadsheets().values().append.call_count == 1
        body = mock_service.spreadsheets().values().append.call_args[1]["body"]
        assert len(body["values"]) == 10

    def test_append_uses_insert_rows_option(self, sheets_logger, mock_service):
        sheets_logger.emit(_make_record())
        sheets_logger.drain()
        call_kwargs = mock_service.spreadsheets().values().append.call_args[1]
        assert call_kwargs["insertDataOption"] == "INSERT_ROWS"


# ── Row structure ─────────────────────────────────────────────────────────────


class TestRowStructure:
    def test_fixed_columns_in_correct_positions(self, sheets_logger, mock_service):
        sheets_logger.emit(_make_record())
        sheets_logger.drain()
        row = mock_service.spreadsheets().values().append.call_args[1]["body"]["values"][0]
        assert row[0] == _KNOWN_TS_STR  # timestamp
        assert row[1] == "a1b2c3d4"  # run_id
        assert row[2] == "INFO"  # level
        assert row[3] == "__main__"  # logger
        assert row[4] == "Test log line"  # message
        assert row[5] == "main"  # module
        assert row[6] == "main"  # function
        assert row[7] == 51  # line

    def test_details_column_contains_extra_fields(self, sheets_logger, mock_service):
        record = _make_record(url="https://organistsonline.org/required/", status=200)
        sheets_logger.emit(record)
        sheets_logger.drain()
        row = mock_service.spreadsheets().values().append.call_args[1]["body"]["values"][0]
        details = json.loads(row[8])
        assert details["url"] == "https://organistsonline.org/required/"
        assert details["status"] == 200

    def test_fixed_columns_not_duplicated_in_details(self, sheets_logger, mock_service):
        sheets_logger.emit(_make_record())
        sheets_logger.drain()
        row = mock_service.spreadsheets().values().append.call_args[1]["body"]["values"][0]
        assert row[8] == ""

    def test_details_empty_string_when_no_extras(self, sheets_logger, mock_service):
        sheets_logger.emit(_make_record())
        sheets_logger.drain()
        row = mock_service.spreadsheets().values().append.call_args[1]["body"]["values"][0]
        assert row[8] == ""

    def test_row_has_nine_columns(self, sheets_logger, mock_service):
        """Each appended row must have exactly len(_HEADERS) = 9 columns."""
        record = _make_record(extra_field="hello")
        sheets_logger.emit(record)
        sheets_logger.drain()
        row = mock_service.spreadsheets().values().append.call_args[1]["body"]["values"][0]
        assert len(row) == len(_HEADERS)

    def test_details_json_non_serialisable_values_use_str(self, sheets_logger, mock_service):
        """Non-JSON-serialisable extras (e.g. sets) must not cause drain() to raise."""
        record = _make_record()
        record.non_serialisable = {1, 2, 3}  # set is not JSON-serialisable by default
        sheets_logger.emit(record)
        result = sheets_logger.drain()
        assert result == 1

    def test_timestamp_format_is_iso8601_utc(self, sheets_logger, mock_service):
        """Timestamp must be formatted as YYYY-MM-DDTHH:MM:SS.mmmZ."""
        sheets_logger.emit(_make_record())
        sheets_logger.drain()
        row = mock_service.spreadsheets().values().append.call_args[1]["body"]["values"][0]
        ts = row[0]
        # Parse back to verify round-trip
        parsed = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=datetime.UTC
        )
        assert abs((parsed - _KNOWN_TS).total_seconds()) < 0.001


# ── Excluded loggers ──────────────────────────────────────────────────────────


class TestExcludedLoggers:
    def test_telegram_library_records_are_excluded(self, sheets_logger, mock_service):
        """telegram.*, httpx, and httpcore records must be dropped; organist_bot kept."""
        sheets_logger.emit(_make_record(name="telegram.ext.ExtBot"))
        sheets_logger.emit(_make_record(name="telegram.ext.Application"))
        sheets_logger.emit(_make_record(name="httpx"))
        sheets_logger.emit(_make_record(name="httpcore.connection"))
        sheets_logger.emit(_make_record(name="organist_bot.integrations.telegram_bot"))
        sheets_logger.emit(_make_record(name="__main__"))
        result = sheets_logger.drain()
        assert result == 2  # only organist_bot.integrations.telegram_bot + __main__
        body = mock_service.spreadsheets().values().append.call_args[1]["body"]
        kept_loggers = [row[3] for row in body["values"]]
        assert "organist_bot.integrations.telegram_bot" in kept_loggers
        assert "__main__" in kept_loggers
        assert not any(n.startswith("telegram") for n in kept_loggers)
        assert "httpx" not in kept_loggers
        assert "httpcore.connection" not in kept_loggers

    def test_excluded_prefixes_constant_covers_known_libraries(self):
        """Sanity-check that the constant includes the three known noisy libraries."""
        assert "telegram" in _EXCLUDED_LOGGER_PREFIXES
        assert "httpx" in _EXCLUDED_LOGGER_PREFIXES
        assert "httpcore" in _EXCLUDED_LOGGER_PREFIXES

    def test_organist_bot_telegram_integration_is_not_excluded(self, sheets_logger, mock_service):
        """organist_bot.integrations.telegram_bot must NOT be filtered out."""
        sheets_logger.emit(_make_record(name="organist_bot.integrations.telegram_bot"))
        result = sheets_logger.drain()
        assert result == 1

    def test_all_records_excluded_returns_zero(self, sheets_logger, mock_service):
        """If every record is from an excluded logger, drain returns 0."""
        sheets_logger.emit(_make_record(name="telegram.ext.ExtBot"))
        sheets_logger.emit(_make_record(name="httpx"))
        result = sheets_logger.drain()
        assert result == 0
        mock_service.spreadsheets().values().append.assert_not_called()
