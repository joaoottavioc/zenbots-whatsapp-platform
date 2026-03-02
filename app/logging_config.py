"""
Structured JSON logging with automatic context injection.

Call ``setup_logging()`` once at process startup — in ``main.py`` module
scope and in the ARQ worker's ``on_startup()``.  Every log record will
automatically include *trace_id*, *bot_id*, and *contact_id* from the
request-scoped context variables defined in ``app.context``.

Set the environment variable ``LOG_FORMAT=text`` to fall back to
human-readable plain-text output (useful during local development).
"""

import logging
import os

from pythonjsonlogger import jsonlogger

from app.context import trace_id_var, current_bot_id, current_contact_id


class ContextFilter(logging.Filter):
    """Inject request-scoped context into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = trace_id_var.get("")  # type: ignore[attr-defined]
        record.bot_id = current_bot_id.get(None)  # type: ignore[attr-defined]
        record.contact_id = current_contact_id.get(None)  # type: ignore[attr-defined]
        return True


_LOG_FORMAT = (
    "%(asctime)s %(levelname)s %(name)s %(message)s "
    "%(trace_id)s %(bot_id)s %(contact_id)s"
)

# Noisy third-party loggers to suppress
_NOISY_LOGGERS = (
    "httpx",
    "httpcore",
    "sqlalchemy.engine",
    "urllib3",
    "uvicorn.access",
    "watchfiles",
    "arq",
)


def setup_logging() -> None:
    """Configure the root logger with JSON output and context injection.

    Safe to call multiple times — subsequent calls are no-ops.
    """
    root = logging.getLogger()

    # Guard against duplicate setup
    if any(isinstance(f, ContextFilter) for f in root.filters):
        return

    level = logging.DEBUG if os.getenv("ENVIRONMENT") == "development" else logging.INFO

    root.setLevel(level)
    root.addFilter(ContextFilter())

    # Remove any pre-existing handlers (e.g. basicConfig defaults)
    root.handlers.clear()

    handler = logging.StreamHandler()

    if os.getenv("LOG_FORMAT", "json").lower() == "text":
        formatter = logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s | trace=%(trace_id)s "
            "bot=%(bot_id)s contact=%(contact_id)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    else:
        formatter = jsonlogger.JsonFormatter(
            fmt=_LOG_FORMAT,
            datefmt="%Y-%m-%dT%H:%M:%S",
        )

    handler.setFormatter(formatter)
    root.addHandler(handler)

    # Suppress noisy third-party loggers
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
