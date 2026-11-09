"""Structured JSON logs for Cloud Logging, with personal data kept out by construction.

Only whitelisted `extra` fields are written: numbers and booleans under any name, and strings
only under the names in SAFE_TEXT_FIELDS (IDs, error types, provider names). Message text,
names, emails and phone numbers therefore cannot reach the logs even by mistake, and exception
tracebacks (which can quote message content) are reduced to the exception type.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

SAFE_TEXT_FIELDS = frozenset(
    {
        "task_id", "job", "kind", "error_type", "caller", "reason", "provider", "model",
        "prompt_version", "rule",
    }
)  # fmt: skip
_STANDARD = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD or key.startswith("_"):
                continue
            if isinstance(value, bool | int | float):
                entry[key] = value
            elif isinstance(value, str) and key in SAFE_TEXT_FIELDS:
                entry[key] = value[:200]
        if record.exc_info and record.exc_info[0] is not None:
            entry["error_type"] = record.exc_info[0].__name__
        return json.dumps(entry, ensure_ascii=False)


def setup_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # Library loggers can include request URLs with IDs; keep them quiet.
    for noisy in ("googleapiclient", "google", "urllib3", "httpx", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
