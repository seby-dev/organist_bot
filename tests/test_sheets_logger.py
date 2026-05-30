# tests/test_sheets_logger.py
"""Tests for SheetsLogger (in-memory logging.Handler — emit + drain)."""

import datetime
import json
import logging
from unittest.mock import MagicMock, patch

import pytest

from organist_bot.integrations.sheets_logger import (
    _CELL_THRESHOLD,
    _EXCLUDED_LOGGER_PREFIXES,
    _HEADERS,
    _NUM_COLS,
    SheetsLogger,
    _latest_log_sheet,
    _parse_last_row,
)

# ── Fixtures ───────────────────────────────────────────────────────────────────

# A known UTC instant used to verify timestamp formatting in row assertions.
_KNOWN_TS = datetime.datetime(2026, 3, 3, 10, 0, 1, 3000, tzinfo=datetime.UTC)
_KNOWN_TS_STR = "2026-03-03T10:00:01.003Z"


def _make_append_response(sheet: str = "Logs", last_row: int = 10) -> dict:
    """Build a minimal Sheets append() response with a realistic updatedRange."""
    col = chr(ord("A") + _NUM_COLS - 1)  # "I" for 9 columns
    return {"updates": {"updatedRange": f"{sheet}!A1:{col}{last_row}"}}


@pytest.fixture
def mock_service():
    svc = MagicMock()
    # Default: sheet has a header row (A1:A1 returns a value).
    svc.spreadsheets().values().get().execute.return_value = {"values": [["timestamp"]]}
    # Default: spreadsheet has only the "Logs" sheet (used by _resolve_active_sheet).
    svc.spreadsheets().get().execute.return_value = {"sheets": [{"properties": {"title": "Logs"}}]}
    # Default append response — well below the cell threshold.
    # Use .return_value rather than calling append() to avoid counting as a call.
    svc.spreadsheets().values().append.return_value.execute.return_value = _make_append_response(
        "Logs", last_row=10
    )
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


# ── Buffer cap ──────────────────────────────────────────────────────────────────


class TestBufferCap:
    def test_emit_bounds_buffer_and_drops_oldest(self, sheets_logger, monkeypatch):
        """Once the cap is exceeded, emit() drops the OLDEST rows and counts them."""
        monkeypatch.setattr("organist_bot.integrations.sheets_logger._MAX_BUFFER_ROWS", 10)
        for i in range(25):
            sheets_logger.emit(_make_record(msg=f"line {i}"))
        assert len(sheets_logger._buffer) == 10
        assert sheets_logger._dropped == 15
        # The 10 retained rows are the most recent (lines 15..24) — message is col index 4.
        retained = [row[4] for row in sheets_logger._buffer]
        assert retained == [f"line {i}" for i in range(15, 25)]

    def test_drain_alerts_and_resets_dropped_count(self, sheets_logger, monkeypatch):
        """drain() reports the dropped count once via alert, then resets it."""
        monkeypatch.setattr("organist_bot.integrations.sheets_logger._MAX_BUFFER_ROWS", 5)
        calls: list[str] = []
        monkeypatch.setattr(
            "organist_bot.integrations.sheets_logger.alert.send_alert",
            lambda m: calls.append(m),
        )
        for _ in range(8):  # 3 over the cap → 3 dropped
            sheets_logger.emit(_make_record())
        assert sheets_logger._dropped == 3
        sheets_logger.drain()
        assert any("dropped 3" in c for c in calls)
        assert sheets_logger._dropped == 0  # reset after reporting

    def test_restore_path_stays_bounded_on_persistent_failure(
        self, sheets_logger, mock_service, monkeypatch
    ):
        """If the API keeps failing, the restore path must not grow the buffer past the cap."""
        monkeypatch.setattr("organist_bot.integrations.sheets_logger._MAX_BUFFER_ROWS", 10)
        mock_service.spreadsheets().values().get().execute.side_effect = RuntimeError("down")
        for _ in range(10):
            sheets_logger.emit(_make_record())
        with pytest.raises(RuntimeError):
            sheets_logger.drain()  # rows restored
        # Emit more while still "down" — buffer must stay capped.
        for _ in range(10):
            sheets_logger.emit(_make_record())
        assert len(sheets_logger._buffer) <= 10


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


# ── Helper functions ───────────────────────────────────────────────────────────


class TestLatestLogSheet:
    def test_returns_logs_when_only_sheet(self):
        assert _latest_log_sheet(["Logs"]) == "Logs"

    def test_returns_highest_numbered_sheet(self):
        assert _latest_log_sheet(["Logs", "Logs 2", "Logs 3"]) == "Logs 3"

    def test_returns_logs_when_no_log_sheets(self):
        assert _latest_log_sheet(["Sheet1", "Data"]) == "Logs"

    def test_empty_list_returns_default(self):
        assert _latest_log_sheet([]) == "Logs"

    def test_ignores_non_logs_sheets(self):
        assert _latest_log_sheet(["Logs", "Logs 2", "Archive", "Sheet1"]) == "Logs 2"

    def test_handles_out_of_order_sheets(self):
        assert _latest_log_sheet(["Logs 3", "Logs", "Logs 2"]) == "Logs 3"


class TestParseLastRow:
    def test_parses_standard_range(self):
        response = {"updates": {"updatedRange": "Logs!A1001:I1010"}}
        assert _parse_last_row(response) == 1010

    def test_parses_single_row_range(self):
        response = {"updates": {"updatedRange": "Logs!A5:I5"}}
        assert _parse_last_row(response) == 5

    def test_returns_none_for_missing_updates(self):
        assert _parse_last_row({}) is None

    def test_returns_none_for_empty_range(self):
        assert _parse_last_row({"updates": {"updatedRange": ""}}) is None

    def test_handles_multi_digit_sheet_name(self):
        response = {"updates": {"updatedRange": "Logs 12!A1:I99999"}}
        assert _parse_last_row(response) == 99999


# ── Sheet rotation ─────────────────────────────────────────────────────────────


class TestSheetRotation:
    def test_no_rotation_below_threshold(self, sheets_logger, mock_service):
        """drain() must NOT create a new sheet when below the cell threshold."""
        sheets_logger.emit(_make_record())
        sheets_logger.drain()
        mock_service.spreadsheets().batchUpdate.assert_not_called()
        assert sheets_logger._active_sheet == "Logs"

    def test_rotates_when_threshold_reached(self, sheets_logger, mock_service):
        """drain() creates 'Logs 2' and switches active sheet when last_row hits threshold."""
        rows_at_threshold = _CELL_THRESHOLD // _NUM_COLS
        mock_service.spreadsheets().values().append.return_value.execute.return_value = (
            _make_append_response("Logs", last_row=rows_at_threshold)
        )
        mock_service.spreadsheets().get().execute.return_value = {
            "sheets": [{"properties": {"title": "Logs"}}]
        }
        sheets_logger.emit(_make_record())
        sheets_logger.drain()

        mock_service.spreadsheets().batchUpdate.assert_called_once()
        request_body = mock_service.spreadsheets().batchUpdate.call_args[1]["body"]
        new_title = request_body["requests"][0]["addSheet"]["properties"]["title"]
        assert new_title == "Logs 2"
        assert sheets_logger._active_sheet == "Logs 2"

    def test_next_drain_targets_new_sheet(self, sheets_logger, mock_service):
        """After rotation, the next drain() appends to the new sheet."""
        rows_at_threshold = _CELL_THRESHOLD // _NUM_COLS
        mock_service.spreadsheets().values().append.return_value.execute.return_value = (
            _make_append_response("Logs", last_row=rows_at_threshold)
        )
        mock_service.spreadsheets().get().execute.return_value = {
            "sheets": [{"properties": {"title": "Logs"}}]
        }
        sheets_logger.emit(_make_record())
        sheets_logger.drain()

        # Force second drain to use a normal (non-threshold) response.
        mock_service.spreadsheets().values().append.return_value.execute.return_value = (
            _make_append_response("Logs 2", last_row=1)
        )
        sheets_logger.emit(_make_record())
        sheets_logger.drain()

        # Second append call should target "Logs 2".
        second_call_kwargs = mock_service.spreadsheets().values().append.call_args_list[-1][1]
        assert second_call_kwargs["range"].startswith("Logs 2!")


# ── query_run_stats ───────────────────────────────────────────────────────────


def _make_sheets_logger_for_query(mock_service) -> SheetsLogger:
    """Construct a SheetsLogger pointing at mock_service for query tests."""
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
        return SheetsLogger(spreadsheet_id="sid", credentials_file="fake.json")


class TestQueryRunStats:
    def _mock_service_with_rows(self, rows: list[list[str]]) -> MagicMock:
        svc = MagicMock()
        svc.spreadsheets().get().execute.return_value = {
            "sheets": [{"properties": {"title": "Logs"}}]
        }
        svc.spreadsheets().values().get().execute.return_value = {"values": rows}
        return svc

    def test_returns_empty_when_no_rows(self):
        svc = self._mock_service_with_rows([])
        sl = _make_sheets_logger_for_query(svc)
        assert sl.query_run_stats(7) == []

    def test_returns_empty_when_only_header(self):
        header = [
            [
                "timestamp",
                "run_id",
                "level",
                "logger",
                "message",
                "module",
                "function",
                "line",
                "details",
            ]
        ]
        svc = self._mock_service_with_rows(header)
        sl = _make_sheets_logger_for_query(svc)
        assert sl.query_run_stats(7) == []

    def test_aggregates_run_from_two_messages(self):
        import json as _json

        now = datetime.datetime.now(datetime.UTC)
        ts = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        header = [
            [
                "timestamp",
                "run_id",
                "level",
                "logger",
                "message",
                "module",
                "function",
                "line",
                "details",
            ]
        ]
        scrape_row = [
            ts,
            "abc12345",
            "INFO",
            "m",
            "Scraping complete",
            "m",
            "f",
            "1",
            _json.dumps(
                {
                    "listed": 20,
                    "pre_filter_passed": 3,
                    "scraped": 3,
                    "gig_errors": 0,
                    "elapsed_ms": 500,
                }
            ),
        ]
        summary_row = [
            ts,
            "abc12345",
            "INFO",
            "m",
            "Run summary",
            "m",
            "f",
            "2",
            _json.dumps(
                {"scraped": 3, "valid": 1, "notified": 1, "gig_errors": 0, "elapsed_ms": 800}
            ),
        ]
        svc = self._mock_service_with_rows([header[0], scrape_row, summary_row])
        sl = _make_sheets_logger_for_query(svc)
        runs = sl.query_run_stats(7)
        assert len(runs) == 1
        r = runs[0]
        assert r["run_id"] == "abc12345"
        assert r["listed"] == 20
        assert r["valid"] == 1
        assert r["gig_errors"] == 0

    def test_includes_filter_breakdown(self):
        import json as _json

        now = datetime.datetime.now(datetime.UTC)
        ts = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        header = [
            [
                "timestamp",
                "run_id",
                "level",
                "logger",
                "message",
                "module",
                "function",
                "line",
                "details",
            ]
        ]
        summary_row = [
            ts,
            "run1",
            "INFO",
            "m",
            "Run summary",
            "m",
            "f",
            "1",
            _json.dumps(
                {"scraped": 5, "valid": 0, "notified": 0, "gig_errors": 0, "elapsed_ms": 300}
            ),
        ]
        filter_row = [
            ts,
            "run1",
            "INFO",
            "m",
            "Filter chain applied",
            "m",
            "f",
            "2",
            _json.dumps({"filter_breakdown": {"SeenFilter": 10, "FeeFilter": 3}}),
        ]
        svc = self._mock_service_with_rows([header[0], summary_row, filter_row])
        sl = _make_sheets_logger_for_query(svc)
        runs = sl.query_run_stats(7)
        assert runs[0]["filter_breakdown"] == {"SeenFilter": 10, "FeeFilter": 3}

    def test_excludes_incomplete_runs(self):
        import json as _json

        now = datetime.datetime.now(datetime.UTC)
        ts = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        header = [
            [
                "timestamp",
                "run_id",
                "level",
                "logger",
                "message",
                "module",
                "function",
                "line",
                "details",
            ]
        ]
        scrape_row = [
            ts,
            "incomplete",
            "INFO",
            "m",
            "Scraping complete",
            "m",
            "f",
            "1",
            _json.dumps(
                {
                    "listed": 5,
                    "pre_filter_passed": 1,
                    "scraped": 1,
                    "gig_errors": 0,
                    "elapsed_ms": 200,
                }
            ),
        ]
        svc = self._mock_service_with_rows([header[0], scrape_row])
        sl = _make_sheets_logger_for_query(svc)
        assert sl.query_run_stats(7) == []

    def test_filters_old_rows_by_days(self):
        import json as _json

        old_ts = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=10)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )
        header = [
            [
                "timestamp",
                "run_id",
                "level",
                "logger",
                "message",
                "module",
                "function",
                "line",
                "details",
            ]
        ]
        summary_row = [
            old_ts,
            "old_run",
            "INFO",
            "m",
            "Run summary",
            "m",
            "f",
            "1",
            _json.dumps(
                {"scraped": 1, "valid": 0, "notified": 0, "gig_errors": 0, "elapsed_ms": 200}
            ),
        ]
        svc = self._mock_service_with_rows([header[0], summary_row])
        sl = _make_sheets_logger_for_query(svc)
        assert sl.query_run_stats(7) == []

    def test_results_sorted_newest_first(self):
        import json as _json

        now = datetime.datetime.now(datetime.UTC)
        ts1 = (now - datetime.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        ts2 = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        header = [
            [
                "timestamp",
                "run_id",
                "level",
                "logger",
                "message",
                "module",
                "function",
                "line",
                "details",
            ]
        ]
        row1 = [
            ts1,
            "run_old",
            "INFO",
            "m",
            "Run summary",
            "m",
            "f",
            "1",
            _json.dumps(
                {"scraped": 1, "valid": 0, "notified": 0, "gig_errors": 0, "elapsed_ms": 100}
            ),
        ]
        row2 = [
            ts2,
            "run_new",
            "INFO",
            "m",
            "Run summary",
            "m",
            "f",
            "2",
            _json.dumps(
                {"scraped": 2, "valid": 1, "notified": 1, "gig_errors": 0, "elapsed_ms": 200}
            ),
        ]
        svc = self._mock_service_with_rows([header[0], row1, row2])
        sl = _make_sheets_logger_for_query(svc)
        runs = sl.query_run_stats(7)
        assert runs[0]["run_id"] == "run_new"
        assert runs[1]["run_id"] == "run_old"

    def test_sequential_rotation_creates_logs_3(self, sheets_logger, mock_service):
        """When 'Logs 2' already exists, next rotation creates 'Logs 3'."""
        rows_at_threshold = _CELL_THRESHOLD // _NUM_COLS
        mock_service.spreadsheets().values().append.return_value.execute.return_value = (
            _make_append_response("Logs 2", last_row=rows_at_threshold)
        )
        mock_service.spreadsheets().get().execute.return_value = {
            "sheets": [
                {"properties": {"title": "Logs"}},
                {"properties": {"title": "Logs 2"}},
            ]
        }
        sheets_logger._active_sheet = "Logs 2"  # simulate already-rotated state
        sheets_logger.emit(_make_record())
        sheets_logger.drain()

        request_body = mock_service.spreadsheets().batchUpdate.call_args[1]["body"]
        new_title = request_body["requests"][0]["addSheet"]["properties"]["title"]
        assert new_title == "Logs 3"


class TestDrainAlerts:
    def test_sheets_api_failure_triggers_alert(self, sheets_logger, mock_service):
        """drain() API failure calls alert.send_alert before re-raising."""
        sheets_logger.emit(_make_record())
        mock_service.spreadsheets().values().get().execute.side_effect = Exception("quota exceeded")
        with patch("organist_bot.integrations.sheets_logger.alert") as mock_alert:
            with pytest.raises(Exception, match="quota exceeded"):
                sheets_logger.drain()
        mock_alert.send_alert.assert_called_once()
        assert "Sheets" in mock_alert.send_alert.call_args.args[0]


class TestActiveSheetResolution:
    def test_resolves_active_sheet_on_first_drain(self, sheets_logger, mock_service):
        """_active_sheet is None until the first drain() queries the spreadsheet."""
        assert sheets_logger._active_sheet is None
        sheets_logger.emit(_make_record())
        sheets_logger.drain()
        assert sheets_logger._active_sheet == "Logs"

    def test_uses_latest_sheet_after_restart(self, sheets_logger, mock_service):
        """If 'Logs 2' already exists, drain() targets it (simulates process restart)."""
        mock_service.spreadsheets().get().execute.return_value = {
            "sheets": [
                {"properties": {"title": "Logs"}},
                {"properties": {"title": "Logs 2"}},
            ]
        }
        sheets_logger.emit(_make_record())
        sheets_logger.drain()

        assert sheets_logger._active_sheet == "Logs 2"
        append_kwargs = mock_service.spreadsheets().values().append.call_args[1]
        assert append_kwargs["range"].startswith("Logs 2!")
