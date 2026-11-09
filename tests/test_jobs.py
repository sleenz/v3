from datetime import date, datetime, timedelta

from app import clock
from app.extract.ai import NullProvider
from app.jobs import (
    NO_EMAIL_FLAG,
    run_changes,
    run_digest,
)
from app.models import RawRequest, Task
from app.pipeline import Services, process_request
from tests.fakes import FakeDriveStore, FakeGmailClient, FakeSheetsBackend, FakeSheetStore

SHEET = "https://docs.google.com/spreadsheets/d/sheet-1"
TODAY = date(2026, 10, 26)


def make(backend=None):
    store = FakeSheetStore(backend or FakeSheetsBackend.from_fixtures())
    store.load()
    return Services(store=store, drive=FakeDriveStore(), ai=NullProvider()), FakeGmailClient()


def set_cell(backend, tab, key_header, key, header, value):
    rows = backend.tabs[tab]
    for row in rows[1:]:
        if row[rows[0].index(key_header)] == key:
            row[rows[0].index(header)] = value
            return
    raise KeyError(key)


def new_task(svc, text, source_id):
    raw = RawRequest(
        source="Email",
        source_id=source_id,
        received_at=clock.now(),
        body_text=text,
        sender_email="grc-manager@example.com",
    )
    return process_request(raw, svc).task_id


def reload(svc):
    svc.store.load()
    return svc.store


def changes(svc, gmail):
    return run_changes(reload(svc), gmail, SHEET)


# --- /jobs/changes -----------------------------------------------------------------------------


def test_first_run_only_records_state():
    svc, gmail = make()
    task_id = new_task(svc, "Laporan stress test, deadline 20 November 2026.", "m1")
    svc.store.update_task(task_id, {"Status": "In Progress"})
    run = changes(svc, gmail)
    assert run.baseline and run.emailed == 0 and gmail.sent == []
    state = svc.store.state()
    assert state[task_id].notified_doer == "Staff A"
    assert changes(svc, gmail).emailed == 0


def test_approval_emails_doer_once():
    svc, gmail = make()
    changes(svc, gmail)  # baseline
    task_id = new_task(svc, "Laporan stress test, deadline 20 November 2026.", "m1")
    assert changes(svc, gmail).emailed == 0  # still Proposed

    svc.store.update_task(task_id, {"Status": "Not Started"})
    run = changes(svc, gmail)
    assert run.emailed == 1
    [(email, sender)] = gmail.sent
    assert email.to == ["staff.a@example.com"] and email.cc == []
    assert email.subject.startswith(f"[GRC] {task_id} – ")
    assert sender == "GRC Tracker"
    state = svc.store.state()[task_id]
    assert state.notified_doer == "Staff A" and state.notified_at == clock.now()
    audit = svc.store.backend.tabs["AuditLog"]
    assert audit[-1][audit[0].index("Action")] == "'Assignment emailed"

    assert changes(svc, gmail).emailed == 0
    svc.store.update_task(task_id, {"Status": "In Progress"})
    assert changes(svc, gmail).emailed == 0
    assert len(gmail.sent) == 1


def test_handoff_emails_new_doer_with_owner_in_cc():
    svc, gmail = make()
    changes(svc, gmail)
    task_id = new_task(svc, "Laporan stress test, deadline 20 November 2026.", "m1")
    svc.store.update_task(task_id, {"Status": "Not Started"})
    changes(svc, gmail)
    svc.store.update_task(task_id, {"Doer": "Staff I"})
    run = changes(svc, gmail)
    assert run.emailed == 1
    email = gmail.sent[-1][0]
    assert email.to == ["staff.i@example.com"] and email.cc == ["staff.a@example.com"]


def test_doer_change_while_proposed_emails_only_after_approval():
    svc, gmail = make()
    changes(svc, gmail)
    task_id = new_task(svc, "Tolong cek printer", "m1")  # rule 5: no owner
    svc.store.update_task(task_id, {"Doer": "Staff O", "Owner": "Staff O"})
    assert changes(svc, gmail).emailed == 0
    svc.store.update_task(task_id, {"Status": "Not Started"})
    assert changes(svc, gmail).emailed == 1
    assert gmail.sent[0][0].to == ["staff.o@example.com"]


def test_closed_tasks_are_never_emailed():
    svc, gmail = make()
    changes(svc, gmail)
    task_id = new_task(svc, "Laporan stress test, deadline 20 November 2026.", "m1")
    svc.store.update_task(task_id, {"Status": "Done", "Doer": "Staff I"})
    assert changes(svc, gmail).emailed == 0


def test_missing_doer_email_flags_once_and_retries_later():
    backend = FakeSheetsBackend.from_fixtures()
    set_cell(backend, "Staff", "Name", "Staff A", "Email", "")
    svc, gmail = make(backend)
    changes(svc, gmail)
    task_id = new_task(svc, "Laporan stress test, deadline 20 November 2026.", "m1")
    svc.store.update_task(task_id, {"Status": "Not Started"})
    assert changes(svc, gmail).not_sent == 1
    assert changes(svc, gmail).not_sent == 1
    flags = svc.store.task_by_source_id("m1").flags
    assert flags.count(NO_EMAIL_FLAG) == 1

    set_cell(backend, "Staff", "Name", "Staff A", "Email", "staff.a@example.com")
    assert changes(svc, gmail).emailed == 1


def test_failed_send_is_retried_next_run():
    svc, gmail = make()
    changes(svc, gmail)
    task_id = new_task(svc, "Laporan stress test, deadline 20 November 2026.", "m1")
    svc.store.update_task(task_id, {"Status": "Not Started"})

    def broken(*args):
        raise OSError("smtp")

    gmail.send = broken
    assert changes(svc, gmail).not_sent == 1
    del gmail.send
    assert changes(svc, gmail).emailed == 1


def test_row_added_by_hand_after_go_live_is_emailed():
    svc, gmail = make()
    changes(svc, gmail)
    task = Task(
        title="Manual", status="Not Started", doer="Staff P", owner="Staff P", source="Manual"
    )
    svc.store.append_task(task)
    run = changes(svc, gmail)
    assert run.emailed == 1 and gmail.sent[0][0].to == ["staff.p@example.com"]


# --- /jobs/digest ------------------------------------------------------------------------------


def digest(svc, gmail, now=None):
    return run_digest(reload(svc), gmail, SHEET, now or clock.now())


def test_digest_skips_empty_and_excludes_proposed():
    svc, gmail = make()
    new_task(svc, "Laporan stress test, deadline 27 Oktober 2026.", "m1")  # Proposed, due soon
    run = digest(svc, gmail)
    assert run.personal_sent == 0
    # the manager still hears about it as awaiting approval
    assert run.manager_sent
    [(email, _)] = gmail.sent
    assert email.to == ["manager@example.com"]
    assert "Awaiting approval (1)" in email.text
    assert "Overdue" not in email.text


def test_nothing_to_report_sends_nothing():
    svc, gmail = make()
    run = digest(svc, gmail)
    assert (run.personal_sent, run.manager_sent) == (0, False)
    assert gmail.sent == []


def test_personal_digest_sections():
    svc, gmail = make()
    overdue = new_task(svc, "Laporan stress test, paling lambat 20 Oktober 2026.", "m1")
    soon = new_task(svc, "Laporan stress test, deadline 30 Oktober 2026.", "m2")
    later = new_task(svc, "Laporan stress test, deadline 30 Desember 2026.", "m3")
    stale = new_task(svc, "Laporan stress test, deadline 31 Desember 2026.", "m4")
    for task_id in (overdue, soon, later):
        svc.store.update_task(task_id, {"Status": "Not Started"})
    svc.store.update_task(
        stale, {"Status": "In Progress", "Last updated": clock.now() - timedelta(days=6)}
    )
    run = digest(svc, gmail)
    assert run.personal_sent == 1
    personal = next(email for email, _ in gmail.sent if email.to == ["staff.a@example.com"])
    text = personal.text
    assert f"Overdue (1)\n  - {overdue}" in text
    assert f"Due within 7 days (1)\n  - {soon}" in text
    assert f"In progress, not updated recently (1)\n  - {stale}" in text
    assert later not in text


def test_manager_digest_lists_leaving_interns_who_own_reports_and_overload():
    backend = FakeSheetsBackend.from_fixtures()
    set_cell(backend, "Staff", "Name", "Staff F", "Internship end date", "2026-11-15")
    set_cell(backend, "Staff", "Name", "Staff G", "Weighted open load", 12)
    svc, gmail = make(backend)
    run = digest(svc, gmail)
    assert run.manager_sent
    text = gmail.sent[0][0].text
    assert "Interns leaving who still own Catalog reports (1)" in text
    assert "Staff F leaves 15 Nov 2026; default owner of" in text and "HR-DATA" in text
    assert "Staff G: load 12 (threshold 10)" in text


def test_manager_digest_counts_ai_unavailable_last_24h():
    svc, gmail = make()
    svc.store.append_task(
        Task(title="T", status="Not Started", doer="Staff A", flag="AI unavailable",
             start=TODAY, due_date=date(2026, 12, 1))
    )  # fmt: skip
    svc.store.append_task(
        Task(title="Old", status="Not Started", doer="Staff A", flag="AI unavailable",
             start=date(2026, 10, 1), due_date=date(2026, 12, 1))
    )  # fmt: skip
    digest(svc, gmail)
    assert "AI unavailable (last 24 h) (1)\n  - 1 request(s)" in gmail.sent[0][0].text


def test_no_manager_email_skips_manager_digest():
    backend = FakeSheetsBackend.from_fixtures()
    set_cell(backend, "Config", "Setting", "Manager email", "Value", "")
    svc, gmail = make(backend)
    new_task(svc, "Tolong cek printer", "m1")
    assert digest(svc, gmail).manager_sent is False


def test_digest_uses_now_for_staleness():
    svc, gmail = make()
    task_id = new_task(svc, "Laporan stress test, deadline 31 Desember 2026.", "m1")
    svc.store.update_task(task_id, {"Status": "In Progress"})
    assert digest(svc, gmail).personal_sent == 0
    later = datetime(2026, 11, 2, 9, 0, tzinfo=clock.TZ)
    assert digest(svc, FakeGmailClient(), later).personal_sent == 1
