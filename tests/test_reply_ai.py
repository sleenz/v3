"""Typed replies: AI first, rules as fallback, confirmation before any change (DESIGN 22 part C).

Fakes only: the AI is `FakeAIProvider(reply=...)`. Monday 26 Oct 2026, 09:00 (working hours).
"""

from datetime import date, timedelta

import pytest

from app import clock
from app.extract.ai import NullProvider
from app.extract.reply import PROMPT_PATH, ReplyReading, TaskLine, read_reply
from app.gmail import process_inbox
from app.models import Task
from app.pipeline import Services
from app.replies import understand
from app.whatsapp import WaContext, WAMedia, WAMessage, handle_message
from tests.fakes import (
    FakeAIProvider,
    FakeDriveStore,
    FakeGmailClient,
    FakeQueue,
    FakeSheetsBackend,
    FakeSheetStore,
    FakeWhatsApp,
)

A = "85255500001"  # Staff A
MONDAY = date(2026, 10, 26)


def reading(**values) -> dict:
    base = {"task_id": None, "intent": "unknown", "progress": None, "note": None,
            "requested_date": None, "reply_to_user": "ok", "confidence": 0.9}  # fmt: skip
    return {**base, **values}


def context(ai=None, ai_on: bool = True) -> WaContext:
    backend = FakeSheetsBackend.from_fixtures()
    for row in backend.tabs["Config"]:
        if row[0] == "AI provider":
            row[1] = "Gemini API key" if ai_on else "Off"
    store = FakeSheetStore(backend)
    store.load()
    services = Services(store=store, drive=FakeDriveStore(), ai=ai or NullProvider())
    return WaContext(services=services, wa=FakeWhatsApp({"m-file": b"%PDF"}), queue=FakeQueue(),
                     gmail=FakeGmailClient())  # fmt: skip


def add(ctx: WaContext, title: str, days: int, **values) -> str:
    base = {"Title": title, "Owner": "Staff A", "Doer": "Staff A",
            "Due date": MONDAY + timedelta(days=days), "Status": "In Progress",
            "Source ID": f"src-{title}"}  # fmt: skip
    saved, _ = ctx.store.append_task(Task.model_validate({**base, **values}))
    ctx.store.load()
    return saved.id


def task(ctx: WaContext, task_id: str) -> Task:
    ctx.store.load()
    return next(t for t in ctx.store.tasks() if t.id == task_id)


_n = [0]


def text(body: str, context_id: str = "", media=()) -> WAMessage:
    _n[0] += 1
    return WAMessage(id=f"wamid.r{_n[0]}", sender=A, received_at=clock.now(),
                     kind="media" if media else "text", text=body, context_id=context_id,
                     media=list(media))  # fmt: skip


def tap(value: str) -> WAMessage:
    _n[0] += 1
    return WAMessage(id=f"wamid.t{_n[0]}", sender=A, received_at=clock.now(), kind="tap",
                     tap=value)  # fmt: skip


def staff_a(ctx: WaContext):
    return next(p for p in ctx.store.staff() if p.name == "Staff A")


# --- the prompt -----------------------------------------------------------------------------


def test_the_prompt_has_at_least_ten_examples_and_every_intent():
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    examples = [line for line in prompt.splitlines() if line[:3].strip().rstrip(".").isdigit()]
    assert len(examples) >= 10
    for phrase in ("udah 70% tinggal nunggu data finance", "G-0012 done, file terlampir",
                   "minta mundur ke 20 Nov", "besok aja ingetin", "lagi stuck nunggu tanda tangan",
                   "setengah jalan", "kapan deadline G-0012?"):  # fmt: skip
        assert phrase in prompt


# --- every example through a fake AI ---------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "linked", "ai_says", "kind", "command"),
    [
        ("udah 70% tinggal nunggu data finance", "first",
         {"intent": "progress", "progress": 70, "note": "tinggal nunggu data finance"},
         "change", ("progress", "70")),
        ("{first} done, file terlampir", None, {"intent": "done", "task_id": "{first}"},
         "change", ("done", "")),
        ("minta mundur ke 20 Nov", "first",
         {"intent": "extension", "requested_date": "2026-11-20"}, "change",
         ("extension", "2026-11-20")),
        ("besok aja ingetin", "first", {"intent": "snooze", "requested_date": "2026-10-27"},
         "change", ("snooze", "2026-10-27")),
        ("lagi stuck nunggu tanda tangan", "first",
         {"intent": "blocked", "note": "nunggu tanda tangan"}, "change",
         ("blocked", "nunggu tanda tangan")),
        ("setengah jalan", "first", {"intent": "progress", "progress": 50}, "change",
         ("progress", "50")),
        ("kapan deadline {first}?", None, {"intent": "question", "task_id": "{first}"},
         "question", None),
        ("please send {second} for review", None, {"intent": "review", "task_id": "{second}"},
         "change", ("review", "")),
        ("the manpower one is about 30% done", None,
         {"intent": "progress", "progress": 30, "task_id": "{second}", "confidence": 0.75},
         "change", ("progress", "30")),
        ("tugas saya apa aja?", None, {"intent": "list"}, "list", None),
        ("FYI finance said the data comes Thursday", "first",
         {"intent": "note", "note": "finance said the data comes Thursday"}, "change",
         ("note", "finance said the data comes Thursday")),
        ("Mohon disiapkan laporan stress test Q3 paling lambat 25 Nov", None,
         {"intent": "request"}, "request", None),
        ("ok", "first", {"intent": "unknown", "confidence": 0.9}, "nothing", None),
    ],
)  # fmt: skip
def test_every_prompt_example(message, linked, ai_says, kind, command):
    ctx = context()
    first = add(ctx, "ATMR report", 3, Link="")
    second = add(ctx, "Manpower form", 9)
    fill = {"first": first, "second": second}
    says = {k: v.format(**fill) if isinstance(v, str) else v for k, v in ai_says.items()}
    ai = FakeAIProvider(reply=reading(**says))
    mine = [task(ctx, first), task(ctx, second)]
    files = message.startswith("{first} done")
    found = understand(message.format(**fill), staff_a(ctx), mine,
                       fill[linked] if linked else None, ai, True, False, MONDAY,
                       has_files=files)  # fmt: skip
    assert found.kind == kind and found.via == "ai"
    if command is not None:
        assert (found.command.name, found.command.value) == command
        assert found.command.task_id in (first, second)
    assert len(ai.reply_calls) == 1


# --- fallback to the rules --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ai", "ai_on"),
    [
        (NullProvider(), True),  # no AI configured
        (FakeAIProvider(reply=reading(intent="done")), False),  # Config AI provider = Off
        (FakeAIProvider(reply=TimeoutError()), True),  # fails (timeout)
        (FakeAIProvider(reply="not json at all"), True),  # invalid JSON
        (FakeAIProvider(reply=reading(intent="done", confidence=0.4)), True),  # unsure
    ],
)
def test_the_rules_take_over(ai, ai_on):
    ctx = context()
    first = add(ctx, "ATMR report", 3)
    found = understand(f"progress {first} 70", staff_a(ctx), [task(ctx, first)], None, ai,
                       ai_on, False, MONDAY)  # fmt: skip
    assert found.via == "rules" and found.kind == "change"
    got = (found.command.name, found.command.task_id, found.command.value)
    assert got == ("progress", first, "70")
    assert understand("setengah jalan", staff_a(ctx), [task(ctx, first)], None, ai, ai_on,
                      False, MONDAY).command.value == "50"  # fmt: skip


def test_a_slow_ai_times_out_and_is_retried_once():
    ai = FakeAIProvider(reply=reading(intent="list"), delay=0.3)
    lines = [TaskLine(id="G-0001", title="x")]
    assert read_reply(ai, "tugas saya", "Staff", lines, None, MONDAY, timeout=0.05) is None
    assert len(ai.reply_calls) == 2


def test_the_ai_cannot_invent_a_task_or_a_past_date():
    ai = FakeAIProvider(reply=reading(intent="extension", task_id="G-9999",
                                      requested_date="2026-10-01"))  # fmt: skip
    lines = [TaskLine(id="G-0001", title="x")]
    found = read_reply(ai, "mundur ya", "Staff", lines, None, MONDAY)
    assert isinstance(found, ReplyReading)
    assert (found.task_id, found.requested_date) == (None, None)


def test_the_ai_sees_masked_text_only(caplog):
    import logging

    from app.logs import JsonFormatter

    ctx = context()
    first = add(ctx, "ATMR report", 3)
    ai = FakeAIProvider(reply=reading(intent="note", note="call me"))
    with caplog.at_level(logging.DEBUG, logger="grc"):
        understand("call me on +852 9123 4567 please", staff_a(ctx), [task(ctx, first)], first,
                   ai, True, False, MONDAY)  # fmt: skip
    assert "9123 4567" not in ai.reply_calls[0] and "[phone]" in ai.reply_calls[0]
    records = [r for r in caplog.records if r.getMessage() == "ai_reply_call"]
    assert records and records[0].prompt_version == "reply_v1"
    assert records[0].confidence == 0.9 and records[0].fallback is False
    formatted = "".join(JsonFormatter().format(r) for r in caplog.records)
    assert "call me" not in formatted and "9123" not in formatted


# --- confirmation on WhatsApp ------------------------------------------------------------------


def test_ya_writes_batal_writes_nothing_ubah_asks_again():
    ai = FakeAIProvider(reply=reading(intent="progress", progress=70))
    ctx = context(ai)
    first = add(ctx, "ATMR report", 3)
    assert handle_message(text("udah 70% tinggal nunggu data finance"), ctx) == "confirm_progress"
    assert task(ctx, first).progress_value is None
    assert handle_message(tap("cf:no"), ctx) == "confirm_cancelled"
    assert task(ctx, first).progress_value is None
    handle_message(text("udah 70%"), ctx)
    assert handle_message(tap("cf:edit"), ctx) == "confirm_edit"
    assert "Ketik perubahan yang benar" in ctx.wa.last_text(A)
    handle_message(text("udah 70%"), ctx)
    assert handle_message(tap("cf:yes"), ctx) == "confirmed_progress"
    assert task(ctx, first).progress_value == 0.7
    assert handle_message(tap("cf:yes"), ctx) == "confirm_none"  # nothing left to confirm


def test_a_new_message_replaces_the_pending_change():
    ctx = context()
    first = add(ctx, "ATMR report", 3)
    handle_message(text(f"progress {first} 40"), ctx)
    handle_message(text(f"progress {first} 60"), ctx)
    handle_message(text("ya"), ctx)
    assert task(ctx, first).progress_value == 0.6


def test_done_with_a_file_keeps_the_file_until_ya():
    ai = FakeAIProvider(reply=reading(intent="done"))
    ctx = context(ai)
    first = add(ctx, "ATMR report", 3)
    proof = WAMedia(id="m-file", filename="bukti.pdf", mime="application/pdf")
    assert handle_message(text(f"{first} done, file terlampir", media=[proof]), ctx) == (
        "confirm_done"
    )
    assert "file: bukti.pdf" in ctx.wa.buttons[-1][1]
    handle_message(tap("cf:yes"), ctx)
    saved = task(ctx, first)
    assert saved.status == "Done" and "bukti.pdf" in saved.link


def test_button_taps_never_call_the_ai():
    ai = FakeAIProvider(reply=reading(intent="progress", progress=10))
    ctx = context(ai)
    first = add(ctx, "ATMR report", 3)
    handle_message(tap(f"prog:{first}:50"), ctx)
    handle_message(tap("view:morning:2026-10-26"), ctx)
    assert ai.reply_calls == []
    assert task(ctx, first).progress_value == 0.5  # a tap saves directly


def test_a_question_is_answered_from_the_sheet_without_confirmation():
    ai = FakeAIProvider(reply=reading(intent="question", task_id=None))
    ctx = context(ai)
    first = add(ctx, "ATMR report", 3)
    assert handle_message(text(f"kapan deadline {first}?"), ctx) == "reply_question"
    answer = ctx.wa.last_text(A)
    assert answer.startswith(f"{first} – ATMR report: due Thu 29 Oct 2026 (3 hari lagi)")
    assert ctx.wa.buttons == []


# --- mapping ----------------------------------------------------------------------------------


def test_one_open_task_is_used_several_give_a_list():
    ai = FakeAIProvider(reply=reading(intent="progress", progress=40))
    ctx = context(ai)
    first = add(ctx, "ATMR report", 3)
    handle_message(text("udah 40%"), ctx)
    assert ctx.wa.buttons[-1][1].startswith(f"Progress {first} → 40%")
    handle_message(tap("cf:no"), ctx)
    add(ctx, "Manpower form", 9)
    assert handle_message(text("udah 40%"), ctx) == "reply_pick"
    _, body, rows = ctx.wa.lists[-1]
    assert body.startswith("Tugas yang mana?") and len(rows) == 2


def test_a_forwarded_message_or_an_ai_request_is_a_new_request():
    ctx = context(FakeAIProvider(reply=reading(intent="request")))
    add(ctx, "ATMR report", 3)
    assert handle_message(text("Mohon disiapkan laporan stress test Q3"), ctx) == "draft_opened"
    forwarded = text("Mohon kirim laporan likuiditas")
    forwarded.forwarded = True
    assert handle_message(forwarded, ctx) in ("draft_opened", "draft_joined")


def test_with_the_ai_off_plain_text_stays_intake_unless_it_replies_to_the_bot():
    ctx = context(ai_on=False)
    first = add(ctx, "ATMR report", 3)
    assert handle_message(text("Mohon kirim laporan stress test"), ctx) == "draft_opened"
    ctx2 = context(ai_on=False)
    first = add(ctx2, "ATMR report", 3)
    handle_message(tap(f"card:{first}"), ctx2)
    assert handle_message(text("hmm gimana ya", context_id="btn-1"), ctx2) == (
        "reply_not_understood"
    )


# --- email --------------------------------------------------------------------------------------


def test_email_reply_in_a_reminder_thread_read_by_the_ai_then_yes():
    ai = FakeAIProvider(reply=reading(intent="blocked", note="waiting for finance"))
    ctx = context(ai)
    first = add(ctx, "ATMR report", 3)
    gmail = ctx.gmail
    from app import outbox

    outbox.record(ctx.store, "Email", "sent-x", "Staff A", [first], "morning", "th-1")
    body = "still stuck, waiting for finance"
    gmail.add("e1", thread_id="th-1", sender_email="staff.a@example.com",
              subject="Re: [GRC] Your tasks", body_text=body)  # fmt: skip
    for row in ctx.store.backend.tabs["Config"]:
        if row[0] == "Allowed senders":
            row[1] = "@example.com"
    ctx.store.load()
    process_inbox(gmail, ctx.services)
    assert "Reply YES to confirm" in gmail.replies[-1][1].text
    assert f"{first} terhambat (On Hold): waiting for finance" in gmail.replies[-1][1].text
    assert task(ctx, first).status == "In Progress"
    gmail.add("e2", thread_id="th-1", sender_email="staff.a@example.com",
              subject="Re: [GRC] Your tasks", body_text="Yes")  # fmt: skip
    process_inbox(gmail, ctx.services)
    assert task(ctx, first).status == "On Hold"
    gmail.add("e3", thread_id="th-1", sender_email="staff.a@example.com",
              subject="Re: [GRC] Your tasks", body_text="NO")  # fmt: skip
    process_inbox(gmail, ctx.services)  # no open question: just a reply, nothing changes
    assert task(ctx, first).status == "On Hold"
