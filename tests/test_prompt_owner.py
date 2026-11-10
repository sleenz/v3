"""Reading the sender's PROMPT (DESIGN section 21): the AI first, the rules as fallback and check.

Names here are made up; the AI is a fake, so nothing touches the network.
"""

from datetime import date, datetime

import pytest

from app import clock
from app.extract.ai import NullProvider, read_prompt
from app.models import RawRequest, StaffMember
from app.pipeline import AI_READ_FLAG, Services, decide_prompt, process_request
from app.route import fuzzy_staff_name, staff_mentioned
from tests.conftest import TODAY
from tests.fakes import FakeAIProvider, FakeDriveStore, FakeSheetsBackend, FakeSheetStore

TEAM = [
    StaffMember(name="Ana Putri"),
    StaffMember(name="Budi Santoso"),
    StaffMember(name="Budi Hartono"),
    StaffMember(name="Rina Wijaya"),
]
FRIDAY = date(2026, 10, 30)  # TODAY is Monday 26 Oct 2026


def decide(prompt: str, ai=None):
    return decide_prompt(prompt, TODAY, TEAM, ai or NullProvider())


# --- the rules alone (AI off or failed) -----------------------------------------------------


@pytest.mark.parametrize(
    ("prompt", "owner"),
    [
        ("assign this to ana", "Ana Putri"),
        ("assign ana to do this, deadline tomorrow", "Ana Putri"),
        ("ask rina to make it", "Rina Wijaya"),
        ("assign blablabla blablabla ana", "Ana Putri"),
        ("assign the report for this quarter to budi santoso please", "Budi Santoso"),
        ("assign this to Anaa", "Ana Putri"),  # a typo
        ("ask Rinna to do it", "Rina Wijaya"),
        ("assign blabla ana with reviewer rina", "Ana Putri"),  # the reviewer is not the owner
    ],
)
def test_rules_find_the_owner(prompt, owner):
    assert decide(prompt).owner == owner


def test_rules_flags():
    assert decide("assign this to Anaa").flags == ['PROMPT: "Anaa" read as Ana Putri']
    assert decide("assign this to Ana").flags == []
    assert decide("assign this to budi").flags == ['PROMPT: "budi" not found in Staff']
    assert decide("assign whoever").flags == ["PROMPT: couldn't read who to assign"]


def test_an_ai_failure_falls_back_to_the_rules():
    ai = FakeAIProvider(error=TimeoutError())
    decision = decide("ask rina to do it with reviewer ana, deadline this friday", ai)
    assert (decision.owner, decision.reviewer, decision.due) == ("Rina Wijaya", "Ana Putri", FRIDAY)
    assert AI_READ_FLAG not in decision.flags


# --- the AI first ---------------------------------------------------------------------------


def test_the_ai_reads_owner_reviewer_and_deadline():
    reading = {"owner": "Rinaa", "reviewer": "Ana", "deadline": "2026-11-04"}
    ai = FakeAIProvider(reading=reading)
    prompt = "can Rinaa from finance handle it, Ana checks it, needed by end of next week"
    decision = decide(prompt, ai)
    assert (decision.owner, decision.reviewer) == ("Rina Wijaya", "Ana Putri")
    assert decision.due == date(2026, 11, 4)
    assert decision.flags[0] == AI_READ_FLAG
    assert 'PROMPT: owner "Rinaa" read as Rina Wijaya' in decision.flags
    assert ai.prompt_calls == [prompt]  # only the PROMPT line goes to the AI


def test_a_rules_deadline_beats_a_different_ai_date():
    ai = FakeAIProvider(reading={"owner": "ana", "deadline": "2026-11-06"})
    decision = decide("assign to ana, deadline this friday", ai)
    assert decision.due == FRIDAY
    assert "PROMPT: deadline unclear (AI read 06 Nov 2026) - check" in decision.flags


def test_an_ai_owner_that_differs_from_the_rules_is_flagged():
    ai = FakeAIProvider(reading={"owner": "Rina"})
    decision = decide("assign to ana, or Rina if she is busy", ai)
    assert decision.owner == "Rina Wijaya"
    assert "PROMPT: owner unclear (rules read Ana Putri) - check" in decision.flags


def test_names_not_in_the_line_and_odd_dates_are_dropped():
    ai = FakeAIProvider(
        reading={"owner": "Ana Putri", "reviewer": "Rina", "deadline": "2026-01-01"}
    )
    reading = read_prompt(ai, "assign this to whoever is free", TODAY)
    assert (reading.owner, reading.reviewer, reading.deadline) == (None, None, None)
    decision = decide("assign this to whoever is free", ai)
    assert decision.owner is None and AI_READ_FLAG not in decision.flags


def test_an_ai_name_not_in_staff_falls_back_to_the_rules():
    ai = FakeAIProvider(reading={"owner": "Joko"})
    assert decide("ask Joko to do it", ai).flags == ['PROMPT: "Joko" not found in Staff']
    decision = decide("give it to Joko or ana, assign ana", ai)
    assert decision.owner == "Ana Putri" and AI_READ_FLAG not in decision.flags


def test_fuzzy_and_mentions():
    assert fuzzy_staff_name("Rinna", TEAM).name == "Rina Wijaya"
    assert fuzzy_staff_name("Budy", TEAM) is None  # two Budis equally close
    assert fuzzy_staff_name("xx", TEAM) is None
    assert [p.name for p in staff_mentioned("give it to ana or budi hartono", TEAM)] == [
        "Ana Putri",
        "Budi Hartono",
    ]


def test_pipeline_uses_the_ai_reading_for_an_email_prompt():
    backend = FakeSheetsBackend.from_fixtures()
    for row in backend.tabs["Config"]:
        if row[0] == "AI provider":
            row[1] = "Gemini API key"
    store = FakeSheetStore(backend)
    store.load()
    ai = FakeAIProvider(
        reading={"owner": "Staf P", "reviewer": "Staff D", "deadline": "2026-10-28"}
    )
    services = Services(store=store, drive=FakeDriveStore(), ai=ai)
    line = "whoever is Staf P handles it, Staff D reviews, need it the day after tomorrow"
    raw = RawRequest(
        source="Email",
        source_id="m-ai",
        received_at=datetime(2026, 10, 26, 11, 30, tzinfo=clock.TZ),
        subject="Stress test",
        body_text=f"Mohon kirim laporan stress test.\nPROMPT: {line}",
        sender_name="Staff A",
        sender_email="staff.a@example.com",
    )
    result = process_request(raw, services)
    [task] = [t for t in store.tasks() if t.id.startswith("G-")]
    assert (task.owner, task.reviewer, task.due_date) == ("Staff P", "Staff D", date(2026, 10, 28))
    assert task.status == "Proposed" and result.due_from_prompt
    assert AI_READ_FLAG in task.flags
    assert ai.prompt_calls == [line]
