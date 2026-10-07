"""Logging to stdout (12-Factor XI).

The process never writes log files; it streams to stdout and lets the runtime
decide where that goes. ``LOG_FORMAT=json`` produces one JSON object per line
for log shippers, ``text`` stays readable in a terminal.

The current request id lives in a :class:`~contextvars.ContextVar` so every log
line can carry it without threading an argument through all four layers.
"""

import json
import logging
import sys
from contextvars import ContextVar

#: Set by ``RequestIdMiddleware`` for the duration of each request.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "asctime",
    "message",
    "taskName",
}


class _ContextFilter(logging.Filter):
    """Attach the current request id to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


def _extras(record: logging.LogRecord) -> dict[str, object]:
    return {k: v for k, v in record.__dict__.items() if k not in _RESERVED and k != "request_id"}


class TextFormatter(logging.Formatter):
    """Human-readable lines with ``key=value`` extras appended."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-8s %(name)s [%(request_id)s] %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = " ".join(f"{k}={v}" for k, v in _extras(record).items())
        return f"{base} | {extras}" if extras else base


class JsonFormatter(logging.Formatter):
    """One JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "message": record.getMessage(),
        }
        payload.update(_extras(record))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO", log_format: str = "text") -> None:
    """Point the root logger at stdout.

    Replaces the root handlers outright so uvicorn's own loggers flow through
    the same formatter (the app is started with ``log_config=None``).
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if log_format == "json" else TextFormatter())
    handler.addFilter(_ContextFilter())

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())


def get_logger(name: str) -> logging.Logger:
    """Module-level logger helper, so callers don't import ``logging``."""
    return logging.getLogger(name)
