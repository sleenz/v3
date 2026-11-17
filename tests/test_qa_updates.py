"""QA-B: scenario tests that try to break task updates by email and WhatsApp.

Spec: docs/DESIGN.md sections 19, 22 (part C), 24 and D15. Fakes only, made-up people and
numbers. The clock is frozen by conftest at Monday 26 Oct 2026 09:00 Asia/Hong_Kong.

A test marked `xfail(strict=True)` documents a behaviour that contradicts DESIGN.md
(finding QA-B<n>); it starts passing, and so fails the suite, once the code is fixed.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import pytest

from app import clock, outbox
from app.extract.ai import NullProvider
from app.gmail import process_inbox
from app.models import PROGRESS_LOG, SNOOZE, Attachment, Task
from app.pipeline import Services
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

# Timing, thread and process fields every log record has: never message data, and numbers in
# them (e.g. relativeCreated 12365.1555) must not look like a phone number.
_STD = set(logging.LogRecord("x", 0, "", 0, "", (), None).__dict__) - {"msg", "args"}

MONDAY = date(2026, 10, 26)
A_MAIL = "staff.a@example.com"
K_MAIL = "staff.k@example.com"  # Staff K, the Manager
P_MAIL = "staff.p@example.com"
A_WA = "85255500001"
PDF = Attachment(filename="proof.pdf", mime="application/pdf", data=b"%PDF-1.7 fake proof")
DOCX = Attachment(
    filename="draft.docx",
    mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    data=b"PK fake draft",
)


# --- helpers -----------------------------------------------------------------------------------


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


def email(ctx, message_id: str, body: str, subject: str = "Update", sender: str = A_MAIL,
          **extra):  # fmt: skip
    ctx.gmail.add(message_id, sender_email=sender, subject=subject, body_text=body, **extra)
    ctx.store.load()
    return process_inbox(ctx.gmail, ctx.services)


def answer(ctx, message_id: str, body: str, thread: str = "thread-m-1", sender: str = A_MAIL):
    """A reply in an existing thread (YES / NO / anything)."""
    return email(ctx, message_id, body, subject="Re: Update", sender=sender, thread_id=thread)


def requests(ctx) -> list[Task]:
    """Rows logged as requests from email (Source ID = the Gmail message id)."""
    ctx.store.load()
    return [t for t in ctx.store.tasks() if t.source_id.startswith(("m-", "e-"))]


def last_reply(ctx) -> str:
    return ctx.gmail.replies[-1][1].text


def bot_thread(ctx, task_id: str, thread: str = "th-1") -> None:
    """A reminder email about one task that the bot sent earlier (`_outbox`)."""
    outbox.record(ctx.store, "Email", f"sent-{thread}", "Staff A", [task_id], "morning", thread)


def in_thread(ctx, message_id: str, body: str, thread: str = "th-1", sender: str = A_MAIL):
    return email(ctx, message_id, body, subject="Re: [GRC] Your tasks", sender=sender,
                 thread_id=thread)  # fmt: skip


_n = [0]


def wa_text(body: str, context_id: str = "", message_id: str = "") -> WAMessage:
    _n[0] += 1
    return WAMessage(id=message_id or f"wamid.q{_n[0]}", sender=A_WA, received_at=clock.now(),
                     kind="text", text=body, context_id=context_id)  # fmt: skip


def wa_tap(value: str, message_id: str = "") -> WAMessage:
    _n[0] += 1
    return WAMessage(id=message_id or f"wamid.t{_n[0]}", sender=A_WA, received_at=clock.now(),
                     kind="tap", tap=value)  # fmt: skip


def proposal(ctx, body: str = "Hi, G-0001 is at 70% now, waiting for finance.") -> str:
    """Staff A's new email about G-0001 read by the AI as progress 70, waiting for YES / NO.
    Afterwards the fake AI reads everything as `unknown`, so answers are not misread."""
    first = add(ctx, "ATMR report")
    assert first == "G-0001"
    email(ctx, "m-1", body)
    assert "Reply YES to confirm" in last_reply(ctx)
    ctx.services.ai.reply = reading()
    return first


def ai_ctx(**values) -> WaContext:
    return context(FakeAIProvider(reply=reading(**values)), ai_on=True)


# --- 1. a new email with a task ID: propose, nothing before YES, before → after ---------------


@pytest.mark.parametrize(
    ("ai_says", "shown", "check"),
    [
        (
            {"intent": "progress", "progress": 70},
            "Progress G-0001: 40% → 70%",
            lambda t, ctx: t.progress_value == 0.7,
        ),
        (
            {"intent": "blocked", "note": "waiting for finance data"},
            "Status G-0001: In Progress → On Hold (terhambat): waiting for finance data",
            lambda t, ctx: t.status == "On Hold" and "waiting for finance data" in t.notes,
        ),
        (
            {"intent": "extension", "requested_date": "2026-11-06"},
            "Minta G-0001 due 31 Oct 2026 → 06 Nov 2026 (manager yang memutuskan)",
            lambda t, ctx: (
                t.due_date == date(2026, 10, 31)
                and any("extension asked" in e.subject for e, _ in ctx.gmail.sent)
            ),
        ),
        (
            {"intent": "note", "note": "finance data received"},
            "Catatan di G-0001: finance data received",
            lambda t, ctx: "Note 26 Oct 2026 by Staff A: finance data received" in t.notes,
        ),
    ],
    ids=["progress", "blocked", "extension", "note"],
)
def test_new_email_change_waits_for_yes_and_shows_before_and_after(ai_says, shown, check):
    ctx = ai_ctx(**ai_says)
    first = add(ctx, "ATMR report")
    before = task(ctx, first)
    email(ctx, "m-1", f"About {first}: see below, thanks")
    ask = last_reply(ctx)
    assert shown in ask and "new request instead" in ask
    after_ask = task(ctx, first)
    assert (after_ask.progress_value, after_ask.status, after_ask.notes) == (
        before.progress_value,
        before.status,
        before.notes,
    )
    assert "INBOX" in ctx.gmail.labels("m-1") and "UNREAD" not in ctx.gmail.labels("m-1")
    ctx.services.ai.reply = reading()
    answer(ctx, "m-2", "Yes")
    assert check(task(ctx, first), ctx)
    assert "INBOX" not in ctx.gmail.labels("m-1") and not requests(ctx)


def test_new_email_blocked_without_reason_asks_and_changes_nothing():
    ctx = ai_ctx(intent="blocked")
    first = add(ctx, "ATMR report")
    email(ctx, "m-1", f"{first} is stuck")
    assert last_reply(ctx) == f"What is blocking {first}? Reply: blocked {first} <reason>"
    assert task(ctx, first).status == "In Progress" and not requests(ctx)


def test_new_email_done_with_a_proof_then_yes():
    ctx = ai_ctx(intent="done")
    first = add(ctx, "ATMR report")
    email(ctx, "m-1", f"{first} finished, proof attached", attachments=[PDF])
    assert f"{first} selesai – Status {first}: In Progress → Done (file: proof.pdf)" in last_reply(
        ctx
    )
    assert task(ctx, first).status == "In Progress"
    ctx.services.ai.reply = reading()
    answer(ctx, "m-2", "yes")
    saved = task(ctx, first)
    assert saved.status == "Done" and "proof.pdf" in saved.link
    assert saved.submitted_date == MONDAY
    assert [a.filename for a in ctx.gmail.stored[first]] == ["proof.pdf"]


def test_new_email_done_without_a_proof_asks_for_it():
    ctx = ai_ctx(intent="done")
    first = add(ctx, "ATMR report")
    email(ctx, "m-1", f"{first} finished")
    assert last_reply(ctx) == f"Attach the proof and reply: done {first}"
    assert task(ctx, first).status == "In Progress" and not requests(ctx)


def test_new_email_review_with_a_file_then_yes_emails_the_reviewer():
    ctx = ai_ctx(intent="review")
    first = add(ctx, "ATMR report")
    email(ctx, "m-1", f"Please review {first}, draft attached", attachments=[DOCX])
    assert (
        f"Kirim {first} untuk review – Status {first}: In Progress → In Review (file: draft.docx)"
        in last_reply(ctx)
    )
    ctx.services.ai.reply = reading()
    answer(ctx, "m-2", "Yes")
    saved = task(ctx, first)
    assert saved.status == "In Review" and "draft.docx" in saved.review_file
    review = [e for e, _ in ctx.gmail.sent if "review requested" in e.subject]
    assert review and review[0].to == [K_MAIL]
    assert [a.filename for a in review[0].attachments] == ["draft.docx"]


@pytest.mark.parametrize(
    ("sender", "body", "status", "shown"),
    [
        (K_MAIL, "assign G-0001 to Staff P", "In Progress",
         "Serahkan G-0001: doer Staff A → Staff P"),
        (K_MAIL, "approve G-0001", "In Review",
         "Approve G-0001 – Status G-0001: In Review → Approved"),
        (K_MAIL, "return G-0001 fix table 3", "In Review",
         "Kembalikan G-0001 – Status G-0001: In Review → Returned: fix table 3"),
        (A_MAIL, "progress G-0001 10", "Not Started",
         "Progress G-0001: 40% → 10% (status → In Progress)"),
    ],
    ids=["assign", "approve", "return", "not-started"],
)  # fmt: skip
def test_command_confirmations_show_before_and_after(sender, body, status, shown):
    ctx = context()
    add(ctx, "ATMR report", Status=status)
    email(ctx, "m-1", body, sender=sender)
    assert shown in last_reply(ctx)
    assert task(ctx, "G-0001").status == status


# --- 2. YES / NO edge cases ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reply", "progress", "logged"),
    [
        ("yes, thanks", 0.7, 0),
        ("ok", 0.7, 0),
        ("ya", 0.7, 0),
        ("YES!", 0.7, 0),
        (
            "Yes\n\nOn Mon, 26 Oct 2026 at 09:00, GRC Tracker <grc-bot@example.com> wrote:\n"
            "> Reply YES to confirm (nothing has changed yet):\n>   Progress G-0001: 40% → 70%\n"
            "> Reply NO if this is a new request instead: it is then logged as one.",
            0.7,
            0,
        ),
        ("tidak", 0.4, 1),
        ("NO.", 0.4, 1),
        ("Yes but change it to 80", 0.4, 0),
    ],
    ids=["yes-thanks", "ok", "ya", "YES!", "quoted", "tidak", "NO.", "yes-but"],
)
def test_yes_and_no_variants(reply, progress, logged):
    ctx = ai_ctx(intent="progress", progress=70)
    first = proposal(ctx)
    answer(ctx, "m-2", reply)
    assert task(ctx, first).progress_value == progress
    assert len(requests(ctx)) == logged


def test_yes_but_drops_the_proposal_so_a_later_yes_changes_nothing():
    ctx = ai_ctx(intent="progress", progress=70)
    first = proposal(ctx)
    answer(ctx, "m-2", "Yes but change it to 80")
    answer(ctx, "m-3", "Yes")
    assert task(ctx, first).progress_value == 0.4


def test_yes_after_30_minutes_changes_nothing():
    ctx = ai_ctx(intent="progress", progress=70)
    first = proposal(ctx)
    with clock.freeze(datetime(2026, 10, 26, 9, 31)):
        answer(ctx, "m-2", "Yes")
    assert task(ctx, first).progress_value == 0.4


def test_a_late_yes_is_told_that_the_proposal_expired():
    ctx = ai_ctx(intent="progress", progress=70)
    proposal(ctx)
    replies_before = len(ctx.gmail.replies)
    with clock.freeze(datetime(2026, 10, 26, 9, 31)):
        answer(ctx, "m-2", "Yes")
    assert len(ctx.gmail.replies) > replies_before
    assert "30" in last_reply(ctx)


def test_yes_from_a_different_person_does_not_confirm():
    ctx = ai_ctx(intent="progress", progress=70)
    first = proposal(ctx)
    answer(ctx, "m-2", "Yes", sender=P_MAIL)
    assert task(ctx, first).progress_value == 0.4
    answer(ctx, "m-3", "Yes")  # Staff A's own YES still works
    assert task(ctx, first).progress_value == 0.7


def test_a_second_proposal_replaces_the_first():
    ctx = ai_ctx(intent="progress", progress=70)
    first = proposal(ctx)
    second = add(ctx, "Stress test")
    ctx.services.ai.reply = reading(intent="progress", progress=30)
    email(ctx, "m-2", f"{second} is at 30%", subject="Stress test")
    ctx.services.ai.reply = reading()
    answer(ctx, "m-3", "Yes", thread="thread-m-2")
    assert task(ctx, first).progress_value == 0.4
    assert task(ctx, second).progress_value == 0.3


def test_an_email_yes_does_not_confirm_a_whatsapp_proposal():
    ctx = context()
    first = add(ctx, "ATMR report")
    handle_message(wa_text(f"{first} 70"), ctx)
    assert ctx.wa.buttons[-1][1].startswith(f"Progress {first}: 40% → 70%")
    email(ctx, "m-1", "Yes", subject="Re: Update")
    assert task(ctx, first).progress_value == 0.4


def test_decimal_progress_confirmation_matches_what_is_written():
    ctx = context()
    first = add(ctx, "ATMR report")
    bot_thread(ctx, first)
    in_thread(ctx, "e-1", f"{first} 0.5")
    shown = last_reply(ctx)
    in_thread(ctx, "e-2", "Yes")
    written = round(task(ctx, first).progress_value * 100)
    assert f"→ {written}%" in shown


# --- 3. NO on a new-email proposal logs the original as a request, once (D15) -------------------


def test_no_logs_the_original_once_and_archives_it_only_then():
    ctx = ai_ctx(intent="progress", progress=70)
    first = proposal(ctx)
    labels = ctx.gmail.labels("m-1")
    assert "INBOX" in labels and "UNREAD" not in labels and "GRC/Logged" not in labels
    answer(ctx, "m-2", "No")
    [logged] = requests(ctx)
    assert logged.source_id == "m-1" and task(ctx, first).progress_value == 0.4
    assert "GRC/Logged" in ctx.gmail.labels("m-1") and "INBOX" not in ctx.gmail.labels("m-1")
    assert f"Logged your email as a new request: {logged.id}" in last_reply(ctx)
    process_inbox(ctx.gmail, ctx.services)
    ctx.gmail.labels("m-1").add("UNREAD")  # someone marks it unread again
    ctx.gmail.labels("m-1").add("INBOX")
    process_inbox(ctx.gmail, ctx.services)
    assert len(requests(ctx)) == 1


def test_no_logs_the_original_with_its_attachments():
    ctx = ai_ctx(intent="review")
    first = add(ctx, "ATMR report")
    email(ctx, "m-1", f"New: like {first}, please review this policy draft", attachments=[DOCX])
    assert "Reply YES to confirm" in last_reply(ctx)
    ctx.services.ai.reply = reading()
    answer(ctx, "m-2", "NO")
    [logged] = requests(ctx)
    assert logged.source_id == "m-1"
    assert "draft.docx" in ctx.services.drive.only_folder()["files"]
    assert task(ctx, first).status == "In Progress"


def test_a_failure_while_logging_after_no_never_loses_the_request(monkeypatch):
    import app.gmail as gmail_module

    ctx = ai_ctx(intent="progress", progress=70)
    proposal(ctx)
    real = gmail_module.process_request
    calls = []

    def flaky(raw, services):
        calls.append(raw.source_id)
        if len(calls) == 1:
            raise OSError("sheet write failed")
        return real(raw, services)

    monkeypatch.setattr(gmail_module, "process_request", flaky)
    answer(ctx, "m-2", "No")
    assert not requests(ctx)  # the first try failed
    process_inbox(ctx.gmail, ctx.services)  # the retry
    process_inbox(ctx.gmail, ctx.services)
    assert [t.source_id for t in requests(ctx)] == ["m-1"]


def test_no_with_the_words_of_the_confirmation_logs_the_request():
    ctx = ai_ctx(intent="progress", progress=70)
    proposal(ctx)
    answer(ctx, "m-2", "No, this is a new request")
    assert [t.source_id for t in requests(ctx)] == ["m-1"]


# --- 4. requests stay requests (D15) --------------------------------------------------------------

_FORWARDS = {
    "gmail-id": (
        "Fwd: Q3",
        "---------- Pesan yang diteruskan ---------\nDari: Regulator X <x@other.example>\n"
        "Tanggal: Sen, 26 Okt 2026 pukul 08.00\nPerihal: Laporan Q3\nKepada: <grc-bot@example.com>"
        "\n\nG-0001 sudah 70%, mohon laporan Q3 dikirim",
    ),
    "header-id-no-fwd": (
        "Laporan Q3",
        "Dari: Regulator X <x@other.example>\nTanggal: 26 Okt 2026\nPerihal: Laporan Q3\n\n"
        "G-0001 sudah 70%, mohon laporan Q3 dikirim",
    ),
    "outlook": (
        "Q3 report",
        "________________________________\nFrom: Regulator X <x@other.example>\n"
        "Sent: Monday, 26 October 2026 08:00\nTo: GRC\nSubject: Q3 report\n\nG-0001 70%",
    ),
    "original-message": (
        "Q3 report",
        "-----Original Message-----\nFrom: Regulator X\nSent: Monday\nSubject: Q3\n\nG-0001 70%",
    ),
    "apple": (
        "Q3 report",
        "Begin forwarded message:\n\nFrom: Regulator X <x@other.example>\nSubject: Q3 report\n"
        "Date: 26 October 2026 at 08:00:00 HKT\nTo: GRC\n\nG-0001 is at 70%",
    ),
    "fw-subject": ("FW: Q3 report", "G-0001 is at 70%"),
    "tr-subject": ("TR: Q3 report", "G-0001 is at 70%"),
}


@pytest.mark.parametrize("key", list(_FORWARDS))
def test_forwarded_mail_is_a_request(key):
    subject, body = _FORWARDS[key]
    ctx = ai_ctx(intent="progress", progress=70, task_id="G-0001")
    first = add(ctx, "ATMR report")
    email(ctx, "m-1", body, subject=subject)
    assert len(requests(ctx)) == 1 and task(ctx, first).progress_value == 0.4


@pytest.mark.parametrize(
    ("body", "subject"),
    [
        ("PROMPT: Staff P, deadline Friday\n\nG-0001 follow-up: prepare Q4", "Request"),
        ("prompt: reviewer Staff K\nPlease prepare the board pack", "G-0001 board pack"),
    ],
)
def test_a_prompt_line_is_a_request(body, subject):
    ctx = ai_ctx(intent="progress", progress=70, task_id="G-0001")
    add(ctx, "ATMR report")
    email(ctx, "m-1", body, subject=subject)
    assert len(requests(ctx)) == 1


def test_attachments_without_a_task_id_are_a_request_and_skip_the_ai():
    ai = FakeAIProvider(reply=reading(intent="progress", progress=70, task_id="G-0001"))
    ctx = context(ai, ai_on=True)
    add(ctx, "ATMR report")
    email(ctx, "m-1", "The ATMR report is at 70%, file attached", attachments=[PDF])
    assert len(requests(ctx)) == 1 and ai.reply_calls == []


@pytest.mark.parametrize("intent", ["request", "unknown"])
def test_a_new_request_mentioning_an_old_task_id_is_a_request(intent):
    ctx = ai_ctx(intent=intent)
    first = add(ctx, "Q3 stress test", Status="Done", Progress=1)
    email(ctx, "m-1", f"Please prepare the Q4 stress test the same way as {first}", subject="Q4")
    assert len(requests(ctx)) == 1


@pytest.mark.parametrize(
    "ai",
    [
        FakeAIProvider(reply=reading(intent="progress", progress=50, task_id="G-0001",
                                     confidence=0.5)),
        FakeAIProvider(reply=RuntimeError("location not supported")),
        FakeAIProvider(reply="not json at all"),
        FakeAIProvider(reply=reading(intent="progress", progress=50, task_id="G-0099")),
    ],
    ids=["low-confidence", "error", "invalid-json", "unknown-task"],
)  # fmt: skip
def test_without_a_task_id_an_unsure_or_failing_ai_leaves_a_request(ai):
    ctx = context(ai, ai_on=True)
    first = add(ctx, "ATMR report")
    add(ctx, "Stress test")
    email(ctx, "m-1", "The ATMR report is half done, waiting for finance")
    assert len(requests(ctx)) == 1 and task(ctx, first).progress_value == 0.4


def test_with_a_task_id_a_failing_ai_falls_back_to_rules_and_finds_a_request():
    ctx = context(FakeAIProvider(reply=RuntimeError("boom")), ai_on=True)
    first = add(ctx, "ATMR report")
    email(ctx, "m-1", f"Please send the {first} pack to the regulator by Friday")
    assert len(requests(ctx)) == 1 and task(ctx, first).progress_value == 0.4


def test_a_sender_with_no_open_tasks_is_a_request_and_costs_no_ai_call():
    ai = FakeAIProvider(reply=reading(intent="progress", progress=70, task_id="G-0001"))
    ctx = context(ai, ai_on=True)
    add(ctx, "ATMR report")  # Staff A's, not Staff P's
    email(ctx, "m-1", "The report is at 70%", sender=P_MAIL)
    assert len(requests(ctx)) == 1 and ai.reply_calls == []


def test_a_task_id_from_someone_not_in_staff_is_a_request():
    ctx = ai_ctx(intent="progress", progress=70)
    first = add(ctx, "ATMR report")
    email(ctx, "m-1", f"{first} is at 70%", sender="visitor@example.com")
    assert len(requests(ctx)) == 1 and task(ctx, first).progress_value == 0.4


def test_an_email_about_two_tasks_does_not_update_the_wrong_one():
    ctx = ai_ctx(intent="progress", progress=50, task_id="G-0002")
    first = add(ctx, "ATMR report")
    second = add(ctx, "Stress test")
    email(ctx, "m-1", f"{first} is done, see the link. {second} is at 50%.")
    assert f"Progress {first}" not in last_reply(ctx)


def test_a_new_email_starting_with_a_command_word_keeps_the_request_path():
    ctx = context()
    first = add(ctx, "ATMR report")
    email(ctx, "m-1", f"Review {first} again with the new circular and send it by Friday",
          subject="New circular", sender=K_MAIL)  # fmt: skip
    assert "INBOX" in ctx.gmail.labels("m-1")  # stays until YES / NO
    answer(ctx, "m-2", "No", sender=K_MAIL)
    assert [t.source_id for t in requests(ctx)] == ["m-1"]


def test_a_forward_with_a_command_like_top_line_is_a_request():
    ctx = context()
    first = add(ctx, "ATMR report")
    body = (f"review {first} against this please\n\n---------- Forwarded message ---------\n"
            "From: Regulator X <x@other.example>\nDate: Mon, 26 Oct 2026\nSubject: Circular\n\n"
            "New circular text")  # fmt: skip
    email(ctx, "m-1", body, subject="Fwd: Circular", sender=K_MAIL)
    assert len(requests(ctx)) == 1


# --- 5. status ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "typed", ["status g1", "how is G-1?", "gimana G-0001", "cek status G-0001"]
)
@pytest.mark.parametrize(
    ("status", "extra", "line"),
    [
        ("Proposed", {}, "Waiting for the manager's approval."),
        ("On Hold", {"Notes": "Blocked 20 Oct 2026 by Staff A: finance data"},
         "Blocked 20 Oct 2026 by Staff A: finance data"),
        ("Returned", {"Review notes": "fix table 3"}, "Review notes: fix table 3"),
        ("Done", {"Progress": 1}, "progress 100%"),
    ],
)  # fmt: skip
def test_status_by_new_email_for_any_status(typed, status, extra, line):
    ctx = context()
    first = add(ctx, "ATMR report", Status=status, **extra)
    before = task(ctx, first)
    email(ctx, "m-1", typed, subject="status", sender=P_MAIL)  # any Staff member
    reply = last_reply(ctx)
    assert reply.startswith(f"{first} – ATMR report: due Sat 31 Oct 2026 (5 hari lagi)")
    assert f"status {status}" in reply and line in reply
    assert task(ctx, first) == before and not requests(ctx)


def test_status_of_an_unknown_task_says_so_and_logs_nothing():
    ctx = context()
    add(ctx, "ATMR report")
    email(ctx, "m-1", "status G-0099")
    assert last_reply(ctx) == "I can't find G-0099. Nothing changed."
    assert not requests(ctx)


@pytest.mark.parametrize("ai_on", [False, True])
def test_status_alone_in_a_bot_thread(ai_on):
    ctx = ai_ctx(intent="question") if ai_on else context()
    first = add(ctx, "ATMR report")
    bot_thread(ctx, first)
    in_thread(ctx, "e-1", "status")
    assert last_reply(ctx).startswith(f"{first} – ATMR report")


@pytest.mark.parametrize(
    ("typed", "status"),
    [("status g1", "On Hold"), ("gimana G-1", "Proposed"), ("how is G-0001?", "Returned")],
)
def test_status_on_whatsapp(typed, status):
    ctx = context()
    first = add(ctx, "ATMR report", Status=status)
    handle_message(wa_text(typed), ctx)
    assert ctx.wa.last_text(A_WA).startswith(f"{first} – ATMR report")
    assert f"status {status}" in ctx.wa.last_text(A_WA)


def test_status_on_whatsapp_unknown_task():
    ctx = context()
    add(ctx, "ATMR report")
    handle_message(wa_text("status G-0099"), ctx)
    assert ctx.wa.last_text(A_WA) == "I can't find G-0099. Nothing changed."


# --- 6. replies in a bot thread --------------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "shown", "value"),
    [
        ("70%", "Progress G-0001: 40% → 70%", 0.7),
        ("G-0001 70", "Progress G-0001: 40% → 70%", 0.7),
        ("setengah", "Progress G-0001: 40% → 50%", 0.5),
    ],
)
def test_rules_in_a_bot_thread_with_the_ai_off(typed, shown, value):
    ctx = context()
    first = add(ctx, "ATMR report")
    bot_thread(ctx, first)
    in_thread(ctx, "e-1", typed)
    assert shown in last_reply(ctx) and "Reply NO to cancel" in last_reply(ctx)
    assert task(ctx, first).progress_value == 0.4
    in_thread(ctx, "e-2", "Yes")
    assert task(ctx, first).progress_value == value


def test_ai_reading_in_a_bot_thread():
    ctx = ai_ctx(intent="progress", progress=70)
    first = add(ctx, "ATMR report")
    bot_thread(ctx, first)
    in_thread(ctx, "e-1", "udah 70% tinggal nunggu data finance")
    assert "Progress G-0001: 40% → 70%" in last_reply(ctx)
    ctx.services.ai.reply = reading()
    in_thread(ctx, "e-2", "ok")
    assert task(ctx, first).progress_value == 0.7


def test_besok_aja_snoozes_after_yes():
    ctx = context()
    first = add(ctx, "ATMR report")
    bot_thread(ctx, first)
    in_thread(ctx, "e-1", "besok aja")
    assert f"Tanpa pengingat {first}" in last_reply(ctx)
    assert not ctx.store.backend.tabs[SNOOZE][1:]
    in_thread(ctx, "e-2", "Yes")
    assert "until Tue 27 Oct" in last_reply(ctx)
    rows = ctx.store.backend.tabs[SNOOZE][1:]
    assert len(rows) == 1


def test_undo_in_a_bot_thread_restores_the_old_value():
    ctx = context()
    first = add(ctx, "ATMR report", Status="Not Started", Progress="")
    bot_thread(ctx, first)
    in_thread(ctx, "e-1", "70%")
    in_thread(ctx, "e-2", "Yes")
    assert task(ctx, first).status == "In Progress"
    in_thread(ctx, "e-3", "undo")
    assert f"Batalkan perubahan terakhir Anda di {first}" in last_reply(ctx)
    in_thread(ctx, "e-4", "Yes")
    saved = task(ctx, first)
    assert saved.status == "Not Started" and not saved.progress_value


def test_a_plain_reply_in_a_bot_thread_goes_to_notes_never_a_request():
    ctx = context()
    first = add(ctx, "ATMR report")
    bot_thread(ctx, first)
    in_thread(ctx, "e-1", "Thanks for the reminder, will finish this week")
    assert not requests(ctx)
    assert "will finish this week" in task(ctx, first).notes


# --- 7. WhatsApp typed updates -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer_text", "value", "outcome"),
    [("ya", 0.7, "confirmed_progress"), ("Ok.", 0.7, "confirmed_progress"),
     ("Batal", 0.4, "confirm_cancelled"), ("tidak", 0.4, "confirm_cancelled")],
)  # fmt: skip
def test_whatsapp_typed_answers_to_a_proposal(answer_text, value, outcome):
    ctx = context()
    first = add(ctx, "ATMR report")
    assert handle_message(wa_text(f"{first} 70"), ctx) == "confirm_progress"
    assert task(ctx, first).progress_value == 0.4
    assert handle_message(wa_text(answer_text), ctx) == outcome
    assert task(ctx, first).progress_value == value


def test_whatsapp_reply_to_a_card_uses_the_card_task_over_the_ai():
    ctx = ai_ctx(intent="progress", progress=70, task_id="G-0002")
    first = add(ctx, "ATMR report")
    add(ctx, "Stress test")
    handle_message(wa_tap(f"card:{first}"), ctx)
    card_id = f"btn-{ctx.wa.sent_count}"
    assert handle_message(wa_text("udah 70%", context_id=card_id), ctx) == "confirm_progress"
    assert ctx.wa.buttons[-1][1].startswith(f"Progress {first}: 40% → 70%")


def test_whatsapp_two_open_tasks_give_a_list_then_the_card_reply_proposes():
    ctx = ai_ctx(intent="progress", progress=70)
    first = add(ctx, "ATMR report")
    add(ctx, "Stress test")
    assert handle_message(wa_text("udah 70%"), ctx) == "reply_pick"
    assert len(ctx.wa.lists[-1][2]) == 2
    handle_message(wa_tap(f"card:{first}"), ctx)
    card_id = f"btn-{ctx.wa.sent_count}"
    handle_message(wa_text("udah 70%", context_id=card_id), ctx)
    handle_message(wa_tap("cf:yes"), ctx)
    assert task(ctx, first).progress_value == 0.7


def test_whatsapp_a_new_change_replaces_the_pending_one():
    ctx = context()
    first = add(ctx, "ATMR report")
    second = add(ctx, "Stress test")
    handle_message(wa_text(f"{first} 70"), ctx)
    handle_message(wa_text(f"{second} 30"), ctx)
    handle_message(wa_tap("cf:yes"), ctx)
    assert task(ctx, first).progress_value == 0.4
    assert task(ctx, second).progress_value == 0.3


def test_whatsapp_ubah_then_the_right_value():
    ctx = context()
    first = add(ctx, "ATMR report")
    handle_message(wa_text(f"{first} 70"), ctx)
    assert handle_message(wa_tap("cf:edit"), ctx) == "confirm_edit"
    handle_message(wa_text(f"{first} 80"), ctx)
    handle_message(wa_text("ya"), ctx)
    assert task(ctx, first).progress_value == 0.8


# --- 8. duplicate delivery ------------------------------------------------------------------------


def test_whatsapp_duplicate_typed_change_after_ya_is_not_applied_again():
    ctx = context()
    first = add(ctx, "ATMR report")
    handle_message(wa_text(f"{first} 70", message_id="wamid.dup1"), ctx)
    handle_message(wa_tap("cf:yes"), ctx)
    handle_message(wa_tap(f"prog:{first}:90"), ctx)
    assert task(ctx, first).progress_value == 0.9
    handle_message(wa_text(f"{first} 70", message_id="wamid.dup1"), ctx)  # Meta resends
    handle_message(wa_tap("cf:yes"), ctx)
    assert task(ctx, first).progress_value == 0.9


def test_whatsapp_duplicate_tap_is_applied_once():
    ctx = context()
    first = add(ctx, "ATMR report")
    handle_message(wa_tap(f"prog:{first}:50", message_id="wamid.tap1"), ctx)
    handle_message(wa_tap(f"prog:{first}:90", message_id="wamid.tap2"), ctx)
    handle_message(wa_tap(f"prog:{first}:50", message_id="wamid.tap1"), ctx)
    assert task(ctx, first).progress_value == 0.9


def test_whatsapp_duplicate_ya_does_not_confirm_a_newer_proposal():
    ctx = context()
    first = add(ctx, "ATMR report")
    handle_message(wa_text(f"{first} 70"), ctx)
    handle_message(wa_text("ya", message_id="wamid.ya1"), ctx)
    assert task(ctx, first).progress_value == 0.7
    handle_message(wa_text(f"{first} 90"), ctx)  # a new proposal, not answered yet
    handle_message(wa_text("ya", message_id="wamid.ya1"), ctx)  # Meta resends the old "ya"
    assert task(ctx, first).progress_value == 0.7


def test_gmail_yes_processed_twice_applies_once():
    ctx = ai_ctx(intent="note", note="finance data received")
    first = add(ctx, "ATMR report")
    email(ctx, "m-1", f"{first}: finance data received")
    ctx.services.ai.reply = reading()
    ctx.gmail.fail_labelling = True  # the YES stays unread and is read again
    answer(ctx, "m-2", "Yes")
    process_inbox(ctx.gmail, ctx.services)
    ctx.gmail.fail_labelling = False
    assert task(ctx, first).notes.count("Note 26 Oct 2026 by Staff A") == 1


def test_gmail_same_update_email_twice_applies_once():
    ctx = ai_ctx(intent="note", note="finance data received")
    first = add(ctx, "ATMR report")
    email(ctx, "m-1", f"{first}: finance data received")
    answer(ctx, "m-2", "Yes")
    ctx.gmail.labels("m-1").update({"UNREAD", "INBOX"})  # delivered / marked unread again
    process_inbox(ctx.gmail, ctx.services)
    ctx.services.ai.reply = reading()
    answer(ctx, "m-3", "Yes")
    assert task(ctx, first).notes.count("Note 26 Oct 2026 by Staff A") == 1
    assert ctx.store.backend.column(PROGRESS_LOG, "Message ID").count("m-1") == 1


# --- 9. privacy ----------------------------------------------------------------------------------


def test_the_ai_gets_masked_text_for_a_new_email():
    ai = FakeAIProvider(reply=reading(intent="progress", progress=70))
    ctx = context(ai, ai_on=True)
    first = add(ctx, "ATMR report")
    email(ctx, "m-1", f"{first} is at 70%, call me on +852 5550 0099 if needed")
    assert ai.reply_calls and "5550 0099" not in ai.reply_calls[0]


def test_updates_log_no_text_names_emails_or_numbers(caplog):
    caplog.set_level(logging.DEBUG)
    ctx = ai_ctx(intent="blocked", note="waiting for the confidential finance data")
    first = add(ctx, "ATMR report")
    email(ctx, "m-1", f"{first} blocked: waiting for the confidential finance data",
          subject="Secret subject line")  # fmt: skip
    ctx.services.ai.reply = reading()
    answer(ctx, "m-2", "Yes")
    email(ctx, "m-3", f"status {first}")
    handle_message(wa_text(f"{first} 70"), ctx)
    handle_message(wa_text("ya"), ctx)
    secrets = ["confidential", "Secret subject", A_MAIL, "Staff A", "Staff K", K_MAIL,
               A_WA, "5550 0001", "ATMR report"]  # fmt: skip
    leaks = []
    for record in caplog.records:
        blob = " ".join(str(value) for key, value in record.__dict__.items() if key not in _STD)
        leaks += [(record.getMessage(), s) for s in secrets if s in blob]
    assert not leaks
