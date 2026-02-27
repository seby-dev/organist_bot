"""
organist_bot/logging_config.py
──────────────────────────────
Central logging configuration for OrganistBot.

Two handlers are attached to the root logger:

  Console (stdout)
    Level  : INFO and above
    Format : human-readable with ANSI colour coding
    Purpose: quick feedback while the bot is running

  Rotating file
    Level  : DEBUG and above
    Format : one JSON object per line
    Purpose: full audit trail; machine-parseable with jq / pandas / ELK

Call setup_logging() exactly once at the top of main().

Silences urllib3 / requests chatter so only application-level
messages appear on the console.
"""

import contextvars
import datetime
import json
import logging
import logging.handlers
import sys
from pathlib import Path

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


# ── ANSI colour palette ────────────────────────────────────────────────────────

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[36m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_MAGENTA = "\033[35m"

_LEVEL_COLORS: dict[str, str] = {
    "DEBUG": _CYAN,
    "INFO": _GREEN,
    "WARNING": _YELLOW,
    "ERROR": _RED,
    "CRITICAL": "\033[1;31m",  # bold red
}

# ── Fields that belong to LogRecord itself (never treated as "extra") ──────────

_STDLIB_FIELDS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "id",
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
        "run_id",
    }
)


# ── Formatters ─────────────────────────────────────────────────────────────────


class ConsoleFormatter(logging.Formatter):
    """
    Colour-coded, human-readable single-line formatter for the terminal.

    Every line includes a fixed-width run_id bracket so columns stay aligned
    regardless of whether a run is in progress.  Pre-run messages (startup,
    logging init) use [--------] as a placeholder.

    Example output:
        2026-02-25 14:32:01.004 [--------] INFO      __main__                    Scheduler starting  poll_minutes=2
        2026-02-25 14:32:01.021 [--------] INFO      organist_bot.logging_config Logging initialised  log_file='...'
        2026-02-25 14:32:01.045 [a1b2c3d4] INFO      __main__                    OrganistBot run started
        2026-02-25 14:32:01.312 [a1b2c3d4] INFO      organist_bot.scraper        Fetch successful  url='https://...'  elapsed_ms=234
        2026-02-25 14:32:02.089 [a1b2c3d4] WARNING   organist_bot.notifier       No contact email for 'Wedding' — skipped
        2026-02-25 14:32:02.091 [a1b2c3d4] DEBUG     organist_bot.filters        Gig rejected  filter='FeeFilter(...)' gig='Sunday Service'
    """

    def format(self, record: logging.LogRecord) -> str:
        ts = (
            datetime.datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S")
            + f".{int(record.msecs):03d}"
        )
        color = _LEVEL_COLORS.get(record.levelname, "")
        level = f"{color}{record.levelname:<9}{_RESET}"
        name = f"{_DIM}{record.name:<27}{_RESET}"
        msg = f"{_BOLD}{record.getMessage()}{_RESET}"

        # Collect caller-supplied extra fields
        ctx_parts = [
            f"{_YELLOW}{k}{_RESET}={v!r}"
            for k, v in record.__dict__.items()
            if k not in _STDLIB_FIELDS
        ]
        ctx = "  ".join(ctx_parts)

        run_id = getattr(record, "run_id", "") or "--------"
        run_id_str = f" {_DIM}[{run_id}]{_RESET}"

        parts = [f"{_DIM}{ts}{_RESET}{run_id_str}", level, name, msg]
        if ctx:
            parts.append(ctx)

        line = " ".join(parts)

        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)

        return line


class JSONFormatter(logging.Formatter):
    """
    Structured JSON formatter — one complete JSON object per log line.

    Standard fields are always present; any extras passed by the caller
    are merged in at the top level.  Exceptions are serialised as a
    'exception' string field.

    Example:
        {
          "timestamp": "2026-02-25T14:32:01.234Z",
          "level": "INFO",
          "logger": "organist_bot.scraper",
          "message": "Fetch successful",
          "module": "scraper",
          "function": "fetch",
          "line": 31,
          "url": "https://organistsonline.org/required/",
          "status": 200,
          "size_bytes": 18432,
          "elapsed_ms": 312
        }
    """

    def format(self, record: logging.LogRecord) -> str:
        doc: dict = {
            "timestamp": (
                datetime.datetime.utcfromtimestamp(record.created).strftime("%Y-%m-%dT%H:%M:%S.")
                + f"{int(record.msecs):03d}Z"
            ),
            "run_id": getattr(record, "run_id", ""),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Merge caller-supplied extra context
        for key, value in record.__dict__.items():
            if key not in _STDLIB_FIELDS:
                doc[key] = value

        if record.exc_info:
            doc["exception"] = self.formatException(record.exc_info)

        return json.dumps(doc, default=str)


# ── Public API ─────────────────────────────────────────────────────────────────


def setup_logging(log_file: str, level: int = logging.DEBUG) -> None:
    """
    Attach a coloured console handler (INFO+) and a rotating JSON file
    handler (DEBUG+) to the root logger.

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
    console.setFormatter(ConsoleFormatter())
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
    file_handler.setFormatter(JSONFormatter())
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
