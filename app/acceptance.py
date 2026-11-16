"""Accepting an assignment (DESIGN section 23).

When a Manager assigns a task by PROMPT, the task is written as `Proposed` with Master
`Acceptance` = `Pending`. The change job (every 2 minutes, working hours only) then asks the
Doer once, on their channel:
- WhatsApp: [Terima] [Tolak] buttons inside the 24-hour window, else the template
  `grc_tugas_baru` with the same two quick replies;
- email: "Reply YES to accept, or NO and the reason".
The answer is the `accept` / `decline` command (`lifecycle`): accept sets `Not Started`, so the
change job then sends the usual assignment email with the files; decline returns the task to
the Manager. Whether someone was asked is read from `_outbox` (kind `accept_ask`).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app import outbox
from app.models import OutgoingEmail, StaffMember, Task
from app.reminders import TEMPLATE_LANGUAGE, channel_for
from app.route import find_person
from app.sheets import SheetStore

logger = logging.getLogger("grc.acceptance")

ASK_KIND = "accept_ask"
TEMPLATE_ASK = "grc_tugas_baru"


ASKED = "Asked"


def pending(task: Task) -> bool:
    """Waiting for the Doer's answer: Acceptance `Pending` (not asked yet) or `Asked`."""
    return task.awaiting_acceptance


def asked(store: SheetStore, task: Task) -> bool:
    """The current Doer was asked: the change job writes Acceptance `Asked` after the ask.
    A reassignment writes `Pending` again, so the new (or the same) person is asked again."""
    return task.acceptance.strip().lower() == ASKED.lower()


def _mark_asked(store: SheetStore, task: Task) -> None:
    try:
        store.update_task(task.id, {"Acceptance": ASKED})
    except Exception as error:  # the next run asks again rather than never
        logger.error("accept_mark_failed", extra={"task_id": task.id,
                                                  "error_type": type(error).__name__})  # fmt: skip


def _due(task: Task) -> str:
    return f"{task.due_date:%a %d %b %Y}" if task.due_date else "not set"


def ask_email(task: Task, person: StaffMember, sheet_url: str) -> OutgoingEmail:
    files = task.files.strip()
    if files and not files.startswith("http") and ":" in files:
        files = files.split(":", 1)[1].strip()  # "In the original email ...: a.pdf, b.xlsx"
    lines = [
        f"Hi {person.name.split()[0] if person.name.split() else person.name},",
        "",
        f"The manager assigned you {task.id}: {task.title}",
        f"Due: {_due(task)}",
    ]
    if task.ai_summary:
        lines += ["", task.ai_summary]
    if files:
        lines += ["", f"Files (sent to you once you accept): {files}"]
    lines += [
        "",
        "Reply YES to accept it. The assignment email with the files follows.",
        "Reply NO and the reason if you cannot take it; the manager will reassign it.",
        "",
        f"Sheet: {sheet_url}" if sheet_url else "",
    ]
    text = "\n".join(lines).rstrip()
    html = "".join(f"<p>{line}</p>" if line else "<br>" for line in lines)
    subject = f"[GRC] {task.id} – please accept: {task.title} (due {_due(task)})"
    return OutgoingEmail(to=[person.email.strip()], subject=subject, text=text, html=html)


def ask(store: SheetStore, gmail: Any, wa: Any, task: Task, sheet_url: str) -> bool:
    """Ask the Doer once, on their channel (WhatsApp falls back to email). True when sent."""
    person = find_person(task.doer, store.staff())
    if person is None or person.paused:
        return False
    if channel_for(person) == "WhatsApp" and wa is not None:
        from app.wa_ui import window_open

        buttons = [(f"acc:{task.id}", "Terima"), (f"dec:{task.id}", "Tolak")]
        body = f"Tugas baru dari manager: {task.id} – {task.title}, due {_due(task)}. Terima?"
        try:
            if window_open(store, person.name):
                message_id = wa.send_buttons(person.wa_id, body, buttons)
            else:
                summary = f"{task.title}, due {_due(task)}"
                params, payloads = [task.id, summary], [b[0] for b in buttons]
                message_id = wa.send_template(
                    person.wa_id, TEMPLATE_ASK, TEMPLATE_LANGUAGE, params, payloads
                )
            outbox.record(store, "WhatsApp", message_id, person.name, [task.id], ASK_KIND)
            _mark_asked(store, task)
            return True
        except Exception as error:
            kind = type(error).__name__
            logger.warning("accept_ask_wa_failed", extra={"task_id": task.id, "error_type": kind})
    if not person.email.strip():
        return False
    sent = gmail.send(ask_email(task, person, sheet_url), store.config().sender_name)
    if sent is not None:
        outbox.record(store, "Email", sent.id, person.name, [task.id], ASK_KIND, sent.thread_id)
    _mark_asked(store, task)
    return True


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
    if _ACCEPT.match(first) and not _NOT_PLAIN.search(first):
        return ("accept", "")
    found = _DECLINE.match(first)
    if found:
        reason = first[found.end() :].strip()
        if not reason:
            reason = " ".join(lines[1:3]).strip()
        reason = re.sub(r"^(?:thanks|thank you|sorry|maaf)\b\W*", "", reason, flags=re.I)
        return ("decline", reason.strip()[:300])
    return None


def thread_task(store: SheetStore, thread_id: str) -> str | None:
    """The task an acceptance question in this email thread was about."""
    for row in outbox.by_thread(store, thread_id):
        if row.kind == ASK_KIND and row.tasks:
            return row.tasks[0]
    return None
