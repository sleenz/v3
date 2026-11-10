"""WhatsApp buttons, lists and short conversations (DESIGN section 22, part B).

Button and list ids carry the action and the task, e.g. `prog:G-0012:50`; they are parsed here
by code only and never sent to the AI. Every change goes through `lifecycle.run_command`, so
permissions, AuditLog, ProgressLog and notices are the same as for typed commands and email.

A question that needs a typed answer (the reason it is blocked, the new date, the proof file,
the review file, the return notes) is a `_conversations` row that waits 30 minutes.

The 24-hour window (`_wa_window`): free-form messages (text, buttons, lists) only reach a person
who wrote to the bot in the last 24 hours; otherwise a notice goes as the template
`grc_update_tugas`, whose "Lihat tugas" button opens the window again.

Logs carry counts, kinds and task IDs only, never names, numbers or text.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from app import clock, outbox
from app.commands import Command
from app.lifecycle import CommandContext, CommandResult, reviewer_for, run_command
from app.models import (
    CONVERSATIONS,
    OPEN_STATUSES,
    WA_WINDOW,
    Conversation,
    ConversationStep,
    StaffMember,
    Task,
    WaWindowRow,
)
from app.reminders import TEMPLATE_LANGUAGE, channel_for
from app.route import default_reviewer, find_person, managers
from app.sheets import SchemaError, SheetStore

logger = logging.getLogger("grc.wa_ui")

CONVERSATION_MINUTES = 30
WINDOW_HOURS = 23.5  # Meta's window is 24 hours; keep a margin
MAX_CARDS = 5
TEMPLATE_UPDATE = "grc_update_tugas"
PROGRESS_STEPS = (25, 50, 75, 90, 100)
CANCEL_WORDS = frozenset({"batal", "cancel", "stop", "tidak jadi", "gak jadi"})
NO_FILE_WORDS = frozenset({"tanpa file", "skip", "no file", "tidak ada file", "lewati"})

QUESTIONS: dict[str, str] = {
    "blocked_reason": "Apa yang menghambat {task}? Ketik alasannya (atau batal).",
    "extension_date": "Mundur ke tanggal berapa untuk {task}? Contoh: 20 Nov (atau batal).",
    "done_proof": "Kirim file bukti untuk {task} (dokumen atau foto), atau ketik batal.",
    "review_file": "Kirim file untuk review {task}, atau ketik 'tanpa file' (atau batal).",
    "return_notes": "Apa yang perlu diperbaiki di {task}? Ketik catatannya (atau batal).",
}
EXPIRED_TEXT = "Waktu menjawab untuk {task} sudah habis (30 menit). Tidak ada yang diubah."


# --- Ids -------------------------------------------------------------------------------------


def tap_id(action: str, task_id: str = "", value: str = "") -> str:
    return ":".join(part for part in (action, task_id, value) if part)


def parse_tap(tap: str) -> tuple[str, str, str]:
    """`prog:G-0012:50` -> ("prog", "G-0012", "50"); `view:morning:2026-11-10` -> ("view",
    "morning", "2026-11-10")."""
    action, task_id, value = [*tap.split(":", 2), "", "", ""][:3]
    return action, task_id, value


# --- 24-hour window --------------------------------------------------------------------------


def touch_window(store: SheetStore, person: str, now: datetime | None = None) -> None:
    """The person just wrote to the bot: the window is open for 24 hours."""
    now = now or clock.now()
    try:
        rows = [r for r in store.service_rows(WA_WINDOW, WaWindowRow)
                if r.person.casefold() != person.casefold()]  # fmt: skip
        rows.append(WaWindowRow(person=person, last_inbound_at=now))
        store.rewrite_service(WA_WINDOW, rows)
    except SchemaError:
        logger.warning("wa_window_missing")


def window_open(store: SheetStore, person: str, now: datetime | None = None) -> bool:
    now = now or clock.now()
    try:
        rows = store.service_rows(WA_WINDOW, WaWindowRow)
    except SchemaError:
        return False
    last = next((r.last_inbound_at for r in rows if r.person.casefold() == person.casefold()),
                None)  # fmt: skip
    return last is not None and now - last < timedelta(hours=WINDOW_HOURS)


# --- Conversations ---------------------------------------------------------------------------


def _conversations(store: SheetStore) -> list[Conversation]:
    try:
        return store.service_rows(CONVERSATIONS, Conversation)
    except SchemaError:
        return []


def start_conversation(
    store: SheetStore, person: str, step: ConversationStep, task_id: str, now: datetime
) -> bool:
    """Ask a question; one open question per person (a new one replaces the old)."""
    rows = [r for r in _conversations(store) if r.person.casefold() != person.casefold()
            and r.expires_at > now]  # fmt: skip
    rows.append(
        Conversation(person=person, channel="WhatsApp", pending_action=step, task_id=task_id,
                     started_at=now, expires_at=now + timedelta(minutes=CONVERSATION_MINUTES))
    )  # fmt: skip
    try:
        store.rewrite_service(CONVERSATIONS, rows)
    except SchemaError:
        logger.warning("conversations_missing")
        return False
    return True


def take_conversation(
    store: SheetStore, person: str, now: datetime
) -> tuple[Conversation | None, Conversation | None]:
    """(open question, expired question) for the person; either is removed from the tab."""
    rows = _conversations(store)
    mine = [
        r
        for r in rows
        if r.person.casefold() == person.casefold() and r.pending_action != "confirm"
    ]  # confirmations: app/replies.py
    if not mine:
        return None, None
    try:
        store.rewrite_service(CONVERSATIONS, [r for r in rows if r not in mine])
    except SchemaError:
        return None, None
    newest = max(mine, key=lambda r: r.started_at)
    return (newest, None) if newest.expires_at > now else (None, newest)


# --- Cards and lists -------------------------------------------------------------------------


def _pct(task: Task) -> int:
    value = task.progress_value
    return round(value * 100) if value is not None else 0


def card_text(task: Task, today: date) -> str:
    if task.due_date is not None:
        left = (task.due_date - today).days
        when = (
            f"{-left} hari terlambat" if left < 0 else "hari ini" if left == 0 else f"{left} hari"
        )
        due = f"due {task.due_date:%d %b %Y} ({when})"
    else:
        due = "tanpa due date"
    return f"{task.id} – {task.title}, {due}, progress {_pct(task)}%, {task.status}"


def card_buttons(
    task: Task, person: StaffMember, staff: list[StaffMember]
) -> list[tuple[str, str]]:
    """[Update progres] [Kirim review | Selesai] [Lainnya]; the reviewer of a task In Review
    gets [Approve] [Return] instead."""
    reviewer = default_reviewer(task.reviewer, task.doer, staff)
    if task.status == "In Review" and (
        person.role == "Manager" or (reviewer is not None and reviewer.name == person.name)
    ):
        return [(tap_id("apv", task.id), "Approve"), (tap_id("ret", task.id), "Return")]
    second = (
        (tap_id("rev", task.id), "Kirim review")
        if task.review_needed and task.status != "Approved"
        else (tap_id("done", task.id), "Selesai")
    )
    return [
        (tap_id("upd", task.id), "Update progres"),
        second,
        (tap_id("more", task.id), "Lainnya"),
    ]


def my_tasks(person: StaffMember, tasks: list[Task], staff: list[StaffMember]) -> list[Task]:
    """Open tasks the person is Doer or Owner of, or reviews (In Review); earliest due first."""
    name = person.name.casefold()
    found = []
    for task in tasks:
        if task.status not in OPEN_STATUSES:
            continue
        reviewer = default_reviewer(task.reviewer, task.doer, staff)
        reviews = (
            task.status == "In Review"
            and reviewer is not None
            and (reviewer.name.casefold() == name)
        )
        if name in (task.doer.casefold(), task.owner.casefold()) or reviews:
            found.append(task)
    return sorted(found, key=lambda t: (t.due_date is None, t.due_date or date.max, t.id))


def _record(store: SheetStore, message_id: str | None, person: StaffMember, ids: list[str],
            kind: str) -> None:  # fmt: skip
    outbox.record(store, "WhatsApp", message_id, person.name, ids, kind)


def send_card(ctx: Any, person: StaffMember, task: Task, today: date) -> None:
    staff = ctx.store.staff()
    message_id = ctx.wa.send_buttons(person.wa_id, card_text(task, today),
                                     card_buttons(task, person, staff))  # fmt: skip
    _record(ctx.store, message_id, person, [task.id], "card")


def send_task_list(
    ctx: Any, person: StaffMember, tasks: list[Task], today: date, body: str
) -> None:
    rows = [
        (tap_id("card", t.id), f"{t.id} {t.title}",
         f"due {t.due_date:%d %b}" if t.due_date else t.status)
        for t in tasks[:10]
    ]  # fmt: skip
    if len(tasks) > 10:
        body += f"\n(+{len(tasks) - 10} lagi: kirim ID tugasnya, mis. G-0012)"
    message_id = ctx.wa.send_list(person.wa_id, body, "Pilih tugas", rows, "Tugas")
    _record(ctx.store, message_id, person, [t.id for t in tasks[:10]], "list")


def show_tasks(ctx: Any, person: StaffMember, now: datetime, focus: str = "") -> str:
    """The "Lihat tugas" tap: up to 5 task cards, the rest as a list. `focus` = one task."""
    tasks = [t for t in ctx.store.tasks() if t.id]
    staff = ctx.store.staff()
    today = now.date()
    if focus:
        task = next((t for t in tasks if t.id == focus), None)
        if task is None or task.status not in OPEN_STATUSES:
            _say(ctx, person, f"{focus} tidak ada atau sudah tidak terbuka.")
            return "tap_view_gone"
        send_card(ctx, person, task, today)
        return "tap_view_task"
    mine = my_tasks(person, tasks, staff)
    if not mine:
        _say(ctx, person, "Tidak ada tugas terbuka untuk Anda. 👍")
        return "tap_view_empty"
    for task in mine[:MAX_CARDS]:
        send_card(ctx, person, task, today)
    if len(mine) > MAX_CARDS:
        send_task_list(ctx, person, mine[MAX_CARDS:], today,
                       f"{len(mine) - MAX_CARDS} tugas lain:")  # fmt: skip
    return "tap_view"


def _say(ctx: Any, person: StaffMember, text: str, task_id: str = "") -> None:
    try:
        message_id = ctx.wa.send_text(person.wa_id, text)
    except Exception as error:
        logger.error("wa_reply_failed", extra={"error_type": type(error).__name__})
        return
    _record(ctx.store, message_id, person, [task_id], "reply")


# --- Taps ------------------------------------------------------------------------------------


def _run(ctx: Any, person: StaffMember, message: Any, command: Command,
         files: list | None = None) -> CommandResult:  # fmt: skip
    return run_command(
        command,
        CommandContext(
            store=ctx.store,
            gmail=ctx.gmail,
            actor=person,
            channel="WhatsApp",
            sheet_url=ctx.sheet_url,
            files=files or [],
            context_task_id=command.task_id,
            now=None,
            message_id=message.id,
            wa=ctx.wa,
        ),
    )


def handle_tap(message: Any, person: StaffMember, ctx: Any, now: datetime) -> str:
    """A reply button, list row or template quick-reply. Parsed by code only (no AI)."""
    action, task_id, value = parse_tap(message.tap)
    if action == "view":
        return show_tasks(ctx, person, now, focus=value if task_id == "task" else "")
    if action == "card":
        return show_tasks(ctx, person, now, focus=task_id)
    if not task_id:
        _say(ctx, person, "Tombol itu tidak dikenal. Kirim help untuk bantuan.")
        return "tap_unknown"
    if action == "upd":
        rows = [(tap_id("prog", task_id, str(p)), f"{p}%", "") for p in PROGRESS_STEPS]
        message_id = ctx.wa.send_list(person.wa_id, f"Progress {task_id} sekarang berapa?",
                                      "Pilih progress", rows, "Progress")  # fmt: skip
        _record(ctx.store, message_id, person, [task_id], "list")
        return "tap_progress_list"
    if action == "more":
        rows = [
            (tap_id("done", task_id), "Selesai", "Kirim bukti, tugas selesai"),
            (tap_id("blk", task_id), "Terhambat", "Tulis apa yang menghambat"),
            (tap_id("ext", task_id), "Minta perpanjangan", "Usulkan tanggal baru"),
            (tap_id("snz", task_id), "Ingatkan besok", "Tanpa pengingat sampai besok"),
            (tap_id("files", task_id), "Lihat file", "File dikirim ke email Anda"),
        ]
        message_id = ctx.wa.send_list(person.wa_id, f"Pilihan untuk {task_id}:", "Pilih",
                                      rows, "Lainnya")  # fmt: skip
        _record(ctx.store, message_id, person, [task_id], "list")
        return "tap_more"
    steps: dict[str, ConversationStep] = {
        "blk": "blocked_reason", "ext": "extension_date", "done": "done_proof",
        "rev": "review_file", "ret": "return_notes",
    }  # fmt: skip
    if action in steps:
        task = next((t for t in ctx.store.tasks() if t.id == task_id), None)
        if action == "done" and task is not None and task.link.strip():
            return _finish(ctx, person, message, Command("done", task_id))  # proof already in
        if not start_conversation(ctx.store, person.name, steps[action], task_id, now):
            _say(ctx, person, "Ketik saja perintahnya, mis. blocked G-0012 <alasan>.", task_id)
            return "tap_no_conversation"
        _say(ctx, person, QUESTIONS[steps[action]].format(task=task_id), task_id)
        return f"tap_ask_{steps[action]}"
    simple = {
        "prog": Command("progress", task_id, value),
        "snz": Command("snooze", task_id),
        "files": Command("files", task_id),
        "apv": Command("approve", task_id),
        "exty": Command("extension_ok", task_id, value),
        "extn": Command("extension_no", task_id, value),
    }
    if action in simple:
        return _finish(ctx, person, message, simple[action])
    _say(ctx, person, "Tombol itu tidak dikenal. Kirim help untuk bantuan.")
    return "tap_unknown"


def _finish(ctx: Any, person: StaffMember, message: Any, command: Command,
            files: list | None = None) -> str:  # fmt: skip
    result = _run(ctx, person, message, command, files)
    reply = result.reply
    if result.attachments and ctx.gmail is not None and person.email.strip():
        from app.models import OutgoingEmail
        from app.notify import with_files

        subject = f"[GRC] Files for {result.task_id}"
        email = OutgoingEmail(
            to=[person.email.strip()], subject=subject, text=reply, html=f"<p>{reply}</p>"
        )
        try:
            ctx.gmail.send(with_files(email, result.attachments), ctx.store.config().sender_name)
            reply += " File dikirim ke email Anda."
        except Exception as error:
            logger.error("files_email_failed", extra={"error_type": type(error).__name__})
    _say(ctx, person, reply, result.task_id or command.task_id or "")
    return f"tap_{command.name}"


# --- Answers to a question -------------------------------------------------------------------


def handle_answer(
    message: Any, person: StaffMember, ctx: Any, convo: Conversation, files: list
) -> str:
    """The person's answer to the open question (text or a file)."""
    text = message.text.strip()
    task_id = convo.task_id
    if text.lower().strip(" .!") in CANCEL_WORDS:
        _say(ctx, person, f"Dibatalkan. {task_id} tidak diubah.", task_id)
        return "answer_cancelled"
    step = convo.pending_action
    if step == "blocked_reason" and text:
        return _finish(ctx, person, message, Command("blocked", task_id, text))
    if step == "extension_date" and text:
        return _finish(ctx, person, message, Command("extension", task_id, text))
    if step == "return_notes" and text:
        return _finish(ctx, person, message, Command("return", task_id, text))
    if step == "done_proof" and files:
        return _finish(ctx, person, message, Command("done", task_id), files)
    if step == "review_file" and (files or text.lower() in NO_FILE_WORDS):
        return _finish(ctx, person, message, Command("review", task_id), files)
    # Not an answer yet: ask again, keeping the original deadline.
    start_conversation(ctx.store, person.name, step, task_id, convo.started_at)
    _say(ctx, person, QUESTIONS[step].format(task=task_id), task_id)
    return "answer_again"


def expired(ctx: Any, person: StaffMember, convo: Conversation) -> None:
    _say(ctx, person, EXPIRED_TEXT.format(task=convo.task_id), convo.task_id)


# --- Notices on WhatsApp ---------------------------------------------------------------------


def _notice(event: str, task: Task, note: str, staff: list[StaffMember]
            ) -> list[tuple[StaffMember, str, list[tuple[str, str]]]]:  # fmt: skip
    """(person, text, buttons) for each person an event concerns."""
    due = f"{task.due_date:%d %b %Y}" if task.due_date else "-"
    doer = find_person(task.doer, staff)
    owner = find_person(task.owner, staff)
    out: list[tuple[StaffMember, str, list[tuple[str, str]]]] = []
    if event == "in_review":
        reviewer = reviewer_for(task, staff)
        if reviewer is not None:
            out.append(
                (
                    reviewer,
                    f"{task.doer or 'Doer'} mengirim {task.id} – {task.title} "
                    f"untuk review. Due {due}.",
                    [(tap_id("apv", task.id), "Approve"), (tap_id("ret", task.id), "Return")],
                )
            )
    elif event == "approved" and doer is not None:
        out.append(
            (
                doer,
                f"{task.id} disetujui reviewer. Kirim bukti untuk menyelesaikan.",
                [(tap_id("done", task.id), "Selesai")],
            )
        )
    elif event == "returned" and doer is not None:
        notes = f" Catatan: {task.review_notes}" if task.review_notes else ""
        out.append(
            (
                doer,
                f"{task.id} dikembalikan untuk diperbaiki.{notes}",
                card_buttons(task, doer, staff),
            )
        )
    elif event == "assigned" and doer is not None:
        out.append(
            (
                doer,
                f"Tugas baru untuk Anda: {card_text(task, clock.today())}",
                card_buttons(task, doer, staff),
            )
        )
    elif event == "blocked":
        people = {p.name.casefold(): p for p in [owner, *managers(staff)] if p is not None}
        for person in people.values():
            out.append((person, f"{task.id} – {task.title} terhambat: {note or '-'}", []))
    elif event == "extension_requested":
        shown, iso, who = [*note.split("|"), "", "", ""][:3]
        for manager in managers(staff):
            out.append(
                (
                    manager,
                    f"{who or 'Staff'} minta perpanjangan {task.id} – {task.title} "
                    f"dari {due} ke {shown}.",
                    [
                        (tap_id("exty", task.id, iso), "Setuju"),
                        (tap_id("extn", task.id, iso), "Tolak"),
                    ],
                )
            )
    elif event in ("extension_approved", "extension_rejected") and doer is not None:
        verdict = "disetujui" if event == "extension_approved" else "ditolak"
        out.append((doer, f"Perpanjangan {task.id} {verdict}. Due sekarang {due}.", []))
    return out  # fmt: skip


def notify_whatsapp(event: str, task: Task, store: SheetStore, wa: Any, note: str = "") -> int:
    """Tell WhatsApp-channel people about an event: buttons inside the 24-hour window, else the
    `grc_update_tugas` template (its button opens the window). Returns how many were sent."""
    staff = store.staff()
    sent = 0
    for person, text, buttons in _notice(event, task, note, staff):
        if person.paused or not person.wa_id or channel_for(person) != "WhatsApp":
            continue
        try:
            if window_open(store, person.name):
                message_id = (
                    wa.send_buttons(person.wa_id, text, buttons)
                    if buttons
                    else wa.send_text(person.wa_id, text)
                )
            else:
                message_id = wa.send_template(
                    person.wa_id, TEMPLATE_UPDATE, TEMPLATE_LANGUAGE, [task.id, text],
                    [tap_id("view", "task", task.id)],
                )  # fmt: skip
        except Exception as error:
            kind = type(error).__name__
            logger.error("wa_notice_failed", extra={"task_id": task.id, "error_type": kind})
            continue
        outbox.record(store, "WhatsApp", message_id, person.name, [task.id], event)
        sent += 1
    return sent
