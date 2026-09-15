# Stage 1: structured JSON logging (structlog)

Status: approved
Stage: 1 of 3 in the observability rollout (structured logging → error tracking → uptime watchdog)

## Purpose

Replace the two hand-rolled `logging.Formatter` subclasses in
`organist_bot/logging_config.py` with `structlog`-based formatters, so that
every log destination that isn't an interactive terminal emits structured
JSON — including `logs/launchd-supervisord.log`, which today captures raw
ANSI-coded console text and is the one genuinely ad hoc, unparseable log
output in the project.

## Constraints

- No new infrastructure. This is a dependency addition (`structlog`) and a
  rewrite confined to `organist_bot/logging_config.py`.
- Zero call-site changes. Every module already logs via plain
  `logging.getLogger(__name__).info(msg, extra={...})`; none of that changes.
  Nothing in the codebase calls `structlog.get_logger()` or
  `structlog.configure()` — all log records are "foreign" (stdlib-originated)
  from structlog's point of view.
- `RunIdFilter` and `set_run_id()` are unchanged. `RunIdFilter` is a
  `logging.Filter`, which runs before formatting regardless of which
  formatter is attached, so it keeps stamping `record.run_id` exactly as
  today.
- `pytest`'s `caplog` fixture must keep working. `caplog` attaches its own
  handler directly to the logger and reads raw `LogRecord` attributes
  (`.message`, `.getMessage()`, `.levelno`, ...); it does not depend on
  formatters at all. This is unaffected by this change, but a regression
  test asserts it explicitly rather than leaving it as an unverified
  assumption.

## Current state (baseline)

`organist_bot/logging_config.py` attaches two handlers to the root logger:

- **Console** (`sys.stdout`, INFO+): `ConsoleFormatter` — colorized,
  human-readable, single-line text with ANSI codes.
- **Rotating file** (`data/<log_file>`, DEBUG+): `JSONFormatter` — one JSON
  object per line, with fields `timestamp`, `run_id`, `level`, `logger`,
  `message`, `module`, `function`, `line`, plus any caller-supplied `extra=`
  fields merged at the top level, plus an `exception` string field when
  `exc_info` is present.

Both handlers get `RunIdFilter` so every record carries the current
`run_id`. `setup_logging()` is idempotent (returns immediately if the root
logger already has handlers).

Under `launchd` (`scripts/install-launchagent.sh`), stdout/stderr are
redirected to `logs/launchd-supervisord.log` / `logs/launchd-supervisord-err.log`.
Because the console formatter is used unconditionally today, that file
contains the same ANSI-coded text a human would see in a terminal — not
machine-parseable, unlike the rotating JSON file.

## Design

### Formatter pipeline

Replace `ConsoleFormatter` and `JSONFormatter` with
`structlog.stdlib.ProcessorFormatter` instances built from a shared
`foreign_pre_chain` (this is the documented structlog pattern for
stdlib-only codebases — see `docs/standard-library.md` in structlog):

```python
foreign_pre_chain = [
    structlog.stdlib.add_log_level,
    structlog.stdlib.ExtraAdder(),   # pulls extra= kwargs + run_id into event_dict
    _add_callsite,                   # custom: module/function/line from the LogRecord
    structlog.processors.TimeStamper(fmt="iso", key="timestamp"),  # "iso" is always UTC, trailing "Z"
]
```

`_add_callsite` is a small processor modeled directly on structlog's own
`extract_from_record` recipe (`docs/standard-library.md`):

```python
def _add_callsite(_, __, event_dict):
    record = event_dict["_record"]
    event_dict["module"] = record.module
    event_dict["function"] = record.funcName
    event_dict["line"] = record.lineno
    return event_dict
```

### File handler (JSON, all environments)

```python
ProcessorFormatter(
    foreign_pre_chain=foreign_pre_chain,
    processors=[
        structlog.processors.ExceptionRenderer(
            structlog.tracebacks.ExceptionDictTransformer(show_locals=False)
        ),
        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
        structlog.processors.EventRenamer("message"),
        structlog.processors.JSONRenderer(),
    ],
)
```

- `EventRenamer("message")` keeps the message key as `message` (structlog's
  default is `event`) — preserves the existing schema, no downstream
  tooling needs to change.
- `show_locals=False` is a deliberate security decision: structlog's
  `ExceptionDictTransformer` defaults to `show_locals=True`, which serializes
  every local variable in every stack frame of a logged exception. In this
  codebase that risks writing SMTP passwords, OAuth tokens, or API keys into
  `data/*.log` whenever an exception is logged from a function that has one
  in scope. `show_locals=False` still gives structured frames
  (`exc_type`, `exc_value`, `filename`, `lineno`, `name`) without variable
  values — a genuine improvement over today's flat traceback string, without
  the leak risk. This must not be silently switched to `True` in a later
  stage without a matching decision to add secret scrubbing first (see
  Stage 2).
- The `exception` field's shape therefore changes from a string (today) to
  a structured list of frame dicts. This is the one intentional schema
  change; everything else (`timestamp`, `run_id`, `logger`, `message`,
  `module`, `function`, `line`, extras) is preserved byte-for-byte in
  meaning, field names, and value types.
- One more small, verified-during-planning value change: `structlog.stdlib.add_log_level`
  emits lowercase level strings (`"info"`, `"warning"`) — today's hand-rolled
  formatter emits `record.levelname` uppercase (`"INFO"`, `"WARNING"`). This
  is structlog's own idiomatic convention (its console output and every
  stdlib-integration example in its docs use lowercase), and nothing
  downstream of these logs depends on a specific case today, so this is kept
  as-is rather than fought with a custom processor. Flagged here because it
  wasn't caught until real API verification during plan-writing, after this
  spec was first approved.

### Console handler (tty-aware)

Chosen once, at `setup_logging()` call time, via `sys.stdout.isatty()` — not
re-evaluated per record, matching structlog's documented
pretty-vs-JSON recipe (`docs/logging-best-practices.md`):

- **tty** (interactive terminal): `structlog.dev.ConsoleRenderer()` —
  colorized, human-readable. Replaces the bespoke `ConsoleFormatter` ANSI
  code. Run-id, level, logger name, and message all still render; exact byte
  formatting will differ cosmetically from today's `ConsoleFormatter` (this
  is expected and acceptable — no test asserts on the human-readable format's
  exact text, only on structured content).
- **non-tty** (launchd/supervisord capture, CI, piped output): same JSON
  pipeline as the file handler. This is what fixes
  `logs/launchd-supervisord.log`.

### What doesn't change

- `RunIdFilter`, `set_run_id()`, `_run_id` contextvar: untouched.
- `setup_logging()` signature (`log_file: str, level: int = logging.DEBUG`):
  untouched.
- The idempotency guard (`if root.handlers: return`): untouched.
- Third-party logger silencing (`urllib3`, `requests`, `tenacity`,
  `googlemaps`): untouched.
- Every call site across the codebase: untouched.

## Testing (TDD)

`tests/test_logging_config.py` is rewritten first (red), then the
implementation follows (green):

1. **File/JSON formatter tests** (replacing the current `TestJSONFormatter`
   class): build a real `logging.LogRecord` (same `_make_record` helper),
   run it through the new `ProcessorFormatter` pipeline used by the file
   handler, and assert the resulting JSON has the same fields as today
   (`message`, `level`, `logger`, `timestamp`, `module`, `function`, `line`,
   `run_id`, arbitrary extras) — i.e. port every existing assertion in
   `TestJSONFormatter` to the new pipeline.
2. **Exception shape test**: logging with `exc_info` set produces an
   `exception` field that is a list of dicts containing `exc_type` and
   `exc_value`, and confirms no `locals` key is present anywhere in the
   output (regression guard for the `show_locals=False` decision).
3. **`RunIdFilter` tests**: unchanged — `RunIdFilter` itself isn't touched,
   so its existing test class carries over as-is.
4. **tty-selection test**: `setup_logging()` attaches a JSON-rendering
   formatter to the console handler when `sys.stdout.isatty()` is `False`
   (patch/monkeypatch `isatty`), and a `ConsoleRenderer`-based formatter when
   it's `True`.
5. **`caplog` regression test**: after calling `setup_logging()`, a test
   using `caplog.at_level(...)` on some logger still captures records with
   the expected `.message`/`.levelno` — an explicit assertion that the
   formatter swap doesn't interfere with pytest's log capture, addressing
   the requirement directly rather than leaving it as an inferred property.

## Dependencies

Add to `pyproject.toml` `[project].dependencies`:

```
"structlog>=24.1",
```

(Latest stable at time of writing is 26.1.0; confirmed compatible with the
project's `requires-python = ">=3.12"`.)

## Documentation updates

- `organist_bot/logging_config.py`'s module docstring is updated to describe
  the structlog-based pipeline and the tty-aware console behavior, replacing
  the outdated "coloured console + JSON file" description.
- `CLAUDE.md`'s architecture section is not otherwise affected — it doesn't
  currently describe the logging formatter internals in detail.

## Manual verification (beyond passing tests)

After implementation, before shipping:

1. Run `python main.py` (or a short-lived invocation) directly in a
   terminal and confirm colorized, human-readable console output still
   appears.
2. Run the same entry point with stdout piped to a file
   (`python -c "..." | cat` or similar non-tty invocation) and confirm the
   captured output is valid JSON lines.
3. Inspect `data/<log_file>` after a run and confirm it's still valid JSON
   with the expected field names.
4. Trigger a logged exception path (e.g. a deliberately broken SMTP call in
   a throwaway local run) and confirm the `exception` field is a structured
   list with no `locals` key anywhere in the output.

## Out of scope

- Stage 2 (GlitchTip/error tracking) and Stage 3 (uptime watchdog) are
  separate specs with their own brainstorm/approval cycles.
- Secret scrubbing of log content beyond the `show_locals=False` decision
  above — full scrubbing is explicitly a Stage 2 concern (GlitchTip's
  `before_send`), not retrofitted into Stage 1's plain log formatting.
- Any change to what gets logged (log levels, messages, which events are
  logged) — this is purely a formatting-layer change.
