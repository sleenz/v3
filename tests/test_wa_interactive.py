"""WhatsApp buttons, lists and questions; ProgressLog, extension, snooze, undo (DESIGN 22 part B).

Fakes only. The clock is frozen on Monday 26 Oct 2026, 09:00 (inside the fixture's working
hours, so notices go at once).
"""

from datetime import date, datetime, timedelta

from app import clock
from app.commands import parse_command
from app.extract.ai import NullProvider
from app.gmail import process_inbox
from app.lifecycle import CommandContext, run_command
from app.models import (
    CONVERSATIONS,
    PROGRESS_LOG,
    WA_WINDOW,
    Conversation,
    ProgressRow,
    ReminderSent,
    SnoozeRow,
    Task,
    WaWindowRow,
)
from app.pipeline import Services
from app.schedule import run_reminders
from app.wa_ui import notify_whatsapp, parse_tap
from app.whatsapp import WaContext, WAMedia, WAMessage, handle_message, parse_webhook
from tests.fakes import (
    FakeDriveStore,
    FakeGmailClient,
    FakeQueue,
    FakeSheetsBackend,
    FakeSheetStore,
    FakeWhatsApp,
)

A = "85255500001"  # Staff A
P = "85255500016"  # Staff P
MONDAY = date(2026, 10, 26)
SHEET = "https://docs.google.com/spreadsheets/d/sheet-1"


def now() -> datetime:
    return clock.now()


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


def context() -> WaContext:
    backend = FakeSheetsBackend.from_fixtures()
    set_staff(backend, "Staff K", WhatsApp="+852 5550 0011", Role="Manager",
              **{"WhatsApp opt-in": "Yes"})  # fmt: skip
    set_staff(backend, "Staff A", **{"WhatsApp opt-in": "Yes"})
    store = FakeSheetStore(backend)
    store.load()
    services = Services(store=store, drive=FakeDriveStore(), ai=NullProvider())
    return WaContext(services=services, wa=FakeWhatsApp({"m-proof": b"%PDF proof"}),
                     queue=FakeQueue(), gmail=FakeGmailClient(), sheet_url=SHEET)  # fmt: skip


def add(ctx: WaContext, title: str, due: date, **values) -> str:
    base = {"Title": title, "Owner": "Staff A", "Doer": "Staff A", "Due date": due,
            "Status": "In Progress", "Source ID": f"src-{title}"}  # fmt: skip
    saved, _ = ctx.store.append_task(Task.model_validate({**base, **values}))
    ctx.store.load()
    return saved.id


def task(ctx: WaContext, task_id: str) -> Task:
    ctx.store.load()
    return next(t for t in ctx.store.tasks() if t.id == task_id)


_count = [0]


def tap(value: str, sender: str = A, context_id: str = "") -> WAMessage:
    _count[0] += 1
    return WAMessage(id=f"wamid.tap{_count[0]}", sender=sender, received_at=now(), kind="tap",
                     tap=value, context_id=context_id)  # fmt: skip


def text(body: str, sender: str = A, context_id: str = "", message_id: str = "") -> WAMessage:
    _count[0] += 1
    return WAMessage(id=message_id or f"wamid.text{_count[0]}", sender=sender,
                     received_at=now(), kind="text", text=body, context_id=context_id)  # fmt: skip


def log(ctx: WaContext) -> list[ProgressRow]:
    return ctx.store.service_rows(PROGRESS_LOG, ProgressRow)


# --- webhook --------------------------------------------------------------------------------


def test_webhook_reads_taps_and_forwarded_messages():
    value = {
        "metadata": {"phone_number_id": "PNID"},
        "messages": [
            {"id": "w1", "from": A, "timestamp": "1793000000", "type": "button",
             "context": {"from": "PNID", "id": "wamid.template"},
             "button": {"payload": "view:morning:2026-10-26", "text": "Lihat tugas"}},
            {"id": "w2", "from": A, "timestamp": "1793000001", "type": "interactive",
             "context": {"id": "wamid.card"},
             "interactive": {"type": "button_reply",
                             "button_reply": {"id": "upd:G-0012", "title": "Update progres"}}},
            {"id": "w3", "from": A, "timestamp": "1793000002", "type": "interactive",
             "interactive": {"type": "list_reply",
                             "list_reply": {"id": "prog:G-0012:50", "title": "50%"}}},
            {"id": "w4", "from": A, "timestamp": "1793000003", "type": "text",
             "context": {"forwarded": True}, "text": {"body": "Mohon kirim laporan"}},
        ],
    }  # fmt: skip
    first, second, third, fourth = parse_webhook({"entry": [{"changes": [{"value": value}]}]},
                                                 "PNID")  # fmt: skip
    assert (first.kind, first.tap, first.context_id) == ("tap", "view:morning:2026-10-26",
                                                        "wamid.template")  # fmt: skip
    assert (second.tap, second.context_id) == ("upd:G-0012", "wamid.card")
    assert third.tap == "prog:G-0012:50"
    assert fourth.kind == "text" and fourth.forwarded
    assert parse_tap("prog:G-0012:50") == ("prog", "G-0012", "50")
    assert parse_tap("view:task:G-0012") == ("view", "task", "G-0012")


# --- Lihat tugas and cards ------------------------------------------------------------------


def test_lihat_tugas_sends_up_to_five_cards_then_a_list():
    ctx = context()
    ids = [add(ctx, f"t{i}", MONDAY + timedelta(days=i)) for i in range(7)]
    review = add(ctx, "needs review", MONDAY + timedelta(days=9), **{"Needs review": "Yes"})
    outcome = handle_message(tap("view:morning:2026-10-26"), ctx)
    assert outcome == "tap_view"
    cards = [b for b in ctx.wa.buttons if b[0] == A]
    assert len(cards) == 5
    _, body, buttons = cards[0]
    assert body.startswith(f"{ids[0]} – t0, due 26 Oct 2026 (hari ini), progress 0%")
    assert [title for _, title in buttons] == ["Update progres", "Selesai", "Lainnya"]
    assert buttons[0][0] == f"upd:{ids[0]}"
    [(_, _, rows)] = ctx.wa.lists
    assert [r[0] for r in rows] == [f"card:{ids[5]}", f"card:{ids[6]}", f"card:{review}"]
    handle_message(tap(f"card:{review}"), ctx)
    assert [t for _, t in ctx.wa.buttons[-1][2]] == ["Update progres", "Kirim review", "Lainnya"]


def test_the_reviewer_gets_approve_and_return():
    ctx = context()
    task_id = add(ctx, "in review", MONDAY + timedelta(days=3), Status="In Review",
                  Doer="Staff P", Owner="Staff P", Reviewer="Staff A")  # fmt: skip
    handle_message(tap("view:morning:2026-10-26"), ctx)
    [(_, _, buttons)] = [b for b in ctx.wa.buttons if task_id in b[1]]
    assert buttons == [(f"apv:{task_id}", "Approve"), (f"ret:{task_id}", "Return")]
    assert handle_message(tap(f"apv:{task_id}"), ctx) == "tap_approve"
    assert task(ctx, task_id).status == "Approved"


# --- progress -------------------------------------------------------------------------------


def test_update_progres_list_then_save_with_progress_log_and_dedupe():
    ctx = context()
    task_id = add(ctx, "work", MONDAY + timedelta(days=3), Status="Not Started")
    assert handle_message(tap(f"upd:{task_id}"), ctx) == "tap_progress_list"
    [(_, _, rows)] = ctx.wa.lists
    assert [r[1] for r in rows] == ["25%", "50%", "75%", "90%", "100%"]
    choice = tap(rows[1][0])
    assert handle_message(choice, ctx) == "tap_progress"
    saved = task(ctx, task_id)
    assert (saved.progress_value, saved.status) == (0.5, "In Progress")
    fields = {(r.field, r.old_value, r.new_value, r.channel, r.message_id) for r in log(ctx)}
    assert ("Progress", "", "0.5", "WhatsApp", choice.id) in fields
    assert ("Status", "Not Started", "In Progress", "WhatsApp", choice.id) in fields
    assert "progress 50%" in ctx.wa.last_text(A)
    handle_message(choice, ctx)  # Meta delivered the same tap twice
    assert ctx.wa.last_text(A) == "Already done."
    assert len([r for r in log(ctx) if r.field == "Progress"]) == 1


def test_a_typed_70_percent_in_reply_to_a_card():
    ctx = context()
    task_id = add(ctx, "work", MONDAY + timedelta(days=3))
    handle_message(tap(f"card:{task_id}"), ctx)
    card_id = "btn-1"
    assert handle_message(text("70%", context_id=card_id), ctx) == "confirm_progress"
    _, body, buttons = ctx.wa.buttons[-1]
    assert body == f"Progress {task_id} → 70%\nBenar?"
    assert [t for _, t in buttons] == ["Ya", "Ubah", "Batal"]
    assert task(ctx, task_id).progress_value is None  # nothing written before Ya
    assert handle_message(tap("cf:yes"), ctx) == "confirmed_progress"
    assert task(ctx, task_id).progress_value == 0.7


def test_only_doer_owner_or_manager_may_update():
    ctx = context()
    task_id = add(ctx, "not yours", MONDAY + timedelta(days=3), Owner="Staff K", Doer="Staff K")
    handle_message(tap(f"prog:{task_id}:50"), ctx)
    assert "not the Owner or Doer" in ctx.wa.last_text(A)
    assert task(ctx, task_id).progress_value in (None, 0)
    handle_message(tap(f"prog:{task_id}:50", sender="85255500011"), ctx)  # the Manager
    assert task(ctx, task_id).progress_value == 0.5


# --- Lainnya: blocked, extension, snooze, done ------------------------------------------------


def test_blocked_asks_the_reason_then_puts_it_on_hold_and_tells_owner_and_manager():
    ctx = context()
    task_id = add(ctx, "stuck", MONDAY + timedelta(days=3), Doer="Staff P", Owner="Staff A")
    handle_message(
        tap(f"more:{task_id}"),
        ctx,
    )
    assert [r[1] for r in ctx.wa.lists[-1][2]] == ["Selesai", "Terhambat", "Minta perpanjangan",
                                                   "Ingatkan besok", "Lihat file"]  # fmt: skip
    assert handle_message(tap(f"blk:{task_id}", sender=P), ctx) == "tap_ask_blocked_reason"
    assert "Apa yang menghambat" in ctx.wa.last_text(P)
    handle_message(text("nunggu tanda tangan direksi", sender=P), ctx)
    saved = task(ctx, task_id)
    assert saved.status == "On Hold" and "nunggu tanda tangan direksi" in saved.notes
    assert any(r.note.startswith("Blocked: nunggu") for r in log(ctx))
    # Owner (Staff A) and Manager (Staff K) are on WhatsApp: told there, not by email.
    told = {to for to, body in ctx.wa.texts if "terhambat" in body}
    templates = {t[0] for t in ctx.wa.templates if t[1] == "grc_update_tugas"}
    assert told | templates == {A, "85255500011"}
    assert not [e for e, _ in ctx.gmail.sent if "blocked" in e.subject]


def test_extension_request_then_manager_setuju_changes_the_due_date():
    ctx = context()
    task_id = add(ctx, "late", MONDAY + timedelta(days=3))
    handle_message(tap(f"ext:{task_id}"), ctx)
    handle_message(text("20 Nov", sender=A), ctx)
    assert task(ctx, task_id).due_date == MONDAY + timedelta(days=3)  # unchanged until Setuju
    asked = [r for r in log(ctx) if r.field == "Extension requested"]
    assert asked[0].new_value == "2026-11-20"
    manager = "85255500011"
    template = [t for t in ctx.wa.templates if t[0] == manager]  # window closed: template
    assert template and template[-1][1] == "grc_update_tugas"
    handle_message(text("hi", sender=manager), ctx)  # the Manager opens the window
    notify_whatsapp("extension_requested", task(ctx, task_id), ctx.store, ctx.wa,
                    "Fri 20 Nov 2026|2026-11-20|Staff A")  # fmt: skip
    buttons = ctx.wa.buttons[-1][2]
    assert buttons == [(f"exty:{task_id}:2026-11-20", "Setuju"),
                       (f"extn:{task_id}:2026-11-20", "Tolak")]  # fmt: skip
    handle_message(tap(f"exty:{task_id}:2026-11-20", sender=A), ctx)  # not the manager
    assert "Only the manager" in ctx.wa.last_text(A)
    handle_message(tap(buttons[0][0], sender=manager), ctx)
    assert task(ctx, task_id).due_date == date(2026, 11, 20)
    assert any(r.note.startswith("Extension approved") for r in log(ctx))


def test_extension_rejected_keeps_the_due_date():
    ctx = context()
    task_id = add(ctx, "late", MONDAY + timedelta(days=3))
    handle_message(tap(f"ext:{task_id}"), ctx)
    handle_message(text("this friday", sender=A), ctx)
    handle_message(tap(f"extn:{task_id}:2026-10-30", sender="85255500011"), ctx)
    assert task(ctx, task_id).due_date == MONDAY + timedelta(days=3)
    assert any(r.note == "Extension rejected" for r in log(ctx))


def test_snooze_skips_reminders_until_the_next_working_day_and_respects_limits():
    ctx = context()
    overdue = add(ctx, "overdue", MONDAY - timedelta(days=1))
    handle_message(tap(f"snz:{overdue}"), ctx)
    assert "is overdue; it can't be snoozed" in ctx.wa.last_text(A)  # never past the due date
    late = add(ctx, "in a week", MONDAY + timedelta(days=7))  # "due in 7 days" on Monday
    handle_message(tap(f"snz:{late}"), ctx)
    assert "until Tue 27 Oct" in ctx.wa.last_text(A)
    [row] = ctx.store.service_rows("_snooze", SnoozeRow)
    assert (row.task_id, row.until) == (late, date(2026, 10, 27))
    gmail = FakeGmailClient()
    store = ctx.store
    run_reminders(store, gmail, None, SHEET, now=datetime(2026, 10, 26, 10, 0, tzinfo=clock.TZ))
    sent = store.service_rows("_reminders_sent", ReminderSent)
    assert not [r for r in sent if r.task_id == late and r.person == "Staff A"]
    soon = add(ctx, "soon", MONDAY + timedelta(days=1))
    actor = next(p for p in store.staff() if p.name == "Staff A")
    reply = run_command(parse_command(f"snooze {soon} 30 Nov"),
                        CommandContext(store=store, gmail=gmail, actor=actor,
                                       channel="Email"))  # fmt: skip
    assert "not past the due date" in reply.reply or "at most 2 working days" in reply.reply


def test_done_asks_for_proof_then_a_file_finishes_it():
    ctx = context()
    task_id = add(ctx, "finish", MONDAY + timedelta(days=3))
    handle_message(tap(f"done:{task_id}"), ctx)
    assert "Kirim file bukti" in ctx.wa.last_text(A)
    handle_message(text("sebentar ya"), ctx)  # not a file: asked again
    assert "Kirim file bukti" in ctx.wa.last_text(A)
    proof = WAMessage(id="wamid.proof", sender=A, received_at=now(), kind="media",
                      media=[WAMedia(id="m-proof", filename="bukti.pdf",
                                     mime="application/pdf")])  # fmt: skip
    handle_message(proof, ctx)
    saved = task(ctx, task_id)
    assert saved.status == "Done" and "bukti.pdf" in saved.link
    assert saved.submitted_date == MONDAY


def test_a_question_expires_after_30_minutes():
    ctx = context()
    task_id = add(ctx, "stuck", MONDAY + timedelta(days=3))
    handle_message(tap(f"blk:{task_id}"), ctx)
    later = now() + timedelta(minutes=31)
    with clock.freeze(later):
        handle_message(text("nunggu data", sender=A), ctx, now=later)
    assert any("sudah habis" in body for to, body in ctx.wa.texts if to == A)
    assert task(ctx, task_id).status == "In Progress"
    assert ctx.store.service_rows(CONVERSATIONS, Conversation) == []


def test_batal_cancels_a_question():
    ctx = context()
    task_id = add(ctx, "stuck", MONDAY + timedelta(days=3))
    handle_message(tap(f"blk:{task_id}"), ctx)
    assert handle_message(text("batal"), ctx) == "answer_cancelled"
    assert task(ctx, task_id).status == "In Progress"


# --- undo -----------------------------------------------------------------------------------


def test_undo_within_24_hours_and_not_after():
    ctx = context()
    task_id = add(ctx, "work", MONDAY + timedelta(days=3), Status="Not Started")
    handle_message(tap(f"prog:{task_id}:50"), ctx)
    handle_message(text("undo"), ctx)
    handle_message(text("ya"), ctx)  # typed "ya" confirms too
    saved = task(ctx, task_id)
    assert (saved.progress_value, saved.status) == (None, "Not Started")
    assert "Undone" in ctx.wa.last_text(A)
    handle_message(tap(f"prog:{task_id}:75"), ctx)
    later = now() + timedelta(hours=25)
    with clock.freeze(later):
        handle_message(text("undo"), ctx, now=later)
        handle_message(tap("cf:yes"), ctx, now=later)
    assert "Nothing of yours to undo" in ctx.wa.last_text(A)
    assert task(ctx, task_id).progress_value == 0.75


def test_approve_cannot_be_undone():
    ctx = context()
    task_id = add(ctx, "in review", MONDAY + timedelta(days=3), Status="In Review",
                  Doer="Staff P", Owner="Staff P", Reviewer="Staff A")  # fmt: skip
    handle_message(tap(f"apv:{task_id}"), ctx)
    handle_message(text(f"undo {task_id}"), ctx)
    handle_message(tap("cf:yes"), ctx)
    assert "can't be undone" in ctx.wa.last_text(A)
    assert task(ctx, task_id).status == "Approved"


# --- notices and the 24-hour window --------------------------------------------------------


def test_notice_outside_the_window_is_a_template_inside_it_buttons():
    ctx = context()
    task_id = add(ctx, "review me", MONDAY + timedelta(days=3), Status="In Review",
                  Doer="Staff P", Owner="Staff P", Reviewer="Staff A")  # fmt: skip
    notify_whatsapp("in_review", task(ctx, task_id), ctx.store, ctx.wa)
    to, name, _, params = ctx.wa.templates[-1]
    assert (to, name, params[0]) == (A, "grc_update_tugas", task_id)
    assert ctx.wa.payloads[-1] == [f"view:task:{task_id}"]
    handle_message(text("halo"), ctx)  # Staff A writes: window open
    rows = ctx.store.service_rows(WA_WINDOW, WaWindowRow)
    assert [r.person for r in rows] == ["Staff A"]
    notify_whatsapp("in_review", task(ctx, task_id), ctx.store, ctx.wa)
    assert ctx.wa.buttons[-1][2] == [(f"apv:{task_id}", "Approve"), (f"ret:{task_id}", "Return")]
    handle_message(tap(f"view:task:{task_id}"), ctx)  # the template's button
    assert task_id in ctx.wa.buttons[-1][1]


def test_unknown_sender_is_refused():
    ctx = context()
    assert handle_message(tap("view:morning:x", sender="85299999999"), ctx) == "not_registered"


# --- the same commands by email -------------------------------------------------------------


def test_blocked_and_extension_decision_by_email():
    ctx = context()
    task_id = add(ctx, "email", MONDAY + timedelta(days=3))
    gmail = FakeGmailClient()
    gmail.add("e1", sender_email="staff.a@example.com", subject=f"Re: [GRC] {task_id}",
              body_text=f"blocked {task_id} waiting for finance data")  # fmt: skip
    gmail.add("e2", sender_email="staff.a@example.com", subject="Re: x",
              body_text=f"extend {task_id} 20 Nov")  # fmt: skip
    gmail.add("e3", sender_email="staff.k@example.com", subject="Re: x",
              body_text=f"approve extension {task_id}")  # fmt: skip
    services = Services(store=ctx.store, drive=FakeDriveStore(), ai=NullProvider())
    for row in ctx.store.backend.tabs["Config"]:
        if row[0] == "Allowed senders":
            row[1] = "@example.com"
    ctx.store.load()
    # One at a time, each confirmed with YES (section 22: typed changes wait for YES).
    pending = {mid: gmail.messages.pop(mid) for mid in ("e1", "e2", "e3")}
    for mid, sender in (("e1", "staff.a"), ("e2", "staff.a"), ("e3", "staff.k")):
        gmail.messages[mid] = pending[mid]
        process_inbox(gmail, services)
        assert "Reply YES to confirm" in gmail.replies[-1][1].text
        gmail.add(f"{mid}-yes", sender_email=f"{sender}@example.com", subject="Re: x",
                  body_text="YES")  # fmt: skip
        assert process_inbox(gmail, services).commands == 1
    saved = task(ctx, task_id)
    assert saved.status == "On Hold" and saved.due_date == date(2026, 11, 20)
    assert {r.channel for r in log(ctx)} == {"Email"}


def test_logs_never_carry_numbers_names_or_text(caplog):
    import logging

    from app.logs import JsonFormatter

    ctx = context()
    task_id = add(ctx, "secret title", MONDAY + timedelta(days=3), Doer="Staff P")
    with caplog.at_level(logging.DEBUG, logger="grc"):
        handle_message(tap(f"blk:{task_id}", sender=P), ctx)
        handle_message(text("nunggu tanda tangan direksi", sender=P), ctx)
        handle_message(tap(f"ext:{task_id}", sender=P), ctx)
        handle_message(text("20 Nov", sender=P), ctx)
        handle_message(tap(f"prog:{task_id}:50", sender=P), ctx)
        handle_message(text("undo", sender=P), ctx)
    formatted = "".join(JsonFormatter().format(record) for record in caplog.records)
    for secret in (A, P, "Staff P", "Staff A", "secret title", "tanda tangan"):
        assert secret not in formatted
