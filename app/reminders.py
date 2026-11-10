"""Reminder rules on a work-day schedule (DESIGN section 22). Pure: no network, no clock.

`app/schedule.py` runs them every 15 minutes and decides what to send; this module only says
which day is a working day, which slots are due, who each rule concerns, which channel a person
gets and what the message says.

Slots (Config, Asia/Hong_Kong): morning (one message per person combining every rule that fired
for them), manager (the team summary), due today (14:00) and last call (16:30).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from app.models import OPEN_STATUSES, AppConfig, Channel, StaffMember, Task
from app.route import default_reviewer, find_person, managers

MORNING = "morning"
MANAGER = "manager"
DUE_TODAY = "due_today"
LAST_CALL = "last_call"
SLOT_ORDER = (MORNING, MANAGER, DUE_TODAY, LAST_CALL)
PERSONAL_SLOTS = (MORNING, DUE_TODAY, LAST_CALL)

OVERDUE = "overdue"
STALE = "stale"
REVIEW_WAITING = "review_waiting"
STALE_STATUSES = frozenset({"Not Started", "In Progress", "Returned"})
TEMPLATE_LANGUAGE = "id"
TEMPLATE_MORNING = "grc_pagi"
TEMPLATE_DUE = "grc_jatuh_tempo"
TEMPLATE_MANAGER = "grc_ringkasan_manager"
VIEW_PAYLOAD = "view"  # the "Lihat tugas" quick-reply payload: "view:<slot>:<yyyy-mm-dd>"


@dataclass(frozen=True)
class Trigger:
    """One rule that fired for one task and one person (a Staff name)."""

    rule: str
    task_id: str
    person: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.rule, self.task_id, self.person.casefold())


# --- Working days and slots ----------------------------------------------------------------------


def is_workday(day: date, holidays: Iterable[date]) -> bool:
    return day.weekday() < 5 and day not in set(holidays)


def next_workday(day: date, holidays: Iterable[date]) -> date:
    """The first working day after `day`."""
    days = set(holidays)
    candidate = day + timedelta(days=1)
    while not is_workday(candidate, days):
        candidate += timedelta(days=1)
    return candidate


def within_workday(now: datetime, config: AppConfig) -> bool:
    """A working day, between Workday start (included) and Workday end (excluded)."""
    if not is_workday(now.date(), config.holidays):
        return False
    return config.workday_start <= now.time().replace(tzinfo=None) < config.workday_end


def next_workday_start(now: datetime, config: AppConfig) -> datetime:
    """When a notice raised at `now` outside working hours may go out."""
    today = now.date()
    if is_workday(today, config.holidays) and now.time().replace(tzinfo=None) < (
        config.workday_start
    ):
        day = today
    else:
        day = next_workday(today, config.holidays)
    return datetime.combine(day, config.workday_start, tzinfo=now.tzinfo)


def slot_times(config: AppConfig) -> dict[str, time]:
    return {
        MORNING: config.reminder_time_morning,
        MANAGER: config.reminder_time_manager,
        DUE_TODAY: config.reminder_time_due_today,
        LAST_CALL: config.reminder_time_last_call,
    }


def due_slots(now: datetime, config: AppConfig) -> list[str]:
    """Slots whose time has come today and whose day is not over (before Workday end). A run
    that missed a slot (the job failed) still sends it later the same working day."""
    if not is_workday(now.date(), config.holidays):
        return []
    clock_time = now.time().replace(tzinfo=None)
    if clock_time >= config.workday_end:
        return []
    times = slot_times(config)
    return [slot for slot in SLOT_ORDER if times[slot] <= clock_time]


# --- Who each rule concerns ----------------------------------------------------------------------


def _open(tasks: Iterable[Task]) -> list[Task]:
    return [task for task in tasks if task.id and task.status in OPEN_STATUSES]


def _doer(task: Task, staff: list[StaffMember]) -> StaffMember | None:
    return find_person(task.doer or task.owner, staff)


def days_before_rule(days: int) -> str:
    return f"due_in_{days}"


def morning_triggers(
    tasks: Iterable[Task], staff: list[StaffMember], today: date, config: AppConfig
) -> list[Trigger]:
    """Days before due, overdue and progress stale go to the Doer; review waiting to the
    reviewer (the task's Reviewer, else the Doer's supervisor, else the Manager)."""
    triggers: list[Trigger] = []
    stale_before = today - timedelta(days=config.stale_after_days)
    for task in _open(tasks):
        doer = _doer(task, staff)
        if doer is not None and task.due_date is not None:
            left = (task.due_date - today).days
            if left < 0:
                triggers.append(Trigger(OVERDUE, task.id, doer.name))
            elif left in config.remind_days_before:
                triggers.append(Trigger(days_before_rule(left), task.id, doer.name))
        touched = task.last_updated.date() if task.last_updated else task.start
        if (
            doer is not None
            and task.status in STALE_STATUSES
            and touched
            and touched < (stale_before)
        ):
            triggers.append(Trigger(STALE, task.id, doer.name))
        if task.status == "In Review":
            reviewer = default_reviewer(task.reviewer, task.doer, staff)
            if reviewer is not None:
                triggers.append(Trigger(REVIEW_WAITING, task.id, reviewer.name))
    return triggers


def due_today_triggers(
    tasks: Iterable[Task], staff: list[StaffMember], today: date, rule: str
) -> list[Trigger]:
    """Tasks due today that are not Done: the Doer and the Owner; at last call also every
    Manager."""
    triggers: list[Trigger] = []
    for task in _open(tasks):
        if task.due_date != today:
            continue
        people = [find_person(task.doer, staff), find_person(task.owner, staff)]
        if rule == LAST_CALL:
            people += managers(staff)
        seen: set[str] = set()
        for person in people:
            if person is not None and person.name.casefold() not in seen:
                seen.add(person.name.casefold())
                triggers.append(Trigger(rule, task.id, person.name))
    return triggers


def slot_triggers(
    slot: str, tasks: list[Task], staff: list[StaffMember], today: date, config: AppConfig
) -> list[Trigger]:
    if slot == MORNING:
        return morning_triggers(tasks, staff, today, config)
    if slot in (DUE_TODAY, LAST_CALL):
        return due_today_triggers(tasks, staff, today, slot)
    return []


def manager_sections(tasks: Iterable[Task], today: date) -> list[tuple[str, list[Task]]]:
    """The team summary: overdue, due today, waiting for review, awaiting approval, blocked."""
    every = [task for task in tasks if task.id]
    current = _open(every)
    return [
        ("Overdue", [t for t in current if t.due_date and t.due_date < today]),
        ("Due today", [t for t in current if t.due_date == today]),
        ("Waiting for review", [t for t in current if t.status == "In Review"]),
        ("Awaiting approval", [t for t in every if t.status == "Proposed"]),
        ("Blocked", [t for t in current if t.status == "On Hold"]),
    ]


# --- Channels ------------------------------------------------------------------------------------


def opted_in(person: StaffMember) -> bool:
    """Staff `WhatsApp opt-in`: `Yes` or the date consent was given; blank or `No` = not yet."""
    value = person.whatsapp_opt_in.strip().lower()
    return bool(value) and value not in {"no", "n", "tidak", "false"}


def channel_for(person: StaffMember) -> Channel | None:
    """Staff `Reminder channel`; blank = WhatsApp when the person has a number and opted in,
    else email. None: no way to reach them."""
    has_wa = bool(person.wa_id) and opted_in(person)
    has_email = bool(person.email.strip())
    wanted = person.reminder_channel.strip().lower()
    if wanted == "email":
        return "Email" if has_email else ("WhatsApp" if has_wa else None)
    if has_wa:
        return "WhatsApp"
    return "Email" if has_email else None


# --- Message content -----------------------------------------------------------------------------


def rule_label(rule: str, config: AppConfig) -> str:
    if rule.startswith("due_in_"):
        days = int(rule.removeprefix("due_in_"))
        return f"Due in {days} day{'s' if days != 1 else ''}"
    return {
        OVERDUE: "Overdue",
        STALE: f"Not updated for {config.stale_after_days}+ days",
        REVIEW_WAITING: "Waiting for your review",
        DUE_TODAY: "Due today, not done yet",
        LAST_CALL: "Last call: due today, not done yet",
    }.get(rule, rule)


def _rule_order(rule: str) -> tuple[int, int]:
    order = [LAST_CALL, DUE_TODAY, OVERDUE, REVIEW_WAITING]
    if rule in order:
        return (order.index(rule), 0)
    if rule.startswith("due_in_"):
        return (len(order), int(rule.removeprefix("due_in_")))
    return (len(order) + 1, 0)


def grouped(triggers: Iterable[Trigger]) -> list[tuple[str, list[str]]]:
    """(rule, task IDs) in reading order: the most urgent rule first, IDs in order."""
    by_rule: dict[str, list[str]] = {}
    for trigger in triggers:
        ids = by_rule.setdefault(trigger.rule, [])
        if trigger.task_id not in ids:
            ids.append(trigger.task_id)
    return [(rule, sorted(by_rule[rule])) for rule in sorted(by_rule, key=_rule_order)]


def task_ids(triggers: Iterable[Trigger]) -> list[str]:
    return sorted({trigger.task_id for trigger in triggers if trigger.task_id})


def short_ids(ids: list[str], limit: int = 5) -> str:
    """`G-0012, G-0015 +3`: fits a one-line template parameter."""
    shown = ", ".join(ids[:limit])
    return f"{shown} +{len(ids) - limit}" if len(ids) > limit else shown


def first_name(person: StaffMember) -> str:
    return person.name.split()[0] if person.name.split() else person.name
