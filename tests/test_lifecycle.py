"""Roles, review lifecycle and commands (DESIGN section 19, v1.4) with fakes. Names are fake."""

from datetime import date, datetime, timedelta

import pytest

from app import clock
from app.commands import parse_command, progress_fraction, task_id_in
from app.extract.ai import NullProvider
from app.gmail import COMMAND_LABEL, process_inbox
from app.lifecycle import CommandContext, run_command
from app.models import Attachment, RouteResult, StaffMember, Task
from app.pipeline import Services
from app.route import (
    default_reviewer,
    delegation_problem,
    owner_and_doer,
    route,
    unavailable_reason,
)
from app.whatsapp import WaContext, WAMedia, WAMessage, handle_message
from tests.fakes import (
    FakeDriveStore,
    FakeGmailClient,
    FakeQueue,
    FakeSheetsBackend,
    FakeSheetStore,
    FakeWhatsApp,
)

# Fake team: Manager Boss; Lead (full time) with Intern Ann; Intern Hr reports to the Manager;
# Helper is additional help; Other is a full-timer in another division.
TEAM = [
    ("Boss Manager", "boss@example.com", "Manager", "", "", "+852 5550 0101"),
    (
        "Lead Finance",
        "lead@example.com",
        "Full time",
        "Finance & Risk; Account & Finance",
        "Boss Manager",
        "+852 5550 0102",
    ),
    (
        "Ann Intern",
        "ann@example.com",
        "Intern",
        "Account & Finance",
        "Lead Finance",
        "+852 5550 0103",
    ),
    ("Hr Intern", "hr@example.com", "Intern", "HC", "Boss Manager", ""),
    ("Helper Extra", "helper@example.com", "Additional help", "", "Boss Manager", ""),
    ("Other Policy", "other@example.com", "Full time", "Policy & Risk", "Boss Manager", ""),
]
LEAD_WA = "85255500102"
ANN_WA = "85255500103"


def team_backend() -> FakeSheetsBackend:
    backend = FakeSheetsBackend.from_fixtures()
    backend.tabs["Staff"] = [
        [
            "Name",
            "Email",
            "Role",
            "Divisions",
            "Reports to",
            "WhatsApp",
            "Main division",
            "Internship end date",
            "Capacity (1 = full time)",
            "Weighted open load",
            "Reminders paused",
        ],
        *[
            [n, e, r, d, rt, w, "", "2027-06-30" if r == "Intern" else "", 1, 0, ""]
            for n, e, r, d, rt, w in TEAM
        ],
    ]
    return backend


def store_with(*tasks: Task) -> FakeSheetStore:
    store = FakeSheetStore(team_backend())
    store.load()
    for task in tasks:
        store.append_task(task)
    store.load()
    return store


def task(**values) -> Task:
    base = {
        "title": "Quarterly report",
        "status": "Not Started",
        "owner": "Lead Finance",
        "doer": "Lead Finance",
        "due_date": date(2026, 11, 20),
        "source": "Manual",
        "source_id": f"manual-{values.get('title', 'x')}-{len(values)}",
    }
    return Task.model_validate(
        {Task.model_fields[k].alias: v for k, v in {**base, **values}.items()}
    )


def person(store, name) -> StaffMember:
    return next(p for p in store.staff() if p.name == name)


def run(store, gmail, actor, text, channel="WhatsApp", files=(), context=None):
    command = parse_command(text, has_context=context is not None)
    assert command is not None, text
    ctx = CommandContext(
        store=store,
        gmail=gmail,
        actor=person(store, actor),
        channel=channel,
        files=list(files),
        context_task_id=context,
    )
    return run_command(command, ctx)


def get(store, task_id) -> Task:
    store.load()
    return next(t for t in store.tasks() if t.id == task_id)


PDF = Attachment(filename="Review.pdf", mime="application/pdf", data=b"%PDF review")
PROOF = Attachment(filename="Proof.pdf", mime="application/pdf", data=b"%PDF proof")


# --- parser -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "context", "expected"),
    [
        ("progress G-0012 50", False, ("progress", "G-0012", "50")),
        ("Progres g12 30%", False, ("progress", "G-0012", "30")),
        ("review G-0012", False, ("review", "G-0012", "")),
        ("selesai G-7", False, ("done", "G-0007", "")),
        ("files G-0012", False, ("files", "G-0012", "")),
        ("approve G-0012", False, ("approve", "G-0012", "")),
        ("return G-0012 fix table 3", False, ("return", "G-0012", "fix table 3")),
        ("revisi tabel 3", True, ("return", None, "tabel 3")),
        ("tugas", False, ("mine", None, "")),
        ("hari ini", False, ("today", None, "")),
        ("minggu ini", False, ("week", None, "")),
        ("semua", False, ("all", None, "")),
        ("pause Ann", False, ("pause", None, "Ann")),
        ("lanjutkan Ann", False, ("resume", None, "Ann")),
        ("assign G-0012 to Ann", False, ("assign", "G-0012", "Ann")),
        ("kasih G-0012 ke Ann", False, ("assign", "G-0012", "Ann")),
        ("assign that to Ann", True, ("assign", None, "Ann")),
        ("selesai", True, ("done", None, "")),
    ],
)
def test_parser(text, context, expected):
    command = parse_command(text, has_context=context)
    assert (command.name, command.task_id, command.value) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Review laporan stress test deadline 20 Nov",
        "cek laporan ATMR ya",
        "selesai",
        "update",
        "Mohon kirim laporan",
        "progress G-0012 lots",
    ],
)
def test_forwarded_requests_are_not_commands(text):
    assert parse_command(text) is None


def test_progress_and_ids():
    assert progress_fraction("50") == 0.5 and progress_fraction("0.3") == 0.3
    assert progress_fraction("150") is None
    assert task_id_in("Re: [GRC] G-0042 – x") == "G-0042" and task_id_in("nothing") is None


# --- roles (route.py) -------------------------------------------------------------------------


def staff_list():
    return store_with().staff()


def test_intern_under_a_full_timer_is_doer_and_the_supervisor_owns():
    people = staff_list()
    ann = next(p for p in people if p.name == "Ann Intern")
    hr = next(p for p in people if p.name == "Hr Intern")
    assert owner_and_doer(ann, people) == ("Lead Finance", "Ann Intern")
    assert owner_and_doer(hr, people) == ("Hr Intern", "Hr Intern")  # reports to the Manager


def test_additional_help_and_managers_are_never_proposed():
    people = staff_list()
    helper = next(p for p in people if p.name == "Helper Extra")
    assert unavailable_reason(helper, date(2026, 10, 26), None) == "is additional help"


def test_rule_3_uses_every_division_a_person_covers():
    store = store_with()
    config = store.config()
    result = route(
        text="",
        contacts=[],
        letter_code=None,
        due=None,
        catalog=[],
        staff=store.staff(),
        letter_codes=[],
        config=config,
        today=date(2026, 10, 26),
        ai_catalog_id=None,
    )
    assert result.rule == 5  # no division known
    routed = _route_division(store, "Account & Finance")
    assert routed.rule == 3 and routed.owner == "Lead Finance"
    assert routed.doer in ("Lead Finance", "Ann Intern")


def _route_division(store, division) -> RouteResult:
    from app.models import LetterCode

    codes = [LetterCode.model_validate({"Letter code": "AF", "Route to division": division})]
    return route(
        text="",
        contacts=[],
        letter_code="AF",
        due=None,
        catalog=[],
        staff=store.staff(),
        letter_codes=codes,
        config=store.config(),
        today=date(2026, 10, 26),
    )


def test_default_reviewer():
    people = staff_list()
    assert default_reviewer("", "Ann Intern", people).name == "Lead Finance"
    assert default_reviewer("", "Lead Finance", people).name == "Boss Manager"
    assert default_reviewer("", "Hr Intern", people).name == "Boss Manager"
    assert default_reviewer("Other Policy", "Ann Intern", people).name == "Other Policy"


def test_delegation_rules():
    people = staff_list()
    by = {p.name: p for p in people}
    lead, ann, other, boss = (
        by[n] for n in ("Lead Finance", "Ann Intern", "Other Policy", "Boss Manager")
    )
    assert delegation_problem(lead, "Lead Finance", "Lead Finance", ann, people) is None
    assert "does not report to you" in delegation_problem(lead, "Lead Finance", "", other, people)
    assert "Owner, Doer or the Manager" in delegation_problem(
        other, "Lead Finance", "", ann, people
    )
    assert delegation_problem(boss, "Lead Finance", "", other, people) is None
    assert delegation_problem(ann, "Lead Finance", "Ann Intern", lead, people) is not None


# --- lifecycle --------------------------------------------------------------------------------


def test_assign_that_to_my_intern_moves_the_doer_and_emails_her():
    store = store_with(task(source_id="t1"))
    gmail = FakeGmailClient()
    result = run(store, gmail, "Lead Finance", "assign that to ann", context="G-0001")
    assert result.changed and "now with Ann Intern" in result.reply
    row = get(store, "G-0001")
    assert (row.owner, row.doer) == ("Lead Finance", "Ann Intern")
    [(email, _)] = gmail.sent
    assert email.to == ["ann@example.com"] and "G-0001" in email.subject
    assert store.state()["G-0001"].notified_doer == "Ann Intern"  # no second email from /changes
    actions = [a for a in store.backend.column("AuditLog", "Action")]
    assert "Delegated" in actions
    assert "Command via WhatsApp" in store.backend.column("AuditLog", "Reason")


def test_assign_refusals_change_nothing():
    store = store_with(task(source_id="t1"), task(source_id="t2", status="Proposed"))
    gmail = FakeGmailClient()
    assert (
        "does not report to you"
        in run(store, gmail, "Lead Finance", "assign G-0001 to Other").reply
    )
    assert (
        "waiting for the manager" in run(store, gmail, "Lead Finance", "assign G-0002 to Ann").reply
    )
    assert "Which task" in run(store, gmail, "Lead Finance", "assign that to Ann").reply
    assert "can't find G-0099" in run(store, gmail, "Lead Finance", "assign G-0099 to Ann").reply
    assert get(store, "G-0001").doer == "Lead Finance" and gmail.sent == []


def test_assign_to_me_takes_it_back():
    store = store_with(task(source_id="t1", doer="Ann Intern"))
    run(store, FakeGmailClient(), "Lead Finance", "assign G-0001 to me")
    assert get(store, "G-0001").doer == "Lead Finance"


def test_progress_moves_not_started_to_in_progress():
    store = store_with(task(source_id="t1"))
    result = run(store, FakeGmailClient(), "Lead Finance", "progress G-0001 50")
    row = get(store, "G-0001")
    assert row.progress_value == 0.5 and row.status == "In Progress" and "50%" in result.reply


def test_review_flow_end_to_end():
    store = store_with(task(source_id="t1", doer="Ann Intern", needs_review="Yes"))
    gmail = FakeGmailClient()
    # Ann sends for review with a file: kept in the mailbox, reviewer (her supervisor) emailed it
    result = run(store, gmail, "Ann Intern", "review G-0001", files=[PDF])
    assert "sent to Lead Finance" in result.reply
    row = get(store, "G-0001")
    assert row.status == "In Review" and row.review_file == "In the GRC bot mailbox: Review.pdf"
    assert gmail.stored["G-0001"] == [PDF]
    review_email, _ = gmail.sent[-1]
    assert review_email.to == ["lead@example.com"] and review_email.attachments == [PDF]
    assert "approve G-0001" in review_email.text
    # Done before approval is refused
    assert (
        "needs review first" in run(store, gmail, "Ann Intern", "done G-0001", files=[PROOF]).reply
    )
    # Only the reviewer can return
    assert "Only Lead Finance" in run(store, gmail, "Ann Intern", "return G-0001 x").reply
    result = run(store, gmail, "Lead Finance", "return G-0001 fix table 3")
    row = get(store, "G-0001")
    assert row.status == "Returned" and row.review_notes == "fix table 3"
    returned_email, _ = gmail.sent[-1]
    assert returned_email.to == ["ann@example.com"] and "fix table 3" in returned_email.text
    # back to review, approve, then done with proof
    run(store, gmail, "Ann Intern", "review G-0001", files=[PDF])
    run(store, gmail, "Lead Finance", "approve G-0001")
    assert get(store, "G-0001").status == "Approved"
    approved_email, _ = gmail.sent[-1]
    assert approved_email.to == ["ann@example.com"]
    run(store, gmail, "Ann Intern", "done G-0001", files=[PROOF])
    row = get(store, "G-0001")
    assert row.status == "Done" and row.link == "In the GRC bot mailbox: Proof.pdf"
    assert row.submitted_date == clock.today()


def test_done_without_proof_is_refused_unless_link_is_filled():
    store = store_with(task(source_id="t1"), task(source_id="t2", link="In the GRC bot mailbox: a"))
    gmail = FakeGmailClient()
    assert "Send the proof file" in run(store, gmail, "Lead Finance", "done G-0001").reply
    assert get(store, "G-0001").status == "Not Started"
    run(store, gmail, "Lead Finance", "done G-0002")
    assert get(store, "G-0002").status == "Done"


def test_manager_commands_are_manager_only():
    store = store_with(task(source_id="t1"))
    gmail = FakeGmailClient()
    assert "Only the manager" in run(store, gmail, "Lead Finance", "semua").reply
    assert "Lead Finance" in run(store, gmail, "Boss Manager", "semua").reply
    assert "paused" in run(store, gmail, "Boss Manager", "pause Ann").reply
    assert person(store, "Ann Intern").paused
    run(store, gmail, "Boss Manager", "resume Ann")
    store.load()
    assert not person(store, "Ann Intern").paused


def test_paused_person_gets_no_assignment_email():
    store = store_with(task(source_id="t1"))
    store.update_staff("Ann Intern", {"Reminders paused": "Yes"})
    gmail = FakeGmailClient()
    run(store, gmail, "Lead Finance", "assign G-0001 to Ann")
    assert gmail.sent == []


def test_files_resend_and_the_20mb_cap():
    big = Attachment(filename="Big.pdf", mime="application/pdf", data=b"x" * (21 * 1024 * 1024))
    store = store_with(task(source_id="t1"))
    gmail = FakeGmailClient()
    gmail.store_files("G-0001", [PDF, big])
    result = run(store, gmail, "Lead Finance", "files G-0001")
    assert result.attachments == [PDF, big]
    from app.models import OutgoingEmail
    from app.notify import with_files

    email = with_files(OutgoingEmail(to=["x"], subject="s", text="t", html="h"), result.attachments)
    assert email.attachments == [PDF] and "Big.pdf" in email.text  # over 20 MB: named only
    assert "not on G-0001" in run(store, gmail, "Other Policy", "files G-0001").reply


def test_lists():
    store = store_with(
        task(source_id="t1", due_date=clock.today()),
        task(source_id="t2", due_date=clock.today() + timedelta(days=30)),
    )
    gmail = FakeGmailClient()
    assert "(2)" in run(store, gmail, "Lead Finance", "tugas").reply
    assert "(1)" in run(store, gmail, "Lead Finance", "hari ini").reply


# --- the same command over WhatsApp and email -------------------------------------------------


def wa_context(store, gmail) -> WaContext:
    services = Services(store=store, drive=FakeDriveStore(), ai=NullProvider())
    return WaContext(
        services=services,
        wa=FakeWhatsApp({"m-proof": b"%PDF proof"}),
        queue=FakeQueue(),
        gmail=gmail,
    )


def test_whatsapp_and_email_commands_give_the_same_result():
    # WhatsApp: progress, and done with the proof as a document captioned with the command
    store_a = store_with(task(source_id="t1"))
    gmail_a = FakeGmailClient()
    ctx = wa_context(store_a, gmail_a)
    now = clock.now()

    def wa(i, text, media=()):
        return WAMessage(
            id=i,
            sender=LEAD_WA,
            received_at=now,
            kind="media" if media else "text",
            text=text,
            media=list(media),
        )

    assert handle_message(wa("w1", "progress G-0001 40"), ctx) == "command_progress"
    proof = WAMedia(id="m-proof", filename="Proof.pdf", mime="application/pdf")
    assert handle_message(wa("w2", "done G-0001", [proof]), ctx) == "command_done"
    # Email: the same two commands as replies in the task's thread (no ID needed there)
    store_b = store_with(task(source_id="t1"))
    gmail_b = FakeGmailClient()
    services = Services(store=store_b, drive=FakeDriveStore(), ai=NullProvider())
    subject = "Re: [GRC] G-0001 – Quarterly report (due 20 Nov 2026)"
    gmail_b.add(
        "e1",
        subject=subject,
        body_text="progress 40\n\n> quoted",
        sender_email="lead@example.com",
        thread_id="th",
    )
    gmail_b.add(
        "e2",
        subject=subject,
        body_text="done",
        sender_email="lead@example.com",
        thread_id="th",
        attachments=[PROOF],
    )
    result = process_inbox(gmail_b, services)
    assert result.commands == 2 and gmail_b.labels("e1") == {COMMAND_LABEL}
    for store in (store_a, store_b):
        row = get(store, "G-0001")
        assert (row.status, row.progress_value, row.link) == (
            "Done",
            0.4,
            "In the GRC bot mailbox: Proof.pdf",
        )
    reasons = set(store_b.backend.column("AuditLog", "Reason"))
    assert "Command via Email" in reasons
    assert "Command via WhatsApp" in set(store_a.backend.column("AuditLog", "Reason"))


def test_whatsapp_reply_context_gives_the_task():
    store = store_with(task(source_id="t1"))
    gmail = FakeGmailClient()
    ctx = wa_context(store, gmail)
    now = clock.now()
    first = WAMessage(
        id="w1", sender=LEAD_WA, received_at=now, kind="text", text="progress G-0001 10"
    )
    handle_message(first, ctx)
    bot_message = [r for r in store.wa_pending() if r.kind == "sent"][-1]
    assert bot_message.task_id == "G-0001"
    reply = WAMessage(
        id="w2",
        sender=LEAD_WA,
        received_at=now,
        kind="text",
        text="assign that to Ann",
        context_id=bot_message.key,
    )
    assert handle_message(reply, ctx) == "command_assign"
    assert get(store, "G-0001").doer == "Ann Intern"


def test_email_reply_that_is_not_a_command_still_goes_to_notes(fixture_text):
    store = store_with()
    gmail = FakeGmailClient()
    services = Services(store=store, drive=FakeDriveStore(), ai=NullProvider())
    gmail.add(
        "m1",
        body_text="Laporan stress test, deadline 20 November 2026.",
        thread_id="t",
        sender_email="lead@example.com",
    )
    process_inbox(gmail, services)
    gmail.add("m2", body_text="Terima kasih, noted", thread_id="t", sender_email="lead@example.com")
    run_ = process_inbox(gmail, services)
    assert (run_.commands, run_.replies) == (0, 1)


def test_sheet_edit_to_in_review_emails_the_reviewer():
    from app.jobs import run_changes

    store = store_with(task(source_id="t1", doer="Ann Intern", status="In Progress"))
    gmail = FakeGmailClient()
    run_changes(store, gmail, "https://sheet")  # baseline
    store.update_task("G-0001", {"Status": "In Review"})
    store.load()
    run_changes(store, gmail, "https://sheet")
    subjects = [email.subject for email, _ in gmail.sent]
    assert any("review requested" in s for s in subjects)
    to = [email.to for email, _ in gmail.sent if "review requested" in email.subject]
    assert to == [["lead@example.com"]]


def test_assignment_email_carries_the_files():
    from app.jobs import run_changes

    store = store_with(task(source_id="t1", status="Proposed"))
    gmail = FakeGmailClient()
    gmail.store_files("G-0001", [PDF])
    run_changes(store, gmail, "https://sheet")  # baseline
    store.update_task("G-0001", {"Status": "Not Started"})
    store.load()
    run_changes(store, gmail, "https://sheet")
    [(email, _)] = gmail.sent
    assert email.attachments == [PDF]


def test_now_is_frozen():
    assert clock.now() == datetime(2026, 10, 26, 9, 0, tzinfo=clock.TZ)


def test_health_warns_about_divisions_nobody_covers(monkeypatch):
    from fastapi.testclient import TestClient

    from app import main as main_module

    warnings = main_module.uncovered_divisions(store_with())
    assert warnings and all(w.startswith("nobody in Staff covers division") for w in warnings)
    assert not any("Account & Finance" in w for w in warnings)  # Lead Finance covers it
    healthy = main_module.Health(missing=[], sheet=[], checked_at=0.0, warnings=warnings)
    monkeypatch.setattr(main_module, "_health", healthy)
    response = TestClient(main_module.app).get("/health")
    assert response.status_code == 200 and response.json()["warnings"] == warnings


def test_a_sheet_without_the_new_columns_keeps_working_and_says_what_to_add():
    from app import main as main_module

    backend = team_backend()
    header = backend.tabs["Master"][0]
    keep = [i for i, name in enumerate(header) if name not in ("Review file", "Reviewer")]
    backend.tabs["Master"] = [[row[i] for i in keep] for row in backend.tabs["Master"]]
    store = FakeSheetStore(backend)
    store.load()  # no SchemaError: the v1.4 columns are optional
    store.append_task(task(source_id="t1"))
    store.load()
    result = run(store, FakeGmailClient(), "Lead Finance", "review G-0001", files=[PDF])
    assert "missing a column" in result.reply and "Review file" in result.reply
    assert get(store, "G-0001").status == "Not Started"
    assert any("Review file" in w for w in main_module.missing_review_columns(store))
