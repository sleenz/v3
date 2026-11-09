from datetime import date

import pytest

from app.models import StaffMember, Task
from app.notify import MissingEmail, assignment_email

STAFF = [
    StaffMember(name="Staff F", email="staff.f@example.com"),
    StaffMember(name="Staff P", email="staff.p@example.com"),
    StaffMember(name="Staff Q", email=""),
]
SHEET = "https://docs.google.com/spreadsheets/d/sheet-1"


def task(**overrides):
    values = {
        "id": "G-0012",
        "title": "Manpower Planning <form>",
        "due_date": date(2026, 11, 6),
        "doer": "Staff F",
        "owner": "Staff P",
        "division": "HR",
        "flag": "Weekday mismatch: Rabu = Wed, 06 Nov 2026 is Fri",
        "routing_note": 'Rule 1: catalog "Employee data" (Hana Setyaningsih) → default owner',
        "ai_summary": "BRI asks for the form.",
        "deliverables": "- Form\n- Deck",
        "files": "https://drive.google.com/drive/folders/f1",
        "original_message": "Selamat siang,\n<script>x</script> deadline Rabu 06 November 2026",
    }
    values.update(overrides)
    return Task(**values)


def test_assignment_email_content():
    out = assignment_email(task(), STAFF, SHEET)
    assert out.to == ["staff.f@example.com"]
    assert out.cc == ["staff.p@example.com"]
    assert out.subject == "[GRC] G-0012 – Manpower Planning <form> (due 06 Nov 2026)"
    for expected in (
        "Due date: 06 Nov 2026",
        "Flags: Weekday mismatch",
        "Division: HR",
        "Owner: Staff P",
        "Routing note: Rule 1",
        "AI summary: BRI asks for the form.",
        "  - Form",
        "  - Deck",
        "Files: https://drive.google.com/drive/folders/f1",
        f"Sheet: {SHEET}",
    ):
        assert expected in out.text
    original_at = out.text.index("Original message")
    assert out.text.index("Files:") < original_at
    assert out.text.endswith(task().original_message)
    assert "&lt;script&gt;" in out.html and "<script>" not in out.html
    assert "href='https://drive.google.com/drive/folders/f1'" in out.html
    assert "<li>Deck</li>" in out.html


def test_no_cc_when_owner_is_doer_and_no_ai_sections():
    out = assignment_email(task(owner="Staff F", ai_summary="", deliverables=""), STAFF, SHEET)
    assert out.cc == []
    assert "AI summary" not in out.text and "Deliverables" not in out.text


def test_missing_doer_email():
    with pytest.raises(MissingEmail):
        assignment_email(task(doer="Staff Q"), STAFF, SHEET)
    with pytest.raises(MissingEmail):
        assignment_email(task(doer="Nobody"), STAFF, SHEET)
