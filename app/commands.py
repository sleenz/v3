"""Task commands (DESIGN section 19, v1.4): one parser for WhatsApp messages and email replies.

Pure: text in, `Command` out. A message is a command only when it matches a command fully, so a
forwarded request that happens to start with "review" or "cek" is still logged as a request:
verbs that act on a task need a task ID, unless the message replies to a message about one
task (`has_context`), and then only a short one-line message counts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

CommandName = Literal[
    "progress", "review", "done", "files", "mine", "today", "week",
    "approve", "return", "all", "recurring", "pause", "resume", "assign",
]  # fmt: skip

_TASK_ID = re.compile(r"\bG-?(\d{1,5})\b", re.IGNORECASE)
_LISTS: dict[str, CommandName] = {
    "tugas": "mine", "tugas saya": "mine", "tasks": "mine", "my tasks": "mine", "my task": "mine",
    "today": "today", "hari ini": "today", "tugas hari ini": "today",
    "week": "week", "this week": "week", "minggu ini": "week", "tugas minggu ini": "week",
    "semua": "all", "all": "all", "all tasks": "all", "semua tugas": "all",
    "recurring": "recurring", "rutin": "recurring", "laporan rutin": "recurring",
}  # fmt: skip
_TASK_VERBS: dict[str, CommandName] = {
    "progress": "progress", "progres": "progress", "kemajuan": "progress", "update": "progress",
    "review": "review", "cek": "review", "periksa": "review", "kirim review": "review",
    "done": "done", "selesai": "done", "beres": "done", "finish": "done", "finished": "done",
    "files": "files", "file": "files", "berkas": "files", "lampiran": "files",
    "approve": "approve", "approved": "approve", "setuju": "approve", "setujui": "approve",
    "acc": "approve",
    "return": "return", "kembalikan": "return", "revisi": "return", "tolak": "return",
}  # fmt: skip
_PERSON_VERBS: dict[str, CommandName] = {
    "pause": "pause", "jeda": "pause", "stop": "pause",
    "resume": "resume", "lanjut": "resume", "lanjutkan": "resume",
}  # fmt: skip
_ASSIGN = re.compile(
    r"^(?:assign|delegate|kasih(?:kan)?|tugaskan|serahkan)\s+"
    r"(?:(?P<id>G-?\d{1,5})\s+|(?:that|this|it|ini|itu)\s+)?"
    r"(?:to|ke|kepada)\s+(?P<name>.+?)\s*[.!]?$",
    re.IGNORECASE,
)
_ME = frozenset({"me", "myself", "saya", "aku", "gue", "gw"})
_MAX_CONTEXT_CHARS = 200  # a reply without a task ID is a command only if it is this short


@dataclass(frozen=True)
class Command:
    name: CommandName
    task_id: str | None = None
    value: str = ""  # progress number, return notes, or the person named


def task_id_in(text: str) -> str | None:
    """The first task ID in `text`, as stored (G-0012); "g12" and "G-12" count too."""
    match = _TASK_ID.search(text or "")
    return f"G-{int(match.group(1)):04d}" if match else None


def is_me(name: str) -> bool:
    return name.strip().casefold() in _ME


def _first_line(text: str) -> str:
    return next((line.strip() for line in (text or "").splitlines() if line.strip()), "")


def parse_command(text: str, has_context: bool = False) -> Command | None:
    """The command in the first line of `text`, or None when it is not one."""
    line = _first_line(text)
    if not line:
        return None
    plain = re.sub(r"\s+", " ", line.casefold()).strip(" .!?")
    if plain in _LISTS:
        return Command(_LISTS[plain])

    assign = _ASSIGN.match(line)
    if assign:
        named = assign.group("id")
        if named is None and not has_context:
            return Command("assign", None, assign.group("name").strip())  # "which task?"
        return Command("assign", task_id_in(named) if named else None, assign.group("name").strip())

    words = plain.split(" ")
    for size in (2, 1):  # two-word verbs first ("kirim review")
        verb = " ".join(words[:size])
        rest_words = line.split()[size:]
        if verb in _PERSON_VERBS and rest_words:
            return Command(_PERSON_VERBS[verb], value=" ".join(rest_words))
        if verb not in _TASK_VERBS:
            continue
        name = _TASK_VERBS[verb]
        rest = " ".join(rest_words)
        found = task_id_in(rest_words[0]) if rest_words else None
        if found:
            rest = " ".join(rest_words[1:])
        elif not has_context or len(line) > _MAX_CONTEXT_CHARS:
            return None
        if name == "progress":
            number = re.fullmatch(r"(\d{1,3}(?:[.,]\d+)?)\s*%?", rest.strip())
            if not number:
                return None
            return Command("progress", found, number.group(1).replace(",", "."))
        if name in ("approve", "files", "done", "review") and rest and not found:
            return None  # with context, "cek laporan ini dulu" is not a bare command
        return Command(name, found, rest.strip())
    return None


def progress_fraction(value: str) -> float | None:
    """ "50" → 0.5; "0.5" → 0.5 (a decimal up to 1 is already a fraction); None if out of range."""
    try:
        number = float(value)
    except ValueError:
        return None
    fraction = number if ("." in value and number <= 1) else number / 100
    return fraction if 0 <= fraction <= 1 else None
