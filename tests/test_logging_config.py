"""Tests for organist_bot/logging_config.py — JSONFormatter and RunIdFilter."""

import json
import logging

import pytest

from organist_bot.logging_config import _STDLIB_FIELDS, JSONFormatter, RunIdFilter, set_run_id


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


# ── JSONFormatter ─────────────────────────────────────────────────────────────


class TestJSONFormatter:
    def _format(self, record: logging.LogRecord) -> dict:
        formatter = JSONFormatter()
        line = formatter.format(record)
        return json.loads(line)

    def test_core_fields_present(self):
        """The formatter always emits the required structural fields."""
        record = _make_record("test message")
        doc = self._format(record)

        assert doc["message"] == "test message"
        assert doc["level"] == "INFO"
        assert doc["logger"] == "test.logger"
        assert "timestamp" in doc
        assert "module" in doc
        assert "function" in doc
        assert "line" in doc

    def test_extra_attribute_included(self):
        """A caller-supplied extra attribute (not in _STDLIB_FIELDS) appears in the output."""
        record = _make_record("gig found", gig_count=7)
        doc = self._format(record)
        assert doc["gig_count"] == 7

    def test_multiple_extra_attributes_included(self):
        """Multiple extra attributes all appear in the output."""
        record = _make_record("run done", run_id="abc123", elapsed_ms=250)
        doc = self._format(record)
        assert doc["elapsed_ms"] == 250
        # run_id is in _STDLIB_FIELDS (injected by RunIdFilter), so it's handled as
        # a standard field rather than an extra — it still appears but via the fixed slot
        assert doc["run_id"] == "abc123"

    def test_stdlib_noise_fields_excluded(self):
        """Fields that are part of LogRecord internals do not leak into the JSON output."""
        record = _make_record("noise check")
        doc = self._format(record)
        # Sample several members of the exclusion set that should never appear as top-level keys
        stdlib_noise = {
            "args",
            "msg",
            "levelno",
            "msecs",
            "relativeCreated",
            "exc_text",
            "stack_info",
        }
        for field in stdlib_noise:
            assert field not in doc, f"stdlib field {field!r} leaked into JSON output"

    def test_output_is_valid_json(self):
        """The formatter always produces a single parseable JSON object."""
        record = _make_record("json check", level=logging.WARNING, extra_key="value")
        line = JSONFormatter().format(record)
        # Must not raise
        doc = json.loads(line)
        assert isinstance(doc, dict)

    def test_exception_serialised_as_string(self):
        """When exc_info is present, the 'exception' key contains the traceback text."""
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            exc_info = sys.exc_info()

        record = _make_record("with exc")
        record.exc_info = exc_info
        doc = self._format(record)
        assert "exception" in doc
        assert "ValueError" in doc["exception"]
        assert "boom" in doc["exception"]

    @pytest.mark.parametrize("field", list(_STDLIB_FIELDS - {"run_id", "message"}))
    def test_no_stdlib_field_leaks(self, field):
        """Every member of _STDLIB_FIELDS (except the intentionally-emitted ones) is excluded."""
        # The formatter intentionally surfaces "message" and "run_id" as fixed keys;
        # all other _STDLIB_FIELDS members must not appear as extra keys.
        record = _make_record("stdlib exclusion test")
        doc = self._format(record)
        # Only check fields that are not part of the fixed schema
        fixed_schema = {
            "timestamp",
            "run_id",
            "level",
            "logger",
            "message",
            "module",
            "function",
            "line",
        }
        if field not in fixed_schema:
            assert field not in doc, f"_STDLIB_FIELDS member {field!r} leaked into JSON output"


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

    def test_json_formatter_picks_up_run_id_after_filter(self):
        """Integration: RunIdFilter + JSONFormatter round-trip produces correct run_id in output."""
        set_run_id("int_test_99")
        record = _make_record("integrated")
        RunIdFilter().filter(record)  # stamp the record first
        doc = json.loads(JSONFormatter().format(record))
        assert doc["run_id"] == "int_test_99"
