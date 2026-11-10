"""Event notices outside working hours wait in `_event_queue` (DESIGN section 22).

Assigned, review requested / approved / returned (and, later, blocked and extension notices)
go out at once between Config `Workday start` and `Workday end` on a working day. Raised at any
other time, they are queued and sent by `/jobs/reminders` at the next Workday start. If the
queue tab is missing or cannot be written, the notice is sent at once instead: a notice is
never lost (D15).
"""

from __future__ import annotations

import logging
from datetime import datetime

from app import clock
from app.models import EVENT_QUEUE, QueuedEvent
from app.reminders import within_workday
from app.sheets import SchemaError, SheetStore

logger = logging.getLogger("grc.events")


def should_wait(store: SheetStore, now: datetime | None = None) -> bool:
    """True outside working hours (weekend, holiday, before start or after end)."""
    return not within_workday(now or clock.now(), store.config())


def queue_event(
    store: SheetStore, kind: str, task_id: str, note: str = "", now: datetime | None = None
) -> bool:
    """Queue a notice for the next Workday start. False: could not queue (send it now)."""
    row = QueuedEvent(queued_at=now or clock.now(), kind=kind, task_id=task_id, note=note)
    try:
        store.append_service(EVENT_QUEUE, [row])
    except SchemaError:
        logger.warning("event_queue_missing", extra={"task_id": task_id})
        return False
    except Exception as error:
        logger.warning(
            "event_not_queued", extra={"task_id": task_id, "error_type": type(error).__name__}
        )
        return False
    logger.info("event_queued", extra={"task_id": task_id, "kind": kind})
    return True


def flush_events(store: SheetStore, gmail: object, sheet_url: str, wa: object = None) -> int:
    """Send every queued notice (inside working hours); a failed one stays for the next run.
    Returns how many were sent."""
    from app.gmail import GmailAuthError
    from app.lifecycle import notify

    try:
        queued = store.service_rows(EVENT_QUEUE, QueuedEvent)
    except SchemaError:
        return 0
    if not queued:
        return 0
    tasks = {task.id: task for task in store.tasks()}
    left: list[QueuedEvent] = []
    sent = 0
    for item in queued:
        task = tasks.get(item.task_id)
        if task is None:
            continue  # the row was deleted: nothing to announce
        try:
            notify(
                item.kind,
                task,
                store,
                gmail,
                sheet_url,
                queue_when_closed=False,  # type: ignore[arg-type]
                note=item.note,
                wa=wa,
            )
            sent += 1
        except GmailAuthError:
            left.append(item)
            store.rewrite_service(EVENT_QUEUE, left + queued[queued.index(item) + 1 :])
            raise
        except Exception as error:
            left.append(item)
            logger.error(
                "queued_event_failed",
                extra={"task_id": item.task_id, "error_type": type(error).__name__},
            )
    store.rewrite_service(EVENT_QUEUE, left)
    return sent
