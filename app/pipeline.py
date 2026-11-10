"""process_request: the one path every request takes (DESIGN section 4, steps 1-11).

Step 11 (Gmail label change, Chat reply) belongs to the caller and happens only after this
function returns, i.e. after the row is written. Exceptions propagate so the caller can count the
failure and leave the request for a retry (DESIGN D15, section 11); a retry is safe because the
Source ID dedupes the row and the Drive folder.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

from app import clock
from app.drive import DriveStore, folder_name
from app.extract import prompt as sender_prompt
from app.extract import rules
from app.extract.ai import PDFS_SKIPPED_FLAG, AIProvider, pdf_documents, read_prompt, run_ai
from app.extract.clean import (
    Unwrapped,
    clean_text,
    html_to_text,
    mask_text,
    strip_noise,
    unwrap_forward,
)
from app.models import AuditEntry, Contact, RawRequest, StaffMember, Task, TaskResult
from app.route import (
    fuzzy_staff_name,
    match_staff_name,
    route,
    sender_assignment,
    staff_mentioned,
)
from app.sheets import SheetStore

logger = logging.getLogger("grc.pipeline")

MAX_CELL_CHARS = 45_000
MAX_FILE_BYTES = 25 * 1024 * 1024
SERVICE_USER = "GRC service"
ORIGINAL_FILENAME = "original.txt"


@dataclass
class Services:
    store: SheetStore
    drive: DriveStore
    ai: AIProvider


def _result(task: Task, created: bool, due_from_prompt: bool = False) -> TaskResult:
    return TaskResult(
        due_from_prompt=due_from_prompt,
        task_id=task.id,
        created=created,
        title=task.title,
        owner=task.owner or None,
        due_date=task.due_date,
        flags=task.flags,
        files_url=task.files,
    )


def _strip_subject(subject: str) -> str:
    previous = None
    while previous != subject:
        previous = subject
        subject = re.sub(r"^\s*(?:re|fw|fwd|tr)\s*:\s*", "", subject, flags=re.I)
    return subject.strip()


def _original_file(raw: RawRequest, body: str) -> bytes:
    header = [
        f"Source: {raw.source}",
        f"Received: {raw.received_at.astimezone(clock.TZ):%Y-%m-%d %H:%M} HKT",
    ]
    if raw.sender_name or raw.sender_email:
        header.append(f"From: {raw.sender_name} <{raw.sender_email}>".replace(" <>", ""))
    if raw.subject:
        header.append(f"Subject: {raw.subject}")
    return ("\n".join(header) + "\n\n" + body).encode("utf-8")


def _files_note(raw: RawRequest) -> str:
    """`Files` without file storage: which attachments came with the request, and where."""
    if not raw.attachments:
        return ""
    names = ", ".join(attachment.filename for attachment in raw.attachments)
    if raw.source == "Email":
        return f"In the original email in the GRC bot mailbox: {names}"
    if raw.source == "WhatsApp":  # v1.4 decision F2: downloaded and kept in the bot mailbox
        return f"In the GRC bot mailbox: {names}"
    return f"Not stored: {names}"


PROMPT_NOT_STAFF_FLAG = "PROMPT ignored: sender is not in Staff"


@dataclass
class OwnerChoice:
    """Who the sender's PROMPT names as owner (a Staff full name, or None) and how it was read."""

    name: str | None = None
    flags: list[str] = field(default_factory=list)


def staff_match(name: str, staff: list[StaffMember]) -> tuple[StaffMember | None, bool]:
    """(person, typo corrected): exact Staff match first, then a close spelling."""
    person = match_staff_name(name, staff)
    if person is not None:
        return person, False
    person = fuzzy_staff_name(name, staff)
    return person, person is not None


def rules_owner(
    prompt: str, asked: sender_prompt.PromptOverrides, staff: list[StaffMember]
) -> OwnerChoice:
    """The owner the rules read in the PROMPT (the fallback when the AI cannot):
    1. the name in "assign to X", "ask X to", "assign X to", matched to Staff with a small typo
       allowed; 2. a Staff member named anywhere in a PROMPT that has an assign word."""
    if not asked.assign_to and not sender_prompt.wants_assignment(prompt):
        return OwnerChoice()
    if asked.assign_to:
        person, corrected = staff_match(asked.assign_to, staff)
        if person is not None:
            note = [f'PROMPT: "{asked.assign_to}" read as {person.name}'] if corrected else []
            return OwnerChoice(person.name, note)
    reviewer = staff_match(asked.reviewer, staff)[0] if asked.reviewer else None
    named = [
        p for p in staff_mentioned(sender_prompt.without_reviewer(prompt), staff) if p != reviewer
    ]
    if len(named) == 1:
        return OwnerChoice(named[0].name)
    if asked.assign_to:
        return OwnerChoice(None, [f'PROMPT: "{asked.assign_to}" not found in Staff'])
    return OwnerChoice(None, ["PROMPT: couldn't read who to assign"])


AI_READ_FLAG = "PROMPT read by AI - check"


@dataclass
class PromptDecision:
    """What the sender's PROMPT sets: Staff full names for owner and reviewer, and a date."""

    owner: str | None = None
    reviewer: str | None = None
    due: date | None = None
    flags: list[str] = field(default_factory=list)


def decide_prompt(
    prompt: str,
    today: date,
    staff: list[StaffMember],
    ai: AIProvider,
    ai_prompt: str = "",
) -> PromptDecision:
    """Read the sender's PROMPT (DESIGN section 21): the AI first, the rules as fallback and check.

    The AI sees only the PROMPT line. Names it reads must be written in the line and must match
    Staff by rules (typo allowed). When the AI fails, is off, or reads nothing usable, the rules
    decide. A deadline the rules also read wins over a different AI date (flagged). Anything the
    AI read is flagged; the manager still approves every task."""
    asked = sender_prompt.overrides(prompt, today)
    reading = read_prompt(ai, ai_prompt or prompt, today)
    flags: list[str] = []
    used_ai = False

    def person(name: str | None, what: str) -> str | None:
        if not name:
            return None
        found, corrected = staff_match(name, staff)
        if found is not None and corrected:
            flags.append(f'PROMPT: {what} "{name}" read as {found.name}')
        return found.name if found else None

    # Owner: the AI's name if it is in Staff, else the rules'.
    owner = person(reading.owner, "owner") if reading else None
    if owner:
        used_ai = True
        rules = rules_owner(prompt, asked, staff)
        if rules.name and rules.name != owner:
            flags.append(f"PROMPT: owner unclear (rules read {rules.name}) - check")
    else:
        choice = rules_owner(prompt, asked, staff)
        owner = choice.name
        flags.extend(choice.flags)
        if owner is None and reading and reading.owner:
            flags = [f for f in flags if not f.startswith("PROMPT: couldn't read")]
            flags.append(f'PROMPT: "{reading.owner}" not found in Staff')

    # Reviewer: the same, never the owner.
    reviewer = person(reading.reviewer, "reviewer") if reading else None
    if reviewer:
        used_ai = True
    elif asked.reviewer:
        reviewer = person(asked.reviewer, "reviewer")
        if reviewer is None:
            flags.append(f'PROMPT: reviewer "{asked.reviewer}" not found in Staff')
    elif reading and reading.reviewer:
        flags.append(f'PROMPT: reviewer "{reading.reviewer}" not found in Staff')

    # Deadline: the rules' date when they read one (an AI date that differs is flagged).
    ai_due = reading.deadline if reading else None
    due = asked.due
    flags.extend(asked.flags)
    if due is not None and ai_due is not None and ai_due != due:
        flags.append(f"PROMPT: deadline unclear (AI read {ai_due:%d %b %Y}) - check")
    elif due is None and ai_due is not None:
        due = ai_due
        used_ai = True

    if used_ai:
        flags.insert(0, AI_READ_FLAG)
    return PromptDecision(owner, reviewer, due, list(dict.fromkeys(flags)))


def _is_staff(store: SheetStore, email: str) -> bool:
    wanted = email.strip().lower()
    return bool(wanted) and any(p.email.strip().lower() == wanted for p in store.staff())


def _unique_contacts(contacts: list[Contact]) -> list[Contact]:
    seen: set[str] = set()
    unique = []
    for contact in contacts:
        key = (contact.email or contact.name or "").casefold()
        if key and key not in seen:
            seen.add(key)
            unique.append(contact)
    return unique


def process_request(raw: RawRequest, services: Services, today: date | None = None) -> TaskResult:
    store, drive = services.store, services.drive
    today = today or clock.today()
    config = store.config()

    # 1. Dedupe.
    existing = store.task_by_source_id(raw.source_id)
    if existing is not None:
        return _result(existing, created=False)

    original = raw.body_text if raw.body_text.strip() else html_to_text(raw.body_html)
    flags: list[str] = []

    # 2. Unwrap forwards (email only).
    if raw.source == "Email":
        unwrapped = unwrap_forward(original, raw.sender_name, raw.sender_email)
    else:
        unwrapped = Unwrapped(original, raw.sender_name, raw.sender_email, "", None, False)

    # 2b. An email PROMPT (DESIGN section 21): any `PROMPT:` line the sender typed, above a
    # forward or anywhere in a new email. Only a Staff member's PROMPT counts.
    note = raw.sender_note.strip()
    if raw.source == "Email" and not note:
        typed = unwrapped.manager_note if unwrapped.forwarded else unwrapped.body
        found, rest = sender_prompt.split_prompt_lines(typed or "")
        if found and _is_staff(store, raw.sender_email):
            note = found
            if unwrapped.forwarded:
                unwrapped = replace(unwrapped, manager_note=rest.strip() or None)
            else:
                unwrapped = replace(unwrapped, body=rest)
        elif found:
            flags.append(PROMPT_NOT_STAFF_FLAG)
    staff = store.staff()
    decision = PromptDecision()
    if note:
        masked_prompt = mask_text(note, mask_emails=config.mask_emails_for_ai)
        decision = decide_prompt(note, today, staff, services.ai, masked_prompt)
        flags.extend(decision.flags)
    body = unwrapped.body

    # 3-4. Clean (light for rules, full for title/routing/AI) and mask the AI copy.
    light = strip_noise(body)
    cleaned = clean_text(body)
    masked = mask_text(cleaned, mask_emails=config.mask_emails_for_ai)
    # The PROMPT guides the AI; the rules read the forwarded request alone, except for the
    # overrides the PROMPT states (assignee, reviewer, deadline), read by fixed rules.
    ai_text = masked
    quote_text = light
    if note:
        masked_note = mask_text(note, mask_emails=config.mask_emails_for_ai)
        ai_text = f"{masked}\n\nSender's instructions: {masked_note}"
        quote_text = f"{light}\n{note}"

    # 5. Rules.
    letter = rules.letter_ref(light)
    due = rules.due_date(light, today)
    due_date = due.value
    if decision.due is not None:  # the sender's deadline beats the message, the PDF and the AI
        due_date = decision.due
    else:
        flags.extend(due.flags)
    contacts = rules.contacts(light)
    if unwrapped.forwarded and unwrapped.sender_email:
        sender = Contact(name=unwrapped.sender_name or None, email=unwrapped.sender_email)
        contacts = [sender, *contacts]
    contacts = _unique_contacts(contacts)
    title = rules.title_rule(cleaned) if cleaned else ""
    if not title:
        title = rules.cap_title(_strip_subject(unwrapped.subject or raw.subject)) or "(no text)"
    requester = rules.requester_org(
        unwrapped.sender_email if unwrapped.forwarded else "",
        light,
        rules.parse_mapping(config.requester_domains),
        config.known_requesters,
    )

    # 6. AI (optional) and merge.
    summary = ""
    deliverables: list[str] = []
    ai_catalog_id = None
    # Recurring reports are generated from the Catalog, so there is nothing for the AI to read.
    if config.ai_provider != "Off" and raw.source != "Recurring":
        catalog = store.catalog()
        # v1.2 item 11: an email's (and v1.3 a WhatsApp message's) PDFs go to the AI as they are.
        # Chat attachments carry no bytes.
        documents, pdfs_skipped = (
            pdf_documents(raw.attachments)
            if config.ai_reads_pdfs and raw.source in ("Email", "WhatsApp")
            else ([], False)
        )
        if pdfs_skipped:
            flags.append(PDFS_SKIPPED_FLAG)
        outcome = run_ai(
            services.ai,
            ai_text,
            quote_text,
            catalog,
            [attachment.filename for attachment in raw.attachments],
            documents=documents,
        )
        flags.extend(outcome.flags)
        ai = outcome.result
        if ai is not None:
            title = ai.title_en.strip() or title
            summary = ai.summary_en.strip()
            deliverables = [item.strip() for item in ai.deliverables if item.strip()]
            ai_catalog_id = ai.catalog_id if ai.catalog_id != "NONE" else None
            if ai.deadline is not None and decision.due is None:
                if due_date is None:
                    due_date = ai.deadline
                elif ai.deadline != due_date:
                    flags.append(f"AI deadline differs: {ai.deadline:%d %b %Y}")

    # 7. Missing deadline.
    if due_date is None:
        due_date = today + timedelta(days=config.default_due_days)
        flags.append("No deadline found, default used")

    # 8. Files. With `File storage = Drive`: a folder found or created by Source ID, then
    # original.txt and attachments. With `None` (v1.2 default): nothing is stored; `Files` names
    # the attachments and where they are (the email in the bot mailbox).
    folder = None
    if config.file_storage == "Drive":
        folder = drive.task_folder(raw.source_id, folder_name("Pending", title))
        already = drive.file_names(folder.id)
        if ORIGINAL_FILENAME not in already:
            drive.upload(folder.id, ORIGINAL_FILENAME, "text/plain", _original_file(raw, original))
        for attachment in raw.attachments:
            if attachment.size > MAX_FILE_BYTES:
                flags.append(f"File skipped (over 25 MB): {attachment.filename}")
                continue
            if attachment.filename not in already:
                drive.upload(folder.id, attachment.filename, attachment.mime, attachment.data)
        files = folder.url
    else:
        files = _files_note(raw)
        if raw.source == "Google Chat" and raw.attachments:
            flags.append("Chat attachments not stored (no file storage): email them to the bot")

    # 9. Route.
    routed = route(
        text=cleaned,
        contacts=contacts,
        letter_code=letter.letter_code if letter else None,
        due=due_date,
        catalog=store.catalog(),
        staff=store.staff(),
        letter_codes=store.letter_codes(),
        config=config,
        today=today,
        ai_catalog_id=ai_catalog_id,
        known_catalog_id=raw.catalog_id,
    )
    assign_to = decision.owner if note else raw.assign_to
    if assign_to:
        routed = sender_assignment(routed, assign_to, staff, today, due_date, channel=raw.source)
    flags.extend(routed.flags)
    reviewer = decision.reviewer or ""
    if reviewer and reviewer == (routed.doer or routed.owner):
        flags.append("PROMPT: the reviewer is also the Doer")

    # 10. Write the row, then AuditLog.
    cell_text = original
    if len(cell_text) > MAX_CELL_CHARS:
        cell_text = cell_text[:MAX_CELL_CHARS]
        where = "full text in Files" if folder else "full text only in the original email"
        flags.append(f"Original message cut to {MAX_CELL_CHARS:,} characters, {where}")
    notes = f"Manager note: {unwrapped.manager_note}" if unwrapped.manager_note else ""
    if note:
        notes = "\n".join(part for part in (notes, f"Sender's PROMPT: {note}") if part)
    source = raw.source_id if raw.source == "Recurring" else raw.source
    task = Task(
        title=title,
        catalog_id=routed.catalog_id or "",
        division=routed.division or "",
        requester=requester or "",
        owner=routed.owner or "",
        doer=routed.doer or routed.owner or "",
        letter_no=letter.letter_no if letter else "",
        letter_code=letter.letter_code if letter else "",
        start=raw.received_at.astimezone(clock.TZ).date(),
        due_date=due_date,
        status="Proposed",
        last_updated=clock.now(),
        notes=notes,
        flag="; ".join(dict.fromkeys(flags)),
        source=source,
        routing_note=routed.note,
        original_message=cell_text,
        files=files,
        ai_summary=summary,
        deliverables="\n".join(f"- {item}" for item in deliverables),
        contacts="; ".join(contact.label() for contact in contacts),
        source_id=raw.source_id,
        needs_review="Yes" if reviewer else "",
        reviewer=reviewer,
    )
    saved, created = store.append_task(task)
    if not created:
        # Another writer stored this Source ID between step 1 and the write: nothing new written.
        return _result(saved, created=False)
    # The row is the record of the request; failures after this point are logged, not raised,
    # so the caller still acknowledges (a retry would only find the duplicate).
    try:
        store.append_audit(
            [
                AuditEntry(
                    timestamp=clock.now(),
                    user=SERVICE_USER,
                    task_id=saved.id,
                    action="Created",
                    reason=routed.note,
                )
            ]
        )
    except Exception as error:
        logger.error(
            "audit_write_failed", extra={"task_id": saved.id, "error_type": type(error).__name__}
        )
    if folder is not None:
        try:
            drive.rename_folder(folder.id, folder_name(saved.id, title))
        except Exception as error:
            logger.error(
                "folder_rename_failed",
                extra={"task_id": saved.id, "error_type": type(error).__name__},
            )
    logger.info(
        "task_created",
        extra={"task_id": saved.id, "rule": routed.rule, "flags": len(flags)},
    )
    return _result(saved, created=True, due_from_prompt=decision.due is not None)
