"""Structured JSON logging + a per-request/task correlation id (PRD §18, §22).

Usage:
    from app.core.logging import get_logger, set_correlation_id, new_correlation_id
    log = get_logger(__name__)
    log.info("thing happened", extra={"scope_id": "C123"})

Secrets and full message bodies must never be passed as fields.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import uuid

_correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default="-"
)

# Standard LogRecord attributes we do not want to duplicate into the JSON blob.
_RESERVED = set(
    logging.makeLogRecord({}).__dict__.keys()
) | {"message", "asctime", "taskName"}


def set_correlation_id(value: str) -> None:
    _correlation_id.set(value)


def get_correlation_id() -> str:
    return _correlation_id.get()


def new_correlation_id() -> str:
    cid = uuid.uuid4().hex[:12]
    _correlation_id.set(cid)
    return cid


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "cid": get_correlation_id(),
            "msg": record.getMessage(),
        }
        # include any structured fields passed via `extra=`
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


_configured = False


def configure_logging(level: str = "INFO") -> None:
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    # quiet noisy libraries a notch
    for noisy in ("httpx", "urllib3", "openai"):
        logging.getLogger(noisy).setLevel("WARNING")
    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
