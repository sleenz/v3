"""Rule-based extraction: letter number/code, due date, weekday check, contacts, title, requester.

Pure functions. Anything that depends on "today" takes it as an argument.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from app.models import Contact

# --- Letter number -----------------------------------------------------------------------------

_LETTER = re.compile(
    r"(?<![\w.])(?P<prefix>[A-Z]{1,3})\s*\.\s*(?P<num>\d{1,5})"
    r"(?P<sub>(?:\s*\.\s*[A-Za-z0-9]{1,5})*)"
    r"\s*-\s*"
    r"(?P<units>[A-Za-z][A-Za-z0-9]*(?:\s*/\s*[A-Za-z][A-Za-z0-9]*)*)"
    r"\s*/\s*(?P<month>\d{1,2}|[IVXivx]{1,4})\s*/\s*(?P<year>\d{4})(?!\d)"
)


@dataclass(frozen=True)
class LetterRef:
    letter_no: str
    letter_code: str


def letter_ref(text: str) -> LetterRef | None:
    """First letter number like `B.91.e-EMR/IRM/10/2026` -> number (spaces removed) and code.

    The code is the first two unit parts in upper case (`EMR/IRM`); one part if only one exists.
    """
    match = _LETTER.search(text)
    if not match:
        return None
    letter_no = re.sub(r"\s+", "", match.group(0))
    units = [unit.strip().upper() for unit in match.group("units").split("/")]
    return LetterRef(letter_no=letter_no, letter_code="/".join(units[:2]))


# --- Dates -------------------------------------------------------------------------------------

_MONTHS: dict[str, int] = {}
for _number, _names in enumerate(
    [
        ("januari", "january", "jan"),
        ("februari", "february", "feb", "pebruari"),
        ("maret", "march", "mar"),
        ("april", "apr"),
        ("mei", "may"),
        ("juni", "june", "jun"),
        ("juli", "july", "jul"),
        ("agustus", "august", "aug", "agu", "agt", "ags"),
        ("september", "sep", "sept"),
        ("oktober", "october", "okt", "oct"),
        ("november", "nov", "nop", "nopember"),
        ("desember", "december", "des", "dec"),
    ],
    start=1,
):
    for _name in _names:
        _MONTHS[_name] = _number

_MONTH_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))
_YEAR = r"(?P<year>\d{4}|'?\d{2})"
_DAY = r"(?P<day>\d{1,2})(?:st|nd|rd|th)?"

_DATE_PATTERNS = [
    # 06 November 2026 · 6 Nov 2026 · 6-Nov-26 · 6 November (no year)
    re.compile(
        rf"(?<![\w/]){_DAY}[\s-]+(?P<month>{_MONTH_RE})\.?(?:,?[\s-]+{_YEAR})?(?![\w/])",
        re.IGNORECASE,
    ),
    # November 6, 2026 · Nov 6 2026
    re.compile(
        rf"(?<![\w/])(?P<month>{_MONTH_RE})\.?\s+{_DAY},?\s+{_YEAR}(?![\w/])",
        re.IGNORECASE,
    ),
    # 2026-11-06
    re.compile(r"(?<![\w/.-])(?P<year>\d{4})-(?P<mnum>\d{1,2})-(?P<day>\d{1,2})(?![\w/-]|\.\d)"),
    # 06/11/2026 · 06.11.2026 · 06-11-2026 · 06/11/26 (day first)
    re.compile(
        r"(?<![\w/.-])(?P<day>\d{1,2})(?P<sep>[/.-])(?P<mnum>\d{1,2})(?P=sep)"
        r"(?P<year>\d{4}|\d{2})(?![\w/-]|\.\d)"
    ),
]

_DEADLINE_WORDS = re.compile(
    r"\b(?:deadline|paling\s+lambat|selambat-lambatnya|batas\s+waktu|tenggat|sebelum|due"
    r"|by|no\s+later\s+than|pengumpulan\s+pada|before)\b",
    re.IGNORECASE,
)
_DEADLINE_WINDOW = 60

_WEEKDAYS: dict[str, int] = {
    "senin": 0, "selasa": 1, "rabu": 2, "kamis": 3, "jumat": 4, "jum'at": 4, "sabtu": 5,
    "minggu": 6, "ahad": 6,
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5,
    "sunday": 6,
    "mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3, "fri": 4, "sat": 5,
    "sun": 6,
}  # fmt: skip
_WEEKDAY_BEFORE = re.compile(
    r"\b(?P<weekday>" + "|".join(sorted(_WEEKDAYS, key=len, reverse=True)) + r")\.?\s*,?\s*$",
    re.IGNORECASE,
)
_WEEKDAY_SHORT = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(frozen=True)
class DateHit:
    value: date
    start: int
    end: int
    year_assumed: bool = False


@dataclass
class DueDate:
    value: date | None
    hit: DateHit | None = None
    flags: list[str] = field(default_factory=list)


def _full_year(raw: str) -> int:
    raw = raw.lstrip("'")
    return int(raw) + 2000 if len(raw) == 2 else int(raw)


def find_dates(text: str, today: date) -> list[DateHit]:
    """All valid dates in `text`, in order of position. Overlapping matches keep the first."""
    hits: list[DateHit] = []
    taken: list[tuple[int, int]] = []
    for pattern in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span()
            if any(start < t_end and t_start < end for t_start, t_end in taken):
                continue
            groups = match.groupdict()
            month = int(groups["mnum"]) if groups.get("mnum") else _MONTHS[groups["month"].lower()]
            day = int(groups["day"])
            year_assumed = groups.get("year") is None
            year = today.year if year_assumed else _full_year(groups["year"])
            try:
                value = date(year, month, day)
                if year_assumed and value < today:
                    value = date(year + 1, month, day)
            except ValueError:
                continue
            hits.append(DateHit(value, start, end, year_assumed))
            taken.append((start, end))
    return sorted(hits, key=lambda hit: hit.start)


def due_date(text: str, today: date) -> DueDate:
    """Pick the deadline (DESIGN section 4 step 5) and return it with its flags."""
    hits = find_dates(text, today)
    if not hits:
        return DueDate(None)

    deadline_hits: list[DateHit] = []
    for word in _DEADLINE_WORDS.finditer(text):
        following = [hit for hit in hits if 0 <= hit.start - word.end() <= _DEADLINE_WINDOW]
        if following:
            deadline_hits.append(following[0])

    flags: list[str] = []
    if deadline_hits:
        upcoming = [hit for hit in deadline_hits if hit.value >= today]
        if upcoming:
            chosen = min(upcoming, key=lambda hit: hit.value)
        else:
            chosen = max(deadline_hits, key=lambda hit: hit.value)
            flags.append("Deadline already passed")
    else:
        upcoming = [hit for hit in hits if hit.value >= today]
        if not upcoming:
            return DueDate(None)
        chosen = min(upcoming, key=lambda hit: hit.value)
        flags.append("Deadline guessed (no deadline word)")

    if chosen.year_assumed:
        flags.append("Deadline year assumed")
    mismatch = weekday_check(text, chosen)
    if mismatch:
        flags.append(mismatch)
    return DueDate(chosen.value, chosen, flags)


def weekday_check(text: str, hit: DateHit) -> str | None:
    """Flag text when a weekday written right before the date does not match it (D13)."""
    match = _WEEKDAY_BEFORE.search(text[max(0, hit.start - 15) : hit.start])
    if not match:
        return None
    written = match.group("weekday")
    if _WEEKDAYS[written.lower()] == hit.value.weekday():
        return None
    written_short = _WEEKDAY_SHORT[_WEEKDAYS[written.lower()]]
    actual_short = _WEEKDAY_SHORT[hit.value.weekday()]
    return f"Weekday mismatch: {written} = {written_short}, {hit.value:%d %b %Y} is {actual_short}"


# --- Contacts ----------------------------------------------------------------------------------

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_CONTACT_TRIGGER = re.compile(
    r"(?<!\w)(?:a\.\s?n\.|a/n|PIC\b|menghubungi\b|hubungi\b|contact\b|CP\b)", re.IGNORECASE
)
_NAME_WORD = r"[A-Z][a-z]+(?:[-'][A-Za-z]+)?"
_NAME = re.compile(rf"{_NAME_WORD}(?:[ \t]+{_NAME_WORD}){{0,4}}")
_GAP_WORDS = {
    "dan", "and", "atau", "or", "terkait", "a.n.", "a.n", "a/n", "an", "pic", "yaitu", "yakni",
    "sdr", "sdr.", "sdri", "sdri.", "bapak", "ibu", "bpk", "bpk.", "pak", "bu", "mr", "mr.", "mrs",
    "mrs.", "ms", "ms.", "saudara", "saudari", "cp", "contact", "person", "kami", "di", "ke",
    "dengan", "with", "our", "the", "melalui", "via",
}  # fmt: skip
_HONORIFICS = {"Bapak", "Ibu", "Bpk", "Pak", "Bu", "Sdr", "Sdri", "Saudara", "Saudari", "Mr",
               "Mrs", "Ms", "Dr"}  # fmt: skip
_CONTACT_WINDOW = 200


def _gap_is_connector(gap: str) -> bool:
    gap = _EMAIL.sub(" ", gap)
    tokens = re.findall(r"[^\s,;:&/()\-–]+", gap)
    return all(token.lower() in _GAP_WORDS for token in tokens)


def _names_after(window: str) -> list[str]:
    window = re.sub(r"\([^)]*\)", " ", window)
    window = re.split(r"(?<![a-z]\.[a-z])(?<!\b[a-z])[.!?](?=\s|$)|\n\s*\n", window)[0]
    names: list[str] = []
    position = 0
    for match in _NAME.finditer(window):
        if not _gap_is_connector(window[position : match.start()]):
            break
        words = match.group(0).split()
        while words and words[0] in _HONORIFICS:
            words.pop(0)
        if len(words) >= 2 or (words and not names and match.start() - position <= 3):
            names.append(" ".join(words))
        position = match.end()
    return names


def contacts(text: str) -> list[Contact]:
    """Email addresses plus capitalised names after `a.n.`, `PIC`, `menghubungi`, `contact`.

    Single-word names are only taken right after the trigger word.
    """
    found: list[Contact] = []
    seen: set[str] = set()
    for trigger in _CONTACT_TRIGGER.finditer(text):
        window = text[trigger.end() : trigger.end() + _CONTACT_WINDOW]
        for name in _names_after(window):
            if name.lower() not in seen:
                seen.add(name.lower())
                found.append(Contact(name=name))
    for email in _EMAIL.findall(text):
        if email.lower() not in seen:
            seen.add(email.lower())
            found.append(Contact(email=email.lower()))
    return found


# --- Title -------------------------------------------------------------------------------------

_ABBREVIATIONS = {
    "no", "nomor", "yth", "bpk", "dll", "dsb", "dst", "tbk", "pt", "jl", "mr", "mrs", "ms", "dr",
    "e.g", "i.e", "a.n", "u.p", "etc", "vs", "hal", "lamp",
}  # fmt: skip
_TITLE_MAX = 90


def first_sentence(text: str) -> str:
    paragraph = re.split(r"\n\s*\n", text.strip(), maxsplit=1)[0]
    paragraph = re.sub(r"\s+", " ", paragraph)
    for match in re.finditer(r"[.!?](?=\s+[A-Z0-9\"“(])|[.!?]$", paragraph):
        before = paragraph[: match.start()]
        last_word = re.split(r"\s", before)[-1].lower() if before else ""
        if last_word in _ABBREVIATIONS or len(last_word.strip(".")) == 1:
            continue
        return paragraph[: match.end()].strip()
    return paragraph.strip()


_SUBJECT_LINE = re.compile(r"^\s*(?:perihal|hal|subject)\s*:\s*(?P<value>\S.*)$", re.I | re.M)
_LETTER_HEADER_LINE = re.compile(
    r"^\s*(?:(?:nomor|no|lampiran|lamp|sifat|tanggal|attachment|ref)\.?\s*:.*"
    r"|(?:kepada(?:\s+yth)?|yth|di)\b\.?.{0,30})$",
    re.IGNORECASE,
)


def title_rule(cleaned_text: str) -> str:
    """Rule title, at most 90 characters, cut at a word boundary.

    A formal letter's `Perihal:`/`Hal:`/`Subject:` line wins. Otherwise the first sentence of the
    text after letter header lines (`Nomor:`, `Lampiran:`, `Kepada`, `di …`) are skipped.
    """
    subject = _SUBJECT_LINE.search(cleaned_text)
    if subject:
        sentence = subject.group("value").strip()
    else:
        lines = cleaned_text.split("\n")
        while lines and (not lines[0].strip() or _LETTER_HEADER_LINE.match(lines[0])):
            lines.pop(0)
        sentence = first_sentence("\n".join(lines)).rstrip(".")
    return cap_title(sentence)


def cap_title(text: str) -> str:
    """One line, first letter capitalised, at most 90 characters cut at a word boundary."""
    text = re.sub(r"\s+", " ", text).strip()
    text = text[:1].upper() + text[1:]
    if len(text) <= _TITLE_MAX:
        return text
    cut = text[: _TITLE_MAX - 1]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:-") + "…"


# --- Requester ---------------------------------------------------------------------------------


def parse_mapping(value: str) -> dict[str, str]:
    """Config cell `a.co.id=BRI; b.go.id=OJK` -> {"a.co.id": "BRI", "b.go.id": "OJK"}."""
    mapping: dict[str, str] = {}
    for pair in re.split(r"[;\n]", value or ""):
        if "=" in pair:
            key, _, org = pair.partition("=")
            if key.strip() and org.strip():
                mapping[key.strip().lower()] = org.strip()
    return mapping


def requester_org(
    sender_email: str, text: str, domains: dict[str, str], known_orgs: list[str]
) -> str | None:
    """Organisation of the requester: sender domain first, then a known name in the text."""
    domain = sender_email.rpartition("@")[2].lower()
    while domain:
        if domain in domains:
            return domains[domain]
        domain = domain.partition(".")[2] if "." in domain else ""
    best: tuple[int, str] | None = None
    for org in known_orgs:
        org = org.strip()
        if not org:
            continue
        match = re.search(rf"(?<!\w){re.escape(org)}(?!\w)", text, re.IGNORECASE)
        if match and (best is None or match.start() < best[0]):
            best = (match.start(), org)
    return best[1] if best else None
