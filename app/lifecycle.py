"""Task lifecycle with review, run by commands (DESIGN section 19, v1.4).

`run_command` is the one place a command changes a task, whichever channel it came from
(WhatsApp or an email reply), so both give the same result. Every change is written to AuditLog
with the channel; a wrong role or an unknown task changes nothing and says why.

Events (review requested, approved, returned, (re)assigned) go out at once by email to the
people concerned (`notify`); `_state` is updated with the new status so the 5-minute change job
does not announce the same change a second time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from html import escape
from typing import Any, Literal

from app import clock, outbox, progress, recurring
from app.commands import Command, is_me, progress_fraction
from app.events import queue_event, should_wait
from app.gmail import GmailAuthError, GmailClient
from app.models import (
    OPEN_STATUSES,
    Attachment,
    AuditEntry,
    OutgoingEmail,
    StaffMember,
    StateRow,
    Task,
)
from app.notify import with_files
from app.route import (
    default_reviewer,
    delegation_problem,
    find_person,
    managers,
    match_staff_name,
    owner_and_doer,
)
from app.sheets import SchemaError, SheetStore

logger = logging.getLogger("grc.lifecycle")

Channel = Literal["WhatsApp", "Email"]
REVIEWABLE = frozenset({"Not Started", "In Progress", "Returned"})
NOT_ACTIVE = frozenset({"Proposed", "Done", "Cancelled"})
EVENT_STATUSES = {"In Review": "in_review", "Approved": "approved", "Returned": "returned"}


@dataclass
class CommandContext:
    store: SheetStore
    gmail: GmailClient
    actor: StaffMember
    channel: Channel
    sheet_url: str = ""
    files: list[Attachment] = field(default_factory=list)  # sent with the command
    context_task_id: str | None = None  # the task of the message replied to
    now: datetime | None = None
    message_id: str = ""  # the incoming message (dedupe and ProgressLog, section 22)
    wa: Any = None  # WhatsAppClient: notices also go on WhatsApp (section 22)


@dataclass
class CommandResult:
    reply: str
    task_id: str | None = None  # the one task the reply is about (for "that" later)
    attachments: list[Attachment] = field(default_factory=list)  # files to send back
    changed: bool = False


def files_label(attachments: list[Attachment]) -> str:
    names = ", ".join(a.filename for a in attachments)
    return f"In the GRC bot mailbox: {names}" if names else ""


def _is_party(person: StaffMember, task: Task) -> bool:
    names = (task.owner.strip().casefold(), task.doer.strip().casefold())
    return person.role == "Manager" or person.name.casefold() in names


def _audit(
    store: SheetStore,
    ctx: CommandContext,
    task_id: str,
    action: str,
    changes: dict[str, Any],
    before: Task,
) -> None:
    now = ctx.now or clock.now()
    entries = [
        AuditEntry(
            timestamp=now,
            user=ctx.actor.email or ctx.actor.name,
            task_id=task_id,
            action=action,
            field=header,
            old_value=str(before.model_dump(by_alias=True).get(header) or ""),
            new_value=str(value if value is not None else ""),
            reason=f"Command via {ctx.channel}",
        )
        for header, value in changes.items()
        if header != "Last updated"
    ]
    try:
        store.append_audit(entries)
    except Exception as error:
        logger.error(
            "audit_write_failed", extra={"task_id": task_id, "error_type": type(error).__name__}
        )


def _record_state(store: SheetStore, task: Task) -> None:
    """Tell the change job this status is already announced (no second event)."""
    try:
        state = store.state()
        previous = state.get(task.id)
        state[task.id] = StateRow(
            id=task.id,
            status=task.status,
            owner=task.owner,
            doer=task.doer,
            notified_doer=previous.notified_doer if previous else "",
            notified_at=previous.notified_at if previous else None,
        )
        store.write_state(list(state.values()))
    except Exception as error:
        logger.error(
            "state_write_failed", extra={"task_id": task.id, "error_type": type(error).__name__}
        )


def _apply(
    ctx: CommandContext,
    task: Task,
    action: str,
    changes: dict[str, Any],
    event: str = "",
    note: str = "",
) -> Task:
    store = ctx.store
    now = ctx.now or clock.now()
    changes = {**changes, "Last updated": now}
    store.update_task(task.id, changes)
    _audit(store, ctx, task.id, action, changes, task)
    progress.log_changes(store, task, changes, ctx.actor.name, ctx.channel, ctx.message_id,
                         f"{action}: {note}" if note else action, now)  # fmt: skip
    updated = Task.model_validate({**task.model_dump(by_alias=True), **changes})
    if event:
        notify(event, updated, store, ctx.gmail, ctx.sheet_url, ctx.files, note=note, wa=ctx.wa)
        _record_state(store, updated)
    return updated


# --- Notifications (email; DESIGN section 19 moves them to the reminder engine in PR 2) --------


def _email_of(name: str, staff: list[StaffMember]) -> str:
    person = find_person(name, staff)
    return person.email.strip() if person and not person.paused else ""


def _send(
    store: SheetStore,
    gmail: GmailClient,
    to: str,
    subject: str,
    lines: list[str],
    files: list[Attachment] | None = None,
    person: str = "",
    task_id: str = "",
    kind: str = "",
) -> bool:
    if not to:
        return False
    text = "\n".join(lines)
    email = OutgoingEmail(to=[to], subject=subject, text=text, html=f"<pre>{escape(text)}</pre>")
    try:
        sent = gmail.send(with_files(email, files or []), store.config().sender_name)
        if sent is not None:
            outbox.record(store, "Email", sent.id, person, [task_id], kind, sent.thread_id)
        return True
    except GmailAuthError:
        raise
    except Exception as error:
        logger.error("event_email_failed", extra={"error_type": type(error).__name__})
        return False


def _subject(task: Task, what: str) -> str:
    return f"[GRC] {task.id} – {what}: {task.title}"


def _review_files(task: Task, gmail: GmailClient, sent: list[Attachment]) -> list[Attachment]:
    names = task.review_file.removeprefix("In the GRC bot mailbox:").strip()
    wanted = {name.strip() for name in names.split(",") if name.strip()}
    if sent:
        return sent
    if not wanted:
        return []
    return [f for f in gmail.task_files(task.id, task.source_id) if f.filename in wanted]


def notify(
    event: str,
    task: Task,
    store: SheetStore,
    gmail: GmailClient,
    sheet_url: str = "",
    files: list[Attachment] | None = None,
    queue_when_closed: bool = True,
    note: str = "",
    wa: Any = None,
) -> None:
    """Tell the people an event concerns. Paused people (Staff `Reminders paused`) get nothing.
    Outside working hours the notice waits in `_event_queue` for the next Workday start
    (DESIGN section 22); `/jobs/reminders` sends it then with `queue_when_closed=False`.
    Email always goes for `assigned` and `in_review` (they carry the files); the other notices
    go by WhatsApp instead of email to people whose channel is WhatsApp, when `wa` is given."""
    if queue_when_closed and should_wait(store) and queue_event(store, event, task.id, note):
        return
    staff = store.staff()
    if wa is not None:
        try:
            from app.wa_ui import notify_whatsapp

            notify_whatsapp(event, task, store, wa, note)
        except Exception as error:  # a WhatsApp problem never stops the email
            kind = type(error).__name__
            logger.error("wa_notice_failed", extra={"task_id": task.id, "error_type": kind})

    def email_of(name: str) -> str:
        if wa is not None and event not in ("assigned", "in_review"):
            from app.reminders import channel_for

            person = find_person(name, staff)
            if person is not None and person.wa_id and channel_for(person) == "WhatsApp":
                return ""  # told on WhatsApp
        return _email_of(name, staff)

    due = f"{task.due_date:%a %d %b %Y}" if task.due_date else "not set"
    link = f"Sheet: {sheet_url}" if sheet_url else ""
    if event == "in_review":
        reviewer = default_reviewer(task.reviewer, task.doer, staff)
        if reviewer is None:
            return
        lines = [
            f"{task.doer or 'The doer'} sent {task.id} for review: {task.title}",
            f"Due: {due}",
            "",
            "Reply to this email with the first line:",
            f"  approve {task.id}",
            f"  return {task.id} <what to change>",
            "",
            link,
        ]
        _send(
            store,
            gmail,
            _email_of(reviewer.name, staff),
            _subject(task, "review requested"),
            lines,
            _review_files(task, gmail, files or []),
            person=reviewer.name,
            task_id=task.id,
            kind="in_review",
        )
    elif event in ("approved", "returned"):
        verdict = "approved" if event == "approved" else "returned for changes"
        lines = [f"{task.id} was {verdict}: {task.title}", f"Due: {due}"]
        if event == "returned" and task.review_notes:
            lines += ["", f"Review notes: {task.review_notes}"]
        lines += ["", f"When it is done, reply: done {task.id} (attach the proof).", link]
        _send(
            store,
            gmail,
            email_of(task.doer),
            _subject(task, verdict),
            lines,
            person=task.doer,
            task_id=task.id,
            kind=event,
        )
    elif event == "blocked":
        lines = [f"{task.doer or 'The doer'} marked {task.id} as blocked: {task.title}",
                 f"Reason: {note or '(none given)'}", f"Due: {due}", "", link]  # fmt: skip
        people = [find_person(task.owner, staff), *managers(staff)]
        for name in dict.fromkeys(p.name for p in people if p is not None):
            _send(store, gmail, email_of(name), _subject(task, "blocked"), lines,
                  person=name, task_id=task.id, kind=event)  # fmt: skip
    elif event == "extension_requested":
        lines = [
            f"An extension is asked for {task.id}: {task.title}",
            f"Due now: {due}. Asked for: {note.split('|')[0]}.",
            "",
            "Reply to this email with the first line:",
            f"  approve extension {task.id}",
            f"  reject extension {task.id}",
            "",
            link,
        ]
        for manager in managers(staff):
            _send(store, gmail, email_of(manager.name), _subject(task, "extension asked"),
                  lines, person=manager.name, task_id=task.id, kind=event)  # fmt: skip
    elif event in ("extension_approved", "extension_rejected"):
        verdict = "approved" if event == "extension_approved" else "rejected"
        lines = [f"The extension for {task.id} was {verdict}: {task.title}", f"Due: {due}", link]
        _send(store, gmail, email_of(task.doer), _subject(task, f"extension {verdict}"), lines,
              person=task.doer, task_id=task.id, kind=event)  # fmt: skip
    elif event == "assigned":
        from app.jobs import send_assignment

        try:
            send_assignment(store, gmail, task, sheet_url)
        except GmailAuthError:
            raise
        except Exception as error:
            logger.error(
                "assignment_send_failed",
                extra={"task_id": task.id, "error_type": type(error).__name__},
            )


# --- Lists ------------------------------------------------------------------------------------


def _line(task: Task) -> str:
    due = f"due {task.due_date:%d %b}" if task.due_date else "no due date"
    return f"• {task.id} – {task.title} ({due}, {task.status})"


def _mine(tasks: list[Task], person: StaffMember) -> list[Task]:
    name = person.name.casefold()
    return [
        t
        for t in tasks
        if t.status in OPEN_STATUSES and name in (t.doer.casefold(), t.owner.casefold())
    ]


def _sorted(tasks: list[Task]) -> list[Task]:
    return sorted(tasks, key=lambda t: (t.due_date is None, t.due_date or date.max, t.id))


def recurring_reply(store: SheetStore, today: date) -> str:
    """The `recurring` command (v1.4 decision 13): every row with its next deadline, and the
    rows that cannot be used."""
    from app.jobs import recurring_problems

    rules, _ = store.recurring()
    lines = []
    for rule in rules:
        repeat = recurring.repeat_of(rule.repeat)
        if not rule.is_active:
            lines.append(f"• {rule.name}: paused (Active = No)")
            continue
        if repeat is None or rule.first_due is None:
            continue  # listed below as a problem
        nxt = recurring.next_occurrence(rule.first_due, repeat, today)
        if nxt is None:
            continue
        title = recurring.title_for(rule.title, repeat, nxt.nominal, rule.title_period)
        lines.append(f"• {rule.name} ({repeat}): next {title}, due {nxt.due:%a %d %b %Y}, "
                     f"Doer {rule.doer.strip()}")  # fmt: skip
    problems = recurring_problems(store, today)
    if not lines and not problems:
        return "The Recurring tab has no rows."
    parts = ["Recurring tasks:", *lines] if lines else []
    if problems:
        parts += ["", "Not used until fixed:", *(f"• {p}" for p in problems)]
    return "\n".join(parts).strip()


def _list_reply(title: str, tasks: list[Task]) -> str:
    if not tasks:
        return f"{title}: nothing."
    return "\n".join([f"{title} ({len(tasks)}):", *(_line(t) for t in _sorted(tasks))])


# --- Commands ---------------------------------------------------------------------------------


CHANGES = frozenset({"progress", "review", "done", "approve", "return", "assign", "blocked",
                     "extension", "extension_ok", "extension_no", "snooze", "undo"})  # fmt: skip


def run_command(command: Command, ctx: CommandContext) -> CommandResult:
    """Apply one command for `ctx.actor`; never raises for a user mistake. A sheet without a
    column the command needs (e.g. `Review file`) gets a reply naming it, nothing else.
    A message already applied (same message id in ProgressLog) changes nothing again."""
    if command.name in CHANGES and progress.already_processed(ctx.store, ctx.message_id):
        logger.info("command_duplicate", extra={"kind": command.name})
        return CommandResult("Already done.", command.task_id)
    try:
        return _run(command, ctx)
    except SchemaError as error:
        if not all("has no column" in problem for problem in error.problems):
            raise
        logger.warning("command_needs_column", extra={"kind": command.name})
        return CommandResult(
            "The sheet is missing a column this needs ("
            + "; ".join(error.problems)
            + "). Ask the manager to add it. Nothing changed."
        )


def _run(command: Command, ctx: CommandContext) -> CommandResult:
    store = ctx.store
    actor = ctx.actor
    now = ctx.now or clock.now()
    today = now.date()
    tasks = [t for t in store.tasks() if t.id]
    staff = store.staff()
    is_manager = actor.role == "Manager"

    if command.name in ("mine", "today", "week"):
        mine = _mine(tasks, actor)
        if command.name == "today":
            mine = [t for t in mine if t.due_date and t.due_date <= today]
            return CommandResult(_list_reply("Due today or overdue", mine))
        if command.name == "week":
            mine = [t for t in mine if t.due_date and t.due_date <= today + timedelta(days=7)]
            return CommandResult(_list_reply("Due within 7 days", mine))
        return CommandResult(_list_reply(f"{actor.name}, your open tasks", mine))

    if command.name in ("all", "recurring", "pause", "resume"):
        if not is_manager:
            return CommandResult("Only the manager can use that command. Nothing changed.")
        if command.name == "all":
            open_tasks = [t for t in tasks if t.status in OPEN_STATUSES]
            by_doer: dict[str, list[Task]] = {}
            for task in open_tasks:
                by_doer.setdefault(task.doer or "(no doer)", []).append(task)
            if not by_doer:
                return CommandResult("No open tasks.")
            parts = [_list_reply(name, items) for name, items in sorted(by_doer.items())]
            return CommandResult("\n\n".join(parts))
        if command.name == "recurring":
            return CommandResult(recurring_reply(store, today))
        person = match_staff_name(command.value, staff)
        if person is None:
            return CommandResult(f'I can\'t find "{command.value}" in Staff. Nothing changed.')
        paused = command.name == "pause"
        try:
            store.update_staff(person.name, {"Reminders paused": "Yes" if paused else ""})
        except Exception:
            return CommandResult('Staff has no "Reminders paused" column yet. Nothing changed.')
        state = "paused" if paused else "resumed"
        return CommandResult(f"Reminders for {person.name} are {state}.", changed=True)

    if command.name == "undo":
        return _undo(command.task_id or ctx.context_task_id, ctx)

    task_id = command.task_id or ctx.context_task_id
    if task_id is None:
        example = {"assign": f"assign G-0012 to {command.value or 'Cia'}"}.get(
            command.name, f"{command.name} G-0012"
        )
        return CommandResult(f"Which task? Send the ID, e.g. {example}. Nothing changed.")
    task = next((t for t in tasks if t.id == task_id), None)
    if task is None:
        return CommandResult(f"I can't find {task_id}. Nothing changed.")

    if command.name == "files":
        reviewer = default_reviewer(task.reviewer, task.doer, staff)
        allowed = _is_party(actor, task) or (reviewer and reviewer.name == actor.name)
        if not allowed:
            return CommandResult(f"You are not on {task.id}. Nothing sent.", task.id)
        files = ctx.gmail.task_files(task.id, task.source_id)
        if not files:
            return CommandResult(f"{task.id} has no files in the bot mailbox.", task.id)
        names = ", ".join(f.filename for f in files)
        return CommandResult(f"Files for {task.id}: {names}", task.id, attachments=files)

    if task.status in NOT_ACTIVE and command.name != "files":
        why = (
            "is waiting for the manager's approval"
            if task.status == "Proposed"
            else (f"is {task.status}")
        )
        return CommandResult(f"{task.id} {why}. Nothing changed.", task.id)

    if command.name == "approve" or command.name == "return":
        reviewer = default_reviewer(task.reviewer, task.doer, staff)
        if not is_manager and (reviewer is None or reviewer.name != actor.name):
            who = reviewer.name if reviewer else "the manager"
            return CommandResult(
                f"Only {who} (the reviewer) can do that. Nothing changed.", task.id
            )
        if task.status != "In Review":
            return CommandResult(
                f"{task.id} is {task.status}, not In Review. Nothing changed.", task.id
            )
        if command.name == "approve":
            _apply(ctx, task, "Approved", {"Status": "Approved"}, "approved")
            return CommandResult(f"{task.id} approved. {task.doer} is told.", task.id, changed=True)
        if not command.value.strip():
            return CommandResult(
                f"Add what to change: return {task.id} <notes>. Nothing changed.", task.id
            )
        _apply(
            ctx,
            task,
            "Returned",
            {"Status": "Returned", "Review notes": command.value.strip()},
            "returned",
        )
        return CommandResult(
            f"{task.id} returned to {task.doer} with your notes.", task.id, changed=True
        )

    if command.name == "assign":
        return _assign(command, task, ctx, staff)

    if command.name in ("extension_ok", "extension_no"):
        return _decide_extension(command, task, ctx, today)

    if not _is_party(actor, task):
        return CommandResult(
            f"You are not the Owner or Doer of {task.id}. Nothing changed.", task.id
        )

    if command.name == "blocked":
        reason = command.value.strip()
        if not reason:
            return CommandResult(f"What is blocking it? blocked {task.id} <reason>.", task.id)
        stamp = f"Blocked {now:%d %b %Y} by {actor.name}: {reason}"
        notes = f"{task.notes}\n{stamp}" if task.notes else stamp
        _apply(ctx, task, "Blocked", {"Status": "On Hold", "Notes": notes}, "blocked", reason)
        return CommandResult(
            f"{task.id} is On Hold: {reason}. The owner and the manager are told.",
            task.id,
            changed=True,
        )

    if command.name == "extension":
        wanted = requested_date(command.value, today)
        if wanted is None:
            return CommandResult(
                f"Which new date? e.g. extend {task.id} 20 Nov. Nothing changed.", task.id
            )
        floor = max(task.due_date or today, today)
        if wanted <= floor:
            return CommandResult(
                f"{wanted:%d %b %Y} is not later than the due date. Nothing changed.", task.id
            )
        progress.log_note(ctx.store, task.id, actor.name, ctx.channel, ctx.message_id,
                          progress.EXTENSION_FIELD, _day(task.due_date), wanted.isoformat(),
                          "Extension requested", now)  # fmt: skip
        asked = f"{wanted:%a %d %b %Y}|{wanted.isoformat()}|{actor.name}"
        notify("extension_requested", task, store, ctx.gmail, ctx.sheet_url, note=asked, wa=ctx.wa)
        return CommandResult(
            f"Asked the manager to move {task.id} to {wanted:%a %d %b %Y}. "
            "The due date changes only when the manager agrees.",
            task.id,
            changed=True,
        )

    if command.name == "snooze":
        return _snooze(command, task, ctx, today)

    if command.name == "progress":
        fraction = progress_fraction(command.value)
        if fraction is None:
            return CommandResult(
                "Progress is a number from 0 to 100, e.g. progress G-0012 50.", task.id
            )
        changes: dict[str, Any] = {"Progress": fraction}
        if task.status == "Not Started":
            changes["Status"] = "In Progress"
        _apply(ctx, task, "Progress", changes)
        return CommandResult(f"{task.id} progress {round(fraction * 100)}%.", task.id, changed=True)

    if command.name == "review":
        if task.status not in REVIEWABLE:
            return CommandResult(f"{task.id} is {task.status}; it can't go to review now.", task.id)
        reviewer = default_reviewer(task.reviewer, task.doer, staff)
        if reviewer is None:
            return CommandResult(
                "There is no reviewer (no Manager in Staff). Nothing changed.", task.id
            )
        changes = {"Status": "In Review"}
        if ctx.files:
            ctx.gmail.store_files(task.id, ctx.files)
            changes["Review file"] = files_label(ctx.files)
        _apply(ctx, task, "Sent for review", changes, "in_review")
        return CommandResult(
            f"{task.id} sent to {reviewer.name} for review.", task.id, changed=True
        )

    if command.name == "done":
        if task.review_needed and task.status != "Approved":
            return CommandResult(
                f"{task.id} needs review first: send review {task.id} with the file.", task.id
            )
        if not ctx.files and not task.link.strip():
            return CommandResult(
                f"Send the proof file with done {task.id}. Nothing changed.", task.id
            )
        changes = {"Status": "Done", "Submitted date": today}
        if ctx.files:
            ctx.gmail.store_files(task.id, ctx.files)
            changes["Link"] = files_label(ctx.files)
        _apply(ctx, task, "Done", changes)
        return CommandResult(f"{task.id} is Done. Thank you.", task.id, changed=True)

    return CommandResult("I didn't understand that command. Send help.")


def _day(value: date | None) -> str:
    return value.isoformat() if value else ""


def requested_date(text: str, today: date) -> date | None:
    """A date the person typed: "20 Nov", "20/11/2026", "2026-11-20", "this friday", "in 3 days"."""
    from app.extract.prompt import deadline
    from app.extract.rules import find_dates

    text = text.strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        pass
    hits = find_dates(text, today)
    if hits:
        return hits[0].value
    found, _ = deadline(f"deadline {text}", today)
    return found


def _decide_extension(
    command: Command, task: Task, ctx: CommandContext, today: date
) -> CommandResult:
    """Manager only: the due date changes only on approval (DESIGN section 22)."""
    if ctx.actor.role != "Manager":
        return CommandResult("Only the manager can decide an extension. Nothing changed.", task.id)
    asked = progress.last_extension(ctx.store, task.id)
    wanted = requested_date(command.value, today) if command.value else None
    if wanted is None and asked is not None:
        wanted = progress.typed("Due date", asked.new_value)
    if not isinstance(wanted, date):
        return CommandResult(f"No extension was asked for {task.id}. Nothing changed.", task.id)
    if command.name == "extension_ok":
        _apply(ctx, task, "Extension approved", {"Due date": wanted}, "extension_approved",
               f"{_day(task.due_date)} -> {wanted.isoformat()}")  # fmt: skip
        return CommandResult(
            f"{task.id} is now due {wanted:%a %d %b %Y}. {task.doer} is told.",
            task.id,
            changed=True,
        )
    progress.log_note(ctx.store, task.id, ctx.actor.name, ctx.channel, ctx.message_id,
                      progress.EXTENSION_FIELD, wanted.isoformat(), _day(task.due_date),
                      "Extension rejected", ctx.now)  # fmt: skip
    notify("extension_rejected", task, ctx.store, ctx.gmail, ctx.sheet_url, wa=ctx.wa)
    return CommandResult(
        f"Extension for {task.id} rejected; still due {task.due_date:%a %d %b %Y}."
        if task.due_date
        else f"Extension for {task.id} rejected.",
        task.id,
        changed=True,
    )


def _snooze(command: Command, task: Task, ctx: CommandContext, today: date) -> CommandResult:
    """No reminders about the task for this person until a date: at most 2 working days ahead,
    never past the due date (DESIGN section 22)."""
    from app.reminders import next_workday

    holidays = ctx.store.config().holidays
    latest = next_workday(next_workday(today, holidays), holidays)
    until = requested_date(command.value, today) if command.value else None
    until = until or next_workday(today, holidays)
    note = ""
    if until > latest:
        until, note = latest, " (at most 2 working days)"
    if task.due_date is not None and until > task.due_date:
        until, note = task.due_date, " (not past the due date)"
    if until <= today:
        why = "overdue" if task.due_date and task.due_date < today else "due today"
        return CommandResult(f"{task.id} is {why}; it can't be snoozed.", task.id)
    progress.set_snooze(ctx.store, task.id, ctx.actor.name, until, ctx.now)
    progress.log_note(ctx.store, task.id, ctx.actor.name, ctx.channel, ctx.message_id,
                      progress.SNOOZE_FIELD, "", until.isoformat(), "Snoozed", ctx.now)  # fmt: skip
    return CommandResult(
        f"No reminders about {task.id} until {until:%a %d %b}{note}.", task.id, changed=True
    )


def _undo(task_id: str | None, ctx: CommandContext) -> CommandResult:
    """Reverse the person's last change (on `task_id`, or their last on any task) within 24
    hours. Notices already sent stay sent; approve and return are not undone (decision 9)."""
    store = ctx.store
    group = progress.last_change(store, ctx.actor.name, task_id, ctx.now)
    if not group:
        return CommandResult("Nothing of yours to undo in the last 24 hours.", task_id)
    target = group[0].task_id
    if not progress.undoable(group):
        return CommandResult(f"Your last change on {target} can't be undone.", target)
    task = next((t for t in store.tasks() if t.id == target), None)
    if task is None:
        return CommandResult(f"I can't find {target}. Nothing changed.", target)
    changes: dict[str, Any] = {}
    for row in group:
        if row.field == progress.SNOOZE_FIELD:
            progress.clear_snooze(store, target, ctx.actor.name)
        elif row.field == progress.EXTENSION_FIELD:
            continue  # a request: the manager simply does not approve it
        else:
            changes[row.field] = progress.typed(row.field, row.old_value)
    if changes:
        _apply(ctx, task, "Undo", changes)
    else:
        progress.log_note(store, target, ctx.actor.name, ctx.channel, ctx.message_id,
                          group[0].field, group[0].new_value, "", "Undo", ctx.now)  # fmt: skip
    fields = ", ".join(row.field for row in group)
    return CommandResult(f"Undone on {target}: {fields}.", target, changed=True)


def _assign(
    command: Command, task: Task, ctx: CommandContext, staff: list[StaffMember]
) -> CommandResult:
    actor = ctx.actor
    target = actor if is_me(command.value) else match_staff_name(command.value, staff)
    if target is None:
        return CommandResult(f'I can\'t find "{command.value}" in Staff. Nothing changed.', task.id)
    problem = delegation_problem(actor, task.owner, task.doer, target, staff)
    if problem:
        return CommandResult(f"You can't reassign {task.id}: {problem}. Nothing changed.", task.id)
    if target.name.casefold() == task.doer.casefold():
        return CommandResult(f"{target.name} is already the Doer of {task.id}.", task.id)
    changes: dict[str, Any] = {"Doer": target.name}
    if actor.role == "Manager" and target.name != actor.name:
        owner, doer = owner_and_doer(target, staff)
        changes = {"Owner": owner, "Doer": doer}
    _apply(ctx, task, "Delegated", changes, "assigned")
    return CommandResult(
        f"{task.id} is now with {changes['Doer']}. They are told.", task.id, changed=True
    )


def reviewer_for(task: Task, staff: list[StaffMember]) -> StaffMember | None:
    return default_reviewer(task.reviewer, task.doer, staff)


__all__ = ["CommandContext", "CommandResult", "managers", "reviewer_for", "run_command"]
