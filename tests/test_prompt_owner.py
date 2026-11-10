"""Who the sender's PROMPT names as owner (DESIGN section 21): rules first, the AI last.

Names here are made up; the AI is a fake, so nothing touches the network.
"""

from datetime import datetime

import pytest

from app import clock
from app.extract.ai import NullProvider, read_assignee
from app.extract.prompt import overrides
from app.models import RawRequest, StaffMember
from app.pipeline import Services, choose_sender_owner, process_request
from app.route import fuzzy_staff_name, staff_mentioned
from tests.conftest import TODAY
from tests.fakes import FakeAIProvider, FakeDriveStore, FakeSheetsBackend, FakeSheetStore

TEAM = [
    StaffMember(name="Ana Putri"),
    StaffMember(name="Budi Santoso"),
    StaffMember(name="Budi Hartono"),
    StaffMember(name="Rina Wijaya"),
]


def choose(prompt: str, ai=None):
    return choose_sender_owner(prompt, overrides(prompt, TODAY), TEAM, ai or NullProvider())


@pytest.mark.parametrize(
    ("prompt", "owner"),
    [
        ("assign this to ana", "Ana Putri"),
        ("assign ana to do this, deadline tomorrow", "Ana Putri"),
        ("ask rina to make it", "Rina Wijaya"),
        ("assign blablabla blablabla ana", "Ana Putri"),  # a Staff name anywhere after "assign"
        ("assign the report for this quarter to budi santoso please", "Budi Santoso"),
        ("assign this to Anaa", "Ana Putri"),  # a typo
        ("ask Rinna to do it", "Rina Wijaya"),
    ],
)
def test_rules_find_the_owner(prompt, owner):
    choice = choose(prompt)
    assert choice.name == owner


def test_a_corrected_typo_is_flagged_for_the_manager():
    assert choose("assign this to Anaa").flags == ['PROMPT: "Anaa" read as Ana Putri']
    assert choose("assign this to Ana").flags == []


def test_the_reviewer_is_not_read_as_the_owner():
    choice = choose("assign blabla ana with reviewer rina")
    assert choice.name == "Ana Putri"


def test_an_ambiguous_first_name_is_not_guessed():
    choice = choose("assign this to budi")  # two Budis
    assert choice.name is None and choice.flags == ['PROMPT: "budi" not found in Staff']


def test_the_ai_is_the_fallback_and_reads_only_the_prompt_line():
    ai = FakeAIProvider(assignee="Ana")
    choice = choose("tolong yang handle laporan ini Ana saja", ai)
    assert choice.name is None  # no assign word: nothing asked, the AI is not called
    assert ai.assignee_calls == []
    ai = FakeAIProvider(assignee="Anna")
    choice = choose("assign it over to the person called Anna, the new one", ai)
    assert choice.name == "Ana Putri"
    assert choice.flags == ["PROMPT: owner read by AI as Ana Putri - check"]
    ai = FakeAIProvider(assignee="nomor satu")
    prompt = "minta yang nomor satu kerjakan"
    assert choose(prompt, ai).flags == ['PROMPT: "nomor satu" not found in Staff']
    assert ai.assignee_calls == [prompt]


def test_the_ai_name_is_matched_to_staff_and_flagged():
    ai = FakeAIProvider(assignee="Rinaa")
    choice = choose("assign, if possible, the girl Rinaa from finance", ai)
    assert choice.name == "Rina Wijaya"


def test_an_ai_name_that_is_not_in_the_line_is_refused():
    ai = FakeAIProvider(assignee="Ana Putri")  # invented: the line names nobody
    assert read_assignee(ai, "assign this to the usual person") == ""
    choice = choose("assign this to the usual person", ai)
    assert choice.name is None
    assert choice.flags == ['PROMPT: "the usual person" not found in Staff']


def test_ai_failure_or_no_ai_flags_instead_of_guessing():
    for ai in (FakeAIProvider(error=TimeoutError()), NullProvider()):
        choice = choose("ask about it, assign whoever", ai)
        assert choice.name is None and choice.flags


def test_fuzzy_and_mentions():
    assert fuzzy_staff_name("Rinna", TEAM).name == "Rina Wijaya"
    assert fuzzy_staff_name("Budy", TEAM) is None  # two Budis equally close
    assert fuzzy_staff_name("xx", TEAM) is None
    assert [p.name for p in staff_mentioned("give it to ana or budi hartono", TEAM)] == [
        "Ana Putri",
        "Budi Hartono",
    ]


def test_pipeline_uses_the_ai_fallback_for_an_email_prompt():
    backend = FakeSheetsBackend.from_fixtures()
    for row in backend.tabs["Config"]:
        if row[0] == "AI provider":
            row[1] = "Gemini API key"
    store = FakeSheetStore(backend)
    store.load()
    ai = FakeAIProvider(assignee="Staf P")  # the AI copies the sender's typo
    services = Services(store=store, drive=FakeDriveStore(), ai=ai)
    body = "Mohon kirim laporan stress test.\nPROMPT: assign whoever is Staf P, deadline tomorrow"
    raw = RawRequest(
        source="Email",
        source_id="m-ai",
        received_at=datetime(2026, 10, 26, 11, 30, tzinfo=clock.TZ),
        subject="Stress test",
        body_text=body,
        sender_name="Staff A",
        sender_email="staff.a@example.com",
    )
    process_request(raw, services)
    [task] = [t for t in store.tasks() if t.id.startswith("G-")]
    assert task.owner == "Staff P" and task.status == "Proposed"
    assert "PROMPT: owner read by AI as Staff P - check" in task.flags
    assert ai.assignee_calls == ["assign whoever is Staf P, deadline tomorrow"]
