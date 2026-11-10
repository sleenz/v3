"""Typed replies: AI first, rules as fallback, nothing written without confirmation (DESIGN 22 C).

Shared by WhatsApp and email. `understand()` turns one typed message into a proposed command,
a question, a list or help request, a new request (WhatsApp intake), or nothing:
- the task: the message the person replied to (WhatsApp `context.id` or email thread → `_outbox`),
  else a task ID in the text, else the AI's `task_id`, else the person's only open task;
- the AI (`app/extract/reply.py`) reads intent, progress, note and date; when it is off, fails,
  returns invalid JSON or is under 0.6 confident, the section 19 parser decides;
- a change is proposed, never applied: it waits in `_conversations` (step `confirm`, 30 minutes)
  until the person answers Ya / YES. Questions are answered from the Sheet at once.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal

from app.commands import Command, parse_command, task_id_in
from app.extract.ai import AIProvider
from app.extract.clean import mask_text
from app.extract.reply import ReplyReading, TaskLine, read_reply
from app.lifecycle import CHANGES
from app.models import CONVERSATIONS, Channel, Conversation, StaffMember, Task
from app.route import default_reviewer
from app.sheets import SchemaError, SheetStore

Kind = Literal["change", "ask", "pick", "question", "list", "help", "request", "nothing", "now"]
CONFIRM_MINUTES = 30
YES_WORDS = frozenset({"ya", "yes", "y", "ok", "oke", "iya", "betul", "benar", "confirm", "setuju"})
NO_WORDS = frozenset({"batal", "no", "tidak", "cancel", "gak", "nggak", "jangan"})
_HALF = re.compile(r"^(?:udah\s+|sudah\s+)?(?:setengah(?:\s+jalan)?|half(?:\s*way)?)$", re.I)
_HELP = frozenset({"help", "bantuan", "menu", "/help", "tolong"})


@dataclass
class Understanding:
    kind: Kind
    command: Command | None = None
    task_id: str | None = None
    via: Literal["ai", "rules"] = "rules"
    step: str = ""  # for kind "ask": the question to start (blocked_reason, extension_date, …)
    ai_text: str = ""


@dataclass
class Pending:
    """A proposed change waiting for confirmation (stored as JSON in `_conversations.Value`)."""

    name: str
    task_id: str | None
    value: str = ""
    message_id: str = ""  # the message that asked for it (dedupe and ProgressLog)
    media: list[dict[str, str]] = field(default_factory=list)  # WhatsApp files to fetch on Ya
    email_id: str = ""  # the email whose attachments go with it

    def command(self) -> Command:
        return Command(self.name, self.task_id, self.value)  # type: ignore[arg-type]

    def dump(self) -> str:
        return json.dumps(self.__dict__, ensure_ascii=False)

    @classmethod
    def load(cls, text: str) -> Pending:
        return cls(**json.loads(text))


def _pct(task: Task) -> int:
    value = task.progress_value
    return round(value * 100) if value is not None else 0


def task_lines(tasks: list[Task]) -> list[TaskLine]:
    return [
        TaskLine(
            id=t.id,
            title=t.title,
            due=t.due_date,
            status=t.status,
            progress=_pct(t),
            needs_review=t.review_needed,
        )
        for t in tasks
    ]


def _from_reading(
    reading: ReplyReading, task_id: str | None, tasks: dict[str, Task], has_files: bool
) -> Understanding:
    intent = reading.intent
    via: Literal["ai"] = "ai"
    if intent in ("list", "help", "request"):
        return Understanding(intent, via=via, ai_text=reading.reply_to_user)
    if intent == "question":
        return Understanding("question", task_id=task_id, via=via)
    if intent == "unknown":
        return Understanding("nothing", task_id=task_id, via=via)
    if task_id is None:
        return Understanding("pick", via=via)
    task = tasks.get(task_id)
    note = (reading.note or "").strip()
    if intent == "progress":
        if reading.progress is None:
            return Understanding("nothing", task_id=task_id, via=via)
        command = Command("progress", task_id, str(reading.progress))
    elif intent == "blocked":
        if not note:
            return Understanding("ask", task_id=task_id, via=via, step="blocked_reason")
        command = Command("blocked", task_id, note)
    elif intent == "extension":
        if reading.requested_date is None:
            return Understanding("ask", task_id=task_id, via=via, step="extension_date")
        command = Command("extension", task_id, reading.requested_date.isoformat())
    elif intent == "done":
        if not has_files and not (task and task.link.strip()):
            return Understanding("ask", task_id=task_id, via=via, step="done_proof")
        command = Command("done", task_id)
    elif intent == "review":
        command = Command("review", task_id)
    elif intent == "snooze":
        when = reading.requested_date.isoformat() if reading.requested_date else ""
        command = Command("snooze", task_id, when)
    else:  # note
        command = Command("note", task_id, note)
    return Understanding("change", command, task_id, via, ai_text=reading.reply_to_user)


def understand(
    text: str,
    person: StaffMember,
    open_tasks: list[Task],
    linked: str | None,
    ai: AIProvider,
    ai_on: bool,
    mask_emails: bool,
    today: date,
    has_files: bool = False,
) -> Understanding:
    """One typed message → what to do. Never writes anything."""
    tasks = {t.id: t for t in open_tasks}
    single = open_tasks[0].id if len(open_tasks) == 1 else None
    named = task_id_in(text)
    known = linked or named  # mapping order: replied-to message, then an ID in the text
    reading = None
    if ai_on and text.strip():
        lines = task_lines(open_tasks)
        reading = read_reply(
            ai, mask_text(text, mask_emails=mask_emails), person.role, lines, linked, today
        )
    if reading is not None:
        return _from_reading(reading, known or reading.task_id or single, tasks, has_files)
    return _by_rules(text, known, single, tasks, has_files)


def _by_rules(
    text: str, known: str | None, single: str | None, tasks: dict[str, Task], has_files: bool
) -> Understanding:
    """The deterministic fallback: section 19 commands plus `setengah`, help and list words."""
    plain = " ".join(text.lower().split()).strip(" .!?")
    if plain in _HELP:
        return Understanding("help")
    context = known or single
    command = parse_command(text, has_context=context is not None)
    if command is None and _HALF.match(plain) and context:
        command = Command("progress", None, "50")
    if command is None:
        return Understanding("nothing", task_id=known)
    if command.name in ("mine", "today", "week", "all", "recurring", "files", "pause", "resume"):
        return Understanding("now", command, command.task_id or known)
    task_id = command.task_id or context
    if command.name == "undo":
        return Understanding("change", Command("undo", command.task_id or known), task_id)
    if task_id is None:
        return Understanding("pick", command)
    command = Command(command.name, task_id, command.value)
    if command.name == "blocked" and not command.value.strip():
        return Understanding("ask", task_id=task_id, step="blocked_reason")
    if command.name == "extension" and not command.value.strip():
        return Understanding("ask", task_id=task_id, step="extension_date")
    if command.name == "done" and not has_files and not tasks.get(task_id, Task()).link.strip():
        return Understanding("ask", task_id=task_id, step="done_proof")
    return Understanding("change", command, task_id)


# --- What the change says --------------------------------------------------------------------


def describe(command: Command, task: Task | None, files: list[str] | None = None) -> str:
    """The change in one line, for "Benar?" / "Reply YES to confirm"."""
    tid = command.task_id or "tugas terakhir Anda"
    value = command.value.strip()
    with_files = f" (file: {', '.join(files)})" if files else ""
    if command.name == "progress":
        return f"Progress {tid} → {value.rstrip('%')}%"
    if command.name == "blocked":
        return f"{tid} terhambat (On Hold): {value}"
    if command.name == "extension":
        return f"Minta {tid} mundur ke {value} (manager yang memutuskan)"
    if command.name == "review":
        return f"Kirim {tid} untuk review{with_files}"
    if command.name == "done":
        return f"{tid} selesai{with_files}"
    if command.name == "snooze":
        return f"Tanpa pengingat {tid} sampai {value or 'hari kerja berikutnya'}"
    if command.name == "note":
        return f"Catatan di {tid}: {value}"
    if command.name == "undo":
        return f"Batalkan perubahan terakhir Anda di {tid}"
    if command.name == "approve":
        return f"Approve {tid}"
    if command.name == "return":
        return f"Kembalikan {tid}: {value}"
    if command.name == "assign":
        return f"Serahkan {tid} ke {value}"
    if command.name == "extension_ok":
        return f"Setujui perpanjangan {tid}"
    if command.name == "extension_no":
        return f"Tolak perpanjangan {tid}"
    return f"{command.name} {tid} {value}".strip()


def answer(task: Task | None, staff: list[StaffMember], today: date) -> str:
    """A question about a task, answered from the Sheet (no AI wording, no confirmation)."""
    if task is None:
        return "Tugas yang mana? Kirim ID-nya, mis. kapan deadline G-0012?"
    if task.due_date is not None:
        left = (task.due_date - today).days
        when = (
            f"{-left} hari terlambat"
            if left < 0
            else "hari ini"
            if left == 0
            else f"{left} hari lagi"
        )
        due = f"due {task.due_date:%a %d %b %Y} ({when})"
    else:
        due = "belum ada due date"
    reviewer = default_reviewer(task.reviewer, task.doer, staff) if task.review_needed else None
    review = f", reviewer {reviewer.name}" if reviewer is not None else ""
    return (
        f"{task.id} – {task.title}: {due}, status {task.status}, progress {_pct(task)}%"
        f", doer {task.doer or '-'}{review}."
    )


def needs_confirmation(command: Command) -> bool:
    return command.name in CHANGES


# --- Pending confirmations -------------------------------------------------------------------


def _rows(store: SheetStore) -> list[Conversation]:
    try:
        return store.service_rows(CONVERSATIONS, Conversation)
    except SchemaError:
        return []


def propose(
    store: SheetStore, person: str, channel: Channel, pending: Pending, now: datetime
) -> bool:
    """Keep the change until Ya/YES (one open question per person). False: no tab to keep it."""
    rows = [
        r for r in _rows(store) if r.person.casefold() != person.casefold() and r.expires_at > now
    ]
    rows.append(
        Conversation(
            person=person,
            channel=channel,
            pending_action="confirm",
            task_id=pending.task_id or "-",
            value=pending.dump(),
            started_at=now,
            expires_at=now + timedelta(minutes=CONFIRM_MINUTES),
        )
    )
    try:
        store.rewrite_service(CONVERSATIONS, rows)
    except SchemaError:
        return False
    return True


def pending_confirm(
    store: SheetStore, person: str, channel: Channel, now: datetime
) -> Conversation | None:
    """The person's open confirmation on this channel (not removed)."""
    return next(
        (
            r
            for r in _rows(store)
            if r.person.casefold() == person.casefold()
            and r.channel == channel
            and r.pending_action == "confirm"
            and r.expires_at > now
        ),
        None,
    )


def drop_confirm(store: SheetStore, person: str) -> None:
    rows = _rows(store)
    keep = [
        r
        for r in rows
        if not (r.person.casefold() == person.casefold() and r.pending_action == "confirm")
    ]
    if len(keep) != len(rows):
        store.rewrite_service(CONVERSATIONS, keep)


def _short_answer(text: str, words: frozenset[str]) -> bool:
    """`ya`, `Yes.`, `yes, thanks`, `ok sip`: the first word is the answer, and the line is short
    (at most 4 words), so "ok, but the date is wrong" is not a yes."""
    tokens = re.findall(r"[a-z]+", text.lower())
    plain = " ".join(tokens)
    return (
        bool(tokens)
        and (plain in words or (tokens[0] in words and len(tokens) <= 4))
        and ("but" not in tokens and "tapi" not in tokens)
    )


def is_yes(text: str) -> bool:
    return _short_answer(text, YES_WORDS)


def is_no(text: str) -> bool:
    return _short_answer(text, NO_WORDS)
