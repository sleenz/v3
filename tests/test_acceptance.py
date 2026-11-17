"""YES / NO to a PROMPT assignment, asked with the files (DESIGN section 27). Fakes only.

A Manager's PROMPT leaves the task Proposed with Acceptance Pending. The change job sends the Doer
the assignment email with the files and the original message, plus "Reply YES / NO". YES makes
it In Progress and sends nothing more; NO + reason keeps it Proposed for the Manager, flagged.

Fixture people: Staff K is the Manager; Staff P, Staff A and Staff D are full time.
Monday 26 Oct 2026, 09:00 Asia/Hong_Kong; the fixture Workday start is 08:00.
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


def setup(acceptance_column: bool = True):
    data = FakeSheetsBackend.from_fixtures()
    if acceptance_column:
        data.tabs["Master"][0].append("Acceptance")
        for row in data.tabs["Master"][1:]:
            row.append("")
    for row in data.tabs["Config"]:
        if row and row[0] == "File storage":
            row[1] = "None"
    store = FakeSheetStore(data)
    store.load()
    services = Services(store=store, drive=FakeDriveStore(), ai=NullProvider())
    gmail = FakeGmailClient()
    run_changes(store, gmail, SHEET)  # the first run only records the baseline
    return services, gmail


def assigns(services, gmail, prompt="PROMPT: assign this to Staff P", sender="staff.k@example.com",
            message_id="req-1") -> Task:  # fmt: skip
    gmail.add(message_id, sender_email=sender, subject="Laporan stress test",
              body_text=f"{prompt}\n\nMohon kirim laporan stress test, deadline 20 November 2026.",
              attachments=[PDF])  # fmt: skip
    process_inbox(gmail, services)
    services.store.load()
    return next(t for t in services.store.tasks() if t.source_id == message_id)


def now(services, task_id: str) -> Task:
    services.store.load()
    return next(t for t in services.store.tasks() if t.id == task_id)


def changes(services, gmail, wa=None):
    services.store.load()
    return run_changes(services.store, gmail, SHEET, wa)


def assignment_emails(gmail, address: str) -> list:
    return [e for e, _ in gmail.sent if address in e.to and "You have been assigned" in e.text]


def emailed(services, gmail) -> tuple[Task, str]:
    """A PROMPT assignment whose email (with the question) has gone out: (task, its thread)."""
    task = assigns(services, gmail)
    assert changes(services, gmail).asked == 1
    row = next(r for r in services.store.backend.tabs["_outbox"][1:] if "accept_ask" in str(r))
    return task, str(row[3]).lstrip("'")


def reply(gmail, message_id, thread, body, sender="staff.p@example.com"):
    gmail.add(message_id, thread_id=thread, sender_email=sender, subject="Re: assignment",
              body_text=body)  # fmt: skip


# --- logging and the email ------------------------------------------------------------------------


def test_a_prompt_assignment_is_proposed_and_waits_for_the_doers_answer():
    services, gmail = setup()
    task = assigns(services, gmail)
    assert (task.doer, task.status, task.acceptance) == ("Staff P", "Proposed", "Pending")
    assert "asked YES or NO" in gmail.replies[-1][1].text


def test_the_doer_gets_the_files_and_the_original_with_the_question_once():
    services, gmail = setup()
    task = assigns(services, gmail)
    assert changes(services, gmail).asked == 1
    [email] = assignment_emails(gmail, "staff.p@example.com")
    assert [a.filename for a in email.attachments] == ["Surat.pdf"]
    assert "Reply YES to start" in email.text and "Mohon kirim laporan stress test" in email.text
    assert email.subject.endswith("reply YES or NO")
    assert now(services, task.id).acceptance == "Asked"
    assert changes(services, gmail).asked == 0  # once


def test_the_email_waits_for_working_hours():
    services, gmail = setup()
    with clock.freeze(datetime(2026, 10, 26, 21, 0)):
        assigns(services, gmail)
        assert changes(services, gmail).waiting == 1
    assert not assignment_emails(gmail, "staff.p@example.com")


def test_naming_yourself_needs_no_answer():
    services, gmail = setup()
    task = assigns(services, gmail, "PROMPT: assign this to Staff K")
    assert (task.status, task.acceptance) == ("Not Started", "Accepted")


def test_without_the_acceptance_column_the_task_simply_starts():
    services, gmail = setup(acceptance_column=False)
    task = assigns(services, gmail)
    assert task.status == "Not Started"
    changes(services, gmail)
    [email] = assignment_emails(gmail, "staff.p@example.com")
    assert "Reply YES" not in email.text


# --- answering ----------------------------------------------------------------------------------


def test_yes_starts_it_and_sends_nothing_more():
    services, gmail = setup()
    task, thread = emailed(services, gmail)
    reply(gmail, "a1", thread, "Yes, siap.")
    process_inbox(gmail, services)
    saved = now(services, task.id)
    assert (saved.status, saved.acceptance) == ("In Progress", "Accepted")
    assert "In Progress" in gmail.replies[-1][1].text
    gmail.sent.clear()
    assert changes(services, gmail).emailed == 0  # no second assignment email


def test_no_with_a_reason_goes_back_to_the_manager_who_reassigns():
    services, gmail = setup()
    task, thread = emailed(services, gmail)
    reply(gmail, "a2", thread, "No, I am on leave that week")
    process_inbox(gmail, services)
    saved = now(services, task.id)
    assert (saved.status, saved.acceptance) == ("Proposed", "Declined")
    assert "Declined by Staff P: I am on leave that week" in saved.flags
    assert [e for e, _ in gmail.sent if "staff.k@example.com" in e.to and "declined" in e.subject]
    gmail.add("m1", sender_email="staff.k@example.com", subject=f"Re: [GRC] {task.id}",
              body_text=f"assign {task.id} to Staff A")  # fmt: skip
    process_inbox(gmail, services)
    gmail.add("m2", thread_id="thread-m1", sender_email="staff.k@example.com",
              subject=f"Re: [GRC] {task.id}", body_text="YES")  # fmt: skip
    process_inbox(gmail, services)
    saved = now(services, task.id)
    assert (saved.doer, saved.acceptance) == ("Staff A", "Pending")
    assert changes(services, gmail).asked == 1
    [email] = assignment_emails(gmail, "staff.a@example.com")
    assert [a.filename for a in email.attachments] == ["Surat.pdf"]


def test_only_the_doer_can_answer_and_terima_kasih_is_not_yes():
    services, gmail = setup()
    task, thread = emailed(services, gmail)
    reply(gmail, "a3", thread, "Yes", sender="staff.a@example.com")
    reply(gmail, "a4", thread, "Terima kasih")
    process_inbox(gmail, services)
    assert now(services, task.id).acceptance == "Asked"


def test_a_typed_accept_command():
    services, gmail = setup()
    task, _ = emailed(services, gmail)
    gmail.add("c1", sender_email="staff.p@example.com", subject="ok", body_text=f"accept {task.id}")
    process_inbox(gmail, services)
    assert now(services, task.id).status == "In Progress"


# --- WhatsApp -----------------------------------------------------------------------------------


def wa_context(services, gmail) -> WaContext:
    for row in services.store.backend.tabs["Staff"]:
        if row and row[0] == "Staff ID":
            row.append("WhatsApp opt-in")
        elif row:
            row.append("Yes")
    services.store.load()
    return WaContext(services=services, wa=FakeWhatsApp(), queue=FakeQueue(), gmail=gmail,
                     sheet_url=SHEET)  # fmt: skip


def test_on_whatsapp_the_email_still_carries_the_files_and_terima_starts_it():
    services, gmail = setup()
    ctx = wa_context(services, gmail)
    task = assigns(services, gmail)
    changes(services, gmail, ctx.wa)
    assert assignment_emails(gmail, "staff.p@example.com")  # the files go by email
    [(to, name, _, params)] = ctx.wa.templates
    assert (to, name, params[0]) == (P_WA, "grc_tugas_baru", task.id)
    tap = WAMessage(id="wamid.acc", sender=P_WA, received_at=clock.now(), kind="tap",
                    tap=f"acc:{task.id}")  # fmt: skip
    handle_message(tap, ctx)
    assert now(services, task.id).status == "In Progress"


def test_tolak_asks_the_reason():
    services, gmail = setup()
    ctx = wa_context(services, gmail)
    task = assigns(services, gmail)
    changes(services, gmail, ctx.wa)
    handle_message(WAMessage(id="wamid.dec", sender=P_WA, received_at=clock.now(), kind="tap",
                             tap=f"dec:{task.id}"), ctx)  # fmt: skip
    handle_message(WAMessage(id="wamid.why", sender=P_WA, received_at=clock.now(), kind="text",
                             text="cuti sampai Jumat"), ctx)  # fmt: skip
    assert "Declined by Staff P: cuti sampai Jumat" in now(services, task.id).flags


# --- reminders ----------------------------------------------------------------------------------


def test_an_unanswered_assignment_is_in_the_morning_message_and_the_manager_summary():
    services, gmail = setup()
    task, _ = emailed(services, gmail)
    services.store.load()
    run_reminders(services.store, gmail, None, SHEET,
                  now=datetime(2026, 10, 26, 10, 15, tzinfo=clock.TZ))  # fmt: skip
    mine = [e for e, _ in gmail.sent if "staff.p@example.com" in e.to and "Your tasks" in e.subject]
    assert mine and "waiting for your answer" in mine[0].text and task.id in mine[0].text
    summary = [e for e, _ in gmail.sent if "staff.k@example.com" in e.to and "Team" in e.subject]
    assert summary and "Waiting for acceptance (1)" in summary[0].text
