"""QA v1.6 agent C: trying to break the Manager's sheet edits by prompt (DESIGN section 29).

Fakes only, made-up people: Staff K is the Manager (staff.k@example.com, WhatsApp set here),
Staff P is full time, Staff B an intern. Clock: Monday 26 Oct 2026 09:00 Asia/Hong_Kong.
A test marked xfail(strict=True) shows a real finding (QA6-C<n>) against docs/DESIGN.md.
"""

import logging
from datetime import date, datetime

import pytest

from app import clock, outbox, progress
from app.commands import Command
from app.extract.ai import NullProvider
from app.gmail import process_inbox
from app.lifecycle import CommandContext, run_command
from app.models import Task
from app.pipeline import Services
from app.sheet_edit import MAX_CHANGES, Change, apply, by_rules, check
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

K = "staff.k@example.com"
K_WA = "85255500011"
P_WA = "85255500016"
B_WA = "85255500002"


def ai_edit(changes, confidence=0.9, is_edit=True):
    return FakeAIProvider(edit={"is_edit": is_edit, "confidence": confidence,
                                "reply_to_user": "ok", "changes": changes})  # fmt: skip


def setup(ai=None, master_extra=()):
    data = FakeSheetsBackend.from_fixtures()
    staff = data.tabs["Staff"]
    head = staff[0]
    name_at, wa_at = head.index("Name"), head.index("WhatsApp")
    head += ["Reports to", "WhatsApp opt-in"]
    for row in staff[1:]:
        row += [""] * (len(head) - 2 - len(row))
        if row[name_at] == "Staff K":
            row[wa_at] = "+852 5550 0011"
        if row[name_at] == "Staff B":
            row[wa_at] = "+852 5550 0002"
        row += ["", "Yes"]
    master = data.tabs["Master"]
    for column in master_extra:
        master[0].append(column)
        for row in master[1:]:
            row += [""] * (len(master[0]) - len(row))
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


def fresh(services, task_id: str) -> Task:
    services.store.load()
    return next(t for t in services.store.tasks() if t.id == task_id)


def person(services, name: str):
    services.store.load()
    return next(p for p in services.store.staff() if p.name == name)


def email(gmail, services, message_id: str, body: str, sender=K, **extra):
    gmail.add(message_id, sender_email=sender, subject=extra.pop("subject", "Edit"),
              body_text=body, **extra)  # fmt: skip
    services.store.load()
    process_inbox(gmail, services)


def yes(gmail, services, message_id="y1", thread="thread-e1", body="YES", sender=K):
    email(gmail, services, message_id, body, sender=sender, thread_id=thread, subject="Re: Edit")


def wa_ctx(services, gmail):
    return WaContext(services=services, wa=FakeWhatsApp(), queue=FakeQueue(), gmail=gmail)


def wa(ctx, mid, sender=K_WA, text="", tap="", forwarded=False):
    kind = "tap" if tap else "text"
    return handle_message(WAMessage(id=mid, sender=sender, received_at=clock.now(), kind=kind,
                                    text=text, tap=tap, forwarded=forwarded), ctx)  # fmt: skip


def audit_rows(services) -> list[dict]:
    backend = services.store.backend
    head = backend.tabs["AuditLog"][0]
    columns = {h: backend.column("AuditLog", h) for h in head}
    count = len(backend.tabs["AuditLog"]) - 1
    rows = [{h: columns[h][i] for h in head} for i in range(count)]
    return [r for r in rows if str(r.get("Action", "")).startswith("Prompt edit")]


def asked_texts(gmail) -> list[str]:
    return [reply[1].text for reply in gmail.replies]


# --- 1. Permissions ------------------------------------------------------------------------------


@pytest.mark.parametrize("sender", ["staff.p@example.com", "staff.b@example.com"])
def test_non_manager_fixed_form_and_ai_path_by_email_never_apply(sender):
    ai = ai_edit([{"tab": "Master", "key": "G-0001", "column": "Priority", "value": "High"}])
    services, gmail, task = setup(ai)
    email(gmail, services, "e1", f"edit {task.id} Priority = High", sender=sender)
    email(gmail, services, "e2", f"tolong ubah prioritas {task.id} jadi high", sender=sender)
    email(gmail, services, "e3", "YES", sender=sender, thread_id="thread-e1", subject="Re: Edit")
    assert fresh(services, task.id).priority == "Medium"
    assert ai.edit_calls == []  # the edit reader is never asked for a non-Manager
    assert not any("Ubah sheet" in text for text in asked_texts(gmail))
    assert audit_rows(services) == []


@pytest.mark.parametrize("sender", [P_WA, B_WA])
def test_non_manager_on_whatsapp_never_gets_an_edit(sender):
    ai = ai_edit([{"tab": "Master", "key": "G-0001", "column": "Priority", "value": "High"}])
    services, gmail, task = setup(ai)
    ctx = wa_ctx(services, gmail)
    outcome = wa(ctx, "w1", sender, text=f"edit {task.id} Priority = High")
    assert outcome not in ("edit_proposed", "edit_refused")
    wa(ctx, "w2", sender, tap="cf:yes")
    assert fresh(services, task.id).priority == "Medium"
    assert ai.edit_calls == []
    assert not any("Ubah sheet" in body for _, body, _ in ctx.wa.buttons)


def test_yes_from_another_staff_member_does_not_confirm_the_managers_edit():
    services, gmail, task = setup()
    email(gmail, services, "e1", f"edit {task.id} Priority = High")
    yes(gmail, services, "y1", sender="staff.p@example.com")
    assert fresh(services, task.id).priority == "Medium"
    yes(gmail, services, "y2")  # the Manager's own YES still works
    assert fresh(services, task.id).priority == "High"


def test_a_sheet_edit_command_run_for_a_non_manager_is_refused():
    services, gmail, task = setup()
    good, _ = check([Change(tab="Master", key=task.id, column="Priority", value="High")],
                    services.store, TODAY)  # fmt: skip
    from app.sheet_edit import dump

    ctx = CommandContext(store=services.store, gmail=gmail, actor=person(services, "Staff P"),
                         channel="Email", message_id="m1")  # fmt: skip
    result = run_command(Command("sheet_edit", None, dump(good)), ctx)
    assert "Only the manager" in result.reply and not result.changed
    assert fresh(services, task.id).priority == "Medium"


def test_manager_demoted_between_preview_and_yes_changes_nothing():
    services, gmail, task = setup()
    email(gmail, services, "e1", f"edit {task.id} Priority = High")
    services.store.update_staff("Staff K", {"Role": "Full time"})
    yes(gmail, services)
    assert fresh(services, task.id).priority == "Medium"


@pytest.mark.xfail(
    strict=True,
    reason="QA6-C1: a YES from the Manager's address in any thread (a forged From) confirms "
    "a pending edit; nothing ties the YES to the confirmation thread or checks sender auth",
)
def test_forged_manager_edit_and_yes_in_other_threads_do_not_apply():
    services, gmail, _task = setup()
    # The attacker forges From: staff.k@example.com twice, never seeing the question.
    email(gmail, services, "f1", "edit staff Staff P Email = staff.x@example.com")
    yes(gmail, services, "f2", thread="thread-forged-2")
    assert person(services, "Staff P").email == "staff.p@example.com"


# --- 2. Protected and invalid --------------------------------------------------------------------


@pytest.mark.parametrize(
    "tab,column",
    [("Master", c) for c in ("ID", "Weight", "Source ID", "Last updated", "Files",
                             "Original message")]
    + [("Staff", c) for c in ("Name", "Staff ID", "Weighted open load")],
)  # fmt: skip
def test_protected_columns_are_never_written(tab, column):
    services, _, task = setup()
    key = task.id if tab == "Master" else "Staff A"
    good, problems = check([Change(tab=tab, key=key, column=column, value="X")],
                           services.store, TODAY)  # fmt: skip
    assert good == [] and problems == [f"{tab} {column} is not changed by prompt"]


def test_unknown_rows_columns_status_and_progress_are_refused_with_reasons():
    services, _, task = setup()
    asked = [
        Change(tab="Master", key="G-0999", column="Priority", value="High"),
        Change(tab="Master", key=task.id, column="Colour", value="red"),
        Change(tab="Staff", key="Nobody Here", column="Reports to", value="Staff K"),
        Change(tab="Staff", key="Staff A", column="Shoe size", value="42"),
        Change(tab="Master", key=task.id, column="Status", value="Finished"),
        Change(tab="Master", key=task.id, column="Progress", value="150"),
        Change(tab="Master", key=task.id, column="Progress", value="-5"),
        Change(tab="Master", key=task.id, column="Due date", value="31 Feb"),
        Change(tab="Master", key=task.id, column="Owner", value="Zorro"),
    ]
    good, problems = check(asked, services.store, TODAY)
    assert good == [] and len(problems) == len(asked)


def test_dash_clears_and_status_is_read_case_insensitively():
    services, _, task = setup()
    asked = [
        Change(tab="Master", key=task.id, column="Due date", value="-"),
        Change(tab="Master", key=task.id, column="Priority", value="kosong"),
        Change(tab="Master", key=task.id, column="status", value="on HOLD"),
        Change(tab="Master", key=task.id, column="Progress", value="100"),
        Change(tab="Master", key=task.id, column="Progress", value="0"),
    ]
    good, problems = check(asked, services.store, TODAY)
    assert problems == []
    assert [(c.column, c.value) for c in good] == [
        ("Due date", ""), ("Priority", ""), ("Status", "On Hold"), ("Progress", "1.0"),
        ("Progress", "0.0"),
    ]  # fmt: skip


@pytest.mark.parametrize("typed", ["2026-12-05", "5 Dec", "5 Des 2026", "05/12/2026", "5/12"])
def test_dates_in_common_formats(typed):
    services, _, task = setup()
    good, problems = check([Change(tab="Master", key=task.id, column="Due date", value=typed)],
                           services.store, TODAY)  # fmt: skip
    assert problems == [] and good[0].value == "2026-12-05"


@pytest.mark.parametrize("typed", ["Dec 5", "tomorrow", "next friday", "05.12.2026"])
def test_dates_the_rules_cannot_read_are_refused_not_guessed(typed):
    services, _, task = setup()
    good, problems = check([Change(tab="Master", key=task.id, column="Due date", value=typed)],
                           services.store, TODAY)  # fmt: skip
    assert good == [] and "is not a date" in problems[0]


@pytest.mark.xfail(
    strict=True, reason="QA6-C2: Due date '2026/12/05' is silently read as 2026-05-12 (DD/MM "
    "regex matches the '12/05' inside the year-first date)"
)  # fmt: skip
def test_year_first_slash_date_is_not_misread():
    services, _, task = setup()
    good, problems = check(
        [Change(tab="Master", key=task.id, column="Due date", value="2026/12/05")],
        services.store, TODAY,
    )  # fmt: skip
    assert (good and good[0].value == "2026-12-05") or (not good and problems)


@pytest.mark.xfail(
    strict=True,
    reason="QA6-C3: Progress '50%' is refused ('progress is 0-100'); the rules read '70%' "
    "everywhere else (section 19/22), section 29 says Progress is read by rules",
)
def test_progress_with_a_percent_sign_is_read():
    services, _, task = setup()
    good, problems = check([Change(tab="Master", key=task.id, column="Progress", value="50%")],
                           services.store, TODAY)  # fmt: skip
    assert problems == [] and good[0].value == "0.5"


@pytest.mark.xfail(
    strict=True,
    reason="QA6-C4: changes beyond the 20th are dropped silently; section 29 says what fails "
    "is listed with the reason",
)
def test_more_than_twenty_changes_says_what_was_left_out():
    services, _, task = setup()
    asked = [Change(tab="Master", key=task.id, column="Notes", value=f"n{i}") for i in range(25)]
    good, problems = check(asked, services.store, TODAY)
    assert len(good) == MAX_CHANGES
    assert problems  # the five left out are named


def test_mixed_valid_and_invalid_preview_only_the_valid_and_list_the_rest():
    services, gmail, task = setup()
    lines = [f"edit {task.id} Priority = High", f"edit {task.id} ID = G-0100",
             f"edit {task.id} Status = Finished", "edit G-0999 Notes = x"]  # fmt: skip
    email(gmail, services, "e1", "\n".join(lines))
    ask = asked_texts(gmail)[-1]
    assert f"{task.id} · Priority: Medium → High" in ask
    assert "Not changed:" in ask and "ID is not changed" in ask
    assert '"Finished" is not a Status' in ask and "G-0999" in ask
    yes(gmail, services)
    assert fresh(services, task.id).priority == "High"
    assert fresh(services, task.id).status == "In Progress"


def test_fixed_form_staff_name_split_and_new_config_setting():
    services, _, _ = setup()
    changes = by_rules("edit staff Staff A Reports to = Staff K\n"
                       "edit config Some new setting = 5")  # fmt: skip
    good, problems = check(changes, services.store, TODAY)
    assert problems == []
    assert [(c.tab, c.key, c.column) for c in good] == [
        ("Staff", "Staff A", "Reports to"), ("Config", "Some new setting", "Value")]  # fmt: skip


@pytest.mark.xfail(
    strict=True,
    reason="QA6-C5: Master 'Submitted at' (a date-time) takes any text; 'banana' is written "
    "and every later Master read raises SchemaError (section 29: dates read by rules; D15)",
)
def test_submitted_at_must_be_a_date_time():
    services, _, task = setup(master_extra=("Submitted at",))
    good, _problems = check(
        [Change(tab="Master", key=task.id, column="Submitted at", value="banana")],
        services.store, TODAY,
    )  # fmt: skip
    if good:
        apply(good, services.store, "Staff K", clock.now())
    services.store.load()
    assert services.store.tasks()  # the sheet still reads


# --- 3. Values that could hurt -------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="QA6-C6: Config values are not checked; a value AppConfig rejects is previewed "
    "and written, and the next store.config() raises SchemaError (whole service stops)",
)
@pytest.mark.parametrize(
    "setting,value",
    [("Workday start", "banana"), ("Max reminders per day", "many"), ("AI provider", "Gemini"),
     ("Effort: Small", "abc"), ("Holidays", "someday")],
)  # fmt: skip
def test_a_config_value_that_breaks_appconfig_is_refused(setting, value):
    services, _, _ = setup()
    good, _problems = check([Change(tab="Config", key=setting, column="Value", value=value)],
                           services.store, TODAY)  # fmt: skip
    if good:
        apply(good, services.store, "Staff K", clock.now())
    services.store.load()
    services.store.config()  # must still load


@pytest.mark.xfail(
    strict=True,
    reason="QA6-C6: after the Manager's YES to 'Workday start = banana' every inbox run raises "
    "SchemaError: no new request is logged and the Manager cannot fix it by prompt (D15)",
)
def test_after_a_bad_config_edit_requests_are_still_logged_and_it_can_be_fixed_by_prompt():
    services, gmail, _ = setup()
    email(gmail, services, "e1", "edit config Workday start = banana")
    assert "Config Workday start: 08:00 → banana" in asked_texts(gmail)[-1]
    yes(gmail, services)
    assert "Changed:" in asked_texts(gmail)[-1]  # the YES is answered
    email(gmail, services, "r1", "Mohon siapkan laporan OJK", sender="staff.p@example.com",
          subject="Laporan")  # fmt: skip
    assert services.store.task_by_source_id("r1") is not None


def test_bad_config_yes_today_gets_no_answer_and_breaks_the_next_run():
    """What happens now (documents QA6-C6): the value is written, the YES gets no reply (the
    reply needs config().sender_name) and the next run stops on SchemaError."""
    from app.sheets import SchemaError

    services, gmail, _ = setup()
    email(gmail, services, "e1", "edit config Workday start = banana")
    replies_before = len(gmail.replies)
    yes(gmail, services)
    assert len(gmail.replies) == replies_before
    with pytest.raises(SchemaError):
        email(gmail, services, "r1", "Mohon siapkan laporan OJK", sender="staff.p@example.com",
              subject="Laporan")  # fmt: skip
    assert {"INBOX", "UNREAD"} <= gmail.labels("r1")  # left for a later run, not lost


@pytest.mark.xfail(
    strict=True,
    reason="QA6-C7: Staff values are not checked; Role 'Boss' or Capacity 'abc' is written and "
    "every later Staff read raises SchemaError (no one is recognised, service stops)",
)
@pytest.mark.parametrize("column,value", [("Role", "Boss"), ("Capacity (1 = full time)", "abc")])
def test_a_staff_value_that_breaks_the_staff_tab_is_refused(column, value):
    services, _, _ = setup()
    good, _problems = check([Change(tab="Staff", key="Staff A", column=column, value=value)],
                           services.store, TODAY)  # fmt: skip
    if good:
        apply(good, services.store, "Staff K", clock.now())
    services.store.load()
    assert services.store.staff()


def test_max_reminders_minus_one_is_accepted_today():
    """Documented (spec gap, see Questions): a negative cap passes the check and AppConfig."""
    services, _, _ = setup()
    good, _problems = check(
        [Change(tab="Config", key="Max reminders per day", column="Value", value="-1")],
        services.store, TODAY,
    )  # fmt: skip
    apply(good, services.store, "Staff K", clock.now())
    services.store.load()
    assert services.store.config().max_reminders_per_day == -1


@pytest.mark.parametrize("value", ["=IMPORTXML(\"http://x.example\",\"//a\")", "+85255500099",
                                   "@SUM(1)", "-1+1"])  # fmt: skip
def test_formula_like_values_are_written_as_text(value):
    services, _, task = setup()
    asked = [Change(tab="Master", key=task.id, column="Notes", value=value),
             Change(tab="Staff", key="Staff A", column="Reports to", value=value),
             Change(tab="Config", key="Sender name", column="Value", value=value)]  # fmt: skip
    good, problems = check(asked, services.store, TODAY)
    assert problems == [] and len(good) == 3
    apply(good, services.store, "Staff K", clock.now())
    backend = services.store.backend
    master, staff, config = backend.tabs["Master"], backend.tabs["Staff"], backend.tabs["Config"]
    note = master[-1][master[0].index("Notes")]
    reports = next(r for r in staff if r[1] == "Staff A")[staff[0].index("Reports to")]
    sender = next(r for r in config if r[0] == "Sender name")[1]
    assert note == reports == sender == "'" + value


@pytest.mark.xfail(
    strict=True,
    reason="QA6-C8: a new Config setting name is written raw (USER_ENTERED, no ' prefix), so a "
    "setting named '=HYPERLINK(...)' becomes a live formula in the Config tab",
)
def test_a_new_config_setting_name_is_never_a_formula():
    services, _, _ = setup()
    name = '=HYPERLINK("http://x.example","x")'
    good, _problems = check([Change(tab="Config", key=name, column="Value", value="1")],
                           services.store, TODAY)  # fmt: skip
    if good:
        apply(good, services.store, "Staff K", clock.now())
    names = [row[0] for row in services.store.backend.tabs["Config"] if row]
    assert not any(isinstance(n, str) and n.startswith("=") for n in names)


def test_very_long_value_passes_the_check_today():
    """Documented (Questions): no length limit; a Sheets cell holds at most 50,000 characters."""
    services, _, task = setup()
    good, problems = check([Change(tab="Master", key=task.id, column="Notes", value="x" * 60000)],
                           services.store, TODAY)  # fmt: skip
    assert problems == [] and len(good[0].value) == 60000


def test_changing_a_staff_email_moves_who_the_bot_recognises():
    services, gmail, _task = setup()
    email(gmail, services, "e1", "edit staff Staff P Email = staff.x@example.com")
    yes(gmail, services)
    assert person(services, "Staff P").email == "staff.x@example.com"
    from app.gmail import _staff_sender

    gmail.add("m-old", sender_email="staff.p@example.com", body_text="hi")
    gmail.add("m-new", sender_email="staff.x@example.com", body_text="hi")
    services.store.load()
    assert _staff_sender(services.store, gmail.get_message("m-old")) is None
    assert _staff_sender(services.store, gmail.get_message("m-new")).name == "Staff P"


def test_staff_reports_to_is_not_matched_to_staff_today():
    """Documented (Questions): only Master people columns are matched to Staff names."""
    services, _, _ = setup()
    good, problems = check(
        [Change(tab="Staff", key="Staff A", column="Reports to", value="Nobody Real")],
        services.store, TODAY,
    )  # fmt: skip
    assert problems == [] and good[0].value == "Nobody Real"


# --- 4. Confirmation flow ------------------------------------------------------------------------


def test_no_on_the_fixed_form_only_cancels():
    services, gmail, task = setup()
    email(gmail, services, "e1", f"edit {task.id} Priority = High")
    assert "Reply NO to cancel" in asked_texts(gmail)[-1]
    yes(gmail, services, body="NO")
    assert fresh(services, task.id).priority == "Medium"
    assert "Cancelled. Nothing changed." in asked_texts(gmail)[-1]
    assert services.store.task_by_source_id("e1") is None


def test_no_on_an_ai_read_new_email_logs_it_as_a_request():
    ai = ai_edit([{"tab": "Master", "key": "G-0001", "column": "Priority", "value": "High"}])
    services, gmail, task = setup(ai)
    email(gmail, services, "e1", "Tolong G-0001 dibuat prioritas tinggi ya", subject="G-0001")
    assert "new request instead" in asked_texts(gmail)[-1]
    assert "INBOX" in gmail.labels("e1")  # stays in the inbox until YES / NO
    yes(gmail, services, body="No, this is a new request")
    assert fresh(services, task.id).priority == "Medium"
    services.store.load()
    assert services.store.task_by_source_id("e1") is not None
    assert "GRC/Logged" in gmail.labels("e1")


def test_yes_after_thirty_minutes_has_expired():
    services, gmail, task = setup()
    email(gmail, services, "e1", f"edit {task.id} Priority = High")
    with clock.freeze(datetime(2026, 10, 26, 9, 31)):
        yes(gmail, services)
    assert fresh(services, task.id).priority == "Medium"
    assert "expired" in asked_texts(gmail)[-1]


def test_a_second_proposal_replaces_the_first():
    services, gmail, task = setup()
    email(gmail, services, "e1", f"edit {task.id} Priority = High")
    email(gmail, services, "e2", f"edit {task.id} Priority = Low")
    yes(gmail, services, thread="thread-e2")
    assert fresh(services, task.id).priority == "Low"
    assert [r["New value"] for r in audit_rows(services)] == ["Low"]


def test_the_change_is_checked_again_on_yes():
    services, gmail, task = setup()
    email(gmail, services, "e1", f"edit {task.id} Doer = Staff A\nedit {task.id} Priority = High")
    services.store.update_task(task.id, {"Priority": "Low"})  # someone edits the sheet
    services.store.update_staff("Staff A", {"Name": ""})  # Staff A leaves the Staff tab
    yes(gmail, services)
    after = fresh(services, task.id)
    assert after.doer == "Staff P" and after.priority == "High"
    assert '"Staff A" is not in Staff' in asked_texts(gmail)[-1]
    [row] = audit_rows(services)
    assert row["Old value"] == "Low"  # the value replaced is the one there at YES


def test_whatsapp_ya_ubah_batal():
    services, gmail, task = setup()
    ctx = wa_ctx(services, gmail)
    assert wa(ctx, "w1", text=f"edit {task.id} Priority = High") == "edit_proposed"
    assert "Priority: Medium → High" in ctx.wa.buttons[-1][1]
    assert [b[1] for b in ctx.wa.buttons[-1][2]] == ["Ya", "Ubah", "Batal"]
    assert wa(ctx, "w2", tap="cf:edit") == "confirm_edit"
    assert wa(ctx, "w3", tap="cf:yes") == "confirm_none"
    assert wa(ctx, "w4", text=f"edit {task.id} Priority = Low") == "edit_proposed"
    assert wa(ctx, "w5", text="Batal") == "confirm_cancelled"
    assert fresh(services, task.id).priority == "Medium"
    assert wa(ctx, "w6", text=f"edit {task.id} Priority = Low") == "edit_proposed"
    assert wa(ctx, "w7", text="ya") == "confirmed_sheet_edit"
    assert fresh(services, task.id).priority == "Low"


@pytest.mark.parametrize("edit_line", ["edit {tid} Priority = High",
                                       "edit config Reminder time morning = 09:30"])  # fmt: skip
def test_a_redelivered_whatsapp_ya_applies_once(edit_line):
    services, gmail, task = setup()
    ctx = wa_ctx(services, gmail)
    assert wa(ctx, "w1", text=edit_line.format(tid=task.id)) == "edit_proposed"
    assert wa(ctx, "w2", tap="cf:yes") == "confirmed_sheet_edit"
    assert wa(ctx, "w2", tap="cf:yes") in ("confirm_duplicate", "confirm_none")
    assert len(audit_rows(services)) == 1


def test_a_second_yes_email_after_applying_changes_nothing_more():
    services, gmail, task = setup()
    email(gmail, services, "e1", f"edit {task.id} Priority = High")
    yes(gmail, services, "y1")
    services.store.update_task(task.id, {"Priority": "Low"})
    yes(gmail, services, "y2")
    assert fresh(services, task.id).priority == "Low"
    assert len(audit_rows(services)) == 1


# --- 5. Interaction with other Manager messages --------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="QA6-C9: the edit reader runs before command parsing, so every Manager command "
    "(e.g. 'status G-0001') costs a Gemini edit call and can be swallowed as an edit "
    "(section 29: only messages that are not a command)",
)
def test_a_manager_command_by_email_is_not_sent_to_the_edit_reader():
    ai = ai_edit([], confidence=0.9, is_edit=False)
    services, gmail, task = setup(ai)
    email(gmail, services, "e1", f"status {task.id}", subject="status")
    assert "In Progress" in asked_texts(gmail)[-1]  # answered as a command
    assert ai.edit_calls == []


@pytest.mark.xfail(
    strict=True,
    reason="QA6-C9: on WhatsApp too, a Manager command reaches the AI edit reader first",
)
def test_a_manager_command_on_whatsapp_is_not_sent_to_the_edit_reader():
    ai = ai_edit([], confidence=0.9, is_edit=False)
    services, gmail, task = setup(ai)
    ctx = wa_ctx(services, gmail)
    wa(ctx, "w1", text=f"status {task.id}")
    assert ai.edit_calls == []


def test_a_manager_command_still_works_when_the_ai_says_not_an_edit():
    ai = ai_edit([], confidence=0.9, is_edit=False)
    services, gmail, task = setup(ai)
    email(gmail, services, "e1", f"status {task.id}", subject="status")
    assert "In Progress" in asked_texts(gmail)[-1]
    assert services.store.task_by_source_id("e1") is None


@pytest.mark.parametrize("subject,body", [
    ("Fwd: letter", "edit G-0001 Priority = High\n---------- Forwarded message ---------\nx"),
    ("Laporan", "PROMPT: assign this to Staff A\nedit G-0001 Priority = High"),
])  # fmt: skip
def test_forwarded_mail_and_prompt_lines_are_requests_not_edits(subject, body):
    ai = ai_edit([{"tab": "Master", "key": "G-0001", "column": "Priority", "value": "High"}])
    services, gmail, task = setup(ai)
    email(gmail, services, "e1", body, subject=subject)
    assert fresh(services, task.id).priority == "Medium"
    assert ai.edit_calls == []
    assert not any("Ubah sheet" in text for text in asked_texts(gmail))
    services.store.load()
    assert services.store.task_by_source_id("e1") is not None


def test_whatsapp_prompt_and_forwarded_messages_are_not_edits():
    ai = ai_edit([{"tab": "Master", "key": "G-0001", "column": "Priority", "value": "High"}])
    services, gmail, _task = setup(ai)
    ctx = wa_ctx(services, gmail)
    assert wa(ctx, "w1", text="PROMPT: assign this to Staff A") != "edit_proposed"
    assert wa(ctx, "w2", text="ubah G-0001 priority high", forwarded=True) != "edit_proposed"
    assert ai.edit_calls == []


def test_ai_reading_under_point_six_is_ignored_and_the_email_is_logged():
    ai = ai_edit([{"tab": "Master", "key": "G-0001", "column": "Priority", "value": "High"}],
                 confidence=0.5)  # fmt: skip
    services, gmail, task = setup(ai)
    email(gmail, services, "e1", "Please prepare the HKMA return", subject="HKMA")
    assert len(ai.edit_calls) == 1
    assert fresh(services, task.id).priority == "Medium"
    services.store.load()
    assert services.store.task_by_source_id("e1") is not None


def test_manager_reply_in_a_task_notice_thread_is_an_edit_but_not_intake():
    ai = ai_edit([{"tab": "Master", "key": "G-0001", "column": "Due date",
                   "value": "2026-12-05"}])  # fmt: skip
    services, gmail, task = setup(ai)
    outbox.record(services.store, "Email", "n-1", "Staff K", [task.id], "notice", "thread-n1")
    email(gmail, services, "e1", "mundurkan ke 5 Des", thread_id="thread-n1", subject="Re: x")
    ask = asked_texts(gmail)[-1]
    assert "Due date: 2026-11-20 → 2026-12-05" in ask
    assert "Reply NO to cancel" in ask and "new request" not in ask
    yes(gmail, services, thread="thread-n1")
    assert fresh(services, task.id).due_date == date(2026, 12, 5)


@pytest.mark.xfail(
    strict=True,
    reason="QA6-C10: a new Manager email the AI reads as an edit whose changes all fail gets "
    "'I could not change that' and is marked read: never logged, no NO offered (D15)",
)
def test_a_new_email_misread_as_an_invalid_edit_is_still_logged():
    ai = ai_edit([{"tab": "Master", "key": "G-0999", "column": "Due date", "value": "2026-12-05"}])
    services, gmail, _ = setup(ai)
    email(gmail, services, "e1", "OJK minta laporan likuiditas paling lambat 5 Des",
          subject="Laporan OJK")  # fmt: skip
    services.store.load()
    logged = services.store.task_by_source_id("e1") is not None
    offered_no = "new request instead" in asked_texts(gmail)[-1]
    assert logged or offered_no


# --- 6. Record -----------------------------------------------------------------------------------


def test_auditlog_per_cell_progresslog_for_master_and_last_updated():
    services, gmail, task = setup()
    email(gmail, services, "e1", f"edit {task.id} Priority = High\nedit {task.id} Due date = 5 Dec"
                                 "\nedit staff Staff A Reports to = Staff K\n"
                                 "edit config Reminder time morning = 09:30")  # fmt: skip
    yes(gmail, services)
    rows = audit_rows(services)
    assert [(r["Action"], r["Field"], r["Old value"], r["New value"]) for r in rows] == [
        ("Prompt edit (Master)", "Priority", "Medium", "High"),
        ("Prompt edit (Master)", "Due date", "2026-11-20", "2026-12-05"),
        ("Prompt edit (Staff)", "Reports to", "", "Staff K"),
        ("Prompt edit (Config)", "Reminder time morning", "", "09:30"),
    ]
    logged = [r for r in progress.rows(services.store) if r.note == "Prompt edit"]
    assert sorted((r.field, r.old_value, r.new_value) for r in logged) == [
        ("Due date", "2026-11-20", "2026-12-05"), ("Priority", "Medium", "High")]  # fmt: skip
    assert fresh(services, task.id).last_updated == clock.now()


@pytest.mark.xfail(
    strict=True,
    reason="QA6-C11: ProgressLog Channel is hard-coded 'Email' in sheet_edit.apply, so a "
    "WhatsApp edit is logged as Email (section 22: ProgressLog records the channel)",
)
def test_progresslog_channel_of_a_whatsapp_edit():
    services, gmail, task = setup()
    ctx = wa_ctx(services, gmail)
    wa(ctx, "w1", text=f"edit {task.id} Priority = High")
    wa(ctx, "w2", tap="cf:yes")
    [row] = [r for r in progress.rows(services.store) if r.note == "Prompt edit"]
    assert row.channel == "WhatsApp"


def test_undo_restores_a_master_edit_of_several_cells():
    services, gmail, task = setup()
    email(
        gmail, services, "e1", f"edit {task.id} Due date = 5 Dec\nedit {task.id} Status = On Hold"
    )
    yes(gmail, services)
    ctx = CommandContext(store=services.store, gmail=gmail, actor=person(services, "Staff K"),
                         channel="Email", message_id="undo-1")  # fmt: skip
    assert run_command(Command("undo", task.id), ctx).changed
    after = fresh(services, task.id)
    assert after.due_date == date(2026, 11, 20) and after.status == "In Progress"


def test_staff_and_config_edits_are_not_undone_by_undo():
    services, gmail, _ = setup()
    email(gmail, services, "e1", "edit staff Staff A Reports to = Staff K\n"
                                 "edit config Reminder time morning = 09:30")  # fmt: skip
    yes(gmail, services)
    ctx = CommandContext(store=services.store, gmail=gmail, actor=person(services, "Staff K"),
                         channel="Email", message_id="undo-1")  # fmt: skip
    result = run_command(Command("undo"), ctx)
    assert not result.changed
    assert person(services, "Staff A").reports_to == "Staff K"
    assert str(services.store.config().reminder_time_morning) == "09:30:00"


# --- 7. Privacy ----------------------------------------------------------------------------------


def test_logs_hold_no_message_text_names_emails_or_numbers(caplog):
    ai = ai_edit([{"tab": "Staff", "key": "Staff P", "column": "WhatsApp",
                   "value": "+852 5550 0099"}])  # fmt: skip
    services, gmail, task = setup(ai)
    caplog.set_level(logging.DEBUG, logger="grc")
    email(gmail, services, "e1", "nomor WA Staff P ganti ke +852 5550 0099, secret-phrase-77")
    yes(gmail, services)
    ctx = wa_ctx(services, gmail)
    wa(ctx, "w1", text=f"edit {task.id} Notes = secret-phrase-88")
    wa(ctx, "w2", tap="cf:yes")
    text = "\n".join(
        f"{r.getMessage()} {sorted((k, str(v)) for k, v in r.__dict__.items())}"
        for r in caplog.records
    )
    for leak in ("secret-phrase", "Staff P", "Staff K", "@example.com", "5550", K_WA):
        assert leak not in text, leak
