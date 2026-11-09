"""The Recurring tab (v1.4 part 3, DESIGN section 20) with fakes. Names are fake."""

from datetime import date, datetime

import pytest

from app import clock, recurring
from app.extract.ai import NullProvider
from app.jobs import recurring_problems, run_changes, run_digest, run_recurring
from app.models import RecurringRule
from app.pipeline import Services
from tests.fakes import FakeDriveStore, FakeGmailClient, FakeSheetStore
from tests.test_lifecycle import person, run, team_backend

SHEET = "https://docs.google.com/spreadsheets/d/sheet-1"
HEADER = [
    "Name", "Active", "Title", "Repeat", "First due date", "Title period", "Create days before",
    "Owner", "Doer", "Needs review", "Reviewer", "Catalog ID", "Notes",
]  # fmt: skip


def row(**values):
    base = {
        "Name": "Q report",
        "Active": "Yes",
        "Title": "Q{Q} {YYYY} report",
        "Repeat": "Quarterly",
        "First due date": "2026-10-15",
        "Create days before": 7,
        "Owner": "Lead Finance",
        "Doer": "Ann Intern",
    }
    merged = {**base, **values}
    return [merged.get(column, "") for column in HEADER]


def services(*rows, header=HEADER):
    backend = team_backend()
    if rows or header is not HEADER:
        backend.tabs["Recurring"] = [list(header), *rows]
    store = FakeSheetStore(backend)
    store.load()
    return Services(store=store, drive=FakeDriveStore(), ai=NullProvider())


def created(svc):
    svc.store.load()
    return [t for t in svc.store.tasks() if t.source.startswith("Recurring ")]


# --- the schedule (pure) -----------------------------------------------------------------------


def test_repeat_words_in_english_and_indonesian():
    assert recurring.repeat_of(" quarterly ") == "Quarterly"
    assert recurring.repeat_of("Triwulan") == "Quarterly"
    assert recurring.repeat_of("bulanan") == "Monthly"
    assert recurring.repeat_of("tahunan") == "Annual"
    assert recurring.repeat_of("every 3 months") is None


def test_later_deadlines_keep_the_day_capped_at_month_end():
    dates = list(zip(range(4), recurring.nominal_dates(date(2026, 1, 30), "Monthly"), strict=False))
    assert [d for _, d in dates] == [
        date(2026, 1, 30), date(2026, 2, 28), date(2026, 3, 30), date(2026, 4, 30),
    ]  # fmt: skip


def test_a_month_end_first_deadline_stays_on_month_ends():
    nominal = recurring.nominal_dates(date(2026, 3, 31), "Quarterly")
    assert [next(nominal) for _ in range(4)] == [
        date(2026, 3, 31), date(2026, 6, 30), date(2026, 9, 30), date(2026, 12, 31),
    ]  # fmt: skip


def test_weekly_and_annual_steps():
    weekly = recurring.nominal_dates(date(2026, 11, 2), "Weekly")
    assert [next(weekly) for _ in range(3)] == [date(2026, 11, 2), date(2026, 11, 9),
                                               date(2026, 11, 16)]  # fmt: skip
    annual = recurring.nominal_dates(date(2028, 2, 29), "Annual")
    assert [next(annual) for _ in range(2)] == [date(2028, 2, 29), date(2029, 2, 28)]


def test_weekend_deadline_moves_to_the_friday_before():
    assert recurring.off_weekend(date(2026, 11, 14)) == date(2026, 11, 13)  # Saturday
    assert recurring.off_weekend(date(2026, 11, 15)) == date(2026, 11, 13)  # Sunday
    assert recurring.off_weekend(date(2026, 11, 16)) == date(2026, 11, 16)  # Monday


def test_created_lead_days_before_and_never_after_the_deadline():
    first = date(2026, 10, 15)
    assert recurring.to_create(first, "Quarterly", 7, date(2026, 11, 9)) == []
    assert recurring.to_create(first, "Quarterly", 7, date(2027, 1, 7)) == []
    [o] = recurring.to_create(first, "Quarterly", 7, date(2027, 1, 8))
    assert (o.nominal, o.due) == (date(2027, 1, 15), date(2027, 1, 15))
    assert len(recurring.to_create(first, "Quarterly", 7, date(2027, 1, 15))) == 1
    assert recurring.to_create(first, "Quarterly", 7, date(2027, 1, 16)) == []


def test_a_first_date_in_the_past_only_starts_the_series():
    assert recurring.next_occurrence(date(2025, 1, 15), "Quarterly", date(2026, 11, 9)) == (
        recurring.Occurrence(date(2027, 1, 15), date(2027, 1, 15))
    )


def test_weekend_shift_is_used_for_the_lead_time_too():
    # 14 Nov 2026 is a Saturday: due Fri 13 Nov, so it is created on 6 Nov with a 7-day lead.
    [o] = recurring.to_create(date(2026, 11, 14), "Monthly", 7, date(2026, 11, 6))
    assert (o.nominal, o.due) == (date(2026, 11, 14), date(2026, 11, 13))


@pytest.mark.parametrize(
    ("pattern", "repeat", "nominal", "period", "expected"),
    [
        ("Q{Q} {YYYY} report", "Quarterly", date(2026, 10, 15), "", "Q3 2026 report"),
        ("Q{Q} {YYYY} report", "Quarterly", date(2027, 1, 15), "Previous", "Q4 2026 report"),
        ("Q{Q} {YYYY} report", "Quarterly", date(2026, 9, 30), "Same", "Q3 2026 report"),
        ("Laporan {MMM} {YYYY}", "Monthly", date(2026, 11, 15), "", "Laporan Oct 2026"),
        ("Laporan bulan {M}", "Monthly", date(2027, 1, 10), "", "Laporan bulan 12"),
        ("S{S} {YYYY}", "Semester", date(2026, 7, 31), "", "S1 2026"),
        ("Annual {YYYY}", "Annual", date(2027, 3, 31), "", "Annual 2026"),
        ("Plain title", "Weekly", date(2026, 11, 9), "", "Plain title"),
        ("q{q} {yyyy}", "Quarterly", date(2026, 10, 15), "", "q3 2026"),
    ],
)
def test_title_placeholders(pattern, repeat, nominal, period, expected):
    assert recurring.title_for(pattern, repeat, nominal, period) == expected


def rule(**values) -> RecurringRule:
    return RecurringRule.model_validate(dict(zip(HEADER, row(**values), strict=True)))


def test_row_problems_before_people_are_looked_up():
    assert recurring.problem(rule()) is None
    assert "not one of" in recurring.problem(rule(Repeat="every 3 months"))
    assert recurring.problem(rule(**{"First due date": ""})) == "First due date is empty"
    assert recurring.problem(rule(Title=" ")) == "Title is empty"


def test_typed_text_dates_and_blank_active():
    parsed = rule(**{"First due date": "15/10/2026", "Active": ""})
    assert parsed.first_due == date(2026, 10, 15) and parsed.is_active
    assert not rule(Active="No").is_active


# --- who owns it (R9, decision 15) ---------------------------------------------------------------


def staff():
    return services().store.staff()


def test_owner_and_doer_as_written():
    who = recurring.assignee(rule(), staff(), date(2027, 1, 15), date(2027, 1, 8))
    assert (who.owner, who.doer, who.flags) == ("Lead Finance", "Ann Intern", ())


def test_blank_owner_follows_the_doers_supervisor():
    who = recurring.assignee(rule(Owner=""), staff(), date(2027, 1, 15), date(2027, 1, 8))
    assert (who.owner, who.doer) == ("Lead Finance", "Ann Intern")
    # An intern who reports to the Manager owns her own work (R5).
    hr = recurring.assignee(rule(Owner="", Doer="Hr"), staff(), date(2027, 1, 15), date(2027, 1, 8))
    assert (hr.owner, hr.doer) == ("Hr Intern", "Hr Intern")


def test_rows_that_are_skipped():
    team = staff()
    due, today = date(2027, 1, 15), date(2027, 1, 8)
    assert recurring.assignee(rule(Doer=""), team, due, today) == "Doer is empty"
    assert "not in Staff" in recurring.assignee(rule(Doer="Nobody Here"), team, due, today)
    assert "not in Staff" in recurring.assignee(rule(Owner="Nobody Here"), team, due, today)
    no_boss = recurring.assignee(rule(Owner="", Doer="Boss Manager"), team, due, today)
    assert no_boss == "Owner is empty and the Doer reports to nobody"


def test_an_intern_leaving_before_the_deadline_is_flagged_not_replaced():
    # The fake team's interns leave on 30 Jun 2027.
    who = recurring.assignee(rule(), staff(), date(2027, 7, 15), date(2027, 7, 8))
    assert who.doer == "Ann Intern"
    assert who.flags and "internship ended" in who.flags[0]


def test_additional_help_and_manager_may_be_chosen_without_a_flag():
    who = recurring.assignee(rule(Owner="", Doer="Helper"), staff(), date(2027, 1, 15),
                             date(2027, 1, 8))  # fmt: skip
    assert (who.owner, who.doer, who.flags) == ("Helper Extra", "Helper Extra", ())


# --- the daily job -------------------------------------------------------------------------------


def test_job_creates_each_deadline_once_already_approved():
    svc = services(row(**{"Needs review": "Yes", "Reviewer": "Boss Manager", "Notes": "Use v2"}))
    result = run_recurring(svc, date(2027, 1, 8))
    assert (result.rules, result.created, result.skipped) == (1, 1, 0)
    [task] = created(svc)
    assert task.title == "Q4 2026 report"
    assert task.due_date == date(2027, 1, 15)
    assert task.status == "Not Started"
    assert (task.owner, task.doer) == ("Lead Finance", "Ann Intern")
    assert task.source == task.source_id == "Recurring Q report 2027-01-15"
    assert (task.needs_review, task.reviewer, task.notes) == ("Yes", "Boss Manager", "Use v2")
    assert task.start == date(2027, 1, 8)
    assert task.routing_note.startswith("Recurring tab row Q report (Quarterly)")
    assert task.division == "Account & Finance"  # the Doer's division (no Catalog ID)

    again = run_recurring(svc, date(2027, 1, 9))
    assert (again.created, again.existing) == (0, 1)
    assert len(created(svc)) == 1


def test_nothing_before_the_lead_time_and_nothing_for_paused_rows():
    svc = services(row(), row(Name="Off", Active="No", **{"First due date": "2027-01-10"}))
    assert run_recurring(svc, date(2026, 11, 9)).created == 0
    assert run_recurring(svc, date(2027, 1, 8)).created == 1  # only the active row
    assert [t.source_id for t in created(svc)] == ["Recurring Q report 2027-01-15"]


def test_blank_create_days_uses_config_recurring_lead():
    svc = services(row(**{"Create days before": ""}))  # fixture Config: 14 days
    assert run_recurring(svc, date(2027, 1, 1)).created == 1


def test_weekend_deadline_is_written_as_the_friday_with_a_note():
    svc = services(row(Repeat="Monthly", **{"First due date": "2026-11-14"}))
    run_recurring(svc, date(2026, 11, 9))
    [task] = created(svc)
    assert task.due_date == date(2026, 11, 13)
    assert task.source_id == "Recurring Q report 2026-11-14"
    assert "moved from Sat 14 Nov 2026" in task.original_message


def test_catalog_id_fills_division_and_requester():
    svc = services(row(**{"Catalog ID": "rm-monthly"}))
    run_recurring(svc, date(2027, 1, 8))
    [task] = created(svc)
    assert (task.catalog_id, task.division, task.requester) == (
        "RM-MONTHLY", "Policy and Risk", "Internal",
    )  # fmt: skip
    svc = services(row(**{"Catalog ID": "NOPE"}))
    run_recurring(svc, date(2027, 1, 8))
    assert "Catalog ID NOPE not in Catalog" in created(svc)[0].flags


def test_bad_rows_are_skipped_and_named_the_rest_still_created():
    svc = services(
        row(Name="Good"),
        row(Name="Typo", Repeat="Quartely"),
        row(Name="Ghost", Doer="Nobody Here"),
        row(Name="Bad date", **{"First due date": "soon"}),
    )
    result = run_recurring(svc, date(2027, 1, 8))
    assert (result.created, result.skipped) == (1, 3)
    problems = recurring_problems(svc.store, date(2027, 1, 8))
    assert problems[0] == "Recurring row 5: bad value in First due date"
    assert any(p.startswith("Recurring Typo: Repeat") for p in problems)
    assert 'Recurring Ghost: Doer "Nobody Here" is not in Staff' in problems


def test_no_recurring_tab_or_a_missing_column():
    assert run_recurring(services(), date(2027, 1, 8)).rules == 0
    short = [c for c in HEADER if c != "Doer"]
    svc = services(header=short)
    assert recurring_problems(svc.store, date(2027, 1, 8)) == [
        "Recurring is missing column(s): Doer"
    ]
    assert run_recurring(svc, date(2027, 1, 8)).skipped == 1


def test_one_failed_write_does_not_stop_the_other_rows(monkeypatch):
    svc = services(row(Name="A"), row(Name="B"))
    real = svc.store.append_task
    calls = []

    def flaky(task):
        calls.append(task.source_id)
        if task.source_id.startswith("Recurring A "):
            raise OSError("sheets")
        return real(task)

    monkeypatch.setattr(svc.store, "append_task", flaky)
    result = run_recurring(svc, date(2027, 1, 8))
    assert (result.failed, result.created) == (1, 1)
    assert len(calls) == 2


def test_the_change_job_then_emails_the_doer():
    svc = services(row())
    gmail = FakeGmailClient()
    svc.store.load()
    run_changes(svc.store, gmail, SHEET)  # first run: records the baseline only
    run_recurring(svc, date(2027, 1, 8))
    svc.store.load()
    result = run_changes(svc.store, gmail, SHEET)
    assert result.emailed == 1
    assert gmail.sent[0][0].to == ["ann@example.com"]


# --- the manager sees the problems; /health only a count ---------------------------------------


def test_recurring_command_lists_rows_and_problems():
    svc = services(row(), row(Name="Ghost", Doer="Nobody Here"), row(Name="Off", Active="No"))
    gmail = FakeGmailClient()
    reply = run(svc.store, gmail, "Boss Manager", "recurring").reply
    assert "Q report (Quarterly): next Q4 2026 report, due Fri 15 Jan 2027" in reply
    assert "Off: paused" in reply
    assert 'Ghost: Doer "Nobody Here" is not in Staff' in reply
    refused = run(svc.store, gmail, "Lead Finance", "recurring").reply
    assert refused.startswith("Only the manager")


def test_manager_digest_lists_skipped_rows():
    svc = services(row(Name="Ghost", Doer="Nobody Here"))
    gmail = FakeGmailClient()
    run_digest(svc.store, gmail, SHEET, datetime(2026, 11, 9, 9, 0, tzinfo=clock.TZ))
    manager = [email for email, _ in gmail.sent if email.to == ["manager@example.com"]]
    assert manager and "Recurring rows skipped" in manager[0].text
    assert "Nobody Here" in manager[0].text


def test_health_shows_a_count_and_no_names(monkeypatch):
    from app import main as main_module
    from app.settings import Settings
    from tests.test_main import FULL

    svc = services(row(Name="Ghost", Doer="Nobody Here"))
    health = main_module.check_startup(Settings(**FULL), lambda s: svc.store)
    [warning] = [w for w in health.warnings if w.startswith("Recurring")]
    assert warning.startswith("Recurring: 1 row(s) cannot be used")
    assert "Nobody" not in warning and "Ghost" not in warning
    assert person(svc.store, "Boss Manager").role == "Manager"
