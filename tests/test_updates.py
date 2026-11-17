"""Updates by any email, `status G-0012`, and before → after confirmations (DESIGN section 24).

Fakes only: the AI is `FakeAIProvider(reply=...)`. Monday 26 Oct 2026, 09:00 (working hours).
Staff A (staff.a@example.com) is a Full time staff member; Staff K is the Manager.
"""

from datetime import date, timedelta

import pytest

from app import clock
from app.commands import Command, parse_command
from app.extract.ai import NullProvider
from app.gmail import process_inbox
from app.lifecycle import task_status
from app.models import Task
from app.pipeline import Services
from app.replies import describe
from app.whatsapp import WaContext, WAMessage, handle_message
from tests.fakes import (
    FakeAIProvider,
    FakeDriveStore,
    FakeGmailClient,
    FakeQueue,
    FakeSheetsBackend,
    FakeSheetStore,
    FakeWhatsApp,
)

MONDAY = date(2026, 10, 26)
A_MAIL = "staff.a@example.com"


def reading(**values) -> dict:
    base = {"task_id": None, "intent": "unknown", "progress": None, "note": None,
            "requested_date": None, "reply_to_user": "ok", "confidence": 0.9}  # fmt: skip
    return {**base, **values}


def context(ai=None, ai_on: bool = False) -> WaContext:
    backend = FakeSheetsBackend.from_fixtures()
    for row in backend.tabs["Config"]:
        if row[0] == "AI provider":
            row[1] = "Gemini API key" if ai_on else "Off"
        if row[0] == "Allowed senders":
            row[1] = "@example.com"
    store = FakeSheetStore(backend)
    store.load()
    services = Services(store=store, drive=FakeDriveStore(), ai=ai or NullProvider())
    return WaContext(services=services, wa=FakeWhatsApp(), queue=FakeQueue(),
                     gmail=FakeGmailClient())  # fmt: skip


def add(ctx: WaContext, title: str, days: int = 5, **values) -> str:
    base = {"Title": title, "Owner": "Staff A", "Doer": "Staff A",
            "Due date": MONDAY + timedelta(days=days), "Status": "In Progress",
            "Progress": 0.4, "Source ID": f"src-{title}"}  # fmt: skip
    saved, _ = ctx.store.append_task(Task.model_validate({**base, **values}))
    ctx.store.load()
    return saved.id


def task(ctx: WaContext, task_id: str) -> Task:
    ctx.store.load()
    return next(t for t in ctx.store.tasks() if t.id == task_id)


def email(ctx, message_id: str, body: str, subject: str = "Update", **extra) -> None:
    ctx.gmail.add(message_id, sender_email=A_MAIL, subject=subject, body_text=body, **extra)
    ctx.store.load()
    process_inbox(ctx.gmail, ctx.services)


def requests(ctx) -> list[Task]:
    ctx.store.load()
    return [t for t in ctx.store.tasks() if t.source_id.startswith("m-")]


# --- status -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "has_context", "expected"),
    [
        ("status G-12", False, Command("status", "G-0012")),
        ("how is G-0012?", False, Command("status", "G-0012")),
        ("gimana g12", False, Command("status", "G-0012")),
        ("cek status G-0012", False, Command("status", "G-0012")),
        ("status", True, Command("status")),
        ("status", False, None),
        ("status update: the report is finished", True, None),
        ("gimana kabarnya semua", False, None),
    ],
)
def test_status_parsing(text, has_context, expected):
    assert parse_command(text, has_context=has_context) == expected


def test_status_text_reads_the_sheet():
    staff = []
    base = {"ID": "G-0012", "Title": "Stress test", "Doer": "Staff A", "Progress": 0.4,
            "Due date": MONDAY + timedelta(days=3)}  # fmt: skip
    text = task_status(Task.model_validate({**base, "Status": "In Progress"}), staff, MONDAY)
    assert text.startswith("G-0012 – Stress test: due Thu 29 Oct 2026 (3 hari lagi)")
    assert "status In Progress, progress 40%" in text
    proposed = Task.model_validate({**base, "Status": "Proposed"})
    assert "Waiting for the manager's approval." in task_status(proposed, staff, MONDAY)
    notes = "x\nBlocked 26 Oct 2026 by Staff A: finance data"
    held = Task.model_validate({**base, "Status": "On Hold", "Notes": notes})
    assert "Blocked 26 Oct 2026 by Staff A: finance data" in task_status(held, staff, MONDAY)


def test_status_by_email_changes_nothing():
    ctx = context()
    first = add(ctx, "ATMR report")
    email(ctx, "m-1", f"status {first}")
    reply = ctx.gmail.replies[-1][1].text
    assert reply.startswith(f"{first} – ATMR report") and "progress 40%" in reply
    assert task(ctx, first).progress_value == 0.4 and not requests(ctx)


def test_status_on_whatsapp():
    ctx = context()
    first = add(ctx, "ATMR report")
    for row in ctx.store.backend.tabs["Staff"]:
        row.append("WhatsApp opt-in" if row and row[0] == "Staff ID" else "Yes")
    ctx.store.load()
    message = WAMessage(id="wamid.s1", sender="85255500001", received_at=clock.now(),
                        kind="text", text=f"status {first}")  # fmt: skip
    handle_message(message, ctx)
    assert ctx.wa.last_text("85255500001").startswith(f"{first} – ATMR report")


# --- before → after ------------------------------------------------------------------------------


def test_confirmations_show_before_and_after():
    current = Task.model_validate({"ID": "G-0012", "Status": "In Progress", "Progress": 0.4,
                                   "Doer": "Staff A", "Due date": date(2026, 11, 20)})  # fmt: skip
    new = Task.model_validate({"ID": "G-0012", "Status": "Not Started"})
    assert describe(Command("progress", "G-0012", "70"), current) == "Progress G-0012: 40% → 70%"
    assert describe(Command("progress", "G-0012", "10"), new) == (
        "Progress G-0012: 0% → 10% (status → In Progress)"
    )
    assert describe(Command("blocked", "G-0012", "finance"), current) == (
        "Status G-0012: In Progress → On Hold (terhambat): finance"
    )
    assert describe(Command("extension", "G-0012", "2026-11-27"), current) == (
        "Minta G-0012 due 20 Nov 2026 → 27 Nov 2026 (manager yang memutuskan)"
    )
    assert describe(Command("assign", "G-0012", "Staff P"), current) == (
        "Serahkan G-0012: doer Staff A → Staff P"
    )
    assert describe(Command("review", "G-0012"), current, ["a.pdf"]) == (
        "Kirim G-0012 untuk review – Status G-0012: In Progress → In Review (file: a.pdf)"
    )
    assert describe(Command("progress", "G-0012", "70"), None) == "Progress G-0012 → 70%"


# --- updates in a new email ---------------------------------------------------------------------


def test_a_new_email_naming_a_task_is_read_as_an_update_then_yes():
    ai = FakeAIProvider(reply=reading(intent="progress", progress=70))
    ctx = context(ai, ai_on=True)
    first = add(ctx, "ATMR report")
    email(ctx, "m-1", f"Hi, {first} is at 70% now, waiting for finance.")
    ask = ctx.gmail.replies[-1][1].text
    assert f"Progress {first}: 40% → 70%" in ask and "new request instead" in ask
    assert task(ctx, first).progress_value == 0.4 and not requests(ctx)
    assert "INBOX" in ctx.gmail.messages["m-1"]["labels"]  # kept until YES / NO
    email(ctx, "m-2", "Yes", thread_id="thread-m-1")
    assert task(ctx, first).progress_value == 0.7
    assert "INBOX" not in ctx.gmail.messages["m-1"]["labels"]


def test_no_logs_the_new_email_as_a_request():
    ai = FakeAIProvider(reply=reading(intent="progress", progress=70))
    ctx = context(ai, ai_on=True)
    first = add(ctx, "ATMR report")
    email(ctx, "m-1", f"Regarding {first}: please also prepare the 70% scenario.")
    email(ctx, "m-2", "No", thread_id="thread-m-1")
    [logged] = requests(ctx)
    assert logged.source_id == "m-1" and task(ctx, first).progress_value == 0.4
    assert f"new request: {logged.id}" in ctx.gmail.replies[-1][1].text


def test_without_a_task_id_only_the_ai_naming_one_of_my_tasks_counts():
    ctx = context(FakeAIProvider(reply=reading(intent="progress", progress=50)), ai_on=True)
    first = add(ctx, "ATMR report")
    add(ctx, "Stress test")
    email(ctx, "m-1", "Laporannya udah setengah jalan")  # two tasks, the AI named none
    assert len(requests(ctx)) == 1  # logged as a request, as before
    ctx = context(
        FakeAIProvider(reply=reading(intent="progress", progress=50, task_id="G-0001")),
        ai_on=True,
    )
    first = add(ctx, "ATMR report")
    add(ctx, "Stress test")
    email(ctx, "m-1", "The ATMR report is half done")
    assert f"Progress {first}: 40% → 50%" in ctx.gmail.replies[-1][1].text
    assert not requests(ctx)


@pytest.mark.parametrize(
    ("body", "subject", "intent"),
    [
        ("PROMPT: assign this to Staff P\n\nG-0001 follow-up: prepare Q4", "Request", "progress"),
        ("---------- Forwarded message ---------\nFrom: X <x@other.com>\nSubject: Q\n\nG-0001 70%",
         "Fwd: Q", "progress"),
        ("Please prepare the Q4 stress test, see G-0001 for last year", "New", "request"),
    ],
)  # fmt: skip
def test_requests_stay_requests(body, subject, intent):
    ai = FakeAIProvider(reply=reading(intent=intent, progress=70, task_id="G-0001"))
    ctx = context(ai, ai_on=True)
    add(ctx, "ATMR report")
    email(ctx, "m-1", body, subject=subject)
    assert len(requests(ctx)) == 1


def test_with_the_ai_off_an_email_without_a_task_id_is_a_request():
    ctx = context()
    add(ctx, "ATMR report")
    email(ctx, "m-1", "udah 70% tinggal nunggu data finance")
    assert len(requests(ctx)) == 1


def test_a_question_in_a_new_email_is_answered_from_the_sheet():
    ctx = context(FakeAIProvider(reply=reading(intent="question")), ai_on=True)
    first = add(ctx, "ATMR report")
    email(ctx, "m-1", f"When is {first} due?")
    assert ctx.gmail.replies[-1][1].text.startswith(f"{first} – ATMR report: due Sat 31 Oct")
    assert not requests(ctx)


def test_a_forward_without_fwd_in_the_subject_is_a_request():
    ctx = context(FakeAIProvider(reply=reading(intent="progress", progress=70)), ai_on=True)
    first = add(ctx, "ATMR report")
    head = "---------- Forwarded message ---------\nFrom: X <x@other.com>\nSubject: Q\n\n"
    email(ctx, "m-1", f"{head}{first} 70%", subject="Q")
    assert len(requests(ctx)) == 1


def test_the_ai_naming_someone_elses_task_is_a_request():
    ai = FakeAIProvider(reply=reading(intent="progress", progress=50, task_id="G-0002"))
    ctx = context(ai, ai_on=True)
    add(ctx, "ATMR report")
    add(ctx, "Stress test", Owner="Staff P", Doer="Staff P")  # G-0002 is not Staff A's
    email(ctx, "m-1", "The stress test is half done")
    assert len(requests(ctx)) == 1 and task(ctx, "G-0002").progress_value == 0.4
