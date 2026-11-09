"""Email templates (DESIGN section 8). Pure: build the content, `GmailClient.send` delivers it."""

from __future__ import annotations

from html import escape
from typing import Protocol

from app.models import Attachment, OutgoingEmail, StaffMember, Task, TaskResult
from app.route import find_person

DIVIDER = "-" * 60


class HasSender(Protocol):
    subject: str
    sender_email: str


class MissingEmail(Exception):
    """The doer has no email in Staff, so the assignment cannot be sent."""


def _date(task: Task) -> str:
    return f"{task.due_date:%d %b %Y}" if task.due_date else "no due date"


MAX_EMAIL_FILES_BYTES = 20 * 1024 * 1024  # v1.4 decision F3: real attachments up to 20 MB


def fit_files(files: list[Attachment]) -> tuple[list[Attachment], list[str]]:
    """Attachments up to 20 MB in total, in order; the names of the rest (sent by name only)."""
    kept: list[Attachment] = []
    names: list[str] = []
    total = 0
    for attachment in files:
        if total + attachment.size <= MAX_EMAIL_FILES_BYTES:
            kept.append(attachment)
            total += attachment.size
        else:
            names.append(attachment.filename)
    return kept, names


def with_files(email: OutgoingEmail, files: list[Attachment]) -> OutgoingEmail:
    """`email` carrying `files` as attachments; past 20 MB the rest are named in the text."""
    kept, too_big = fit_files(files)
    if not too_big:
        return email.model_copy(update={"attachments": kept})
    note = "Too large to attach (kept in the GRC bot mailbox; ask the manager): " + ", ".join(
        too_big
    )
    return email.model_copy(
        update={
            "attachments": kept,
            "text": f"{email.text}\n\n{note}",
            "html": f"{email.html}<p>{escape(note)}</p>",
        }
    )


def assignment_subject(task: Task) -> str:
    return f"[GRC] {task.id} – {task.title} (due {_date(task)})"


def assignment_email(task: Task, staff: list[StaffMember], sheet_url: str) -> OutgoingEmail:
    """The email to the Doer (cc the Owner if different), with the full original message."""
    doer = find_person(task.doer, staff)
    if doer is None or not doer.email.strip():
        raise MissingEmail(task.id)
    owner = find_person(task.owner, staff)
    cc = []
    if owner is not None and owner.email.strip() and owner.name != doer.name:
        cc.append(owner.email.strip())

    deliverables = [line.removeprefix("- ").strip() for line in task.deliverables.splitlines()]
    deliverables = [item for item in deliverables if item]
    rows: list[tuple[str, str]] = [
        ("Task", f"{task.id} – {task.title}"),
        ("Due date", _date(task)),
        ("Flags", "; ".join(task.flags) or "none"),
        ("Division", task.division or "-"),
        ("Owner", task.owner or "-"),
        ("Routing note", task.routing_note or "-"),
    ]
    if task.ai_summary:
        rows.append(("AI summary", task.ai_summary))
    links = [("Files", task.files or "-"), ("Sheet", sheet_url)]

    text_lines = [f"You have been assigned {task.id}.", ""]
    text_lines += [f"{label}: {value}" for label, value in rows]
    if deliverables:
        text_lines += ["Deliverables:", *(f"  - {item}" for item in deliverables)]
    text_lines += [f"{label}: {value}" for label, value in links]
    text_lines += ["", DIVIDER, "Original message", DIVIDER, task.original_message]

    html_rows = "".join(
        f"<tr><th align='left' valign='top'>{escape(label)}</th><td>{escape(value)}</td></tr>"
        for label, value in rows
    )
    html_deliverables = (
        "<p><b>Deliverables</b></p><ul>"
        + "".join(f"<li>{escape(item)}</li>" for item in deliverables)
        + "</ul>"
        if deliverables
        else ""
    )
    html_links = "".join(
        f"<p><b>{escape(label)}:</b> <a href='{escape(url, quote=True)}'>{escape(url)}</a></p>"
        for label, url in links
        if url.startswith("http")
    )
    html = (
        f"<p>You have been assigned <b>{escape(task.id)}</b>.</p>"
        f"<table cellpadding='4'>{html_rows}</table>{html_deliverables}{html_links}"
        "<hr><p><b>Original message</b></p>"
        f"<pre style='white-space:pre-wrap;font-family:inherit'>"
        f"{escape(task.original_message)}</pre>"
    )
    return OutgoingEmail(
        to=[doer.email.strip()],
        cc=cc,
        subject=assignment_subject(task),
        text="\n".join(text_lines),
        html=html,
    )


# --- Acknowledgement (DESIGN section 4 step 11) ------------------------------------------------


def acknowledgement_email(result: TaskResult, original: HasSender) -> OutgoingEmail:
    """The reply to the person who emailed the bot, sent in the same thread."""
    subject = original.subject.strip()
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}" if subject else f"Re: [GRC] {result.task_id}"
    lines = [
        f"{'Logged' if result.created else 'Already logged'} as {result.task_id}: {result.title}",
        f"Proposed owner: {result.owner or 'none – needs review'}",
        f"Due: {result.due_date:%d %b %Y}" if result.due_date else "Due: not set",
        f"Flags: {'; '.join(result.flags) if result.flags else 'none'}",
        f"Files: {result.files_url or 'none'}",
        "",
        "Waiting for manager approval. This is an automatic reply from the GRC tracker.",
    ]
    html = "".join(f"<p>{escape(line)}</p>" for line in lines if line)
    return OutgoingEmail(
        to=[original.sender_email], subject=subject, text="\n".join(lines), html=html
    )


# --- Digests -----------------------------------------------------------------------------------


def task_line(task: Task) -> str:
    who = f" – {task.doer}" if task.doer else ""
    return f"{task.id} – {task.title} (due {_date(task)}, {task.status}){who}"


def digest_email(
    to: list[str], subject: str, intro: str, sections: list[tuple[str, list[str]]], sheet_url: str
) -> OutgoingEmail:
    """A digest with titled sections; empty sections are left out."""
    sections = [(title, lines) for title, lines in sections if lines]
    text = [intro, ""]
    html = [f"<p>{escape(intro)}</p>"]
    for title, lines in sections:
        text += [f"{title} ({len(lines)})", *(f"  - {line}" for line in lines), ""]
        html.append(f"<p><b>{escape(title)} ({len(lines)})</b></p><ul>")
        html += [f"<li>{escape(line)}</li>" for line in lines]
        html.append("</ul>")
    text.append(f"Sheet: {sheet_url}")
    html.append(f"<p><a href='{escape(sheet_url, quote=True)}'>Open the GRC sheet</a></p>")
    return OutgoingEmail(to=to, subject=subject, text="\n".join(text), html="".join(html))


def personal_digest(
    person: StaffMember,
    overdue: list[Task],
    due_soon: list[Task],
    stale: list[Task],
    sheet_url: str,
    today_label: str,
) -> OutgoingEmail | None:
    """None when there is nothing to report (no empty digests)."""
    sections = [
        ("Overdue", [task_line(task) for task in overdue]),
        ("Due within 7 days", [task_line(task) for task in due_soon]),
        ("In progress, not updated recently", [task_line(task) for task in stale]),
    ]
    if not any(lines for _, lines in sections):
        return None
    return digest_email(
        [person.email.strip()],
        f"[GRC] Your tasks – {today_label}",
        f"Hi {person.name}, here are your GRC tasks that need attention.",
        sections,
        sheet_url,
    )


def manager_digest(
    manager_email: str,
    sections: list[tuple[str, list[str]]],
    sheet_url: str,
    today_label: str,
) -> OutgoingEmail | None:
    if not any(lines for _, lines in sections):
        return None
    return digest_email(
        [manager_email],
        f"[GRC] Manager digest – {today_label}",
        "GRC tracker summary.",
        sections,
        sheet_url,
    )
