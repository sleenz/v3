"""QA6-A: scenario tests that try to break the new assignment flow (DESIGN sections 26 and 27).

Section 26 decides who may assign and when a PROMPT waits for the Manager; section 27 makes a
PROMPT assignment `Proposed` with Acceptance `Pending`, the change job sends the Doer the
assignment email with the files and the YES / NO question (Acceptance `Asked`), YES makes it
`In Progress` and sends nothing more, NO + reason keeps it `Proposed`, `Declined`, managers told,
and the Manager's reassign sets `Pending` again.

Fakes only, made-up people from tests/fixtures/staff.json: Staff K is the Manager; Staff A, D, P
are full time; Staff B and Staff C are interns (Staff B reports to Staff A, set here); Staff R is
additional help (set here). Numbers are fake 8525550xxxx. Clock: Monday 26 Oct 2026, 09:00
Asia/Hong_Kong (fixture Workday start 08:00). Tests marked xfail(strict=True) document behaviour
that contradicts DESIGN.md (finding IDs QA6-A<n>).
"""

from __future__ import annotations

import itertools
import logging
from datetime import datetime, timedelta

import pytest

from app import clock
from app.commands import Command
from app.extract.ai import NullProvider
from app.gmail import process_inbox
from app.jobs import ChangesRun, run_changes
from app.lifecycle import CommandContext, CommandResult, run_command
from app.models import Attachment, Task
from app.pipeline import Services
from app.whatsapp import WaContext, WAMessage, finalize, handle_message
from tests.fakes import (
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

SHEET = "https://docs.google.com/spreadsheets/d/sheet-qa6a"
P_WA = "85255500016"  # Staff P (fixture, fake)
A_WA = "85255500001"  # Staff A (fixture, fake)
K_WA = "85255500011"  # Staff K (added here, fake)
PDF = Attachment(filename="Surat.pdf", mime="application/pdf", data=b"%PDF letter")
XLSX = Attachment(filename="Data.xlsx", mime="application/vnd.ms-excel", data=b"xlsx-bytes")
BODY = "Mohon kirim laporan stress test, deadline 20 November 2026."
QUESTION = "Reply YES to start this task"
_ids = itertools.count(1)


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
    width = len(header)
    for row in rows[1:]:
        row += [""] * (width - len(row))
    for column in values:
        if column not in header:
            header.append(column)
            for row in rows[1:]:
                row.append("")
    for row in rows[1:]:
        if row[header.index("Name")] == name:
            for column, value in values.items():
                row[header.index(column)] = value


def make_backend(acceptance: bool = True, wa: bool = False) -> FakeSheetsBackend:
    data = FakeSheetsBackend.from_fixtures()
    if acceptance:
        data.tabs["Master"][0].append("Acceptance")
        for row in data.tabs["Master"][1:]:
            row.append("")
    set_config(data, "File storage", "None")
    set_staff(data, "Staff B", **{"Reports to": "Staff A"})
    set_staff(data, "Staff R", Role="Additional help")
    if wa:
        set_staff(data, "Staff K", WhatsApp="+852 5550 0011")
        for row in data.tabs["Staff"]:
            if row and row[0] == "Staff ID":
                row.append("WhatsApp opt-in")
            elif row:
                row.append("Yes")
    return data


def setup(acceptance: bool = True, wa: bool = False, data: FakeSheetsBackend | None = None):
    store = FakeSheetStore(data or make_backend(acceptance, wa))
    store.load()
    services = Services(store=store, drive=FakeDriveStore(), ai=NullProvider())
    gmail = FakeGmailClient()
    run_changes(store, gmail, SHEET)  # the first run only records the baseline
    return services, gmail


def wa_ctx(services, gmail) -> WaContext:
    return WaContext(services=services, wa=FakeWhatsApp(), queue=FakeQueue(), gmail=gmail,
                     sheet_url=SHEET)  # fmt: skip


def prompt(services, gmail, line: str = "PROMPT: assign this to Staff P",
           sender: str = "staff.k@example.com", attachments=None) -> Task:  # fmt: skip
    message_id = f"req-{next(_ids)}"
    gmail.add(message_id, sender_email=sender, subject="Laporan stress test",
              body_text=f"{line}\n\n{BODY}",
              attachments=[PDF] if attachments is None else attachments)  # fmt: skip
    process_inbox(gmail, services)
    services.store.load()
    return next(t for t in services.store.tasks() if t.source_id == message_id)


def changes(services, gmail, wa=None) -> ChangesRun:
    services.store.load()
    return run_changes(services.store, gmail, SHEET, wa)


def now(services, task_id: str) -> Task:
    services.store.load()
    return next(t for t in services.store.tasks() if t.id == task_id)


def assignment_emails(gmail, address: str | None = None) -> list:
    return [
        e
        for e, _ in gmail.sent
        if "You have been assigned" in e.text and (address is None or address in e.to)
    ]


def ask_thread(services, task_id: str) -> str:
    rows = [r for r in services.store.backend.tabs["_outbox"][1:] if "accept_ask" in str(r)]
    rows = [r for r in rows if task_id in str(r)]
    return str(rows[-1][3]).lstrip("'")


def emailed(services, gmail, line: str = "PROMPT: assign this to Staff P", wa=None):
    task = prompt(services, gmail, line)
    assert changes(services, gmail, wa).asked == 1
    return task, ask_thread(services, task.id)


def reply(gmail, thread: str, body: str, sender: str = "staff.p@example.com") -> str:
    message_id = f"r-{next(_ids)}"
    gmail.add(message_id, thread_id=thread, sender_email=sender, subject="Re: assignment",
              body_text=body)  # fmt: skip
    return message_id


def run(services, gmail, name: str, command: Command) -> CommandResult:
    services.store.load()
    actor = next(p for p in services.store.staff() if p.name == name)
    ctx = CommandContext(store=services.store, gmail=gmail, actor=actor, channel="Email",
                         sheet_url=SHEET, message_id=f"cmd-{next(_ids)}")  # fmt: skip
    result = run_command(command, ctx)
    services.store.load()
    return result


def sheet_edit(services, task_id: str, **values) -> None:
    """The Manager types into the Master tab (the Sheet), not through the bot."""
    services.store.update_task(task_id, {k.replace("_", " "): v for k, v in values.items()})
    services.store.load()


def tap(ctx, sender: str, data: str) -> str:
    message = WAMessage(id=f"wamid.{next(_ids)}", sender=sender, received_at=clock.now(),
                        kind="tap", tap=data)  # fmt: skip
    return handle_message(message, ctx)


def text(ctx, sender: str, body: str, context_id: str = "") -> str:
    message = WAMessage(id=f"wamid.{next(_ids)}", sender=sender, received_at=clock.now(),
                        kind="text", text=body, context_id=context_id)  # fmt: skip
    return handle_message(message, ctx)


# --- 1. the Manager's PROMPT and the one assignment email ---------------------------------------


def test_manager_prompt_for_an_intern_and_support_one_email_with_every_file_cc_owner_and_support():
    services, gmail = setup()
    task = prompt(services, gmail, "PROMPT: assign this to Staff B and Staff D",
                  attachments=[PDF, XLSX])  # fmt: skip
    assert (task.owner, task.doer, task.support) == ("Staff A", "Staff B", "Staff D")
    assert (task.status, task.acceptance) == ("Proposed", "Pending")
    assert changes(services, gmail).asked == 1
    [email] = assignment_emails(gmail)
    assert email.to == ["staff.b@example.com"]
    assert set(email.cc) == {"staff.a@example.com", "staff.d@example.com"}
    assert sorted(a.filename for a in email.attachments) == ["Data.xlsx", "Surat.pdf"]
    assert BODY in email.text and QUESTION in email.text and "Reply NO" in email.text
    assert email.subject.endswith("reply YES or NO")
    assert now(services, task.id).acceptance == "Asked"


def test_never_a_second_assignment_email_through_runs_yes_and_sheet_status_edits():
    services, gmail = setup()
    task, thread = emailed(services, gmail)
    for _ in range(3):
        assert changes(services, gmail).emailed == 0
    reply(gmail, thread, "Yes, I'll take it")
    process_inbox(gmail, services)
    assert now(services, task.id).status == "In Progress"
    for status in ("On Hold", "In Progress", "Not Started", "In Progress"):
        sheet_edit(services, task.id, Status=status)
        changes(services, gmail)
    assert len(assignment_emails(gmail, "staff.p@example.com")) == 1


def test_the_senders_reply_says_the_doer_is_asked_not_started():
    services, gmail = setup()
    prompt(services, gmail)
    text_ = gmail.replies[-1][1].text
    assert "asked YES or NO" in text_ and "started" not in text_.split("asked")[0]


# --- 2. YES ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    ["YES", "Yes, I'll take it", "Maaf baru balas, siap", "Ok noted", "Siap laksanakan",
     "Sorry for the late reply. Yes", "Baik, saya kerjakan"],
)  # fmt: skip
def test_yes_in_the_thread_starts_it_and_sends_nothing_else(body):
    services, gmail = setup()
    task, thread = emailed(services, gmail)
    sent_before, replies_before = len(gmail.sent), len(gmail.replies)
    reply(gmail, thread, body)
    process_inbox(gmail, services)
    saved = now(services, task.id)
    assert (saved.status, saved.acceptance) == ("In Progress", "Accepted")
    assert len(gmail.sent) == sent_before  # nothing to managers, owner or anyone
    new_replies = gmail.replies[replies_before:]
    assert len(new_replies) == 1 and new_replies[0][1].to == ["staff.p@example.com"]
    assert changes(services, gmail).emailed == 0


def test_a_typed_accept_in_a_new_email_with_a_lowercase_id():
    services, gmail = setup()
    task, _ = emailed(services, gmail)
    gmail.add("acc-1", sender_email="staff.p@example.com", subject="ok",
              body_text=f"accept {task.id.lower()}")  # fmt: skip
    process_inbox(gmail, services)
    assert now(services, task.id).status == "In Progress"


def test_a_second_yes_changes_nothing_and_logs_no_new_request():
    services, gmail = setup()
    task, thread = emailed(services, gmail)
    reply(gmail, thread, "Yes")
    process_inbox(gmail, services)
    tasks_before = len(services.store.tasks())
    reply(gmail, thread, "Yes")
    process_inbox(gmail, services)
    services.store.load()
    assert len(services.store.tasks()) == tasks_before
    assert now(services, task.id).status == "In Progress"


def test_whatsapp_outside_24h_template_then_terima_tap():
    services, gmail = setup(wa=True)
    ctx = wa_ctx(services, gmail)
    task = prompt(services, gmail)
    changes(services, gmail, ctx.wa)
    assert assignment_emails(gmail, "staff.p@example.com")  # the files go by email
    [(to, name, _, params)] = ctx.wa.templates
    assert (to, name, params[0]) == (P_WA, "grc_tugas_baru", task.id)
    assert ctx.wa.payloads[-1] == [f"acc:{task.id}", f"dec:{task.id}"]
    sent_before = len(gmail.sent)
    tap(ctx, P_WA, f"acc:{task.id}")
    saved = now(services, task.id)
    assert (saved.status, saved.acceptance) == ("In Progress", "Accepted")
    assert len(gmail.sent) == sent_before
    assert {to for to, _ in ctx.wa.texts} <= {P_WA}  # only the answer to the Doer


def test_whatsapp_inside_24h_buttons_and_typed_ya_in_reply():
    services, gmail = setup(wa=True)
    ctx = wa_ctx(services, gmail)
    text(ctx, P_WA, "help")  # opens the 24-hour window
    task = prompt(services, gmail)
    changes(services, gmail, ctx.wa)
    assert not ctx.wa.templates
    [(to, _, buttons)] = [b for b in ctx.wa.buttons if f"acc:{task.id}" in str(b)]
    assert to == P_WA and [label for _, label in buttons] == ["Terima", "Tolak"]
    ask_id = f"btn-{ctx.wa.sent_count}"
    text(ctx, P_WA, "ya", context_id=ask_id)
    assert now(services, task.id).status == "In Progress"


def test_whatsapp_ya_replying_to_the_template_accepts():
    services, gmail = setup(wa=True)
    ctx = wa_ctx(services, gmail)
    task = prompt(services, gmail)
    changes(services, gmail, ctx.wa)
    text(ctx, P_WA, "Ya, siap", context_id="tpl-1")
    assert now(services, task.id).status == "In Progress"


def test_whatsapp_ya_from_someone_else_replying_to_the_ask_does_nothing():
    services, gmail = setup(wa=True)
    ctx = wa_ctx(services, gmail)
    task = prompt(services, gmail)
    changes(services, gmail, ctx.wa)
    text(ctx, A_WA, "ya", context_id="tpl-1")
    assert now(services, task.id).acceptance == "Asked"


# --- 3. NO + reason, the manager notice, reassigning ---------------------------------------------


def decline_by_email(services, gmail, body: str) -> Task:
    task, thread = emailed(services, gmail)
    reply(gmail, thread, body)
    process_inbox(gmail, services)
    return now(services, task.id)


def manager_notices(gmail) -> list:
    return [e for e, _ in gmail.sent if "staff.k@example.com" in e.to and "declined" in e.subject]


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("No, I am on leave that week", "I am on leave that week"),
        ("No.\nOn leave next week", "On leave next week"),
        ("Tidak bisa, sedang cuti", "sedang cuti"),
        ("Sorry, I can't, on leave", "on leave"),
    ],
)
def test_no_with_a_reason_stays_proposed_declined_and_the_manager_is_told(body, reason):
    services, gmail = setup()
    saved = decline_by_email(services, gmail, body)
    assert (saved.status, saved.acceptance) == ("Proposed", "Declined")
    assert f"Declined by Staff P: {reason}" in saved.flags
    [notice] = manager_notices(gmail)
    assert f"assign {saved.id} to" in notice.text and reason in notice.text
    assert changes(services, gmail).emailed == 0


@pytest.mark.parametrize(
    "body",
    ["No, sorry.\nI am on leave next week", "Tidak bisa, maaf\nI am on leave next week"],
)
def test_a_reason_on_the_line_after_no_sorry_is_kept(body):
    services, gmail = setup()
    saved = decline_by_email(services, gmail, body)
    assert "Declined by Staff P: I am on leave next week" in saved.flags


@pytest.mark.parametrize(
    "body",
    ["No problem, I will do it", "Tidak masalah, siap", "Gak apa-apa, saya kerjakan",
     "No worries, will do"],
)  # fmt: skip
def test_no_problem_is_not_a_decline(body):
    services, gmail = setup()
    saved = decline_by_email(services, gmail, body)
    assert saved.acceptance != "Declined"
    assert not manager_notices(gmail)


def test_typed_decline_command_with_a_reason():
    services, gmail = setup()
    task, _ = emailed(services, gmail)
    gmail.add("dec-1", sender_email="staff.p@example.com", subject="re",
              body_text=f"decline {task.id} on leave until Friday")  # fmt: skip
    process_inbox(gmail, services)
    saved = now(services, task.id)
    assert saved.acceptance == "Declined"
    assert "Declined by Staff P: on leave until Friday" in saved.flags


def test_whatsapp_tolak_then_batal_keeps_it_asked_and_tolak_then_reason_declines():
    services, gmail = setup(wa=True)
    ctx = wa_ctx(services, gmail)
    task = prompt(services, gmail)
    changes(services, gmail, ctx.wa)
    tap(ctx, P_WA, f"dec:{task.id}")
    text(ctx, P_WA, "batal")
    assert now(services, task.id).acceptance == "Asked"
    tap(ctx, P_WA, f"dec:{task.id}")
    text(ctx, P_WA, "cuti sampai Jumat")
    saved = now(services, task.id)
    assert (saved.status, saved.acceptance) == ("Proposed", "Declined")
    assert "Declined by Staff P: cuti sampai Jumat" in saved.flags
    told = [body for to, body in ctx.wa.texts if to == K_WA and task.id in body]
    told += [str(b) for b in ctx.wa.buttons if b[0] == K_WA and task.id in b[1]]
    told += [str(t) for t in ctx.wa.templates if t[0] == K_WA]
    assert manager_notices(gmail) or told  # the Manager is on WhatsApp here


def test_a_bare_no_with_a_sign_off_does_not_use_the_sign_off_as_the_reason():
    services, gmail = setup()
    saved = decline_by_email(services, gmail, "No\n\nRegards,\nStaff P")
    assert saved.acceptance == "Declined"
    assert not any("Regards" in f or f.endswith(": Staff P") for f in saved.flags)


def test_every_manager_is_told_of_a_decline():
    data = make_backend()
    set_staff(data, "Staff O", Role="Manager")
    services, gmail = setup(data=data)
    saved = decline_by_email(services, gmail, "No, I am on leave that week")
    told = {to for e, _ in gmail.sent if "declined" in e.subject for to in e.to}
    assert told == {"staff.k@example.com", "staff.o@example.com"}
    assert saved.acceptance == "Declined"


def test_quoted_original_below_a_yes_or_no_is_ignored():
    quote = ("\n\nOn Mon, 26 Oct 2026 at 09:05, GRC <grc-bot@example.com> wrote:\n"
             "> Please answer this assignment:\n> Reply YES to start this task")  # fmt: skip
    services, gmail = setup()
    task, thread = emailed(services, gmail)
    reply(gmail, thread, "Yes" + quote)
    process_inbox(gmail, services)
    assert now(services, task.id).status == "In Progress"
    services, gmail = setup()
    saved = decline_by_email(services, gmail, "No" + quote)
    assert "Declined by Staff P: no reason given" in saved.flags


def declined(services, gmail) -> Task:
    return decline_by_email(services, gmail, "No, I am on leave that week")


def test_reassign_to_a_team_clears_the_flag_and_the_new_doer_gets_the_files_once():
    services, gmail = setup()
    task = declined(services, gmail)
    result = run(services, gmail, "Staff K", Command("assign", task.id, "Staff A and Staff D"))
    assert result.changed
    saved = now(services, task.id)
    assert (saved.doer, saved.support, saved.acceptance) == ("Staff A", "Staff D", "Pending")
    assert not any(f.startswith("Declined by") for f in saved.flags)
    assert changes(services, gmail).asked == 1
    assert changes(services, gmail).emailed == 0
    [email] = assignment_emails(gmail, "staff.a@example.com")
    assert email.cc == ["staff.d@example.com"] and QUESTION in email.text
    assert [a.filename for a in email.attachments] == ["Surat.pdf"]
    assert len(assignment_emails(gmail, "staff.p@example.com")) == 1  # no second to Staff P


def test_reassign_to_an_intern_makes_the_supervisor_owner_and_asks_the_intern():
    services, gmail = setup()
    task = declined(services, gmail)
    run(services, gmail, "Staff K", Command("assign", task.id, "Staff B"))
    saved = now(services, task.id)
    assert (saved.owner, saved.doer, saved.acceptance) == ("Staff A", "Staff B", "Pending")
    changes(services, gmail)
    [email] = assignment_emails(gmail, "staff.b@example.com")
    assert email.cc == ["staff.a@example.com"]


def test_reassign_to_the_same_person_asks_again():
    services, gmail = setup()
    task = declined(services, gmail)
    run(services, gmail, "Staff K", Command("assign", task.id, "Staff P"))
    saved = now(services, task.id)
    assert saved.acceptance == "Pending"
    assert not any(f.startswith("Declined by") for f in saved.flags)
    assert changes(services, gmail).asked == 1
    assert len(assignment_emails(gmail, "staff.p@example.com")) == 2


def test_reassign_to_the_manager_themself_starts_without_a_question():
    services, gmail = setup()
    task = declined(services, gmail)
    run(services, gmail, "Staff K", Command("assign", task.id, "me"))
    saved = now(services, task.id)
    assert (saved.doer, saved.status, saved.acceptance) == ("Staff K", "Not Started", "Accepted")
    assert not any(f.startswith("Declined by") for f in saved.flags)
    changes(services, gmail)
    assert all(QUESTION not in e.text for e in assignment_emails(gmail, "staff.k@example.com"))


def test_only_the_manager_reassigns_a_declined_task():
    services, gmail = setup()
    task = declined(services, gmail)
    result = run(services, gmail, "Staff P", Command("assign", task.id, "Staff D"))
    assert not result.changed
    assert now(services, task.id).doer == "Staff P"


def test_reassign_while_unanswered_and_the_old_doers_yes_is_refused():
    services, gmail = setup()
    task, thread = emailed(services, gmail)
    run(services, gmail, "Staff K", Command("assign", task.id, "Staff D"))
    assert changes(services, gmail).asked == 1
    reply(gmail, thread, "Yes")  # Staff P, in the old thread
    process_inbox(gmail, services)
    saved = now(services, task.id)
    assert (saved.doer, saved.status, saved.acceptance) == ("Staff D", "Proposed", "Asked")


def test_after_a_decline_the_manager_changing_doer_and_status_in_the_sheet_emails_once():
    services, gmail = setup()
    task = declined(services, gmail)
    sheet_edit(services, task.id, Doer="Staff D", Owner="Staff D", Status="Not Started")
    changes(services, gmail)
    changes(services, gmail)
    [email] = assignment_emails(gmail, "staff.d@example.com")
    assert [a.filename for a in email.attachments] == ["Surat.pdf"]


def test_a_doer_changed_in_the_sheet_while_asked_gets_the_files_before_answering():
    services, gmail = setup()
    task, _ = emailed(services, gmail)
    sheet_edit(services, task.id, Doer="Staff D", Owner="Staff D")
    changes(services, gmail)
    [email] = assignment_emails(gmail, "staff.d@example.com")
    assert QUESTION in email.text and [a.filename for a in email.attachments] == ["Surat.pdf"]


# --- 4. who may assign (section 26) -------------------------------------------------------------


def test_a_full_timer_prompt_for_their_own_intern_is_asked():
    services, gmail = setup()
    task = prompt(services, gmail, "PROMPT: assign this to Staff B", sender="staff.a@example.com")
    assert (task.doer, task.status, task.acceptance) == ("Staff B", "Proposed", "Pending")
    assert changes(services, gmail).asked == 1
    [email] = assignment_emails(gmail, "staff.b@example.com")
    assert email.cc == ["staff.a@example.com"]


def test_a_full_timer_prompt_for_a_peer_waits_for_the_manager_and_nobody_is_emailed():
    services, gmail = setup()
    task = prompt(services, gmail, "PROMPT: assign this to Staff P", sender="staff.d@example.com")
    assert (task.status, task.acceptance) == ("Proposed", "")
    assert any(f.startswith("PROMPT: waiting for the manager") for f in task.flags)
    changes(services, gmail)
    assert not assignment_emails(gmail)


def test_a_full_timer_prompt_for_someone_elses_intern_waits():
    services, gmail = setup()
    task = prompt(services, gmail, "PROMPT: assign this to Staff B", sender="staff.d@example.com")
    assert (task.status, task.acceptance) == ("Proposed", "")
    changes(services, gmail)
    assert not assignment_emails(gmail)


@pytest.mark.parametrize("sender", ["staff.c@example.com", "staff.r@example.com"])
def test_an_intern_or_additional_help_prompt_is_ignored_and_nobody_is_asked(sender):
    services, gmail = setup()
    task = prompt(services, gmail, "PROMPT: assign this to Staff P", sender=sender)
    assert (task.status, task.acceptance) == ("Proposed", "")
    assert "PROMPT ignored: only a Manager or full-time staff can assign work" in task.flags
    changes(services, gmail)
    assert not assignment_emails(gmail)


@pytest.mark.parametrize(
    ("sender", "name"),
    [("staff.k@example.com", "Staff K"), ("staff.a@example.com", "Staff A")],
)
def test_naming_yourself_starts_with_no_question(sender, name):
    services, gmail = setup()
    task = prompt(services, gmail, f"PROMPT: assign this to {name}", sender=sender)
    assert (task.status, task.acceptance) == ("Not Started", "Accepted")
    changes(services, gmail)
    assert all(QUESTION not in e.text for e in assignment_emails(gmail))


@pytest.mark.parametrize(
    "line",
    ["PROMPT: assign this to Staff P or Staff D", "PROMPT: assign this to Staff Zed"],
)
def test_an_unclear_manager_prompt_waits_and_nobody_is_asked(line):
    services, gmail = setup()
    task = prompt(services, gmail, line)
    assert (task.status, task.acceptance) == ("Proposed", "")
    changes(services, gmail)
    assert not assignment_emails(gmail)


@pytest.mark.parametrize("name", ["Staff R", "Staff O"])
def test_is_additional_help_or_is_a_manager_does_not_hold_a_manager_prompt(name):
    data = make_backend()
    set_staff(data, "Staff O", Role="Manager")
    services, gmail = setup(data=data)
    task = prompt(services, gmail, f"PROMPT: assign this to {name}")
    assert (task.doer, task.status, task.acceptance) == (name, "Proposed", "Pending")
    assert changes(services, gmail).asked == 1


def test_an_intern_whose_internship_ends_before_the_due_date_waits():
    data = make_backend()
    set_staff(data, "Staff B", **{"Internship end date": "2026-11-01"})
    services, gmail = setup(data=data)
    task = prompt(services, gmail, "PROMPT: assign this to Staff B")
    assert (task.status, task.acceptance) == ("Proposed", "")
    changes(services, gmail)
    assert not assignment_emails(gmail)


def late_prompt(services, gmail, line: str) -> tuple[WaContext, Task]:
    ctx = wa_ctx(services, gmail)
    handle_message(WAMessage(id="late-1", sender=K_WA, received_at=clock.now(), kind="text",
                             text=BODY), ctx)  # fmt: skip
    assert finalize("late-1", ctx) == "logged"
    later = clock.now() + timedelta(minutes=2)
    handle_message(WAMessage(id="late-2", sender=K_WA, received_at=later, kind="text",
                             text=line), ctx, now=later)  # fmt: skip
    services.store.load()
    return ctx, next(t for t in services.store.tasks() if t.source_id == "late-1")


def test_a_late_whatsapp_manager_prompt_asks_yes_or_no_like_any_prompt():
    services, gmail = setup(wa=True)
    _, task = late_prompt(services, gmail, "PROMPT: assign this to Staff D")
    assert (task.doer, task.status, task.acceptance) == ("Staff D", "Proposed", "Pending")
    assert changes(services, gmail).asked == 1


# --- 5. without the Acceptance column (section 26) ---------------------------------------------


def test_without_the_column_it_starts_the_email_has_no_question_and_accept_is_refused():
    services, gmail = setup(acceptance=False)
    task = prompt(services, gmail, "PROMPT: assign this to Staff P and Staff D")
    assert task.status == "Not Started"
    assert "Assigned to Staff P; started" in gmail.replies[-1][1].text
    run_ = changes(services, gmail)
    assert (run_.emailed, run_.asked) == (1, 0)
    [email] = assignment_emails(gmail, "staff.p@example.com")
    assert QUESTION not in email.text and email.cc == ["staff.d@example.com"]
    assert not email.subject.endswith("reply YES or NO")
    result = run(services, gmail, "Staff P", Command("accept", task.id))
    assert not result.changed and now(services, task.id).status == "Not Started"


# --- 6. working hours, holidays, paused ---------------------------------------------------------


def test_on_a_holiday_the_email_waits_pending_then_goes_on_the_next_workday():
    data = make_backend()
    set_config(data, "Holidays", "2026-10-26")
    services, gmail = setup(data=data)
    task = prompt(services, gmail)
    run_ = changes(services, gmail)
    assert (run_.waiting, run_.asked) == (1, 0)
    assert now(services, task.id).acceptance == "Pending"
    assert not assignment_emails(gmail)
    with clock.freeze(datetime(2026, 10, 27, 9, 0)):
        assert changes(services, gmail).asked == 1
    assert now(services, task.id).acceptance == "Asked"


def test_in_the_evening_it_stays_pending_then_the_morning_run_asks_once():
    services, gmail = setup()
    with clock.freeze(datetime(2026, 10, 26, 20, 30)):
        task = prompt(services, gmail)
        assert changes(services, gmail).waiting == 1
        assert now(services, task.id).acceptance == "Pending"
    with clock.freeze(datetime(2026, 10, 27, 8, 2)):
        assert changes(services, gmail).asked == 1
        assert changes(services, gmail).asked == 0
    assert len(assignment_emails(gmail, "staff.p@example.com")) == 1


def test_a_paused_doer_is_not_emailed_until_resumed():
    data = make_backend()
    set_staff(data, "Staff P", **{"Reminders paused": "Yes"})
    services, gmail = setup(data=data)
    task = prompt(services, gmail)
    changes(services, gmail)
    assert not assignment_emails(gmail)
    assert now(services, task.id).acceptance == "Pending"
    run(services, gmail, "Staff K", Command("resume", None, "Staff P"))
    assert changes(services, gmail).asked == 1
    assert now(services, task.id).acceptance == "Asked"


# --- 7. a Gmail send failure ----------------------------------------------------------------------


def test_a_failed_send_keeps_it_pending_and_the_retry_sends_exactly_once():
    services, gmail = setup()
    task = prompt(services, gmail)
    gmail.fail_send = True
    run_ = changes(services, gmail)
    assert (run_.not_sent, run_.asked) == (1, 0)
    saved = now(services, task.id)
    assert (saved.status, saved.acceptance) == ("Proposed", "Pending")
    assert "accept_ask" not in str(services.store.backend.tabs["_outbox"])
    gmail.fail_send = False
    assert changes(services, gmail).asked == 1
    assert changes(services, gmail).asked == 0
    assert len(assignment_emails(gmail, "staff.p@example.com")) == 1
    assert now(services, task.id).acceptance == "Asked"


def test_a_failed_whatsapp_ask_does_not_stop_the_email_or_lose_the_answer_by_email():
    services, gmail = setup(wa=True)
    ctx = wa_ctx(services, gmail)
    ctx.wa.fail_send = True
    task, thread = emailed(services, gmail, wa=ctx.wa)
    assert now(services, task.id).acceptance == "Asked"
    reply(gmail, thread, "yes")
    process_inbox(gmail, services)
    assert now(services, task.id).status == "In Progress"


# --- 8. interaction with other features ---------------------------------------------------------


def test_a_progress_reply_in_the_assignment_thread_gets_the_hint_and_changes_nothing():
    services, gmail = setup()
    task, thread = emailed(services, gmail)
    reply(gmail, thread, "70%")
    process_inbox(gmail, services)
    saved = now(services, task.id)
    assert (saved.status, saved.acceptance, saved.progress) == ("Proposed", "Asked", task.progress)
    assert "Reply YES to accept" in gmail.replies[-1][1].text


def test_a_progress_command_before_answering_is_refused_then_yes_still_works():
    services, gmail = setup()
    task, thread = emailed(services, gmail)
    result = run(services, gmail, "Staff P", Command("progress", task.id, "50"))
    assert not result.changed
    reply(gmail, thread, "Yes")
    process_inbox(gmail, services)
    assert now(services, task.id).status == "In Progress"


def test_the_manager_approving_in_the_sheet_while_pending_sends_one_plain_email():
    services, gmail = setup()
    task = prompt(services, gmail)
    sheet_edit(services, task.id, Status="Not Started")
    changes(services, gmail)
    changes(services, gmail)
    [email] = assignment_emails(gmail, "staff.p@example.com")
    assert QUESTION not in email.text


def test_the_manager_approving_in_the_sheet_after_asked_sends_no_second_email():
    services, gmail = setup()
    task, thread = emailed(services, gmail)
    sheet_edit(services, task.id, Status="Not Started")
    changes(services, gmail)
    assert len(assignment_emails(gmail, "staff.p@example.com")) == 1
    tasks_before = len(services.store.tasks())
    reply(gmail, thread, "Yes")  # the Doer answers the (now moot) question
    process_inbox(gmail, services)
    services.store.load()
    assert len(services.store.tasks()) == tasks_before  # not logged as a new request
    assert now(services, task.id).status == "Not Started"


# --- 9. privacy -----------------------------------------------------------------------------------


def test_the_whole_flow_logs_no_names_emails_or_text(caplog):
    caplog.set_level(logging.DEBUG, logger="grc")
    services, gmail = setup(wa=True)
    ctx = wa_ctx(services, gmail)
    task = prompt(services, gmail)
    changes(services, gmail, ctx.wa)
    tap(ctx, P_WA, f"dec:{task.id}")
    text(ctx, P_WA, "cuti sampai Jumat")
    run(services, gmail, "Staff K", Command("assign", task.id, "Staff D"))
    changes(services, gmail, ctx.wa)
    thread = ask_thread(services, task.id)
    reply(gmail, thread, "Yes, I'll take it", sender="staff.d@example.com")
    process_inbox(gmail, services)
    assert now(services, task.id).status == "In Progress"
    banned = ["Staff ", "@example.com", "5550", "cuti", "stress test", "Surat", "take it"]
    for record in caplog.records:
        blob = (
            record.getMessage()
            + " "
            + " ".join(str(v) for k, v in record.__dict__.items() if k not in _STD)
        )
        for word in banned:
            assert word not in blob, (record.msg, word)
