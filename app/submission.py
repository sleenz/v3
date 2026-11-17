"""How and when a task was submitted to its requester (DESIGN section 28). Pure: text in, data out.

"hardcopy ke OJK hari ini 15:30" → method "Hardcopy", at today 15:30, details as typed. The method
is one of a few fixed words when one is named; otherwise the first words typed. A date or time
not given means now; a date in the future is not accepted (a submission has happened).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

METHODS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Hardcopy", ("hardcopy", "hard copy", "fisik", "kurir", "courier", "diantar", "antar",
                  "by hand")),
    ("WhatsApp", ("whatsapp", "wa")),
    ("Email", ("email", "e-mail", "surel", "mail", "gmail")),
    ("Portal", ("portal", "online", "sistem", "system", "website", "aplikasi", "upload",
                "uploaded", "diunggah")),
    ("Fax", ("fax", "faks")),
)  # fmt: skip
_MONTH_NAMES = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_MONTHS = {m: i + 1 for i, m in enumerate(_MONTH_NAMES)}
_MONTHS.update({"mei": 5, "agu": 8, "agt": 8, "okt": 10, "des": 12})
_TIME = re.compile(r"\b(?:jam|pukul|at)?\s*([01]?\d|2[0-3])[:.]([0-5]\d)\b", re.I)
_HOUR = re.compile(r"\b(?:jam|pukul|at)\s*([01]?\d|2[0-3])\b(?![:.]\d)", re.I)  # "jam 3"
_YEAR_FIRST = re.compile(r"(?<![\d/-])(\d{4})[/-](\d{1,2})[/-](\d{1,2})(?![\d/-])")
_NUMERIC_DATE = re.compile(r"(?<![\d/-])(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?(?![\d/-])")
_WORD_DATE = re.compile(r"\b(\d{1,2})\s+([a-z]{3})[a-z]*\.?(?:\s+(\d{4}))?\b", re.I)
_LATER = re.compile(r"\b(besok|tomorrow|lusa|nanti|minggu depan|next week|will|akan)\b", re.I)
_FROZEN = re.compile(r"\s*@at=(\S+)")


@dataclass(frozen=True)
class Submission:
    method: str
    at: datetime
    details: str


def method_in(text: str) -> str:
    """The method named first in `text` ("email ke kurir" is Email)."""
    plain = f" {text.casefold()} "
    found: list[tuple[int, str]] = []
    for name, words in METHODS:
        for word in words:
            hit = re.search(rf"(?<!\w){re.escape(word)}(?!\w)", plain)
            if hit:
                found.append((hit.start(), name))
    return min(found)[1] if found else ""


def _dated(year: int | None, month: int, day: int, today: date, past: bool) -> date | None:
    try:
        when = date(year or today.year, month, day)
    except ValueError:
        return None
    if year is None and past and (when - today).days > 60:  # "30 Des" on 5 Jan: last December
        when = when.replace(year=when.year - 1)
    return when


def day_in(text: str, today: date, past: bool = False) -> date | None:
    """A date named in `text`: today/kemarin/besok, 2026-12-05, 24/10, 23 Okt; None when there
    is none or it is impossible. `past`: a date without a year is the last one (a submission)."""
    plain = text.casefold()
    if re.search(r"\b(kemarin|yesterday)\b", plain):
        return today - timedelta(days=1)
    if re.search(r"\b(besok|tomorrow)\b", plain):
        return today + timedelta(days=1)
    if re.search(r"\blusa\b", plain):
        return today + timedelta(days=2)
    if re.search(r"\b(today|hari ini|tadi|barusan|just now)\b", plain):
        return today
    found = _YEAR_FIRST.search(text)
    if found:
        return _dated(int(found.group(1)), int(found.group(2)), int(found.group(3)), today, past)
    found = _NUMERIC_DATE.search(text)
    if found:
        year = int(found.group(3)) if found.group(3) else None
        year = year + 2000 if year is not None and year < 100 else year
        return _dated(year, int(found.group(2)), int(found.group(1)), today, past)
    found = _WORD_DATE.search(text)
    if found and found.group(2).casefold() in _MONTHS:
        year = int(found.group(3)) if found.group(3) else None
        return _dated(year, _MONTHS[found.group(2).casefold()], int(found.group(1)), today, past)
    return None


def _names_a_date(text: str) -> bool:
    word = _WORD_DATE.search(text)
    return bool(
        _YEAR_FIRST.search(text)
        or _NUMERIC_DATE.search(text)
        or (word and word.group(2).casefold() in _MONTHS)
    )


def freeze(text: str, now: datetime) -> str:
    """The typed text with the moment it means added (`… @at=2026-10-25T17:00:00+08:00`), so a
    YES later writes what the confirmation showed (QA6-B6)."""
    sent = read(text, now)
    return f"{_FROZEN.sub('', text).strip()} @at={sent.at.isoformat()}" if sent else text


def read(text: str, now: datetime) -> Submission | None:
    """The submission in `text` (what follows `submitted G-0012`), or None when it names no
    method, a date that is impossible or in the future, or a later moment ("besok")."""
    frozen = _FROZEN.search(text or "")
    details = " ".join(_FROZEN.sub("", text or "").split()).strip(" .")
    method = method_in(details)
    if not method:
        return None
    if frozen:
        try:
            return Submission(method, datetime.fromisoformat(frozen.group(1)), details)
        except ValueError:
            pass
    if _LATER.search(details):
        return None  # not sent yet
    day = day_in(details, now.date(), past=True)
    if day is None and _names_a_date(details):
        return None  # "31/02": a date was given but cannot be read (QA6-B4)
    day = day or now.date()
    clock_at = _TIME.search(details) or _HOUR.search(details)
    if clock_at:
        hour = int(clock_at.group(1))
        minute = int(clock_at.group(2)) if clock_at.re is _TIME else 0
        if 1 <= hour <= 6 and not re.search(r"\b(pagi|am)\b", details, re.I):
            hour += 12  # "jam 3.10" is the afternoon: nobody submits at 3 a.m.
        when = datetime.combine(day, time(hour, minute), now.tzinfo)
    else:
        when = now if day == now.date() else datetime.combine(day, time(0, 0), now.tzinfo)
    if when > now + timedelta(minutes=5):
        return None
    return Submission(method=method, at=when, details=details)
