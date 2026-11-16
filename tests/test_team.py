"""Tasks with several people: one Doer plus Support (DESIGN section 25). Fakes only.

Made-up names. Fixture Staff: Staff K is the Manager; Staff A and Staff P are full time.
"""

from datetime import date

import pytest

from app.commands import Command
from app.extract.ai import NullProvider
from app.gmail import process_inbox
from app.lifecycle import CommandContext, run_command, task_status
from app.models import Attachment, StaffMember, Task
from app.notify import assignment_email
from app.pipeline import Services, decide_prompt
from app.reminders import DUE_TODAY, due_today_triggers, morning_triggers
from app.route import joined_groups
from app.wa_ui import card_buttons
from tests.conftest import TODAY
from tests.fakes import (
    FakeAIProvider,
    FakeDriveStore,
    FakeGmailClient,
    FakeSheetsBackend,
    FakeSheetStore,
)

PDF = Attachment(filename="a.pdf", mime="application/pdf", data=b"%PDF")
TEAM = [
    StaffMember(name="Ana Putri"),
    StaffMember(name="Budi Santoso"),
    StaffMember(name="Budi Hartono"),
    StaffMember(name="Rina Wijaya"),
]


# --- reading the PROMPT --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "groups"),
    [
        ("assign this to ana and rina", [["Ana Putri", "Rina Wijaya"]]),
        ("assign to ana, budi hartono dan rina", [["Ana Putri", "Budi Hartono", "Rina Wijaya"]]),
        ("ana sama rina ya", [["Ana Putri", "Rina Wijaya"]]),
        ("give it to ana or rina", [["Ana Putri"], ["Rina Wijaya"]]),
        ("ana does it, rina checks the numbers", [["Ana Putri"], ["Rina Wijaya"]]),
        ("budi hartono and budi santoso", [["Budi Hartono", "Budi Santoso"]]),
    ],
)
def test_joined_groups(text, groups):
    assert [[p.name for p in g] for g in joined_groups(text, TEAM)] == groups


@pytest.mark.parametrize(
    ("prompt", "owner", "support"),
    [
        ("assign this to ana and rina", "Ana Putri", ["Rina Wijaya"]),
        ("assign to rina, ana & budi hartono", "Rina Wijaya", ["Ana Putri", "Budi Hartono"]),
        ("assign ana and rina with reviewer budi santoso", "Ana Putri", ["Rina Wijaya"]),
        ("assign to ana, or Rina if she is busy", "Ana Putri", []),  # an alternative
        ("assign this to ana", "Ana Putri", []),
    ],
)
def test_the_first_name_is_the_doer_the_rest_support(prompt, owner, support):
    decision = decide_prompt(prompt, TODAY, TEAM, NullProvider())
    assert (decision.owner, decision.support) == (owner, support)


def test_the_ai_owner_keeps_the_others_as_support():
    ai = FakeAIProvider(reading={"owner": "Rina"})
    decision = decide_prompt("assign to ana and rina", TODAY, TEAM, ai)
    assert (decision.owner, decision.support) == ("Rina Wijaya", ["Ana Putri"])


def test_supporters_are_read_from_the_column():
    task = Task.model_validate({"Doer": "Ana Putri", "Support": "Rina Wijaya; Ana Putri, Budi X; "})
    assert task.supporters == ["Rina Wijaya", "Budi X"]


# --- in the service ------------------------------------------------------------------------------


def setup():
    backend = FakeSheetsBackend.from_fixtures()
    for row in backend.tabs["Config"]:
        if row[0] == "Allowed senders":
            row[1] = "@example.com"
    store = FakeSheetStore(backend)
    store.load()
    return Services(store=store, drive=FakeDriveStore(), ai=NullProvider()), FakeGmailClient()


def person(store, name: str) -> StaffMember:
    return next(p for p in store.staff() if p.name == name)


def add(services, **values) -> Task:
    base = {"Title": "Stress test", "Owner": "Staff P", "Doer": "Staff P", "Support": "Staff A",
            "Status": "In Progress", "Due date": TODAY, "Source ID": "src-1"}  # fmt: skip
    saved, _ = services.store.append_task(Task.model_validate({**base, **values}))
    services.store.load()
    return saved


def run(services, gmail, name: str, command: Command):
    services.store.load()
    message_id = f"msg-{name.replace(' ', '')}-{command.name}"  # real ids have no spaces
    ctx = CommandContext(store=services.store, gmail=gmail, actor=person(services.store, name),
                         channel="Email", message_id=message_id)  # fmt: skip
    result = run_command(command, ctx)
    services.store.load()
    return result


def current(services, task_id: str) -> Task:
    services.store.load()
    return next(t for t in services.store.tasks() if t.id == task_id)


def test_a_prompt_with_two_names_sets_doer_and_support():
    services, gmail = setup()
    gmail.add("req-1", sender_email="staff.k@example.com", subject="Laporan",
              body_text="PROMPT: assign this to Staff P and Staff A\n\nMohon kirim laporan.",
              attachments=[PDF])  # fmt: skip
    process_inbox(gmail, services)
    services.store.load()
    [task] = [t for t in services.store.tasks() if t.source_id == "req-1"]
    assert (task.doer, task.supporters) == ("Staff P", ["Staff A"])


def test_the_assignment_email_goes_to_the_doer_cc_support():
    services, _ = setup()
    task = add(services)
    email = assignment_email(task, services.store.staff(), "")
    assert email.to == ["staff.p@example.com"] and "staff.a@example.com" in email.cc
    assert "Support: Staff A" in email.text and "Staff P sends it for review" in email.text


def test_support_gets_due_today_and_last_call_but_not_the_morning_message():
    services, _ = setup()
    task = add(services, **{"Due date": TODAY})
    staff = services.store.staff()
    today = {t.person for t in due_today_triggers([task], staff, TODAY, DUE_TODAY)}
    assert "Staff A" in today and "Staff P" in today
    config = services.store.config()
    morning = morning_triggers([task.model_copy(update={"due_date": date(2026, 11, 2)})], staff,
                               TODAY, config)  # fmt: skip
    assert {t.person for t in morning} <= {"Staff P"}


def test_support_can_update_progress_notes_and_blocked_but_not_review_or_done():
    services, gmail = setup()
    task = add(services)
    assert run(services, gmail, "Staff A", Command("progress", task.id, "60")).changed
    assert current(services, task.id).progress_value == 0.6
    assert run(services, gmail, "Staff A", Command("note", task.id, "data received")).changed
    refused = run(services, gmail, "Staff A", Command("review", task.id))
    assert not refused.changed and "only Staff P can do that" in refused.reply
    assert not run(services, gmail, "Staff A", Command("done", task.id)).changed
    assert run(services, gmail, "Staff A", Command("blocked", task.id, "waiting")).changed
    assert current(services, task.id).status == "On Hold"


def test_someone_else_still_cannot_update():
    services, gmail = setup()
    task = add(services)
    result = run(services, gmail, "Staff B", Command("progress", task.id, "60"))
    assert not result.changed and "not the Owner or Doer" in result.reply


def test_the_manager_assigns_a_team():
    services, gmail = setup()
    task = add(services, Support="")
    result = run(services, gmail, "Staff K", Command("assign", task.id, "Staff A and Staff P"))
    saved = current(services, task.id)
    assert result.changed and (saved.doer, saved.supporters) == ("Staff A", ["Staff P"])
    assert "Staff P supporting" in result.reply
    refused = run(services, gmail, "Staff P", Command("assign", task.id, "Staff P and Staff B"))
    assert not refused.changed and "Only the manager" in refused.reply


def test_lists_status_and_buttons_show_support():
    services, gmail = setup()
    task = add(services)
    mine = run(services, gmail, "Staff A", Command("mine"))
    assert f"{task.id} – Stress test" in mine.reply and "support)" in mine.reply
    assert "support Staff A" in task_status(task, services.store.staff(), TODAY)
    buttons = [title for _, title in card_buttons(task, person(services.store, "Staff A"),
                                                  services.store.staff())]  # fmt: skip
    assert buttons == ["Update progres", "Lainnya"]
