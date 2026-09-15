# Stage 1: Structured JSON Logging (structlog) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the hand-rolled `ConsoleFormatter`/`JSONFormatter` classes in `organist_bot/logging_config.py` with `structlog`-based formatters, so every non-interactive log destination (the rotating file, and stdout when it isn't a real terminal — e.g. under launchd/supervisord) emits structured JSON, while an interactive terminal still gets colorized human-readable output.

**Architecture:** A shared structlog `foreign_pre_chain` (every call site logs via plain stdlib `logging.getLogger(__name__)`, so every record is "foreign" to structlog) adds log level, extras, callsite info, and a timestamp. Two `structlog.stdlib.ProcessorFormatter` pipelines consume that chain: a JSON pipeline (file handler, and console when stdout isn't a tty) and a `ConsoleRenderer` pipeline (console when stdout is a tty). `setup_logging()` picks the console pipeline once via `sys.stdout.isatty()`. `RunIdFilter`, `set_run_id()`, and every application call site are untouched.

**Tech Stack:** Python 3.12, `structlog>=24.1`, stdlib `logging`, `pytest`/`caplog`.

**Spec:** `docs/superpowers/specs/2026-09-15-structured-json-logging-design.md`

## Global Constraints

- Zero call-site changes anywhere outside `organist_bot/logging_config.py`.
- `RunIdFilter` and `set_run_id()` are not modified.
- `setup_logging(log_file: str, level: int = logging.DEBUG) -> None` keeps its exact signature and idempotency guard (`if root.handlers: return`).
- JSON output preserves the existing field names: `timestamp`, `run_id`, `level`, `logger`, `message`, `module`, `function`, `line`, plus caller `extra=` fields merged at the top level. The one intentional exception: `exception` becomes a structured list of frames instead of a string.
- Exception frames never include local variable values (`show_locals=False`) — this is a deliberate secret-leak-prevention decision, not to be relaxed without a matching secret-scrubbing decision (Stage 2).
- `pytest`'s `caplog` fixture must keep capturing records after `setup_logging()` runs.
- Dependency floor: `structlog>=24.1`.

---

### Task 1: Add the `structlog` dependency

**Files:**
- Modify: `pyproject.toml:6-23` (`[project].dependencies` list)

**Interfaces:**
- Produces: the `structlog` package importable from the project's `.venv`, for every later task to build on.

- [ ] **Step 1: Add the dependency**

Edit `pyproject.toml`'s `dependencies` list (currently lines 6-23) to add one line, keeping alphabetical-ish grouping consistent with the existing list — append it at the end, right before the closing `]`:

```toml
    "google-auth-oauthlib>=1.0",
    "structlog>=24.1",
]
```

- [ ] **Step 2: Sync the environment**

Run: `uv sync`
Expected: resolves and installs `structlog` (26.x as of writing) into `.venv` with no errors.

- [ ] **Step 3: Sanity-check the import**

Run: `.venv/bin/python -c "import structlog; from structlog.stdlib import ProcessorFormatter, ExtraAdder, add_log_level; from structlog.processors import EventRenamer, ExceptionRenderer, JSONRenderer, TimeStamper; from structlog.tracebacks import ExceptionDictTransformer; from structlog.dev import ConsoleRenderer; print('ok')"`
Expected: prints `ok` with no `ImportError`.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "build: add structlog dependency for stage 1 structured logging"
```

---

### Task 2: JSON formatter pipeline (`_build_json_formatter`)

**Files:**
- Modify: `organist_bot/logging_config.py` — insert a new section immediately after the `RunIdFilter` class (currently ends at line 49, right before the `# ── ANSI colour palette` comment at line 52). Leave everything else in the file untouched for now — `ConsoleFormatter`, `JSONFormatter`, `_STDLIB_FIELDS`, and `setup_logging()` are all removed/rewired in Task 4, not here.
- Test: `tests/test_logging_config.py` (full rewrite of the file)

**Interfaces:**
- Consumes: nothing from other tasks yet (this task is self-contained, aside from the `structlog` import from Task 1).
- Produces: `_add_callsite(logger, name, event_dict) -> dict`, `_FOREIGN_PRE_CHAIN: list`, `_build_json_formatter() -> ProcessorFormatter` — Task 3 and Task 4 both call `_build_json_formatter()` and reuse `_FOREIGN_PRE_CHAIN`.

- [ ] **Step 1: Replace `tests/test_logging_config.py` with the new test file (red)**

```python
"""Tests for organist_bot/logging_config.py — structlog-based formatters and RunIdFilter."""

import json
import logging

import pytest

from organist_bot.logging_config import (
    RunIdFilter,
    _build_json_formatter,
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
```

Note: `test_core_fields_present` asserts `doc["module"] == "path"` because the fake `LogRecord` is built with `pathname="/fake/path.py"` — stdlib derives `record.module` from the pathname's stem, exactly as the old test suite relied on.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com .venv/bin/pytest tests/test_logging_config.py -v`
Expected: `ImportError: cannot import name '_build_json_formatter' from 'organist_bot.logging_config'` (collection error) — `TestRunIdFilter` tests haven't run yet either, since the whole module fails to import.

- [ ] **Step 3: Implement the JSON pipeline**

In `organist_bot/logging_config.py`, add these imports right after the existing `from pathlib import Path` line (line 30):

```python
from structlog.dev import ConsoleRenderer
from structlog.processors import EventRenamer, ExceptionRenderer, JSONRenderer, TimeStamper
from structlog.stdlib import ExtraAdder, ProcessorFormatter, add_log_level
from structlog.tracebacks import ExceptionDictTransformer
```

(`ConsoleRenderer` isn't used until Task 3, but importing everything needed for the whole formatter-pipeline section in one place keeps the import block stable across tasks — no need to re-touch it later.)

Then insert this new section immediately after the `RunIdFilter` class (i.e. right after its `filter()` method ends, before the `# ── ANSI colour palette` comment):

```python
# ── structlog processor chain ──────────────────────────────────────────────────
# Every call site in this codebase logs via plain stdlib logging.getLogger(name),
# so every record structlog sees here is "foreign" to it. foreign_pre_chain runs
# once per record before either final renderer below.


def _add_callsite(_logger: object, _name: str, event_dict: dict) -> dict:
    """Add module/function/line from the underlying LogRecord, using the field
    names this project's JSON logs have always used (structlog's own
    CallsiteParameterAdder uses different names)."""
    record = event_dict["_record"]
    event_dict["module"] = record.module
    event_dict["function"] = record.funcName
    event_dict["line"] = record.lineno
    return event_dict


_FOREIGN_PRE_CHAIN = [
    add_log_level,
    ExtraAdder(),
    _add_callsite,
    TimeStamper(fmt="iso", key="timestamp"),  # "iso" is always UTC, trailing "Z"
]


def _build_json_formatter() -> ProcessorFormatter:
    """JSON pipeline shared by the rotating file handler and the console handler
    when stdout isn't a tty (e.g. under launchd)."""
    return ProcessorFormatter(
        foreign_pre_chain=_FOREIGN_PRE_CHAIN,
        processors=[
            ExceptionRenderer(ExceptionDictTransformer(show_locals=False)),
            ProcessorFormatter.remove_processors_meta,
            EventRenamer("message"),
            JSONRenderer(),
        ],
    )


```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com .venv/bin/pytest tests/test_logging_config.py -v`
Expected: all tests in `TestJSONFormatterPipeline` and `TestRunIdFilter` PASS.

- [ ] **Step 5: Commit**

```bash
git add organist_bot/logging_config.py tests/test_logging_config.py
git commit -m "feat: build structlog JSON formatter pipeline in logging_config"
```

---

### Task 3: Console formatter pipeline + tty selection

**Files:**
- Modify: `organist_bot/logging_config.py` — append immediately after `_build_json_formatter()` from Task 2.
- Test: `tests/test_logging_config.py` — append a new test class.

**Interfaces:**
- Consumes: `_FOREIGN_PRE_CHAIN`, `_build_json_formatter()` (Task 2).
- Produces: `_build_console_formatter() -> ProcessorFormatter`, `_select_console_formatter(is_tty: bool) -> ProcessorFormatter` — Task 4's `setup_logging()` calls `_select_console_formatter(sys.stdout.isatty())`.

- [ ] **Step 1: Add the failing tests**

Append to `tests/test_logging_config.py` (after the `TestRunIdFilter` class):

```python

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
```

And update the import line at the top of the file to also pull in `_select_console_formatter`:

```python
from organist_bot.logging_config import (
    RunIdFilter,
    _build_json_formatter,
    _select_console_formatter,
    set_run_id,
)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com .venv/bin/pytest tests/test_logging_config.py -v`
Expected: `ImportError: cannot import name '_select_console_formatter'`.

- [ ] **Step 3: Implement the console pipeline and selector**

Append to `organist_bot/logging_config.py`, right after `_build_json_formatter()`:

```python
def _build_console_formatter() -> ProcessorFormatter:
    """Colorized, human-readable pipeline for an interactive terminal."""
    return ProcessorFormatter(
        foreign_pre_chain=_FOREIGN_PRE_CHAIN,
        processors=[
            ProcessorFormatter.remove_processors_meta,
            ConsoleRenderer(),
        ],
    )


def _select_console_formatter(is_tty: bool) -> ProcessorFormatter:
    """Chosen once, at setup_logging() call time, based on sys.stdout.isatty()."""
    return _build_console_formatter() if is_tty else _build_json_formatter()


```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com .venv/bin/pytest tests/test_logging_config.py -v`
Expected: all tests PASS, including the two new `TestSelectConsoleFormatter` tests.

- [ ] **Step 5: Commit**

```bash
git add organist_bot/logging_config.py tests/test_logging_config.py
git commit -m "feat: build structlog console formatter pipeline with tty selection"
```

---

### Task 4: Rewire `setup_logging()`, remove dead code

**Files:**
- Modify: `organist_bot/logging_config.py:1-269` (module docstring, imports, deletes `_STDLIB_FIELDS`/ANSI palette/`ConsoleFormatter`/`JSONFormatter`, rewrites `setup_logging()`)
- Test: `tests/test_logging_config.py` — append two new test classes.

**Interfaces:**
- Consumes: `_build_json_formatter()` (Task 2), `_select_console_formatter()` (Task 3), `RunIdFilter`/`set_run_id()` (unchanged).
- Produces: the final `setup_logging(log_file: str, level: int = logging.DEBUG) -> None` public API — unchanged signature, new internals. Nothing downstream of this task depends on new names.

- [ ] **Step 1: Add the failing tests**

Append to `tests/test_logging_config.py`:

```python

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

        setup_logging(str(tmp_path / "gigs.log"))

        console_handler = _clean_root_logger.handlers[0]
        assert isinstance(console_handler.formatter, ProcessorFormatter)
        line = console_handler.formatter.format(_make_record("tty wiring check"))
        with pytest.raises(json.JSONDecodeError):
            json.loads(line)

    def test_non_tty_stdout_attaches_json_pipeline(
        self, monkeypatch, tmp_path, _clean_root_logger
    ):
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
        from organist_bot.logging_config import setup_logging

        setup_logging(str(tmp_path / "gigs.log"))

        console_handler = _clean_root_logger.handlers[0]
        doc = json.loads(console_handler.formatter.format(_make_record("non-tty wiring check")))
        assert doc["message"] == "non-tty wiring check"


class TestSetupLoggingCaplogCompatibility:
    def test_caplog_still_captures_after_setup_logging_runs(
        self, monkeypatch, tmp_path, caplog, _clean_root_logger
    ):
        """setup_logging() must not interfere with pytest's own log-capture handler."""
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
        from organist_bot.logging_config import setup_logging

        setup_logging(str(tmp_path / "gigs.log"))

        logger = logging.getLogger("organist_bot.test_caplog_compat")
        with caplog.at_level(logging.INFO, logger="organist_bot.test_caplog_compat"):
            logger.info("caplog compatibility check")

        assert any(r.message == "caplog compatibility check" for r in caplog.records)
```

Add these two imports near the top of the test file (with the other imports): `ProcessorFormatter` is needed for the `isinstance` check, and `sys` is needed to monkeypatch `sys.stdout.isatty`:

```python
import sys

from structlog.stdlib import ProcessorFormatter
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com .venv/bin/pytest tests/test_logging_config.py -v`
Expected: `TestSetupLoggingConsoleSelection` tests FAIL — the console handler's formatter is still the old `ConsoleFormatter`/`JSONFormatter`, not a `ProcessorFormatter` instance (`isinstance` check fails, or the JSON-parse assertions fail because the old formatter's output shape differs). `TestSetupLoggingCaplogCompatibility` may already pass (caplog was never formatter-dependent) — that's fine, it's asserting a guarantee, not chasing a bug.

- [ ] **Step 3: Rewrite the module**

Replace the module docstring (lines 1-22) with:

```python
"""
organist_bot/logging_config.py
──────────────────────────────
Central logging configuration for OrganistBot, built on structlog.

Two handlers are attached to the root logger:

  Console (stdout)
    Level  : INFO and above
    Format : colorized, human-readable when stdout is a real terminal;
             structured JSON when it isn't (e.g. under launchd/supervisord,
             where stdout is redirected to a log file)
    Purpose: quick feedback when run interactively, and a machine-parseable
             record when it isn't

  Rotating file
    Level  : DEBUG and above
    Format : one JSON object per line
    Purpose: full audit trail; machine-parseable with jq / pandas / ELK

Call setup_logging() exactly once at the top of main().

Every call site in the codebase logs via plain stdlib
logging.getLogger(__name__) — nothing calls structlog.get_logger() directly,
so every record is "foreign" to structlog and flows entirely through the
foreign_pre_chain + processors built up below.

Silences urllib3 / requests chatter so only application-level
messages appear on the console.
"""
```

Remove the `import datetime` and `import json` lines (no longer used once the hand-rolled formatters are gone).

Delete the `# ── ANSI colour palette` section, the `_STDLIB_FIELDS` frozenset, the `ConsoleFormatter` class, and the `JSONFormatter` class in their entirety — everything between the end of the `_build_console_formatter`/`_select_console_formatter` section (added in Task 3) and the `# ── Public API` comment.

Replace `setup_logging()`'s body with:

```python
def setup_logging(log_file: str, level: int = logging.DEBUG) -> None:
    """
    Attach a console handler (INFO+) and a rotating JSON file handler
    (DEBUG+) to the root logger.

    The console handler renders colorized, human-readable text when stdout
    is a real terminal, and structured JSON otherwise (e.g. under
    launchd/supervisord, where stdout is redirected to a log file).

    Args:
        log_file: Path to the rotating log file (e.g. "gigs.log").
        level:    Minimum level captured by the file handler. The
                  console handler is always capped at INFO.

    Silences urllib3 and requests so network-layer noise stays out of
    the console while still landing in the file at WARNING+.
    """
    root = logging.getLogger()

    # Idempotency guard — if handlers are already attached, don't add more.
    # This prevents duplicate log lines when setup_logging() is called more
    # than once (e.g. during testing or accidental double-invocation).
    if root.handlers:
        return

    root.setLevel(level)

    # Quieten third-party loggers on the console
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("tenacity").setLevel(logging.WARNING)
    logging.getLogger("googlemaps").setLevel(logging.WARNING)

    run_id_filter = RunIdFilter()

    # ── Console handler ───────────────────────────────────────────────────────
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(_select_console_formatter(sys.stdout.isatty()))
    console.addFilter(run_id_filter)
    root.addHandler(console)

    # ── Rotating JSON file handler ────────────────────────────────────────────
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,  # rotate at 5 MB
        backupCount=3,  # keep gigs.log, gigs.log.1, .2, .3
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(_build_json_formatter())
    file_handler.addFilter(run_id_filter)
    root.addHandler(file_handler)

    logging.getLogger(__name__).info(
        "Logging initialised",
        extra={
            "log_file": str(Path(log_file).resolve()),
            "file_level": "DEBUG",
            "console_level": "INFO",
        },
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com .venv/bin/pytest tests/test_logging_config.py -v`
Expected: every test in the file PASSES.

- [ ] **Step 5: Commit**

```bash
git add organist_bot/logging_config.py tests/test_logging_config.py
git commit -m "feat: rewire setup_logging onto structlog, drop hand-rolled formatters"
```

---

### Task 5: Full regression run and manual end-to-end verification

**Files:** none (verification only)

**Interfaces:** none — this task consumes the finished `setup_logging()` from Task 4 and checks it as a black box.

- [ ] **Step 1: Run the full test suite**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com .venv/bin/pytest --tb=short -q`
Expected: full suite passes, no regressions in `tests/test_main.py`, `tests/test_notifier.py`, `tests/test_filters.py`, `tests/test_dry_run.py`, `tests/test_calendar_client.py` (all of which use `caplog` against loggers this change touches indirectly).

- [ ] **Step 2: Run lint, format, and type checks**

Run: `.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy organist_bot/`
Expected: no errors. If `ruff format --check` fails on the rewritten files, run `.venv/bin/ruff format .` and re-check.

- [ ] **Step 3: Manually verify the tty (interactive) path**

Run:
```bash
mkdir -p /tmp/organistbot_logging_manual_check
script -q /dev/null .venv/bin/python -c "
from organist_bot.logging_config import setup_logging
import logging
setup_logging('/tmp/organistbot_logging_manual_check/gigs.log')
logging.getLogger('manual_check').info('tty check', extra={'foo': 'bar'})
"
```
Expected: one colorized, human-readable line printed to the terminal (not JSON). `script -q /dev/null` allocates a pseudo-terminal for stdout so `isatty()` is `True`, matching a real interactive run.

- [ ] **Step 4: Manually verify the non-tty path**

Run:
```bash
.venv/bin/python -c "
from organist_bot.logging_config import setup_logging
import logging
setup_logging('/tmp/organistbot_logging_manual_check/gigs.log')
logging.getLogger('manual_check').info('non-tty check', extra={'foo': 'bar'})
" | cat
```
Expected: one line of valid JSON (piping through `cat` makes stdout a non-tty, matching launchd/supervisord capture).

- [ ] **Step 5: Manually verify the file output and the exception/secret-leak guard**

Run:
```bash
python3 -c "
from organist_bot.logging_config import setup_logging
import logging
setup_logging('/tmp/organistbot_logging_manual_check/gigs.log')
logger = logging.getLogger('manual_check')
secret_token = 'super-secret-value'
try:
    1 / 0
except ZeroDivisionError:
    logger.exception('boom')
"
cat /tmp/organistbot_logging_manual_check/gigs.log
echo "--- secret-leak check (both must print 0) ---"
grep -c "super-secret-value" /tmp/organistbot_logging_manual_check/gigs.log
grep -c '"locals"' /tmp/organistbot_logging_manual_check/gigs.log
rm -rf /tmp/organistbot_logging_manual_check
```
Expected: `data/<file>.log` content is all valid JSON; the last line's `exception` field is a structured list containing `exc_type: "ZeroDivisionError"`; both `grep -c` commands print `0` — the secret local variable never appears anywhere in the log file.

- [ ] **Step 6: Commit any fixups**

If Steps 2-5 required code changes (formatting, an overlooked edge case), commit them:

```bash
git add -A
git commit -m "fix: address issues found in stage 1 logging manual verification"
```

If no fixups were needed, skip this step — nothing to commit.

---

### Task 6: Ship

**Files:** none

- [ ] **Step 1: Run the full ship workflow**

Run: `make ship`

This runs the full local quality gate (lint, format-check, type-check, security, tests) and, if it passes, pushes the branch, opens a PR as ready-for-review, and enables squash auto-merge — per this repo's `CLAUDE.md` ship workflow. `core.hooksPath` gets set to `.githooks` automatically on first run if it isn't already.

- [ ] **Step 2: Report the PR URL**

`scripts/ship.sh`'s output includes the created PR URL — report it once `make ship` completes.
