"""Reminder rules (DESIGN section 22): pure functions, no clock, no network."""

from datetime import date, datetime, time

import pytest

from app import clock
from app.models import AppConfig, StaffMember, Task
from app.reminders import (
    DUE_TODAY,
    LAST_CALL,
    MANAGER,
    MORNING,
    OVERDUE,
    REVIEW_WAITING,
    STALE,
    Trigger,
    channel_for,
    due_slots,
    due_today_triggers,
    grouped,
    is_workday,
    manager_sections,
    morning_triggers,
    next_workday,
    next_workday_start,
    short_ids,
    within_workday,
)

MONDAY = date(2026, 10, 26)
CONFIG = AppConfig.model_validate(
    {
        "Effort: Small": 1, "Effort: Medium": 2, "Effort: Large": 4, "Support share": 0.5,
        "Overload threshold": 10, "Stale after (days)": 5, "Leaving warning (days)": 30,
        "Unknown letter code": "Needs review", "Manager email": "manager@example.com",
        "Allowed email domain": "example.com", "Gmail done label": "GRC/Logged",
        "Recurring lead (days)": 14, "Default due (days)": 7,
        "Holidays": "2026-10-27; 2026-12-25",
    }
)  # fmt: skip
TEAM = [
    StaffMember(name="Doer One", email="d1@example.com"),
    StaffMember(name="Owner One", email="o1@example.com"),
    StaffMember(name="Boss", email="boss@example.com", role="Manager"),
]


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=clock.TZ)


def task(task_id: str, **values) -> Task:
    base = {"ID": task_id, "Title": "Report", "Owner": "Owner One", "Doer": "Doer One",
            "Status": "In Progress", "Last updated": at(MONDAY, 9)}  # fmt: skip
    return Task.model_validate({**base, **values})


# --- Config ------------------------------------------------------------------------------------


def test_config_defaults_and_typed_values():
    assert (CONFIG.workday_start, CONFIG.workday_end) == (time(10), time(19))
    assert CONFIG.reminder_time_manager == time(10, 15)
    assert CONFIG.max_reminders_per_day == 3 and CONFIG.remind_days_before == [7, 3, 1]
    assert CONFIG.holidays == [date(2026, 10, 27), date(2026, 12, 25)]
    typed = AppConfig.model_validate(
        {**CONFIG.model_dump(by_alias=True), "Workday start": 0.375, "Holidays": 46351,
         "Remind days before due": "5, 2", "Max reminders per day": 2.0}
    )  # fmt: skip
    assert typed.workday_start == time(9) and typed.remind_days_before == [5, 2]
    assert typed.holidays == [date(2026, 11, 25)] and typed.max_reminders_per_day == 2


# --- Working days ---------------------------------------------------------------------------


def test_weekends_and_holidays_are_not_working_days():
    assert is_workday(MONDAY, CONFIG.holidays)
    assert not is_workday(date(2026, 10, 27), CONFIG.holidays)  # holiday
    assert not is_workday(date(2026, 10, 31), CONFIG.holidays)  # Saturday
    assert next_workday(MONDAY, CONFIG.holidays) == date(2026, 10, 28)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (at(MONDAY, 21), at(date(2026, 10, 28), 10)),  # Tuesday is a holiday
        (at(MONDAY, 8, 30), at(MONDAY, 10)),  # before start: today
        (at(date(2026, 10, 30), 21), at(date(2026, 11, 2), 10)),  # Friday night: Monday
        (at(date(2026, 10, 31), 12), at(date(2026, 11, 2), 10)),  # Saturday
    ],
)
def test_next_workday_start(now, expected):
    assert next_workday_start(now, CONFIG) == expected


def test_within_workday():
    assert within_workday(at(MONDAY, 10), CONFIG)
    assert not within_workday(at(MONDAY, 9, 59), CONFIG)
    assert not within_workday(at(MONDAY, 19), CONFIG)
    assert not within_workday(at(date(2026, 10, 27), 12), CONFIG)


@pytest.mark.parametrize(
    ("hour", "minute", "slots"),
    [
        (9, 45, []),
        (10, 0, [MORNING]),
        (10, 15, [MORNING, MANAGER]),
        (14, 0, [MORNING, MANAGER, DUE_TODAY]),
        (16, 30, [MORNING, MANAGER, DUE_TODAY, LAST_CALL]),
        (19, 0, []),  # the day is over: nothing late
    ],
)
def test_due_slots(hour, minute, slots):
    assert due_slots(at(MONDAY, hour, minute), CONFIG) == slots
    assert due_slots(at(date(2026, 10, 31), hour, minute), CONFIG) == []


# --- Who gets what -----------------------------------------------------------------------------


def test_morning_rules():
    tasks = [
        task("G-1", **{"Due date": date(2026, 11, 2)}),  # 7 days
        task("G-2", **{"Due date": date(2026, 10, 29)}),  # 3 days
        task("G-3", **{"Due date": date(2026, 10, 28)}),  # 2 days: no rule
        task("G-4", **{"Due date": date(2026, 10, 20)}),  # overdue
        task("G-5", **{"Due date": date(2026, 12, 1), "Last updated": at(date(2026, 10, 15), 9)}),
        task("G-6", **{"Status": "In Review", "Reviewer": "Owner One"}),
        task("G-7", **{"Status": "Done", "Due date": date(2026, 10, 20)}),
        task("G-8", **{"Status": "Proposed", "Due date": date(2026, 10, 20)}),
    ]
    found = {(t.rule, t.task_id, t.person) for t in morning_triggers(tasks, TEAM, MONDAY, CONFIG)}
    assert found == {
        ("due_in_7", "G-1", "Doer One"),
        ("due_in_3", "G-2", "Doer One"),
        (OVERDUE, "G-4", "Doer One"),
        (STALE, "G-5", "Doer One"),
        (REVIEW_WAITING, "G-6", "Owner One"),
    }


def test_due_today_goes_to_doer_and_owner_and_last_call_also_to_the_manager():
    tasks = [task("G-1", **{"Due date": MONDAY}), task("G-2", **{"Due date": MONDAY,
                                                                 "Status": "Done"})]  # fmt: skip
    today = {(t.person, t.task_id) for t in due_today_triggers(tasks, TEAM, MONDAY, DUE_TODAY)}
    assert today == {("Doer One", "G-1"), ("Owner One", "G-1")}
    last = {t.person for t in due_today_triggers(tasks, TEAM, MONDAY, LAST_CALL)}
    assert last == {"Doer One", "Owner One", "Boss"}


def test_manager_sections():
    tasks = [
        task("G-1", **{"Due date": date(2026, 10, 20)}),
        task("G-2", **{"Due date": MONDAY}),
        task("G-3", **{"Status": "In Review"}),
        task("G-4", **{"Status": "Proposed"}),
        task("G-5", **{"Status": "On Hold"}),
    ]
    sections = {title: [t.id for t in found] for title, found in manager_sections(tasks, MONDAY)}
    assert sections == {
        "Overdue": ["G-1"],
        "Due today": ["G-2"],
        "Waiting for review": ["G-3"],
        "Awaiting approval": ["G-4"],
        "Blocked": ["G-5"],
    }


@pytest.mark.parametrize(
    ("values", "channel"),
    [
        ({"Email": "a@example.com"}, "Email"),
        ({"Email": "a@example.com", "WhatsApp": "+852 5550 0001"}, "Email"),  # no opt-in
        ({"Email": "a@example.com", "WhatsApp": "+852 5550 0001", "WhatsApp opt-in": "Yes"},
         "WhatsApp"),
        ({"Email": "a@example.com", "WhatsApp": "+852 5550 0001", "WhatsApp opt-in": "Yes",
          "Reminder channel": "Email"}, "Email"),
        ({"WhatsApp": "+852 5550 0001", "WhatsApp opt-in": "2026-11-01"}, "WhatsApp"),
        ({"WhatsApp": "+852 5550 0001", "WhatsApp opt-in": "No"}, None),
    ],
)  # fmt: skip
def test_channel_for(values, channel):
    assert channel_for(StaffMember.model_validate({"Name": "A", **values})) == channel


def test_grouping_and_short_ids():
    triggers = [Trigger("due_in_3", "G-2", "A"), Trigger(OVERDUE, "G-1", "A"),
                Trigger("due_in_1", "G-3", "A"), Trigger(OVERDUE, "G-4", "A")]  # fmt: skip
    assert grouped(triggers) == [(OVERDUE, ["G-1", "G-4"]), ("due_in_1", ["G-3"]),
                                 ("due_in_3", ["G-2"])]  # fmt: skip
    assert short_ids([f"G-{i}" for i in range(7)]) == "G-0, G-1, G-2, G-3, G-4 +2"
