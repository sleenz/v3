"""Gmail intake from the dedicated bot mailbox (DESIGN D3, D4, D15, D16; section 4 step 11).

The bot reads every unread message in its inbox:
- an auto-reply, or a sender not in Config `Allowed senders` → label `GRC/Ignored`, marked read;
- a reply in a thread that already produced a task → its text is added to that task's Notes,
  label `GRC/Reply`, marked read and archived (so "thanks, noted" never becomes a task);
- anything else → `process_request`; after the row is written the sender gets a reply in the
  same thread, then the message is labelled `GRC/Logged`, marked read and archived.
A failing message stays unread in the inbox and is retried; after 3 failures it gets
`GRC/Failed` and is marked read (mark it unread to retry by hand).
"""

from __future__ import annotations

import base64
import logging
import re
import threading
import time
from collections.abc import Callable
from datetime import datetime
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from html import escape as html_escape
from typing import Any, Protocol

from pydantic import BaseModel, Field

from app import clock
from app.extract.clean import strip_noise
from app.models import AppConfig, Attachment, AuditEntry, FailureRow, OutgoingEmail, RawRequest
from app.notify import acknowledgement_email, with_files
from app.pipeline import SERVICE_USER, Services, process_request
from app.settings import Settings
from app.sheets import SchemaError

__all__ = ["GmailAuthError", "GmailClient", "GmailMessage", "OutgoingEmail", "process_inbox"]


def _log_counts(run: BaseModel) -> dict[str, object]:
    """Run counts as log fields; prefixed because names like `created` are reserved by logging."""
    return {f"n_{key}": value for key, value in run.model_dump().items()}


logger = logging.getLogger("grc.gmail")

SCOPES = (
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
)
LOGGED_LABEL = "GRC/Logged"
FILES_LABEL = "GRC/Files"  # v1.4: files kept for tasks (proofs, review files, WhatsApp media)
FILES_SUBJECT = "[GRC files]"
IGNORED_LABEL = "GRC/Ignored"
REPLY_LABEL = "GRC/Reply"
COMMAND_LABEL = "GRC/Command"  # v1.4: a reply whose first line was a task command
_TASK_SUBJECT = re.compile(r"\[GRC\]\s+(G-\d+)\b")
SIGNATURE_IMAGE_BYTES = 100 * 1024  # small inline images (logos) are not proof files
FAILED_LABEL = "GRC/Failed"
UNREAD, INBOX = "UNREAD", "INBOX"  # Gmail system labels
MAX_FAILURES = 3
MESSAGES_PER_RUN = 20
# Stop starting new messages after this long, so a run with slow AI calls on PDFs (up to two
# 60-second tries) still ends inside Cloud Run's and Scheduler's 300-second limit. The rest stay
# unread for the next run.
RUN_BUDGET_SECONDS = 150.0
REPLY_NOTE_CHARS = 500
_AUTO_SENDERS = ("mailer-daemon@", "postmaster@", "no-reply@", "noreply@", "do-not-reply@")


class GmailAuthError(Exception):
    """The refresh token was rejected; the Gmail job stops and messages stay unread."""


class GmailMessage(BaseModel):
    id: str
    thread_id: str
    subject: str = ""
    sender_name: str = ""
    sender_email: str = ""
    received_at: datetime
    body_text: str = ""
    body_html: str = ""
    attachments: list[Attachment] = Field(default_factory=list)
    rfc_message_id: str = ""  # the Message-ID header, for In-Reply-To
    references: str = ""
    automatic: bool = False  # auto-reply, bounce or bulk mail


class GmailClient(Protocol):
    def address(self) -> str:
        """The bot mailbox's own address."""
        ...

    def unread_inbox(self, limit: int) -> list[str]:
        """IDs of unread inbox messages, oldest first."""
        ...

    def get_message(self, message_id: str) -> GmailMessage: ...

    def logged_message_ids(self, thread_id: str) -> list[str]:
        """IDs of messages in the thread that carry the `GRC/Logged` label."""
        ...

    def modify(self, message_id: str, add: list[str], remove: list[str]) -> None:
        """Change a message's labels by name (`UNREAD`, `INBOX` are Gmail's own)."""
        ...

    def reply(self, original: GmailMessage, email: OutgoingEmail, sender_name: str) -> None:
        """Send `email` as a reply in `original`'s thread."""
        ...

    def send(self, email: OutgoingEmail, sender_name: str) -> None: ...

    def store_files(self, task_id: str, attachments: list[Attachment]) -> None:
        """Keep files for a task in the bot mailbox (v1.4 F2): a message labelled `GRC/Files`,
        subject `[GRC files] <task ID>`, never sent and not in the inbox."""
        ...

    def task_files(self, task_id: str, source_id: str = "") -> list[Attachment]:
        """Every file the bot mailbox holds for a task (v1.4 F3): the original email
        (`source_id`), messages whose subject names the task, and the stored copies."""
        ...

    def watch(self, topic: str) -> datetime | None:
        """Ask Gmail to publish a note to the Pub/Sub `topic` whenever the Inbox changes
        (DESIGN section 20). Lasts 7 days; returns when it expires."""
        ...


# --- Parsing (pure) ----------------------------------------------------------------------------


def _decode_header_value(value: str) -> str:
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _b64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _charset(part: dict[str, Any]) -> str:
    for header in part.get("headers", []):
        if header.get("name", "").lower() == "content-type":
            for param in header.get("value", "").split(";")[1:]:
                key, _, value = param.strip().partition("=")
                if key.lower() == "charset" and value:
                    return value.strip('"')
    return "utf-8"


def _text(part: dict[str, Any]) -> str:
    data = part.get("body", {}).get("data")
    if not data:
        return ""
    raw = _b64(data)
    try:
        return raw.decode(_charset(part), errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def is_automatic(headers: dict[str, str], sender_email: str) -> bool:
    """Auto-replies, bounces and bulk mail (RFC 3834 and common vendor headers)."""
    if headers.get("auto-submitted", "no").strip().lower() not in ("", "no"):
        return True
    if any(name in headers for name in ("x-autoreply", "x-autorespond", "x-auto-response")):
        return True
    if headers.get("precedence", "").strip().lower() in ("bulk", "junk", "list", "auto_reply"):
        return True
    return sender_email.startswith(_AUTO_SENDERS)


def parse_message(
    message: dict[str, Any], fetch_attachment: Callable[[str, str], bytes]
) -> GmailMessage:
    """Gmail API `messages.get(format=full)` JSON -> GmailMessage.

    The first text/plain and text/html parts are the body; every part with a filename is an
    attachment (inline images included). `fetch_attachment(message_id, attachment_id)` loads
    attachment bytes that are not inline in the JSON.
    """
    payload = message.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    name, address = parseaddr(_decode_header_value(headers.get("from", "")))
    text = html = ""
    attachments: list[Attachment] = []

    def walk(part: dict[str, Any]) -> None:
        nonlocal text, html
        mime = part.get("mimeType", "")
        filename = part.get("filename") or ""
        body = part.get("body", {})
        if filename:
            if body.get("attachmentId"):
                data = fetch_attachment(message["id"], body["attachmentId"])
            else:
                data = _b64(body.get("data", ""))
            attachments.append(Attachment(filename=filename, mime=mime, data=data))
        elif mime == "text/plain" and not text:
            text = _text(part)
        elif mime == "text/html" and not html:
            html = _text(part)
        for child in part.get("parts", []) or []:
            walk(child)

    walk(payload)
    received = datetime.fromtimestamp(int(message.get("internalDate", "0")) / 1000, tz=clock.TZ)
    return GmailMessage(
        id=message["id"],
        thread_id=message.get("threadId", ""),
        subject=_decode_header_value(headers.get("subject", "")),
        sender_name=name,
        sender_email=address.lower(),
        received_at=received,
        body_text=text,
        body_html=html,
        attachments=attachments,
        rfc_message_id=headers.get("message-id", ""),
        references=headers.get("references", ""),
        automatic=is_automatic(headers, address.lower()),
    )


def build_mime(
    email: OutgoingEmail,
    sender: str,
    sender_name: str,
    in_reply_to: str = "",
    references: str = "",
) -> str:
    """Base64url `raw` for messages.send: plain text plus an HTML alternative."""
    message = EmailMessage()
    message["From"] = formataddr((sender_name, sender))
    message["To"] = ", ".join(email.to)
    if email.cc:
        message["Cc"] = ", ".join(email.cc)
    message["Subject"] = email.subject
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = f"{references} {in_reply_to}".strip()
    message.set_content(email.text)
    message.add_alternative(email.html, subtype="html")
    for attachment in email.attachments:
        maintype, _, subtype = (attachment.mime or "application/octet-stream").partition("/")
        message.add_attachment(
            attachment.data,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=attachment.filename,
        )
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


# --- Real client -------------------------------------------------------------------------------


class GoogleGmailClient:
    """Gmail API v1 as the bot mailbox ("me"), one client per thread."""

    def __init__(self, credentials: Any):
        self._credentials = credentials
        self._local = threading.local()
        self._label_ids: dict[str, str] = {}
        self._address: str | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> GoogleGmailClient:
        """GMAIL_REFRESH_TOKEN and the OAuth client JSON, both injected from Secret Manager."""
        from google.oauth2.credentials import Credentials

        client = settings.oauth_client()
        credentials = Credentials(
            token=None,
            refresh_token=settings.gmail_refresh_token.get_secret_value().strip(),
            client_id=client["client_id"],
            client_secret=client["client_secret"],
            token_uri=client.get("token_uri", "https://oauth2.googleapis.com/token"),
            scopes=list(SCOPES),
        )
        return cls(credentials)

    def _users(self) -> Any:
        if not hasattr(self._local, "service"):
            from googleapiclient.discovery import build

            self._local.service = build(
                "gmail", "v1", credentials=self._credentials, cache_discovery=False
            )
        return self._local.service.users()

    def _execute(self, request: Any) -> Any:
        from google.auth.exceptions import RefreshError

        try:
            return request.execute()
        except RefreshError as error:
            # Google's reason code (e.g. "invalid_grant", "unauthorized_client"); never a token.
            reason = str(error.args[0] if error.args else error).split(":")[0][:60]
            raise GmailAuthError(f"RefreshError: {reason}") from None

    def _label_id(self, name: str, create: bool) -> str | None:
        if name in (UNREAD, INBOX):
            return name
        if name not in self._label_ids:
            labels = self._execute(self._users().labels().list(userId="me")).get("labels", [])
            self._label_ids.update({label["name"]: label["id"] for label in labels})
        if name not in self._label_ids and create:
            created = self._execute(self._users().labels().create(userId="me", body={"name": name}))
            self._label_ids[name] = created["id"]
        return self._label_ids.get(name)

    def address(self) -> str:
        if self._address is None:
            profile = self._execute(self._users().getProfile(userId="me"))
            self._address = profile["emailAddress"].lower()
        return self._address

    def unread_inbox(self, limit: int) -> list[str]:
        response = self._execute(
            self._users().messages().list(userId="me", labelIds=[INBOX, UNREAD], maxResults=limit)
        )
        return [item["id"] for item in reversed(response.get("messages", []))]

    def get_message(self, message_id: str) -> GmailMessage:
        full = self._execute(
            self._users().messages().get(userId="me", id=message_id, format="full")
        )
        return parse_message(full, self._attachment)

    def _attachment(self, message_id: str, attachment_id: str) -> bytes:
        response = self._execute(
            self._users()
            .messages()
            .attachments()
            .get(userId="me", messageId=message_id, id=attachment_id)
        )
        return _b64(response["data"])

    def logged_message_ids(self, thread_id: str) -> list[str]:
        logged = self._label_id(LOGGED_LABEL, create=False)
        if logged is None:
            return []
        thread = self._execute(
            self._users().threads().get(userId="me", id=thread_id, format="minimal")
        )
        return [m["id"] for m in thread.get("messages", []) if logged in m.get("labelIds", [])]

    def modify(self, message_id: str, add: list[str], remove: list[str]) -> None:
        body = {
            "addLabelIds": [self._label_id(name, create=True) for name in add],
            "removeLabelIds": [i for i in (self._label_id(n, create=False) for n in remove) if i],
        }
        self._execute(self._users().messages().modify(userId="me", id=message_id, body=body))

    def reply(self, original: GmailMessage, email: OutgoingEmail, sender_name: str) -> None:
        raw = build_mime(
            email, self.address(), sender_name, original.rfc_message_id, original.references
        )
        body = {"raw": raw, "threadId": original.thread_id}
        self._execute(self._users().messages().send(userId="me", body=body))

    def send(self, email: OutgoingEmail, sender_name: str) -> None:
        raw = build_mime(email, self.address(), sender_name)
        self._execute(self._users().messages().send(userId="me", body={"raw": raw}))

    def store_files(self, task_id: str, attachments: list[Attachment]) -> None:
        if not attachments:
            return
        names = ", ".join(a.filename for a in attachments)
        email = OutgoingEmail(
            to=[self.address()],
            subject=f"{FILES_SUBJECT} {task_id}",
            text=f"Files kept for {task_id}: {names}",
            html=f"<p>Files kept for {task_id}.</p>",
            attachments=attachments,
        )
        raw = build_mime(email, self.address(), "GRC tracker")
        label = self._label_id(FILES_LABEL, create=True)
        self._execute(
            self._users().messages().insert(userId="me", body={"raw": raw, "labelIds": [label]})
        )

    def watch(self, topic: str) -> datetime | None:
        body = {"topicName": topic, "labelIds": [INBOX], "labelFilterBehavior": "include"}
        response = self._execute(self._users().watch(userId="me", body=body))
        expiration = response.get("expiration")
        if not expiration:
            return None
        return datetime.fromtimestamp(int(expiration) / 1000, tz=clock.TZ)

    def task_files(self, task_id: str, source_id: str = "") -> list[Attachment]:
        query = f'subject:"{task_id}" has:attachment'
        found = self._execute(
            self._users()
            .messages()
            .list(userId="me", q=query, maxResults=50, includeSpamTrash=False)
        ).get("messages", [])
        ids = [source_id] if source_id and not source_id.startswith(("wamid", "Recurring")) else []
        ids += [item["id"] for item in found if item["id"] not in ids]
        files: list[Attachment] = []
        seen: set[tuple[str, int]] = set()
        for message_id in ids:
            try:
                message = self.get_message(message_id)
            except GmailAuthError:
                raise
            except Exception as error:  # e.g. the original was deleted by hand
                logger.warning("task_file_missing", extra={"error_type": type(error).__name__})
                continue
            for attachment in message.attachments:
                key = (attachment.filename, attachment.size)
                if attachment.data and key not in seen:
                    seen.add(key)
                    files.append(attachment)
        return files


# --- The /jobs/gmail run -----------------------------------------------------------------------


class InboxRun(BaseModel):
    messages: int = 0
    created: int = 0
    duplicates: int = 0
    ignored: int = 0
    replies: int = 0
    failed: int = 0
    gave_up: int = 0
    ack_failed: int = 0
    commands: int = 0  # v1.4


def _record_failure(services: Services, key: str, error: Exception) -> int:
    """Count a failure in `_failures`; returns the new count (best effort)."""
    previous = services.store.failures().get(key)
    count = (previous.count if previous else 0) + 1
    try:
        services.store.upsert_failure(
            FailureRow(key=key, count=count, last_error=type(error).__name__, last_at=clock.now())
        )
    except Exception as write_error:
        logger.error("failure_count_not_saved", extra={"error_type": type(write_error).__name__})
    return count


def _modify(gmail: GmailClient, message_id: str, add: list[str], remove: list[str]) -> None:
    """Label change that never stops the run; a failure is retried by the next run."""
    try:
        gmail.modify(message_id, add, remove)
    except GmailAuthError:
        raise
    except Exception as error:
        logger.error("label_change_failed", extra={"error_type": type(error).__name__})


def _note_reply(services: Services, message: GmailMessage, logged_ids: list[str]) -> None:
    """Add a reply's text to the Notes of the task its thread produced."""
    task = next((t for t in (services.store.task_by_source_id(i) for i in logged_ids) if t), None)
    if task is None:
        return
    # Light clean only: drop the quoted chain but keep short replies such as "Terima kasih, noted".
    text = " ".join(strip_noise(message.body_text).split()) or "(no text)"
    if len(text) > REPLY_NOTE_CHARS:
        text = text[: REPLY_NOTE_CHARS - 1] + "…"
    line = f"Reply from {message.sender_email} on {message.received_at:%d %b %Y %H:%M}: {text}"
    notes = f"{task.notes}\n{line}" if task.notes else line
    services.store.update_task(task.id, {"Notes": notes})
    try:
        services.store.append_audit(
            [
                AuditEntry(
                    timestamp=clock.now(),
                    user=SERVICE_USER,
                    task_id=task.id,
                    action="Reply noted",
                    field="Notes",
                    reason="Email reply in the task's thread",
                )
            ]
        )
    except Exception as error:
        logger.error(
            "audit_write_failed", extra={"task_id": task.id, "error_type": type(error).__name__}
        )


def _ignore_reason(message: GmailMessage, bot: str, config: AppConfig) -> str:
    """Why a message becomes GRC/Ignored (a fixed code, never an address), or "" to process it."""
    if message.automatic:
        return "automatic"
    if message.sender_email == bot:
        return "sent_by_bot_mailbox"
    if not config.sender_allowed(message.sender_email):
        return "sender_not_allowed"
    return ""


def _email_command(
    gmail: GmailClient,
    services: Services,
    message: GmailMessage,
    logged: list[str],
    sheet_url: str,
) -> bool:
    """A task command in the first line of a Staff member's reply (DESIGN section 19): run it,
    answer in the thread, then label `GRC/Command`, read and archive. False: not a command."""
    from app.commands import parse_command
    from app.lifecycle import CommandContext, run_command

    store = services.store
    person = next(
        (p for p in store.staff() if p.email.strip().lower() == message.sender_email), None
    )
    if person is None:
        return False
    found = _TASK_SUBJECT.search(message.subject or "")
    context = found.group(1) if found else None
    if context is None and logged:
        task = next((t for t in (store.task_by_source_id(i) for i in logged) if t), None)
        context = task.id if task else None
    text = strip_noise(message.body_text) or message.body_text
    command = parse_command(text, has_context=context is not None)
    if command is None:
        return False
    files = [
        a
        for a in message.attachments
        if a.data and not (a.mime.startswith("image/") and a.size < SIGNATURE_IMAGE_BYTES)
    ]
    result = run_command(
        command,
        CommandContext(
            store=store,
            gmail=gmail,
            actor=person,
            channel="Email",
            sheet_url=sheet_url,
            files=files,
            context_task_id=context,
        ),
    )
    subject = (
        message.subject if message.subject.lower().startswith("re:") else (f"Re: {message.subject}")
    )
    reply = OutgoingEmail(
        to=[message.sender_email],
        subject=subject,
        text=result.reply,
        html=f"<pre>{html_escape(result.reply)}</pre>",
    )
    try:
        gmail.reply(message, with_files(reply, result.attachments), store.config().sender_name)
    except GmailAuthError:
        raise
    except Exception as error:
        logger.error("command_reply_failed", extra={"error_type": type(error).__name__})
    _modify(gmail, message.id, [COMMAND_LABEL], [UNREAD, INBOX])
    logger.info("email_command", extra={"kind": command.name, "n_changed": int(result.changed)})
    return True


class OneRunAtATime:
    """Inbox runs never overlap (a push and the 10-minute job could arrive together). A call
    that finds a run in progress returns None at once and makes that run go round once more,
    so mail that arrived during it is not left for the next run."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._again = threading.Event()

    def run[Result](self, work: Callable[[], Result]) -> Result | None:
        result: Result | None = None
        while True:
            if not self._lock.acquire(blocking=False):
                self._again.set()
                return result
            try:
                self._again.clear()
                result = work()
            finally:
                self._lock.release()
            if not self._again.is_set():
                return result


INBOX_RUNS = OneRunAtATime()


def process_inbox(
    gmail: GmailClient,
    services: Services,
    limit: int = MESSAGES_PER_RUN,
    budget_seconds: float = RUN_BUDGET_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    sheet_url: str = "",
) -> InboxRun:
    """Handle up to `limit` unread inbox messages, starting no new one after `budget_seconds`.
    Stops on schema or Gmail auth errors, leaving the remaining messages unread for the next run."""
    started = monotonic()
    config = services.store.config()
    if not config.allowed_senders:
        # D16: an empty list allows nobody, so every message is ignored. Say so plainly.
        logger.warning("no_allowed_senders")
    bot = gmail.address()
    run = InboxRun()
    for message_id in gmail.unread_inbox(limit):
        if monotonic() - started > budget_seconds:
            logger.info("run_budget_reached", extra={"budget_seconds": budget_seconds})
            break
        run.messages += 1
        try:
            message = gmail.get_message(message_id)
            reason = _ignore_reason(message, bot, config)
            if reason:
                _modify(gmail, message.id, [IGNORED_LABEL], [UNREAD])
                run.ignored += 1
                logger.info("message_ignored", extra={"reason": reason})
                continue
            logged = [i for i in gmail.logged_message_ids(message.thread_id) if i != message.id]
            if services.store.task_by_source_id(message.id) is None and _email_command(
                gmail, services, message, logged, sheet_url
            ):
                run.commands += 1
                continue
            if logged and services.store.task_by_source_id(message.id) is None:
                _note_reply(services, message, logged)
                _modify(gmail, message.id, [REPLY_LABEL], [UNREAD, INBOX])
                run.replies += 1
                continue
            raw = RawRequest(
                source="Email",
                source_id=message.id,
                received_at=message.received_at,
                subject=message.subject,
                body_text=message.body_text,
                body_html=message.body_html,
                sender_name=message.sender_name,
                sender_email=message.sender_email,
                attachments=message.attachments,
                submitted_by=message.sender_email,
            )
            result = process_request(raw, services)
        except (GmailAuthError, SchemaError):
            raise
        except Exception as error:
            run.failed += 1
            count = _record_failure(services, message_id, error)
            logger.warning(
                "request_failed", extra={"error_type": type(error).__name__, "failures": count}
            )
            if count >= MAX_FAILURES:
                # Stop retrying it; it stays in the inbox for a person (alert, section 11).
                _modify(gmail, message_id, [FAILED_LABEL], [UNREAD])
                run.gave_up += 1
                logger.error("request_gave_up", extra={"failures": count})
            continue
        # Only now, with the row written: acknowledge in the thread, then label, read, archive
        # (D15). If the label change fails, the next run finds the row by Source ID.
        try:
            gmail.reply(message, acknowledgement_email(result, message), config.sender_name)
        except GmailAuthError:
            raise
        except Exception as error:
            run.ack_failed += 1
            logger.error(
                "ack_failed",
                extra={"task_id": result.task_id, "error_type": type(error).__name__},
            )
        _modify(gmail, message.id, [LOGGED_LABEL], [UNREAD, INBOX])
        if result.created:
            run.created += 1
        else:
            run.duplicates += 1
    logger.info("gmail_run", extra=_log_counts(run))
    return run
