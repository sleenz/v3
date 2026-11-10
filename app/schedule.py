"""`/jobs/reminders`: the work-day reminder schedule (DESIGN section 22).

Cloud Scheduler calls it every 15 minutes on weekdays; this module decides what is due:
- inside working hours, queued event notices go out first (`app/events.py`);
- each slot (morning, manager, due today, last call) is processed once per working day, at the
  first run at or after its Config time and before Workday end;
- every person gets at most one message per slot, combining all their rules;
- at most Config `Max reminders per day` reminder messages per person (the manager summary and
  event notices do not count). Triggers over the cap, or whose send failed, are kept as
  deferred rows and merged into that person's next scheduled message;
- rule + task + person is never sent twice on one date (`_reminders_sent`).

Logs carry counts only, never names, numbers or task text.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta

from pydantic import BaseModel

from app import clock, outbox, progress
from app.events import flush_events
from app.gmail import GmailAuthError, GmailClient
from app.models import (
    REMINDERS_SENT,
    AppConfig,
    Channel,
    OutgoingEmail,
    ReminderSent,
    StaffMember,
    Task,
)
from app.notify import digest_email, task_line
from app.reminders import (
    DUE_TODAY,
    LAST_CALL,
    MANAGER,
    MORNING,
    OVERDUE,
    PERSONAL_SLOTS,
    TEMPLATE_DUE,
    TEMPLATE_LANGUAGE,
    TEMPLATE_MANAGER,
    TEMPLATE_MORNING,
    VIEW_PAYLOAD,
    Trigger,
    channel_for,
    days_before_rule,
    due_slots,
    first_name,
    grouped,
    is_workday,
    manager_sections,
    rule_label,
    short_ids,
    slot_triggers,
    task_ids,
    within_workday,
)
from app.route import managers
from app.sheets import SheetStore

logger = logging.getLogger("grc.schedule")

SLOT_MARK = "slot:"  # a `_reminders_sent` row with Person "*" marks a slot processed for the day
EVERYONE = "*"
KEEP_DAYS = 21


class RemindersRun(BaseModel):  # counts only
    working_day: bool = False
    events: int = 0
    slots: list[str] = []
    messages: int = 0
    triggers: int = 0
    deferred: int = 0
    failed: int = 0
    no_channel: int = 0
    manager: int = 0


class _Sender:
    """One person's message on their channel; falls back from WhatsApp to email."""

    def __init__(
        self, store: SheetStore, gmail: GmailClient, wa: object, sheet_url: str, now: datetime
    ):
        self.store = store
        self.gmail = gmail
        self.wa = wa
        self.sheet_url = sheet_url
        self.now = now
        self.sender_name = store.config().sender_name

    def send(
        self,
        person: StaffMember,
        email: OutgoingEmail | None,
        template: tuple[str, list[str], list[str]] | None,
        ids: list[str],
        kind: str,
    ) -> Channel | None:
        """Returns the channel used, or None when nothing could be sent."""
        channel = channel_for(person)
        if channel == "WhatsApp" and self.wa is not None and template is not None:
            name, params, payloads = template
            try:
                message_id = self.wa.send_template(  # type: ignore[attr-defined]
                    person.wa_id, name, TEMPLATE_LANGUAGE, params, payloads
                )
                outbox.record(self.store, "WhatsApp", message_id, person.name, ids, kind)
                return "WhatsApp"
            except Exception as error:
                logger.warning("wa_reminder_fell_back", extra={"error_type": type(error).__name__})
        if email is None or not person.email.strip():
            return None
        try:
            sent = self.gmail.send(email, self.sender_name)
        except GmailAuthError:
            raise
        except Exception as error:
            logger.error("reminder_email_failed", extra={"error_type": type(error).__name__})
            return None
        if sent is not None:
            outbox.record(
                self.store, "Email", sent.id, person.name, ids, kind, thread_id=sent.thread_id
            )
        return "Email"


def _current_rule(trigger: Trigger, task: Task | None, today: date) -> Trigger:
    """A deferred trigger: say how things stand now ("due in 3 days" yesterday is "due in 2
    days"; yesterday's "due today" is overdue)."""
    dated = trigger.rule.startswith("due_in_") or trigger.rule in (DUE_TODAY, LAST_CALL)
    if task is None or task.due_date is None or not dated:
        return trigger
    left = (task.due_date - today).days
    rule = OVERDUE if left < 0 else DUE_TODAY if left == 0 else days_before_rule(left)
    return Trigger(rule, trigger.task_id, trigger.person)


def _personal_email(
    person: StaffMember,
    triggers: list[Trigger],
    tasks: dict[str, Task],
    config: AppConfig,
    sheet_url: str,
    today: date,
) -> OutgoingEmail:
    rules = {trigger.rule for trigger in triggers}
    label = f"{today:%a %d %b %Y}"
    if LAST_CALL in rules:
        subject = f"[GRC] Last call: due today – {label}"
    elif DUE_TODAY in rules:
        subject = f"[GRC] Due today, not done yet – {label}"
    else:
        subject = f"[GRC] Your tasks – {label}"
    sections = [
        (rule_label(rule, config), [task_line(tasks[i]) for i in ids if i in tasks])
        for rule, ids in grouped(triggers)
    ]
    sections.append(
        (
            "To update a task, reply to this email with the first line",
            [
                "progress G-0012 50",
                "review G-0012 (attach the file for review)",
                "done G-0012 (attach the proof)",
            ],
        )
    )
    return digest_email(
        [person.email.strip()],
        subject,
        f"Hi {first_name(person)}, these GRC tasks need your attention.",
        sections,
        sheet_url,
    )


def _personal_template(
    person: StaffMember, triggers: list[Trigger], slot: str, today: date
) -> tuple[str, list[str], list[str]]:
    ids = task_ids(triggers)
    payload = f"{VIEW_PAYLOAD}:{slot}:{today.isoformat()}"
    rules = {trigger.rule for trigger in triggers}
    if rules & {DUE_TODAY, LAST_CALL}:
        label = "Pengingat terakhir" if LAST_CALL in rules else "Pengingat"
        return TEMPLATE_DUE, [label, str(len(ids)), short_ids(ids)], [payload]
    return TEMPLATE_MORNING, [first_name(person), str(len(ids)), short_ids(ids)], [payload]


def run_reminders(
    store: SheetStore,
    gmail: GmailClient,
    wa: object | None,
    sheet_url: str,
    now: datetime | None = None,
) -> RemindersRun:
    now = now or clock.now()
    today = now.date()
    config = store.config()
    run = RemindersRun(working_day=is_workday(today, config.holidays))
    if not run.working_day:
        logger.info("reminders_run", extra={"n_slots": 0, "working_day": False})
        return run
    if within_workday(now, config):
        run.events = flush_events(store, gmail, sheet_url, wa)
    slots = due_slots(now, config)
    stored = store.service_rows(REMINDERS_SENT, ReminderSent)
    rows = [r for r in stored if r.day >= today - timedelta(days=KEEP_DAYS) or r.sent_at is None]
    done_slots = {
        r.rule.removeprefix(SLOT_MARK)
        for r in rows
        if r.day == today and r.person == EVERYONE and r.rule.startswith(SLOT_MARK)
    }
    todo = [slot for slot in slots if slot not in done_slots]
    run.slots = todo
    if not todo:
        if len(rows) != len(stored):
            store.rewrite_service(REMINDERS_SENT, rows)  # old rows dropped
        logger.info("reminders_run", extra={"n_slots": 0, "n_events": run.events})
        return run

    tasks_list = [task for task in store.tasks() if task.id]
    tasks = {task.id: task for task in tasks_list}
    staff = store.staff()
    sender = _Sender(store, gmail, wa, sheet_url, now)
    sent_today = {
        (r.rule, r.task_id, r.person.casefold())
        for r in rows
        if r.day == today and r.sent_at is not None and r.person != EVERYONE
    }
    messages_today: dict[str, set[datetime]] = defaultdict(set)
    for r in rows:
        if (
            r.day == today
            and r.sent_at is not None
            and r.rule not in (MANAGER,)
            and (r.person != EVERYONE)
        ):
            messages_today[r.person.casefold()].add(r.sent_at)

    personal = [slot for slot in todo if slot in PERSONAL_SLOTS]
    if personal:
        _personal_slots(
            personal,
            rows,
            tasks_list,
            tasks,
            staff,
            config,
            today,
            now,
            sender,
            sent_today,
            messages_today,
            run,
            progress.snoozed(store, today),
        )
    if MANAGER in todo:
        _manager_slot(store, rows, tasks_list, staff, config, today, now, sender, sent_today, run)
    for slot in todo:
        rows.append(
            ReminderSent(
                day=today, rule=SLOT_MARK + slot, person=EVERYONE, channel="Email", sent_at=now
            )
        )
    store.rewrite_service(REMINDERS_SENT, rows)
    logger.info(
        "reminders_run",
        extra={
            "n_slots": len(todo),
            "n_messages": run.messages,
            "n_triggers": run.triggers,
            "n_deferred": run.deferred,
            "n_failed": run.failed,
            "n_no_channel": run.no_channel,
            "n_manager": run.manager,
            "n_events": run.events,
        },
    )
    return run


def _personal_slots(
    personal: list[str],
    rows: list[ReminderSent],
    tasks_list: list[Task],
    tasks: dict[str, Task],
    staff: list[StaffMember],
    config: AppConfig,
    today: date,
    now: datetime,
    sender: _Sender,
    sent_today: set[tuple[str, str, str]],
    messages_today: dict[str, set[datetime]],
    run: RemindersRun,
    snoozed: set[tuple[str, str]] = frozenset(),  # type: ignore[assignment]
) -> None:
    """One message per person for the slot(s) due now, plus their deferred triggers. A task the
    person snoozed (`_snooze`) is left out until the snooze ends."""
    slot = personal[-1]  # several slots at once (a missed run): one message, the latest slot
    fresh: list[Trigger] = []
    for name in personal:
        fresh += slot_triggers(name, tasks_list, staff, today, config)
    deferred = [r for r in rows if r.sent_at is None and r.person != EVERYONE]
    by_person: dict[str, list[Trigger]] = defaultdict(list)
    keep_deferred: list[ReminderSent] = []
    for row in deferred:
        task = tasks.get(row.task_id)
        if task is None or task.status in ("Done", "Proposed"):
            rows.remove(row)  # nothing left to remind about
            continue
        trigger = _current_rule(Trigger(row.rule, row.task_id, row.person), task, today)
        keep_deferred.append(row)
        if (row.task_id, row.person.casefold()) not in snoozed:
            by_person[row.person.casefold()].append(trigger)
    for trigger in fresh:
        if trigger.key not in sent_today and (trigger.task_id, trigger.key[2]) not in snoozed:
            by_person[trigger.person.casefold()].append(trigger)
    for key, triggers in by_person.items():
        person = next((p for p in staff if p.name.casefold() == key), None)
        unique = list({t.key: t for t in triggers}.values())
        if person is None:
            continue
        mine = [r for r in keep_deferred if r.person.casefold() == key]
        if person.paused:
            continue  # Staff `Reminders paused`: nothing now, nothing kept
        if len(messages_today[key]) >= config.max_reminders_per_day:
            _defer(rows, unique, mine, today)
            run.deferred += len(unique)
            continue
        email = _personal_email(person, unique, tasks, config, sender.sheet_url, today)
        template = _personal_template(person, unique, slot, today)
        channel = sender.send(person, email, template, task_ids(unique), slot)
        if channel is None:
            if channel_for(person) is None:
                run.no_channel += 1
            else:
                run.failed += 1
            _defer(rows, unique, mine, today)
            continue
        for row in mine:
            rows.remove(row)
        rows.extend(
            ReminderSent(
                day=today,
                rule=t.rule,
                task_id=t.task_id,
                person=person.name,
                channel=channel,
                sent_at=now,
            )
            for t in unique
        )
        messages_today[key].add(now)
        run.messages += 1
        run.triggers += len(unique)


def _defer(
    rows: list[ReminderSent], triggers: list[Trigger], already: list[ReminderSent], today: date
) -> None:
    """Keep triggers for the person's next scheduled message (no Sent at)."""
    have = {(r.rule, r.task_id) for r in already}
    for trigger in triggers:
        if (trigger.rule, trigger.task_id) not in have:
            rows.append(
                ReminderSent(
                    day=today,
                    rule=trigger.rule,
                    task_id=trigger.task_id,
                    person=trigger.person,
                    channel="Email",
                    sent_at=None,
                )
            )


def _manager_slot(
    store: SheetStore,
    rows: list[ReminderSent],
    tasks_list: list[Task],
    staff: list[StaffMember],
    config: AppConfig,
    today: date,
    now: datetime,
    sender: _Sender,
    sent_today: set[tuple[str, str, str]],
    run: RemindersRun,
) -> None:
    """The team summary to every Manager in Staff (none: Config `Manager email`)."""
    from app.jobs import manager_extras

    sections = manager_sections(tasks_list, today)
    counts = [str(len(found)) for _, found in sections]
    text_sections = [(title, [task_line(t) for t in found]) for title, found in sections]
    text_sections += manager_extras(store, tasks_list, staff, config, today)
    label = f"{today:%a %d %b %Y}"
    payload = f"{VIEW_PAYLOAD}:{MANAGER}:{today.isoformat()}"
    ids = sorted({t.id for _, found in sections for t in found})
    recipients = managers(staff)
    if not recipients and config.manager_email.strip():
        recipients = [
            StaffMember(name="Manager", email=config.manager_email.strip(), role="Manager")
        ]
    for person in recipients:
        if (MANAGER, "", person.name.casefold()) in sent_today or person.paused:
            continue
        email = digest_email(
            [person.email.strip()] if person.email.strip() else [],
            f"[GRC] Team summary – {label}",
            "GRC team summary for today.",
            text_sections,
            sender.sheet_url,
        )
        channel = sender.send(
            person,
            email if person.email.strip() else None,
            (TEMPLATE_MANAGER, counts, [payload]),
            ids,
            MANAGER,
        )
        if channel is None:
            run.failed += 1
            continue
        rows.append(
            ReminderSent(
                day=today,
                rule=MANAGER,
                task_id="",
                person=person.name,
                channel=channel,
                sent_at=now,
            )
        )
        run.manager += 1


__all__ = ["MORNING", "RemindersRun", "run_reminders"]
