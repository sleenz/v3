"""QA-C: tasks with several people (one Doer plus Support, DESIGN section 25) and edge cases
around them: PROMPT parsing, the Support column, permissions, `assign`, reminders, the change
job, acceptance (section 23), D15 and privacy. Fakes only; made-up names and numbers.

Fixture people: Staff K is the Manager; Staff A, Staff D and Staff P are full time; Staff B is
an intern. The clock is frozen on Monday 26 Oct 2026, 09:00 HKT (inside the fixture's working
hours, Workday start 08:00). Tests marked xfail(strict=True) document behaviour that
contradicts DESIGN.md (finding IDs QA-C<n>).
"""

from __future__ import annotations

import itertools
import logging
from datetime import date, datetime, timedelta

import pytest

from app import clock
from app.commands import Command
from app.extract.ai import NullProvider
from app.gmail import process_inbox
from app.jobs import run_changes
from app.lifecycle import CommandContext, CommandResult, run_command
from app.models import REMINDERS_SENT, Attachment, ReminderSent, StaffMember, Task
from app.notify import assignment_email
from app.pipeline import Services, decide_prompt
from app.reminders import DUE_TODAY, LAST_CALL, due_today_triggers
from app.route import joined_groups
from app.schedule import run_reminders
from app.sheets import SchemaError
from app.wa_ui import card_buttons, my_tasks
from app.whatsapp import WaContext, WAMessage, handle_message
from tests.conftest import TODAY
from tests.fakes import (
    FakeAIProvider,
    FakeDriveStore,
    FakeGmailClient,
    FakeQueue,
    FakeSheetsBackend,
    FakeSheetStore,
    FakeWhatsApp,
)

SHEET = "https://docs.google.com/spreadsheets/d/sheet-qa"
A_WA = "85255500001"  # Staff A's fake number (fixture)
P_WA = "85255500016"  # Staff P's fake number (fixture)
PDF = Attachment(filename="Surat.pdf", mime="application/pdf", data=b"%PDF letter")
_ids = itertools.count(1)

# Invented people for the pure parsing tests.
TEAM = [
    StaffMember(name="Ana Putri"),
    StaffMember(name="Budi Santoso"),
    StaffMember(name="Budi Hartono"),
    StaffMember(name="Dewi Lestari"),
    StaffMember(name="Rina Wijaya"),
    StaffMember(name="Rina Wijaya Sari"),
    StaffMember(name="Wulan Kusumawardhani Pratiwi"),
]


# --- set-up helpers ------------------------------------------------------------------------------


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


def make_backend(acceptance: bool = True, **config) -> FakeSheetsBackend:
    backend = FakeSheetsBackend.from_fixtures()
    if acceptance:
        backend.tabs["Master"][0].append("Acceptance")
        for row in backend.tabs["Master"][1:]:
            row.append("")
    set_config(backend, "Allowed senders", "@example.com")
    for name, value in config.items():
        set_config(backend, name, value)
    return backend


def make(acceptance: bool = True, backend: FakeSheetsBackend | None = None, ai=None, **config):
    backend = backend or make_backend(acceptance, **config)
    store = FakeSheetStore(backend)
    store.load()
    services = Services(store=store, drive=FakeDriveStore(), ai=ai or NullProvider())
    return services, FakeGmailClient()


def person(store, name: str) -> StaffMember:
    return next(p for p in store.staff() if p.name == name)


def add(services, **values) -> Task:
    base = {"Title": "Stress test", "Owner": "Staff P", "Doer": "Staff P", "Support": "Staff A",
            "Status": "In Progress", "Due date": TODAY + timedelta(days=10),
            "Last updated": datetime(2026, 10, 26, 8, 0, tzinfo=clock.TZ),
            "Source ID": f"src-{next(_ids)}"}  # fmt: skip
    saved, _ = services.store.append_task(Task.model_validate({**base, **values}))
    services.store.load()
    return saved


def run(services, gmail, name: str, command: Command, **extra) -> CommandResult:
    services.store.load()
    ctx = CommandContext(store=services.store, gmail=gmail, actor=person(services.store, name),
                         channel="Email", message_id=f"msg-{next(_ids)}", sheet_url=SHEET,
                         **extra)  # fmt: skip
    result = run_command(command, ctx)
    services.store.load()
    return result


def current(services, task_id: str) -> Task:
    services.store.load()
    return next(t for t in services.store.tasks() if t.id == task_id)


def changes(services, gmail, wa=None):
    services.store.load()
    return run_changes(services.store, gmail, SHEET, wa)


def assignment_emails(gmail, task_id: str) -> list:
    return [e for e, _ in gmail.sent if "You have been assigned" in e.text and task_id in e.text]


def emails_to(gmail, address: str) -> list:
    return [e for e, _ in gmail.sent if address in e.to]


def email_prompt(services, gmail, prompt: str, sender: str = "staff.k@example.com") -> Task:
    message_id = f"req-{next(_ids)}"
    gmail.add(message_id, sender_email=sender, subject="Laporan stress test",
              body_text=f"{prompt}\n\nMohon kirim laporan stress test, deadline 20 November 2026.",
              attachments=[PDF])  # fmt: skip
    process_inbox(gmail, services)
    services.store.load()
    return next(t for t in services.store.tasks() if t.source_id == message_id)


def fixture_decide(prompt: str, ai=None):
    services, _ = make()
    return decide_prompt(prompt, TODAY, services.store.staff(), ai or NullProvider())


def at(hour: int, minute: int = 0, day: date = TODAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=clock.TZ)


# --- 1. PROMPT parsing ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("prompt", "owner", "support"),
    [
        ("assign this to Staff P and Staff A", "Staff P", ["Staff A"]),
        ("assign to Staff P, Staff A dan Staff D", "Staff P", ["Staff A", "Staff D"]),
        ("assign to Staff P & Staff A", "Staff P", ["Staff A"]),
        ("assign to Staff P sama Staff A", "Staff P", ["Staff A"]),
        ("assign to Staff P serta Staff A", "Staff P", ["Staff A"]),
        ("assign to Staff P, Staff A, and Staff D", "Staff P", ["Staff A", "Staff D"]),
        ("tugaskan Staff P dan Staff A untuk laporan ini", "Staff P", ["Staff A"]),
        ("minta Staff P sama Staff A untuk cek", "Staff P", ["Staff A"]),
    ],
)
def test_prompt_joiners_make_one_doer_and_support(prompt, owner, support):
    decision = fixture_decide(prompt)
    assert (decision.owner, decision.support) == (owner, support)


@pytest.mark.parametrize(
    "prompt", ["assign to Staff P or Staff A", "assign to Staff P atau Staff A"]
)
def test_or_names_are_alternatives_nothing_goes_to_support(prompt):
    decision = fixture_decide(prompt)
    assert decision.support == []
    assert decision.owner is None and decision.flags  # unclear: flagged, the rules' owner stays


def test_a_reviewer_named_inside_the_list_is_never_support():
    decision = fixture_decide("assign to Staff P and Staff A, reviewer Staff A")
    assert (decision.owner, decision.reviewer, decision.support) == ("Staff P", "Staff A", [])


def test_the_same_person_twice_is_one_doer_and_no_support():
    decision = fixture_decide("assign to Staff P and Staff P")
    assert (decision.owner, decision.support) == ("Staff P", [])


def test_an_ai_owner_not_written_in_the_line_is_dropped_and_the_rules_team_stays():
    decision = fixture_decide("assign to Staff P and Staff A", FakeAIProvider(assignee="Staff Q"))
    assert (decision.owner, decision.support) == ("Staff P", ["Staff A"])


def test_the_ai_naming_the_second_person_keeps_the_first_as_support_and_flags_it():
    decision = fixture_decide("assign to Staff P and Staff A", FakeAIProvider(assignee="Staff A"))
    assert (decision.owner, decision.support) == ("Staff A", ["Staff P"])
    assert any("owner unclear (rules read Staff P)" in f for f in decision.flags)


def test_a_typo_in_the_first_team_name_does_not_silently_promote_the_supporter():
    decision = fixture_decide("assign to Staf P and Staff A")
    # Section 25: the first name is the Doer; section 21: a small typo is matched (and flagged).
    assert decision.owner == "Staff P" or any("Staf P" in flag for flag in decision.flags)


def test_an_ambiguous_first_name_in_a_team_does_not_silently_promote_the_next_name():
    decision = decide_prompt("assign to budi and ana", TODAY, TEAM, NullProvider())
    assert decision.owner != "Ana Putri" or any("budi" in f.casefold() for f in decision.flags)


def test_a_full_name_that_contains_another_full_name_is_read_whole():
    groups = joined_groups("assign to rina wijaya sari and ana", TEAM)
    assert [[p.name for p in g] for g in groups] == [["Rina Wijaya Sari", "Ana Putri"]]


def test_a_team_with_a_long_second_name_keeps_the_first_name_as_doer():
    prompt = "assign to ana and wulan kusumawardhani pratiwi"
    decision = decide_prompt(prompt, TODAY, TEAM, NullProvider())
    assert (decision.owner, decision.support) == ("Ana Putri", ["Wulan Kusumawardhani Pratiwi"])


def test_the_manager_naming_themselves_and_someone_else_starts_at_once_with_support():
    services, gmail = make()
    task = email_prompt(services, gmail, "PROMPT: assign this to Staff K and Staff A")
    assert (task.owner, task.doer, task.supporters) == ("Staff K", "Staff K", ["Staff A"])
    assert task.status == "Not Started"


def test_an_intern_and_their_supervisor_supervisor_is_owner_not_support():
    backend = make_backend()
    set_staff(backend, "Staff B", **{"Reports to": "Staff A"})
    services, gmail = make(backend=backend)
    task = email_prompt(services, gmail, "PROMPT: assign this to Staff B and Staff A",
                        sender="staff.d@example.com")  # fmt: skip
    assert (task.owner, task.doer, task.support) == ("Staff A", "Staff B", "")


# --- 2. The Support column -----------------------------------------------------------------------


def test_support_cell_with_commas_spaces_the_doer_and_blanks():
    task = Task.model_validate(
        {"Doer": "Staff P", "Support": " Staff A ,  Staff D;;Staff P ; ,Staff A"}
    )
    assert task.supporters == ["Staff A", "Staff D"]
    assert Task.model_validate({"Doer": "Staff P", "Support": "  ;  , "}).supporters == []


def test_unknown_names_in_support_are_skipped_by_email_and_reminders():
    services, _ = make()
    task = add(services, Support="Staff Zed; Staff A", **{"Due date": TODAY})
    staff = services.store.staff()
    email = assignment_email(task, staff, SHEET)
    assert email.cc == ["staff.a@example.com"]
    people = {t.person for t in due_today_triggers([task], staff, TODAY, DUE_TODAY)}
    assert people == {"Staff P", "Staff A"}


def test_owner_also_listed_as_support_gets_one_cc_and_one_reminder():
    services, _ = make()
    task = add(services, Owner="Staff A", Doer="Staff P", Support="Staff A; Staff D",
               **{"Due date": TODAY})  # fmt: skip
    staff = services.store.staff()
    email = assignment_email(task, staff, SHEET)
    assert email.to == ["staff.p@example.com"]
    assert sorted(email.cc) == ["staff.a@example.com", "staff.d@example.com"]
    triggers = due_today_triggers([task], staff, TODAY, LAST_CALL)
    names = [t.person for t in triggers]
    assert len(names) == len(set(names)) and {"Staff A", "Staff D", "Staff K"} <= set(names)


def test_blank_support_changes_nothing_for_the_doer():
    services, _ = make()
    task = add(services, Support="")
    email = assignment_email(task, services.store.staff(), SHEET)
    assert email.cc == [] and "Support:" not in email.text
    titles = [t for _, t in card_buttons(task, person(services.store, "Staff P"),
                                         services.store.staff())]  # fmt: skip
    assert titles == ["Update progres", "Sudah dikirim", "Lainnya"]


# --- 3. Permissions ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "allowed"),
    [
        (Command("progress", None, "40"), True),
        (Command("note", None, "data received"), True),
        (Command("blocked", None, "waiting for finance"), True),
        (Command("review", None), False),
        (Command("done", None), False),
        (Command("extension", None, "20 Nov"), False),
        (Command("snooze", None), False),
        (Command("approve", None), False),
        (Command("return", None, "fix the table"), False),
        (Command("assign", None, "Staff D"), False),
        (Command("extension_ok", None, "20 Nov"), False),
    ],
)
def test_support_permissions_on_every_command(command, allowed):
    services, gmail = make()
    task = add(services, Link="In the GRC bot mailbox: proof.pdf")
    result = run(services, gmail, "Staff A",
                 Command(command.name, task.id, command.value))  # fmt: skip
    assert result.changed is allowed, result.reply
    if command.name in ("review", "done", "extension", "snooze"):
        assert "only Staff P can do that" in result.reply
    if not allowed:
        saved = current(services, task.id)
        assert (saved.status, saved.doer, saved.supporters) == (
            "In Progress", "Staff P", ["Staff A"])  # fmt: skip


def test_support_can_read_status_and_sees_the_task_marked_support():
    services, gmail = make()
    task = add(services, **{"Due date": TODAY})
    status = run(services, gmail, "Staff A", Command("status", task.id))
    assert "support Staff A" in status.reply and not status.changed
    for name in ("mine", "today", "week"):
        listed = run(services, gmail, "Staff A", Command(name))
        assert f"{task.id} – Stress test" in listed.reply and "support)" in listed.reply


def test_support_who_is_also_the_reviewer_can_approve():
    services, gmail = make()
    task = add(services, Status="In Review", Reviewer="Staff A", **{"Needs review": "Yes"})
    titles = [t for _, t in card_buttons(task, person(services.store, "Staff A"),
                                         services.store.staff())]  # fmt: skip
    assert titles == ["Approve", "Return"]
    assert run(services, gmail, "Staff A", Command("approve", task.id)).changed
    assert current(services, task.id).status == "Approved"


def test_a_supporter_removed_by_the_manager_loses_the_rights():
    services, gmail = make()
    task = add(services)
    assert run(
        services, gmail, "Staff K", Command("assign", task.id, "Staff P and Staff D")
    ).changed
    assert current(services, task.id).supporters == ["Staff D"]
    refused = run(services, gmail, "Staff A", Command("progress", task.id, "70"))
    assert not refused.changed and "not the Owner or Doer" in refused.reply
    assert run(services, gmail, "Staff D", Command("progress", task.id, "70")).changed


def test_support_on_a_task_waiting_for_approval_changes_nothing():
    services, gmail = make()
    task = add(services, Status="Proposed")
    result = run(services, gmail, "Staff A", Command("progress", task.id, "50"))
    assert not result.changed and "waiting for the manager" in result.reply


def test_support_by_email_progress_needs_yes_review_is_refused():
    services, gmail = make()
    task = add(services)
    gmail.add("m-prog", sender_email="staff.a@example.com", subject="update",
              body_text=f"progress {task.id} 40")  # fmt: skip
    process_inbox(gmail, services)
    assert "Reply YES" in gmail.replies[-1][1].text
    gmail.add("m-prog-yes", thread_id="thread-m-prog", sender_email="staff.a@example.com",
              subject="Re: update", body_text="YES")  # fmt: skip
    process_inbox(gmail, services)
    assert current(services, task.id).progress_value == 0.4
    gmail.add("m-rev", sender_email="staff.a@example.com", subject="review",
              body_text=f"review {task.id}")  # fmt: skip
    process_inbox(gmail, services)
    gmail.add("m-rev-yes", thread_id="thread-m-rev", sender_email="staff.a@example.com",
              subject="Re: review", body_text="YES")  # fmt: skip
    process_inbox(gmail, services)
    assert "only Staff P can do that" in gmail.replies[-1][1].text
    assert current(services, task.id).status == "In Progress"


# --- 3b. WhatsApp taps by a supporter ------------------------------------------------------------


def wa_setup():
    backend = make_backend()
    set_staff(backend, "Staff A", **{"WhatsApp opt-in": "Yes"})
    services, gmail = make(backend=backend)
    ctx = WaContext(services=services, wa=FakeWhatsApp(), queue=FakeQueue(), gmail=gmail,
                    sheet_url=SHEET)  # fmt: skip
    return services, gmail, ctx


def tap(value: str, sender: str = A_WA) -> WAMessage:
    return WAMessage(id=f"wamid.qa{next(_ids)}", sender=sender, received_at=clock.now(),
                     kind="tap", tap=value)  # fmt: skip


def test_whatsapp_supporter_card_progress_and_refused_lainnya_actions():
    services, _, ctx = wa_setup()
    task = add(services, Link="In the GRC bot mailbox: proof.pdf")
    handle_message(tap("view:morning:2026-10-26"), ctx)
    [(_, _, buttons)] = [b for b in ctx.wa.buttons if b[0] == A_WA and task.id in b[1]]
    assert [t for _, t in buttons] == ["Update progres", "Lainnya"]
    handle_message(tap(f"prog:{task.id}:75"), ctx)
    assert current(services, task.id).progress_value == 0.75
    handle_message(tap(f"done:{task.id}"), ctx)  # Lainnya → Selesai (proof already in Link)
    assert "only Staff P can do that" in ctx.wa.last_text(A_WA)
    handle_message(tap(f"snz:{task.id}"), ctx)  # Lainnya → Ingatkan besok
    assert "only Staff P can do that" in ctx.wa.last_text(A_WA)
    assert current(services, task.id).status == "In Progress"


def test_whatsapp_supporter_marks_blocked_through_the_question():
    services, _, ctx = wa_setup()
    task = add(services)
    assert handle_message(tap(f"blk:{task.id}"), ctx) == "tap_ask_blocked_reason"
    message = WAMessage(id=f"wamid.qa{next(_ids)}", sender=A_WA, received_at=clock.now(),
                        kind="text", text="menunggu data keuangan")  # fmt: skip
    handle_message(message, ctx)
    saved = current(services, task.id)
    assert saved.status == "On Hold" and "by Staff A: menunggu data keuangan" in saved.notes


def test_my_tasks_lists_a_supported_task_once():
    services, _ = make()
    task = add(services, Owner="Staff A", Support="Staff A")
    mine = my_tasks(person(services.store, "Staff A"), services.store.tasks(),
                    services.store.staff())  # fmt: skip
    assert [t.id for t in mine] == [task.id]


# --- 4. The Manager's `assign` -------------------------------------------------------------------


def test_manager_assigns_a_team_one_email_to_the_doer_cc_support_no_duplicates():
    services, gmail = make()
    task = add(services, Support="")
    changes(services, gmail)  # baseline
    result = run(services, gmail, "Staff K", Command("assign", task.id, "Staff A and Staff D"))
    saved = current(services, task.id)
    assert result.changed
    assert (saved.owner, saved.doer, saved.supporters) == ("Staff A", "Staff A", ["Staff D"])
    [email] = assignment_emails(gmail, task.id)
    assert email.to == ["staff.a@example.com"] and email.cc == ["staff.d@example.com"]
    assert "Support: Staff D" in email.text
    changes(services, gmail)
    changes(services, gmail)
    assert len(assignment_emails(gmail, task.id)) == 1


def test_an_unknown_second_name_changes_nothing():
    services, gmail = make()
    task = add(services)
    result = run(services, gmail, "Staff K", Command("assign", task.id, "Staff D and Staff Zed"))
    assert not result.changed and "Staff Zed" in result.reply
    saved = current(services, task.id)
    assert (saved.doer, saved.supporters) == ("Staff P", ["Staff A"])
    assert not assignment_emails(gmail, task.id)


def test_one_name_keeps_support():
    services, gmail = make()
    task = add(services)
    assert run(services, gmail, "Staff K", Command("assign", task.id, "Staff D")).changed
    saved = current(services, task.id)
    assert (saved.doer, saved.supporters) == ("Staff D", ["Staff A"])
    [email] = assignment_emails(gmail, task.id)
    assert "staff.a@example.com" in email.cc


def test_only_the_manager_can_assign_several_people():
    services, gmail = make()
    task = add(services)
    refused = run(services, gmail, "Staff P", Command("assign", task.id, "Staff P and Staff D"))
    assert not refused.changed and "Only the manager" in refused.reply
    assert current(services, task.id).supporters == ["Staff A"]


def test_a_team_with_an_intern_and_their_supervisor():
    backend = make_backend()
    set_staff(backend, "Staff B", **{"Reports to": "Staff A"})
    services, gmail = make(backend=backend)
    task = add(services)
    run(services, gmail, "Staff K", Command("assign", task.id, "Staff B and Staff A"))
    saved = current(services, task.id)
    assert (saved.owner, saved.doer, saved.support) == ("Staff A", "Staff B", "")


def test_the_reviewer_is_never_support_when_the_manager_assigns_a_team():
    services, gmail = make()
    task = add(services, Reviewer="Staff D", **{"Needs review": "Yes"})
    run(services, gmail, "Staff K", Command("assign", task.id, "Staff P and Staff D"))
    assert "Staff D" not in current(services, task.id).supporters  # section 25


def test_the_reviewer_is_never_support_on_a_reassign_for_acceptance():
    services, gmail = make()
    task = add(services, Status="Proposed", Acceptance="Pending", Reviewer="Staff D",
               **{"Needs review": "Yes"})  # fmt: skip
    run(services, gmail, "Staff K", Command("assign", task.id, "Staff A and Staff D"))
    assert "Staff D" not in current(services, task.id).supporters


# --- 4b / 7. Acceptance with Support -------------------------------------------------------------


def test_team_prompt_by_the_manager_starts_and_emails_the_doer_cc_support():
    services, gmail = make()
    changes(services, gmail)  # baseline
    task = email_prompt(services, gmail, "PROMPT: assign this to Staff P and Staff A")
    assert (task.doer, task.supporters, task.status) == ("Staff P", ["Staff A"], "Proposed")
    changes(services, gmail)
    [email] = assignment_emails(gmail, task.id)
    assert email.to == ["staff.p@example.com"] and email.cc == ["staff.a@example.com"]
    assert [a.filename for a in email.attachments] == ["Surat.pdf"]


# --- 5. Reminders --------------------------------------------------------------------------------


def reminders_setup(**config):
    backend = make_backend(**config)
    set_staff(backend, "Staff A", **{"WhatsApp opt-in": "Yes"})
    return make(backend=backend)


def sent_rows(services) -> list[ReminderSent]:
    services.store.load()
    return services.store.service_rows(REMINDERS_SENT, ReminderSent)


def test_support_is_in_due_today_and_last_call_not_in_the_morning():
    services, gmail = reminders_setup()
    soon = add(services, Title="Soon", **{"Due date": TODAY + timedelta(days=3)})
    today = add(services, Title="Today", **{"Due date": TODAY})
    run_reminders(services.store, gmail, None, SHEET, now=at(10, 0))
    morning = {(r.person, r.task_id) for r in sent_rows(services) if r.rule == "due_in_3"}
    assert morning == {("Staff P", soon.id)}
    run_reminders(services.store, gmail, None, SHEET, now=at(14, 0))
    run_reminders(services.store, gmail, None, SHEET, now=at(16, 30))
    rows = sent_rows(services)
    for rule in (DUE_TODAY, LAST_CALL):
        people = {r.person for r in rows if r.rule == rule and r.task_id == today.id}
        assert {"Staff A", "Staff P"} <= people
    assert {r.person for r in rows if r.rule == LAST_CALL} >= {"Staff K"}


def test_doer_on_one_task_and_support_on_another_gets_one_due_today_message():
    services, gmail = reminders_setup()
    own = add(services, Title="Own", Owner="Staff A", Doer="Staff A", Support="",
              **{"Due date": TODAY})  # fmt: skip
    helping = add(services, Title="Helping", **{"Due date": TODAY})  # Staff A is Support
    run_reminders(services.store, gmail, None, SHEET, now=at(14, 0))
    mails = [e for e in emails_to(gmail, "staff.a@example.com") if "Due today" in e.subject]
    assert len(mails) == 1 and own.id in mails[0].text and helping.id in mails[0].text


def test_the_daily_cap_defers_a_supporters_due_today_and_merges_it_later():
    services, gmail = reminders_setup(**{"Max reminders per day": 1})
    add(services, Title="Own", Owner="Staff A", Doer="Staff A", Support="",
        **{"Due date": TODAY + timedelta(days=3)})  # fmt: skip
    helping = add(services, Title="Helping", **{"Due date": TODAY})
    run_reminders(services.store, gmail, None, SHEET, now=at(10, 0))  # Staff A: morning (1/1)
    run_reminders(services.store, gmail, None, SHEET, now=at(14, 0))
    rows = sent_rows(services)
    deferred = [r for r in rows if r.person == "Staff A" and r.sent_at is None]
    assert [(r.rule, r.task_id) for r in deferred] == [(DUE_TODAY, helping.id)]
    assert [r for r in rows if r.person == "Staff P" and r.rule == DUE_TODAY and r.sent_at]
    a_messages = [e for e in emails_to(gmail, "staff.a@example.com")]
    assert len(a_messages) == 1  # the cap holds


def test_channel_per_person_supporter_on_whatsapp_doer_by_email():
    services, gmail = reminders_setup()
    task = add(services, **{"Due date": TODAY})
    wa = FakeWhatsApp()
    run_reminders(services.store, gmail, wa, SHEET, now=at(14, 0))
    templates = [(to, name, params) for to, name, _, params in wa.templates]
    assert any(to == A_WA and name == "grc_jatuh_tempo" and task.id in params[2]
               for to, name, params in templates)  # fmt: skip
    assert not any(to == P_WA for to, _, _ in templates)  # Staff P has not opted in
    assert [e for e in emails_to(gmail, "staff.p@example.com") if task.id in e.text]
    assert not emails_to(gmail, "staff.a@example.com")


def test_a_snoozed_task_is_left_out_only_for_the_person_who_snoozed():
    services, gmail = reminders_setup()
    task = add(services, **{"Due date": TODAY + timedelta(days=1)})
    assert run(services, gmail, "Staff P", Command("snooze", task.id)).changed
    with clock.freeze(at(14, 0, TODAY + timedelta(days=1))):
        run_reminders(services.store, gmail, None, SHEET, now=at(14, 0, TODAY + timedelta(days=1)))
    people = {r.person for r in sent_rows(services) if r.rule == DUE_TODAY}
    assert "Staff A" in people


# --- 6. The change job with Support --------------------------------------------------------------


def test_approval_in_the_sheet_sends_one_email_cc_without_duplicates():
    services, gmail = make()
    task = add(services, Status="Proposed", Owner="Staff A", Doer="Staff P",
               Support="Staff A, Staff D; Staff A")  # fmt: skip
    changes(services, gmail)  # baseline
    services.store.update_task(task.id, {"Status": "Not Started"})
    assert changes(services, gmail).emailed == 1
    [email] = assignment_emails(gmail, task.id)
    assert email.to == ["staff.p@example.com"]
    assert sorted(email.cc) == ["staff.a@example.com", "staff.d@example.com"]
    assert changes(services, gmail).emailed == 0
    assert len(assignment_emails(gmail, task.id)) == 1


# --- 8. D15 and robustness -----------------------------------------------------------------------


def test_a_failed_send_during_a_team_assign_keeps_the_change_and_the_job_retries():
    services, gmail = make()
    task = add(services, Support="")
    changes(services, gmail)  # baseline
    gmail.fail_send = True
    result = run(services, gmail, "Staff K", Command("assign", task.id, "Staff A and Staff D"))
    assert result.changed
    assert (current(services, task.id).doer, current(services, task.id).supporters) == (
        "Staff A", ["Staff D"])  # fmt: skip
    assert changes(services, gmail).not_sent == 1
    gmail.fail_send = False
    assert changes(services, gmail).emailed == 1
    [email] = assignment_emails(gmail, task.id)
    assert email.cc == ["staff.d@example.com"]


def test_a_supporters_new_email_no_logs_the_request_even_when_the_reply_fails():
    task_ai = FakeAIProvider()
    services, gmail = make(ai=task_ai, **{"AI provider": "Gemini API key"})
    task = add(services)
    task_ai.reply = {"task_id": task.id, "intent": "progress", "progress": 60, "note": None,
                     "requested_date": None, "reply_to_user": "ok", "confidence": 0.9}  # fmt: skip
    gmail.add("upd-1", sender_email="staff.a@example.com", subject="weekly note",
              body_text="the stress test is about 60% done now")  # fmt: skip
    process_inbox(gmail, services)
    assert "Reply NO if this is a new request" in gmail.replies[-1][1].text
    gmail.fail_reply = True
    gmail.add("upd-no", thread_id="thread-upd-1", sender_email="staff.a@example.com",
              subject="Re: weekly note", body_text="NO")  # fmt: skip
    process_inbox(gmail, services)
    services.store.load()
    assert services.store.task_by_source_id("upd-1") is not None  # D15: row first
    assert "GRC/Logged" in gmail.labels("upd-1")
    assert current(services, task.id).progress_value is None


def test_a_new_email_with_a_bare_command_first_line_no_still_logs_it():
    services, gmail = make()
    task = add(services)
    gmail.add("upd-2", sender_email="staff.a@example.com", subject="weekly note",
              body_text=f"{task.id} 60\n\nAlso see the new circular.")  # fmt: skip
    process_inbox(gmail, services)
    gmail.add("upd-2-no", thread_id="thread-upd-2", sender_email="staff.a@example.com",
              subject="Re: weekly note", body_text="NO")  # fmt: skip
    process_inbox(gmail, services)
    services.store.load()
    assert services.store.task_by_source_id("upd-2") is not None


def test_a_full_timer_team_prompt_for_peers_waits_for_approval_with_support():
    services, gmail = make(acceptance=False)
    task = email_prompt(services, gmail, "PROMPT: assign this to Staff P and Staff A",
                        sender="staff.d@example.com")  # fmt: skip
    assert (task.status, task.doer, task.supporters) == ("Proposed", "Staff P", ["Staff A"])
    assert "Waiting for manager approval" in gmail.replies[-1][1].text


def test_a_missing_support_header_stops_the_job_at_load():
    """`Support` is a required Master column (section 3): every job loads first and stops."""
    backend = make_backend()
    master = backend.tabs["Master"]
    index = master[0].index("Support")
    for row in master:
        del row[index]
    with pytest.raises(SchemaError) as error:
        FakeSheetStore(backend).load()
    assert any("Support" in problem for problem in error.value.problems)


# --- 9. Privacy ----------------------------------------------------------------------------------


def test_logs_carry_no_names_emails_or_text_in_a_team_flow(caplog):
    from app.logs import JsonFormatter

    services, gmail = reminders_setup()
    with caplog.at_level(logging.DEBUG, logger="grc"):
        task = email_prompt(services, gmail, "PROMPT: assign this to Staff P and Staff A")
        changes(services, gmail)
        changes(services, gmail)
        run(services, gmail, "Staff K", Command("assign", task.id, "Staff D and Staff A"))
        run(services, gmail, "Staff A", Command("blocked", task.id, "rahasia direksi"))
        later = add(services, Title="Laporan rahasia", **{"Due date": TODAY})
        run_reminders(services.store, gmail, FakeWhatsApp(), SHEET, now=at(14, 0))
        assert later.id
    assert len(caplog.records) >= 3  # the flow did log (counts, IDs, kinds)
    formatted = "".join(JsonFormatter().format(record) for record in caplog.records)
    for secret in ("Staff A", "Staff P", "Staff D", "Staff K", "example.com", A_WA,
                   "rahasia", "stress test", "Stress test", "Laporan"):  # fmt: skip
        assert secret not in formatted, secret
