"""Tests for organist_bot/logging_config.py — structlog-based formatters and RunIdFilter."""

import json
import logging
import sys

import pytest
from structlog.stdlib import ProcessorFormatter

from organist_bot.logging_config import (
    RunIdFilter,
    _build_json_formatter,
    _select_console_formatter,
    set_run_id,
)


def _make_record(msg: str = "hello", level: int = logging.INFO, **extra) -> logging.LogRecord:
    """Build a LogRecord, optionally injecting caller-supplied extra attributes."""
    record = logging.LogRecord(
        name="test.logger",
        level=level,
        pathname="/fake/path.py",
        lineno=42,
        msg=msg,
        args=(),
        exc_info=None,
    )
    for k, v in extra.items():
        setattr(record, k, v)
    return record


# ── JSON formatter pipeline ─────────────────────────────────────────────────


class TestJSONFormatterPipeline:
    def _format(self, record: logging.LogRecord) -> dict:
        formatter = _build_json_formatter()
        line = formatter.format(record)
        return json.loads(line)

    def test_core_fields_present(self):
        """The pipeline always emits the required structural fields."""
        record = _make_record("test message")
        doc = self._format(record)

        assert doc["message"] == "test message"
        assert doc["level"] == "info"
        assert doc["logger"] == "test.logger"
        assert "timestamp" in doc
        assert doc["module"] == "path"
        assert doc["function"] is None or isinstance(doc["function"], str)
        assert doc["line"] == 42

    def test_extra_attribute_included(self):
        """A caller-supplied extra attribute appears in the output."""
        record = _make_record("gig found", gig_count=7)
        doc = self._format(record)
        assert doc["gig_count"] == 7

    def test_multiple_extra_attributes_included(self):
        """Multiple extra attributes all appear in the output."""
        record = _make_record("run done", run_id="abc123", elapsed_ms=250)
        doc = self._format(record)
        assert doc["elapsed_ms"] == 250
        assert doc["run_id"] == "abc123"

    def test_stdlib_noise_fields_excluded(self):
        """Fields that are part of LogRecord internals do not leak into the JSON output."""
        record = _make_record("noise check")
        doc = self._format(record)
        stdlib_noise = {
            "args",
            "msg",
            "levelno",
            "msecs",
            "relativeCreated",
            "exc_text",
            "stack_info",
            "_record",
            "_from_structlog",
        }
        for field in stdlib_noise:
            assert field not in doc, f"stdlib field {field!r} leaked into JSON output"

    def test_output_is_valid_json(self):
        """The pipeline always produces a single parseable JSON object."""
        record = _make_record("json check", level=logging.WARNING, extra_key="value")
        line = _build_json_formatter().format(record)
        doc = json.loads(line)
        assert isinstance(doc, dict)

    def test_exception_serialised_as_structured_list(self):
        """When exc_info is present, 'exception' is a list of frame dicts, not a string."""
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            exc_info = sys.exc_info()

        record = _make_record("with exc")
        record.exc_info = exc_info
        doc = self._format(record)

        assert isinstance(doc["exception"], list)
        assert doc["exception"][0]["exc_type"] == "ValueError"
        assert doc["exception"][0]["exc_value"] == "boom"

    def test_exception_never_includes_locals(self):
        """show_locals=False: no stack frame carries a 'locals' key, regardless of scope."""
        secret_token = "super-secret-value"  # noqa: F841 — deliberately in scope for the assertion

        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            exc_info = sys.exc_info()

        record = _make_record("with exc")
        record.exc_info = exc_info
        doc = self._format(record)

        for frame in doc["exception"][0]["frames"]:
            assert "locals" not in frame
        assert "super-secret-value" not in json.dumps(doc)

    def test_run_id_appears_via_run_id_filter(self):
        """Integration: RunIdFilter + JSON pipeline round-trip produces correct run_id."""
        set_run_id("int_test_99")
        record = _make_record("integrated")
        RunIdFilter().filter(record)  # stamp the record first, same as a real handler chain
        doc = self._format(record)
        assert doc["run_id"] == "int_test_99"


# ── RunIdFilter ───────────────────────────────────────────────────────────────


class TestRunIdFilter:
    def test_filter_injects_run_id_set_via_set_run_id(self):
        """After set_run_id('abc'), RunIdFilter stamps every record with run_id='abc'."""
        set_run_id("abc123")
        record = _make_record("stamped")
        f = RunIdFilter()
        result = f.filter(record)

        assert result is True  # filter must not block the record
        assert record.run_id == "abc123"

    def test_filter_injects_empty_string_when_no_run_id_set(self):
        """Before any set_run_id call (or after empty string), run_id is '' on the record."""
        set_run_id("")
        record = _make_record("no run")
        f = RunIdFilter()
        f.filter(record)

        assert record.run_id == ""

    def test_filter_run_id_changes_between_runs(self):
        """Changing the run_id mid-session propagates to new records."""
        set_run_id("first")
        r1 = _make_record("run 1")
        f = RunIdFilter()
        f.filter(r1)
        assert r1.run_id == "first"

        set_run_id("second")
        r2 = _make_record("run 2")
        f.filter(r2)
        assert r2.run_id == "second"

    def test_filter_always_returns_true(self):
        """RunIdFilter is transparent — it never drops records."""
        set_run_id("x")
        record = _make_record("transparent")
        f = RunIdFilter()
        assert f.filter(record) is True


# ── Console formatter selection ────────────────────────────────────────────────


class TestSelectConsoleFormatter:
    def test_non_tty_returns_json_pipeline(self):
        """is_tty=False must produce valid, parseable JSON (matches the file pipeline)."""
        formatter = _select_console_formatter(is_tty=False)
        record = _make_record("check")
        line = formatter.format(record)
        doc = json.loads(line)  # must not raise
        assert doc["message"] == "check"

    def test_tty_returns_console_pipeline(self):
        """is_tty=True must produce human-readable text, not JSON."""
        formatter = _select_console_formatter(is_tty=True)
        record = _make_record("check")
        line = formatter.format(record)
        with pytest.raises(json.JSONDecodeError):
            json.loads(line)
        assert "check" in line


# ── setup_logging() wiring ──────────────────────────────────────────────────────


@pytest.fixture
def _clean_root_logger():
    """Give setup_logging() a root logger with no handlers, and restore the
    original ones afterwards so this test can't leak state into others."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    root.handlers = []
    yield root
    root.handlers = saved_handlers
    root.setLevel(saved_level)


class TestSetupLoggingConsoleSelection:
    def test_tty_stdout_attaches_console_pipeline(self, monkeypatch, tmp_path, _clean_root_logger):
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        from organist_bot.logging_config import setup_logging

        # pytest's own log-capture handler re-attaches to the root logger right as
        # the "call" phase starts — i.e. after _clean_root_logger's fixture body
        # runs but before this test body gets control — which would otherwise trip
        # setup_logging()'s idempotency guard. Clear it here, immediately before
        # exercising setup_logging(), so the guard sees the empty root logger the
        # fixture actually intended.
        _clean_root_logger.handlers = []
        setup_logging(str(tmp_path / "gigs.log"))

        console_handler = _clean_root_logger.handlers[0]
        assert isinstance(console_handler.formatter, ProcessorFormatter)
        line = console_handler.formatter.format(_make_record("tty wiring check"))
        with pytest.raises(json.JSONDecodeError):
            json.loads(line)

    def test_non_tty_stdout_attaches_json_pipeline(self, monkeypatch, tmp_path, _clean_root_logger):
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
        from organist_bot.logging_config import setup_logging

        # See the comment in test_tty_stdout_attaches_console_pipeline above.
        _clean_root_logger.handlers = []
        setup_logging(str(tmp_path / "gigs.log"))

        console_handler = _clean_root_logger.handlers[0]
        doc = json.loads(console_handler.formatter.format(_make_record("non-tty wiring check")))
        assert doc["message"] == "non-tty wiring check"


class TestSetupLoggingCaplogCompatibility:
    def test_caplog_still_captures_after_setup_logging_runs(
        self, monkeypatch, tmp_path, caplog, _clean_root_logger
    ):
        """setup_logging() must not interfere with pytest's own log-capture handler,
        and its own production handlers must genuinely be the ones doing the work.

        Clearing _clean_root_logger.handlers (as in TestSetupLoggingConsoleSelection)
        is required so setup_logging()'s idempotency guard doesn't no-op — but that
        also strips pytest's own capture handler off the root logger. Grab a
        reference to it first, via caplog.handler, and re-attach it *after*
        setup_logging() runs, so caplog's capture path and setup_logging()'s real
        console/file handlers are all live on the root logger simultaneously. That
        way this test actually fails if setup_logging() is broken or a no-op —
        unlike asserting on caplog.records alone, which caplog would satisfy by
        itself even with setup_logging() deleted.
        """
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
        from organist_bot.logging_config import setup_logging

        caplog_handler = caplog.handler
        _clean_root_logger.handlers = []
        log_file = tmp_path / "gigs.log"
        setup_logging(str(log_file))
        _clean_root_logger.addHandler(caplog_handler)

        logger = logging.getLogger("organist_bot.test_caplog_compat")
        with caplog.at_level(logging.INFO, logger="organist_bot.test_caplog_compat"):
            logger.info("caplog compatibility check")

        assert any(r.message == "caplog compatibility check" for r in caplog.records)

        # Prove setup_logging()'s own rotating file handler (DEBUG+) was genuinely
        # attached and processing records, not merely bypassed by the re-attached
        # caplog handler.
        assert "caplog compatibility check" in log_file.read_text()
