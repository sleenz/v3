"""Routing rules (DESIGN section 5). Pure: no network, no clock; `today` is an argument.

The AI never chooses the assignee: `ai_catalog_id` is only a hint used by rule 4, and only after
rules 1-3 found nobody.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from app.models import AppConfig, CatalogEntry, Contact, LetterCode, RouteResult, StaffMember

NEEDS_REVIEW_FLAG = "Needs review: no owner"


@dataclass(frozen=True)
class CatalogMatch:
    entry: CatalogEntry
    score: int
    matched: tuple[str, ...]


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[-_]", " ", text.casefold())).strip()


def _contains_phrase(normalised_text: str, phrase: str) -> bool:
    phrase = _normalise(phrase)
    if not phrase:
        return False
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", normalised_text) is not None


def score_catalog(
    text: str, contacts: list[Contact], catalog: list[CatalogEntry]
) -> CatalogMatch | None:
    """Rule 1 scoring: keyword length per keyword found, +100 per known contact found.

    Each keyword and contact counts once. Ties go to the manager-confirmed row, then Catalog ID.
    """
    normalised = _normalise(text)
    emails = {c.email.casefold() for c in contacts if c.email}
    names = {_normalise(c.name) for c in contacts if c.name}
    best: CatalogMatch | None = None
    for entry in catalog:
        if entry.is_adhoc:
            continue
        score = 0
        matched: list[str] = []
        for keyword in dict.fromkeys(entry.match_keywords):
            if _contains_phrase(normalised, keyword):
                score += len(_normalise(keyword))
                matched.append(keyword)
        for known in dict.fromkeys(entry.known_contacts):
            if "@" in known:
                found = known.casefold() in emails or _contains_phrase(normalised, known)
            else:
                found = _normalise(known) in names or _contains_phrase(normalised, known)
            if found:
                score += 100
                matched.append(known)
        if score <= 0:
            continue
        candidate = CatalogMatch(entry, score, tuple(matched))
        if best is None or _rank(candidate) < _rank(best):
            best = candidate
    return best


def _rank(match: CatalogMatch) -> tuple[int, bool, str]:
    return (-match.score, not match.entry.confirmed, match.entry.catalog_id)


# --- People ------------------------------------------------------------------------------------


def find_person(name: str, staff: list[StaffMember]) -> StaffMember | None:
    wanted = name.strip().casefold()
    if not wanted:
        return None
    return next((person for person in staff if person.name.strip().casefold() == wanted), None)


def match_staff_name(name: str, staff: list[StaffMember]) -> StaffMember | None:
    """A name typed by a person (WhatsApp PROMPT): the full name, else a unique first name,
    else a unique start of a full name; case-insensitive. Ambiguous or unknown → None."""
    wanted = _normalise(name).strip(" .,!")
    if not wanted:
        return None
    exact = find_person(wanted, staff)
    if exact is not None:
        return exact
    for matches in (
        [p for p in staff if _normalise(p.name).split(" ")[:1] == [wanted]],
        [p for p in staff if _normalise(p.name).startswith(wanted)],
    ):
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return None
    return None


def sender_assignment(
    routed: RouteResult,
    name: str,
    staff: list[StaffMember],
    today: date,
    due: date | None,
) -> RouteResult:
    """Apply "assign this to <name>" from the sender's WhatsApp PROMPT (v1.3).

    A person chose, not the AI, and the task still waits for the manager's approval. The rules'
    proposal is kept in the routing note. An unknown name leaves the rules' result and flags it.
    """
    person = match_staff_name(name, staff)
    if person is None:
        return routed.model_copy(
            update={"flags": [*routed.flags, f'PROMPT: "{name.strip()}" not found in Staff']}
        )
    flags = [flag for flag in routed.flags if flag != NEEDS_REVIEW_FLAG]
    reason = unavailable_reason(person, today, due)
    if reason:
        flags.append(f"Assigned by sender: {person.name} {reason}")
    rules_said = routed.owner or "nobody"
    note = f"Assigned by sender (WhatsApp PROMPT) to {person.name}; rules proposed {rules_said}"
    boss, doer = owner_and_doer(person, staff)
    return RouteResult(
        owner=boss,
        doer=doer,
        division=routed.division or next(iter(person.division_list), None),
        catalog_id=routed.catalog_id,
        rule=0,
        note=f"{note} ({routed.note})",
        flags=flags,
    )


def unavailable_reason(person: StaffMember, today: date, due: date | None) -> str | None:
    """Why `person` cannot be proposed, or None if available."""
    if person.role == "Manager":
        return "is a manager"
    if person.role == "Additional help":
        return "is additional help"  # v1.4 R3: assigned by hand only
    end = person.internship_end
    if end is not None:
        if end < today:
            return f"internship ended {end:%d %b %Y}"
        if due is not None and end < due:
            return f"internship ends {end:%d %b %Y}, before the due date"
    return None


def supervisor(person: StaffMember, staff: list[StaffMember]) -> StaffMember | None:
    """The full-time person an intern reports to (v1.4 R4); None for anyone else, and for an
    intern who reports to a manager (R5: that intern owns their own work)."""
    if person.role != "Intern" or not person.reports_to.strip():
        return None
    boss = find_person(person.reports_to, staff)
    if boss is None or boss.role == "Manager":
        return None
    return boss


def owner_and_doer(person: StaffMember, staff: list[StaffMember]) -> tuple[str, str]:
    """(Owner, Doer) when `person` takes a task: an intern under a full-time supervisor does it
    and the supervisor owns it (R4); everyone else owns and does it (R5)."""
    boss = supervisor(person, staff)
    return (boss.name, person.name) if boss else (person.name, person.name)


def managers(staff: list[StaffMember]) -> list[StaffMember]:
    return [person for person in staff if person.role == "Manager"]


def default_reviewer(reviewer: str, doer: str, staff: list[StaffMember]) -> StaffMember | None:
    """The task's Reviewer, else the doer's full-time supervisor, else the first Manager (R7)."""
    if reviewer.strip():
        named = find_person(reviewer, staff)
        if named is not None:
            return named
    person = find_person(doer, staff)
    boss = supervisor(person, staff) if person else None
    if boss is not None:
        return boss
    return next(iter(managers(staff)), None)


def delegation_problem(
    actor: StaffMember, owner: str, doer: str, target: StaffMember, staff: list[StaffMember]
) -> str | None:
    """Why `actor` may not hand a task to `target` (v1.4 A2), or None if allowed.

    The Manager may hand any task to anyone; the task's Owner or Doer only to someone who
    reports to them, or back to themselves."""
    if actor.role == "Manager":
        return None
    is_party = actor.name.casefold() in (owner.strip().casefold(), doer.strip().casefold())
    if not is_party:
        return "only the task's Owner, Doer or the Manager can reassign it"
    if target.name == actor.name:
        return None
    if target.reports_to.strip().casefold() != actor.name.casefold():
        return f"{target.name} does not report to you"
    return None


def load_ratio(person: StaffMember) -> float:
    return person.weighted_open_load / person.capacity


def _owner_problem(
    name: str, staff: list[StaffMember], today: date, due: date | None, threshold: float | None
) -> tuple[StaffMember | None, str | None]:
    """(person, None) if `name` can take the task, else (None, reason)."""
    if not name.strip():
        return None, "not set"
    person = find_person(name, staff)
    if person is None:
        return None, f"{name} is not in Staff"
    reason = unavailable_reason(person, today, due)
    if reason:
        return None, f"{person.name} {reason}"
    if threshold is not None and load_ratio(person) > threshold:
        return None, f"{person.name} is overloaded (load {load_ratio(person):g} > {threshold:g})"
    return person, None


def _catalog_rules(
    match: CatalogMatch,
    staff: list[StaffMember],
    today: date,
    due: date | None,
    config: AppConfig,
    prefix: str,
) -> tuple[RouteResult | None, list[str]]:
    """Rules 1 and 2 for one catalog row. Returns a result or the reasons it failed."""
    entry = match.entry
    label = f'catalog "{entry.report_type}"'
    if match.matched:
        label += f" ({', '.join(match.matched)})"
    owner, owner_problem = _owner_problem(
        entry.default_owner, staff, today, due, config.overload_threshold
    )
    if owner is not None:
        note = f"{prefix.format(rule=1)}: {label} → default owner"
        boss, doer = owner_and_doer(owner, staff)
        return RouteResult(
            owner=boss, doer=doer, division=entry.division or None,
            catalog_id=entry.catalog_id, rule=1, note=note,
        ), []  # fmt: skip
    reasons = [f"default owner {owner_problem}"]
    backup, backup_problem = _owner_problem(entry.backup, staff, today, due, threshold=None)
    if backup is not None:
        note = f"{prefix.format(rule=2)}: {label} → backup; default owner {owner_problem}"
        boss, doer = owner_and_doer(backup, staff)
        return RouteResult(
            owner=boss, doer=doer, division=entry.division or None,
            catalog_id=entry.catalog_id, rule=2, note=note,
        ), []  # fmt: skip
    reasons.append(f"backup {backup_problem}")
    return None, reasons


def route(
    *,
    text: str,
    contacts: list[Contact],
    letter_code: str | None,
    due: date | None,
    catalog: list[CatalogEntry],
    staff: list[StaffMember],
    letter_codes: list[LetterCode],
    config: AppConfig,
    today: date,
    ai_catalog_id: str | None = None,
    known_catalog_id: str | None = None,
) -> RouteResult:
    """Propose an owner by rules 1-5 (DESIGN section 5).

    `known_catalog_id` (recurring reports only) skips rule 1 scoring and uses that row.
    """
    flags: list[str] = []
    reasons: list[str] = []
    division: str | None = None
    division_source = ""

    code_division = None
    if letter_code:
        code_division = next(
            (
                code.route_to_division
                for code in letter_codes
                if code.letter_code.strip().upper() == letter_code.strip().upper()
            ),
            None,
        )
        if code_division is None:
            flags.append(f"{config.unknown_letter_code}: unknown letter code {letter_code}")

    # Rules 1 and 2: a known report.
    known = next(
        (e for e in catalog if known_catalog_id and e.catalog_id == known_catalog_id), None
    )
    if known is not None and not known.is_adhoc:
        # A recurring report already knows its Catalog row: rule 1 uses it directly.
        match: CatalogMatch | None = CatalogMatch(known, 0, ("recurring",))
    else:
        match = score_catalog(text, contacts, catalog)
    matched_id = None
    if match is not None:
        result, failed = _catalog_rules(match, staff, today, due, config, "Rule {rule}")
        if result is not None:
            result.flags = flags
            return result
        reasons.append(f'catalog "{match.entry.report_type}": ' + "; ".join(failed))
        matched_id = match.entry.catalog_id
        if match.entry.division:
            division = match.entry.division
            division_source = f'catalog "{match.entry.report_type}"'

    if division is None and code_division:
        division = code_division
        division_source = f"letter code {letter_code}"

    # Rule 3: least-loaded available person in the division.
    if division:
        wanted = division.strip().casefold()
        candidates = [
            person
            for person in staff
            if wanted in (d.casefold() for d in person.division_list)
            and unavailable_reason(person, today, due) is None
        ]
        if candidates:
            person = min(candidates, key=lambda p: (load_ratio(p), p.name.casefold()))
            note = (
                f"Rule 3: division {division} (from {division_source}) → lowest load "
                f"({load_ratio(person):g})"
            )
            if reasons:
                note += "; " + "; ".join(reasons)
            boss, doer = owner_and_doer(person, staff)
            return RouteResult(
                owner=boss, doer=doer, division=division, catalog_id=matched_id,
                rule=3, note=note, flags=flags,
            )  # fmt: skip
        reasons.append(f"nobody available in division {division}")

    # Rule 4: the AI's catalog suggestion, through rules 1-2 only.
    ai_entry = next(
        (
            entry
            for entry in catalog
            if ai_catalog_id and entry.catalog_id == ai_catalog_id and not entry.is_adhoc
        ),
        None,
    )
    if ai_entry is not None:
        ai_match = CatalogMatch(ai_entry, 0, ())
        result, failed = _catalog_rules(
            ai_match, staff, today, due, config, "Rule 4 (AI suggested)"
        )
        if result is not None:
            result.rule = 4
            result.flags = flags
            return result
        reasons.append(f'AI suggestion "{ai_entry.report_type}": ' + "; ".join(failed))

    # Rule 5: nobody.
    note = "Rule 5: no owner found"
    if reasons:
        note += "; " + "; ".join(reasons)
    return RouteResult(
        owner=None, division=division, catalog_id=matched_id,
        rule=5, note=note, flags=[*flags, NEEDS_REVIEW_FLAG],
    )  # fmt: skip
