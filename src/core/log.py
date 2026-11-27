"""Structured logging: event-style messages plus ``extra`` fields, rendered as key=value or JSON.

Convention used everywhere in the project::

    logger.info("http.retry", extra={"url": url, "attempt": 2, "status": 503})

The message is a short dotted *event name*; context goes in ``extra``. Configure once at process
start with :func:`configure_logging` (the CLI does this from ``SCRAPER_LOG_LEVEL`` /
``SCRAPER_LOG_FORMAT``).
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any

# Attributes every LogRecord has; anything else on a record came from ``extra=``.
_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


def record_fields(record: logging.LogRecord) -> dict[str, Any]:
    """The ``extra`` fields attached to ``record``."""
    return {k: v for k, v in record.__dict__.items() if k not in _RESERVED}


def _timestamp(record: logging.LogRecord) -> str:
    return datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds")


class KeyValueFormatter(logging.Formatter):
    """``2026-11-26T06:15:25.797+00:00 INFO src.core.http http.retry attempt=2 status=503``."""

    def format(self, record: logging.LogRecord) -> str:
        fields = " ".join(
            f"{k}={v!r}" if isinstance(v, str) and " " in v else f"{k}={v}"
            for k, v in record_fields(record).items()
        )
        line = f"{_timestamp(record)} {record.levelname} {record.name} {record.getMessage()}"
        if fields:
            line = f"{line} {fields}"
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ``ts``, ``level``, ``logger``, ``event`` plus extra fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": _timestamp(record),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
            **record_fields(record),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO", fmt: str = "text") -> None:
    """Install a single stderr handler on the root logger (idempotent).

    Args:
        level: ``DEBUG`` / ``INFO`` / ``WARNING`` / ``ERROR``.
        fmt: ``"text"`` (key=value) or ``"json"``.

    Raises:
        ValueError: On an unknown ``fmt``.
    """
    formatters = {"text": KeyValueFormatter, "json": JsonFormatter}
    if fmt not in formatters:
        raise ValueError(f"log format must be one of {sorted(formatters)}, got {fmt!r}")
    handler = logging.StreamHandler()
    handler.setFormatter(formatters[fmt]())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    logging.getLogger("httpx").setLevel(logging.WARNING)  # our client logs its own events
    logging.getLogger("httpcore").setLevel(logging.WARNING)
