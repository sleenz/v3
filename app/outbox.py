"""`_outbox`: every message the bot sends, so a reply to it finds its task (DESIGN section 22).

Recording never raises: a message that went out must not fail because the log could not be
written. Lookups read the tab fresh (another run may have just sent). Logs carry counts and
error types only, never names or text.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timedelta

from app import clock
from app.models import OUTBOX, Channel, OutboxRow
from app.sheets import SchemaError, SheetStore

logger = logging.getLogger("grc.outbox")

KEEP_DAYS = 60  # rows older than this are dropped by `prune`


def record(
    store: SheetStore,
    channel: Channel,
    message_id: str | None,
    person: str,
    task_ids: Iterable[str],
    kind: str,
    thread_id: str = "",
    now: datetime | None = None,
) -> None:
    """Log one sent message. No message id (the API gave none): nothing to map, nothing kept."""
    if not message_id:
        return
    row = OutboxRow(
        sent_at=now or clock.now(),
        channel=channel,
        message_id=message_id,
        thread_id=thread_id,
        person=person,
        task_ids=", ".join(dict.fromkeys(t for t in task_ids if t)),
        kind=kind,
    )
    try:
        store.append_service(OUTBOX, [row])
    except SchemaError:
        logger.warning("outbox_missing")
    except Exception as error:
        logger.warning("outbox_not_recorded", extra={"error_type": type(error).__name__})


def rows(store: SheetStore) -> list[OutboxRow]:
    try:
        return store.service_rows(OUTBOX, OutboxRow)
    except SchemaError:
        return []


def by_message(store: SheetStore, message_id: str) -> OutboxRow | None:
    """The sent message a WhatsApp reply or button tap points at (its `context.id`)."""
    if not message_id:
        return None
    return next((row for row in rows(store) if row.message_id == message_id), None)


def by_thread(store: SheetStore, thread_id: str) -> list[OutboxRow]:
    """Bot messages in a Gmail thread, newest first."""
    if not thread_id:
        return []
    found = [row for row in rows(store) if row.thread_id == thread_id]
    return sorted(found, key=lambda row: row.sent_at, reverse=True)


def thread_task(store: SheetStore, thread_id: str) -> str | None:
    """The one task an email thread is about, if the bot's messages in it name exactly one."""
    tasks = {task for row in by_thread(store, thread_id) for task in row.tasks}
    return next(iter(tasks)) if len(tasks) == 1 else None


def prune(store: SheetStore, now: datetime | None = None) -> int:
    """Drop rows older than KEEP_DAYS; returns how many."""
    now = now or clock.now()
    current = rows(store)
    keep = [row for row in current if row.sent_at >= now - timedelta(days=KEEP_DAYS)]
    if len(keep) != len(current):
        store.rewrite_service(OUTBOX, keep)
    return len(current) - len(keep)
