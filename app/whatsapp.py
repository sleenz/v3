"""WhatsApp bot over the WhatsApp Cloud API (DESIGN section 17, v1.3).

Flow:
1. Meta → `POST /wa/webhook`: the signature is checked, each message is put on the Cloud Tasks
   queue and Meta gets its answer at once (a slow answer makes Meta resend).
2. Cloud Tasks → `POST /tasks/wa-process` (kind `message`): `help`, `my tasks`, a `PROMPT:`, a
   deadline answer, or the message opens or joins the sender's draft. A new draft queues a
   `finalize` task WAIT_SECONDS later, so the sender has time to add a PROMPT or a file.
3. `finalize`: the draft becomes one request (`process_request`, Source `WhatsApp`, Source ID = the
   first message ID); the bot replies with the result and asks the sender to confirm the deadline.
`/jobs/wa-drafts` finalizes drafts whose task never ran (DESIGN D15: never lose a request) and
prunes old rows; `/jobs/wa-reminders` sends the daily reminder template.

"Assign this to …" in a PROMPT is matched against Staff by `route.sender_assignment`: a person
chooses, never the AI, and the task still waits for the manager's approval.

Logs carry outcomes, counts and error types only: never message text or phone numbers.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, SecretStr

from app import clock
from app.chat import logged_text, person_tasks_text
from app.commands import parse_command
from app.extract.rules import find_dates
from app.gmail import GmailClient
from app.jobs import personal_buckets
from app.lifecycle import CommandContext, CommandResult, run_command
from app.models import (
    OPEN_STATUSES,
    Attachment,
    AuditEntry,
    FailureRow,
    OutgoingEmail,
    RawRequest,
    RouteResult,
    StaffMember,
    TaskResult,
    WaPending,
    wa_number,
)
from app.notify import with_files
from app.pipeline import SERVICE_USER, Services, process_request
from app.route import NEEDS_REVIEW_FLAG, match_staff_name, sender_assignment
from app.sheets import SheetStore

logger = logging.getLogger("grc.whatsapp")

GRAPH_URL = "https://graph.facebook.com"
WAIT_SECONDS = 30  # time to add a PROMPT or a file after forwarding a request
STALE_DRAFT_SECONDS = 120  # wa-drafts finalizes an open draft this old (its task never ran)
STUCK_SECONDS = 600  # a draft "processing" this long is retried
CONFIRM_HOURS = 24  # a deadline question can be answered this long (WhatsApp's reply window)
LATE_PROMPT_MINUTES = 5  # a PROMPT this soon after logging still applies to that task
KEEP_DAYS = 7  # finished rows are pruned after this long
MAX_MEDIA_BYTES = 25 * 1024 * 1024
MAX_TEXT_CHARS = 4096  # WhatsApp's limit for one text message
REMINDER_TEMPLATE = "grc_daily_reminder"
TEMPLATE_LANGUAGE = "en"
DEADLINE_FLAG = "Deadline not confirmed by sender (WhatsApp)"
DEFAULT_DUE_FLAG = "No deadline found, default used"

HELP_TEXT = (
    "I log GRC requests into the tracker.\n"
    "• Forward a request (text, PDF or photo). I log it 30 seconds later.\n"
    "• Within those 30 seconds you can send *PROMPT: Assign this to <name>* or other notes, "
    "e.g. *PROMPT: this is the ATMR report*.\n"
    "• After logging I ask you to confirm the deadline: reply *YES* or the correct date.\n"
    "• *my tasks* – your open tasks. *help* – this message.\n"
    "Logged requests wait for the manager's approval before anyone is assigned."
)
NOT_REGISTERED_TEXT = (
    "This number isn't registered with the GRC tracker. Ask the GRC manager to add it to the "
    "Staff tab."
)
UNSUPPORTED_TEXT = "I can read text, PDFs, documents and photos. Please send the request as one."
ACK_TEXT = (
    f"Got it. I'll log it in {WAIT_SECONDS} seconds. To assign it or add notes, send "
    "*PROMPT: …* now (e.g. PROMPT: Assign this to Stesya)."
)
NOTHING_TO_CONFIRM_TEXT = "There's no deadline waiting for a YES. Forward a request to log it."

_YES = frozenset(
    {
        "yes",
        "y",
        "ya",
        "iya",
        "ok",
        "oke",
        "okay",
        "betul",
        "benar",
        "correct",
        "confirm",
        "confirmed",
        "sudah benar",
        "yes correct",
        "ya betul",
        "ok betul",
    }
)
_PROMPT = re.compile(r"^\s*prompt\s*:?\s*(.*)$", re.IGNORECASE | re.DOTALL)
_ASSIGN = re.compile(
    r"\b(?:assign(?:ed)?|give|kasih(?:kan)?|tugaskan)\s+(?:this\s+|it\s+|ini\s+)?"
    r"(?:to|ke|kepada)\s+([^\n.,;!?]+)",
    re.IGNORECASE,
)
_POLITE_TAIL = re.compile(r"(?:\s+(?:please|pls|plz|ya+|thanks|thx|tq|ok|yah))+\s*$", re.I)


# --- Meta Graph API and Cloud Tasks -------------------------------------------------------------


class WhatsAppError(Exception):
    """A Graph API call failed. Carries the HTTP status only (the body can quote messages)."""

    def __init__(self, status: int):
        super().__init__(f"Graph API status {status}")
        self.status_code = status


class WhatsAppClient(Protocol):
    def send_text(self, to: str, text: str) -> str | None:
        """Send a text; returns the message ID (v1.4: replies to it carry it as context)."""
        ...

    def send_template(self, to: str, name: str, language: str, params: list[str]) -> None: ...

    def download_media(self, media_id: str) -> bytes: ...


class GraphWhatsApp:
    """WhatsApp Cloud API with a system-user access token (standard library HTTP)."""

    def __init__(
        self,
        phone_number_id: str,
        access_token: SecretStr,
        version: str,
        urlopen: Callable[..., Any] = urllib.request.urlopen,
    ):
        self._phone_number_id = phone_number_id
        self._token = access_token
        self._version = version
        self._urlopen = urlopen

    def _call(self, url: str, payload: dict[str, Any] | None = None) -> bytes:
        headers = {"Authorization": f"Bearer {self._token.get_secret_value()}"}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers=headers)
        try:
            with self._urlopen(request, timeout=20) as response:
                data = response.read(MAX_MEDIA_BYTES + 1)
        except urllib.error.HTTPError as error:
            raise WhatsAppError(error.code) from None
        if len(data) > MAX_MEDIA_BYTES:
            raise ValueError("WhatsApp media over 25 MB")
        return data

    def _send(self, to: str, message: dict[str, Any]) -> str | None:
        url = f"{GRAPH_URL}/{self._version}/{self._phone_number_id}/messages"
        data = self._call(
            url,
            {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to, **message},
        )
        try:
            return str(json.loads(data or b"{}").get("messages", [{}])[0].get("id") or "") or None
        except (ValueError, IndexError, AttributeError):
            return None

    def send_text(self, to: str, text: str) -> str | None:
        return self._send(
            to, {"type": "text", "text": {"preview_url": False, "body": text[:MAX_TEXT_CHARS]}}
        )

    def send_template(self, to: str, name: str, language: str, params: list[str]) -> None:
        parameters = [{"type": "text", "text": value} for value in params]
        self._send(
            to,
            {
                "type": "template",
                "template": {
                    "name": name,
                    "language": {"code": language},
                    "components": [{"type": "body", "parameters": parameters}],
                },
            },
        )

    def download_media(self, media_id: str) -> bytes:
        info = json.loads(self._call(f"{GRAPH_URL}/{self._version}/{media_id}"))
        return self._call(str(info["url"]))


class TaskQueue(Protocol):
    def enqueue(self, payload: dict[str, Any], delay_seconds: float = 0) -> None: ...


class CloudTasksQueue:
    """Cloud Tasks queue whose tasks POST to `/tasks/wa-process` with the runtime account's
    OIDC token (audience = SERVICE_URL), which `auth.require_task` checks."""

    def __init__(
        self,
        project: str,
        region: str,
        queue: str,
        target_url: str,
        service_account_email: str,
        audience: str,
        credentials: Any = None,
    ):
        self._parent = f"projects/{project}/locations/{region}/queues/{queue}"
        self._target_url = target_url
        self._service_account_email = service_account_email
        self._audience = audience
        self._credentials = credentials
        self._service: Any = None

    def _tasks(self) -> Any:
        if self._service is None:
            import google.auth
            from googleapiclient.discovery import build

            credentials = self._credentials
            if credentials is None:
                scopes = ["https://www.googleapis.com/auth/cloud-platform"]
                credentials, _ = google.auth.default(scopes=scopes)
            self._service = build(
                "cloudtasks", "v2", credentials=credentials, cache_discovery=False
            )
        return self._service.projects().locations().queues().tasks()

    def enqueue(self, payload: dict[str, Any], delay_seconds: float = 0) -> None:
        task: dict[str, Any] = {
            "httpRequest": {
                "httpMethod": "POST",
                "url": self._target_url,
                "headers": {"Content-Type": "application/json"},
                "body": base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii"),
                "oidcToken": {
                    "serviceAccountEmail": self._service_account_email,
                    "audience": self._audience,
                },
            }
        }
        if delay_seconds > 0:
            when = clock.now() + timedelta(seconds=delay_seconds)
            task["scheduleTime"] = when.astimezone(UTC).isoformat().replace("+00:00", "Z")
        self._tasks().create(parent=self._parent, body={"task": task}).execute()


# --- Webhook ------------------------------------------------------------------------------------


class WAMedia(BaseModel):
    id: str
    filename: str
    mime: str


class WAMessage(BaseModel):
    id: str
    sender: str  # digits, as WhatsApp sends them
    received_at: datetime
    kind: Literal["text", "media", "other"]
    text: str = ""
    media: list[WAMedia] = Field(default_factory=list)
    context_id: str = ""  # v1.4: the bot message this one replies to (WhatsApp "reply")


def signature_ok(app_secret: str, body: bytes, header: str | None) -> bool:
    """`X-Hub-Signature-256: sha256=<hex HMAC of the raw body with the app secret>`."""
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def verify_challenge(params: dict[str, str], verify_token: str) -> str | None:
    """Meta's webhook verification: the challenge to echo, or None to refuse."""
    if (
        verify_token
        and params.get("hub.mode") == "subscribe"
        and hmac.compare_digest(params.get("hub.verify_token", ""), verify_token)
    ):
        return params.get("hub.challenge", "")
    return None


def _received(timestamp: Any) -> datetime:
    try:
        return datetime.fromtimestamp(int(timestamp), clock.TZ)
    except (TypeError, ValueError):
        return clock.now()


def parse_webhook(payload: dict[str, Any], phone_number_id: str) -> list[WAMessage]:
    """The messages in one webhook call for our number. Delivery and read receipts are skipped."""
    messages: list[WAMessage] = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value") or {}
            metadata = value.get("metadata") or {}
            if str(metadata.get("phone_number_id", "")) != phone_number_id:
                continue
            for item in value.get("messages", []) or []:
                kind = item.get("type")
                text = ""
                media: list[WAMedia] = []
                if kind == "text":
                    text = str((item.get("text") or {}).get("body", ""))
                elif kind in ("document", "image"):
                    part = item.get(kind) or {}
                    text = str(part.get("caption", ""))
                    mime = str(part.get("mime_type", "application/octet-stream"))
                    default = "photo.jpg" if kind == "image" else "document"
                    name = str(part.get("filename") or default)
                    media.append(WAMedia(id=str(part.get("id", "")), filename=name, mime=mime))
                messages.append(
                    WAMessage(
                        id=str(item.get("id", "")),
                        sender=wa_number(str(item.get("from", ""))),
                        received_at=_received(item.get("timestamp")),
                        kind="text" if kind == "text" else "media" if media else "other",
                        text=text,
                        media=media,
                        context_id=str((item.get("context") or {}).get("id", "")),
                    )
                )
    return messages


# --- PROMPT ---------------------------------------------------------------------------------------


def parse_prompt(text: str) -> str | None:
    """The PROMPT's text, or None when the message is not a PROMPT."""
    match = _PROMPT.match(text)
    return match.group(1).strip() if match else None


def assignee(prompt: str) -> str | None:
    """The person named in "assign this to <name>" (the last one, if repeated)."""
    names = [_POLITE_TAIL.sub("", found).strip() for found in _ASSIGN.findall(prompt)]
    names = [name for name in names if name]
    return names[-1] if names else None


# --- Processing -----------------------------------------------------------------------------------


@dataclass
class WaContext:
    services: Services
    wa: WhatsAppClient
    queue: TaskQueue
    gmail: GmailClient | None = None  # v1.4: files kept in the bot mailbox, commands
    sheet_url: str = ""

    @property
    def store(self) -> SheetStore:
        return self.services.store


def _reply(ctx: WaContext, to: str, text: str, task_id: str = "") -> None:
    """Send a text. Messages about one task are remembered (v1.4), so a WhatsApp reply to them,
    or "assign that to …", knows the task."""
    try:
        message_id = ctx.wa.send_text(to, text)
    except Exception as error:  # the state change already happened; a resend would repeat it
        logger.error("wa_reply_failed", extra={"error_type": type(error).__name__})
        return
    if not message_id:
        return
    now = clock.now()
    sent = WaPending(
        key=message_id,
        kind="sent",
        sender=to,
        task_id=task_id,
        status="done",
        created_at=now,
        updated_at=now,
    )
    try:
        ctx.store.edit_wa_pending(lambda rows: rows.append(sent))
    except Exception as error:
        logger.warning("wa_sent_not_recorded", extra={"error_type": type(error).__name__})


def _context_task(ctx: WaContext, message: WAMessage, implicit: bool) -> str | None:
    """The task of the bot message replied to; with `implicit`, else the task of the newest
    message the bot sent this person (if it was about one task, within 24 hours)."""
    rows = [r for r in ctx.store.wa_pending() if r.kind == "sent"]
    if message.context_id:
        replied = next((r for r in rows if r.key == message.context_id), None)
        return (replied.task_id or None) if replied else None
    if not implicit:
        return None
    since = clock.now() - timedelta(hours=CONFIRM_HOURS)
    mine = [r for r in rows if r.sender == message.sender and (r.created_at or since) >= since]
    newest = max(mine, key=lambda r: r.created_at or since, default=None)
    return (newest.task_id or None) if newest else None


def _download(ctx: WaContext, media: list[WAMedia]) -> tuple[list[Attachment], list[str]]:
    """Every file now: Meta's media links expire (v1.4 decision F2)."""
    files: list[Attachment] = []
    failed: list[str] = []
    for item in media:
        try:
            data = ctx.wa.download_media(item.id)
        except Exception as error:
            logger.warning("wa_media_failed", extra={"error_type": type(error).__name__})
            failed.append(item.filename)
            continue
        files.append(Attachment(filename=item.filename, mime=item.mime, data=data))
    return files, failed


def _handle_command(message: WAMessage, person: StaffMember, ctx: WaContext) -> str | None:
    """Run a task command (DESIGN section 19); None when the message is not one."""
    text = message.text.strip()
    explicit = _context_task(ctx, message, implicit=False) if message.context_id else None
    command = parse_command(text, has_context=explicit is not None)
    if command is None or ctx.gmail is None:
        return None
    context = explicit
    if command.name == "assign" and command.task_id is None and context is None:
        context = _context_task(ctx, message, implicit=True)  # "assign that to Cia" (A1)
    files, failed = _download(ctx, message.media)
    result = run_command(
        command,
        CommandContext(
            store=ctx.store,
            gmail=ctx.gmail,
            actor=person,
            channel="WhatsApp",
            sheet_url=ctx.sheet_url,
            files=files,
            context_task_id=context,
        ),
    )
    reply = result.reply
    if failed:
        reply += "\nI could not download: " + ", ".join(failed) + ". Please send it again."
    if result.attachments:
        reply += _email_files(ctx, person, result)
    _reply(ctx, message.sender, reply, result.task_id or "")
    return f"command_{command.name}"


def _email_files(ctx: WaContext, person: StaffMember, result: CommandResult) -> str:
    """WhatsApp does not carry files unless Config says so (decision F3): email them instead."""
    if not person.email.strip() or ctx.gmail is None:
        return "\nI have no email address for you to send the files to."
    subject = f"[GRC] {result.task_id} – files"
    text = result.reply
    email = OutgoingEmail(to=[person.email.strip()], subject=subject, text=text, html=text)
    try:
        ctx.gmail.send(with_files(email, result.attachments), ctx.store.config().sender_name)
    except Exception as error:
        logger.error("wa_files_email_failed", extra={"error_type": type(error).__name__})
        return "\nI could not email the files; try again later."
    return "\nThe files are in your email."


def _person(store: SheetStore, sender: str) -> StaffMember | None:
    return next((p for p in store.staff() if sender and p.wa_id == sender), None)


def _ids(row: WaPending) -> list[str]:
    return [part for part in row.message_ids.split(",") if part]


def _seen(rows: list[WaPending], message_id: str) -> bool:
    return any(message_id in _ids(row) for row in rows)


def _add_id(row: WaPending, message_id: str) -> None:
    row.message_ids = ",".join([*_ids(row), message_id])


def _open_draft(rows: list[WaPending], sender: str, now: datetime) -> WaPending | None:
    """The sender's draft still in its window. An older open draft (its logging failed and waits
    for a retry) takes no new messages, so an unrelated request never joins it."""
    since = now - timedelta(seconds=2 * WAIT_SECONDS)
    return next(
        (
            r
            for r in rows
            if r.kind == "draft"
            and r.sender == sender
            and r.status == "open"
            and (r.created_at is None or r.created_at >= since)
        ),
        None,
    )


def _open_confirm(rows: list[WaPending], sender: str, now: datetime) -> WaPending | None:
    since = now - timedelta(hours=CONFIRM_HOURS)
    candidates = [
        r
        for r in rows
        if r.kind == "confirm"
        and r.sender == sender
        and r.status == "open"
        and r.created_at is not None
        and r.created_at >= since
    ]
    return max(candidates, key=lambda r: r.created_at or now, default=None)


def handle_task(payload: dict[str, Any], ctx: WaContext) -> str:
    """One Cloud Tasks call. Returns an outcome code for the log."""
    kind = payload.get("kind")
    if kind == "message":
        return handle_message(WAMessage.model_validate(payload.get("message") or {}), ctx)
    if kind == "finalize":
        return finalize(str(payload.get("key", "")), ctx)
    return "unknown_kind"


def handle_message(message: WAMessage, ctx: WaContext, now: datetime | None = None) -> str:
    now = now or clock.now()
    store = ctx.store
    person = _person(store, message.sender)
    if person is None:
        _reply(ctx, message.sender, NOT_REGISTERED_TEXT)
        return "not_registered"
    text = message.text.strip()
    if message.kind in ("text", "media"):
        handled = _handle_command(message, person, ctx)
        if handled is not None:
            return handled
    if message.kind == "text":
        command = text.lower().strip(" .!?")
        if command in {"help", "/help", "menu"}:
            _reply(ctx, message.sender, HELP_TEXT)
            return "help"
        if command in {"my tasks", "my task", "/mytasks", "tugas saya"}:
            _reply(ctx, message.sender, person_tasks_text(ctx.services, person))
            return "my_tasks"
        prompt = parse_prompt(text)
        if prompt is not None:
            return _handle_prompt(message, prompt, ctx, now)
        answered = _deadline_answer(message, text, ctx, now)
        if answered is not None:
            return answered
    if message.kind == "other":
        _reply(ctx, message.sender, UNSUPPORTED_TEXT)
        return "unsupported"
    return _add_to_draft(message, ctx, now)


def _add_to_draft(message: WAMessage, ctx: WaContext, now: datetime) -> str:
    text = message.text.strip()
    prompt = parse_prompt(text) or ""  # a file whose caption is a PROMPT
    if prompt:
        text = ""

    def edit(rows: list[WaPending]) -> tuple[str, str | None]:
        if _seen(rows, message.id):
            return "duplicate", None
        media = [item.model_dump() for item in message.media]
        draft = _open_draft(rows, message.sender, now)
        if draft is not None:
            draft.text = "\n\n".join(part for part in (draft.text, text) if part)
            draft.prompt = "\n".join(part for part in (draft.prompt, prompt) if part)
            draft.media = json.dumps([*json.loads(draft.media or "[]"), *media])
            _add_id(draft, message.id)
            draft.updated_at = now
            return "draft_joined", None
        rows.append(
            WaPending(
                key=message.id,
                kind="draft",
                sender=message.sender,
                text=text,
                prompt=prompt,
                media=json.dumps(media),
                message_ids=message.id,
                status="open",
                created_at=now,
                updated_at=now,
            )
        )
        return "draft_opened", message.id

    outcome, key = ctx.store.edit_wa_pending(edit)
    if key is not None:
        _schedule_finalize(ctx, key)
        _reply(ctx, message.sender, ACK_TEXT)
    return outcome


def _schedule_finalize(ctx: WaContext, key: str) -> None:
    try:
        ctx.queue.enqueue({"kind": "finalize", "key": key}, delay_seconds=WAIT_SECONDS)
    except Exception as error:  # /jobs/wa-drafts logs the draft within minutes (D15)
        logger.error("wa_finalize_enqueue_failed", extra={"error_type": type(error).__name__})


def _handle_prompt(message: WAMessage, prompt: str, ctx: WaContext, now: datetime) -> str:
    def edit(rows: list[WaPending]) -> tuple[str, str]:
        if _seen(rows, message.id):
            return "duplicate", ""
        draft = _open_draft(rows, message.sender, now)
        if draft is not None:
            draft.prompt = "\n".join(part for part in (draft.prompt, prompt) if part)
            _add_id(draft, message.id)
            draft.updated_at = now
            return "prompt", ""
        recent = _open_confirm(rows, message.sender, now)
        if (
            recent
            and recent.created_at
            and recent.created_at >= now - timedelta(minutes=LATE_PROMPT_MINUTES)
        ):
            _add_id(recent, message.id)
            return "late_prompt", recent.task_id
        # The request may still be on its way (Cloud Tasks does not keep order): wait for it.
        rows.append(
            WaPending(
                key=message.id,
                kind="draft",
                sender=message.sender,
                prompt=prompt,
                media="[]",
                message_ids=message.id,
                status="open",
                created_at=now,
                updated_at=now,
            )
        )
        return "prompt_first", message.id

    outcome, value = ctx.store.edit_wa_pending(edit)
    if outcome == "prompt":
        _reply(ctx, message.sender, "PROMPT noted. I'll apply it when I log your request.")
    elif outcome == "prompt_first":
        _schedule_finalize(ctx, value)
        _reply(
            ctx,
            message.sender,
            f"PROMPT noted. Forward the request now; I'll log both in {WAIT_SECONDS} seconds.",
        )
    elif outcome == "late_prompt":
        _reply(ctx, message.sender, _apply_late_prompt(value, prompt, ctx, now))
    return outcome


def _apply_late_prompt(task_id: str, prompt: str, ctx: WaContext, now: datetime) -> str:
    """A PROMPT that arrived just after its request was logged: apply it to that task."""
    store = ctx.store
    store.load()
    task = next((t for t in store.tasks() if t.id == task_id), None)
    if task is None:
        return "I couldn't find the task to apply that PROMPT to."
    if task.status != "Proposed":
        return f"{task_id} is already approved; ask the GRC manager to change it."
    notes = "\n".join(part for part in (task.notes, f"Sender's PROMPT: {prompt}") if part)
    changes: dict[str, Any] = {"Notes": notes, "Last updated": now}
    reply = f"Added your PROMPT to {task_id} as a note."
    name = assignee(prompt)
    if name and match_staff_name(name, store.staff()) is None:
        reply = f'I couldn\'t find "{name}" in the Staff tab; I only added your PROMPT as a note.'
    elif name:
        current = RouteResult(
            owner=task.owner or None,
            division=task.division or None,
            catalog_id=task.catalog_id or None,
            rule=5,
            note=task.routing_note,
        )
        routed = sender_assignment(current, name, store.staff(), now.date(), task.due_date)
        flags = [f for f in task.flags if f != NEEDS_REVIEW_FLAG]
        changes.update(
            {
                "Owner": routed.owner,
                "Doer": routed.doer or routed.owner,
                "Division": routed.division or task.division,
                "Routing note": routed.note,
                "Flag": "; ".join(dict.fromkeys([*flags, *routed.flags])),
            }
        )
        reply = f"Updated {task_id}: proposed owner is now {routed.owner}."
    store.update_task(task_id, changes)
    if "Owner" in changes:
        _audit(store, task_id, "PROMPT applied", "Owner", task.owner, str(changes["Owner"]))
    else:
        _audit(store, task_id, "PROMPT applied", "Notes", "", "Sender's PROMPT added")
    return reply + " Still waiting for manager approval."


def _deadline_answer(message: WAMessage, text: str, ctx: WaContext, now: datetime) -> str | None:
    """YES or a date in reply to the deadline question; None if the message is something else."""
    if len(text) > 60:
        return None
    is_yes = text.lower().strip(" .!?") in _YES
    hits = [] if is_yes else find_dates(text, now.date())
    if hits:
        # Mostly a date ("20/11/2026", "no, Jumat 20 Nov"): not a new request that has a date in it.
        first = min(hits, key=lambda hit: hit.start)
        rest = (text[: first.start] + " " + text[first.end :]).split()
        if len(rest) > 3:
            hits = []
    if not is_yes and not hits:
        return None

    def edit(rows: list[WaPending]) -> str | None:
        confirm = _open_confirm(rows, message.sender, now)
        # During a 30-second window the message belongs to the draft, not to an older question.
        if confirm is None or _seen(rows, message.id) or _open_draft(rows, message.sender, now):
            return None
        confirm.status = "done"
        confirm.updated_at = now
        _add_id(confirm, message.id)
        return confirm.task_id

    task_id = ctx.store.edit_wa_pending(edit)
    if task_id is None:
        has_draft = _open_draft(ctx.store.wa_pending(), message.sender, now) is not None
        if is_yes and not has_draft:
            _reply(ctx, message.sender, NOTHING_TO_CONFIRM_TEXT)
            return "nothing_to_confirm"
        return None  # part of the open draft, or a date with no question open: a request
    store = ctx.store
    store.load()
    task = next((t for t in store.tasks() if t.id == task_id), None)
    if task is None:
        return "confirm_task_missing"
    flags = "; ".join(f for f in task.flags if f not in (DEADLINE_FLAG, DEFAULT_DUE_FLAG))
    if is_yes:
        store.update_task(task_id, {"Flag": flags, "Last updated": now})
        _audit(store, task_id, "Deadline confirmed", "Due date", "", _day(task.due_date))
        _reply(ctx, message.sender, f"Thanks. {task_id} due {_day(task.due_date)} confirmed.")
        return "confirmed"
    new_due = min(hits, key=lambda hit: hit.start).value
    store.update_task(task_id, {"Due date": new_due, "Flag": flags, "Last updated": now})
    _audit(store, task_id, "Deadline corrected", "Due date", _day(task.due_date), _day(new_due))
    _reply(ctx, message.sender, f"Updated {task_id}: due {_day(new_due)}.")
    return "date_changed"


def _day(value: Any) -> str:
    return f"{value:%a %d %b %Y}" if value else "not set"


def _audit(store: SheetStore, task_id: str, action: str, field: str, old: str, new: str) -> None:
    try:
        store.append_audit(
            [
                AuditEntry(
                    timestamp=clock.now(),
                    user=SERVICE_USER,
                    task_id=task_id,
                    action=action,
                    field=field,
                    old_value=old,
                    new_value=new,
                    reason="Sender on WhatsApp",
                )
            ]
        )
    except Exception as error:
        logger.error(
            "audit_write_failed", extra={"task_id": task_id, "error_type": type(error).__name__}
        )


def finalize(key: str, ctx: WaContext, now: datetime | None = None) -> str:
    """Turn the draft `key` into one request. Raises on failure, so Cloud Tasks retries."""
    now = now or clock.now()
    store = ctx.store

    def claim(rows: list[WaPending]) -> WaPending | None:
        draft = next((r for r in rows if r.key == key and r.kind == "draft"), None)
        if draft is None or draft.status == "done":
            return None
        stuck_since = now - timedelta(seconds=STUCK_SECONDS)
        if draft.status == "processing" and (draft.updated_at or now) > stuck_since:
            return None
        draft.status = "processing"
        draft.updated_at = now
        return draft.model_copy()

    draft = store.edit_wa_pending(claim)
    if draft is None:
        return "skipped"
    try:
        result = _log_draft(draft, ctx, now)
    except Exception:
        # Back to open: the Cloud Tasks retry or /jobs/wa-drafts logs it later (D15).
        store.edit_wa_pending(lambda rows: _set_status(rows, key, "open", now))
        raise
    store.edit_wa_pending(lambda rows: _set_status(rows, key, "done", now, result))
    return "logged" if result else "prompt_only"


def _set_status(
    rows: list[WaPending],
    key: str,
    status: Literal["open", "done"],
    now: datetime,
    task_id: str = "",
) -> None:
    for row in rows:
        if row.key == key and row.kind == "draft":
            row.status = status
            row.updated_at = now
            row.task_id = task_id or row.task_id


def _log_draft(draft: WaPending, ctx: WaContext, now: datetime) -> str:
    """process_request for a claimed draft; returns the task ID ("" for a PROMPT alone)."""
    store = ctx.store
    store.load()
    media = [WAMedia.model_validate(item) for item in json.loads(draft.media or "[]")]
    if not draft.text.strip() and not media:
        _reply(
            ctx,
            draft.sender,
            "I got a PROMPT but no request. Forward the request, then the "
            "PROMPT within 30 seconds.",
        )
        return ""
    person = _person(store, draft.sender)
    attachments, failed = _download(ctx, media)
    raw = RawRequest(
        source="WhatsApp",
        source_id=draft.key,
        received_at=draft.created_at or now,
        body_text=draft.text,
        sender_name=person.name if person else "",
        sender_email=person.email if person else "",
        attachments=attachments,
        submitted_by=(person.email or person.name) if person else "WhatsApp",
        assign_to=assignee(draft.prompt),
        sender_note=draft.prompt,
    )
    result = process_request(raw, ctx.services)
    if result.created:
        flags = "; ".join(dict.fromkeys([*result.flags, DEADLINE_FLAG]))
        store.update_task(result.task_id, {"Flag": flags})
        stored = [a for a in attachments if a.data]
        if stored and ctx.gmail is not None:  # kept in the bot mailbox (v1.4 decision F2)
            try:
                ctx.gmail.store_files(result.task_id, stored)
            except Exception as error:
                logger.error(
                    "wa_files_not_kept",
                    extra={"task_id": result.task_id, "error_type": type(error).__name__},
                )
    _reply(ctx, draft.sender, _logged_reply(result, failed), result.task_id)
    if result.created:
        store.edit_wa_pending(lambda rows: _ask_deadline(rows, draft.sender, result.task_id, now))
    return result.task_id


def _logged_reply(result: TaskResult, failed: list[str]) -> str:
    due = _day(result.due_date)
    if not result.created:
        question = ""
    elif DEFAULT_DUE_FLAG in result.flags:
        question = (
            f"\n\nNo deadline found, so I set {due}. Reply *YES* to keep it, or send the "
            "correct date (e.g. 20/11/2026)."
        )
    else:
        question = (
            f"\n\nDeadline: {due}. Reply *YES* if correct, or send the correct date "
            "(e.g. 20/11/2026)."
        )
    text = logged_text(result, failed).replace(
        "Please drop the file(s) in the Files link above.", "Please email the file(s) to the bot."
    )
    return text + question


def _ask_deadline(rows: list[WaPending], sender: str, task_id: str, now: datetime) -> None:
    for row in rows:  # only the newest question per sender is answerable
        if row.kind == "confirm" and row.sender == sender and row.status == "open":
            row.status = "done"
            row.updated_at = now
    rows.append(
        WaPending(
            key=f"confirm:{task_id}",
            kind="confirm",
            sender=sender,
            task_id=task_id,
            status="open",
            created_at=now,
            updated_at=now,
        )
    )


# --- Jobs -----------------------------------------------------------------------------------------


class WaDraftsRun(BaseModel):  # counts only
    finalized: int = 0
    failed: int = 0
    pruned: int = 0


def run_wa_drafts(ctx: WaContext, now: datetime | None = None) -> WaDraftsRun:
    """Safety net (D15): log drafts whose finalize task never ran; prune finished rows."""
    now = now or clock.now()
    run = WaDraftsRun()
    stale = now - timedelta(seconds=STALE_DRAFT_SECONDS)
    stuck = now - timedelta(seconds=STUCK_SECONDS)
    for row in ctx.store.wa_pending():
        if row.kind != "draft":
            continue
        overdue = (row.status == "open" and (row.created_at or now) <= stale) or (
            row.status == "processing" and (row.updated_at or now) <= stuck
        )
        if not overdue:
            continue
        try:
            if finalize(row.key, ctx, now) != "skipped":
                run.finalized += 1
        except Exception as error:
            run.failed += 1
            logger.error("wa_draft_failed", extra={"error_type": type(error).__name__})

    keep_after = now - timedelta(days=KEEP_DAYS)
    expired = now - timedelta(hours=CONFIRM_HOURS)

    def prune(rows: list[WaPending]) -> int:
        before = len(rows)
        rows[:] = [
            r
            for r in rows
            if not (r.status == "done" and (r.updated_at or now) < keep_after)
            and not (r.kind == "confirm" and r.status == "open" and (r.created_at or now) < expired)
        ]
        return before - len(rows)

    run.pruned = ctx.store.edit_wa_pending(prune)
    logger.info("wa_drafts_run", extra={f"n_{k}": v for k, v in run.model_dump().items()})
    return run


class WaRemindersRun(BaseModel):  # counts only
    sent: int = 0
    failed: int = 0
    due: bool = False


def _reminder_time(value: str) -> time | None:
    match = re.match(r"^\s*(\d{1,2})[:.](\d{2})", value or "")
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    return time(hour, minute) if hour < 24 and minute < 60 else None


def run_wa_reminders(
    store: SheetStore, wa: WhatsAppClient, sheet_url: str, now: datetime | None = None
) -> WaRemindersRun:
    """Weekdays, once a day at the first run after Config `Reminder time`: each person with a
    WhatsApp number and something overdue, due within 7 days or stale gets the template."""
    now = now or clock.now()
    run = WaRemindersRun()
    config = store.config()
    at = _reminder_time(config.reminder_time)
    if at is None:
        logger.warning("wa_reminders_bad_time")
        return run
    marker = f"wa-reminders:{now.date().isoformat()}"
    if now.weekday() >= 5 or now.time() < at or marker in store.failures():
        return run
    run.due = True
    open_tasks = [task for task in store.tasks() if task.id and task.status in OPEN_STATUSES]
    for person in store.staff():
        if not person.wa_id:
            continue
        overdue, due_soon, stale = personal_buckets(person, open_tasks, now, config)
        if not (overdue or due_soon or stale):
            continue
        params = [person.name, str(len(overdue)), str(len(due_soon)), str(len(stale)), sheet_url]
        try:
            wa.send_template(person.wa_id, REMINDER_TEMPLATE, TEMPLATE_LANGUAGE, params)
            run.sent += 1
        except Exception as error:
            run.failed += 1
            logger.error("wa_reminder_failed", extra={"error_type": type(error).__name__})
    store.upsert_failure(FailureRow(key=marker, count=run.sent, last_at=now))
    logger.info("wa_reminders_run", extra={"n_sent": run.sent, "n_failed": run.failed})
    return run
