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

from app import clock, recurring
from app.commands import Command, is_me, progress_fraction
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
    ctx: CommandContext, task: Task, action: str, changes: dict[str, Any], event: str = ""
) -> Task:
    store = ctx.store
    changes = {**changes, "Last updated": ctx.now or clock.now()}
    store.update_task(task.id, changes)
    _audit(store, ctx, task.id, action, changes, task)
    updated = Task.model_validate({**task.model_dump(by_alias=True), **changes})
    if event:
        notify(event, updated, store, ctx.gmail, ctx.sheet_url, ctx.files)
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
) -> bool:
    if not to:
        return False
    text = "\n".join(lines)
    email = OutgoingEmail(to=[to], subject=subject, text=text, html=f"<pre>{escape(text)}</pre>")
    try:
        gmail.send(with_files(email, files or []), store.config().sender_name)
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
) -> None:
    """Email the people an event concerns. Paused people (Staff `Reminders paused`) get nothing."""
    staff = store.staff()
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
        )
    elif event in ("approved", "returned"):
        verdict = "approved" if event == "approved" else "returned for changes"
        lines = [f"{task.id} was {verdict}: {task.title}", f"Due: {due}"]
        if event == "returned" and task.review_notes:
            lines += ["", f"Review notes: {task.review_notes}"]
        lines += ["", f"When it is done, reply: done {task.id} (attach the proof).", link]
        _send(store, gmail, _email_of(task.doer, staff), _subject(task, verdict), lines)
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


def run_command(command: Command, ctx: CommandContext) -> CommandResult:
    """Apply one command for `ctx.actor`; never raises for a user mistake. A sheet without a
    column the command needs (e.g. `Review file`) gets a reply naming it, nothing else."""
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

    if not _is_party(actor, task):
        return CommandResult(
            f"You are not the Owner or Doer of {task.id}. Nothing changed.", task.id
        )

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
