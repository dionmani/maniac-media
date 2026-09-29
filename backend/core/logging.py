"""Structured logging with credential redaction.

Every record passes through :class:`RedactingFilter`, which scrubs anything that looks like
a password, token, API key or credential-bearing URL *before* it reaches any handler.
"""
from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import os
import re
import sys
from datetime import datetime, timezone

from backend.core import paths

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)

_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    # bearer tokens first, so "Authorization: Bearer <jwt>" loses the token rather than the word "Bearer"
    (re.compile(r"(?i)bearer\s+[a-z0-9._\-]+"), "Bearer ***"),
    # scheme://user:password@host
    (re.compile(r"(?P<p>[a-zA-Z][a-zA-Z0-9+.-]*://[^:/\s@]+:)[^@\s]+(?=@)"), r"\g<p>***"),
    # key=value / "key": "value" pairs
    (
        re.compile(
            r"(?i)(?P<k>[\"']?(?:password|passwd|pwd|api[_-]?key|apikey|token|secret|authorization|x-api-key|pass)[\"']?\s*[:=]\s*[\"']?)"
            r"(?P<v>[^\"'&\s,}]+)"
        ),
        r"\g<k>***",
    ),
    (re.compile(r"(?i)/bot\d+:[a-z0-9_-]+"), "/bot***"),  # telegram tokens embedded in URLs
]


def redact(text: str) -> str:
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        record.msg = redact(message)
        record.args = None
        rid = request_id_var.get()
        if rid and not hasattr(record, "request_id"):
            record.request_id = rid
        return True


class JsonFormatter(logging.Formatter):
    _std = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in self._std and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        return redact(text) if record.exc_info else text


_configured = False


def configure_logging() -> None:
    global _configured
    if _configured:
        return
    _configured = True
    level = os.environ.get("MANIAC_LOG_LEVEL", "INFO").upper()
    fmt = os.environ.get("MANIAC_LOG_FORMAT", "console")

    paths.LOG_DIR.mkdir(parents=True, exist_ok=True)
    redactor = RedactingFilter()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(JsonFormatter() if fmt == "json" else ConsoleFormatter())
    console.addFilter(redactor)

    file_handler = logging.handlers.RotatingFileHandler(
        paths.LOG_DIR / "maniac.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(JsonFormatter())
    file_handler.addFilter(redactor)

    root = logging.getLogger()
    root.handlers[:] = [console, file_handler]
    root.setLevel(level)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
    for noisy in ("httpx", "httpcore", "apscheduler", "watchdog", "alembic.runtime.migration"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # Windows' Proactor loop logs a traceback whenever a browser drops a socket; it is harmless.
    logging.getLogger("asyncio").addFilter(
        lambda r: "_call_connection_lost" not in r.getMessage() and "ConnectionResetError" not in str(r.exc_info)
    )
