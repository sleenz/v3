"""Unwrap forwarded emails, clean message text, and mask personal data for the AI copy.

All functions are pure. The untouched original is never modified here; callers store it separately.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

# --- Forward unwrapping ------------------------------------------------------------------------

_FORWARD_MARKER = re.compile(
    r"^\s*(?:-{2,}\s*(?:Forwarded message|Pesan yang diteruskan|Original Message|Pesan Asli)"
    r"\s*-{2,}"
    r"|_{10,})\s*$",
    re.IGNORECASE,
)
_HEADER_LINE = re.compile(
    r"^\s*\*?(From|Dari|Date|Sent|Dikirim|Tanggal|To|Kepada|Cc|Bcc|Subject|Perihal|Subjek)\*?\s*:",
    re.IGNORECASE,
)
_FROM_KEYS = {"from", "dari"}
_SUBJECT_KEYS = {"subject", "perihal", "subjek"}
_DATE_KEYS = {"date", "sent", "dikirim", "tanggal"}
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


@dataclass(frozen=True)
class Unwrapped:
    """The request part of a message. `forwarded` is False when no forward block was found."""

    body: str
    sender_name: str
    sender_email: str
    subject: str
    manager_note: str | None
    forwarded: bool


def _header_key(line: str) -> str | None:
    match = _HEADER_LINE.match(line)
    return match.group(1).lower() if match else None


def _header_block_starts(lines: list[str]) -> list[int]:
    """Indices of `From:`/`Dari:` lines that open a forwarded header block.

    A block counts when a subject line and a date/sent line follow within the next 6 lines.
    """
    starts = []
    for i, line in enumerate(lines):
        if _header_key(line) not in _FROM_KEYS:
            continue
        keys = {_header_key(following) for following in lines[i + 1 : i + 7]}
        if keys & _SUBJECT_KEYS and keys & _DATE_KEYS:
            starts.append(i)
    return starts


def _parse_from(value: str) -> tuple[str, str]:
    """`Name <a@b>`, `"Name" <a@b>`, `Name [mailto:a@b]` or a bare address -> (name, email)."""
    email_match = _EMAIL.search(value)
    email = email_match.group(0).lower() if email_match else ""
    name = re.split(r"[<\[]", value, maxsplit=1)[0]
    name = name.replace(email, "") if email and not re.search(r"[<\[]", value) else name
    name = name.strip().strip('"').strip("'").strip()
    return name, email


def unwrap_forward(body: str, sender_name: str = "", sender_email: str = "") -> Unwrapped:
    """Return the innermost forwarded request, its original sender and the manager's note.

    Without a forward block the whole body is the request and the given sender is kept.
    """
    lines = body.splitlines()
    starts = _header_block_starts(lines)
    if not starts:
        return Unwrapped(body, sender_name, sender_email, "", None, forwarded=False)

    # Manager note: everything above the first block, minus marker/separator lines.
    note_lines = [line for line in lines[: starts[0]] if not _FORWARD_MARKER.match(line)]
    note = "\n".join(note_lines).strip() or None

    # Innermost block: header lines run until the first blank line.
    first = starts[-1]
    end = first
    headers: dict[str, str] = {}
    while end < len(lines) and lines[end].strip():
        key = _header_key(lines[end])
        if key:
            headers.setdefault(key, lines[end].split(":", 1)[1].strip())
        end += 1

    from_value = headers.get("from") or headers.get("dari") or ""
    name, email = _parse_from(from_value)
    subject = next((headers[key] for key in ("subject", "perihal", "subjek") if key in headers), "")
    inner = "\n".join(lines[end:]).strip("\n")
    return Unwrapped(inner, name, email, subject.strip("*").strip(), note, forwarded=True)


# --- Cleaning ----------------------------------------------------------------------------------

_QUOTE_HEADER = re.compile(
    r"^[ \t]*(?:On\b[^\n]{0,200}(?:\n[^\n]{0,200})?\bwrote:"
    r"|Pada\b[^\n]{0,200}(?:\n[^\n]{0,200})?\bmenulis:)[ \t]*$",
    re.MULTILINE | re.IGNORECASE,
)
_GREETING = re.compile(
    r"^\s*(?:selamat\s+(?:pagi|siang|sore|malam)|yth\b\.?|kepada\s+yth\b\.?|dengan\s+hormat"
    r"|dear|hi|hai|halo|hallo|hello|assalamu'?alaikum(?:\s+wr\.?\s*wb\.?)?)\b[^,!:\n]*[,!:]?",
    re.IGNORECASE,
)
_SIGN_OFF = re.compile(
    r"^\s*(?:(?:terima\s*kasih|terimakasih|makasih|salam|hormat\s+kami|wassalam"
    r"|(?:best|kind|warm)\s+regards|regards|thanks|thank\s+you|cheers)\b"
    r"|best(?:\s+wishes)?\s*[,.!]?\s*$)",
    re.IGNORECASE,
)
_SIGNATURE_DELIMITER = re.compile(r"^--\s*$")
_SIGN_OFF_MAX_LINE = 60
_SIGN_OFF_MAX_TAIL = 12


def strip_noise(text: str) -> str:
    """Light clean: drop quoted reply chains, `>` lines and `*` emphasis; tidy whitespace."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    quote = _QUOTE_HEADER.search(text)
    if quote:
        text = text[: quote.start()]
    lines = [line for line in text.split("\n") if not line.lstrip().startswith(">")]
    text = "\n".join(lines).replace("*", "")
    return _tidy_whitespace(text)


def _tidy_whitespace(text: str) -> str:
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _is_sign_off(line: str) -> bool:
    return (
        bool(_SIGN_OFF.match(line))
        and len(line) <= _SIGN_OFF_MAX_LINE
        and not any(ch.isdigit() for ch in line)
    )


def _drop_greeting(line: str) -> str:
    match = _GREETING.match(line)
    if not match:
        return line
    return line[match.end() :].strip()


def clean_text(text: str) -> str:
    """Full clean used for titles, routing and AI: light clean plus greetings and sign-offs.

    A greeting only removes its own clause. A sign-off line (short, no digits, so it carries no
    date or number) removes itself, and also the rest of the message when no more than 12
    non-blank lines follow it (the signature block).
    """
    lines = strip_noise(text).split("\n")
    kept: list[str] = []
    for i, line in enumerate(lines):
        if _SIGNATURE_DELIMITER.match(line):
            break
        if _is_sign_off(line):
            tail = [rest for rest in lines[i + 1 :] if rest.strip()]
            if len(tail) <= _SIGN_OFF_MAX_TAIL:
                break
            continue
        line = _drop_greeting(line)
        kept.append(line)
    return _tidy_whitespace("\n".join(kept))


# --- Masking -----------------------------------------------------------------------------------

_HKID = re.compile(r"\b[A-Z]{1,2}\d{6}\s?\(?[0-9A]\)?(?![\w])")
_PASSPORT = re.compile(r"\b[A-Z]\d{7,8}\b")
_KTP = re.compile(r"(?<!\d)\d{16}(?!\d)")
_PHONE = re.compile(
    r"(?<![\w/.])(?:\+\d{1,3}[\s-]?)?(?:\(\d{2,4}\)[\s-]?)?\d{2,4}(?:[\s-]\d{3,4}){1,3}(?![\w/])"
    r"|(?<![\w/.])(?:\+|0)\d{9,13}(?![\w/])"
)
_YEAR_RANGE = re.compile(r"(?:19|20)\d\d[\s-](?:19|20)\d\d")
_ACCOUNT = re.compile(r"(?<!\d)\d{8,}(?!\d)")


def _mask_phone(match: re.Match[str]) -> str:
    value = match.group(0)
    if _YEAR_RANGE.fullmatch(value) or sum(ch.isdigit() for ch in value) < 8:
        return value
    return "[phone]"


def mask_text(text: str, mask_emails: bool = False) -> str:
    """Copy of `text` for the AI with phone, account and ID numbers replaced by placeholders."""
    if mask_emails:
        text = _EMAIL.sub("[email]", text)
    text = _HKID.sub("[id]", text)
    text = _PASSPORT.sub("[id]", text)
    text = _KTP.sub("[id]", text)
    text = _PHONE.sub(_mask_phone, text)
    return _ACCOUNT.sub("[account]", text)


# --- HTML --------------------------------------------------------------------------------------

_BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "head"}:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "head"}:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """Plain text of an HTML email body (used only when the email has no text part)."""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in "".join(parser.parts).split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
