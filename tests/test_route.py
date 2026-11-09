from datetime import date

import pytest

from app.models import CatalogEntry, Contact, LetterCode, StaffMember
from app.route import (
    NEEDS_REVIEW_FLAG,
    load_ratio,
    route,
    score_catalog,
    unavailable_reason,
)
from tests.conftest import TODAY
from tests.fakes import FakeSheetStore

DUE = date(2026, 11, 6)


@pytest.fixture(scope="module")
def sheet():
    store = FakeSheetStore()
    store.load()
    return store


def person(name, division="Policy and Risk", role="Staff", load=2.0, capacity=1.0, end=None):
    return StaffMember(
        name=name,
        main_division=division,
        role=role,
        weighted_open_load=load,
        capacity=capacity,
        internship_end=end,
    )


def entry(catalog_id, keywords="", owner="", backup="", division="Policy and Risk", **extra):
    return CatalogEntry.model_validate(
        {
            "Catalog ID": catalog_id,
            "Report type": f"Report {catalog_id}",
            "Division": division,
            "Default owner": owner,
            "Backup": backup,
            "Match keywords": keywords,
            **extra,
        }
    )


def run(
    sheet, text="", *, staff=None, catalog=None, contacts=(), letter_code=None, due=DUE, ai=None
):
    return route(
        text=text,
        contacts=list(contacts),
        letter_code=letter_code,
        due=due,
        catalog=catalog if catalog is not None else sheet.catalog(),
        staff=staff if staff is not None else sheet.staff(),
        letter_codes=sheet.letter_codes(),
        config=sheet.config(),
        today=TODAY,
        ai_catalog_id=ai,
    )


# --- scoring -----------------------------------------------------------------------------------


def test_score_whole_words_hyphens_and_once_per_keyword():
    catalog = [entry("A", "stress test, ras"), entry("B", "atmr")]
    match = score_catalog("Stress-test stress test RAS; transaksi", [], catalog)
    assert match is not None
    assert match.entry.catalog_id == "A"
    assert match.score == len("stress test") + len("ras")
    assert match.matched == ("stress test", "ras")


def test_score_ignores_partial_words_and_adhoc():
    catalog = [entry("A", "ras"), entry("ADHOC-X", "printer")]
    assert score_catalog("Laporan rasio dan printer", [], catalog) is None


def test_known_contact_scores_100_by_name_or_email():
    catalog = [
        entry("A", "laporan bulanan, data", **{"Known contacts": "Hana Setyaningsih"}),
        entry("B", "laporan bulanan", **{"Known contacts": "x@example.com"}),
    ]
    by_name = score_catalog("data laporan bulanan", [Contact(name="hana setyaningsih")], catalog)
    assert by_name is not None and by_name.entry.catalog_id == "A"
    assert by_name.score == 100 + len("laporan bulanan") + len("data")
    by_email = score_catalog("laporan bulanan", [Contact(email="X@example.com")], catalog)
    assert by_email is not None and by_email.entry.catalog_id == "B"


def test_tie_prefers_confirmed_then_id():
    catalog = [entry("B", "capex"), entry("C", "capex", **{"Confirmed by manager?": "Yes"})]
    assert score_catalog("capex", [], catalog).entry.catalog_id == "C"
    catalog = [entry("B", "capex"), entry("A", "capex")]
    assert score_catalog("capex", [], catalog).entry.catalog_id == "A"


# --- availability ------------------------------------------------------------------------------


def test_availability():
    assert unavailable_reason(person("M", role="Manager"), TODAY, DUE) == "is a manager"
    early = person("I", role="Intern", end=date(2026, 11, 1))
    assert "before the due date" in unavailable_reason(early, TODAY, DUE)
    gone = person("I", role="Intern", end=date(2026, 10, 1))
    assert "ended" in unavailable_reason(gone, TODAY, DUE)
    assert unavailable_reason(person("I", role="Intern", end=DUE), TODAY, DUE) is None
    assert (
        unavailable_reason(person("I", role="Intern", end=date(2026, 11, 1)), TODAY, None) is None
    )


def test_load_ratio_uses_capacity():
    assert load_ratio(person("P", load=3, capacity=0.5)) == 6


# --- rules -------------------------------------------------------------------------------------


def test_rule_1_default_owner(sheet):
    result = run(sheet, "Mohon kirim laporan stress test triwulan III")
    assert (result.rule, result.owner, result.catalog_id, result.division) == (
        1,
        "Staff A",
        "RM-STRESS",
        "Policy and Risk",
    )
    assert result.note == 'Rule 1: catalog "Stress test" (stress test) → default owner'
    assert result.flags == []


def test_rule_2_backup_when_owner_overloaded(sheet):
    staff = [person("Owner", load=11), person("Backup", load=5)]
    catalog = [entry("A", "capex", owner="Owner", backup="Backup")]
    result = run(sheet, "capex", staff=staff, catalog=catalog)
    assert (result.rule, result.owner) == (2, "Backup")
    assert "Owner is overloaded (load 11 > 10)" in result.note


def test_overload_uses_capacity(sheet):
    staff = [person("Owner", load=6, capacity=0.5), person("Backup")]
    catalog = [entry("A", "capex", owner="Owner", backup="Backup")]
    assert run(sheet, "capex", staff=staff, catalog=catalog).owner == "Backup"


def test_load_at_threshold_is_not_overloaded(sheet):
    staff = [person("Owner", load=10), person("Backup")]
    catalog = [entry("A", "capex", owner="Owner", backup="Backup")]
    assert run(sheet, "capex", staff=staff, catalog=catalog).owner == "Owner"


def test_rule_2_when_owner_is_intern_leaving_before_due(sheet):
    staff = [person("Owner", role="Intern", end=date(2026, 11, 2)), person("Backup")]
    catalog = [entry("A", "capex", owner="Owner", backup="Backup")]
    result = run(sheet, "capex", staff=staff, catalog=catalog)
    assert (result.rule, result.owner) == (2, "Backup")
    assert "internship ends 02 Nov 2026, before the due date" in result.note


def test_rule_3_when_owner_and_backup_unavailable(sheet):
    staff = [
        person("Owner", load=20),
        person("Backup", role="Manager"),
        person("Zed", load=1),
        person("Amy", load=1),
        person("Busy", load=3),
        person("Other division", division="HR", load=0),
    ]
    catalog = [entry("A", "capex", owner="Owner", backup="Backup")]
    result = run(sheet, "capex", staff=staff, catalog=catalog)
    assert (result.rule, result.owner, result.catalog_id) == (3, "Amy", "A")
    assert result.note.startswith('Rule 3: division Policy and Risk (from catalog "Report A")')
    assert "Owner is overloaded" in result.note and "Backup is a manager" in result.note


def test_rule_3_by_letter_code(sheet):
    result = run(sheet, "Permintaan data", letter_code="EMR/IRM")
    assert (result.rule, result.division) == (3, "Policy and Risk")
    lowest = min(
        (p for p in sheet.staff() if p.main_division == "Policy and Risk"),
        key=lambda p: (load_ratio(p), p.name),
    )
    assert result.owner == lowest.name
    assert "letter code EMR/IRM" in result.note


def test_rule_3_overloaded_people_still_eligible(sheet):
    staff = [person("Only", load=50)]
    result = run(sheet, "x", staff=staff, catalog=[], letter_code="EMR/IRM")
    assert (result.rule, result.owner) == (3, "Only")


def test_catalog_without_owner_falls_to_division(sheet):
    result = run(sheet, "Evaluasi APU PPT tahunan")
    assert result.rule == 3
    assert result.catalog_id == "CMP-AML"
    assert result.division == "Compliance"
    assert "default owner not set" in result.note


def test_unknown_letter_code_flag(sheet):
    result = run(sheet, "Tolong cek printer", letter_code="XYZ/ABC")
    assert "Needs review: unknown letter code XYZ/ABC" in result.flags
    assert result.rule == 5


def test_rule_4_ai_suggestion(sheet):
    result = run(sheet, "Tolong lengkapi lampiran", ai="FIN-KPMM")
    assert (result.rule, result.owner, result.catalog_id) == (4, "Staff G", "FIN-KPMM")
    assert result.note.startswith('Rule 4 (AI suggested): catalog "KPMM / capital adequacy"')


def test_rule_4_ignores_adhoc_and_unknown_ids(sheet):
    assert run(sheet, "Tolong lengkapi lampiran", ai="ADHOC-FIN").rule == 5
    assert run(sheet, "Tolong lengkapi lampiran", ai="NONE").rule == 5
    assert run(sheet, "Tolong lengkapi lampiran", ai="FAKE-1").rule == 5


def test_rule_4_not_used_when_rules_find_someone(sheet):
    assert run(sheet, "laporan stress test", ai="FIN-KPMM").rule == 1


def test_rule_5(sheet):
    result = run(sheet, "Tolong cek printer di lantai 3")
    assert (result.rule, result.owner, result.division) == (5, None, None)
    assert result.flags == [NEEDS_REVIEW_FLAG]


def test_rule_5_keeps_division_when_nobody_available(sheet):
    staff = [person("Mgr", division="HR", role="Manager")]
    catalog = [entry("A", "talent", owner="Mgr", division="HR")]
    result = run(sheet, "talent", staff=staff, catalog=catalog)
    assert (result.rule, result.division, result.catalog_id) == (5, "HR", "A")
    assert "nobody available in division HR" in result.note


def test_intern_ending_before_deadline_never_proposed(sheet):
    intern = person("Intern", division="HR", role="Intern", load=0, end=date(2026, 11, 1))
    staff = [intern, person("Staffer", division="HR", load=9)]
    for catalog in ([entry("A", "talent", owner="Intern", backup="Intern", division="HR")], []):
        result = run(sheet, "talent", staff=staff, catalog=catalog, letter_code=None)
        assert result.owner != "Intern"
    letter = [LetterCode(letter_code="HR/X", route_to_division="HR")]
    result = route(
        text="x", contacts=[], letter_code="HR/X", due=DUE, catalog=[], staff=staff,
        letter_codes=letter, config=sheet.config(), today=TODAY,
    )  # fmt: skip
    assert result.owner == "Staffer"


def test_owner_not_in_staff(sheet):
    catalog = [entry("A", "capex", owner="Ghost", backup="")]
    result = run(sheet, "capex", staff=[person("Amy")], catalog=catalog)
    assert result.rule == 3
    assert "Ghost is not in Staff" in result.note


def test_known_catalog_id_skips_scoring(sheet):
    result = route(
        text="Corporate governance report (Laporan Tata Kelola) – Oct 2026",
        contacts=[], letter_code=None, due=DUE, catalog=sheet.catalog(), staff=sheet.staff(),
        letter_codes=sheet.letter_codes(), config=sheet.config(), today=TODAY,
        known_catalog_id="HR-DATA",
    )  # fmt: skip
    assert (result.rule, result.catalog_id) == (1, "HR-DATA")
    assert "(recurring)" in result.note
