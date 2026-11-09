"""Recurring tab rules (v1.4 part 3, DESIGN section 20). Pure: no network, no Google clients.

A row says: the first deadline, how often it repeats, how many days before each deadline the
task is created, and who owns and does it. Later deadlines keep the first one's day of the
month (capped at a shorter month's last day; a first deadline on a month's last day stays on the
last day). A deadline on a Saturday or Sunday moves to the Friday before (decision D2).
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta

from app.models import RecurringRule, StaffMember
from app.route import match_staff_name, owner_and_doer, unavailable_reason

STEP_MONTHS = {"Monthly": 1, "Quarterly": 3, "Semester": 6, "Annual": 12}
REPEATS = ("Weekly", *STEP_MONTHS)
_REPEAT_WORDS = {
    "weekly": "Weekly", "mingguan": "Weekly",
    "monthly": "Monthly", "bulanan": "Monthly",
    "quarterly": "Quarterly", "triwulan": "Quarterly", "triwulanan": "Quarterly",
    "kuartal": "Quarterly", "kuartalan": "Quarterly",
    "semester": "Semester", "semesteran": "Semester", "half-yearly": "Semester",
    "annual": "Annual", "annually": "Annual", "yearly": "Annual", "tahunan": "Annual",
}  # fmt: skip
MAX_PERIODS = 2000  # weekly for ~38 years; stops a loop on a nonsense first date
SOURCE_PREFIX = "Recurring"


def repeat_of(text: str) -> str | None:
    """`Quarterly`, `quarterly` or `Triwulan` -> `Quarterly`; None when not a known repeat."""
    return _REPEAT_WORDS.get(text.strip().lower())


def add_months(day: date, months: int, keep_month_end: bool) -> date:
    month_index = day.year * 12 + day.month - 1 + months
    year, month = divmod(month_index, 12)
    month += 1
    last = calendar.monthrange(year, month)[1]
    return date(year, month, last if keep_month_end else min(day.day, last))


def off_weekend(day: date) -> date:
    """Saturday or Sunday -> the Friday before."""
    return day - timedelta(days=max(0, day.weekday() - 4))


def nominal_dates(first: date, repeat: str) -> Iterator[date]:
    """Every deadline before the weekend shift: first, first + one period, ..."""
    month_end = first.day == calendar.monthrange(first.year, first.month)[1]
    for k in range(MAX_PERIODS):
        if repeat == "Weekly":
            yield first + timedelta(weeks=k)
        else:
            yield add_months(first, k * STEP_MONTHS[repeat], month_end)


@dataclass(frozen=True)
class Occurrence:
    nominal: date  # the deadline by the schedule; names the period (Source ID)
    due: date  # the deadline written to the task (weekend -> Friday before)


def occurrences(first: date, repeat: str) -> Iterator[Occurrence]:
    for nominal in nominal_dates(first, repeat):
        yield Occurrence(nominal, off_weekend(nominal))


def to_create(first: date, repeat: str, lead_days: int, today: date) -> list[Occurrence]:
    """Deadlines whose task should exist by `today`: created `lead_days` before the deadline,
    and never after it has passed (a first date in the past only starts the series)."""
    found = []
    for occurrence in occurrences(first, repeat):
        if occurrence.due - timedelta(days=lead_days) > today:
            break
        if occurrence.due >= today:
            found.append(occurrence)
    return found


def next_occurrence(first: date, repeat: str, today: date) -> Occurrence | None:
    return next((o for o in occurrences(first, repeat) if o.due >= today), None)


def source_id(rule_name: str, occurrence: Occurrence) -> str:
    return f"{SOURCE_PREFIX} {rule_name.strip()} {occurrence.nominal:%Y-%m-%d}"


def title_for(pattern: str, repeat: str, nominal: date, period: str) -> str:
    """Fill {Q} {S} {M} {MMM} {YYYY}. `Previous` (default) names the period before the deadline
    (a 15 Oct deadline is the Q3 report); `Same` names the deadline's own period."""
    reference = nominal
    if period.strip().lower() not in {"same", "sama"}:
        if repeat == "Weekly":
            reference = nominal - timedelta(weeks=1)
        else:
            reference = add_months(nominal, -STEP_MONTHS[repeat], keep_month_end=False)
    values = {
        "Q": str((reference.month - 1) // 3 + 1),
        "S": "1" if reference.month <= 6 else "2",
        "M": str(reference.month),
        "MMM": f"{reference:%b}",
        "YYYY": str(reference.year),
    }
    return re.sub(
        r"\{(Q|S|M|MMM|YYYY)\}", lambda match: values[match.group(1).upper()], pattern, flags=re.I
    ).strip()


@dataclass(frozen=True)
class Assignee:
    owner: str
    doer: str
    flags: tuple[str, ...] = ()


def problem(rule: RecurringRule) -> str | None:
    """What stops a row before any people are looked up (repeat, first date, title)."""
    if repeat_of(rule.repeat) is None:
        return f'Repeat "{rule.repeat.strip()}" is not one of {", ".join(REPEATS)}'
    if rule.first_due is None:
        return "First due date is empty"
    if not rule.title.strip():
        return "Title is empty"
    if rule.create_days_before is not None and rule.create_days_before < 0:
        return "Create days before is negative"
    return None


def assignee(
    rule: RecurringRule, staff: list[StaffMember], due: date, today: date
) -> Assignee | str:
    """(Owner, Doer) for a row, or why it is skipped (decision 15 and R9).

    The Doer must be in Staff. A blank Owner is allowed when the Doer reports to someone: the
    Owner then follows R4/R5 (a full-time supervisor owns an intern's work). A person chose
    them, so an unavailable Doer is flagged, not replaced."""
    if not rule.doer.strip():
        return "Doer is empty"
    doer = match_staff_name(rule.doer, staff)
    if doer is None:
        return f'Doer "{rule.doer.strip()}" is not in Staff'
    if rule.owner.strip():
        owner = match_staff_name(rule.owner, staff)
        if owner is None:
            return f'Owner "{rule.owner.strip()}" is not in Staff'
        owner_name = owner.name
    elif doer.reports_to.strip():
        owner_name = owner_and_doer(doer, staff)[0]
    else:
        return "Owner is empty and the Doer reports to nobody"
    flags = []
    reason = unavailable_reason(doer, today, due)
    # A Manager or Additional help may be picked by hand (R3); only an internship end matters.
    if reason and reason.startswith("internship"):
        flags.append(f"Recurring Doer {doer.name}: {reason}")
    return Assignee(owner_name, doer.name, tuple(flags))
