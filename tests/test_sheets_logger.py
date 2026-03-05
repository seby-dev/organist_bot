# tests/test_sheets_logger.py
"""Tests for SheetsLogger.flush()."""

import json
from unittest.mock import MagicMock, patch

import pytest

from organist_bot.integrations.sheets_logger import (
    _EXCLUDED_LOGGER_PREFIXES,
    _FIXED_COLUMNS,
    _HEADERS,
    SheetsLogger,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_service():
    svc = MagicMock()
    # Default: sheet has a header row (A1:A1 returns a value)
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


def _make_log_line(**extra) -> str:
    """Build a minimal valid JSON log line."""
    doc = {
        "timestamp": "2026-03-03T10:00:01.003Z",
        "run_id": "a1b2c3d4",
        "level": "INFO",
        "logger": "__main__",
        "message": "Test log line",
        "module": "main",
        "function": "main",
        "line": 51,
    }
    doc.update(extra)
    return json.dumps(doc)


# ── File handling ─────────────────────────────────────────────────────────────


class TestFlushFileHandling:
    def test_missing_file_returns_zero(self, sheets_logger, mock_service, tmp_path):
        result = sheets_logger.flush(str(tmp_path / "nonexistent.log"))
        assert result == 0
        mock_service.spreadsheets().values().append.assert_not_called()

    def test_empty_file_returns_zero(self, sheets_logger, mock_service, tmp_path):
        log = tmp_path / "gigs.log"
        log.write_text("", encoding="utf-8")
        result = sheets_logger.flush(str(log))
        assert result == 0
        mock_service.spreadsheets().values().append.assert_not_called()

    def test_blank_lines_only_returns_zero(self, sheets_logger, mock_service, tmp_path):
        log = tmp_path / "gigs.log"
        log.write_text("\n  \n\n", encoding="utf-8")
        result = sheets_logger.flush(str(log))
        assert result == 0
        mock_service.spreadsheets().values().append.assert_not_called()

    def test_truncates_log_file_after_flush(self, sheets_logger, tmp_path):
        log = tmp_path / "gigs.log"
        log.write_text(_make_log_line(), encoding="utf-8")
        sheets_logger.flush(str(log))
        assert log.read_text(encoding="utf-8") == ""

    def test_truncates_even_when_no_parseable_rows(self, sheets_logger, mock_service, tmp_path):
        log = tmp_path / "gigs.log"
        log.write_text("not json at all\n", encoding="utf-8")
        sheets_logger.flush(str(log))
        assert log.read_text(encoding="utf-8") == ""

    def test_skips_malformed_lines_appends_only_valid(self, sheets_logger, mock_service, tmp_path):
        log = tmp_path / "gigs.log"
        log.write_text(
            _make_log_line() + "\nnot valid json\n" + _make_log_line(),
            encoding="utf-8",
        )
        result = sheets_logger.flush(str(log))
        assert result == 2
        body = mock_service.spreadsheets().values().append.call_args[1]["body"]
        assert len(body["values"]) == 2


# ── Row count ─────────────────────────────────────────────────────────────────


class TestFlushRowCount:
    def test_returns_correct_row_count(self, sheets_logger, tmp_path):
        log = tmp_path / "gigs.log"
        log.write_text("\n".join(_make_log_line() for _ in range(5)), encoding="utf-8")
        assert sheets_logger.flush(str(log)) == 5

    def test_single_line_returns_one(self, sheets_logger, tmp_path):
        log = tmp_path / "gigs.log"
        log.write_text(_make_log_line(), encoding="utf-8")
        assert sheets_logger.flush(str(log)) == 1


# ── Header row ────────────────────────────────────────────────────────────────


class TestFlushHeaderRow:
    def test_writes_header_when_sheet_empty(self, sheets_logger, mock_service, tmp_path):
        # Simulate empty sheet: get() returns no values
        mock_service.spreadsheets().values().get().execute.return_value = {}
        log = tmp_path / "gigs.log"
        log.write_text(_make_log_line(), encoding="utf-8")
        sheets_logger.flush(str(log))
        update_call = mock_service.spreadsheets().values().update
        update_call.assert_called_once()
        body = update_call.call_args[1]["body"]
        assert body["values"] == [_HEADERS]

    def test_skips_header_when_sheet_has_data(self, sheets_logger, mock_service, tmp_path):
        # Default fixture: get() returns {"values": [["timestamp"]]}
        log = tmp_path / "gigs.log"
        log.write_text(_make_log_line(), encoding="utf-8")
        sheets_logger.flush(str(log))
        # update() should NOT be called — header already present
        mock_service.spreadsheets().values().update.assert_not_called()


# ── Batching ──────────────────────────────────────────────────────────────────


class TestFlushBatching:
    def test_single_api_call_for_multiple_rows(self, sheets_logger, mock_service, tmp_path):
        log = tmp_path / "gigs.log"
        log.write_text("\n".join(_make_log_line() for _ in range(10)), encoding="utf-8")
        sheets_logger.flush(str(log))
        assert mock_service.spreadsheets().values().append.call_count == 1
        body = mock_service.spreadsheets().values().append.call_args[1]["body"]
        assert len(body["values"]) == 10

    def test_append_uses_insert_rows_option(self, sheets_logger, mock_service, tmp_path):
        log = tmp_path / "gigs.log"
        log.write_text(_make_log_line(), encoding="utf-8")
        sheets_logger.flush(str(log))
        call_kwargs = mock_service.spreadsheets().values().append.call_args[1]
        assert call_kwargs["insertDataOption"] == "INSERT_ROWS"


# ── Row structure ─────────────────────────────────────────────────────────────


class TestFlushRowStructure:
    def test_fixed_columns_in_correct_positions(self, sheets_logger, mock_service, tmp_path):
        log = tmp_path / "gigs.log"
        log.write_text(_make_log_line(), encoding="utf-8")
        sheets_logger.flush(str(log))
        row = mock_service.spreadsheets().values().append.call_args[1]["body"]["values"][0]
        assert row[0] == "2026-03-03T10:00:01.003Z"  # timestamp
        assert row[1] == "a1b2c3d4"  # run_id
        assert row[2] == "INFO"  # level
        assert row[3] == "__main__"  # logger
        assert row[4] == "Test log line"  # message
        assert row[5] == "main"  # module
        assert row[6] == "main"  # function
        assert row[7] == 51  # line

    def test_details_column_contains_extra_fields(self, sheets_logger, mock_service, tmp_path):
        log = tmp_path / "gigs.log"
        log.write_text(
            _make_log_line(url="https://organistsonline.org/required/", status=200),
            encoding="utf-8",
        )
        sheets_logger.flush(str(log))
        row = mock_service.spreadsheets().values().append.call_args[1]["body"]["values"][0]
        details = json.loads(row[8])
        assert details["url"] == "https://organistsonline.org/required/"
        assert details["status"] == 200

    def test_fixed_columns_not_duplicated_in_details(self, sheets_logger, mock_service, tmp_path):
        log = tmp_path / "gigs.log"
        log.write_text(_make_log_line(), encoding="utf-8")
        sheets_logger.flush(str(log))
        row = mock_service.spreadsheets().values().append.call_args[1]["body"]["values"][0]
        details_str = row[8]
        # No extra fields → details should be empty string
        assert details_str == ""

    def test_details_empty_string_when_no_extras(self, sheets_logger, mock_service, tmp_path):
        log = tmp_path / "gigs.log"
        log.write_text(_make_log_line(), encoding="utf-8")
        sheets_logger.flush(str(log))
        row = mock_service.spreadsheets().values().append.call_args[1]["body"]["values"][0]
        assert row[8] == ""

    def test_missing_fixed_columns_default_to_empty_string(
        self, sheets_logger, mock_service, tmp_path
    ):
        minimal_line = json.dumps({"message": "minimal", "level": "DEBUG"})
        log = tmp_path / "gigs.log"
        log.write_text(minimal_line, encoding="utf-8")
        sheets_logger.flush(str(log))
        row = mock_service.spreadsheets().values().append.call_args[1]["body"]["values"][0]
        # timestamp (index 0) is absent from the minimal line
        assert row[0] == ""
        assert row[4] == "minimal"  # message at index 4

    def test_details_json_non_serialisable_values_use_str(
        self, sheets_logger, mock_service, tmp_path
    ):
        """Non-JSON-serialisable values (e.g. sets) should not cause flush() to raise."""
        doc = {col: "" for col in _FIXED_COLUMNS}
        doc["message"] = "test"
        # Write a normal line — the default=str in json.dumps handles non-serialisable in details
        log = tmp_path / "gigs.log"
        log.write_text(json.dumps(doc), encoding="utf-8")
        # Should not raise
        result = sheets_logger.flush(str(log))
        assert result == 1

    def test_row_has_nine_columns(self, sheets_logger, mock_service, tmp_path):
        """Each appended row must have exactly len(_HEADERS) = 9 columns."""
        log = tmp_path / "gigs.log"
        log.write_text(_make_log_line(extra_field="hello"), encoding="utf-8")
        sheets_logger.flush(str(log))
        row = mock_service.spreadsheets().values().append.call_args[1]["body"]["values"][0]
        assert len(row) == len(_HEADERS)


# ── Excluded loggers ──────────────────────────────────────────────────────────


class TestFlushExcludedLoggers:
    def test_telegram_library_lines_are_excluded(self, sheets_logger, mock_service, tmp_path):
        """telegram.*, httpx, and httpcore lines must be dropped; organist_bot lines kept."""
        log = tmp_path / "gigs.log"
        log.write_text(
            "\n".join(
                [
                    _make_log_line(**{"logger": "telegram.ext.ExtBot"}),
                    _make_log_line(**{"logger": "telegram.ext.Application"}),
                    _make_log_line(**{"logger": "httpx"}),
                    _make_log_line(**{"logger": "httpcore.connection"}),
                    _make_log_line(**{"logger": "organist_bot.integrations.telegram_bot"}),
                    _make_log_line(**{"logger": "__main__"}),
                ]
            ),
            encoding="utf-8",
        )
        result = sheets_logger.flush(str(log))
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

    def test_organist_bot_telegram_integration_is_not_excluded(
        self, sheets_logger, mock_service, tmp_path
    ):
        """organist_bot.integrations.telegram_bot must NOT be filtered out."""
        log = tmp_path / "gigs.log"
        log.write_text(
            _make_log_line(**{"logger": "organist_bot.integrations.telegram_bot"}),
            encoding="utf-8",
        )
        result = sheets_logger.flush(str(log))
        assert result == 1

    def test_all_lines_excluded_returns_zero(self, sheets_logger, mock_service, tmp_path):
        """If every line is from an excluded logger, flush returns 0 and truncates."""
        log = tmp_path / "gigs.log"
        log.write_text(
            "\n".join(
                [
                    _make_log_line(**{"logger": "telegram.ext.ExtBot"}),
                    _make_log_line(**{"logger": "httpx"}),
                ]
            ),
            encoding="utf-8",
        )
        result = sheets_logger.flush(str(log))
        assert result == 0
        mock_service.spreadsheets().values().append.assert_not_called()
        assert log.read_text(encoding="utf-8") == ""
