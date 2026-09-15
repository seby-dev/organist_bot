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

import contextvars
import logging
import logging.handlers
import sys
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import Any

from structlog.dev import ConsoleRenderer, RichTracebackFormatter
from structlog.processors import EventRenamer, ExceptionRenderer, JSONRenderer, TimeStamper
from structlog.stdlib import ExtraAdder, ProcessorFormatter, add_log_level
from structlog.tracebacks import ExceptionDictTransformer
from structlog.typing import Processor

# ── Run ID context variable ────────────────────────────────────────────────────
# Set once per main() call via set_run_id(); automatically injected into every
# log record within that run by RunIdFilter.

_run_id: contextvars.ContextVar[str] = contextvars.ContextVar("run_id", default="")


def set_run_id(run_id: str) -> None:
    """Call at the top of each main() run to stamp all subsequent log records."""
    _run_id.set(run_id)


class RunIdFilter(logging.Filter):
    """Injects the current run_id into every LogRecord."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = _run_id.get("")
        return True


# ── structlog processor chain ──────────────────────────────────────────────────
# Every call site in this codebase logs via plain stdlib logging.getLogger(name),
# so every record structlog sees here is "foreign" to it. foreign_pre_chain runs
# once per record before either final renderer below.


def _add_callsite(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> Mapping[str, Any]:
    """Add module/function/line/logger from the underlying LogRecord, using the field
    names this project's JSON logs have always used (structlog's own
    CallsiteParameterAdder uses different names)."""
    record = event_dict["_record"]
    event_dict["module"] = record.module
    event_dict["function"] = record.funcName
    event_dict["line"] = record.lineno
    event_dict["logger"] = record.name
    return event_dict


_FOREIGN_PRE_CHAIN: list[Processor] = [
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


def _build_console_formatter() -> ProcessorFormatter:
    """Colorized, human-readable pipeline for an interactive terminal.

    Explicitly disables rich's traceback locals (mirrors the JSON path's
    ExceptionDictTransformer(show_locals=False)) — otherwise, because rich is
    installed transitively (via litellm), structlog would default to
    RichTracebackFormatter(show_locals=True), rendering every local variable
    (including secrets like passwords or API keys) into an exception logged
    to an interactive terminal.
    """
    return ProcessorFormatter(
        foreign_pre_chain=_FOREIGN_PRE_CHAIN,
        processors=[
            ProcessorFormatter.remove_processors_meta,
            ConsoleRenderer(exception_formatter=RichTracebackFormatter(show_locals=False)),
        ],
    )


def _select_console_formatter(is_tty: bool) -> ProcessorFormatter:
    """Chosen once, at setup_logging() call time, based on sys.stdout.isatty()."""
    return _build_console_formatter() if is_tty else _build_json_formatter()


# ── Public API ─────────────────────────────────────────────────────────────────


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
