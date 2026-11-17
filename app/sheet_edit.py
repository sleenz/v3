"""The Manager changes the sheet by prompt (DESIGN section 29).

"ubah deadline G-0012 jadi 5 Des", "set Staff A Reports to Staff K", "reminder pagi jam 9:30":
the AI (or the fixed form `edit G-0012 Priority = High`) turns the message into cell changes on
Master, Staff or Config. Code then checks every change: the tab, the row (task ID, Staff name,
Config setting) and the column must exist, formula and key columns are never written, and values
are read by rules (dates, progress, status). The Manager sees each change as before → after and
it is applied only on YES (the question goes to his own inbox or phone). Every change is written
to AuditLog; Master changes also to ProgressLog, so `undo` works for them.
"""

from __future__ import annotations

import json
import logging
import re
import time as _time
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from app.commands import progress_fraction, task_id_in
from app.extract.ai import AIProvider, NotConfigured, NullProvider, _executor
from app.models import AppConfig, AuditEntry, StaffMember, Task
from app.route import match_staff_name
from app.sheets import DATE_COLUMNS, DATETIME_COLUMNS, SheetStore

logger = logging.getLogger("grc.sheet_edit")

Tab = Literal["Master", "Staff", "Config"]
PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "edit_v1.md"
PROMPT_VERSION = "edit_v1"
TIMEOUT_SECONDS = 10.0
MIN_CONFIDENCE = 0.6
MAX_CHANGES = 20
# Never written by prompt: keys, formulas, and what the service itself owns.
PROTECTED = {
    "Master": {"ID", "Weight", "Source ID", "Original message", "Files", "Last updated"},
    "Staff": {"Name", "Weighted open load", "Staff ID"},
    "Config": set(),
}
STATUSES = ("Proposed", "Not Started", "In Progress", "In Review", "Approved", "Returned",
            "On Hold", "Done", "Cancelled")  # fmt: skip
_RULE = re.compile(
    r"^\s*(?:edit|ubah|set)\s+(?:(?P<tab>staff|config|master)\s+)?(?P<key>.+?)\s+"
    r"(?P<column>[^=]+?)\s*=\s*(?P<value>.*)$",
    re.IGNORECASE,
)


class Change(BaseModel):
    """One cell: the tab, the row key (task ID, Staff name or Config setting), the column
    (`Value` for Config) and the new value, with the value it replaces once checked."""

    tab: Tab
    key: str
    column: str
    value: str
    before: str = ""


class EditReading(BaseModel):
    is_edit: bool = False
    changes: list[Change] = Field(default_factory=list)
    reply_to_user: str = ""
    confidence: float = Field(default=0.0, ge=0, le=1)


EDIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "is_edit": {"type": "boolean"},
        "changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tab": {"type": "string", "enum": ["Master", "Staff", "Config"]},
                    "key": {"type": "string"},
                    "column": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["tab", "key", "column", "value"],
            },
        },
        "reply_to_user": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["is_edit", "changes", "reply_to_user", "confidence"],
}


# --- Reading -------------------------------------------------------------------------------------


def by_rules(text: str) -> list[Change] | None:
    """The fixed form, one change per line: `edit G-0012 Priority = High`,
    `edit staff Staff A Reports to = Staff K`, `edit config Reminder time morning = 09:30`.
    None when no line has that form."""
    changes: list[Change] = []
    for line in (text or "").splitlines():
        found = _RULE.match(line)
        if not found:
            continue
        tab = (found.group("tab") or "").capitalize()
        key, column = found.group("key").strip(), found.group("column").strip()
        if not tab:
            tab = "Master" if task_id_in(key) else ""
        if tab == "Config":  # "edit config Reminder time morning = 09:30": all of it is the key
            key, column = f"{key} {column}".strip(), "Value"
        if tab not in ("Master", "Staff", "Config"):
            continue
        changes.append(Change(tab=tab, key=key, column=column, value=found.group("value")))  # type: ignore[arg-type]
    return changes or None


def _catalog(store: SheetStore) -> str:
    staff = ", ".join(p.name for p in store.staff())
    return "\n".join(
        [
            "Master columns: " + ", ".join(store.master_header()),
            "Staff columns: " + ", ".join(store.staff_header()),
            "Staff names: " + staff,
            "Config settings: " + ", ".join(store.setting_names()),
        ]
    )


def by_ai(ai: AIProvider, masked_text: str, store: SheetStore, today: date) -> EditReading | None:
    """The AI's reading, or None when it is off, fails, times out or is unsure."""
    if isinstance(ai, NullProvider):
        return None
    prompt = (
        PROMPT_PATH.read_text(encoding="utf-8")
        .replace("{{today}}", today.isoformat())
        .replace("{{sheet}}", _catalog(store))
        .replace("{{message}}", masked_text[:2000])
    )
    started = _time.monotonic()
    reading: EditReading | None = None
    error_type = ""
    try:
        reading = _executor.submit(ai.read_edit, prompt).result(timeout=TIMEOUT_SECONDS)
    except (FutureTimeout, NotConfigured, ValidationError, ValueError) as error:
        error_type = type(error).__name__
    except Exception as error:  # the AI never stops the bot
        error_type = type(error).__name__
    logger.info(
        "ai_edit_call",
        extra={
            "provider": ai.name, "model": ai.model, "prompt_version": PROMPT_VERSION,
            "latency_ms": round((_time.monotonic() - started) * 1000),
            "confidence": reading.confidence if reading else None, "error_type": error_type,
            "n_changes": len(reading.changes) if reading else 0,
        },
    )  # fmt: skip
    if reading is None or reading.confidence < MIN_CONFIDENCE:
        return None
    return reading


# --- Checking ------------------------------------------------------------------------------------


def _header_named(name: str, header: list[str]) -> str | None:
    wanted = " ".join(name.split()).casefold()
    return next((h for h in header if h.casefold() == wanted), None)


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return f"{value:%Y-%m-%d %H:%M}"
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _value(column: str, raw: str, today: date) -> tuple[Any, str]:
    """The value to write and a problem ("" when fine). Dates, progress and Status are read
    by rules; anything else is written as typed. `-` or `blank` clears the cell."""
    from app.submission import day_in

    text = raw.strip()
    if text.casefold() in {"-", "blank", "kosong", "clear", "hapus", "none"}:
        return "", ""
    if column in DATE_COLUMNS:
        try:
            return date.fromisoformat(text), ""
        except ValueError:
            day = day_in(text, today)
            return (day, "") if day else (None, f'"{raw}" is not a date')
    if column in DATETIME_COLUMNS:  # e.g. Submitted at (QA6-C5)
        try:
            return datetime.fromisoformat(text), ""
        except ValueError:
            return None, f'"{raw}" is not a date and time (YYYY-MM-DD HH:MM)'
    if column == "Progress":
        fraction = progress_fraction(text.rstrip("%").strip())  # "50%" (QA6-C3)
        return (fraction, "") if fraction is not None else (None, "progress is 0-100")
    if column == "Status":
        status = next((s for s in STATUSES if s.casefold() == text.casefold()), None)
        return (status, "") if status else (None, f'"{raw}" is not a Status')
    return text, ""


PEOPLE_COLUMNS = {"Owner", "Doer", "Reviewer", "Support"}


def _people(text: str, staff: list[StaffMember], many: bool) -> tuple[str, str]:
    """Names written into Owner / Doer / Reviewer / Support are matched to Staff by rules, as
    for a PROMPT (section 21): the full Staff name, or a reason."""
    parts = [p.strip() for p in re.split(r"[;,]|\band\b|\bdan\b", text)] if many else [text]
    names = []
    for part in (p for p in parts if p):
        person = match_staff_name(part, staff)
        if person is None:
            return "", f'"{part}" is not in Staff'
        names.append(person.name)
    return "; ".join(dict.fromkeys(names)), ""


def _staff_and_column(
    change: Change, staff: list[StaffMember], header: list[str]
) -> tuple[StaffMember | None, str | None]:
    """The person and the Staff column. The fixed form cannot tell where a name ends
    ("edit staff Budi Hartono Reports to = …"), so every split is tried, longest name first."""
    person = match_staff_name(change.key, staff)
    column = _header_named(change.column, header)
    if person is not None and column is not None:
        return person, column
    words = f"{change.key} {change.column}".split()
    for cut in range(len(words) - 1, 0, -1):
        found = match_staff_name(" ".join(words[:cut]), staff)
        named = _header_named(" ".join(words[cut:]), header)
        if found is not None and named is not None:
            return found, named
    return person, column


def _still_loads(
    tab: str,
    key: str,
    column: str,
    value: Any,
    row: dict[str, Any],
    rows_after: dict[tuple[str, str], dict[str, Any]],
    config_after: dict[str, Any],
) -> str:
    """A problem when the row (Master task, Staff person) or the whole Config would no longer
    load with the new value: such a value would stop every later job (D15, QA6-C6/C7)."""
    try:
        if tab == "Config":
            trial = {**config_after, key: value}
            AppConfig.model_validate(trial)
            config_after.update(trial)
            return ""
        merged = {**rows_after.get((tab, key), row), column: value}
        (Task if tab == "Master" else StaffMember).model_validate(merged)
        rows_after[(tab, key)] = merged
        return ""
    except ValidationError:
        return f'"{value}" is not a value the sheet can use there'


def check(changes: list[Change], store: SheetStore, today: date) -> tuple[list[Change], list[str]]:
    """The changes that can be written (key and column as in the sheet, value read, the old
    value kept for the preview) and a reason for each one that cannot."""
    good: list[Change] = []
    problems: list[str] = []
    tasks = {t.id: t for t in store.tasks() if t.id}
    staff = store.staff()
    settings = {name.casefold(): name for name in store.setting_names()}
    config_after = dict(store.setting_values())  # every Config change must still load (QA6-C6)
    rows_after: dict[tuple[str, str], dict[str, Any]] = {}
    if len(changes) > MAX_CHANGES:  # QA6-C4
        problems.append(f"{len(changes) - MAX_CHANGES} more changes left out (at most "
                        f"{MAX_CHANGES} per message)")  # fmt: skip
    for change in changes[:MAX_CHANGES]:
        if change.tab == "Master":
            task_id = task_id_in(change.key)
            task = tasks.get(task_id or "")
            column = _header_named(change.column, store.master_header())
            if task is None:
                problems.append(f'"{change.key}" is not a task in Master')
                continue
            row = task.model_dump(by_alias=True)
            key = task.id
        elif change.tab == "Staff":
            person, column = _staff_and_column(change, staff, store.staff_header())
            if person is None:
                problems.append(f'"{change.key}" is not in Staff')
                continue
            row = person.model_dump(by_alias=True)
            key = person.name
        else:
            key = settings.get(" ".join(change.key.split()).casefold(), change.key.strip())
            column = "Value"
            row = {"Value": store.setting_values().get(key, "")}
        if column is None:
            problems.append(f'{change.tab} has no column "{change.column}"')
            continue
        if column in PROTECTED[change.tab]:
            problems.append(f"{change.tab} {column} is not changed by prompt")
            continue
        value, problem = _value(column, change.value, today)
        if not problem and change.tab == "Master" and column in PEOPLE_COLUMNS and value:
            value, problem = _people(str(value), staff, many=column == "Support")
        if not problem:
            problem = _still_loads(change.tab, key, column, value, row, rows_after, config_after)
        if problem:
            problems.append(f"{key} {column}: {problem}")
            continue
        good.append(Change(tab=change.tab, key=key, column=column, value=_cell(value),
                           before=_cell(row.get(column))))  # fmt: skip
    return good, problems


# --- Showing and applying ------------------------------------------------------------------------


def dump(changes: list[Change]) -> str:
    return json.dumps([c.model_dump() for c in changes], ensure_ascii=False)


def load(text: str) -> list[Change]:
    return [Change.model_validate(item) for item in json.loads(text or "[]")]


def preview(changes: list[Change]) -> str:
    """One line per change: `G-0012 · Due date: 2026-11-20 → 2026-12-05`."""
    lines = []
    for change in changes:
        where = change.key if change.tab == "Master" else f"{change.tab} {change.key}"
        column = "" if change.tab == "Config" else f" · {change.column}"
        lines.append(f"{where}{column}: {change.before or '(blank)'} → {change.value or '(blank)'}")
    return "\n".join(lines)


def apply(
    changes: list[Change],
    store: SheetStore,
    actor: str,
    now: datetime,
    message_id: str = "",
    audit_user: str = "",
    channel: str = "Email",
) -> list[str]:
    """Write the checked changes; return what was written (one line each). Master goes through
    `update_task` (with Last updated), Staff through `update_staff`, Config through
    `update_config`. AuditLog gets every cell."""
    from app import progress

    written: list[str] = []
    audits: list[AuditEntry] = []
    by_task: dict[str, dict[str, Any]] = {}
    for change in changes:
        value: Any = change.value
        if change.tab == "Master" and change.column in DATE_COLUMNS and value:
            value = date.fromisoformat(value)
        if change.tab == "Master" and change.column == "Progress" and value:
            value = progress_fraction(value)
        if change.tab == "Master":
            by_task.setdefault(change.key, {})[change.column] = value
        elif change.tab == "Staff":
            store.update_staff(change.key, {change.column: value})
        else:
            store.update_config(change.key, value)
        audits.append(
            AuditEntry(
                timestamp=now,
                user=audit_user or actor,
                task_id=change.key if change.tab == "Master" else "",
                action=f"Prompt edit ({change.tab})",
                field=change.column if change.tab != "Config" else change.key,
                old_value=change.before,
                new_value=change.value,
            )
        )
        written.append(preview([change]))
    tasks = {t.id: t for t in store.tasks() if t.id}
    for task_id, cells in by_task.items():
        store.update_task(task_id, {**cells, "Last updated": now})
        if task_id in tasks:
            progress.log_changes(store, tasks[task_id], cells, actor, channel, message_id,
                                 "Prompt edit", now)  # fmt: skip
    try:
        store.append_audit(audits)
    except Exception as error:
        logger.error("audit_write_failed", extra={"error_type": type(error).__name__})
    logger.info("prompt_edit_applied", extra={"n_changes": len(changes)})
    return written
