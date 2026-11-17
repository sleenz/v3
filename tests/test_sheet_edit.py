"""The Manager changes the sheet by prompt (DESIGN section 29). Fakes only.

Staff K is the Manager (staff.k@example.com, WhatsApp set here). Monday 26 Oct 2026 09:00.
"""

from datetime import date

from app import clock
from app.commands import Command
from app.extract.ai import NullProvider
from app.gmail import process_inbox
from app.lifecycle import CommandContext, run_command
from app.models import Task
from app.pipeline import Services
from app.sheet_edit import Change, by_rules, check, preview
from app.whatsapp import WaContext, WAMessage, handle_message
from tests.conftest import TODAY
from tests.fakes import (
    FakeAIProvider,
    FakeDriveStore,
    FakeGmailClient,
    FakeQueue,
    FakeSheetsBackend,
    FakeSheetStore,
    FakeWhatsApp,
)

K_WA = "85255500011"


def setup(ai=None):
    data = FakeSheetsBackend.from_fixtures()
    staff = data.tabs["Staff"]
    head = staff[0]
    name_at, wa_at = head.index("Name"), head.index("WhatsApp")
    head += ["Reports to", "WhatsApp opt-in"]
    for row in staff[1:]:
        row += [""] * (len(head) - 2 - len(row))
        if row[name_at] == "Staff K":
            row[wa_at] = "+852 5550 0011"
        row += ["", "Yes"]
    for row in data.tabs["Config"]:
        if row and row[0] == "Allowed senders":
            row[1] = "@example.com"
        if row and row[0] == "AI provider" and ai is not None:
            row[1] = "Gemini API key"
    store = FakeSheetStore(data)
    store.load()
    services = Services(store=store, drive=FakeDriveStore(), ai=ai or NullProvider())
    task, _ = store.append_task(Task.model_validate(
        {"Title": "Stress test", "Owner": "Staff P", "Doer": "Staff P", "Status": "In Progress",
         "Due date": date(2026, 11, 20), "Priority": "Medium", "Source ID": "src-1"}))  # fmt: skip
    store.load()
    return services, FakeGmailClient(), task


def now_task(services, task_id: str) -> Task:
    services.store.load()
    return next(t for t in services.store.tasks() if t.id == task_id)


def email(gmail, services, message_id: str, body: str, sender="staff.k@example.com", **extra):
    gmail.add(message_id, sender_email=sender, subject=extra.pop("subject", "Edit"),
              body_text=body, **extra)  # fmt: skip
    services.store.load()
    process_inbox(gmail, services)


# --- reading and checking ------------------------------------------------------------------------


def test_the_fixed_form():
    changes = by_rules("edit G-0001 Priority = High\nedit staff Staff A Reports to = Staff K\n"
                       "edit config Reminder time morning = 09:30\nhello")  # fmt: skip
    assert [c.tab for c in changes] == ["Master", "Staff", "Config"]
    services, _, _ = setup()
    good, problems = check(changes, services.store, TODAY)
    assert [(c.tab, c.key, c.column, c.value) for c in good] == [
        ("Master", "G-0001", "Priority", "High"),
        ("Staff", "Staff A", "Reports to", "Staff K"),
        ("Config", "Reminder time morning", "Value", "09:30"),
    ] and problems == []
    assert by_rules("please edit the report") is None


def test_check_reads_values_and_refuses_what_it_cannot_write():
    services, _, task = setup()
    asked = [
        Change(tab="Master", key=task.id, column="due date", value="5 Dec"),
        Change(tab="Master", key=task.id, column="Progress", value="70"),
        Change(tab="Master", key=task.id, column="Status", value="on hold"),
        Change(tab="Master", key="G-0999", column="Priority", value="High"),
        Change(tab="Master", key=task.id, column="Colour", value="red"),
        Change(tab="Master", key=task.id, column="ID", value="G-0100"),
        Change(tab="Master", key=task.id, column="Status", value="Finished"),
        Change(tab="Staff", key="staff a", column="Reports to", value="Staff K"),
        Change(tab="Staff", key="Nobody", column="Reports to", value="Staff K"),
    ]
    good, problems = check(asked, services.store, TODAY)
    assert [(c.key, c.column, c.before, c.value) for c in good] == [
        (task.id, "Due date", "2026-11-20", "2026-12-05"),
        (task.id, "Progress", "", "0.7"),
        (task.id, "Status", "In Progress", "On Hold"),
        ("Staff A", "Reports to", "", "Staff K"),
    ]
    assert len(problems) == 5
    assert any("G-0999" in p for p in problems) and any("Colour" in p for p in problems)
    assert any("ID is not changed" in p for p in problems)
    assert preview(good[:1]) == f"{task.id} · Due date: 2026-11-20 → 2026-12-05"


# --- by email ------------------------------------------------------------------------------------


def test_a_manager_edit_by_email_is_shown_then_applied_on_yes():
    services, gmail, task = setup()
    email(gmail, services, "e1", f"edit {task.id} Priority = High\n"
                                 "edit config Reminder time morning = 09:30")  # fmt: skip
    ask = gmail.replies[-1][1].text
    assert f"{task.id} · Priority: Medium → High" in ask and "Reply YES" in ask
    assert "Reply NO to cancel" in ask  # the fixed form: NO only cancels
    assert now_task(services, task.id).priority == "Medium"
    email(gmail, services, "e2", "YES", thread_id="thread-e1", subject="Re: Edit")
    assert now_task(services, task.id).priority == "High"
    assert str(services.store.config().reminder_time_morning) == "09:30:00"
    audit = services.store.backend.tabs["AuditLog"]
    assert any("Prompt edit (Master)" in str(row) and "High" in str(row) for row in audit)
    assert "Changed:" in gmail.replies[-1][1].text


def test_the_ai_reads_plain_words_for_staff_and_master():
    changes = [
        {"tab": "Master", "key": "G-0001", "column": "Due date", "value": "2026-12-05"},
        {"tab": "Staff", "key": "Staff A", "column": "Reports to", "value": "Staff K"},
    ]
    ai = FakeAIProvider(edit={"is_edit": True, "confidence": 0.9, "reply_to_user": "ok",
                              "changes": changes})  # fmt: skip
    services, gmail, task = setup(ai)
    email(gmail, services, "e1", "ubah deadline G-0001 jadi 5 Des, dan Staff A lapor ke Staff K")
    ask = gmail.replies[-1][1].text
    assert "Due date: 2026-11-20 → 2026-12-05" in ask and "Staff Staff A · Reports to" in ask
    assert "new request instead" in ask  # read by the AI from a new email
    email(gmail, services, "e2", "ya", thread_id="thread-e1", subject="Re: Edit")
    assert now_task(services, task.id).due_date == date(2026, 12, 5)
    assert next(p for p in services.store.staff() if p.name == "Staff A").reports_to == "Staff K"


def test_a_request_the_ai_does_not_read_as_an_edit_is_logged_as_before():
    ai = FakeAIProvider(edit={"is_edit": False, "changes": [], "reply_to_user": "",
                              "confidence": 0.9})  # fmt: skip
    services, gmail, _ = setup(ai)
    email(gmail, services, "e1", "Mohon siapkan laporan stress test untuk OJK", subject="Laporan")
    services.store.load()
    assert services.store.task_by_source_id("e1") is not None


def test_only_the_manager_edits_and_forwards_are_never_edits():
    services, gmail, task = setup()
    email(gmail, services, "e1", f"edit {task.id} Priority = High", sender="staff.p@example.com")
    email(gmail, services, "e2", f"edit {task.id} Priority = High", subject="Fwd: x")
    assert now_task(services, task.id).priority == "Medium"
    assert not any("Priority: Medium → High" in r[1].text for r in gmail.replies)


def test_nothing_valid_gets_the_reasons():
    services, gmail, _ = setup()
    email(gmail, services, "e1", "edit G-0999 Priority = High")
    assert "is not a task in Master" in gmail.replies[-1][1].text


def test_undo_after_a_master_edit():
    services, gmail, task = setup()
    email(gmail, services, "e1", f"edit {task.id} Priority = High")
    email(gmail, services, "e2", "YES", thread_id="thread-e1", subject="Re: Edit")
    assert now_task(services, task.id).priority == "High"
    manager = next(p for p in services.store.staff() if p.name == "Staff K")
    services.store.load()
    ctx = CommandContext(store=services.store, gmail=gmail, actor=manager, channel="Email",
                         message_id="undo-1")  # fmt: skip
    assert run_command(Command("undo", task.id), ctx).changed
    assert now_task(services, task.id).priority == "Medium"


# --- on WhatsApp ---------------------------------------------------------------------------------


def test_a_manager_edit_on_whatsapp_with_ya():
    ai = FakeAIProvider(edit={"is_edit": True, "confidence": 0.9, "reply_to_user": "ok",
                              "changes": [{"tab": "Master", "key": "G-0001", "column": "Status",
                                           "value": "On Hold"}]})  # fmt: skip
    services, gmail, task = setup(ai)
    ctx = WaContext(services=services, wa=FakeWhatsApp(), queue=FakeQueue(), gmail=gmail)
    outcome = handle_message(WAMessage(id="k1", sender=K_WA, received_at=clock.now(),
                                       kind="text", text="G-0001 di-hold dulu"), ctx)  # fmt: skip
    assert outcome == "edit_proposed"
    assert "Status: In Progress → On Hold" in ctx.wa.buttons[-1][1]
    handle_message(WAMessage(id="k2", sender=K_WA, received_at=clock.now(), kind="tap",
                             tap="cf:yes"), ctx)  # fmt: skip
    assert now_task(services, task.id).status == "On Hold"


def test_people_columns_take_staff_names_only():
    services, _, task = setup()
    asked = [Change(tab="Master", key=task.id, column="Doer", value="staff a"),
             Change(tab="Master", key=task.id, column="Support", value="Staff D and staff b"),
             Change(tab="Master", key=task.id, column="Reviewer", value="Zorro")]  # fmt: skip
    good, problems = check(asked, services.store, TODAY)
    assert [(c.column, c.value) for c in good] == [("Doer", "Staff A"),
                                                   ("Support", "Staff D; Staff B")]  # fmt: skip
    assert problems == ['G-0001 Reviewer: "Zorro" is not in Staff']


def test_the_manager_cannot_change_his_own_email_or_role_by_prompt():
    services, _, _ = setup()
    asked = [Change(tab="Staff", key="Staff K", column="Email", value="typo@example.com"),
             Change(tab="Staff", key="Staff K", column="Role", value="Full time"),
             Change(tab="Staff", key="Staff A", column="Role", value="Intern")]  # fmt: skip
    good, problems = check(asked, services.store, TODAY, actor="Staff K")
    assert [(c.key, c.column) for c in good] == [("Staff A", "Role")]
    assert len(problems) == 2 and all("your own" in p for p in problems)
