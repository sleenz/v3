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
NO_WORDS = frozenset({"no", "tidak", "nggak", "gak", "ga", "tolak", "decline", "cannot", "can't",
                      "cant", "sorry", "maaf"})  # fmt: skip


def pending(task: Task) -> bool:
    return task.status == "Proposed" and task.acceptance.strip().lower() == "pending"


def asked(store: SheetStore, task: Task) -> bool:
    """The current Doer was already asked about this task (a reassignment asks again)."""
    doer = task.doer.strip().casefold()
    return any(
        row.kind == ASK_KIND and task.id in row.tasks and row.person.casefold() == doer
        for row in outbox.rows(store)
    )


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
            return True
        except Exception as error:
            kind = type(error).__name__
            logger.warning("accept_ask_wa_failed", extra={"task_id": task.id, "error_type": kind})
    if not person.email.strip():
        return False
    sent = gmail.send(ask_email(task, person, sheet_url), store.config().sender_name)
    if sent is not None:
        outbox.record(store, "Email", sent.id, person.name, [task.id], ASK_KIND, sent.thread_id)
    return True


def answer_in(text: str) -> tuple[str, str] | None:
    """("accept", "") or ("decline", reason) from the first line of a reply; None otherwise.
    "Yes", "ya, siap", "ok" accept; "No, I am on leave" declines with the rest as the reason."""
    from app.replies import is_yes

    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if not first:
        return None
    if is_yes(first):
        return ("accept", "")
    words = re.findall(r"[a-z']+", first.lower())
    if words and words[0] in NO_WORDS:
        reason = re.sub(r"^\W*[\w']+\W*", "", first, count=1)
        reason = re.sub(r"^(?:bisa|can|thanks|thank you|sorry|maaf)\b\W*", "", reason, flags=re.I)
        return ("decline", reason.strip())
    return None


def thread_task(store: SheetStore, thread_id: str) -> str | None:
    """The task an acceptance question in this email thread was about."""
    for row in outbox.by_thread(store, thread_id):
        if row.kind == ASK_KIND and row.tasks:
            return row.tasks[0]
    return None
