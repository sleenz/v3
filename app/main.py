"""FastAPI routes. Each call builds a fresh Runtime, so every run starts with one sheet read.

/jobs/* needs a Cloud Scheduler OIDC token, /tasks/* a Cloud Tasks OIDC token and /chat a Google
Chat token (app/auth.py); /wa/webhook checks Meta's signature (app/whatsapp.py, v1.3);
/health and /healthz are open and return no data (Cloud Run reserves some paths ending in
"z", so /health is the one to use there; /healthz stays for DESIGN section 7).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import ValidationError

from app import clock
from app.alerts import send_alert
from app.auth import require_chat, require_scheduler, require_task
from app.chat import ChatClient, GoogleChatClient, handle_event
from app.drive import DriveStore, GoogleDriveStore, NoFileStorage
from app.extract.ai import provider_from_config
from app.gmail import INBOX_RUNS, GmailAuthError, GmailClient, GoogleGmailClient, process_inbox
from app.jobs import recurring_problems, run_changes, run_digest, run_recurring
from app.logs import setup_logging
from app.models import REVIEW_COLUMNS
from app.pipeline import Services
from app.settings import Settings, get_settings, missing_settings
from app.sheets import GoogleSheetsBackend, SchemaError, SheetStore
from app.whatsapp import (
    CloudTasksQueue,
    GraphWhatsApp,
    TaskQueue,
    WaContext,
    WhatsAppClient,
    handle_task,
    parse_webhook,
    run_wa_drafts,
    run_wa_reminders,
    signature_ok,
    verify_challenge,
)

logger = logging.getLogger("grc.main")


@dataclass
class Health:
    """The startup check: missing variable names and sheet problems (names only, no values)."""

    missing: list[str]
    sheet: list[str]
    checked_at: float
    warnings: list[str] = field(default_factory=list)  # reported, but /health stays 200

    @property
    def ok(self) -> bool:
        return not self.missing and not self.sheet


RECHECK_SECONDS = 60.0
_health: Health | None = None


def _sheet_store(settings: Settings) -> SheetStore:
    return SheetStore(GoogleSheetsBackend(settings.sheet_id))


def check_startup(
    settings: Settings, store_factory: Callable[[Settings], SheetStore] = _sheet_store
) -> Health:
    """Every required variable set, and the sheet has every required tab, column and setting.
    Never raises: a broken setup must still boot so /health can say what is wrong."""
    missing = missing_settings(settings)
    sheet: list[str] = []
    warnings: list[str] = []
    if "SHEET_ID" not in missing:
        try:
            store = store_factory(settings)
            store.load()
            config = store.config()
            if config.ai_provider == "Gemini API key" and not (
                settings.gemini_api_key.get_secret_value()
            ):
                missing.append("GEMINI_API_KEY")
            if config.file_storage == "Drive" and not settings.shared_drive_folder_id:
                missing.append("SHARED_DRIVE_FOLDER_ID")
            if settings.wa_enabled:
                store.wa_pending()
            warnings = (
                uncovered_divisions(store)
                + missing_review_columns(store)
                + recurring_warning(store)
            )
        except SchemaError as error:
            sheet = list(error.problems)
        except Exception as error:  # unreachable, not shared with the runtime account, ...
            sheet = [f"sheet not readable: {type(error).__name__}"]
    for name in missing:
        logger.error("startup_missing_setting", extra={"reason": name})
    for problem in sheet:
        logger.error("startup_sheet_problem", extra={"reason": problem})
    for warning in warnings:
        logger.warning("startup_warning", extra={"reason": warning})
    return Health(missing, sheet, time.monotonic(), warnings)


def recurring_warning(store: SheetStore) -> list[str]:
    """How many Recurring rows cannot be used. A count only: /health is public and its warnings
    are logged, and the details name people (the manager sees them in the digest and with the
    `recurring` command)."""
    count = len(recurring_problems(store, clock.today()))
    if not count:
        return []
    return [f"Recurring: {count} row(s) cannot be used (details: manager digest or `recurring`)"]


def missing_review_columns(store: SheetStore) -> list[str]:
    """The v1.4 Master columns the sheet lacks (optional, but review commands need them)."""
    header = store.master_header()
    missing = [column for column in REVIEW_COLUMNS if column not in header]
    return [f"Master is missing column(s): {', '.join(missing)}"] if missing else []


def uncovered_divisions(store: SheetStore) -> list[str]:
    """Divisions named in Catalog or LetterCodes that nobody in Staff covers (v1.4 R10): the
    names must match exactly, or routing rule 3 finds nobody."""
    covered = {d.casefold() for person in store.staff() for d in person.division_list}
    named = {e.division.strip() for e in store.catalog() if e.division.strip()}
    named |= {c.route_to_division.strip() for c in store.letter_codes() if c.route_to_division}
    return [
        f"nobody in Staff covers division {d}" for d in sorted(named) if d.casefold() not in covered
    ]


def current_health() -> Health | None:
    """The startup result; sheet problems are re-checked at most once a minute, so fixing the
    sheet needs no redeploy (missing variables do: they only change with a new revision)."""
    global _health
    health = _health
    stale = health is not None and time.monotonic() - health.checked_at > RECHECK_SECONDS
    if stale and health is not None and health.sheet and not health.missing:
        _health = health = check_startup(get_settings())
    return health


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    global _health
    setup_logging()
    _health = await run_in_threadpool(check_startup, get_settings())
    yield


app = FastAPI(
    title="grc-tracker", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
)


@dataclass
class Runtime:
    settings: Settings
    store: SheetStore
    drive: DriveStore
    gmail: GmailClient
    chat: ChatClient
    wa: WhatsAppClient | None = None  # v1.3; None when the WA_* settings are not set
    queue: TaskQueue | None = None

    def services(self) -> Services:
        config = self.store.config()
        ai = provider_from_config(
            config.ai_provider,
            config.ai_model,
            config.ai_region,
            self.settings.gcp_project,
            self.settings.gemini_api_key.get_secret_value(),
        )
        return Services(store=self.store, drive=self.drive, ai=ai)

    def wa_context(self) -> WaContext | None:
        if self.wa is None or self.queue is None:
            return None
        return WaContext(
            services=self.services(),
            wa=self.wa,
            queue=self.queue,
            gmail=self.gmail,
            sheet_url=self.settings.sheet_url,
        )


_gmail_client: GmailClient | None = None
_wa_links: tuple[WhatsAppClient, TaskQueue] | None = None


@dataclass
class WaLinks:
    """What the WhatsApp webhook needs: settings and the queue (no Sheets or Gmail)."""

    settings: Settings
    wa: WhatsAppClient | None
    queue: TaskQueue | None


def get_wa_links() -> WaLinks:
    global _wa_links
    settings = get_settings()
    if not settings.wa_enabled:
        return WaLinks(settings, None, None)
    if _wa_links is None:
        _wa_links = (
            GraphWhatsApp(
                settings.wa_phone_number_id, settings.wa_access_token, settings.wa_graph_version
            ),
            CloudTasksQueue(
                project=settings.gcp_project,
                region=settings.gcp_region,
                queue=settings.wa_tasks_queue or "wa-process",
                target_url=f"{settings.service_url}/tasks/wa-process",
                service_account_email=settings.runtime_sa_email,
                audience=settings.service_url,
            ),
        )
    return WaLinks(settings, *_wa_links)


WaLinksDep = Annotated[WaLinks, Depends(get_wa_links)]


def get_runtime() -> Runtime:
    """Real Google clients. Tests replace this with `app.dependency_overrides`."""
    global _gmail_client
    settings = get_settings()
    if _gmail_client is None:
        _gmail_client = GoogleGmailClient.from_settings(settings)
    links = get_wa_links()
    return Runtime(
        settings=settings,
        wa=links.wa,
        queue=links.queue,
        store=SheetStore(GoogleSheetsBackend(settings.sheet_id)),
        drive=(
            GoogleDriveStore(settings.shared_drive_folder_id)
            if settings.shared_drive_folder_id
            else NoFileStorage()
        ),
        gmail=_gmail_client,
        chat=GoogleChatClient(),
    )


RuntimeDep = Annotated[Runtime, Depends(get_runtime)]


def _log_schema_error(job: str, error: SchemaError) -> None:
    # Problems name only tabs, columns and settings (never cell values), so they are safe to log.
    # The log is the only place they show when Config itself is unreadable and no alert can go.
    logger.error("schema_error", extra={"job": job, "n_problems": len(error.problems)})
    for problem in error.problems:
        logger.error("schema_problem", extra={"job": job, "reason": problem})


def _job_error(job: str, error: Exception, runtime: Runtime) -> JSONResponse:
    if isinstance(error, SchemaError):
        _log_schema_error(job, error)
        send_alert("schema_error", error.problems, runtime.gmail, runtime.store)
        kind = "schema_error"
    else:
        # The mailbox itself is broken, so no email can go out: the log-based alert covers it.
        # str(error) is "RefreshError: <Google reason code>", never a token (gmail.py).
        logger.error("gmail_auth_error", extra={"job": job, "reason": str(error)})
        kind = "gmail_auth_error"
    return JSONResponse(status_code=500, content={"job": job, "error": kind})


jobs = APIRouter(prefix="/jobs", dependencies=[Depends(require_scheduler)])


@app.get("/health", response_model=None)
@app.get("/healthz", response_model=None)
def healthz() -> dict[str, str] | JSONResponse:
    health = current_health()
    if health is None or health.ok:
        if health is not None and health.warnings:
            return {"status": "ok", "warnings": health.warnings}
        return {"status": "ok"}
    content = {"status": "unhealthy", "missing": health.missing, "sheet": health.sheet}
    return JSONResponse(content, status_code=503)


def _gmail_run(
    runtime: Runtime, job: str, only_if_unread: bool = False
) -> dict[str, object] | JSONResponse:
    try:
        if only_if_unread and not runtime.gmail.unread_inbox(1):
            # Most pushes are the bot's own label changes: no sheet read when nothing is new.
            return {"job": job, "messages": 0}
        runtime.store.load()
        run = process_inbox(runtime.gmail, runtime.services(), sheet_url=runtime.settings.sheet_url)
    except (SchemaError, GmailAuthError) as error:
        return _job_error(job, error, runtime)
    if run.gave_up:
        details = [f"{run.gave_up} request(s) moved to GRC/Failed in this run"]
        send_alert("request_gave_up", details, runtime.gmail, runtime.store)
    return {"job": job, **run.model_dump()}


@jobs.post("/gmail", response_model=None)
def gmail_job(runtime: RuntimeDep) -> dict[str, object] | JSONResponse:
    result = INBOX_RUNS.run(lambda: _gmail_run(runtime, "gmail"))
    if result is None:
        return {"job": "gmail", "skipped": "a run is in progress; it will read the inbox again"}
    return result


def _push_run(runtime: Runtime) -> None:
    try:
        INBOX_RUNS.run(lambda: _gmail_run(runtime, "gmail-push", only_if_unread=True))
    except Exception as error:  # nothing is lost: the mail stays unread for the 10-minute job
        logger.error("gmail_push_failed", extra={"error_type": type(error).__name__})


@jobs.post("/gmail-push", status_code=204, response_model=None)
def gmail_push(runtime: RuntimeDep, background: BackgroundTasks) -> Response:
    """Pub/Sub push: Gmail says the Inbox changed (DESIGN section 20). Answered at once, so
    Pub/Sub does not resend; the inbox is read right after the answer. The note itself (the
    mailbox address and a history number) is not needed and not read."""
    background.add_task(_push_run, runtime)
    return Response(status_code=204)


@jobs.post("/gmail-watch", response_model=None)
def gmail_watch_job(runtime: RuntimeDep) -> dict[str, object] | JSONResponse:
    """Daily: renew Gmail's push to the topic (it lapses after 7 days)."""
    topic = runtime.settings.gmail_push_topic.strip()
    if not topic:
        return {"job": "gmail-watch", "skipped": "GMAIL_PUSH_TOPIC not set"}
    try:
        expires = runtime.gmail.watch(topic)
    except GmailAuthError as error:
        return _job_error("gmail-watch", error, runtime)
    except Exception as error:  # topic missing, or Gmail may not publish to it (org policy)
        status = getattr(getattr(error, "resp", None), "status", 0)
        logger.error(
            "gmail_watch_failed", extra={"error_type": type(error).__name__, "status": status}
        )
        return JSONResponse(
            status_code=500, content={"job": "gmail-watch", "error": type(error).__name__}
        )
    until = expires.isoformat() if expires else ""
    logger.info("gmail_watch", extra={"reason": until})
    return {"job": "gmail-watch", "expires": until}


@jobs.post("/changes", response_model=None)
def changes_job(runtime: RuntimeDep) -> dict[str, object] | JSONResponse:
    try:
        runtime.store.load()
        run = run_changes(runtime.store, runtime.gmail, runtime.settings.sheet_url)
    except (SchemaError, GmailAuthError) as error:
        return _job_error("changes", error, runtime)
    return {"job": "changes", **run.model_dump()}


@jobs.post("/recurring", response_model=None)
def recurring_job(runtime: RuntimeDep) -> dict[str, object] | JSONResponse:
    try:
        runtime.store.load()
        run = run_recurring(runtime.services())
    except SchemaError as error:
        return _job_error("recurring", error, runtime)
    return {"job": "recurring", **run.model_dump()}


@jobs.post("/digest", response_model=None)
def digest_job(runtime: RuntimeDep) -> dict[str, object] | JSONResponse:
    try:
        runtime.store.load()
        run = run_digest(runtime.store, runtime.gmail, runtime.settings.sheet_url)
    except (SchemaError, GmailAuthError) as error:
        return _job_error("digest", error, runtime)
    return {"job": "digest", **run.model_dump()}


@jobs.post("/wa-reminders", response_model=None)
def wa_reminders_job(runtime: RuntimeDep) -> dict[str, object] | JSONResponse:
    if runtime.wa is None:
        return {"job": "wa-reminders", "skipped": "WhatsApp not configured"}
    try:
        runtime.store.load()
        run = run_wa_reminders(runtime.store, runtime.wa, runtime.settings.sheet_url)
    except SchemaError as error:
        return _job_error("wa-reminders", error, runtime)
    return {"job": "wa-reminders", **run.model_dump()}


@jobs.post("/wa-drafts", response_model=None)
def wa_drafts_job(runtime: RuntimeDep) -> dict[str, object] | JSONResponse:
    try:
        runtime.store.load()
        context = runtime.wa_context()
        if context is None:
            return {"job": "wa-drafts", "skipped": "WhatsApp not configured"}
        run = run_wa_drafts(context)
    except SchemaError as error:
        return _job_error("wa-drafts", error, runtime)
    return {"job": "wa-drafts", **run.model_dump()}


app.include_router(jobs)


# --- WhatsApp (v1.3, DESIGN section 17) ----------------------------------------------------------


@app.get("/wa/webhook", response_model=None)
def wa_verify(request: Request, links: WaLinksDep) -> PlainTextResponse:
    if not links.settings.wa_enabled:
        return PlainTextResponse("not configured", status_code=404)
    challenge = verify_challenge(dict(request.query_params), links.settings.wa_verify_token)
    if challenge is None:
        logger.warning("wa_verify_refused")
        return PlainTextResponse("forbidden", status_code=403)
    return PlainTextResponse(challenge)


@app.post("/wa/webhook", response_model=None)
async def wa_webhook(request: Request, links: WaLinksDep) -> dict[str, object] | JSONResponse:
    """Check Meta's signature, queue each message, answer at once (Meta resends slow calls)."""
    settings = links.settings
    if not settings.wa_enabled or links.queue is None:
        return JSONResponse({"error": "not configured"}, status_code=404)
    body = await request.body()
    signature = request.headers.get("x-hub-signature-256")
    if not signature_ok(settings.wa_app_secret.get_secret_value(), body, signature):
        logger.warning("wa_bad_signature")
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        payload = json.loads(body)
    except ValueError:
        return {"ok": True}
    messages = parse_webhook(payload, settings.wa_phone_number_id)
    queue = links.queue
    try:
        for message in messages:
            task = {"kind": "message", "message": message.model_dump(mode="json")}
            await run_in_threadpool(queue.enqueue, task)
    except Exception as error:  # 500 makes Meta resend; the message IDs dedupe (D15)
        logger.error("wa_enqueue_failed", extra={"error_type": type(error).__name__})
        return JSONResponse({"error": "retry"}, status_code=500)
    logger.info("wa_webhook", extra={"n_messages": len(messages)})
    return {"ok": True}


tasks = APIRouter(prefix="/tasks", dependencies=[Depends(require_task)])


@tasks.post("/wa-process", response_model=None)
async def wa_process(request: Request, runtime: RuntimeDep) -> dict[str, object] | JSONResponse:
    payload = await request.json()
    return await run_in_threadpool(_handle_wa_task, payload, runtime)


def _handle_wa_task(
    payload: dict[str, object], runtime: Runtime
) -> dict[str, object] | JSONResponse:
    """Other errors raise: the 500 makes Cloud Tasks retry, and the drafts job is the backstop."""
    try:
        runtime.store.load()
        context = runtime.wa_context()
        if context is None:
            return {"ok": False, "skipped": "WhatsApp not configured"}
        outcome = handle_task(payload, context)
    except SchemaError as error:
        _log_schema_error("wa", error)
        return JSONResponse({"error": "schema_error"}, status_code=500)
    except ValidationError:  # a malformed task would fail on every retry
        logger.error("wa_bad_task")
        return {"ok": False}
    logger.info("wa_task", extra={"kind": str(payload.get("kind", "")), "reason": outcome})
    return {"ok": True, "outcome": outcome}


app.include_router(tasks)


@app.post("/chat", dependencies=[Depends(require_chat)])
async def chat_event(request: Request, runtime: RuntimeDep) -> dict[str, object]:
    event = await request.json()
    return await run_in_threadpool(_handle_chat, event, runtime)


def _handle_chat(event: dict[str, object], runtime: Runtime) -> dict[str, object]:
    try:
        runtime.store.load()
        return handle_event(event, runtime.services(), runtime.chat, runtime.gmail)
    except SchemaError as error:
        _log_schema_error("chat", error)
        return {"text": "The GRC tracker is not available right now. Please email the request."}
    except GmailAuthError as error:
        logger.error("gmail_auth_error", extra={"job": "chat", "reason": str(error)})
        return {"text": "The GRC tracker is not available right now. Please email the request."}
