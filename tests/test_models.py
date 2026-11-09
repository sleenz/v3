from datetime import date, datetime

import pytest
from pydantic import ValidationError

from app.models import (
    MASTER_REQUIRED_COLUMNS,
    AIResult,
    Attachment,
    CatalogEntry,
    Contact,
    LetterCode,
    RawRequest,
    StaffMember,
    Task,
)


def test_staff_from_sheet_row_with_blanks():
    staff = StaffMember.model_validate(
        {
            "Name": "Staff A",
            "Email": "a@example.com",
            "Main division": "HR",
            "Role": "Intern",
            "Internship end date": "",
            "Capacity (1 = full time)": "",
            "Weighted open load": "",
        }
    )
    assert staff.internship_end is None
    assert staff.capacity == 1.0
    assert staff.weighted_open_load == 0.0


@pytest.mark.parametrize("typed", ["31/12/2026", "31-12-2026", "31 Dec 2026", "31 December 2026"])
def test_staff_internship_end_typed_as_text(typed):
    staff = StaffMember.model_validate({"Name": "C", "Internship end date": typed})
    assert staff.internship_end == date(2026, 12, 31)


def test_staff_internship_end_that_is_not_a_date_is_refused():
    with pytest.raises(ValidationError):
        StaffMember.model_validate({"Name": "C", "Internship end date": "end of year"})


@pytest.mark.parametrize("value", ["Yes", "No", 46022, ""])
def test_staff_whatsapp_opt_in_is_free_text(value):
    staff = StaffMember.model_validate({"Name": "D", "WhatsApp opt-in": value})
    assert isinstance(staff.whatsapp_opt_in, str)


def test_staff_zero_capacity_treated_as_full_time():
    staff = StaffMember.model_validate({"Name": "B", "Capacity (1 = full time)": 0})
    assert staff.capacity == 1.0


def test_staff_rejects_unknown_role():
    with pytest.raises(ValidationError):
        StaffMember.model_validate({"Name": "B", "Role": "Boss"})


def test_catalog_lists_and_flags():
    entry = CatalogEntry.model_validate(
        {
            "Catalog ID": "HR-DATA",
            "Match keywords": "manpower, mpp ,  ",
            "Known contacts": "Hana Setyaningsih, hana@example.co.id",
            "Confirmed by manager?": "Yes",
            "Typical due day": 10.0,
        }
    )
    assert entry.match_keywords == ["manpower", "mpp"]
    assert entry.known_contacts == ["Hana Setyaningsih", "hana@example.co.id"]
    assert entry.confirmed is True
    assert entry.typical_due_day == "10"
    assert not entry.is_adhoc
    assert CatalogEntry.model_validate({"Catalog ID": "ADHOC-1"}).is_adhoc


def test_letter_code_aliases():
    code = LetterCode.model_validate({"Letter code": "EMR/IRM", "Route to division": "Risk"})
    assert code.route_to_division == "Risk"


def test_task_round_trip_by_header():
    task = Task(id="G-0001", title="T", due_date=date(2026, 11, 6), flag="a; b ;")
    row = task.model_dump(by_alias=True)
    assert row["ID"] == "G-0001"
    assert row["Requester / Regulator"] == ""
    assert row["Status"] == "Proposed"
    assert task.flags == ["a", "b"]
    assert Task.model_validate({**row, "Due date": "", "Unknown column": 1}).due_date is None


def test_task_rejects_unknown_status():
    with pytest.raises(ValidationError):
        Task(status="Waiting")


def test_master_required_columns_cover_design():
    for header in ("ID", "Weight", "Original message", "Source ID", "Requester / Regulator"):
        assert header in MASTER_REQUIRED_COLUMNS


def test_raw_request_and_attachment():
    raw = RawRequest(
        source="Email",
        source_id="m1",
        received_at=datetime(2026, 10, 26, 9),
        attachments=[Attachment(filename="a.xlsx", mime="application/x", data=b"123")],
    )
    assert raw.attachments[0].size == 3
    assert "123" not in repr(raw.attachments[0])


def _ai(**overrides):
    payload = {
        "title_en": "Manpower Planning submission",
        "summary_en": "Fill the form. Send the deck. Then wait.",
        "deliverables": ["Form", "Deck"],
        "deadline": "2026-11-06",
        "deadline_quote": "Rabu 06 November 2026",
        "requester_org": "BRI",
        "contacts": [{"name": "Hana", "email": None}],
        "catalog_id": "HR-DATA",
        "language": "id",
    }
    payload.update(overrides)
    return AIResult.model_validate_json(__import__("json").dumps(payload))


def test_ai_result_parses_json_and_trims_summary():
    result = _ai()
    assert result.deadline == date(2026, 11, 6)
    assert result.summary_en == "Fill the form. Send the deck."
    assert result.contacts == [Contact(name="Hana")]


def test_ai_result_trims_long_title():
    result = _ai(title_en="word " * 40)
    assert len(result.title_en) <= 90
    assert result.title_en.endswith("…")


@pytest.mark.parametrize(
    "overrides",
    [{"language": "fr"}, {"extra_field": 1}, {"deliverables": "Form"}, {"deadline": "6 Nov"}],
)
def test_ai_result_strict(overrides):
    with pytest.raises(ValidationError):
        _ai(**overrides)


def test_ai_reads_pdfs_blank_means_on_and_no_turns_it_off():
    from tests.fakes import FakeSheetsBackend, FakeSheetStore

    for value, expected in (("", True), ("Yes", True), ("FALSE", False), ("No", False)):
        backend = FakeSheetsBackend.from_fixtures()
        backend.tabs["Config"].append(["AI reads PDFs", value])
        store = FakeSheetStore(backend)
        store.load()
        assert store.config().ai_reads_pdfs is expected
