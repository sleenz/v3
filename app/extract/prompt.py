"""The sender's PROMPT (DESIGN sections 17.5 and 21): parsed by fixed rules, never by the AI.

A PROMPT is text the sender typed: a WhatsApp message starting `PROMPT:` (or a file's caption),
or any line starting `PROMPT:` in an email from a Staff member (in the text above a forward, or
anywhere in a new email). `PROMPT inject:` is accepted as the same thing. Text inside a PDF or a
forwarded message is never a PROMPT, so a document cannot choose who does the work.

From the PROMPT the rules read three overrides:
- the owner: `assign (this) to <name>` (also `kasih ke`, `tugaskan ke`, `give it to`) or
  `ask <name> to …` (also `minta <name> untuk`, `suruh <name> buat`);
- `reviewer <name>` (also `with reviewer`, `reviewed by`, `direview oleh`): the reviewer;
- a deadline: `deadline 20 Nov`, `due 20/11/2026`, `deadline 3 days from now`, `in 2 weeks`,
  `tomorrow`, `deadline this friday`, `by next monday`, `paling lambat jumat ini`. Counted
  deadlines are calendar days; a Saturday or Sunday moves to the Friday before (as decision D2).
  A typed date or weekday is kept as typed.

Pure: no network, no clock (the caller passes `today`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.extract.rules import find_dates
from app.recurring import off_weekend

_PROMPT = re.compile(r"^\s*prompt(?:\s+inject(?:ion)?)?\s*:?\s*(.*)$", re.IGNORECASE | re.DOTALL)
# An email PROMPT line needs the colon (or "inject"), so "Prompt action is required" is not one.
_EMAIL_PROMPT_LINE = re.compile(
    r"^\s*\*?prompt(?:\s+inject(?:ion)?\s*:?|\s*:)\s*(.+?)\s*\*?\s*$", re.IGNORECASE
)

# Words that end a name: what follows is another instruction, not part of the name.
_NAME_END = re.compile(
    r"\s+(?:(?:and|&)\s+)?"
    r"(?:with|dengan|reviewer|review(?:ed)?|direview|deadline|due|by|in|within|before|on"
    r"|tomorrow|besok|sebelum|tenggat|batas|paling|dalam)\b.*$",
    re.IGNORECASE | re.DOTALL,
)
_POLITE_TAIL = re.compile(r"(?:\s+(?:please|pls|plz|ya+|thanks|thx|tq|ok|yah))+\s*$", re.I)
_NAME = r"([^\n.,;!?]+)"
_ASSIGN = re.compile(
    r"\b(?:assign(?:ed)?|give|kasih(?:kan)?|tugaskan)\s+(?:this\s+|it\s+|ini\s+)?"
    r"(?:to|ke|kepada)\s+" + _NAME,
    re.IGNORECASE,
)
# "ask cia to do it", "minta cia untuk …", "suruh cia buat …": the shortest name before "to".
_ASK = re.compile(
    r"\b(?:ask|minta(?:lah)?|suruh)\s+([^\n.,;!?]+?)\s+(?:to|untuk|buat)\b",
    re.IGNORECASE,
)
_REVIEWER = re.compile(
    r"\b(?:reviewer(?:\s+is)?|review(?:ed)?\s+by|direview\s+oleh|reviewernya)\s*:?\s+" + _NAME,
    re.IGNORECASE,
)

_UNITS = {"day": 1, "days": 1, "hari": 1, "week": 7, "weeks": 7, "minggu": 7}
_UNIT = r"(days?|weeks?|hari|minggu)"
_COUNTED = [
    # "3 days from now", "3 hari lagi", "3 days later"
    re.compile(
        r"\b(\d{1,3})\s*" + _UNIT + r"\s+(?:from\s+(?:now|today)|later|lagi"
        r"|dari\s+(?:sekarang|hari\s+ini))\b",
        re.IGNORECASE,
    ),
    # "in 3 days", "within 2 weeks", "dalam 3 hari"
    re.compile(r"\b(?:in|within|dalam)\s+(\d{1,3})\s*" + _UNIT + r"\b", re.IGNORECASE),
    # "deadline 3 days", "due: 5 hari"
    re.compile(
        r"\b(?:deadline|due(?:\s+date)?|tenggat|batas\s+waktu)\s*(?:is|in|:)?\s*"
        r"(\d{1,3})\s*" + _UNIT + r"\b",
        re.IGNORECASE,
    ),
]
_TOMORROW = re.compile(r"\b(?:tomorrow|besok)\b", re.IGNORECASE)
_DEADLINE_WORD = re.compile(
    r"\b(?:deadline|due(?:\s+date)?|by|before|sebelum|tenggat|batas\s+waktu|paling\s+lambat)\b",
    re.IGNORECASE,
)
_DEADLINE_WINDOW = 20  # characters between the deadline word and the date ("is on Saturday ")

# Weekdays (Monday = 0). "Minggu" is left out: in Indonesian it also means "week".
_WEEKDAYS = {
    "monday": 0, "senin": 0, "tuesday": 1, "selasa": 1, "wednesday": 2, "rabu": 2,
    "thursday": 3, "kamis": 3, "friday": 4, "jumat": 4, "jum'at": 4, "saturday": 5, "sabtu": 5,
    "sunday": 6,
}  # fmt: skip
_DAY = (
    r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday|senin|selasa|rabu|kamis"
    r"|jum'?at|sabtu)"
)
_WEEKDAY = re.compile(
    r"\b(?:(this|coming|next)\s+)?" + _DAY + r"\b(?:\s+(ini|depan|next\s+week))?",
    re.IGNORECASE,
)


@dataclass
class PromptOverrides:
    """What the sender's PROMPT sets. None: the PROMPT did not say."""

    assign_to: str | None = None
    reviewer: str | None = None
    due: date | None = None
    flags: list[str] = field(default_factory=list)


def parse_prompt(text: str) -> str | None:
    """The PROMPT's text, or None when the text is not a PROMPT."""
    match = _PROMPT.match(text)
    return match.group(1).strip() if match else None


def _clean_name(raw: str) -> str:
    name = _NAME_END.sub("", raw)
    return _POLITE_TAIL.sub("", name).strip()


def _last_name(prompt: str, *patterns: re.Pattern[str]) -> str | None:
    found = [(m.start(), _clean_name(m.group(1))) for p in patterns for m in p.finditer(prompt)]
    names = [name for _, name in sorted(found) if name]
    return names[-1] if names else None


def assignee(prompt: str) -> str | None:
    """The person named in "assign this to <name>" or "ask <name> to …" (the last one)."""
    return _last_name(prompt, _ASSIGN, _ASK)


def reviewer(prompt: str) -> str | None:
    """The person named in "reviewer <name>" (the last one, if repeated)."""
    return _last_name(prompt, _REVIEWER)


def deadline(prompt: str, today: date) -> tuple[date | None, list[str]]:
    """The deadline the PROMPT sets, and its flags."""
    for pattern in _COUNTED:
        found = pattern.search(prompt)
        if found:
            days = int(found.group(1)) * _UNITS[found.group(2).lower()]
            return off_weekend(today + timedelta(days=days)), []
    if _TOMORROW.search(prompt):
        return off_weekend(today + timedelta(days=1)), []
    hits = find_dates(prompt, today)
    for word in _DEADLINE_WORD.finditer(prompt):
        following = [hit for hit in hits if 0 <= hit.start - word.end() <= _DEADLINE_WINDOW]
        if following:
            value = following[0].value
            flags = ["PROMPT deadline already passed"] if value < today else []
            return value, flags
        day = _WEEKDAY.search(prompt, word.end())
        if day and day.start() - word.end() <= _DEADLINE_WINDOW:
            return weekday_date(day, today), []
    return None, []


def weekday_date(found: re.Match[str], today: date) -> date:
    """ "this friday" / "friday" / "jumat ini": the coming one (today counts);
    "next friday" / "jumat depan" / "friday next week": that day of next week."""
    target = _WEEKDAYS[found.group(2).lower()]
    before, after = (found.group(1) or "").lower(), (found.group(3) or "").lower()
    if before == "next" or after in ("depan", "next week") or after.startswith("next"):
        next_monday = today + timedelta(days=7 - today.weekday())
        return next_monday + timedelta(days=target)
    ahead = (target - today.weekday()) % 7
    if ahead == 0 and before != "this" and after != "ini":
        ahead = 7  # a bare "monday" said on a Monday means the next one
    return today + timedelta(days=ahead)


def overrides(prompt: str, today: date) -> PromptOverrides:
    """Everything the PROMPT sets (assignee, reviewer, deadline)."""
    due, flags = deadline(prompt, today)
    return PromptOverrides(
        assign_to=assignee(prompt), reviewer=reviewer(prompt), due=due, flags=flags
    )


def split_prompt_lines(text: str) -> tuple[str, str]:
    """(PROMPT, the rest): every line of `text` that starts with `PROMPT:` (or `PROMPT inject`)
    is taken out; together they are the PROMPT. ("", text) when there is none."""
    prompts: list[str] = []
    kept: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        match = _EMAIL_PROMPT_LINE.match(line)
        if match:
            prompts.append(match.group(1))
        else:
            kept.append(line)
    if not prompts:
        return "", text
    return "\n".join(prompts), "\n".join(kept).strip("\n")
