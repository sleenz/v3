"""The Doer reports how and when a task was submitted (DESIGN section 28). Fakes only.

Monday 26 Oct 2026, 09:00 Asia/Hong_Kong (frozen). Staff P does the task; Staff K is the Manager.
"""

from datetime import datetime

import pytest

from app import clock
from app.commands import Command, parse_command
from app.extract.ai import NullProvider
from app.gmail import process_inbox
from app.lifecycle import CommandContext, run_command
from app.models import Attachment, Task
from app.pipeline import Services
from app.replies import describe
from app.submission import read
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

NOW = datetime(2026, 10, 26, 16, 0, tzinfo=clock.TZ)
P_WA = "85255500016"


# --- reading how and when -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "method", "at"),
    [
        ("hardcopy ke OJK hari ini 15:30", "Hardcopy", datetime(2026, 10, 26, 15, 30)),
        ("via email tadi jam 3.10", "Email", datetime(2026, 10, 26, 15, 10)),  # afternoon
        ("by WA", "WhatsApp", datetime(2026, 10, 26, 16, 0)),  # now
        ("uploaded to the OJK portal 24/10 10:00", "Portal", datetime(2026, 10, 24, 10, 0)),
        ("kurir kemarin", "Hardcopy", datetime(2026, 10, 25, 0, 0)),
        ("email 23 Okt pagi 9.00", "Email", datetime(2026, 10, 23, 9, 0)),
    ],
)
def test_how_and_when(text, method, at):
    sent = read(text, NOW)
    assert sent is not None and (sent.method, sent.at.replace(tzinfo=None)) == (method, at)
    assert sent.details == text


@pytest.mark.parametrize("text", ["sudah ke regulator", "email 30 Oct", ""])
def test_no_method_or_a_future_date_is_not_a_submission(text):
    assert read(text, NOW) is None


def test_the_command_words():
    assert parse_command("submitted G-12 hardcopy ke OJK") == Command(
        "submitted", "G-0012", "hardcopy ke OJK"
    )
    assert parse_command("sudah dikirim G-0012 via email") == Command(
        "submitted", "G-0012", "via email"
    )
    assert parse_command("sudah dikirim via portal", has_context=True) == Command(
        "submitted", None, "via portal"
    )


# --- applying it ---------------------------------------------------------------------------------


def setup(columns: bool = True, ai=None):
    data = FakeSheetsBackend.from_fixtures()
    if columns:
        data.tabs["Master"][0] += ["Submission method", "Submitted at"]
        for row in data.tabs["Master"][1:]:
            row += ["", ""]
    for row in data.tabs["Config"]:
        if row and row[0] == "Allowed senders":
            row[1] = "@example.com"
        if row and row[0] == "AI provider" and ai is not None:
            row[1] = "Gemini API key"
    store = FakeSheetStore(data)
    store.load()
    return Services(store=store, drive=FakeDriveStore(), ai=ai or NullProvider()), FakeGmailClient()


def add(services, **values) -> Task:
    base = {"Title": "Stress test", "Owner": "Staff P", "Doer": "Staff P",
            "Status": "Approved", "Needs review": "Yes", "Reviewer": "Staff K",
            "Source ID": "src-1"}  # fmt: skip
    saved, _ = services.store.append_task(Task.model_validate({**base, **values}))
    services.store.load()
    return saved


def now(services, task_id: str) -> Task:
    services.store.load()
    return next(t for t in services.store.tasks() if t.id == task_id)


def submit(services, gmail, task_id: str, how: str, files=(), actor="Staff P"):
    services.store.load()
    person = next(p for p in services.store.staff() if p.name == actor)
    ctx = CommandContext(store=services.store, gmail=gmail, actor=person, channel="Email",
                         files=list(files), message_id=f"m-{how[:6]}")  # fmt: skip
    with clock.freeze(NOW.replace(tzinfo=None)):
        return run_command(Command("submitted", task_id, how), ctx)


def test_a_submission_closes_the_task_writes_how_and_when_and_tells_the_manager():
    services, gmail = setup()
    task = add(services)
    result = submit(services, gmail, task.id, "hardcopy ke OJK hari ini 15:30")
    assert result.changed and "submitted by hardcopy" in result.reply
    saved = now(services, task.id)
    assert (saved.status, saved.submission_method) == ("Done", "Hardcopy")
    assert saved.submitted_at.replace(tzinfo=None) == datetime(2026, 10, 26, 15, 30)
    assert saved.submitted_date.isoformat() == "2026-10-26"
    assert "Submitted 26 Oct 2026 15:30 by Staff P: hardcopy ke OJK" in saved.notes
    [notice] = [e for e, _ in gmail.sent if "staff.k@example.com" in e.to]
    assert "submitted" in notice.subject and "How: Hardcopy" in notice.text


def test_proof_is_optional_but_kept_when_attached():
    services, gmail = setup()
    task = add(services)
    pdf = Attachment(filename="Tanda terima.pdf", mime="application/pdf", data=b"%PDF")
    submit(services, gmail, task.id, "via email", files=[pdf])
    assert "Tanda terima.pdf" in now(services, task.id).link


def test_without_a_method_or_before_approval_nothing_changes():
    services, gmail = setup()
    task = add(services)
    assert "How and when" in submit(services, gmail, task.id, "sudah ya").reply
    other = add(services, **{"Status": "In Progress", "Source ID": "src-2"})
    assert "needs review first" in submit(services, gmail, other.id, "via email").reply
    assert now(services, other.id).status == "In Progress"


def test_without_the_new_columns_it_still_closes_the_task():
    services, gmail = setup(columns=False)
    task = add(services)
    assert submit(services, gmail, task.id, "via portal").changed
    assert now(services, task.id).status == "Done"


def test_support_cannot_submit():
    services, gmail = setup()
    task = add(services, Support="Staff A")
    result = submit(services, gmail, task.id, "via email", actor="Staff A")
    assert not result.changed and "only Staff P" in result.reply


# --- by email and WhatsApp -----------------------------------------------------------------------


def test_by_email_with_the_ai_then_yes():
    ai = FakeAIProvider(reply={"task_id": "G-0001", "intent": "submitted", "progress": None,
                               "note": "hardcopy ke OJK tadi jam 3", "requested_date": None,
                               "reply_to_user": "ok", "confidence": 0.9})  # fmt: skip
    services, gmail = setup(ai=ai)
    task = add(services)
    with clock.freeze(NOW.replace(tzinfo=None)):
        gmail.add("e1", sender_email="staff.p@example.com", subject="Laporan",
                  body_text=f"{task.id} sudah dikirim hardcopy ke OJK tadi jam 3")  # fmt: skip
        process_inbox(gmail, services)
        ask = gmail.replies[-1][1].text
        assert f"{task.id} sudah dikirim (Hardcopy, 26 Oct 2026 15:00)" in ask
        assert "Approved → Done" in ask
        gmail.add("e2", thread_id="thread-e1", sender_email="staff.p@example.com",
                  subject="Re: Laporan", body_text="YES")  # fmt: skip
        process_inbox(gmail, services)
    assert now(services, task.id).status == "Done"


def test_on_whatsapp_the_sudah_dikirim_button_asks_how():
    services, gmail = setup()
    for row in services.store.backend.tabs["Staff"]:
        row.append("WhatsApp opt-in" if row and row[0] == "Staff ID" else "Yes")
    services.store.load()
    task = add(services)
    ctx = WaContext(services=services, wa=FakeWhatsApp(), queue=FakeQueue(), gmail=gmail)
    with clock.freeze(NOW.replace(tzinfo=None)):
        handle_message(WAMessage(id="w1", sender=P_WA, received_at=clock.now(), kind="tap",
                                 tap=f"sub:{task.id}"), ctx)  # fmt: skip
        assert "Bagaimana dan kapan" in ctx.wa.last_text(P_WA)
        handle_message(WAMessage(id="w2", sender=P_WA, received_at=clock.now(), kind="text",
                                 text="hardcopy ke OJK jam 14.00"), ctx)  # fmt: skip
    saved = now(services, task.id)
    assert (saved.status, saved.submission_method) == ("Done", "Hardcopy")


def test_the_preview_line():
    task = Task.model_validate({"ID": "G-0012", "Status": "Approved"})
    with clock.freeze(NOW.replace(tzinfo=None)):
        line = describe(Command("submitted", "G-0012", "via email 15:00"), task)
    assert (
        line == "G-0012 sudah dikirim (Email, 26 Oct 2026 15:00) – Status G-0012: Approved → Done"
    )
