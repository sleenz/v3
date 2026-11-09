"""process_request: the one path every request takes (DESIGN section 4, steps 1-11).

Step 11 (Gmail label change, Chat reply) belongs to the caller and happens only after this
function returns, i.e. after the row is written. Exceptions propagate so the caller can count the
failure and leave the request for a retry (DESIGN D15, section 11); a retry is safe because the
Source ID dedupes the row and the Drive folder.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta

from app import clock
from app.drive import DriveStore, folder_name
from app.extract import rules
from app.extract.ai import PDFS_SKIPPED_FLAG, AIProvider, pdf_documents, run_ai
from app.extract.clean import (
    Unwrapped,
    clean_text,
    html_to_text,
    mask_text,
    strip_noise,
    unwrap_forward,
)
from app.models import AuditEntry, Contact, RawRequest, Task, TaskResult
from app.route import route, sender_assignment
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


def _result(task: Task, created: bool) -> TaskResult:
    return TaskResult(
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
    body = unwrapped.body

    # 3-4. Clean (light for rules, full for title/routing/AI) and mask the AI copy.
    light = strip_noise(body)
    cleaned = clean_text(body)
    masked = mask_text(cleaned, mask_emails=config.mask_emails_for_ai)
    # A WhatsApp PROMPT (v1.3) guides the AI only; the rules read the forwarded request alone.
    note = raw.sender_note.strip()
    ai_text = masked
    quote_text = light
    if note:
        masked_note = mask_text(note, mask_emails=config.mask_emails_for_ai)
        ai_text = f"{masked}\n\nSender's instructions: {masked_note}"
        quote_text = f"{light}\n{note}"

    # 5. Rules.
    letter = rules.letter_ref(light)
    due = rules.due_date(light, today)
    flags.extend(due.flags)
    due_date = due.value
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
            if ai.deadline is not None:
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
    if raw.assign_to:
        routed = sender_assignment(routed, raw.assign_to, store.staff(), today, due_date)
    flags.extend(routed.flags)

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
    return _result(saved, created=True)
