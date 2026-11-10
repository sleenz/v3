"""/jobs/reminders on a work-day schedule (DESIGN section 22), with fakes only."""

from datetime import date, datetime, timedelta

from app import clock
from app.events import flush_events
from app.extract.ai import NullProvider
from app.gmail import process_inbox
from app.jobs import run_changes
from app.lifecycle import notify
from app.models import EVENT_QUEUE, OUTBOX, REMINDERS_SENT, QueuedEvent, ReminderSent, Task
from app.pipeline import Services
from app.schedule import run_reminders
from tests.fakes import (
    FakeDriveStore,
    FakeGmailClient,
    FakeSheetsBackend,
    FakeSheetStore,
    FakeWhatsApp,
)

SHEET = "https://docs.google.com/spreadsheets/d/sheet-1"
MONDAY = date(2026, 10, 26)
FRIDAY = date(2026, 10, 30)


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=clock.TZ)


def set_config(backend: FakeSheetsBackend, name: str, value) -> None:
    rows = backend.tabs["Config"]
    for row in rows[1:]:
        if row and row[0] == name:
            row[1] = value
            return
    rows.append([name, value])


def set_staff(backend: FakeSheetsBackend, name: str, **values) -> None:
    rows = backend.tabs["Staff"]
    header = rows[0]
    for column in values:
        if column not in header:
            header.append(column)
            for row in rows[1:]:
                row.append("")
    for row in rows[1:]:
        if row[header.index("Name")] == name:
            for column, value in values.items():
                row[header.index(column)] = value


def make(**config) -> tuple[FakeSheetStore, FakeGmailClient]:
    backend = FakeSheetsBackend.from_fixtures()
    set_config(backend, "Workday start", "10:00")
    for name, value in config.items():
        set_config(backend, name, value)
    set_staff(backend, "Staff K", Role="Manager")  # the fixture Manager
    store = FakeSheetStore(backend)
    store.load()
    return store, FakeGmailClient()


def add(store: FakeSheetStore, title: str, due: date, **values) -> str:
    base = {
        "Title": title,
        "Owner": "Staff A",
        "Doer": "Staff P",
        "Due date": due,
        "Status": "In Progress",
        "Last updated": at(MONDAY, 9),
        "Source ID": f"src-{title}",
    }
    saved, _ = store.append_task(Task.model_validate({**base, **values}))
    store.load()
    return saved.id


def sent_to(gmail: FakeGmailClient, address: str) -> list:
    return [email for email, _ in gmail.sent if address in email.to]


def column(store: FakeSheetStore, tab: str, header: str) -> list:
    return store.backend.column(tab, header)


# --- the slots ---------------------------------------------------------------------------------


def test_morning_sends_one_message_per_person_combining_rules_once():
    store, gmail = make()
    a = add(store, "a", MONDAY + timedelta(days=3))
    b = add(store, "b", MONDAY - timedelta(days=2))
    add(store, "c", MONDAY + timedelta(days=2))  # no rule today
    assert run_reminders(store, gmail, None, SHEET, now=at(MONDAY, 9, 45)).slots == []
    run = run_reminders(store, gmail, None, SHEET, now=at(MONDAY, 10))
    assert run.slots == ["morning"] and run.messages == 1
    [email] = sent_to(gmail, "staff.p@example.com")
    assert email.subject.startswith("[GRC] Your tasks")
    assert "Overdue (1)" in email.text and "Due in 3 days (1)" in email.text
    assert a in email.text and b in email.text
    assert run_reminders(store, gmail, None, SHEET, now=at(MONDAY, 10, 15)).messages == 0
    assert len(sent_to(gmail, "staff.p@example.com")) == 1
    assert column(store, OUTBOX, "Kind")[-1] in ("morning", "manager")
    assert set(column(store, REMINDERS_SENT, "Rule")) >= {"overdue", "due_in_3", "slot:morning"}


def test_manager_summary_at_1015_once():
    store, gmail = make()
    add(store, "late", MONDAY - timedelta(days=1))
    add(store, "proposed", MONDAY + timedelta(days=9), Status="Proposed")
    run = run_reminders(store, gmail, None, SHEET, now=at(MONDAY, 10, 15))
    assert run.slots == ["morning", "manager"] and run.manager == 1
    [summary] = sent_to(gmail, "staff.k@example.com")
    assert summary.subject.startswith("[GRC] Team summary")
    assert "Overdue (1)" in summary.text and "Awaiting approval (1)" in summary.text
    run_reminders(store, gmail, None, SHEET, now=at(MONDAY, 10, 30))
    assert len(sent_to(gmail, "staff.k@example.com")) == 1


def test_due_today_at_1400_and_last_call_at_1630_with_the_manager():
    store, gmail = make()
    task_id = add(store, "today", MONDAY)
    run_reminders(store, gmail, None, SHEET, now=at(MONDAY, 10, 15))
    gmail.sent.clear()
    run = run_reminders(store, gmail, None, SHEET, now=at(MONDAY, 14))
    assert run.slots == ["due_today"]
    subjects = {email.to[0]: email.subject for email, _ in gmail.sent}
    assert set(subjects) == {"staff.p@example.com", "staff.a@example.com"}  # doer and owner
    assert all("Due today, not done yet" in s for s in subjects.values())
    gmail.sent.clear()
    run_reminders(store, gmail, None, SHEET, now=at(MONDAY, 16, 30))
    to = {email.to[0] for email, _ in gmail.sent}
    assert to == {"staff.p@example.com", "staff.a@example.com", "staff.k@example.com"}
    assert all("Last call" in email.subject for email, _ in gmail.sent)
    assert all(task_id in email.text for email, _ in gmail.sent)


def test_nothing_after_workday_end_or_on_weekends_and_holidays():
    store, gmail = make(Holidays="2026-10-27")
    add(store, "late", MONDAY - timedelta(days=1))
    assert run_reminders(store, gmail, None, SHEET, now=at(MONDAY, 19)).slots == []
    assert not run_reminders(store, gmail, None, SHEET, now=at(date(2026, 10, 27), 10)).working_day
    assert not run_reminders(store, gmail, None, SHEET, now=at(date(2026, 10, 31), 10)).working_day
    assert gmail.sent == []


def test_a_rule_is_never_sent_twice_on_one_date():
    store, gmail = make()
    add(store, "late", MONDAY - timedelta(days=1))
    run_reminders(store, gmail, None, SHEET, now=at(MONDAY, 10))
    rows = store.backend.tabs[REMINDERS_SENT]
    rule = rows[0].index("Rule")
    store.backend.tabs[REMINDERS_SENT] = [r for r in rows if r[rule] != "'slot:morning"]
    assert run_reminders(store, gmail, None, SHEET, now=at(MONDAY, 10, 5)).messages == 0


def test_max_per_day_merges_into_the_next_scheduled_message():
    store, gmail = make(**{"Max reminders per day": 1})
    soon = add(store, "soon", MONDAY + timedelta(days=3))
    today = add(store, "today", MONDAY)
    run_reminders(store, gmail, None, SHEET, now=at(MONDAY, 10))
    run = run_reminders(store, gmail, None, SHEET, now=at(MONDAY, 14))
    assert run.deferred >= 1
    assert len(sent_to(gmail, "staff.p@example.com")) == 1  # the cap: morning only
    gmail.sent.clear()
    run_reminders(store, gmail, None, SHEET, now=at(date(2026, 10, 27), 10))
    [merged] = sent_to(gmail, "staff.p@example.com")
    assert today in merged.text and "Overdue (1)" in merged.text  # yesterday's due today
    assert "Due today" not in merged.text
    assert soon not in merged.text  # sent Monday morning; due in 2 days has no rule
    rows = store.service_rows(REMINDERS_SENT, ReminderSent)
    assert all(r.sent_at is not None for r in rows if r.person == "Staff P")


def test_whatsapp_template_with_lihat_tugas_payload_and_email_fallback():
    store, gmail = make()
    set_staff(store.backend, "Staff P", **{"WhatsApp opt-in": "Yes"})
    store.load()
    add(store, "late", MONDAY - timedelta(days=1))
    wa = FakeWhatsApp()
    run_reminders(store, gmail, wa, SHEET, now=at(MONDAY, 10))
    [(_, name, language, params)] = [t for t in wa.templates if t[0] == "85255500016"]
    assert (name, language) == ("grc_pagi", "id") and params[0] == "Staff"
    assert wa.payloads[0] == ["view:morning:2026-10-26"]
    assert sent_to(gmail, "staff.p@example.com") == []
    assert "WhatsApp" in column(store, OUTBOX, "Channel")

    store2, gmail2 = make()
    set_staff(store2.backend, "Staff P", **{"WhatsApp opt-in": "Yes"})
    store2.load()
    add(store2, "late", MONDAY - timedelta(days=1))
    broken = FakeWhatsApp()
    broken.fail_send = True
    run_reminders(store2, gmail2, broken, SHEET, now=at(MONDAY, 10))
    assert len(sent_to(gmail2, "staff.p@example.com")) == 1  # fell back to email


def test_paused_people_get_nothing():
    store, gmail = make()
    set_staff(store.backend, "Staff P", **{"Reminders paused": "Yes"})
    store.load()
    add(store, "late", MONDAY - timedelta(days=1))
    run_reminders(store, gmail, None, SHEET, now=at(MONDAY, 10))
    assert sent_to(gmail, "staff.p@example.com") == []


# --- events outside working hours --------------------------------------------------------------


def test_an_event_at_2100_waits_for_the_next_workday_start():
    store, gmail = make()
    task_id = add(store, "review me", MONDAY + timedelta(days=5), Status="In Review",
                  Reviewer="Staff A")  # fmt: skip
    task = next(t for t in store.tasks() if t.id == task_id)
    with clock.freeze(at(FRIDAY, 21)):
        notify("in_review", task, store, gmail, SHEET)
    assert gmail.sent == [] and column(store, EVENT_QUEUE, "Task ID") == [task_id]
    run_reminders(store, gmail, None, SHEET, now=at(date(2026, 11, 2), 9, 45))
    assert gmail.sent == []  # before Workday start
    run = run_reminders(store, gmail, None, SHEET, now=at(date(2026, 11, 2), 10))
    assert run.events == 1
    notices = [e for e in sent_to(gmail, "staff.a@example.com") if "review requested" in e.subject]
    assert len(notices) == 1  # the morning reminder ("waiting for your review") also went
    assert store.service_rows(EVENT_QUEUE, QueuedEvent) == []
    assert flush_events(store, gmail, SHEET) == 0  # sent once


def test_an_event_inside_working_hours_goes_at_once():
    store, gmail = make()
    task_id = add(store, "review me", MONDAY + timedelta(days=5), Status="In Review",
                  Reviewer="Staff A")  # fmt: skip
    task = next(t for t in store.tasks() if t.id == task_id)
    with clock.freeze(at(MONDAY, 11)):
        notify("in_review", task, store, gmail, SHEET)
    assert len(sent_to(gmail, "staff.a@example.com")) == 1


def test_without_the_queue_tab_an_event_is_sent_at_once():
    store, gmail = make()
    del store.backend.tabs[EVENT_QUEUE]
    task_id = add(store, "review me", MONDAY + timedelta(days=5), Status="In Review",
                  Reviewer="Staff A")  # fmt: skip
    task = next(t for t in store.tasks() if t.id == task_id)
    with clock.freeze(at(FRIDAY, 21)):
        notify("in_review", task, store, gmail, SHEET)
    assert len(sent_to(gmail, "staff.a@example.com")) == 1  # never lost (D15)


def test_assignment_emails_wait_outside_working_hours():
    store, gmail = make()
    with clock.freeze(at(MONDAY, 9)):
        run_changes(store, gmail, SHEET)  # baseline
        add(store, "new", MONDAY + timedelta(days=5), Status="Proposed")
        run_changes(store, gmail, SHEET)
        task_id = next(t.id for t in store.tasks() if t.title == "new")
        store.update_task(task_id, {"Status": "Not Started"})
        store.load()
        run = run_changes(store, gmail, SHEET)
    assert run.waiting == 1 and gmail.sent == []
    with clock.freeze(at(MONDAY, 10, 5)):
        store.load()
        assert run_changes(store, gmail, SHEET).emailed == 1
    assert "assigned" in column(store, OUTBOX, "Kind")


# --- replies to reminder emails ----------------------------------------------------------------


def test_a_reply_to_a_reminder_is_never_a_new_request():
    store, gmail = make()
    add(store, "late", MONDAY - timedelta(days=1))
    add(store, "late too", MONDAY - timedelta(days=1))
    run_reminders(store, gmail, None, SHEET, now=at(MONDAY, 10))
    thread = column(store, OUTBOX, "Thread ID")[0]
    gmail.add("r1", thread_id=thread, sender_email="staff.p@example.com",
              subject="Re: [GRC] Your tasks", body_text="ok noted, will do")  # fmt: skip
    services = Services(store=store, drive=FakeDriveStore(), ai=NullProvider())
    before = len(store.tasks())
    with clock.freeze(at(MONDAY, 10, 30)):
        run = process_inbox(gmail, services)
    assert run.replies == 1 and len(store.tasks()) == before
    [(original, help_email, _)] = gmail.replies
    assert original == "r1" and "progress G-0012 50" in help_email.text


def test_a_command_reply_to_a_single_task_notice_needs_no_task_id():
    store, gmail = make()
    task_id = add(store, "returned", MONDAY + timedelta(days=5), Status="Returned")
    task = next(t for t in store.tasks() if t.id == task_id)
    with clock.freeze(at(MONDAY, 11)):
        notify("returned", task, store, gmail, SHEET)
    thread = column(store, OUTBOX, "Thread ID")[-1]
    gmail.add("r2", thread_id=thread, sender_email="staff.p@example.com",
              subject="Re: returned", body_text="progress 60")  # fmt: skip
    services = Services(store=store, drive=FakeDriveStore(), ai=NullProvider())
    with clock.freeze(at(MONDAY, 11, 30)):
        run = process_inbox(gmail, services)
    assert run.commands == 1
    store.load()
    assert next(t for t in store.tasks() if t.id == task_id).progress_value == 0.6
