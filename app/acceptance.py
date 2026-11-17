"""Answering a PROMPT assignment with YES or NO (DESIGN section 27).

A PROMPT assignment is written as `Proposed` with Master `Acceptance` = `Pending`. The change job
(every 2 minutes, working hours only) then sends the Doer the usual assignment email with the
files and the original message (cc Owner and Support), ending with "Reply YES to start, or NO and
the reason", and sets `Asked`. On WhatsApp the Doer also gets [Terima] [Tolak] buttons (the
template `grc_tugas_baru` outside the 24-hour window).
The answer is the `accept` / `decline` command (`lifecycle`): accept sets `In Progress` and
sends nothing more; decline keeps the task `Proposed` for the Manager, flagged with the reason.
A reassignment writes `Pending` again, so the new (or the same) person gets the email again.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app import outbox
from app.models import OutgoingEmail, Task
from app.reminders import TEMPLATE_LANGUAGE, channel_for
from app.route import find_person
from app.sheets import SheetStore

logger = logging.getLogger("grc.acceptance")

ASK_KIND = "accept_ask"
TEMPLATE_ASK = "grc_tugas_baru"
ASKED = "Asked"
QUESTION = [
    "Reply YES to start this task (Status becomes In Progress).",
    "Reply NO and the reason if you cannot take it; the manager will reassign it.",
]


def pending(task: Task) -> bool:
    """Waiting for the Doer's answer: Acceptance `Pending` (not emailed yet) or `Asked`."""
    return task.awaiting_acceptance


def asked(store: SheetStore, task: Task) -> bool:
    """The current Doer already got the assignment email with the question."""
    return task.acceptance.strip().lower() == ASKED.lower()


def mark_asked(store: SheetStore, task: Task) -> None:
    try:
        store.update_task(task.id, {"Acceptance": ASKED})
    except Exception as error:  # the next run emails again rather than never
        logger.error("accept_mark_failed", extra={"task_id": task.id,
                                                  "error_type": type(error).__name__})  # fmt: skip


def _due(task: Task) -> str:
    return f"{task.due_date:%a %d %b %Y}" if task.due_date else "not set"


def with_question(email: OutgoingEmail, task: Task) -> OutgoingEmail:
    """The assignment email (files attached) with the YES / NO question on top."""
    head = ["Please answer this assignment:", *QUESTION, ""]
    html_head = "".join(f"<p><b>{line}</b></p>" for line in head if line)
    return email.model_copy(
        update={
            "subject": f"{email.subject} – reply YES or NO",
            "text": "\n".join(head) + "\n" + email.text,
            "html": html_head + email.html,
        }
    )


def ask_whatsapp(store: SheetStore, wa: Any, task: Task) -> None:
    """[Terima] [Tolak] for a Doer on WhatsApp (the files are in the email). Never raises."""
    person = find_person(task.doer, store.staff())
    if wa is None or person is None or channel_for(person) != "WhatsApp":
        return
    from app.wa_ui import window_open

    buttons = [(f"acc:{task.id}", "Terima"), (f"dec:{task.id}", "Tolak")]
    body = (f"Tugas baru dari manager: {task.id} – {task.title}, due {_due(task)}. File sudah "
            "dikirim ke email Anda. Mulai sekarang?")  # fmt: skip
    try:
        if window_open(store, person.name):
            message_id = wa.send_buttons(person.wa_id, body, buttons)
        else:
            params = [task.id, f"{task.title}, due {_due(task)}"]
            message_id = wa.send_template(person.wa_id, TEMPLATE_ASK, TEMPLATE_LANGUAGE, params,
                                          [b[0] for b in buttons])  # fmt: skip
        outbox.record(store, "WhatsApp", message_id, person.name, [task.id], ASK_KIND)
    except Exception as error:
        kind = type(error).__name__
        logger.warning("accept_ask_wa_failed", extra={"task_id": task.id, "error_type": kind})


_APOLOGY = re.compile(r"^(?:sorry|maaf)\b[^.!,;]*[.!,;]+\s*", re.I)  # "Maaf baru balas. "
_ACCEPT = re.compile(
    r"^(?:yes|ya|iya|y|ok|oke|okay|siap|sure|baik|boleh|accept(?:ed)?|setuju|"
    r"terima(?!\s+kasih))\b",
    re.I,
)
_NOT_PLAIN = re.compile(r"\b(?:but|tapi|namun|not|tidak|gak|nggak|can't|cannot)\b", re.I)
_DECLINE = re.compile(
    r"^(?:no|tidak|nggak|gak|ga|tolak|decline|cannot|can't|cant|"
    r"i\s+(?:can't|cannot|can\s+not)|saya\s+(?:tidak|gak|nggak)\s+bisa|not\s+able)\b"
    r"(?:\s+(?:bisa|can|do\s+it|take\s+it))?\W*",
    re.I,
)


_NO_PROBLEM = re.compile(  # "No problem, I'll do it" is a yes, not a no (QA6-A2)
    r"^(?:no\s+(?:problem|worries|prob)|(?:tidak|nggak|gak|ga)\s+(?:masalah|apa[- ]?apa|papa))\b",
    re.I,
)
_SIGN_OFF = re.compile(  # where an email's body ends (QA6-A5)
    r"^(?:--\s*|regards|best|kind regards|thanks|thank you|terima kasih|salam|hormat saya|"
    r"sent from my)\b",
    re.I,
)
_COURTESY = re.compile(r"^(?:thanks|thank you|sorry|maaf)\b\W*", re.I)


def _reason_lines(lines: list[str]) -> str:
    kept = []
    for line in lines:
        if _SIGN_OFF.match(line):
            break
        kept.append(line)
    return " ".join(kept[:2]).strip()


def answer_in(text: str) -> tuple[str, str] | None:
    """("accept", "") or ("decline", reason) from a reply to "please accept"; None when it is
    neither. "Yes, I will take it", "Siap" accept ("terima kasih" does not); "No, I am on leave"
    declines with the rest as the reason, which may also be on the next lines. A leading
    apology ("Maaf baru balas.") is skipped; a yes with "but" / "tapi" is not a plain yes."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    first = lines[0]
    if _APOLOGY.match(first) and len(_APOLOGY.sub("", first, count=1)) > 0:
        first = _APOLOGY.sub("", first, count=1)
    elif re.fullmatch(r"(?:sorry|maaf)\W*", first, re.I) and len(lines) > 1:
        first, lines = lines[1], lines[1:]
    if _NO_PROBLEM.match(first) and not re.search(r"\b(?:but|tapi)\b", first, re.I):
        return ("accept", "")
    if _ACCEPT.match(first) and not _NOT_PLAIN.search(first):
        return ("accept", "")
    found = _DECLINE.match(first)
    if found:
        reason = _COURTESY.sub("", first[found.end() :].strip()).strip()  # "No, sorry." (QA6-A1)
        if not reason:
            reason = _COURTESY.sub("", _reason_lines(lines[1:])).strip()
        return ("decline", reason[:300])
    return None


def thread_task(store: SheetStore, thread_id: str) -> str | None:
    """The task an acceptance question in this email thread was about."""
    for row in outbox.by_thread(store, thread_id):
        if row.kind == ASK_KIND and row.tasks:
            return row.tasks[0]
    return None
