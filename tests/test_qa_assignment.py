"""QA scenarios that try to break the assignment journeys (DESIGN sections 19, 21, 23, 25).

Fakes only, made-up people from tests/fixtures/staff.json. Staff K is the Manager; Staff A, D, P
are full time; Staff B is an intern who reports to Staff A (added here), Staff E an intern who
reports to Staff K. The clock is frozen on Monday 26 Oct 2026, 09:00 Asia/Hong_Kong; the fixture
Workday start is 08:00 and Workday end the default 19:00.

Tests marked xfail(strict=True) expose behaviour that contradicts DESIGN.md (QA-A<n>).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app import clock, outbox
from app.commands import parse_command
from app.extract.ai import NullProvider
from app.gmail import process_inbox
from app.jobs import ChangesRun, run_changes
from app.lifecycle import CommandContext, CommandResult, run_command
from app.models import PROGRESS_LOG, Attachment, Task
from app.pipeline import Services
from app.wa_ui import touch_window
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

SHEET = "https://docs.google.com/spreadsheets/d/sheet-qa"
P_WA = "85255500016"  # Staff P (fixture, fake)
A_WA = "85255500001"  # Staff A (fixture, fake)
K_WA = "85255500011"  # Staff K, added here (fake)
PDF = Attachment(filename="Surat.pdf", mime="application/pdf", data=b"%PDF letter")
XLSX = Attachment(
    filename="Data.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    data=b"xlsx-bytes",
)
REPORTS_TO = {"Staff B": "Staff A", "Staff E": "Staff K"}
BODY = "Mohon kirim laporan stress test, deadline 20 November 2026."


# --- setup ---------------------------------------------------------------------------------------


def backend(
    storage: str = "Drive", holidays: str = "", wa_opt_in: bool = False, ai_on: bool = False
) -> FakeSheetsBackend:
    data = FakeSheetsBackend.from_fixtures()
    master = data.tabs["Master"]
    master[0].append("Acceptance")
    for row in master[1:]:
        row.append("")
    staff = data.tabs["Staff"]
    head = staff[0]
    name_at, wa_at = head.index("Name"), head.index("WhatsApp")
    width = len(head)
    head += ["Reports to", "WhatsApp opt-in"]
    for row in staff[1:]:
        row += [""] * (width - len(row))
        if row[name_at] == "Staff K":
            row[wa_at] = "+852 5550 0011"
        row += [REPORTS_TO.get(row[name_at], ""), "Yes" if wa_opt_in else ""]
    for row in data.tabs["Config"]:
        if row and row[0] == "File storage":
            row[1] = storage
        if row and row[0] == "AI provider" and ai_on:
            row[1] = "Gemini API key"
    if holidays:
        data.tabs["Config"].append(["Holidays", holidays])
    return data


def setup(storage: str = "Drive", holidays: str = "", wa_opt_in: bool = False, ai=None):
    store = FakeSheetStore(backend(storage, holidays, wa_opt_in, ai_on=ai is not None))
    store.load()
    services = Services(store=store, drive=FakeDriveStore(), ai=ai or NullProvider())
    gmail = FakeGmailClient()
    run_changes(store, gmail, SHEET)  # the first run only records the baseline
    return services, gmail


def wa_ctx(services, gmail) -> WaContext:
    return WaContext(services=services, wa=FakeWhatsApp(), queue=FakeQueue(), gmail=gmail,
                     sheet_url=SHEET)  # fmt: skip


def manager_assigns(
    services,
    gmail,
    prompt: str = "PROMPT: assign this to Staff P",
    sender: str = "staff.k@example.com",
    message_id: str = "req-1",
    body: str = BODY,
    attachments: list[Attachment] | None = None,
) -> Task:
    gmail.add(message_id, sender_email=sender, subject="Laporan stress test",
              body_text=f"{prompt}\n\n{body}",
              attachments=[PDF] if attachments is None else attachments)  # fmt: skip
    process_inbox(gmail, services)
    services.store.load()
    return next(t for t in services.store.tasks() if t.source_id == message_id)


def task_now(services, task_id: str) -> Task:
    services.store.load()
    return next(t for t in services.store.tasks() if t.id == task_id)


def changes(services, gmail, wa=None) -> ChangesRun:
    services.store.load()
    return run_changes(services.store, gmail, SHEET, wa)


def emails_to(gmail, address: str) -> list:
    return [email for email, _ in gmail.sent if address in email.to]


def asks_to(gmail, address: str) -> list:
    return [e for e in emails_to(gmail, address) if "please accept" in e.subject]


def assignments_to(gmail, address: str) -> list:
    return [e for e in emails_to(gmail, address) if e.text.startswith("You have been assigned")]


def ask_rows(services, person: str | None = None) -> list:
    services.store.load()
    return [
        r
        for r in outbox.rows(services.store)
        if r.kind == "accept_ask" and (person is None or r.person == person)
    ]


def ask_thread(services, person: str) -> str:
    return ask_rows(services, person)[-1].thread_id


def reply(gmail, message_id: str, thread: str, body: str, sender: str = "staff.p@example.com"):
    gmail.add(message_id, thread_id=thread, sender_email=sender, subject="Re: please accept",
              body_text=body)  # fmt: skip


def cmd(services, gmail, actor: str, text: str, channel="Email", wa=None, message_id: str = ""):
    """Run a typed command directly (the email YES step for changes is tested elsewhere)."""
    store = services.store
    store.load()
    command = parse_command(text)
    assert command is not None, text
    person = next(p for p in store.staff() if p.name == actor)
    return run_command(command, CommandContext(store=store, gmail=gmail, actor=person,
                       channel=channel, sheet_url=SHEET, message_id=message_id, wa=wa))  # fmt: skip


def tap(value: str, sender: str = P_WA, suffix: str = "") -> WAMessage:
    return WAMessage(id=f"wamid.{value}{suffix}", sender=sender, received_at=clock.now(),
                     kind="tap", tap=value)  # fmt: skip


def wa_text(body: str, sender: str = P_WA, context_id: str = "", mid: str = "") -> WAMessage:
    return WAMessage(id=mid or f"wamid.t-{body[:10]}", sender=sender, received_at=clock.now(),
                     kind="text", text=body, context_id=context_id)  # fmt: skip


def asked_by_email(services, gmail, **kwargs) -> tuple[Task, str]:
    task = manager_assigns(services, gmail, **kwargs)
    assert changes(services, gmail).asked == 1
    return task, ask_thread(services, task.doer)


# --- 1. A Manager's PROMPT naming an intern ------------------------------------------------------


def test_intern_under_a_full_timer_is_doer_and_asked_owner_is_cc_after_yes():
    services, gmail = setup()
    task = manager_assigns(services, gmail, prompt="PROMPT: assign this to Staff B")
    assert (task.owner, task.doer, task.status, task.acceptance) == (
        "Staff A",
        "Staff B",
        "Proposed",
        "Pending",
    )
    assert "waiting for Staff B to accept" in gmail.replies[0][1].text
    assert changes(services, gmail).asked == 1
    assert len(asks_to(gmail, "staff.b@example.com")) == 1
    assert not asks_to(gmail, "staff.a@example.com")  # only the Doer is asked
    # the Owner (supervisor) cannot answer for the intern
    owner_try = cmd(services, gmail, "Staff A", f"accept {task.id}")
    assert "Only Staff B" in owner_try.reply
    assert task_now(services, task.id).acceptance == "Asked"
    reply(gmail, "b-yes", ask_thread(services, "Staff B"), "Ya, siap", "staff.b@example.com")
    process_inbox(gmail, services)
    assert task_now(services, task.id).status == "Not Started"
    gmail.sent.clear()
    assert changes(services, gmail).emailed == 1
    [assignment] = assignments_to(gmail, "staff.b@example.com")
    assert assignment.cc == ["staff.a@example.com"]
    assert [a.filename for a in assignment.attachments] == ["Surat.pdf"]


def test_intern_under_the_manager_is_owner_and_doer_and_asked():
    services, gmail = setup()
    task = manager_assigns(services, gmail, prompt="PROMPT: assign this to Staff E")
    assert (task.owner, task.doer, task.acceptance) == ("Staff E", "Staff E", "Pending")
    assert changes(services, gmail).asked == 1
    assert asks_to(gmail, "staff.e@example.com")


# --- 2. Typos, reviewer and deadline in a Manager's PROMPT ---------------------------------------


def test_a_typo_in_the_name_still_asks_the_right_person_and_is_flagged():
    services, gmail = setup()
    task = manager_assigns(services, gmail, prompt="PROMPT: assign this to Staf P")
    assert (task.doer, task.acceptance) == ("Staff P", "Pending")
    assert any('"Staf P" read as Staff P' in f for f in task.flags)


def test_reviewer_and_counted_deadline_with_acceptance():
    services, gmail = setup()
    task = manager_assigns(
        services, gmail, prompt="PROMPT: assign this to Staff P with reviewer Staff D "
        "deadline 3 days from now"
    )  # fmt: skip
    assert (task.doer, task.reviewer, task.needs_review) == ("Staff P", "Staff D", "Yes")
    assert task.due_date == date(2026, 10, 29)  # beats the 20 Nov in the message
    assert task.acceptance == "Pending"
    changes(services, gmail)
    [ask] = asks_to(gmail, "staff.p@example.com")
    assert "Thu 29 Oct 2026" in ask.subject


def test_a_counted_deadline_on_a_weekend_moves_to_friday():
    services, gmail = setup()
    task = manager_assigns(services, gmail, prompt="PROMPT: assign this to Staff P in 5 days")
    assert task.due_date == date(2026, 10, 30)  # Sat 31 Oct -> Fri 30 Oct (section 21)
    assert (task.doer, task.acceptance) == ("Staff P", "Pending")


def test_names_joined_by_or_are_unclear_so_no_acceptance():
    services, gmail = setup()
    task = manager_assigns(services, gmail, prompt="PROMPT: assign this to Staff P or Staff A")
    assert (task.status, task.acceptance) == ("Proposed", "")
    assert changes(services, gmail).asked == 0


def test_an_unknown_name_leaves_the_task_for_the_manager():
    services, gmail = setup()
    task = manager_assigns(services, gmail, prompt="PROMPT: assign this to Nobody Here")
    assert (task.status, task.acceptance) == ("Proposed", "")
    assert "Waiting for manager approval" in gmail.replies[0][1].text


def test_manager_prompt_with_team_asks_only_the_doer_and_cc_support_after_yes():
    services, gmail = setup()
    task = manager_assigns(services, gmail, prompt="PROMPT: assign this to Staff P and Staff D")
    assert (task.doer, task.supporters, task.acceptance) == ("Staff P", ["Staff D"], "Pending")
    changes(services, gmail)
    assert asks_to(gmail, "staff.p@example.com") and not asks_to(gmail, "staff.d@example.com")
    cmd(services, gmail, "Staff P", f"accept {task.id}")
    gmail.sent.clear()
    changes(services, gmail)
    [assignment] = assignments_to(gmail, "staff.p@example.com")
    assert "staff.d@example.com" in assignment.cc
    assert [a.filename for a in assignment.attachments] == ["Surat.pdf"]


# --- 3. A non-Manager's PROMPT: the manager approves ---------------------------------------------


def test_staff_prompt_waits_for_approval_then_sheet_approval_sends_the_assignment():
    services, gmail = setup()
    task = manager_assigns(services, gmail, sender="staff.a@example.com")
    assert (task.status, task.acceptance) == ("Proposed", "")
    assert changes(services, gmail).asked == 0
    # nobody can "accept" a task that is not waiting for an answer
    assert "not waiting" in cmd(services, gmail, "Staff P", f"accept {task.id}").reply
    # the manager approves in the Sheet
    services.store.update_task(task.id, {"Status": "Not Started"})
    run = changes(services, gmail)
    assert (run.asked, run.emailed) == (0, 1)
    assert assignments_to(gmail, "staff.p@example.com")


def test_manager_assign_on_a_plain_proposed_task_is_refused():
    services, gmail = setup()
    task = manager_assigns(services, gmail, sender="staff.a@example.com")
    result = cmd(services, gmail, "Staff K", f"assign {task.id} to Staff D")
    assert "waiting for the manager's approval" in result.reply
    assert task_now(services, task.id).doer == "Staff P"


# --- 4. Ask once per Doer; reassign ---------------------------------------------------------------


def test_reassign_while_pending_asks_the_new_doer_once_and_ignores_the_old_doers_yes():
    services, gmail = setup()
    task, old_thread = asked_by_email(services, gmail)
    result = cmd(services, gmail, "Staff K", f"assign {task.id} to Staff A")
    assert "asked to accept" in result.reply
    assert changes(services, gmail).asked == 1
    assert changes(services, gmail).asked == 0
    assert len(asks_to(gmail, "staff.a@example.com")) == 1
    # Staff P answers the old question late
    reply(gmail, "p-late", old_thread, "Yes")
    process_inbox(gmail, services)
    saved = task_now(services, task.id)
    assert (saved.doer, saved.status, saved.acceptance) == ("Staff A", "Proposed", "Asked")
    assert "Only Staff A" in gmail.replies[-1][1].text


def test_reassign_back_to_an_earlier_doer_asks_them_again():
    services, gmail = setup()
    task, _ = asked_by_email(services, gmail)  # Staff P asked
    cmd(services, gmail, "Staff K", f"assign {task.id} to Staff A")
    changes(services, gmail)  # Staff A asked
    cmd(services, gmail, "Staff K", f"assign {task.id} to Staff P")
    assert task_now(services, task.id).doer == "Staff P"
    assert changes(services, gmail).asked == 1  # section 23: "The new person is asked."


def test_old_doer_tapping_terima_after_reassign_is_refused():
    services, gmail = setup(wa_opt_in=True)
    ctx = wa_ctx(services, gmail)
    task = manager_assigns(services, gmail)
    changes(services, gmail, ctx.wa)
    cmd(services, gmail, "Staff K", f"assign {task.id} to Staff D")
    handle_message(tap(f"acc:{task.id}"), ctx)
    saved = task_now(services, task.id)
    assert (saved.doer, saved.acceptance) == ("Staff D", "Pending")
    assert "Only Staff D" in ctx.wa.last_text(P_WA)


def test_a_paused_doer_is_asked_only_after_resume():
    services, gmail = setup()
    staff = services.store.backend.tabs["Staff"]
    staff[0].append("Reminders paused")
    for row in staff[1:]:
        row.append("Yes" if row[1] == "Staff P" else "")
    manager_assigns(services, gmail)
    assert changes(services, gmail).asked == 0
    for row in staff[1:]:
        if row[1] == "Staff P":
            row[-1] = ""
    assert changes(services, gmail).asked == 1


def test_without_the_outbox_tab_the_doer_is_still_asked_once():
    services, gmail = setup()
    del services.store.backend.tabs["_outbox"]
    manager_assigns(services, gmail)
    for _ in range(3):
        changes(services, gmail)
    assert len(asks_to(gmail, "staff.p@example.com")) == 1


# --- 5. Answers when nothing is pending; wrong person --------------------------------------------


def test_accept_twice_says_not_waiting_and_sends_one_assignment():
    services, gmail = setup()
    task, _ = asked_by_email(services, gmail)
    assert cmd(services, gmail, "Staff P", f"accept {task.id}").changed
    second = cmd(services, gmail, "Staff P", f"accept {task.id}")
    assert not second.changed and "not waiting" in second.reply
    gmail.sent.clear()
    changes(services, gmail)
    changes(services, gmail)
    assert len(assignments_to(gmail, "staff.p@example.com")) == 1


@pytest.mark.parametrize("status", ["Cancelled", "Done"])
def test_accept_on_a_closed_task_changes_nothing(status):
    services, gmail = setup()
    task, _ = asked_by_email(services, gmail)
    services.store.update_task(task.id, {"Status": status})  # closed in the Sheet
    result = cmd(services, gmail, "Staff P", f"accept {task.id}")
    assert task_now(services, task.id).status == status
    assert not result.changed


def test_decline_without_reason_is_recorded_as_no_reason_given():
    services, gmail = setup()
    task, _ = asked_by_email(services, gmail)
    result = cmd(services, gmail, "Staff P", f"decline {task.id}")
    assert result.changed
    saved = task_now(services, task.id)
    assert saved.acceptance == "Declined"
    assert "Declined by Staff P: no reason given" in saved.flags


def test_decline_twice_tells_the_manager_once():
    services, gmail = setup()
    task, _ = asked_by_email(services, gmail)
    cmd(services, gmail, "Staff P", f"decline {task.id} sedang cuti")
    second = cmd(services, gmail, "Staff P", f"decline {task.id} masih cuti")
    assert not second.changed
    notices = [e for e in emails_to(gmail, "staff.k@example.com") if "declined" in e.subject]
    assert len(notices) == 1
    assert task_now(services, task.id).flags == ["Declined by Staff P: sedang cuti"]


def test_a_stranger_cannot_decline():
    services, gmail = setup()
    task, _ = asked_by_email(services, gmail)
    result = cmd(services, gmail, "Staff D", f"decline {task.id} bukan tugas saya")
    assert "Only Staff P" in result.reply
    assert task_now(services, task.id).acceptance == "Asked"


def test_even_the_manager_cannot_accept_for_the_doer():
    services, gmail = setup()
    task, _ = asked_by_email(services, gmail)
    result = cmd(services, gmail, "Staff K", f"accept {task.id}")
    assert "Only Staff P" in result.reply
    assert task_now(services, task.id).status == "Proposed"


# --- 6. The same Gmail message twice ------------------------------------------------------------


def unread_again(gmail, message_id: str) -> None:
    gmail.labels(message_id).update({"INBOX", "UNREAD"})


def test_the_original_request_processed_twice_makes_one_task_and_one_ask():
    services, gmail = setup()
    task = manager_assigns(services, gmail)
    unread_again(gmail, "req-1")
    run = process_inbox(gmail, services)
    assert run.duplicates == 1
    services.store.load()
    assert [t.id for t in services.store.tasks() if t.source_id == "req-1"] == [task.id]
    assert "waiting for Staff P to accept" in gmail.replies[-1][1].text
    changes(services, gmail)
    changes(services, gmail)
    assert len(asks_to(gmail, "staff.p@example.com")) == 1


def test_the_same_yes_twice_sends_one_assignment():
    services, gmail = setup()
    task, thread = asked_by_email(services, gmail)
    reply(gmail, "yes-1", thread, "Yes")
    process_inbox(gmail, services)
    unread_again(gmail, "yes-1")
    process_inbox(gmail, services)
    saved = task_now(services, task.id)
    assert (saved.status, saved.acceptance) == ("Not Started", "Accepted")
    gmail.sent.clear()
    changes(services, gmail)
    changes(services, gmail)
    assert len(assignments_to(gmail, "staff.p@example.com")) == 1
    services.store.load()
    assert [t.id for t in services.store.tasks() if t.source_id == "yes-1"] == []


def test_the_same_no_twice_tells_the_manager_once():
    services, gmail = setup()
    task, thread = asked_by_email(services, gmail)
    reply(gmail, "no-1", thread, "No, cuti minggu depan")
    process_inbox(gmail, services)
    unread_again(gmail, "no-1")
    process_inbox(gmail, services)
    notices = [e for e in emails_to(gmail, "staff.k@example.com") if "declined" in e.subject]
    assert len(notices) == 1
    assert task_now(services, task.id).flags == ["Declined by Staff P: cuti minggu depan"]


# --- 7. Typed accept / decline; "terima kasih" -------------------------------------------------


def test_typed_terima_by_email_accepts():
    services, gmail = setup()
    task = manager_assigns(services, gmail)
    gmail.add("t1", sender_email="staff.p@example.com", subject="ok", body_text=f"terima {task.id}")
    process_inbox(gmail, services)
    assert task_now(services, task.id).status == "Not Started"


def test_typed_decline_with_reason_by_email():
    services, gmail = setup()
    task = manager_assigns(services, gmail)
    gmail.add("d1", sender_email="staff.p@example.com", subject="re",
              body_text=f"decline {task.id} sedang cuti")  # fmt: skip
    process_inbox(gmail, services)
    saved = task_now(services, task.id)
    assert saved.acceptance == "Declined"
    assert "Declined by Staff P: sedang cuti" in saved.flags


def test_terima_kasih_in_the_ask_thread_is_not_an_accept():
    services, gmail = setup()
    task, thread = asked_by_email(services, gmail)
    reply(gmail, "tk", thread, "Terima kasih")
    process_inbox(gmail, services)
    assert task_now(services, task.id).acceptance == "Asked"


def test_terima_kasih_with_the_id_is_not_an_accept():
    services, gmail = setup()
    task = manager_assigns(services, gmail)
    gmail.add("tk2", sender_email="staff.p@example.com", subject=f"Re: [GRC] {task.id}",
              body_text=f"terima {task.id} kasih")  # fmt: skip
    process_inbox(gmail, services)
    assert task_now(services, task.id).acceptance == "Pending"


def test_a_natural_yes_sentence_in_the_ask_thread_accepts():
    services, gmail = setup()
    task, thread = asked_by_email(services, gmail)
    reply(gmail, "yes-long", thread, "Yes, I will take it.")
    process_inbox(gmail, services)
    assert task_now(services, task.id).status == "Not Started"


def test_email_no_with_the_reason_on_the_next_line_keeps_the_reason():
    services, gmail = setup()
    task, thread = asked_by_email(services, gmail)
    reply(gmail, "no-2", thread, "No.\nI am on leave from 27 Oct to 6 Nov.")
    process_inbox(gmail, services)
    saved = task_now(services, task.id)
    assert saved.acceptance == "Declined"
    assert any("on leave" in f for f in saved.flags)


def test_a_polite_yes_starting_with_maaf_is_not_a_decline():
    services, gmail = setup()
    task, thread = asked_by_email(services, gmail)
    reply(gmail, "maaf", thread, "Maaf baru balas. Siap, saya ambil.")
    process_inbox(gmail, services)
    assert task_now(services, task.id).acceptance != "Declined"


def test_typed_terima_and_decline_on_whatsapp():
    services, gmail = setup(wa_opt_in=True)
    ctx = wa_ctx(services, gmail)
    first = manager_assigns(services, gmail)
    second = manager_assigns(services, gmail, message_id="req-2")
    handle_message(wa_text(f"terima {first.id}"), ctx)
    handle_message(wa_text(f"decline {second.id} sedang cuti"), ctx)
    assert task_now(services, first.id).status == "Not Started"
    saved = task_now(services, second.id)
    assert saved.acceptance == "Declined" and "Declined by Staff P: sedang cuti" in saved.flags


def test_terima_kasih_on_whatsapp_in_reply_to_the_ask_is_not_an_accept():
    services, gmail = setup(wa_opt_in=True)
    ctx = wa_ctx(services, gmail)
    task = manager_assigns(services, gmail)
    changes(services, gmail, ctx.wa)
    ask_id = ask_rows(services, "Staff P")[-1].message_id
    handle_message(wa_text("Terima kasih", context_id=ask_id), ctx)
    assert task_now(services, task.id).acceptance == "Asked"


@pytest.mark.xfail(
    strict=True,
    reason="QA-A5: typed 'ya' replying to the WhatsApp ask is caught by the deadline-YES check",
)
def test_typed_ya_in_reply_to_the_whatsapp_ask_accepts():
    services, gmail = setup(wa_opt_in=True)
    ctx = wa_ctx(services, gmail)
    task = manager_assigns(services, gmail)
    changes(services, gmail, ctx.wa)
    ask_id = ask_rows(services, "Staff P")[-1].message_id
    handle_message(wa_text("ya", context_id=ask_id), ctx)
    assert task_now(services, task.id).status == "Not Started"


def test_typed_no_with_reason_in_reply_to_the_whatsapp_ask_declines():
    services, gmail = setup(wa_opt_in=True)
    ctx = wa_ctx(services, gmail)
    task = manager_assigns(services, gmail)
    changes(services, gmail, ctx.wa)
    ask_id = ask_rows(services, "Staff P")[-1].message_id
    handle_message(wa_text("tidak bisa, sedang cuti", context_id=ask_id), ctx)
    saved = task_now(services, task.id)
    assert saved.acceptance == "Declined" and "Declined by Staff P: sedang cuti" in saved.flags


# --- 8. WhatsApp taps ----------------------------------------------------------------------------


def test_inside_the_window_the_ask_has_terima_and_tolak_buttons():
    services, gmail = setup(wa_opt_in=True)
    ctx = wa_ctx(services, gmail)
    task = manager_assigns(services, gmail)
    touch_window(services.store, "Staff P")
    changes(services, gmail, ctx.wa)
    [(to, body, buttons)] = ctx.wa.buttons
    assert to == P_WA and task.id in body
    assert buttons == [(f"acc:{task.id}", "Terima"), (f"dec:{task.id}", "Tolak")]
    assert ctx.wa.templates == []
    assert not asks_to(gmail, "staff.p@example.com")


def test_whatsapp_send_failure_falls_back_to_email_once():
    services, gmail = setup(wa_opt_in=True)
    ctx = wa_ctx(services, gmail)
    manager_assigns(services, gmail)
    ctx.wa.fail_send = True
    assert changes(services, gmail, ctx.wa).asked == 1
    assert len(asks_to(gmail, "staff.p@example.com")) == 1
    assert changes(services, gmail, ctx.wa).asked == 0


def test_a_tap_from_someone_else_changes_nothing():
    services, gmail = setup(wa_opt_in=True)
    ctx = wa_ctx(services, gmail)
    task = manager_assigns(services, gmail)
    handle_message(tap(f"acc:{task.id}", sender=A_WA), ctx)
    assert "Only Staff P" in ctx.wa.last_text(A_WA)
    handle_message(tap(f"dec:{task.id}", sender=A_WA), ctx)
    handle_message(wa_text("bukan saya", sender=A_WA), ctx)
    saved = task_now(services, task.id)
    assert (saved.status, saved.acceptance, saved.flags) == ("Proposed", "Pending", [])


def test_tolak_then_batal_keeps_the_question_open():
    services, gmail = setup(wa_opt_in=True)
    ctx = wa_ctx(services, gmail)
    task = manager_assigns(services, gmail)
    handle_message(tap(f"dec:{task.id}"), ctx)
    handle_message(wa_text("batal"), ctx)
    assert "Dibatalkan" in ctx.wa.last_text(P_WA)
    assert task_now(services, task.id).acceptance == "Pending"
    assert not [e for e in emails_to(gmail, "staff.k@example.com") if "declined" in e.subject]
    handle_message(tap(f"acc:{task.id}"), ctx)  # can still accept afterwards
    assert task_now(services, task.id).status == "Not Started"


def test_tolak_with_no_reason_within_30_minutes_expires_and_changes_nothing():
    services, gmail = setup(wa_opt_in=True)
    ctx = wa_ctx(services, gmail)
    task = manager_assigns(services, gmail)
    handle_message(tap(f"dec:{task.id}"), ctx)
    later = clock.now() + timedelta(minutes=31)
    handle_message(wa_text("cuti"), ctx, now=later)
    assert any("sudah habis" in t for n, t in ctx.wa.texts if n == P_WA)
    assert task_now(services, task.id).acceptance == "Pending"


def test_the_same_terima_tap_delivered_twice_applies_once():
    services, gmail = setup(wa_opt_in=True)
    ctx = wa_ctx(services, gmail)
    task = manager_assigns(services, gmail)
    handle_message(tap(f"acc:{task.id}"), ctx)
    handle_message(tap(f"acc:{task.id}"), ctx)
    assert "Already done" in ctx.wa.last_text(P_WA)
    assert services.store.backend.column(PROGRESS_LOG, "Field").count("Acceptance") == 1


def test_tolak_reason_tells_every_manager_with_the_assign_hint():
    services, gmail = setup(wa_opt_in=True)
    ctx = wa_ctx(services, gmail)
    task = manager_assigns(services, gmail)
    handle_message(tap(f"dec:{task.id}"), ctx)
    handle_message(wa_text("cuti sampai Jumat"), ctx)
    # Staff K opted in to WhatsApp here, so the notice goes there (template outside the window)
    told = [p for to, _, _, p in ctx.wa.templates if to == K_WA] + [
        t for n, t in ctx.wa.texts if n == K_WA
    ]
    told_email = [e for e in emails_to(gmail, "staff.k@example.com") if "declined" in e.subject]
    assert told or told_email
    text = " ".join(str(x) for x in told) + " ".join(e.text for e in told_email)
    assert "cuti sampai Jumat" in text and f"assign {task.id} to" in text


# --- 9. Working hours and holidays --------------------------------------------------------------


def test_ask_waits_before_workday_start_and_goes_at_start():
    services, gmail = setup()
    manager_assigns(services, gmail)
    with clock.freeze(datetime(2026, 10, 26, 7, 59)):
        assert changes(services, gmail).waiting == 1
    with clock.freeze(datetime(2026, 10, 26, 8, 0)):
        assert changes(services, gmail).asked == 1


def test_ask_waits_at_exactly_workday_end():
    services, gmail = setup()
    manager_assigns(services, gmail)
    with clock.freeze(datetime(2026, 10, 26, 19, 0)):
        run = changes(services, gmail)
    assert (run.asked, run.waiting) == (0, 1)


def test_ask_waits_on_a_config_holiday_and_goes_next_working_day():
    services, gmail = setup(holidays="2026-10-26")
    manager_assigns(services, gmail)
    with clock.freeze(datetime(2026, 10, 26, 10, 0)):
        assert changes(services, gmail).waiting == 1
    assert not asks_to(gmail, "staff.p@example.com")
    with clock.freeze(datetime(2026, 10, 27, 8, 0)):
        assert changes(services, gmail).asked == 1


def test_ask_waits_over_the_weekend():
    services, gmail = setup()
    manager_assigns(services, gmail)
    with clock.freeze(datetime(2026, 10, 31, 10, 0)):
        assert changes(services, gmail).waiting == 1
    with clock.freeze(datetime(2026, 11, 2, 8, 0)):
        assert changes(services, gmail).asked == 1


def test_accept_in_the_evening_sends_the_assignment_next_morning():
    services, gmail = setup(wa_opt_in=True)
    ctx = wa_ctx(services, gmail)
    task = manager_assigns(services, gmail)
    with clock.freeze(datetime(2026, 10, 26, 20, 0)):
        handle_message(tap(f"acc:{task.id}"), ctx)
        assert "in working hours" in ctx.wa.last_text(P_WA)
        assert changes(services, gmail).waiting == 1
    assert not assignments_to(gmail, "staff.p@example.com")
    with clock.freeze(datetime(2026, 10, 27, 8, 0)):
        assert changes(services, gmail).emailed == 1


def test_decline_outside_hours_queues_the_manager_notice():
    services, gmail = setup()
    task, _ = asked_by_email(services, gmail)
    with clock.freeze(datetime(2026, 10, 26, 21, 0)):
        cmd(services, gmail, "Staff P", f"decline {task.id} cuti")
    assert not [e for e in emails_to(gmail, "staff.k@example.com") if "declined" in e.subject]
    queued = services.store.backend.column("_event_queue", "Kind")
    assert "declined" in queued


# --- 10. The files follow the YES ----------------------------------------------------------------


def test_with_no_file_storage_the_ask_names_the_files_and_the_assignment_attaches_them():
    services, gmail = setup(storage="None")
    task = manager_assigns(services, gmail, attachments=[PDF, XLSX])
    assert task.files == "In the original email in the GRC bot mailbox: Surat.pdf, Data.xlsx"
    changes(services, gmail)
    [ask] = asks_to(gmail, "staff.p@example.com")
    assert ask.attachments == [] and "Surat.pdf, Data.xlsx" in ask.text
    cmd(services, gmail, "Staff P", f"accept {task.id}")
    gmail.sent.clear()
    changes(services, gmail)
    [assignment] = assignments_to(gmail, "staff.p@example.com")
    assert [a.filename for a in assignment.attachments] == ["Surat.pdf", "Data.xlsx"]


def test_with_drive_the_ask_links_the_folder_and_the_assignment_attaches_the_files():
    services, gmail = setup(storage="Drive")
    task = manager_assigns(services, gmail, attachments=[PDF, XLSX])
    assert task.files.startswith("https://drive.google.com/")
    changes(services, gmail)
    [ask] = asks_to(gmail, "staff.p@example.com")
    assert task.files in ask.text and ask.attachments == []
    cmd(services, gmail, "Staff P", f"accept {task.id}")
    gmail.sent.clear()
    changes(services, gmail)
    [assignment] = assignments_to(gmail, "staff.p@example.com")
    assert sorted(a.filename for a in assignment.attachments) == ["Data.xlsx", "Surat.pdf"]


def wa_manager_request(services, gmail, prompt: str) -> tuple[WaContext, Task]:
    ctx = WaContext(services=services, wa=FakeWhatsApp({"media-k2": b"%PDF surat"}),
                    queue=FakeQueue(), gmail=gmail, sheet_url=SHEET)  # fmt: skip
    handle_message(wa_text(BODY, sender=K_WA, mid="k1"), ctx)
    media = [WAMedia(id="media-k2", filename="Surat.pdf", mime="application/pdf")]
    handle_message(WAMessage(id="k2", sender=K_WA, received_at=clock.now(), kind="media",
                             text=prompt, media=media), ctx)  # fmt: skip
    assert finalize("k1", ctx) == "logged"
    services.store.load()
    return ctx, next(t for t in services.store.tasks() if t.source_id == "k1")


def test_whatsapp_manager_prompt_without_deadline_keeps_files_for_the_assignment():
    services, gmail = setup(storage="None")
    ctx, task = wa_manager_request(services, gmail, "PROMPT: assign this to Staff P")
    assert (task.doer, task.acceptance) == ("Staff P", "Pending")
    assert "waiting for Staff P to accept" in ctx.wa.last_text(K_WA)
    cmd(services, gmail, "Staff P", f"accept {task.id}")
    changes(services, gmail)
    [assignment] = assignments_to(gmail, "staff.p@example.com")
    assert [a.filename for a in assignment.attachments] == ["Surat.pdf"]


@pytest.mark.xfail(
    strict=True,
    reason="QA-A6: WhatsApp files are not kept when the PROMPT has a deadline, so none follow YES",
)
def test_whatsapp_manager_prompt_with_deadline_keeps_files_for_the_assignment():
    services, gmail = setup(storage="None")
    _, task = wa_manager_request(services, gmail, "PROMPT: assign this to Staff P deadline besok")
    assert task.acceptance == "Pending"
    cmd(services, gmail, "Staff P", f"accept {task.id}")
    changes(services, gmail)
    [assignment] = assignments_to(gmail, "staff.p@example.com")
    assert [a.filename for a in assignment.attachments] == ["Surat.pdf"]


# --- 11. The Manager takes a declined task -------------------------------------------------------


def test_manager_reassigns_a_declined_task_to_themselves():
    services, gmail = setup()
    task, _ = asked_by_email(services, gmail)
    cmd(services, gmail, "Staff P", f"decline {task.id} cuti")
    result = cmd(services, gmail, "Staff K", f"assign {task.id} to me")
    assert "yours and started" in result.reply
    saved = task_now(services, task.id)
    assert (saved.owner, saved.doer, saved.status, saved.acceptance) == (
        "Staff K",
        "Staff K",
        "Not Started",
        "Accepted",
    )
    assert not any(f.startswith("Declined by") for f in saved.flags)
    run = changes(services, gmail)
    assert run.asked == 0 and run.emailed == 1  # the usual assignment email, no ask


def test_manager_reassigns_a_declined_task_to_an_intern_and_the_intern_is_asked():
    services, gmail = setup()
    task, _ = asked_by_email(services, gmail)
    cmd(services, gmail, "Staff P", f"decline {task.id} cuti")
    cmd(services, gmail, "Staff K", f"assign {task.id} to Staff B")
    saved = task_now(services, task.id)
    assert (saved.owner, saved.doer, saved.acceptance) == ("Staff A", "Staff B", "Pending")
    assert changes(services, gmail).asked == 1
    assert asks_to(gmail, "staff.b@example.com")


def test_manager_reassigns_a_declined_task_to_a_team():
    services, gmail = setup()
    task, _ = asked_by_email(services, gmail)
    cmd(services, gmail, "Staff P", f"decline {task.id} cuti")
    cmd(services, gmail, "Staff K", f"assign {task.id} to Staff A and Staff D")
    saved = task_now(services, task.id)
    assert (saved.doer, saved.supporters, saved.acceptance) == ("Staff A", ["Staff D"], "Pending")
    changes(services, gmail)
    assert asks_to(gmail, "staff.a@example.com") and not asks_to(gmail, "staff.d@example.com")


def test_a_non_manager_cannot_reassign_a_declined_task():
    services, gmail = setup()
    task, _ = asked_by_email(services, gmail)
    cmd(services, gmail, "Staff P", f"decline {task.id} cuti")
    result = cmd(services, gmail, "Staff P", f"assign {task.id} to Staff A")
    assert "waits for the manager" in result.reply
    assert task_now(services, task.id).doer == "Staff P"


# --- 12. What the texts say next ------------------------------------------------------------------


def test_the_manager_naming_themselves_is_told_it_started():
    services, gmail = setup()
    manager_assigns(services, gmail, prompt="PROMPT: assign this to Staff K")
    assert "Assigned by the manager to Staff K; started." in gmail.replies[0][1].text


def test_status_says_waiting_for_the_doer_then_shows_the_decline():
    services, gmail = setup()
    task, _ = asked_by_email(services, gmail)
    pending = cmd(services, gmail, "Staff D", f"status {task.id}")
    assert "Waiting for Staff P to accept." in pending.reply
    cmd(services, gmail, "Staff P", f"decline {task.id} cuti")
    declined = cmd(services, gmail, "Staff D", f"status {task.id}")
    assert "Waiting for the manager's approval." in declined.reply
    assert "Declined by Staff P: cuti" in declined.reply


def test_accept_and_decline_replies_say_what_happens_next():
    services, gmail = setup()
    first = manager_assigns(services, gmail)
    second = manager_assigns(services, gmail, message_id="req-2")
    yes = cmd(services, gmail, "Staff P", f"accept {first.id}")
    assert "assignment email with the files follows" in yes.reply
    no: CommandResult = cmd(services, gmail, "Staff P", f"decline {second.id} cuti")
    assert "manager is told" in no.reply


def test_ask_email_says_how_to_answer():
    services, gmail = setup()
    task, _ = asked_by_email(services, gmail)
    [ask] = asks_to(gmail, "staff.p@example.com")
    assert ask.subject.startswith(f"[GRC] {task.id} – please accept: ")
    assert "Reply YES" in ask.text and "Reply NO and the reason" in ask.text


def test_ai_owner_differs_from_the_rules_and_the_ai_person_is_asked():
    """Section 21: the AI's owner wins and is flagged; section 23 then asks that person."""
    services, gmail = setup(ai=FakeAIProvider(assignee="Staff A"))
    prompt = "PROMPT: assign this to Staff P, or Staff A if she is busy"
    task = manager_assigns(services, gmail, prompt=prompt)
    assert (task.doer, task.acceptance) == ("Staff A", "Pending")
    assert any("owner unclear (rules read Staff P)" in f for f in task.flags)
