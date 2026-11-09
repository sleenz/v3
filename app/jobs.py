"""Timed jobs: change detection, recurring reports, digests (DESIGN sections 7 and 8)."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from pydantic import BaseModel

from app import clock, recurring
from app.gmail import GmailAuthError, GmailClient
from app.models import (
    OPEN_STATUSES,
    AppConfig,
    AuditEntry,
    CatalogEntry,
    OutgoingEmail,
    RecurringRule,
    StaffMember,
    StateRow,
    Task,
)
from app.notify import (
    MissingEmail,
    assignment_email,
    manager_digest,
    personal_digest,
    task_line,
    with_files,
)
from app.pipeline import SERVICE_USER, Services
from app.route import find_person, load_ratio
from app.sheets import SchemaError, SheetStore


def _log_counts(run: BaseModel) -> dict[str, object]:
    """Run counts as log fields; prefixed because names like `created` are reserved by logging."""
    return {f"n_{key}": value for key, value in run.model_dump().items()}


logger = logging.getLogger("grc.jobs")

NO_EMAIL_FLAG = "Assignment email not sent: doer has no email in Staff"


# --- /jobs/changes -----------------------------------------------------------------------------


class ChangesRun(BaseModel):  # counts only (no names or emails in logs)
    tasks: int = 0
    baseline: bool = False
    emailed: int = 0
    not_sent: int = 0


def _state_row(task: Task, notified_doer: str, notified_at: datetime | None) -> StateRow:
    return StateRow(
        id=task.id,
        status=task.status,
        owner=task.owner,
        doer=task.doer,
        notified_doer=notified_doer,
        notified_at=notified_at,
    )


def run_changes(store: SheetStore, gmail: GmailClient, sheet_url: str) -> ChangesRun:
    """Email the doer of every open task whose current doer has not been emailed yet.

    That covers both triggers in section 7: approval (Proposed → open, nobody notified) and
    handoff (Doer changed on an open task). A send that fails stays pending and is retried on the
    next run. The first run (empty `_state`) only records the current state (v1.1).
    """
    tasks = [task for task in store.tasks() if task.id]
    state = store.state()
    run = ChangesRun(tasks=len(tasks), baseline=not state)
    if not state:
        rows = [
            _state_row(task, task.doer if task.status in OPEN_STATUSES else "", None)
            for task in tasks
        ]
        store.write_state(rows)
        logger.info("changes_run", extra=_log_counts(run))
        return run

    staff = store.staff()
    sender_name = store.config().sender_name
    rows: dict[str, StateRow] = {}
    for task in tasks:
        previous = state.get(task.id)
        notified_doer = previous.notified_doer if previous else ""
        notified_at = previous.notified_at if previous else None
        rows[task.id] = _state_row(task, notified_doer, notified_at)
        if previous is not None and previous.status != task.status:
            _status_event(store, gmail, task, sheet_url)  # v1.4: a review step edited in Sheet
        doer = task.doer.strip()
        if task.status not in OPEN_STATUSES or not doer or doer == notified_doer:
            continue
        person = find_person(doer, staff)
        if person is not None and person.paused:
            continue  # v1.4 decision 12: stays pending until `resume`
        try:
            email = _assignment_with_files(task, staff, sheet_url, gmail)
        except MissingEmail:
            run.not_sent += 1
            if NO_EMAIL_FLAG not in task.flags:
                store.update_task(task.id, {"Flag": "; ".join([*task.flags, NO_EMAIL_FLAG])})
            logger.warning("assignment_not_sent", extra={"task_id": task.id})
            continue
        try:
            gmail.send(email, sender_name)
        except GmailAuthError:
            raise
        except Exception as error:
            run.not_sent += 1  # stays pending: retried on the next run
            logger.error(
                "assignment_send_failed",
                extra={"task_id": task.id, "error_type": type(error).__name__},
            )
            continue
        sent_at = clock.now()
        rows[task.id] = _state_row(task, doer, sent_at)
        run.emailed += 1
        # Record each send at once, so a crash later in the run cannot cause a second email.
        store.write_state(list(rows.values()) + [s for i, s in state.items() if i not in rows])
        _audit(store, task.id, "Assignment emailed", "Doer", doer)
    store.write_state(list(rows.values()))
    logger.info("changes_run", extra=_log_counts(run))
    return run


def _status_event(store: SheetStore, gmail: GmailClient, task: Task, sheet_url: str) -> None:
    """In Review / Approved / Returned set in the Sheet: tell the reviewer or the doer."""
    from app.lifecycle import EVENT_STATUSES, notify

    event = EVENT_STATUSES.get(task.status)
    if event is None:
        return
    try:
        notify(event, task, store, gmail, sheet_url)
    except GmailAuthError:
        raise
    except Exception as error:  # a notice must never stop the change job
        logger.error("event_failed", extra={"task_id": task.id,
                                            "error_type": type(error).__name__})  # fmt: skip


def _assignment_with_files(
    task: Task, staff: list[StaffMember], sheet_url: str, gmail: GmailClient
) -> OutgoingEmail:
    """The assignment email with the task's files from the bot mailbox (v1.4 decision F4)."""
    email = assignment_email(task, staff, sheet_url)
    try:
        files = gmail.task_files(task.id, task.source_id)
    except GmailAuthError:
        raise
    except Exception as error:
        logger.warning(
            "task_files_failed", extra={"task_id": task.id, "error_type": type(error).__name__}
        )
        files = []
    return with_files(email, files)


def send_assignment(store: SheetStore, gmail: GmailClient, task: Task, sheet_url: str) -> bool:
    """The assignment email now (after `assign`), recorded in `_state` so the change job does
    not send it again. False when it could not go (no email, paused, send failed)."""
    staff = store.staff()
    person = find_person(task.doer, staff)
    if person is None or person.paused or not person.email.strip():
        return False
    email = _assignment_with_files(task, staff, sheet_url, gmail)
    gmail.send(email, store.config().sender_name)
    state = store.state()
    state[task.id] = _state_row(task, task.doer.strip(), clock.now())
    store.write_state(list(state.values()))
    _audit(store, task.id, "Assignment emailed", "Doer", task.doer.strip())
    return True


def _audit(store: SheetStore, task_id: str, action: str, field: str, value: str) -> None:
    try:
        store.append_audit(
            [
                AuditEntry(
                    timestamp=clock.now(),
                    user=SERVICE_USER,
                    task_id=task_id,
                    action=action,
                    field=field,
                    new_value=value,
                )
            ]
        )
    except Exception as error:
        logger.error(
            "audit_write_failed", extra={"task_id": task_id, "error_type": type(error).__name__}
        )


# --- /jobs/recurring (v1.4 part 3: the Recurring tab, DESIGN section 20) ----------------------


class RecurringRun(BaseModel):
    rules: int = 0
    created: int = 0
    existing: int = 0
    skipped: int = 0  # rows that could not be used (named in /health and the manager digest)
    failed: int = 0


def recurring_problems(store: SheetStore, today: date) -> list[str]:
    """Why Recurring rows cannot be used today: bad cells, unknown repeat, people not in Staff.
    For /health warnings, the manager digest and the `recurring` command."""
    rules, problems = store.recurring()
    staff = store.staff()
    for rule in rules:
        if not rule.is_active:
            continue
        reason = recurring.problem(rule)
        if reason is None:
            nxt = recurring.next_occurrence(rule.first_due, recurring.repeat_of(rule.repeat), today)
            who = recurring.assignee(rule, staff, nxt.due if nxt else today, today)
            reason = who if isinstance(who, str) else None
        if reason:
            problems.append(f"Recurring {rule.name.strip()}: {reason}")
    return problems


def recurring_task(
    rule: RecurringRule,
    occurrence: recurring.Occurrence,
    who: recurring.Assignee,
    catalog: list[CatalogEntry],
    staff: list[StaffMember],
    today: date,
) -> Task:
    """The Master row for one deadline: already approved (`Not Started`, decision 2), so the
    change job emails the Doer the assignment on its next run."""
    repeat = recurring.repeat_of(rule.repeat) or rule.repeat
    wanted = rule.catalog_id.strip().casefold()
    entry = next((e for e in catalog if wanted and e.catalog_id.strip().casefold() == wanted), None)
    doer = find_person(who.doer, staff)
    division = entry.division if entry else next(iter(doer.division_list if doer else []), "")
    key = recurring.source_id(rule.name, occurrence)
    flags = list(who.flags)
    if rule.catalog_id.strip() and entry is None:
        flags.append(f"Catalog ID {rule.catalog_id.strip()} not in Catalog")
    moved = occurrence.due != occurrence.nominal
    when = f"due {occurrence.due:%a %d %b %Y}"
    if moved:
        when += f" (moved from {occurrence.nominal:%a %d %b %Y}, a weekend)"
    return Task(
        title=recurring.title_for(rule.title, repeat, occurrence.nominal, rule.title_period),
        catalog_id=entry.catalog_id if entry else "",
        division=division,
        requester=entry.regulator if entry else "",
        owner=who.owner,
        doer=who.doer,
        start=today,
        due_date=occurrence.due,
        status="Not Started",
        last_updated=clock.now(),
        notes=rule.notes.strip(),
        flag="; ".join(flags),
        source=key,
        routing_note=f"Recurring tab row {rule.name.strip()} ({repeat}): set up by the team",
        original_message=f"Recurring task from the Recurring tab, row {rule.name.strip()} "
        f"({repeat}), {when}.",
        source_id=key,
        needs_review="Yes" if rule.needs_review.strip().lower() in {"yes", "y", "ya"} else "",
        reviewer=rule.reviewer.strip(),
    )


def run_recurring(services: Services, today: date | None = None) -> RecurringRun:
    """Create each Recurring row's task once per deadline, `Create days before` the deadline
    (blank: Config `Recurring lead (days)`). Rows that cannot be used are skipped and named."""
    today = today or clock.today()
    store = services.store
    rules, problems = store.recurring()
    run = RecurringRun(rules=len(rules), skipped=len(problems))
    config = store.config()
    staff = store.staff()
    catalog = store.catalog()
    for rule in rules:
        if not rule.is_active:
            continue
        if recurring.problem(rule) is not None or rule.first_due is None:
            run.skipped += 1
            continue
        repeat = recurring.repeat_of(rule.repeat) or ""
        lead = rule.create_days_before
        lead = config.recurring_lead_days if lead is None else lead
        for occurrence in recurring.to_create(rule.first_due, repeat, lead, today):
            who = recurring.assignee(rule, staff, occurrence.due, today)
            if isinstance(who, str):
                run.skipped += 1
                break
            task = recurring_task(rule, occurrence, who, catalog, staff, today)
            try:
                saved, created = store.append_task(task)
            except SchemaError:
                raise
            except Exception as error:
                run.failed += 1  # retried on the next daily run: the Source ID dedupes it
                logger.error("recurring_failed", extra={"error_type": type(error).__name__})
                continue
            if not created:
                run.existing += 1
                continue
            run.created += 1
            _audit(store, saved.id, "Created", "Source", saved.source_id)
            logger.info("task_created", extra={"task_id": saved.id, "rule": "recurring"})
    logger.info("recurring_run", extra=_log_counts(run))
    return run


# --- /jobs/digest ------------------------------------------------------------------------------


class DigestRun(BaseModel):
    personal_sent: int = 0
    manager_sent: bool = False
    failed: int = 0


def _open(tasks: list[Task]) -> list[Task]:
    return [task for task in tasks if task.status in OPEN_STATUSES]


def personal_buckets(
    person: StaffMember, open_tasks: list[Task], now: datetime, config: AppConfig
) -> tuple[list[Task], list[Task], list[Task]]:
    """(overdue, due within 7 days, In Progress not updated for `Stale after (days)`) for one
    person's open tasks: the personal digest, by email and (v1.3) on WhatsApp."""
    today = now.date()
    stale_before = now - timedelta(days=config.stale_after_days)
    mine = [t for t in open_tasks if t.doer.strip().casefold() == person.name.casefold()]
    overdue = [t for t in mine if t.due_date and t.due_date < today]
    due_soon = [t for t in mine if t.due_date and today <= t.due_date <= today + timedelta(7)]
    stale = [
        t
        for t in mine
        if t.status == "In Progress" and t.last_updated and t.last_updated < stale_before
    ]
    return overdue, due_soon, stale


def run_digest(
    store: SheetStore, gmail: GmailClient, sheet_url: str, now: datetime | None = None
) -> DigestRun:
    now = now or clock.now()
    today = now.date()
    config = store.config()
    tasks = [task for task in store.tasks() if task.id]
    staff = store.staff()
    open_tasks = _open(tasks)  # Proposed is not open, so it never appears in personal digests
    label = f"{today:%a %d %b %Y}"
    run = DigestRun()

    def send(email, kind: str) -> bool:
        try:
            gmail.send(email, config.sender_name)
            return True
        except GmailAuthError:
            raise
        except Exception as error:
            run.failed += 1
            logger.error("digest_failed", extra={"kind": kind, "error_type": type(error).__name__})
            return False

    for person in staff:
        if not person.email.strip():
            continue
        overdue, due_soon, stale = personal_buckets(person, open_tasks, now, config)
        email = personal_digest(person, overdue, due_soon, stale, sheet_url, label)
        if email is not None and send(email, "personal"):
            run.personal_sent += 1

    if not config.manager_email.strip():
        logger.warning("manager_digest_skipped_no_email")
        return run
    leaving_by = today + timedelta(days=config.leaving_warning_days)
    catalog = store.catalog()
    leaving = []
    for person in staff:
        end = person.internship_end
        if person.role != "Intern" or end is None or not today <= end <= leaving_by:
            continue
        owned = [e.catalog_id for e in catalog if find_person(e.default_owner, [person])]
        if owned:
            leaving.append(
                f"{person.name} leaves {end:%d %b %Y}; default owner of {', '.join(owned)}"
            )
    since = today - timedelta(days=1)
    ai_down = sum(
        1 for t in tasks if "AI unavailable" in t.flags and t.start is not None and t.start >= since
    )
    sections = [
        ("Awaiting approval", [task_line(t) for t in tasks if t.status == "Proposed"]),
        ("Overdue", [task_line(t) for t in open_tasks if t.due_date and t.due_date < today]),
        (
            "Over the load threshold",
            [
                f"{p.name}: load {load_ratio(p):g} (threshold {config.overload_threshold:g})"
                for p in staff
                if p.role != "Manager" and load_ratio(p) > config.overload_threshold
            ],
        ),
        ("Interns leaving who still own Catalog reports", leaving),
        ("AI unavailable (last 24 h)", [f"{ai_down} request(s)"] if ai_down else []),
        (
            "Recurring rows skipped (fix them in the Recurring tab)",
            recurring_problems(store, today),
        ),
    ]
    email = manager_digest(config.manager_email.strip(), sections, sheet_url, label)
    if email is not None:
        run.manager_sent = send(email, "manager")
    logger.info("digest_run", extra=_log_counts(run))
    return run
