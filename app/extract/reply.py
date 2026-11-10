"""Typed replies read by the AI first (DESIGN section 22, part C).

The AI gets the masked message, the sender's role, their open tasks, the task the message is
linked to (WhatsApp reply or email thread) and today's date, and returns strict JSON
(`ReplyReading`). Its output is checked here: a task ID it was not given, a progress outside
0–100 or a date in the past is dropped, and a confidence under 0.6 counts as no answer. The
caller then falls back to the deterministic parser (`app/commands.py`).

Logged per call: provider, model, prompt version, latency, confidence and whether the caller
must fall back. Never the text.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from app.extract.ai import AIProvider, NotConfigured, NullProvider, _executor

logger = logging.getLogger("grc.reply")

PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "reply_v1.md"
PROMPT_VERSION = "reply_v1"
TIMEOUT_SECONDS = 8.0
TRIES = 2  # one retry
MIN_CONFIDENCE = 0.6
MAX_MESSAGE_CHARS = 2000

Intent = Literal[
    "progress", "blocked", "extension", "review", "done", "snooze", "note", "question", "list",
    "help", "request", "unknown",
]  # fmt: skip


class ReplyReading(BaseModel):
    """What the AI read in one typed message."""

    task_id: str | None = None
    intent: Intent = "unknown"
    progress: int | None = Field(default=None, ge=0, le=100)
    note: str | None = None
    requested_date: date | None = None
    reply_to_user: str = ""
    confidence: float = Field(default=0.0, ge=0, le=1)

    @field_validator("task_id", "note", mode="before")
    @classmethod
    def _blank(cls, value: object) -> object:
        return (value.strip() or None) if isinstance(value, str) else value


REPLY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {"type": ["string", "null"]},
        "intent": {"type": "string", "enum": list(Intent.__args__)},  # type: ignore[attr-defined]
        "progress": {"type": ["integer", "null"], "minimum": 0, "maximum": 100},
        "note": {"type": ["string", "null"]},
        "requested_date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
        "reply_to_user": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": [
        "task_id", "intent", "progress", "note", "requested_date", "reply_to_user", "confidence",
    ],
}  # fmt: skip


class TaskLine(BaseModel):
    """One open task as the AI sees it."""

    id: str
    title: str
    due: date | None = None
    status: str = ""
    progress: int = 0
    needs_review: bool = False

    def line(self) -> str:
        due = self.due.isoformat() if self.due else "no due date"
        review = "yes" if self.needs_review else "no"
        return f"{self.id} | {self.title} | {due} | {self.status} | {self.progress}% | {review}"


def build_prompt(
    masked_text: str, role: str, tasks: list[TaskLine], linked: str | None, today: date
) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    return (
        template.replace("{{role}}", role or "Staff")
        .replace("{{today}}", f"{today:%A %Y-%m-%d}")
        .replace("{{linked}}", linked or "null")
        .replace("{{tasks}}", "\n".join(t.line() for t in tasks) or "(none)")
        .replace("{{message}}", masked_text[:MAX_MESSAGE_CHARS])
    )


def checked(reading: ReplyReading, tasks: list[TaskLine], today: date) -> ReplyReading:
    """Drop what the AI may not decide: a task it was not given, a date in the past."""
    ids = {task.id for task in tasks}
    updates: dict[str, Any] = {}
    if reading.task_id is not None and reading.task_id not in ids:
        updates["task_id"] = None
    if reading.requested_date is not None and reading.requested_date < today:
        updates["requested_date"] = None
    return reading.model_copy(update=updates)


def read_reply(
    provider: AIProvider,
    masked_text: str,
    role: str,
    tasks: list[TaskLine],
    linked: str | None,
    today: date,
    timeout: float = TIMEOUT_SECONDS,
    tries: int = TRIES,
) -> ReplyReading | None:
    """The AI's reading, or None when the caller must fall back to the rules: AI off, a failure
    or timeout (after one retry), invalid JSON, or confidence under 0.6. Never raises."""
    reader = getattr(provider, "read_reply", None)
    if isinstance(provider, NullProvider) or reader is None:
        return None
    prompt = build_prompt(masked_text, role, tasks, linked, today)
    for attempt in range(1, tries + 1):
        started = time.monotonic()
        error_type = ""
        reading: ReplyReading | None = None
        try:
            reading = _executor.submit(reader, prompt).result(timeout=timeout)
        except NotConfigured:
            error_type = "NotConfigured"
        except FutureTimeout:
            error_type = "Timeout"
        except ValidationError:
            error_type = "InvalidResponse"
        except Exception as error:  # any SDK/network error: the rules take over
            error_type = type(error).__name__
        if reading is not None:
            reading = checked(reading, tasks, today)
        confident = reading is not None and reading.confidence >= MIN_CONFIDENCE
        logger.info(
            "ai_reply_call",
            extra={
                "provider": provider.name,
                "model": provider.model,
                "prompt_version": PROMPT_VERSION,
                "latency_ms": round((time.monotonic() - started) * 1000),
                "confidence": reading.confidence if reading is not None else None,
                "fallback": not confident,
                "attempt": attempt,
                "error_type": error_type or ("" if confident else "LowConfidence"),
            },
        )
        if reading is not None:
            return reading if confident else None
        if error_type in ("NotConfigured", "InvalidResponse"):
            return None  # a retry would not help
    return None
