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
                  "by hand", "langsung")),
    ("WhatsApp", ("whatsapp", "wa")),
    ("Email", ("email", "e-mail", "surel", "mail")),
    ("Portal", ("portal", "online", "sistem", "system", "website", "aplikasi", "upload",
                "diunggah")),
    ("Fax", ("fax", "faks")),
)  # fmt: skip
_MONTH_NAMES = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_MONTHS = {m: i + 1 for i, m in enumerate(_MONTH_NAMES)}
_MONTHS.update({"mei": 5, "agu": 8, "agt": 8, "okt": 10, "des": 12})
_TIME = re.compile(r"\b(?:jam|pukul|at)?\s*([01]?\d|2[0-3])[:.]([0-5]\d)\b", re.I)
_HOUR = re.compile(r"\b(?:jam|pukul|at)\s*([01]?\d|2[0-3])\b(?![:.]\d)", re.I)  # "jam 3"
_NUMERIC_DATE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b")
_WORD_DATE = re.compile(r"\b(\d{1,2})\s+([a-z]{3})[a-z]*\.?(?:\s+(\d{4}))?\b", re.I)


@dataclass(frozen=True)
class Submission:
    method: str
    at: datetime
    details: str


def method_in(text: str) -> str:
    plain = f" {text.casefold()} "
    for name, words in METHODS:
        if any(re.search(rf"(?<!\w){re.escape(word)}(?!\w)", plain) for word in words):
            return name
    return ""


def _day(text: str, today: date) -> date | None:
    plain = text.casefold()
    if re.search(r"\b(kemarin|yesterday)\b", plain):
        return today - timedelta(days=1)
    if re.search(r"\b(today|hari ini|tadi|barusan|just now)\b", plain):
        return today
    found = _NUMERIC_DATE.search(text)
    if found:
        day, month = int(found.group(1)), int(found.group(2))
        year = int(found.group(3)) if found.group(3) else today.year
        year = year + 2000 if year < 100 else year
        try:
            return date(year, month, day)
        except ValueError:
            return None
    found = _WORD_DATE.search(text)
    if found and found.group(2).casefold() in _MONTHS:
        year = int(found.group(3)) if found.group(3) else today.year
        try:
            return date(year, _MONTHS[found.group(2).casefold()], int(found.group(1)))
        except ValueError:
            return None
    return None


def read(text: str, now: datetime) -> Submission | None:
    """The submission in `text` (what follows `submitted G-0012`), or None when it names no
    method or gives a date in the future."""
    details = " ".join(text.split()).strip(" .")
    method = method_in(details)
    if not method:
        return None
    day = _day(details, now.date()) or now.date()
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
