"""QA agent B: review loop reminders and submission reports (DESIGN section 28). Fakes only.

The clock is frozen by conftest on Monday 26 Oct 2026, 09:00 Asia/Hong_Kong; most tests move it
to 16:00 the same day. Staff P does the task, Staff A owns it, Staff K is the Manager. Made-up
people and numbers only.
"""

import logging
from datetime import date, datetime, timedelta

import pytest

from app import clock
from app.commands import Command, parse_command
from app.extract.ai import NullProvider
from app.gmail import process_inbox
from app.lifecycle import CommandContext, run_command
from app.models import REMINDERS_SENT, Attachment, ReminderSent, Task
from app.pipeline import Services
from app.reminders import (
    APPROVED_SUBMIT,
    RETURNED_REVISE,
    STALE,
    morning_triggers,
)
from app.replies import describe
from app.schedule import run_reminders
from app.submission import method_in, read
from app.wa_ui import card_buttons
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

# Timing, thread and process fields every log record has: never message data, and numbers in
# them (e.g. relativeCreated 12365.1555) must not look like a phone number.
_STD = set(logging.LogRecord("x", 0, "", 0, "", (), None).__dict__) - {"msg", "args"}

MONDAY = date(2026, 10, 26)
NOW = datetime(2026, 10, 26, 16, 0, tzinfo=clock.TZ)
P_WA = "85255500016"  # Staff P (fixture)
K_WA = "85255500011"  # Staff K, the Manager (given a fake number here)
SHEET = "https://docs.google.com/spreadsheets/d/sheet-qa"


def naive(moment: datetime) -> datetime:
    return moment.replace(tzinfo=None)


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=clock.TZ)


# --- set-up --------------------------------------------------------------------------------------


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


def make(ai=None, columns: bool = True, wa: bool = False, **config) -> Services:
    backend = FakeSheetsBackend.from_fixtures()
    if columns:
        backend.tabs["Master"][0] += ["Submission method", "Submitted at", "Acceptance"]
        for row in backend.tabs["Master"][1:]:
            row += ["", "", ""]
    set_config(backend, "Allowed senders", "@example.com")
    set_config(backend, "Workday start", "08:00")
    if ai is not None:
        set_config(backend, "AI provider", "Gemini API key")
    for name, value in config.items():
        set_config(backend, name, value)
    if wa:
        set_staff(backend, "Staff P", **{"WhatsApp opt-in": "Yes"})
        set_staff(backend, "Staff K", WhatsApp="+852 5550 0011", **{"WhatsApp opt-in": "Yes"})
    store = FakeSheetStore(backend)
    store.load()
    return Services(store=store, drive=FakeDriveStore(), ai=ai or NullProvider())


_source = [0]


def add(services: Services, **values) -> str:
    _source[0] += 1
    base = {"Title": "Quarterly return", "Owner": "Staff A", "Doer": "Staff P",
            "Status": "Approved", "Needs review": "Yes", "Reviewer": "Staff K",
            "Due date": MONDAY + timedelta(days=4), "Last updated": at(MONDAY, 9),
            "Source ID": f"src-qa-{_source[0]}"}  # fmt: skip
    saved, _ = services.store.append_task(Task.model_validate({**base, **values}))
    services.store.load()
    return saved.id


def get(services: Services, task_id: str) -> Task:
    services.store.load()
    return next(t for t in services.store.tasks() if t.id == task_id)


def person(services: Services, name: str):
    return next(p for p in services.store.staff() if p.name == name)


_mid = [0]


def submit(services, gmail, task_id, how, actor="Staff P", files=(), when=NOW):
    _mid[0] += 1
    ctx = CommandContext(
        store=services.store,
        gmail=gmail,
        actor=person(services, actor),
        channel="Email",
        files=list(files),
        message_id=f"qa-m{_mid[0]}",
    )
    with clock.freeze(naive(when)):
        return run_command(Command("submitted", task_id, how), ctx)


def sent_to(gmail: FakeGmailClient, address: str) -> list:
    return [email for email, _ in gmail.sent if address in email.to]


def wa_ctx(services: Services) -> WaContext:
    return WaContext(services=services, wa=FakeWhatsApp({"m-photo": b"\xff\xd8 photo"}),
                     queue=FakeQueue(), gmail=FakeGmailClient(), sheet_url=SHEET)  # fmt: skip


_wid = [0]


def wa_text(body: str, sender: str = P_WA, media=(), message_id: str = "") -> WAMessage:
    _wid[0] += 1
    return WAMessage(id=message_id or f"wamid.qa{_wid[0]}", sender=sender,
                     received_at=clock.now(), kind="media" if media else "text", text=body,
                     media=list(media))  # fmt: skip


def wa_tap(value: str, sender: str = P_WA) -> WAMessage:
    _wid[0] += 1
    return WAMessage(id=f"wamid.qatap{_wid[0]}", sender=sender, received_at=clock.now(),
                     kind="tap", tap=value)  # fmt: skip


# --- 1. the parser -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "method", "when"),
    [
        ("barusan via email", "Email", datetime(2026, 10, 26, 16, 0)),
        ("tadi pukul 9 pagi lewat portal OJK", "Portal", datetime(2026, 10, 26, 9, 0)),
        ("hardcopy ke OJK 24/10/2026 10:00", "Hardcopy", datetime(2026, 10, 24, 10, 0)),
        ("kurir 23 Oktober", "Hardcopy", datetime(2026, 10, 23, 0, 0)),
        ("email 2 Agustus 2026", "Email", datetime(2026, 8, 2, 0, 0)),
        ("email 15.30", "Email", datetime(2026, 10, 26, 15, 30)),
        ("email jam 3", "Email", datetime(2026, 10, 26, 15, 0)),  # 1-6 without pagi: afternoon
        ("via WA jam 7", "WhatsApp", datetime(2026, 10, 26, 7, 0)),  # 7 stays morning
        ("fax pukul 12", "Fax", datetime(2026, 10, 26, 12, 0)),
        ("diunggah ke sistem kemarin 17:45", "Portal", datetime(2026, 10, 25, 17, 45)),
        ("sent by e-mail at 2", "Email", datetime(2026, 10, 26, 14, 0)),
        ("faks 26-10 jam 1", "Fax", datetime(2026, 10, 26, 13, 0)),
        ("hard copy diantar ke regulator 22 Okt jam 4 pagi", "Hardcopy",
         datetime(2026, 10, 22, 4, 0)),
        ("email 7.75", "Email", datetime(2026, 10, 26, 16, 0)),  # not a time: now
    ],
)  # fmt: skip
def test_parser_reads_method_and_moment(text, method, when):
    sent = read(text, NOW)
    assert sent is not None, text
    assert (sent.method, naive(sent.at)) == (method, when)


@pytest.mark.parametrize(
    "text",
    [
        "email 27/10",  # tomorrow
        "via portal 30 Okt",
        "kurir jam 5",  # 17:00 today, still in the future at 16:00
        "sudah ke regulator",  # no method
        "done",
        "   ",
    ],
)
def test_parser_refuses_a_future_moment_or_no_method(text):
    assert read(text, NOW) is None


def test_jam_3_in_the_morning_clock_is_still_the_afternoon_and_so_refused():
    """At 09:00, "jam 3" is 15:00 today: in the future, so the bot asks instead."""
    assert read("email jam 3", at(MONDAY, 9)) is None
    assert naive(read("email jam 3 pagi", at(MONDAY, 9)).at) == datetime(2026, 10, 26, 3, 0)


def test_langsung_via_email_is_email():
    assert method_in("sudah langsung dikirim via email ke OJK") == "Email"


def test_a_date_without_year_in_january_means_last_december():
    jan = at(date(2027, 1, 5), 11)
    sent = read("hardcopy ke OJK 30 Des 15:00", jan)
    assert sent is not None and naive(sent.at) == datetime(2026, 12, 30, 15, 0)
    sent = read("email 30/12", jan)
    assert sent is not None and sent.at.date() == date(2026, 12, 30)


def test_tomorrow_is_a_future_day_and_refused():
    assert read("via email besok", NOW) is None
    assert read("will send by courier tomorrow", NOW) is None


def test_an_impossible_date_is_not_turned_into_now():
    assert read("email 31/02", NOW) is None


def test_command_words_route_to_submitted():
    assert parse_command("terkirim G-7 via portal").name == "submitted"
    assert parse_command("sudah kirim G-0007 hardcopy") == Command(
        "submitted", "G-0007", "hardcopy"
    )
    assert parse_command("dikirim lewat email", has_context=True) == Command(
        "submitted", None, "lewat email"
    )
    assert parse_command("dikirim lewat email") is None  # no ID, no context: intake


# --- 2. applying it (run_command) ----------------------------------------------------------------


def test_submission_writes_every_column_and_reads_back():
    services = make()
    gmail = FakeGmailClient()
    task_id = add(services, Notes="Older note")
    result = submit(services, gmail, task_id, "hardcopy ke OJK kemarin 17:45")
    assert result.changed
    saved = get(services, task_id)
    assert saved.status == "Done"
    assert saved.submitted_date == date(2026, 10, 25)
    assert saved.submission_method == "Hardcopy"
    assert saved.submitted_at is not None
    assert saved.submitted_at.tzinfo is not None
    assert naive(saved.submitted_at) == datetime(2026, 10, 25, 17, 45)
    assert saved.notes.splitlines() == [
        "Older note",
        "Submitted 25 Oct 2026 17:45 by Staff P: hardcopy ke OJK kemarin 17:45",
    ]
    raw = services.store.backend.column("Master", "Submitted at")[-1]
    assert str(raw).startswith("2026-10-25 17:45") or isinstance(raw, int | float)


def test_notice_goes_to_managers_and_owner_not_the_doer():
    services = make()
    gmail = FakeGmailClient()
    task_id = add(services)
    submit(services, gmail, task_id, "via email 15:30 ke ojk@example.go.id")
    [to_k] = sent_to(gmail, "staff.k@example.com")
    [to_a] = sent_to(gmail, "staff.a@example.com")
    assert sent_to(gmail, "staff.p@example.com") == []
    for notice in (to_k, to_a):
        assert task_id in notice.subject and "submitted" in notice.subject
        assert "How: Email" in notice.text
        assert "When: Mon 26 Oct 2026 15:30" in notice.text
        assert "Details: via email 15:30" in notice.text
        assert "Due was: Fri 30 Oct 2026" in notice.text


def test_owner_who_is_the_doer_is_not_told_twice():
    services = make()
    gmail = FakeGmailClient()
    task_id = add(services, Owner="Staff P")
    submit(services, gmail, task_id, "portal")
    assert sent_to(gmail, "staff.p@example.com") == []
    assert len(sent_to(gmail, "staff.k@example.com")) == 1


def test_proof_file_kept_with_the_submission():
    services = make()
    gmail = FakeGmailClient()
    task_id = add(services)
    receipt = Attachment(filename="Tanda terima OJK.pdf", mime="application/pdf", data=b"%PDF")
    submit(services, gmail, task_id, "hardcopy tadi 14:00", files=[receipt])
    assert "Tanda terima OJK.pdf" in get(services, task_id).link
    assert gmail.stored[task_id][0].filename == "Tanda terima OJK.pdf"


@pytest.mark.parametrize(
    ("status", "review", "allowed"),
    [
        ("Approved", "Yes", True),
        ("In Progress", "Yes", False),
        ("In Review", "Yes", False),
        ("Returned", "Yes", False),
        ("Not Started", "Yes", False),
        ("In Progress", "", True),  # no review needed: straight from In Progress
        ("Not Started", "No", True),
        ("On Hold", "", True),
    ],
)
def test_review_gating(status, review, allowed):
    services = make()
    gmail = FakeGmailClient()
    task_id = add(services, Status=status, **{"Needs review": review})
    result = submit(services, gmail, task_id, "via email")
    assert result.changed is allowed
    assert get(services, task_id).status == ("Done" if allowed else status)
    if not allowed:
        assert "needs review first" in result.reply
        assert gmail.sent == []


@pytest.mark.parametrize("status", ["Done", "Cancelled", "Proposed"])
def test_closed_or_proposed_tasks_refuse(status):
    services = make()
    gmail = FakeGmailClient()
    task_id = add(services, Status=status)
    result = submit(services, gmail, task_id, "via email")
    assert not result.changed and "Nothing changed" in result.reply


def test_who_may_submit():
    services = make()
    gmail = FakeGmailClient()
    task_id = add(services, Support="Staff D")
    support = submit(services, gmail, task_id, "via email", actor="Staff D")
    assert not support.changed and "only Staff P" in support.reply
    stranger = submit(services, gmail, task_id, "via email", actor="Staff G")
    assert not stranger.changed and "not the Owner or Doer" in stranger.reply
    assert get(services, task_id).status == "Approved"
    manager = submit(services, gmail, task_id, "via email", actor="Staff K")
    assert manager.changed
    assert "by Staff K: via email" in get(services, task_id).notes


def test_same_message_applies_once():
    services = make()
    gmail = FakeGmailClient()
    task_id = add(services)
    ctx = CommandContext(store=services.store, gmail=gmail, actor=person(services, "Staff P"),
                         channel="Email", message_id="dup-1")  # fmt: skip
    with clock.freeze(naive(NOW)):
        assert run_command(Command("submitted", task_id, "via email"), ctx).changed
        again = run_command(Command("submitted", task_id, "via email"), ctx)
    assert not again.changed
    assert get(services, task_id).notes.count("Submitted ") == 1
    assert len(sent_to(gmail, "staff.k@example.com")) == 1


# --- 2b. by email --------------------------------------------------------------------------------


def test_typed_command_by_email_waits_for_yes_and_proof_is_kept():
    services = make()
    gmail = FakeGmailClient()
    task_id = add(services)
    receipt = Attachment(filename="Bukti kirim.pdf", mime="application/pdf", data=b"%PDF-1")
    with clock.freeze(naive(NOW)):
        gmail.add("qa-e1", sender_email="staff.p@example.com", subject=f"{task_id} terkirim",
                  body_text=f"submitted {task_id} hardcopy ke OJK hari ini 15:30",
                  attachments=[receipt])  # fmt: skip
        process_inbox(gmail, services)
        ask = gmail.replies[-1][1].text
        assert "Reply YES" in ask
        assert f"{task_id} sudah dikirim (Hardcopy, 26 Oct 2026 15:30)" in ask
        assert "Approved → Done" in ask
        assert get(services, task_id).status == "Approved"  # nothing yet
        gmail.add("qa-e2", thread_id="thread-qa-e1", sender_email="staff.p@example.com",
                  subject=f"Re: {task_id} terkirim", body_text="YES")  # fmt: skip
        process_inbox(gmail, services)
    saved = get(services, task_id)
    assert (saved.status, saved.submission_method) == ("Done", "Hardcopy")
    assert naive(saved.submitted_at) == datetime(2026, 10, 26, 15, 30)
    assert "Bukti kirim.pdf" in saved.link
    assert len(sent_to(gmail, "staff.k@example.com")) == 1
    assert len(sent_to(gmail, "staff.a@example.com")) == 1
    assert sent_to(gmail, "staff.p@example.com") == []


def test_ai_reading_of_plain_words_by_email():
    ai = FakeAIProvider(reply={"task_id": None, "intent": "submitted", "progress": None,
                               "note": "lewat portal OJK tadi jam 2", "requested_date": None,
                               "reply_to_user": "ok", "confidence": 0.9})  # fmt: skip
    services = make(ai=ai)
    gmail = FakeGmailClient()
    task_id = add(services)
    with clock.freeze(naive(NOW)):
        gmail.add("qa-ai1", sender_email="staff.p@example.com", subject=f"Re {task_id}",
                  body_text=f"{task_id} sudah saya upload ke portal OJK tadi jam 2")  # fmt: skip
        process_inbox(gmail, services)
        ask = gmail.replies[-1][1].text
        assert f"{task_id} sudah dikirim (Portal, 26 Oct 2026 14:00)" in ask
        gmail.add("qa-ai2", thread_id="thread-qa-ai1", sender_email="staff.p@example.com",
                  subject="Re", body_text="yes, thanks")  # fmt: skip
        process_inbox(gmail, services)
    saved = get(services, task_id)
    assert saved.status == "Done" and saved.submission_method == "Portal"


def test_redelivered_yes_does_not_submit_twice():
    services = make()
    gmail = FakeGmailClient()
    task_id = add(services)
    with clock.freeze(naive(NOW)):
        gmail.add("qa-d1", sender_email="staff.p@example.com", subject="kirim",
                  body_text=f"submitted {task_id} via email 15:00")  # fmt: skip
        process_inbox(gmail, services)
        gmail.add("qa-d2", thread_id="thread-qa-d1", sender_email="staff.p@example.com",
                  subject="Re: kirim", body_text="YES")  # fmt: skip
        process_inbox(gmail, services)
        gmail.add(
            "qa-d2",
            thread_id="thread-qa-d1",
            sender_email="staff.p@example.com",
            subject="Re: kirim",
            body_text="YES",
        )  # Gmail shows it unread again
        process_inbox(gmail, services)
    saved = get(services, task_id)
    assert saved.notes.count("Submitted ") == 1
    assert len(sent_to(gmail, "staff.k@example.com")) == 1


def test_typed_submission_without_method_asks_how_instead_of_yes():
    services = make()
    gmail = FakeGmailClient()
    task_id = add(services)
    with clock.freeze(naive(NOW)):
        gmail.add("qa-n1", sender_email="staff.p@example.com", subject="kirim",
                  body_text=f"submitted {task_id} ke regulator")  # fmt: skip
        process_inbox(gmail, services)
    ask = gmail.replies[-1][1].text
    assert "How and when" in ask and "Reply YES" not in ask


def test_yes_writes_the_moment_that_was_shown():
    services = make()
    gmail = FakeGmailClient()
    task_id = add(services)
    shown = at(MONDAY, 23, 50)
    with clock.freeze(naive(shown)):
        gmail.add("qa-t1", sender_email="staff.p@example.com", subject="kirim",
                  body_text=f"submitted {task_id} kurir kemarin 17:00")  # fmt: skip
        process_inbox(gmail, services)
        assert "25 Oct 2026 17:00" in gmail.replies[-1][1].text
    with clock.freeze(naive(shown + timedelta(minutes=20))):  # Tuesday 00:10
        gmail.add("qa-t2", thread_id="thread-qa-t1", sender_email="staff.p@example.com",
                  subject="Re: kirim", body_text="YES")  # fmt: skip
        process_inbox(gmail, services)
    saved = get(services, task_id)
    assert saved.status == "Done"
    assert naive(saved.submitted_at) == datetime(2026, 10, 25, 17, 0)


def test_preview_lines():
    approved = Task.model_validate({"ID": "G-0042", "Status": "Approved"})
    in_progress = Task.model_validate({"ID": "G-0043", "Status": "In Progress"})
    with clock.freeze(naive(NOW)):
        assert describe(Command("submitted", "G-0042", "fax kemarin 10.15"), approved) == (
            "G-0042 sudah dikirim (Fax, 25 Oct 2026 10:15) – Status G-0042: Approved → Done"
        )
        line = describe(Command("submitted", "G-0043", "portal"), in_progress, ["bukti.png"])
    assert line == (
        "G-0043 sudah dikirim (Portal, 26 Oct 2026 16:00) – Status G-0043: In Progress → Done"
        " (file: bukti.png)"
    )


# --- 4. reminders --------------------------------------------------------------------------------


def morning_mail(services, gmail, day: date, address="staff.p@example.com"):
    run_reminders(services.store, gmail, None, SHEET, now=at(day, 10))
    mails = [e for e in sent_to(gmail, address) if "Your tasks" in e.subject and f"{day:%d %b}"
             in e.subject]  # fmt: skip
    return mails[-1].text if mails else ""


def test_approved_listed_every_morning_until_done():
    services = make()
    gmail = FakeGmailClient()
    task_id = add(services, **{"Due date": MONDAY + timedelta(days=20)})
    monday = morning_mail(services, gmail, MONDAY)
    assert "Approved: submit it, then reply: submitted" in monday and task_id in monday
    tuesday = morning_mail(services, gmail, MONDAY + timedelta(days=1))
    assert "Approved: submit it" in tuesday and task_id in tuesday
    submit(services, gmail, task_id, "via email", when=at(MONDAY + timedelta(days=1), 11))
    wednesday = morning_mail(services, gmail, MONDAY + timedelta(days=2))
    assert task_id not in wednesday


def test_returned_listed_until_back_in_review_and_not_stale():
    services = make()
    gmail = FakeGmailClient()
    task_id = add(services, Status="Returned", **{"Review notes": "Fix table 2",
                  "Due date": MONDAY + timedelta(days=20), "Last updated": at(MONDAY, 9)
                  - timedelta(days=30)})  # fmt: skip
    text = morning_mail(services, gmail, MONDAY)
    assert "Returned with feedback: revise and send review" in text and task_id in text
    assert "Not updated for" not in text
    tuesday = MONDAY + timedelta(days=1)
    ctx = CommandContext(store=services.store, gmail=gmail, actor=person(services, "Staff P"),
                         channel="Email", message_id="qa-review-1")  # fmt: skip
    with clock.freeze(naive(at(tuesday, 9))):
        assert run_command(Command("review", task_id), ctx).changed
    text = morning_mail(services, gmail, tuesday + timedelta(days=1))
    assert "Returned with feedback" not in text
    reviewer = [e for e in sent_to(gmail, "staff.k@example.com") if "Your tasks" in e.subject]
    assert reviewer and "Waiting for your review" in reviewer[-1].text


@pytest.mark.parametrize("status", ["Done", "Cancelled", "Proposed"])
def test_no_review_loop_reminder_for_closed_tasks(status):
    services = make()
    gmail = FakeGmailClient()
    add(services, Status=status, **{"Due date": MONDAY + timedelta(days=20)})
    assert morning_mail(services, gmail, MONDAY) == ""


def test_morning_ordering_of_the_review_loop_labels():
    services = make()
    gmail = FakeGmailClient()
    far = MONDAY + timedelta(days=20)
    overdue = add(services, Status="In Progress", **{"Due date": MONDAY - timedelta(days=1)})
    approved = add(services, **{"Due date": far})
    returned = add(services, Status="Returned", **{"Due date": far})
    add(services, Status="In Review", Doer="Staff A", Owner="Staff A", Reviewer="Staff P",
        **{"Due date": far})  # fmt: skip
    text = morning_mail(services, gmail, MONDAY)
    positions = [
        text.index("Overdue"),
        text.index("Returned with feedback"),
        text.index("Approved: submit it"),
        text.index("Waiting for your review"),
    ]
    assert positions == sorted(positions)
    assert all(task_id in text for task_id in (overdue, approved, returned))


def test_approved_and_returned_rules_go_to_the_doer_only():
    services = make()
    staff = services.store.staff()
    tasks = [
        Task.model_validate({"ID": "G-0901", "Owner": "Staff A", "Doer": "Staff P",
                             "Status": "Approved", "Support": "Staff D"}),
        Task.model_validate({"ID": "G-0902", "Owner": "Staff A", "Doer": "Staff P",
                             "Status": "Returned", "Last updated": at(MONDAY, 9)
                             - timedelta(days=40)}),
    ]  # fmt: skip
    fired = morning_triggers(tasks, staff, MONDAY, services.store.config())
    found = {(t.rule, t.task_id, t.person) for t in fired}
    assert found == {(APPROVED_SUBMIT, "G-0901", "Staff P"), (RETURNED_REVISE, "G-0902", "Staff P")}
    assert not any(rule == STALE for rule, _, _ in found)


def _over_cap_today(services: Services, day: date) -> None:
    """Staff P already had today's one allowed reminder (Max reminders per day = 1)."""
    rows = services.store.service_rows(REMINDERS_SENT, ReminderSent)
    rows.append(ReminderSent(day=day, rule="due_in_7", task_id="G-0999", person="Staff P",
                             channel="Email", sent_at=at(day, 9, 30)))  # fmt: skip
    services.store.rewrite_service(REMINDERS_SENT, rows)


def test_cap_defers_the_approved_reminder_to_the_next_morning():
    services = make(**{"Max reminders per day": 1})
    gmail = FakeGmailClient()
    task_id = add(services, **{"Due date": MONDAY + timedelta(days=20)})
    _over_cap_today(services, MONDAY)
    assert morning_mail(services, gmail, MONDAY) == ""
    rows = services.store.service_rows(REMINDERS_SENT, ReminderSent)
    assert any(r.rule == APPROVED_SUBMIT and r.sent_at is None for r in rows)
    text = morning_mail(services, gmail, MONDAY + timedelta(days=1))
    assert text.count(task_id) == 1 and "Approved: submit it" in text


def test_deferred_returned_reminder_dropped_once_back_in_review():
    services = make(**{"Max reminders per day": 1})
    gmail = FakeGmailClient()
    task_id = add(services, Status="Returned", **{"Due date": MONDAY + timedelta(days=20)})
    _over_cap_today(services, MONDAY)
    morning_mail(services, gmail, MONDAY)  # deferred
    services.store.update_task(task_id, {"Status": "In Review"})
    services.store.load()
    text = morning_mail(services, gmail, MONDAY + timedelta(days=1))
    assert "Returned with feedback" not in text


def test_deferred_approved_reminder_dropped_once_cancelled():
    services = make(**{"Max reminders per day": 1})
    gmail = FakeGmailClient()
    task_id = add(services, **{"Due date": MONDAY + timedelta(days=20)})
    _over_cap_today(services, MONDAY)
    morning_mail(services, gmail, MONDAY)
    services.store.update_task(task_id, {"Status": "Cancelled"})
    services.store.load()
    assert task_id not in morning_mail(services, gmail, MONDAY + timedelta(days=1))


def test_manager_summary_sections_and_whatsapp_five_numbers():
    services = make(wa=True)
    gmail = FakeGmailClient()
    wa = FakeWhatsApp()
    far = MONDAY + timedelta(days=20)
    add(services, **{"Due date": far})
    add(services, **{"Due date": far})
    add(services, Status="Returned", **{"Due date": far})
    add(services, Status="Proposed", Acceptance="Asked", **{"Due date": far})
    add(services, Status="Proposed", **{"Due date": far})
    run_reminders(services.store, gmail, wa, SHEET, now=at(MONDAY, 10, 15))
    [summary] = [t for t in wa.templates if t[1] == "grc_ringkasan_manager"]
    params = summary[3]
    assert len(params) == 5 and all(p.isdigit() for p in params)
    assert params[3] == "1"  # awaiting approval leaves out the one waiting for the Doer's YES
    # the email summary: K on email now
    set_staff(services.store.backend, "Staff K", **{"Reminder channel": "Email"})
    services.store.load()
    gmail2 = FakeGmailClient()
    run_reminders(services.store, gmail2, None, SHEET, now=at(MONDAY + timedelta(days=1), 10, 15))
    [mail] = [e for e in sent_to(gmail2, "staff.k@example.com") if "Team summary" in e.subject]
    assert "Approved, not submitted yet (2)" in mail.text
    assert "Returned for changes (1)" in mail.text
    assert "Waiting for acceptance (1)" in mail.text
    assert "Awaiting approval (1)" in mail.text


def test_whatsapp_morning_template_parameters_are_one_line():
    services = make(wa=True)
    gmail = FakeGmailClient()
    wa = FakeWhatsApp()
    add(services, **{"Due date": MONDAY + timedelta(days=20)})
    add(services, Status="Returned", **{"Due date": MONDAY + timedelta(days=20)})
    run_reminders(services.store, gmail, wa, SHEET, now=at(MONDAY, 10))
    [morning] = [t for t in wa.templates if t[0] == P_WA and t[1] == "grc_pagi"]
    assert morning[3][1] == "2"
    assert all("\n" not in p for p in morning[3])


# --- 5. WhatsApp ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "review", "second"),
    [
        ("Approved", "Yes", "Sudah dikirim"),
        ("In Progress", "", "Sudah dikirim"),
        ("In Progress", "Yes", "Kirim review"),
        ("Returned", "Yes", "Kirim review"),
        ("Not Started", "Yes", "Kirim review"),
    ],
)
def test_card_second_button(status, review, second):
    services = make()
    task = Task.model_validate({"ID": "G-0100", "Owner": "Staff A", "Doer": "Staff P",
                                "Status": status, "Needs review": review})  # fmt: skip
    buttons = card_buttons(task, person(services, "Staff P"), services.store.staff())
    assert [label for _, label in buttons] == ["Update progres", second, "Lainnya"]


def test_supporter_card_has_no_submit_button():
    services = make()
    task = Task.model_validate({"ID": "G-0100", "Owner": "Staff A", "Doer": "Staff P",
                                "Status": "Approved", "Support": "Staff D"})  # fmt: skip
    buttons = card_buttons(task, person(services, "Staff D"), services.store.staff())
    assert [label for _, label in buttons] == ["Update progres", "Lainnya"]


def test_wa_tap_then_answer_with_photo_proof_closes_task():
    services = make(wa=True)
    ctx = wa_ctx(services)
    task_id = add(services)
    with clock.freeze(naive(NOW)):
        handle_message(wa_tap(f"sub:{task_id}"), ctx)
        assert "Bagaimana dan kapan" in ctx.wa.last_text(P_WA)
        photo = WAMedia(id="m-photo", filename="tanda_terima.jpg", mime="image/jpeg")
        handle_message(wa_text("diantar kurir ke OJK jam 2", media=[photo]), ctx)
    saved = get(services, task_id)
    assert (saved.status, saved.submission_method) == ("Done", "Hardcopy")
    assert naive(saved.submitted_at) == datetime(2026, 10, 26, 14, 0)
    assert "tanda_terima.jpg" in saved.link
    assert "Done" in ctx.wa.last_text(P_WA)


def test_wa_batal_and_expiry_change_nothing():
    services = make(wa=True)
    ctx = wa_ctx(services)
    task_id = add(services)
    with clock.freeze(naive(NOW)):
        handle_message(wa_tap(f"sub:{task_id}"), ctx)
        assert handle_message(wa_text("batal"), ctx) == "answer_cancelled"
        handle_message(wa_tap(f"sub:{task_id}"), ctx)
    later = NOW + timedelta(minutes=31)
    with clock.freeze(naive(later)):
        handle_message(wa_text("via email jam 4"), ctx, now=later)
    assert any("sudah habis" in body for to, body in ctx.wa.texts if to == P_WA)
    assert get(services, task_id).status == "Approved"


def test_wa_tap_on_task_needing_review_refuses():
    services = make(wa=True)
    ctx = wa_ctx(services)
    task_id = add(services, Status="In Progress")
    with clock.freeze(naive(NOW)):
        handle_message(wa_tap(f"sub:{task_id}"), ctx)
        handle_message(wa_text("via email"), ctx)
    assert "needs review first" in ctx.wa.last_text(P_WA)
    assert get(services, task_id).status == "In Progress"


def test_wa_second_answer_after_the_bot_asks_again_is_used():
    services = make(wa=True)
    ctx = wa_ctx(services)
    task_id = add(services)
    with clock.freeze(naive(NOW)):
        handle_message(wa_tap(f"sub:{task_id}"), ctx)
        handle_message(wa_text("sudah tadi siang"), ctx)  # no method
        assert "How and when" in ctx.wa.last_text(P_WA) or "Bagaimana" in ctx.wa.last_text(P_WA)
        handle_message(wa_text("lewat email"), ctx)
    assert get(services, task_id).status == "Done"


def test_wa_redelivered_answer_is_ignored():
    services = make(wa=True)
    ctx = wa_ctx(services)
    task_id = add(services)
    with clock.freeze(naive(NOW)):
        handle_message(wa_tap(f"sub:{task_id}"), ctx)
        answer = wa_text("hardcopy ke OJK jam 14.00", message_id="wamid.qa-same")
        handle_message(answer, ctx)
        outcome = handle_message(answer, ctx)
    assert get(services, task_id).notes.count("Submitted ") == 1
    assert outcome not in ("draft_opened", "draft_joined")
    assert not [row for row in services.store.wa_pending() if row.kind == "draft"]


def test_supporter_via_lainnya_is_refused():
    services = make(wa=True)
    set_staff(services.store.backend, "Staff A", **{"WhatsApp opt-in": "Yes"})
    services.store.load()
    ctx = wa_ctx(services)
    task_id = add(services, Owner="Staff P", Support="Staff A")
    with clock.freeze(naive(NOW)):
        handle_message(wa_tap(f"sub:{task_id}", sender="85255500001"), ctx)
        handle_message(wa_text("via email", sender="85255500001"), ctx)
    assert "only Staff P" in ctx.wa.last_text("85255500001")
    assert get(services, task_id).status == "Approved"


def test_wa_approval_notice_tells_the_doer_to_submit():
    services = make(wa=True)
    ctx = wa_ctx(services)
    task_id = add(services, Status="In Review")
    with clock.freeze(naive(NOW)):
        handle_message(wa_text("halo", sender=P_WA), ctx)  # opens P's window
        handle_message(wa_tap(f"apv:{task_id}", sender=K_WA), ctx)
    assert get(services, task_id).status == "Approved"
    [(_, body, buttons)] = [b for b in ctx.wa.buttons if b[0] == P_WA and task_id in b[1]]
    assert "submitted" in body.lower() or "dikirim" in body.lower()
    assert [label for _, label in buttons] == ["Sudah dikirim"]


def test_wa_submitted_notice_has_details_and_due_date():
    services = make(wa=True)
    ctx = wa_ctx(services)
    task_id = add(services)
    with clock.freeze(naive(NOW)):
        handle_message(wa_text("halo", sender=K_WA), ctx)  # K's window open
        handle_message(wa_tap(f"sub:{task_id}"), ctx)
        handle_message(wa_text("hardcopy ke OJK loket 3 jam 14.00"), ctx)
    [body] = [text for to, text in ctx.wa.texts if to == K_WA and "sudah mengirim" in text]
    assert "loket 3" in body
    assert "30 Oct 2026" in body


def test_wa_returned_notice_carries_notes_and_review_button():
    services = make(wa=True)
    ctx = wa_ctx(services)
    task_id = add(services, Status="In Review")
    with clock.freeze(naive(NOW)):
        handle_message(wa_text("halo", sender=P_WA), ctx)
        handle_message(wa_tap(f"ret:{task_id}", sender=K_WA), ctx)
        handle_message(wa_text("Lengkapi lampiran 2", sender=K_WA), ctx)
    assert get(services, task_id).status == "Returned"
    [(_, body, buttons)] = [b for b in ctx.wa.buttons if b[0] == P_WA and task_id in b[1]]
    assert "Lengkapi lampiran 2" in body
    assert "Kirim review" in [label for _, label in buttons]


# --- 6. email notices -----------------------------------------------------------------------------


def test_email_approved_and_returned_notices_tell_the_next_step():
    services = make()
    gmail = FakeGmailClient()
    first = add(services, Status="In Review")
    second = add(services, Status="In Review")
    k = person(services, "Staff K")
    with clock.freeze(naive(NOW)):
        run_command(
            Command("approve", first),
            CommandContext(
                store=services.store, gmail=gmail, actor=k, channel="Email", message_id="qa-ap"
            ),
        )
        run_command(
            Command("return", second, "Tambah tabel 3"),
            CommandContext(
                store=services.store, gmail=gmail, actor=k, channel="Email", message_id="qa-rt"
            ),
        )
    approved, returned = sent_to(gmail, "staff.p@example.com")
    assert first in approved.subject and f"submitted {first} <how and when>" in approved.text
    assert "Review notes: Tambah tabel 3" in returned.text
    assert f"review {second}" in returned.text


def test_status_shows_returned_review_notes():
    services = make()
    gmail = FakeGmailClient()
    task_id = add(services, Status="Returned", **{"Review notes": "Ganti angka Q3"})
    ctx = CommandContext(store=services.store, gmail=gmail, actor=person(services, "Staff P"),
                         channel="Email", message_id="qa-st")  # fmt: skip
    assert "Review notes: Ganti angka Q3" in run_command(Command("status", task_id), ctx).reply


# --- 8. privacy ----------------------------------------------------------------------------------


def test_logs_carry_no_text_names_or_addresses(caplog):
    services = make(wa=True)
    ctx = wa_ctx(services)
    task_id = add(services)
    caplog.set_level(logging.DEBUG, logger="grc")
    with clock.freeze(naive(NOW)):
        handle_message(wa_tap(f"sub:{task_id}"), ctx)
        handle_message(wa_text("hardcopy ke OJK loket rahasia jam 14.00"), ctx)
        gmail = FakeGmailClient()
        task2 = add(services)
        gmail.add("qa-p1", sender_email="staff.p@example.com", subject="kirim",
                  body_text=f"submitted {task2} portal rahasia 15:00")  # fmt: skip
        process_inbox(gmail, services)
    assert caplog.records  # the flow did log something
    for record in caplog.records:
        blob = (
            record.getMessage()
            + " "
            + " ".join(str(v) for k, v in record.__dict__.items() if k not in _STD)
        )
        for secret in ("rahasia", "Staff P", "Staff K", "staff.p@example.com", P_WA, "OJK"):
            assert secret not in blob, (record.msg, secret)
