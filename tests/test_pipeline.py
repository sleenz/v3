"""DESIGN section 4 end to end with fakes, including the section 12 acceptance list."""

from datetime import date, datetime

import pytest

from app import clock
from app.extract import ai as ai_module
from app.extract.ai import NullProvider
from app.models import Attachment, RawRequest
from app.pipeline import MAX_CELL_CHARS, Services, process_request
from tests.fakes import FakeAIProvider, FakeDriveStore, FakeSheetsBackend, FakeSheetStore

MANAGER = {"sender_name": "GRC Manager", "sender_email": "grc-manager@example.com"}

VALID_AI = {
    "title_en": "Submit Manpower Planning form and JPS deck",
    "summary_en": "BRI asks each subsidiary to fill the Manpower Planning form and deck. "
    "Submission is due 6 Nov 2026. Extra sentence.",
    "deliverables": ["Manpower Planning form (Form_MPP.xlsx)", "JPS presentation (JPS_MPP.pptx)"],
    "deadline": "2026-11-06",
    "deadline_quote": "Rabu 06 November 2026",
    "requester_org": "BRI",
    "contacts": [{"name": "Hana Setyaningsih", "email": None}],
    "catalog_id": "HR-DATA",
    "language": "id",
}


def make_services(ai=None, ai_setting="Off"):
    backend = FakeSheetsBackend.from_fixtures()
    for row in backend.tabs["Config"]:
        if row[0] == "AI provider":
            row[1] = ai_setting
    store = FakeSheetStore(backend)
    store.load()
    return Services(store=store, drive=FakeDriveStore(), ai=ai or NullProvider())


def email(text, source_id="msg-1", attachments=(), **extra):
    values = {
        "source": "Email",
        "source_id": source_id,
        "received_at": datetime(2026, 10, 26, 11, 30, tzinfo=clock.TZ),
        "subject": "Fwd: Joint Planning Session Manpower Planning",
        "body_text": text,
        "attachments": list(attachments),
        **MANAGER,
    }
    values.update(extra)
    return RawRequest(**values)


def manpower(fixture_text, **extra):
    attachments = [
        Attachment(filename="Form_MPP.xlsx", mime="application/vnd.ms-excel", data=b"xlsx"),
        Attachment(filename="JPS_MPP.pptx", mime="application/vnd.ms-powerpoint", data=b"pptx"),
    ]
    return email(fixture_text("manpower_planning.txt"), attachments=attachments, **extra)


def only_task(services):
    tasks = [task for task in services.store.tasks() if task.id.startswith("G-")]
    assert len(tasks) == 1
    return tasks[0]


# --- section 12, AI off ------------------------------------------------------------------------


def test_manpower_ai_off(fixture_text):
    services = make_services()
    raw = manpower(fixture_text)
    result = process_request(raw, services)
    assert result.created and result.task_id == "G-0001"

    task = only_task(services)
    assert task.requester == "BRI"
    assert task.notes == "Manager note: Tolong dicek ya"
    assert not task.title.lower().startswith("selamat")
    assert task.due_date == date(2026, 11, 6)
    assert "Weekday mismatch: Rabu = Wed, 06 Nov 2026 is Fri" in task.flags
    assert (task.catalog_id, task.division) == ("HR-DATA", "HR")
    assert task.routing_note.startswith("Rule 1:") and "Hana Setyaningsih" in task.routing_note
    assert task.owner == task.doer == "Staff F"
    assert task.status == "Proposed"
    assert task.original_message == raw.body_text
    assert task.start == date(2026, 10, 26)
    assert task.source == "Email" and task.source_id == "msg-1"
    assert "fajar@example.co.id" in task.contacts
    assert task.ai_summary == "" and task.deliverables == ""
    assert "AI unavailable" not in task.flags

    folder = services.drive.only_folder()
    assert set(folder["files"]) == {"original.txt", "Form_MPP.xlsx", "JPS_MPP.pptx"}
    assert raw.body_text.encode() in folder["files"]["original.txt"]
    assert folder["name"].startswith("G-0001 – ")
    assert task.files == result.files_url and task.files.startswith("https://drive.google.com/")

    audit = services.store.backend.tabs["AuditLog"]
    assert len(audit) == 2
    assert audit[1][audit[0].index("Action")] == "'Created"
    assert audit[1][audit[0].index("Reason")] == "'" + task.routing_note


def test_weight_formula_written(fixture_text):
    services = make_services()
    process_request(manpower(fixture_text), services)
    weight = services.store.backend.raw("Master", 3, "Weight")
    assert weight.startswith("=IFERROR(") and "S3" in weight


# --- section 12, AI on -------------------------------------------------------------------------


def test_valid_ai_result(fixture_text):
    ai = FakeAIProvider(VALID_AI)
    services = make_services(ai, "Gemini API key")
    process_request(manpower(fixture_text), services)
    task = only_task(services)
    assert "Manpower Planning" in task.title
    assert task.deliverables.count("\n") + 1 == 2
    assert task.ai_summary.count(". ") == 1  # trimmed to two sentences
    assert task.due_date == date(2026, 11, 6)
    assert not any(flag.startswith("AI deadline differs") for flag in task.flags)
    assert task.catalog_id == "HR-DATA"
    # the AI saw masked text, no phone numbers, and only file names
    [call] = ai.calls
    assert "0812-0000-0000" not in call["text"] and "[phone]" in call["text"]
    assert call["attachments"] == ["Form_MPP.xlsx", "JPS_MPP.pptx"]


def test_ai_deadline_with_bad_quote_is_dropped(fixture_text):
    bad = {**VALID_AI, "deadline": "2026-11-20", "deadline_quote": "20 November 2026"}
    services = make_services(FakeAIProvider(bad), "Gemini API key")
    process_request(email(fixture_text("no_date.txt")), services)
    task = only_task(services)
    assert task.due_date == date(2026, 11, 2)  # today + Default due (7 days)
    assert "No deadline found, default used" in task.flags


def test_ai_deadline_used_when_rules_find_none(fixture_text):
    text = fixture_text("no_date.txt") + "\nKirim sebelum akhir bulan depan (30 Nov)."
    result = {**VALID_AI, "deadline": "2026-11-30", "deadline_quote": "akhir bulan depan (30 Nov)"}
    services = make_services(FakeAIProvider(result), "Gemini API key")
    process_request(email(text), services)
    task = only_task(services)
    assert task.due_date == date(2026, 11, 30)


def test_ai_deadline_differs_keeps_rule_date(fixture_text):
    other = {**VALID_AI, "deadline": "2026-11-05", "deadline_quote": "Rabu 06 November 2026"}
    services = make_services(FakeAIProvider(other), "Gemini API key")
    process_request(manpower(fixture_text), services)
    task = only_task(services)
    assert task.due_date == date(2026, 11, 6)
    assert "AI deadline differs: 05 Nov 2026" in task.flags


def test_ai_timeout_continues_on_rules(fixture_text, monkeypatch):
    monkeypatch.setattr(ai_module, "TIMEOUT_SECONDS", 0.05)
    slow = FakeAIProvider(VALID_AI, delay=0.3)
    services = make_services(slow, "Gemini API key")
    process_request(manpower(fixture_text), services)
    task = only_task(services)
    assert "AI unavailable" in task.flags
    assert len(slow.calls) == 2  # one retry
    assert task.title.startswith("Dalam rangka")
    assert task.due_date == date(2026, 11, 6)


def test_gemini_location_error_falls_back_to_rules(fixture_text):
    from unittest.mock import MagicMock

    from app.extract.ai import GeminiProvider

    class LocationError(Exception):
        status_code = 400

    client = MagicMock()
    client.interactions.create.side_effect = LocationError("User location is not supported")
    provider = GeminiProvider("api_key", "gemini-3.8-flash", client_factory=lambda: client)
    services = make_services(provider, "Gemini API key")
    result = process_request(manpower(fixture_text), services)
    task = only_task(services)
    assert result.created
    assert "AI unavailable" in task.flags
    assert task.title.startswith("Dalam rangka")  # rule title
    assert task.due_date == date(2026, 11, 6)  # rule deadline
    assert task.catalog_id == "HR-DATA"


def test_ai_suggestion_drives_rule_4():
    suggestion = {**VALID_AI, "catalog_id": "FIN-KPMM", "deadline": None, "deadline_quote": None}
    services = make_services(FakeAIProvider(suggestion), "Gemini API key")
    process_request(email("Mohon dilengkapi lampiran terkait permodalan kuartal ini."), services)
    task = only_task(services)
    assert task.routing_note.startswith("Rule 1")  # "permodalan" is a FIN-KPMM keyword

    services = make_services(FakeAIProvider(suggestion), "Gemini API key")
    process_request(email("Mohon dilengkapi lampiran terlampir."), services)
    task = only_task(services)
    assert task.routing_note.startswith("Rule 4 (AI suggested)")
    assert task.catalog_id == "FIN-KPMM"


# --- other section 12 cases --------------------------------------------------------------------


def test_letter_code_routes_to_division(fixture_text):
    services = make_services()
    process_request(email(fixture_text("formal_letter_emr_irm.txt")), services)
    task = only_task(services)
    assert task.routing_note.startswith("Rule 3")
    assert task.division == "Policy and Risk"
    assert (task.letter_no, task.letter_code) == ("B.91.e-EMR/IRM/10/2026", "EMR/IRM")
    assert task.title == "Permintaan Data Eksposur Risiko Kantor Cabang Luar Negeri"
    assert task.due_date == date(2026, 11, 13)


def test_printer_needs_review(fixture_text):
    services = make_services()
    process_request(email(fixture_text("printer.txt")), services)
    task = only_task(services)
    assert task.owner == "" and task.doer == ""
    assert "Needs review: no owner" in task.flags
    assert "No deadline found, default used" in task.flags


def test_duplicate_source_id_writes_one_row(fixture_text):
    services = make_services()
    first = process_request(manpower(fixture_text), services)
    second = process_request(manpower(fixture_text), services)
    assert first.created and not second.created
    assert second.task_id == first.task_id
    only_task(services)
    assert len(services.store.backend.tabs["AuditLog"]) == 2


def test_retry_after_failure_reuses_folder_and_skips_uploaded_files(fixture_text):
    services = make_services()
    services.drive.fail_upload = "JPS_MPP.pptx"
    with pytest.raises(OSError):
        process_request(manpower(fixture_text), services)
    assert [t for t in services.store.tasks() if t.id.startswith("G-")] == []

    services.drive.fail_upload = None
    process_request(manpower(fixture_text), services)
    folder = services.drive.only_folder()
    assert set(folder["files"]) == {"original.txt", "Form_MPP.xlsx", "JPS_MPP.pptx"}
    only_task(services)


def test_file_over_25_mb_is_skipped_and_flagged():
    big = Attachment(filename="big.zip", mime="application/zip", data=b"0" * (25 * 1024 * 1024 + 1))
    small = Attachment(filename="small.pdf", mime="application/pdf", data=b"pdf")
    services = make_services()
    process_request(email("Mohon kirim laporan.", attachments=[big, small]), services)
    task = only_task(services)
    assert "File skipped (over 25 MB): big.zip" in task.flags
    assert set(services.drive.only_folder()["files"]) == {"original.txt", "small.pdf"}


def test_long_original_is_cut_in_cell_but_full_in_file():
    text = "Mohon kirim laporan.\n" + "x" * 50_000
    services = make_services()
    process_request(email(text), services)
    task = only_task(services)
    assert len(task.original_message) == MAX_CELL_CHARS
    assert any(flag.startswith("Original message cut to 45,000") for flag in task.flags)
    assert text.encode() in services.drive.only_folder()["files"]["original.txt"]


def test_intern_ending_before_deadline_not_proposed(fixture_text):
    services = make_services()
    staff = services.store.backend.tabs["Staff"]
    end_col = staff[0].index("Internship end date")
    for row in staff[1:]:
        if row[staff[0].index("Name")] == "Staff F":
            row[end_col] = "2026-11-01"
    services.store.load()
    process_request(manpower(fixture_text), services)
    task = only_task(services)
    assert task.owner == "Staff P"  # the HR-DATA backup
    assert task.routing_note.startswith("Rule 2")


def test_html_only_email_and_chat_source():
    services = make_services()
    html = "<html><body><p>Hi team,</p><p>Please send the <b>stress test</b> by 20/11/2026.</p>"
    process_request(email("", body_html=html, subject="Stress test"), services)
    task = only_task(services)
    assert task.title.startswith("Please send the stress test")
    assert task.due_date == date(2026, 11, 20)
    assert task.catalog_id == "RM-STRESS"

    services = make_services()
    chat = RawRequest(
        source="Google Chat",
        source_id="spaces/x/messages/y",
        received_at=datetime(2026, 10, 26, 10, 0, tzinfo=clock.TZ),
        body_text="From: someone\nSubject: x\nDate: today\n\nTolong cek printer",
        submitted_by="staff.a@example.com",
    )
    process_request(chat, services)
    task = only_task(services)
    assert task.source == "Google Chat"
    assert task.notes == ""  # chat text is never unwrapped as a forward


def test_recurring_source_column():
    services = make_services()
    raw = RawRequest(
        source="Recurring",
        source_id="Recurring HR-DATA 2026-11",
        received_at=datetime(2026, 10, 26, 7, 0, tzinfo=clock.TZ),
        body_text="Employee data & talent mapping, due 15 November 2026",
    )
    process_request(raw, services)
    assert only_task(services).source == "Recurring HR-DATA 2026-11"


def test_empty_message_title_falls_back_to_subject():
    services = make_services()
    process_request(email("", subject="FW: RE: Data request"), services)
    assert only_task(services).title == "Data request"


def test_subject_fallback_title_is_capped():
    services = make_services()
    process_request(email("", subject="Fwd: " + "Permintaan data " * 20), services)
    title = only_task(services).title
    assert len(title) <= 90 and title.endswith("…")


# --- File storage = None (v1.2 decision A: no Drive) --------------------------------------------


def no_drive_services():
    from app.drive import NoFileStorage

    backend = FakeSheetsBackend.from_fixtures()
    for row in backend.tabs["Config"]:
        if row[0] == "File storage":
            row[1] = "None"
    store = FakeSheetStore(backend)
    store.load()
    return Services(store=store, drive=NoFileStorage(), ai=NullProvider())


def test_no_file_storage_never_calls_drive_and_names_attachments(fixture_text):
    services = no_drive_services()  # NoFileStorage raises if the pipeline touches Drive
    result = process_request(manpower(fixture_text), services)
    task = only_task(services)
    assert result.created
    assert task.files == (
        "In the original email in the GRC bot mailbox: Form_MPP.xlsx, JPS_MPP.pptx"
    )
    assert task.catalog_id == "HR-DATA" and task.due_date == date(2026, 11, 6)
    assert not any("over 25 MB" in flag for flag in task.flags)


def test_no_file_storage_without_attachments_leaves_files_blank():
    services = no_drive_services()
    process_request(email("Tolong cek printer"), services)
    assert only_task(services).files == ""


def test_no_file_storage_long_original_is_flagged_as_only_in_the_email():
    services = no_drive_services()
    process_request(email("Mohon kirim laporan.\n" + "x" * 50_000), services)
    task = only_task(services)
    assert len(task.original_message) == MAX_CELL_CHARS
    assert any("full text only in the original email" in flag for flag in task.flags)


def test_no_file_storage_chat_attachments_are_flagged_not_stored():
    services = no_drive_services()
    chat = RawRequest(
        source="Google Chat",
        source_id="spaces/x/messages/z",
        received_at=datetime(2026, 10, 26, 10, 0, tzinfo=clock.TZ),
        body_text="Laporan stress test, deadline 20 November 2026.",
        attachments=[Attachment(filename="scan.pdf", mime="application/pdf", data=b"")],
    )
    process_request(chat, services)
    task = only_task(services)
    assert task.files == "Not stored: scan.pdf"
    assert any(flag.startswith("Chat attachments not stored") for flag in task.flags)


def test_file_storage_defaults_to_none():
    from app.models import AppConfig
    from tests.test_error_paths import config_values

    assert AppConfig.model_validate(config_values()).file_storage == "None"
    assert AppConfig.model_validate(config_values(**{"File storage": ""})).file_storage == "None"


# --- PDFs to the AI (v1.2 item 11) ---------------------------------------------------------------

LETTER = Attachment(filename="Surat_OJK.pdf", mime="application/pdf", data=b"%PDF-1.4 surat")
SHEET = Attachment(filename="Form.xlsx", mime="application/vnd.ms-excel", data=b"xlsx")


def set_config(services, name, value):
    services.store.backend.tabs["Config"].append([name, value])
    services.store.load()


def test_email_pdfs_go_to_the_ai_other_files_by_name(fixture_text):
    ai = FakeAIProvider(VALID_AI)
    services = make_services(ai, "Gemini API key")
    process_request(email(fixture_text("no_date.txt"), attachments=[LETTER, SHEET]), services)
    [call] = ai.calls
    assert call["documents"] == ["Surat_OJK.pdf"]
    assert call["attachments"] == ["Surat_OJK.pdf", "Form.xlsx"]


def test_ai_reads_pdfs_off_sends_names_only(fixture_text):
    ai = FakeAIProvider(VALID_AI)
    services = make_services(ai, "Gemini API key")
    set_config(services, "AI reads PDFs", "FALSE")
    process_request(email(fixture_text("no_date.txt"), attachments=[LETTER]), services)
    assert ai.calls[0]["documents"] == []


def test_chat_attachments_never_go_to_the_ai(fixture_text):
    ai = FakeAIProvider(VALID_AI)
    services = make_services(ai, "Gemini API key")
    chat = email(fixture_text("no_date.txt"), attachments=[LETTER], source="Google Chat")
    process_request(chat, services)
    assert ai.calls[0]["documents"] == []


def test_deadline_only_in_the_pdf_is_used_and_flagged(fixture_text):
    from_pdf = {**VALID_AI, "deadline": "2026-11-20", "deadline_quote": "20 November 2026"}
    services = make_services(FakeAIProvider(from_pdf), "Gemini API key")
    process_request(email(fixture_text("no_date.txt"), attachments=[LETTER]), services)
    task = only_task(services)
    assert task.due_date == date(2026, 11, 20)
    assert ai_module.PDF_DEADLINE_FLAG in task.flags
    assert "No deadline found, default used" not in task.flags


def test_too_many_pdfs_are_flagged(fixture_text):
    ai = FakeAIProvider(VALID_AI)
    services = make_services(ai, "Gemini API key")
    pdfs = [
        Attachment(filename=f"p{i}.pdf", mime="application/pdf", data=b"%PDF") for i in range(4)
    ]
    process_request(email(fixture_text("no_date.txt"), attachments=pdfs), services)
    assert ai.calls[0]["documents"] == ["p0.pdf", "p1.pdf", "p2.pdf"]
    assert ai_module.PDFS_SKIPPED_FLAG in only_task(services).flags
