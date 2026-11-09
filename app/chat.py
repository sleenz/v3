"""Google Chat bot over HTTP (DESIGN D7 and section 8).

Commands: `help`, `my tasks`; anything else is logged as a request. The reply is synchronous and
must come within 25 seconds: processing runs in a worker thread, and if it is not finished in
time the bot answers at once and the worker emails the result to the sender when it is done
(Cloud Run must keep CPU allocated for this, DESIGN D1).
"""

from __future__ import annotations

import io
import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime
from html import escape
from typing import Any, Protocol

from app import clock
from app.gmail import GmailClient, OutgoingEmail
from app.models import OPEN_STATUSES, Attachment, RawRequest, StaffMember, TaskResult
from app.pipeline import Services, process_request
from app.route import find_person

logger = logging.getLogger("grc.chat")

REPLY_WITHIN_SECONDS = 20.0  # the spec allows 25 s; keep a margin for the network
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
DEFERRED_TEXT = "Received – I'm still logging it. You'll get the task ID and details by email."
FAILED_TEXT = "Sorry, I couldn't log that. Please try again in a minute or email it."

HELP_TEXT = (
    "I log GRC requests into the tracker.\n"
    "• Paste a request (e.g. a WhatsApp message) and I will log it and propose an owner.\n"
    "• *my tasks* – your open tasks.\n"
    "• *help* – this message.\n"
    "Logged requests wait for the manager's approval before anyone is assigned."
)

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="chat")


class ChatClient(Protocol):
    def download_attachment(self, resource_name: str) -> bytes: ...


class GoogleChatClient:
    """Chat API with the runtime service account (app authentication, chat.bot scope)."""

    SCOPES = ("https://www.googleapis.com/auth/chat.bot",)

    def __init__(self, credentials: Any = None):
        self._credentials = credentials
        self._local = threading.local()

    def _service(self) -> Any:
        if not hasattr(self._local, "service"):
            import google.auth
            from googleapiclient.discovery import build

            credentials = self._credentials
            if credentials is None:
                credentials, _ = google.auth.default(scopes=list(self.SCOPES))
            self._local.service = build(
                "chat", "v1", credentials=credentials, cache_discovery=False
            )
        return self._local.service

    def download_attachment(self, resource_name: str) -> bytes:
        from googleapiclient.http import MediaIoBaseDownload

        buffer = io.BytesIO()
        request = self._service().media().download_media(resourceName=resource_name)
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
            if buffer.tell() > MAX_ATTACHMENT_BYTES:
                raise ValueError("attachment over 25 MB")
        return buffer.getvalue()


# --- Event parsing -----------------------------------------------------------------------------


def _sender(event: dict[str, Any]) -> tuple[str, str]:
    user = event.get("user") or event.get("message", {}).get("sender") or {}
    return str(user.get("displayName", "")), str(user.get("email", "")).strip().lower()


def _received_at(message: dict[str, Any]) -> datetime:
    raw = message.get("createTime") or ""
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(clock.TZ)
    except ValueError:
        return clock.now()


def allowed(email: str, domain: str) -> bool:
    domain = domain.strip().lower().lstrip("@")
    return bool(domain) and email.endswith("@" + domain)


def download_attachments(
    chat: ChatClient, message: dict[str, Any]
) -> tuple[list[Attachment], list[str]]:
    """(downloaded attachments, names that could not be downloaded)."""
    downloaded: list[Attachment] = []
    failed: list[str] = []
    for item in message.get("attachment", []) or []:
        name = item.get("contentName") or "attachment"
        resource = (item.get("attachmentDataRef") or {}).get("resourceName")
        if not resource:  # e.g. a Drive file: the service cannot read the sender's Drive
            failed.append(name)
            continue
        try:
            data = chat.download_attachment(resource)
        except Exception as error:
            logger.warning("chat_attachment_failed", extra={"error_type": type(error).__name__})
            failed.append(name)
            continue
        mime = item.get("contentType") or "application/octet-stream"
        downloaded.append(Attachment(filename=name, mime=mime, data=data))
    return downloaded, failed


# --- Replies -----------------------------------------------------------------------------------


def logged_text(result: TaskResult, failed_files: list[str]) -> str:
    lines = [
        f"{'Logged' if result.created else 'Already logged'} as {result.task_id}: {result.title}",
        f"Proposed owner: {result.owner or 'none – needs review'}",
        f"Due: {result.due_date:%d %b %Y}" if result.due_date else "Due: not set",
    ]
    if result.flags:
        lines.append("Flags: " + "; ".join(result.flags))
    if result.files_url:
        lines.append(f"Files: {result.files_url}")
    if failed_files:
        lines.append(
            "I could not attach " + ", ".join(failed_files) + ". Please drop the file(s) in the "
            "Files link above."
        )
    lines.append("Waiting for manager approval.")
    return "\n".join(lines)


def my_tasks_text(services: Services, email: str) -> str:
    person = next((p for p in services.store.staff() if p.email.strip().lower() == email), None)
    if person is None:
        return "I can't find your email in the Staff tab. Ask the GRC manager to add it."
    return person_tasks_text(services, person)


def person_tasks_text(services: Services, person: StaffMember) -> str:
    """`my tasks` for one Staff member (Chat, and WhatsApp in v1.3)."""
    tasks = [
        task
        for task in services.store.tasks()
        if task.status in OPEN_STATUSES and find_person(task.doer, [person]) is not None
    ]
    if not tasks:
        return f"{person.name}, you have no open tasks."
    tasks.sort(key=lambda t: (t.due_date is None, t.due_date))
    lines = [f"{person.name}, your open tasks ({len(tasks)}):"]
    for t in tasks:
        due = f"due {t.due_date:%d %b %Y}, " if t.due_date else ""
        lines.append(f"• {t.id} – {t.title} ({due}{t.status})")
    return "\n".join(lines)


def email_result(
    gmail: GmailClient, to: str, sender_name: str, done: Future[tuple[TaskResult, list[str]]]
) -> None:
    """Background completion: email the sender the full reply (or that it failed)."""
    try:
        result, failed = done.result()
        subject = f"[GRC] {result.task_id} – {result.title}"
        body = logged_text(result, failed)
    except Exception as error:
        logger.error("chat_request_failed", extra={"error_type": type(error).__name__})
        subject = "[GRC] Your request could not be logged"
        body = "Your Google Chat request could not be logged. Please send it again or email it."
    email = OutgoingEmail(to=[to], subject=subject, text=body, html=f"<pre>{escape(body)}</pre>")
    try:
        gmail.send(email, sender_name)
    except Exception as error:
        logger.error("chat_followup_failed", extra={"error_type": type(error).__name__})


# --- Event handling ----------------------------------------------------------------------------


def handle_event(
    event: dict[str, Any],
    services: Services,
    chat: ChatClient,
    gmail: GmailClient,
    wait_seconds: float = REPLY_WITHIN_SECONDS,
    submit: Callable[..., Future[Any]] = _executor.submit,
) -> dict[str, Any]:
    """Turn one Chat event into the synchronous reply body."""
    kind = event.get("type", "")
    if kind == "REMOVED_FROM_SPACE":
        return {}
    config = services.store.config()
    display_name, email = _sender(event)
    if not allowed(email, config.allowed_email_domain):
        logger.warning("chat_rejected_domain")
        return {"text": "Sorry, this bot is only for the GRC team's organisation."}
    if kind == "ADDED_TO_SPACE":
        return {"text": "Hi! " + HELP_TEXT}
    if kind != "MESSAGE":
        return {}

    message = event.get("message", {})
    text = (message.get("argumentText") or message.get("text") or "").strip()
    command = text.lower().strip(" .!?")
    if command in {"help", "/help"}:
        return {"text": HELP_TEXT}
    if command in {"my tasks", "my task", "/mytasks"}:
        return {"text": my_tasks_text(services, email)}
    if not text and not message.get("attachment"):
        return {"text": HELP_TEXT}

    def work() -> tuple[TaskResult, list[str]]:
        if config.file_storage == "Drive":
            attachments, failed = download_attachments(chat, message)
        else:  # no file storage: keep only the names (the pipeline flags them as not stored)
            attachments = [
                Attachment(filename=item.get("contentName") or "attachment",
                           mime=item.get("contentType") or "application/octet-stream", data=b"")
                for item in message.get("attachment", []) or []
            ]  # fmt: skip
            failed = []
        raw = RawRequest(
            source="Google Chat",
            source_id=message.get("name") or f"chat-{clock.now():%Y%m%d%H%M%S%f}",
            received_at=_received_at(message),
            body_text=text,
            sender_name=display_name,
            sender_email=email,
            attachments=attachments,
            submitted_by=email,
        )
        return process_request(raw, services), failed

    future = submit(work)
    try:
        result, failed = future.result(timeout=wait_seconds)
    except FutureTimeout:
        # The task ID only exists once the row is written, so the quick reply cannot carry it.
        logger.info("chat_reply_deferred")
        future.add_done_callback(lambda done: email_result(gmail, email, config.sender_name, done))
        return {"text": DEFERRED_TEXT}
    except Exception as error:
        logger.error("chat_request_failed", extra={"error_type": type(error).__name__})
        return {"text": FAILED_TEXT}
    return {"text": logged_text(result, failed)}
