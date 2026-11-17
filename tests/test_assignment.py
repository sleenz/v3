"""A PROMPT assignment starts the task at once and the Doer gets the files (DESIGN section 26).

Only a Manager or full-time staff may assign: the Manager anyone, full-time staff themselves or
someone who reports to them. Interns and additional help cannot (their PROMPT is ignored and
flagged, their `assign` refused). A PROMPT that is unsure about the owner waits for the Manager.

Fakes only, made-up people from tests/fixtures/staff.json: Staff K is the Manager; Staff A, D, P
are full time; Staff B is an intern who reports to Staff A (set here); Staff R is additional
help (set here). Monday 26 Oct 2026, 09:00 Asia/Hong_Kong; Workday 08:00-19:00.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app import clock
from app.commands import parse_command
from app.extract.ai import NullProvider
from app.gmail import process_inbox
from app.jobs import ChangesRun, run_changes
from app.lifecycle import CommandContext, run_command
from app.models import Attachment, StaffMember, Task
from app.pipeline import Services
from app.route import assignment_hold
from app.whatsapp import WaContext, WAMedia, WAMessage, finalize, handle_message
from tests.fakes import (
    FakeAIProvider,
    FakeDriveStore,
    FakeGmailClient,
    FakeQueue,
    FakeSheetsBackend,
    FakeSheetStore,
    FakeWhatsApp,
)

SHEET = "https://docs.google.com/spreadsheets/d/sheet-assign"
K_WA = "85255500011"  # Staff K, added here (fake)
B_WA = "85255500002"  # Staff B, added here (fake)
PDF = Attachment(filename="Surat.pdf", mime="application/pdf", data=b"%PDF letter")
XLSX = Attachment(filename="Data.xlsx", mime="application/vnd.ms-excel", data=b"xlsx-bytes")
BODY = "Mohon kirim laporan stress test, deadline 20 November 2026."
REPORTS_TO = {"Staff B": "Staff A"}
WA = {"Staff K": "+852 5550 0011", "Staff B": "+852 5550 0002"}


def backend(storage: str = "Drive", holidays: str = "") -> FakeSheetsBackend:
    data = FakeSheetsBackend.from_fixtures()
    staff = data.tabs["Staff"]
    head = staff[0]
    name_at, wa_at, role_at = head.index("Name"), head.index("WhatsApp"), head.index("Role")
    width = len(head)
    head.append("Reports to")
    for row in staff[1:]:
        row += [""] * (width - len(row))
        row[wa_at] = WA.get(row[name_at], row[wa_at])
        if row[name_at] == "Staff R":
            row[role_at] = "Additional help"
        row.append(REPORTS_TO.get(row[name_at], ""))
    for row in data.tabs["Config"]:
        if row and row[0] == "File storage":
            row[1] = storage
    if holidays:
        data.tabs["Config"].append(["Holidays", holidays])
    return data


def setup(storage: str = "Drive", holidays: str = "", ai=None):
    store = FakeSheetStore(backend(storage, holidays))
    if ai is not None:
        for row in store.backend.tabs["Config"]:
            if row and row[0] == "AI provider":
                row[1] = "Gemini API key"
    store.load()
    services = Services(store=store, drive=FakeDriveStore(), ai=ai or NullProvider())
    gmail = FakeGmailClient()
    run_changes(store, gmail, SHEET)  # the first run only records the baseline
    return services, gmail


_ids = iter(range(1, 1000))


def prompt_email(services, gmail, prompt: str = "PROMPT: assign this to Staff P",
                 sender: str = "staff.k@example.com", attachments=None) -> Task:  # fmt: skip
    message_id = f"req-{next(_ids)}"
    gmail.add(message_id, sender_email=sender, subject="Laporan stress test",
              body_text=f"{prompt}\n\n{BODY}",
              attachments=[PDF] if attachments is None else attachments)  # fmt: skip
    process_inbox(gmail, services)
    services.store.load()
    return next(t for t in services.store.tasks() if t.source_id == message_id)


def changes(services, gmail) -> ChangesRun:
    services.store.load()
    return run_changes(services.store, gmail, SHEET)


def assignments_to(gmail, address: str) -> list:
    return [
        e for e, _ in gmail.sent if address in e.to and e.text.startswith("You have been assigned")
    ]


def now_task(services, task_id: str) -> Task:
    services.store.load()
    return next(t for t in services.store.tasks() if t.id == task_id)


def cmd(services, gmail, actor: str, text: str):
    services.store.load()
    command = parse_command(text)
    assert command is not None, text
    person = next(p for p in services.store.staff() if p.name == actor)
    ctx = CommandContext(store=services.store, gmail=gmail, actor=person, channel="Email",
                         sheet_url=SHEET, message_id=f"m-{next(_ids)}")  # fmt: skip
    return run_command(command, ctx)


# --- the rule ------------------------------------------------------------------------------------


def person(name: str, role: str = "Full time", reports_to: str = "") -> StaffMember:
    return StaffMember.model_validate({"Name": name, "Role": role, "Reports to": reports_to})


@pytest.mark.parametrize(
    ("sender", "doer", "hold"),
    [
        (person("Boss", "Manager"), person("Anyone"), None),
        (person("Full"), person("Full"), None),  # themselves
        (person("Full"), person("Intern", "Intern", reports_to="Full"), None),  # their report
        (person("Full"), person("Other"), "Other does not report to Full"),
        (person("Intern", "Intern"), person("Intern", "Intern"), "only a Manager or full-time"),
        (person("Help", "Additional help"), person("X"), "only a Manager or full-time"),
        (None, person("X"), "the sender is not in Staff"),
    ],
)
def test_who_may_assign_whom(sender, doer, hold):
    found = assignment_hold(sender, doer)
    assert found == hold if hold is None else found is not None and found.startswith(hold)


# --- a PROMPT starts the task and the files go out -----------------------------------------------


def test_a_manager_prompt_starts_at_once_and_the_doer_gets_the_files():
    services, gmail = setup(storage="None")
    task = prompt_email(services, gmail, attachments=[PDF, XLSX])
    assert (task.doer, task.status) == ("Staff P", "Not Started")
    assert "Assigned to Staff P; started" in gmail.replies[-1][1].text
    assert changes(services, gmail).emailed == 1
    [email] = assignments_to(gmail, "staff.p@example.com")
    assert [a.filename for a in email.attachments] == ["Surat.pdf", "Data.xlsx"]
    assert changes(services, gmail).emailed == 0  # once


def test_with_drive_the_files_are_attached_too():
    services, gmail = setup(storage="Drive")
    prompt_email(services, gmail, attachments=[PDF])
    changes(services, gmail)
    [email] = assignments_to(gmail, "staff.p@example.com")
    assert [a.filename for a in email.attachments] == ["Surat.pdf"]


def test_an_intern_named_by_the_manager_does_it_and_their_supervisor_is_cc():
    services, gmail = setup()
    task = prompt_email(services, gmail, "PROMPT: assign this to Staff B")
    assert (task.owner, task.doer, task.status) == ("Staff A", "Staff B", "Not Started")
    changes(services, gmail)
    [email] = assignments_to(gmail, "staff.b@example.com")
    assert "staff.a@example.com" in email.cc


def test_a_team_prompt_starts_and_support_is_cc():
    services, gmail = setup()
    task = prompt_email(services, gmail, "PROMPT: assign this to Staff P and Staff D")
    assert (task.doer, task.supporters, task.status) == ("Staff P", ["Staff D"], "Not Started")
    changes(services, gmail)
    [email] = assignments_to(gmail, "staff.p@example.com")
    assert "staff.d@example.com" in email.cc and email.attachments


def test_a_typo_is_corrected_flagged_and_still_starts():
    services, gmail = setup()
    task = prompt_email(services, gmail, "PROMPT: assign this to Staf P")
    assert (task.doer, task.status) == ("Staff P", "Not Started")
    assert any("read as Staff P" in f for f in task.flags)


def test_reviewer_and_deadline_from_the_prompt_are_kept():
    services, gmail = setup()
    prompt = "PROMPT: assign this to Staff P with reviewer Staff D, deadline 3 days"
    task = prompt_email(services, gmail, prompt)
    assert (task.status, task.reviewer, task.due_date.isoformat()) == (
        "Not Started", "Staff D", "2026-10-29",
    )  # fmt: skip


def test_the_email_waits_for_working_hours_then_goes():
    services, gmail = setup(holidays="2026-10-27")
    with clock.freeze(datetime(2026, 10, 26, 21, 0)):
        prompt_email(services, gmail)
        assert changes(services, gmail).waiting == 1
    with clock.freeze(datetime(2026, 10, 27, 10, 0)):  # a holiday
        assert changes(services, gmail).waiting == 1
    with clock.freeze(datetime(2026, 10, 28, 8, 0)):
        assert changes(services, gmail).emailed == 1


def test_the_same_request_read_twice_makes_one_task_and_one_email():
    services, gmail = setup()
    task = prompt_email(services, gmail)
    gmail.messages[task.source_id]["labels"] |= {"INBOX", "UNREAD"}
    process_inbox(gmail, services)
    services.store.load()
    assert [t.source_id for t in services.store.tasks()].count(task.source_id) == 1
    changes(services, gmail)
    changes(services, gmail)
    assert len(assignments_to(gmail, "staff.p@example.com")) == 1


# --- full-time staff ----------------------------------------------------------------------------


def test_a_full_timer_assigning_their_own_intern_starts_at_once():
    services, gmail = setup()
    task = prompt_email(services, gmail, "PROMPT: assign this to Staff B",
                        sender="staff.a@example.com")  # fmt: skip
    assert (task.doer, task.status) == ("Staff B", "Not Started")


def test_a_full_timer_assigning_themselves_starts_at_once():
    services, gmail = setup()
    task = prompt_email(services, gmail, "PROMPT: assign this to me Staff A",
                        sender="staff.a@example.com")  # fmt: skip
    assert (task.doer, task.status) == ("Staff A", "Not Started")


def test_a_full_timer_assigning_a_peer_waits_for_the_manager():
    services, gmail = setup()
    task = prompt_email(services, gmail, "PROMPT: assign this to Staff P",
                        sender="staff.a@example.com")  # fmt: skip
    assert (task.doer, task.status) == ("Staff P", "Proposed")
    assert "PROMPT: waiting for the manager (Staff P does not report to Staff A)" in task.flags
    assert "Waiting for manager approval" in gmail.replies[-1][1].text
    assert changes(services, gmail).emailed == 0


# --- interns and additional help cannot assign ---------------------------------------------------


@pytest.mark.parametrize("sender", ["staff.b@example.com", "staff.r@example.com"])
def test_an_intern_or_additional_help_prompt_is_ignored(sender):
    services, gmail = setup()
    task = prompt_email(services, gmail, "PROMPT: assign this to Staff P", sender=sender)
    assert task.status == "Proposed"
    assert "PROMPT ignored: only a Manager or full-time staff can assign work" in task.flags
    assert "Sender's PROMPT" not in task.notes


def test_an_intern_whatsapp_prompt_is_ignored():
    services, gmail = setup()
    ctx = WaContext(services=services, wa=FakeWhatsApp(), queue=FakeQueue(), gmail=gmail,
                    sheet_url=SHEET)  # fmt: skip
    handle_message(WAMessage(id="b1", sender=B_WA, received_at=clock.now(), kind="text",
                             text=BODY), ctx)  # fmt: skip
    handle_message(WAMessage(id="b2", sender=B_WA, received_at=clock.now(), kind="text",
                             text="PROMPT: assign this to Staff P"), ctx)  # fmt: skip
    assert finalize("b1", ctx) == "logged"
    services.store.load()
    [task] = [t for t in services.store.tasks() if t.source_id == "b1"]
    assert task.status == "Proposed" and task.doer != "Staff P"
    assert "PROMPT ignored: only a Manager or full-time staff can assign work" in task.flags


@pytest.mark.parametrize("actor", ["Staff B", "Staff R"])
def test_an_intern_or_additional_help_cannot_use_assign(actor):
    services, gmail = setup()
    task, _ = services.store.append_task(Task.model_validate(
        {"Title": "Report", "Owner": actor, "Doer": actor, "Status": "In Progress",
         "Source ID": f"src-{actor}"}))  # fmt: skip
    result = cmd(services, gmail, actor, f"assign {task.id} to Staff P")
    assert not result.changed and "only a Manager or full-time staff" in result.reply
    assert now_task(services, task.id).doer == actor


def test_a_full_timer_can_still_hand_their_task_to_their_intern():
    services, gmail = setup()
    task = prompt_email(services, gmail, "PROMPT: assign this to Staff A")
    result = cmd(services, gmail, "Staff A", f"assign {task.id} to Staff B")
    assert result.changed and now_task(services, task.id).doer == "Staff B"


# --- unsure PROMPTs wait for the Manager --------------------------------------------------------


def test_names_joined_by_or_wait_for_the_manager():
    services, gmail = setup()
    task = prompt_email(services, gmail, "PROMPT: assign this to Staff P or Staff D")
    assert task.status == "Proposed"


def test_an_unknown_name_waits_for_the_manager():
    services, gmail = setup()
    task = prompt_email(services, gmail, "PROMPT: assign this to Zorro")
    assert task.status == "Proposed" and any("not found" in f for f in task.flags)


def test_the_ai_and_the_rules_disagreeing_on_the_owner_waits_for_the_manager():
    ai = FakeAIProvider(reading={"owner": "Staff D"})
    services, gmail = setup(ai=ai)
    task = prompt_email(services, gmail, "PROMPT: assign this to Staff P, or Staff D if busy")
    assert task.status == "Proposed"
    assert any(f.startswith("PROMPT: owner unclear") for f in task.flags)
    assert any(f.startswith("PROMPT: waiting for the manager") for f in task.flags)


# --- WhatsApp -----------------------------------------------------------------------------------


def wa_manager_request(services, gmail, prompt: str) -> tuple[WaContext, Task]:
    ctx = WaContext(services=services, wa=FakeWhatsApp({"media-k2": b"%PDF surat"}),
                    queue=FakeQueue(), gmail=gmail, sheet_url=SHEET)  # fmt: skip
    handle_message(WAMessage(id="k1", sender=K_WA, received_at=clock.now(), kind="text",
                             text=BODY), ctx)  # fmt: skip
    media = [WAMedia(id="media-k2", filename="Surat.pdf", mime="application/pdf")]
    handle_message(WAMessage(id="k2", sender=K_WA, received_at=clock.now(), kind="media",
                             text=prompt, media=media), ctx)  # fmt: skip
    assert finalize("k1", ctx) == "logged"
    services.store.load()
    return ctx, next(t for t in services.store.tasks() if t.source_id == "k1")


@pytest.mark.parametrize("prompt", ["PROMPT: assign this to Staff P",
                                    "PROMPT: assign this to Staff P deadline besok"])  # fmt: skip
def test_a_whatsapp_manager_prompt_starts_and_the_files_follow(prompt):
    services, gmail = setup(storage="None")
    ctx, task = wa_manager_request(services, gmail, prompt)
    assert (task.doer, task.status) == ("Staff P", "Not Started")
    assert "started" in ctx.wa.last_text(K_WA)
    changes(services, gmail)
    [email] = assignments_to(gmail, "staff.p@example.com")
    assert [a.filename for a in email.attachments] == ["Surat.pdf"]


def test_an_internship_ending_before_the_deadline_waits_for_the_manager():
    services, gmail = setup()
    staff = services.store.backend.tabs["Staff"]
    end_at, name_at = staff[0].index("Internship end date"), staff[0].index("Name")
    for row in staff[1:]:
        if row[name_at] == "Staff B":
            row[end_at] = "2026-11-01"  # before the 20 Nov deadline
    services.store.load()
    task = prompt_email(services, gmail, "PROMPT: assign this to Staff B")
    assert task.status == "Proposed" and any("internship ends" in f for f in task.flags)


# --- a WhatsApp PROMPT that arrives just after its request ---------------------------------------


def late_prompt(services, gmail, sender: str, prompt: str) -> tuple[WaContext, Task]:
    ctx = WaContext(services=services, wa=FakeWhatsApp(), queue=FakeQueue(), gmail=gmail,
                    sheet_url=SHEET)  # fmt: skip
    handle_message(WAMessage(id="l1", sender=sender, received_at=clock.now(), kind="text",
                             text=BODY), ctx)  # fmt: skip
    assert finalize("l1", ctx) == "logged"
    later = clock.now() + timedelta(minutes=2)
    handle_message(WAMessage(id="l2", sender=sender, received_at=later, kind="text",
                             text=prompt), ctx, now=later)  # fmt: skip
    services.store.load()
    return ctx, next(t for t in services.store.tasks() if t.source_id == "l1")


def test_a_late_manager_prompt_starts_the_task():
    services, gmail = setup()
    ctx, task = late_prompt(services, gmail, K_WA, "PROMPT: assign this to Staff P")
    assert (task.doer, task.status) == ("Staff P", "Not Started")
    assert "Started: Staff P gets the assignment email" in ctx.wa.last_text(K_WA)


def test_a_late_intern_prompt_is_only_a_note():
    services, gmail = setup()
    ctx, task = late_prompt(services, gmail, B_WA, "PROMPT: assign this to Staff P")
    assert task.status == "Proposed" and task.doer != "Staff P"
    assert "only a Manager or full-time staff can assign" in ctx.wa.last_text(B_WA)
    assert "Sender's PROMPT" in task.notes
