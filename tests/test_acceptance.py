"""Accepting an assignment (DESIGN section 23) with fakes: a Manager's PROMPT names the owner,
the Doer is asked YES / NO, YES brings the files and Not Started, NO goes back to the Manager.

Fixture people: Staff K is the Manager (staff.k@example.com); Staff P and Staff A are staff.
The clock is frozen on Monday 26 Oct 2026, 09:00; the fixture's Workday start is 08:00.
"""

from datetime import datetime

from app import clock
from app.extract.ai import NullProvider
from app.gmail import process_inbox
from app.jobs import run_changes
from app.models import Attachment, Task
from app.pipeline import Services
from app.schedule import run_reminders
from app.whatsapp import WaContext, WAMessage, handle_message
from tests.fakes import (
    FakeDriveStore,
    FakeGmailClient,
    FakeQueue,
    FakeSheetsBackend,
    FakeSheetStore,
    FakeWhatsApp,
)

SHEET = "https://docs.google.com/spreadsheets/d/sheet-1"
P_WA = "85255500016"  # Staff P's fake number
PDF = Attachment(filename="Surat.pdf", mime="application/pdf", data=b"%PDF letter")


def backend(acceptance_column: bool = True) -> FakeSheetsBackend:
    data = FakeSheetsBackend.from_fixtures()
    if acceptance_column:
        data.tabs["Master"][0].append("Acceptance")
        for row in data.tabs["Master"][1:]:
            row.append("")
    return data


def setup(acceptance_column: bool = True):
    store = FakeSheetStore(backend(acceptance_column))
    store.load()
    services = Services(store=store, drive=FakeDriveStore(), ai=NullProvider())
    return services, FakeGmailClient()


def manager_assigns(services, gmail, prompt: str = "PROMPT: assign this to Staff P",
                    sender: str = "staff.k@example.com") -> Task:  # fmt: skip
    gmail.add("req-1", sender_email=sender, subject="Laporan stress test",
              body_text=f"{prompt}\n\nMohon kirim laporan stress test, deadline 20 November "
                        "2026.", attachments=[PDF])  # fmt: skip
    process_inbox(gmail, services)
    services.store.load()
    return next(t for t in services.store.tasks() if t.source_id == "req-1")


def task_now(services, task_id: str) -> Task:
    services.store.load()
    return next(t for t in services.store.tasks() if t.id == task_id)


def changes(services, gmail, wa=None):
    services.store.load()
    return run_changes(services.store, gmail, SHEET, wa)


def emails_to(gmail, address: str) -> list:
    return [email for email, _ in gmail.sent if address in email.to]


def reply(gmail, message_id: str, thread: str, body: str, sender: str = "staff.p@example.com"):
    gmail.add(message_id, thread_id=thread, sender_email=sender, subject="Re: please accept",
              body_text=body)  # fmt: skip


# --- logging --------------------------------------------------------------------------------


def test_a_managers_prompt_waits_for_the_doer_to_accept():
    services, gmail = setup()
    task = manager_assigns(services, gmail)
    assert (task.doer, task.status, task.acceptance) == ("Staff P", "Proposed", "Pending")
    [(_, ack, _)] = gmail.replies
    assert "waiting for Staff P to accept" in ack.text


def test_a_staff_members_prompt_still_waits_for_the_manager():
    services, gmail = setup()
    task = manager_assigns(services, gmail, sender="staff.a@example.com")
    assert (task.status, task.acceptance) == ("Proposed", "")
    assert "Waiting for manager approval" in gmail.replies[0][1].text


def test_a_manager_who_names_themselves_starts_at_once():
    services, gmail = setup()
    task = manager_assigns(services, gmail, prompt="PROMPT: assign this to Staff K")
    assert (task.status, task.acceptance) == ("Not Started", "Accepted")


def test_without_the_acceptance_column_nothing_changes():
    services, gmail = setup(acceptance_column=False)
    task = manager_assigns(services, gmail)
    assert task.status == "Proposed"
    changes(services, gmail)  # baseline
    changes(services, gmail)
    assert not [e for e in gmail.sent if "please accept" in e[0].subject]


# --- asking ---------------------------------------------------------------------------------


def test_the_doer_is_asked_once_by_email():
    services, gmail = setup()
    task = manager_assigns(services, gmail)
    changes(services, gmail)  # first run records the baseline
    run = changes(services, gmail)
    assert run.asked == 1
    [ask] = emails_to(gmail, "staff.p@example.com")
    assert ask.subject.startswith(f"[GRC] {task.id} – please accept")
    assert "Reply YES to accept" in ask.text
    assert "https://drive.google.com/drive/folders/folder-1" in ask.text
    assert ask.attachments == []  # the files come only after YES
    assert changes(services, gmail).asked == 0  # not twice


def test_outside_working_hours_the_question_waits():
    services, gmail = setup()
    manager_assigns(services, gmail)
    with clock.freeze(datetime(2026, 10, 26, 21, 0)):
        changes(services, gmail)
        assert changes(services, gmail).waiting == 1
    assert not emails_to(gmail, "staff.p@example.com")


# --- answering by email -----------------------------------------------------------------------


def asked(services, gmail) -> tuple[Task, str]:
    task = manager_assigns(services, gmail)
    changes(services, gmail)
    changes(services, gmail)
    thread = next(row for row in services.store.backend.tabs["_outbox"][1:]
                  if "accept_ask" in str(row))[3]  # fmt: skip
    return task, str(thread).lstrip("'")


def test_yes_starts_the_task_and_the_files_follow():
    services, gmail = setup()
    task, thread = asked(services, gmail)
    reply(gmail, "a1", thread, "Yes, siap.\n\n> quoted")
    run = process_inbox(gmail, services)
    assert run.commands == 1
    saved = task_now(services, task.id)
    assert (saved.status, saved.acceptance) == ("Not Started", "Accepted")
    assert "yours" in gmail.replies[-1][1].text
    gmail.sent.clear()
    assert changes(services, gmail).emailed == 1
    [assignment] = emails_to(gmail, "staff.p@example.com")
    assert [a.filename for a in assignment.attachments] == ["Surat.pdf"]


def test_no_returns_it_to_the_manager_who_reassigns_it():
    services, gmail = setup()
    task, thread = asked(services, gmail)
    reply(gmail, "a2", thread, "No, I am on leave that week")
    process_inbox(gmail, services)
    saved = task_now(services, task.id)
    assert (saved.status, saved.acceptance) == ("Proposed", "Declined")
    assert "Declined by Staff P: I am on leave that week" in saved.flags
    [notice] = emails_to(gmail, "staff.k@example.com")
    assert "declined" in notice.subject and "assign" in notice.text
    gmail.add("m-assign", sender_email="staff.k@example.com", subject=f"Re: [GRC] {task.id}",
              body_text=f"assign {task.id} to Staff A")  # fmt: skip
    process_inbox(gmail, services)  # a typed change waits for YES
    gmail.add("m-yes", thread_id="thread-m-assign", sender_email="staff.k@example.com",
              subject=f"Re: [GRC] {task.id}", body_text="YES")  # fmt: skip
    process_inbox(gmail, services)
    saved = task_now(services, task.id)
    assert (saved.doer, saved.acceptance) == ("Staff A", "Pending")
    assert not any(f.startswith("Declined by") for f in saved.flags)
    assert changes(services, gmail).asked == 1
    assert [e for e in emails_to(gmail, "staff.a@example.com") if "please accept" in e.subject]


def test_only_the_doer_can_answer():
    services, gmail = setup()
    task, thread = asked(services, gmail)
    reply(gmail, "a3", thread, "Yes", sender="staff.a@example.com")
    process_inbox(gmail, services)
    assert task_now(services, task.id).acceptance == "Asked"  # asked, still waiting


def test_a_typed_accept_command_anywhere():
    services, gmail = setup()
    task = manager_assigns(services, gmail)
    gmail.add("c1", sender_email="staff.p@example.com", subject="ok",
              body_text=f"accept {task.id}")  # fmt: skip
    process_inbox(gmail, services)
    assert task_now(services, task.id).status == "Not Started"


# --- answering on WhatsApp --------------------------------------------------------------------


def wa_context(services, gmail) -> WaContext:
    for row in services.store.backend.tabs["Staff"]:
        if row and row[0] == "Staff ID":
            row.append("WhatsApp opt-in")
        elif row:
            row.append("Yes")
    services.store.load()
    return WaContext(services=services, wa=FakeWhatsApp(), queue=FakeQueue(), gmail=gmail,
                     sheet_url=SHEET)  # fmt: skip


def tap(value: str) -> WAMessage:
    return WAMessage(id=f"wamid.{value}", sender=P_WA, received_at=clock.now(), kind="tap",
                     tap=value)  # fmt: skip


def text(body: str) -> WAMessage:
    return WAMessage(id=f"wamid.t-{body[:8]}", sender=P_WA, received_at=clock.now(),
                     kind="text", text=body)  # fmt: skip


def test_whatsapp_template_then_terima():
    services, gmail = setup()
    ctx = wa_context(services, gmail)
    task = manager_assigns(services, gmail)
    changes(services, gmail, ctx.wa)
    changes(services, gmail, ctx.wa)
    [(to, name, _, params)] = ctx.wa.templates
    assert (to, name, params[0]) == (P_WA, "grc_tugas_baru", task.id)
    assert ctx.wa.payloads[-1] == [f"acc:{task.id}", f"dec:{task.id}"]
    assert not emails_to(gmail, "staff.p@example.com")  # asked on WhatsApp only
    handle_message(tap(f"acc:{task.id}"), ctx)
    assert task_now(services, task.id).status == "Not Started"


def test_whatsapp_tolak_asks_the_reason():
    services, gmail = setup()
    ctx = wa_context(services, gmail)
    task = manager_assigns(services, gmail)
    handle_message(tap(f"dec:{task.id}"), ctx)
    assert "Kenapa tidak bisa" in ctx.wa.last_text(P_WA)
    handle_message(text("cuti sampai Jumat"), ctx)
    saved = task_now(services, task.id)
    assert (
        saved.acceptance == "Declined" and "Declined by Staff P: cuti sampai Jumat" in saved.flags
    )


# --- reminders --------------------------------------------------------------------------------


def test_an_unanswered_assignment_is_in_the_morning_message_and_the_manager_summary():
    services, gmail = setup()
    task = manager_assigns(services, gmail)
    services.store.load()
    run_reminders(services.store, gmail, None, SHEET,
                  now=datetime(2026, 10, 26, 10, 15, tzinfo=clock.TZ))  # fmt: skip
    mine = [e for e in emails_to(gmail, "staff.p@example.com") if "Your tasks" in e.subject]
    assert mine and "waiting for your answer" in mine[0].text and task.id in mine[0].text
    [summary] = [e for e in emails_to(gmail, "staff.k@example.com") if "Team summary" in e.subject]
    assert "Waiting for acceptance (1)" in summary.text


def test_an_unclear_answer_in_the_ask_thread_gets_a_hint_not_silence():
    services, gmail = setup()
    task, thread = asked(services, gmail)
    reply(gmail, "a4", thread, "Ya, tapi minggu depan baru bisa mulai")
    process_inbox(gmail, services)
    assert task_now(services, task.id).acceptance == "Asked"
    assert "Reply YES to accept" in gmail.replies[-1][1].text
