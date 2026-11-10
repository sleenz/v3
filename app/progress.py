"""ProgressLog, snooze and undo (DESIGN section 22, part B).

`ProgressLog` (visible, protected) records every field a person changed through the bot: who,
which channel and message, old and new value. Undo reads it; incoming messages are deduped by
its `Message ID`. `_snooze` holds "no reminders about this task for me until …".

Writes never raise for a missing tab: the change itself is in Master and AuditLog already;
a missing ProgressLog only costs the undo (logged as a warning).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from app import clock
from app.models import PROGRESS_LOG, SNOOZE, ProgressRow, SnoozeRow, Task
from app.sheets import SchemaError, SheetStore

logger = logging.getLogger("grc.progress")

UNDO_HOURS = 24
NO_UNDO = ("Approved", "Returned", "Undo", "Extension approved", "Extension rejected")
DATE_FIELDS = frozenset({"Due date", "Submitted date"})
EXTENSION_FIELD = "Extension requested"
SNOOZE_FIELD = "Snooze until"


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return f"{value:%Y-%m-%d %H:%M}"
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def rows(store: SheetStore) -> list[ProgressRow]:
    try:
        return store.service_rows(PROGRESS_LOG, ProgressRow)
    except SchemaError:
        return []


def append(store: SheetStore, entries: list[ProgressRow]) -> None:
    try:
        store.append_service(PROGRESS_LOG, entries)
    except SchemaError:
        logger.warning("progress_log_missing")
    except Exception as error:
        logger.warning("progress_log_failed", extra={"error_type": type(error).__name__})


def log_changes(
    store: SheetStore,
    before: Task,
    changes: dict[str, Any],
    person: str,
    channel: str,
    message_id: str,
    note: str,
    now: datetime | None = None,
) -> None:
    """One row per changed Master field (not `Last updated`)."""
    now = now or clock.now()
    old = before.model_dump(by_alias=True)
    entries = [
        ProgressRow(
            timestamp=now,
            task_id=before.id,
            person=person,
            channel=channel,
            field=header,
            old_value=_text(old.get(header)),
            new_value=_text(value),
            note=note,
            message_id=message_id,
        )
        for header, value in changes.items()
        if header != "Last updated"
    ]
    append(store, entries)


def log_note(
    store: SheetStore,
    task_id: str,
    person: str,
    channel: str,
    message_id: str,
    field: str,
    old: str,
    new: str,
    note: str,
    now: datetime | None = None,
) -> None:
    """A row for something that is not a Master field (extension request, snooze)."""
    append(store, [ProgressRow(timestamp=now or clock.now(), task_id=task_id, person=person,
                               channel=channel, field=field, old_value=old, new_value=new,
                               note=note, message_id=message_id)])  # fmt: skip


def already_processed(store: SheetStore, message_id: str) -> bool:
    """Meta and Gmail can deliver a message twice: a change is applied once per message id."""
    return bool(message_id) and any(row.message_id == message_id for row in rows(store))


def last_change(
    store: SheetStore, person: str, task_id: str | None, now: datetime | None = None
) -> list[ProgressRow]:
    """The person's most recent change (all fields written at that moment) within 24 hours,
    on `task_id` or on any task; [] when there is nothing to undo."""
    now = now or clock.now()
    since = now - timedelta(hours=UNDO_HOURS)
    mine = [
        row
        for row in rows(store)
        if row.person.casefold() == person.casefold()
        and row.timestamp >= since
        and (task_id is None or row.task_id == task_id)
    ]
    if not mine:
        return []
    newest = max(mine, key=lambda row: row.timestamp)
    group = [
        row for row in mine if row.timestamp == newest.timestamp and row.task_id == newest.task_id
    ]
    return group


def undoable(group: list[ProgressRow]) -> bool:
    return bool(group) and not any(row.note.startswith(NO_UNDO) for row in group)


def typed(field: str, text: str) -> Any:
    """A ProgressLog text value back to what Master holds (for undo)."""
    if not text:
        return ""
    if field == "Progress":
        try:
            return float(text)
        except ValueError:
            return text
    if field in DATE_FIELDS:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return text
    return text


def last_extension(store: SheetStore, task_id: str) -> ProgressRow | None:
    """The newest extension request for the task (its New value is the date asked for)."""
    asked = [r for r in rows(store) if r.task_id == task_id and r.field == EXTENSION_FIELD]
    return max(asked, key=lambda r: r.timestamp) if asked else None


# --- Snooze -------------------------------------------------------------------------------------


def snoozes(store: SheetStore) -> list[SnoozeRow]:
    try:
        return store.service_rows(SNOOZE, SnoozeRow)
    except SchemaError:
        return []


def set_snooze(
    store: SheetStore, task_id: str, person: str, until: date, now: datetime | None = None
) -> None:
    """Replace any snooze of this task for this person (and drop expired rows)."""
    today = (now or clock.now()).date()
    keep = [
        row
        for row in snoozes(store)
        if row.until > today
        and not (row.task_id == task_id and row.person.casefold() == person.casefold())
    ]
    keep.append(SnoozeRow(task_id=task_id, person=person, until=until, set_at=now or clock.now()))
    store.rewrite_service(SNOOZE, keep)


def clear_snooze(store: SheetStore, task_id: str, person: str) -> None:
    current = snoozes(store)
    keep = [
        row
        for row in current
        if not (row.task_id == task_id and row.person.casefold() == person.casefold())
    ]
    if len(keep) != len(current):
        store.rewrite_service(SNOOZE, keep)


def snoozed(store: SheetStore, today: date) -> set[tuple[str, str]]:
    """(task ID, person casefolded) with no reminders today: snoozed until a later day."""
    return {(row.task_id, row.person.casefold()) for row in snoozes(store) if row.until > today}
