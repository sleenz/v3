"""WhatsApp bot (DESIGN section 17, v1.3) with fakes: no Meta, no Cloud Tasks, no network."""

import base64
import hashlib
import hmac
import io
import json
import urllib.error
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import clock
from app import whatsapp as wa_module
from app.auth import require_scheduler, require_task
from app.extract.ai import NullProvider
from app.main import Runtime, WaLinks, app, get_runtime, get_wa_links
from app.models import RouteResult, StaffMember
from app.pipeline import Services
from app.route import match_staff_name, sender_assignment
from app.settings import Settings
from app.sheets import SchemaError
from app.whatsapp import (
    DEADLINE_FLAG,
    GraphWhatsApp,
    WaContext,
    WAMedia,
    WAMessage,
    WhatsAppError,
    assignee,
    finalize,
    handle_message,
    parse_prompt,
    parse_webhook,
    run_wa_drafts,
    run_wa_reminders,
    signature_ok,
    verify_challenge,
)
from tests.fakes import (
    FakeAIProvider,
    FakeChatClient,
    FakeDriveStore,
    FakeGmailClient,
    FakeQueue,
    FakeSheetsBackend,
    FakeSheetStore,
    FakeWhatsApp,
)

A = "85255500001"  # Staff A in tests/fixtures/staff.json (fake number)
P = "85255500016"  # Staff P
STRANGER = "85299999999"
REQUEST = "Mohon kirim laporan stress test, deadline 20 November 2026. Terima kasih."
AI_RESULT = {
    "title_en": "Submit the stress test report",
    "summary_en": "Send the stress test report by 20 Nov 2026.",
    "deliverables": ["Stress test report"],
    "deadline": "2026-11-20",
    "deadline_quote": "20 November 2026",
    "requester_org": None,
    "contacts": [],
    "catalog_id": "NONE",
    "language": "id",
}


def now() -> datetime:
    return clock.now()


def context(ai=None, media=None, backend=None) -> WaContext:
    backend = backend or FakeSheetsBackend.from_fixtures()
    for row in backend.tabs["Config"]:
        if row[0] == "File storage":
            row[1] = "None"  # the production default (v1.2 decision A)
        if row[0] == "AI provider" and ai is not None:
            row[1] = "Gemini API key"
    store = FakeSheetStore(backend)
    store.load()
    services = Services(store=store, drive=FakeDriveStore(), ai=ai or NullProvider())
    return WaContext(services=services, wa=FakeWhatsApp(media), queue=FakeQueue())


def text(message_id: str, body: str, sender: str = A) -> WAMessage:
    return WAMessage(id=message_id, sender=sender, received_at=now(), kind="text", text=body)


def pdf(message_id: str, caption: str = "", sender: str = A) -> WAMessage:
    media = [WAMedia(id=f"media-{message_id}", filename="Surat.pdf", mime="application/pdf")]
    return WAMessage(
        id=message_id, sender=sender, received_at=now(), kind="media", text=caption, media=media
    )


def pending(ctx: WaContext):
    """Drafts and deadline questions (not the "sent" rows that remember bot messages)."""
    return [row for row in ctx.store.wa_pending() if row.kind != "sent"]


def task(ctx: WaContext, task_id: str):
    ctx.store.load()
    return next(t for t in ctx.store.tasks() if t.id == task_id)


def logged(ctx: WaContext) -> str:
    [row] = [r for r in pending(ctx) if r.kind == "draft" and r.status == "done"]
    return row.task_id


# --- webhook ----------------------------------------------------------------------------------


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_signature_check():
    body = b'{"entry": []}'
    assert signature_ok("app-secret", body, sign("app-secret", body))
    assert not signature_ok("app-secret", body, sign("other", body))
    assert not signature_ok("app-secret", body + b" ", sign("app-secret", body))
    assert not signature_ok("app-secret", body, None)
    assert not signature_ok("", body, sign("", body))


def test_verify_challenge():
    good = {"hub.mode": "subscribe", "hub.verify_token": "tok", "hub.challenge": "42"}
    assert verify_challenge(good, "tok") == "42"
    assert verify_challenge({**good, "hub.verify_token": "nope"}, "tok") is None
    assert verify_challenge({**good, "hub.mode": "unsubscribe"}, "tok") is None
    assert verify_challenge(good, "") is None


def webhook_payload(messages, phone_number_id="PNID", statuses=()):
    value = {"metadata": {"phone_number_id": phone_number_id}, "messages": list(messages)}
    if statuses:
        value["statuses"] = list(statuses)
    return {"entry": [{"changes": [{"value": value}]}]}


def test_parse_webhook_kinds_and_numbers():
    payload = webhook_payload(
        [
            {
                "id": "w1",
                "from": "85255500001",
                "timestamp": "1793000000",
                "type": "text",
                "text": {"body": "hello"},
            },
            {
                "id": "w2",
                "from": "85255500001",
                "timestamp": "1793000001",
                "type": "document",
                "document": {
                    "id": "m2",
                    "filename": "Surat.pdf",
                    "mime_type": "application/pdf",
                    "caption": "see attached",
                },
            },
            {
                "id": "w3",
                "from": "85255500001",
                "timestamp": "1793000002",
                "type": "image",
                "image": {"id": "m3", "mime_type": "image/jpeg"},
            },
            {"id": "w4", "from": "85255500001", "timestamp": "1793000003", "type": "sticker"},
        ],
        statuses=[{"id": "w0", "status": "read"}],
    )
    first, document, image, sticker = parse_webhook(payload, "PNID")
    assert (first.kind, first.text, first.sender) == ("text", "hello", A)
    assert document.kind == "media" and document.text == "see attached"
    assert document.media == [WAMedia(id="m2", filename="Surat.pdf", mime="application/pdf")]
    assert image.media[0].filename == "photo.jpg"
    assert sticker.kind == "other"
    assert parse_webhook(payload, "ANOTHER-NUMBER") == []


# --- PROMPT and assignment --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "prompt", "name"),
    [
        ("PROMPT : Assign this to stesya.", "Assign this to stesya.", "stesya"),
        ("prompt: kasih ke Staff P ya", "kasih ke Staff P ya", "Staff P"),
        ("Prompt assign it to Staff A please", "assign it to Staff A please", "Staff A"),
        ("PROMPT: this is the ATMR report", "this is the ATMR report", None),
        ("Mohon kirim laporan", None, None),
    ],
)
def test_parse_prompt_and_assignee(message, prompt, name):
    assert parse_prompt(message) == prompt
    assert (assignee(prompt) if prompt else None) == name


def staff(*names: str, **extra) -> list[StaffMember]:
    return [StaffMember(name=n, main_division="Finance", **extra) for n in names]


def test_match_staff_name():
    people = staff("Stesya Putri", "Budi Santoso", "Budi Hartono")
    assert match_staff_name("stesya putri", people).name == "Stesya Putri"
    assert match_staff_name("Stesya", people).name == "Stesya Putri"  # unique first name
    assert match_staff_name("budi s", people).name == "Budi Santoso"  # unique start
    assert match_staff_name("Budi", people) is None  # ambiguous
    assert match_staff_name("Nobody", people) is None


def test_sender_assignment_overrides_the_rules_but_keeps_their_note():
    routed = RouteResult(
        owner=None, rule=5, note="Rule 5: no owner found", flags=["Needs review: no owner"]
    )
    people = staff("Stesya Putri")
    result = sender_assignment(routed, "stesya", people, date(2026, 10, 26), None)
    assert (result.owner, result.rule, result.division) == ("Stesya Putri", 0, "Finance")
    assert "Assigned by sender" in result.note and "Rule 5" in result.note
    assert result.flags == []
    missing = sender_assignment(routed, "nobody", people, date(2026, 10, 26), None)
    assert missing.owner is None and 'PROMPT: "nobody" not found in Staff' in missing.flags


def test_sender_assignment_flags_an_intern_leaving_before_the_deadline():
    routed = RouteResult(owner="Someone", rule=1, note="Rule 1")
    people = staff("Ina Intern", role="Intern", internship_end=date(2026, 11, 1))
    result = sender_assignment(routed, "Ina", people, date(2026, 10, 26), date(2026, 11, 20))
    assert result.owner == "Ina Intern"
    assert any(
        flag.startswith("Assigned by sender: Ina Intern internship ends") for flag in result.flags
    )


# --- message flow -------------------------------------------------------------------------------


def test_unregistered_number_gets_one_reply_and_nothing_is_stored():
    ctx = context()
    assert handle_message(text("w1", REQUEST, sender=STRANGER), ctx) == "not_registered"
    assert "isn't registered" in ctx.wa.last_text(STRANGER)
    assert pending(ctx) == [] and ctx.queue.tasks == []


def test_help_and_my_tasks():
    ctx = context()
    assert handle_message(text("w1", "help"), ctx) == "help"
    assert "PROMPT" in ctx.wa.last_text(A)
    assert handle_message(text("w2", "my tasks"), ctx) == "my_tasks"
    assert "Staff A" in ctx.wa.last_text(A)


def test_forward_then_prompt_then_log_with_the_senders_choice():
    ai = FakeAIProvider(AI_RESULT)
    ctx = context(ai=ai, media={"media-w2": b"%PDF-1.4 surat"})
    assert handle_message(text("w1", REQUEST), ctx) == "draft_opened"
    assert ctx.queue.tasks == [({"kind": "finalize", "key": "w1"}, wa_module.WAIT_SECONDS)]
    assert "30 seconds" in ctx.wa.last_text(A)
    assert handle_message(pdf("w2"), ctx) == "draft_joined"
    assert handle_message(text("w3", "PROMPT: Assign this to Staff P"), ctx) == "prompt"

    assert finalize("w1", ctx) == "logged"
    task_id = logged(ctx)
    row = task(ctx, task_id)
    assert (row.source, row.source_id, row.status) == ("WhatsApp", "w1", "Proposed")
    assert (row.owner, row.doer) == ("Staff P", "Staff P")
    assert row.routing_note.startswith("Assigned by sender (WhatsApp PROMPT) to Staff P")
    assert "Sender's PROMPT: Assign this to Staff P" in row.notes
    assert row.due_date == date(2026, 11, 20) and DEADLINE_FLAG in row.flags
    assert row.files == "In the GRC bot mailbox: Surat.pdf"
    # the AI got the PROMPT as instructions and the PDF as a document
    [call] = ai.calls
    assert "Sender's instructions: Assign this to Staff P" in call["text"]
    assert call["documents"] == ["Surat.pdf"]
    reply = ctx.wa.last_text(A)
    assert f"Logged as {task_id}" in reply and "Deadline: Fri 20 Nov 2026" in reply
    confirm = [r for r in pending(ctx) if r.kind == "confirm"]
    assert [(c.task_id, c.status) for c in confirm] == [(task_id, "open")]


def test_prompt_before_the_request_still_applies():
    ctx = context()
    assert handle_message(text("w1", "PROMPT: assign to Staff P"), ctx) == "prompt_first"
    assert handle_message(text("w2", REQUEST), ctx) == "draft_joined"
    finalize("w1", ctx)
    assert task(ctx, logged(ctx)).owner == "Staff P"


def test_pdf_with_a_prompt_caption():
    ctx = context()
    handle_message(text("w1", REQUEST), ctx)
    handle_message(pdf("w2", caption="PROMPT: assign to Staff P"), ctx)
    finalize("w1", ctx)
    row = task(ctx, logged(ctx))
    assert row.owner == "Staff P" and "PROMPT" not in row.original_message


def test_unknown_name_keeps_the_rules_and_flags_it():
    ctx = context()
    handle_message(text("w1", REQUEST), ctx)
    handle_message(text("w2", "PROMPT: assign to Nobody Here"), ctx)
    finalize("w1", ctx)
    row = task(ctx, logged(ctx))
    assert 'PROMPT: "Nobody Here" not found in Staff' in row.flags
    assert "Assigned by sender" not in row.routing_note


def test_prompt_alone_logs_nothing():
    ctx = context()
    handle_message(text("w1", "PROMPT: assign to Staff P"), ctx)
    assert finalize("w1", ctx) == "prompt_only"
    assert "no request" in ctx.wa.last_text(A)
    assert [t for t in ctx.store.tasks() if t.source == "WhatsApp"] == []


def test_late_prompt_updates_the_task_just_logged():
    ctx = context()
    handle_message(text("w1", REQUEST), ctx)
    finalize("w1", ctx)
    task_id = logged(ctx)
    later = now() + timedelta(minutes=2)
    outcome = handle_message(text("w2", "PROMPT: assign this to Staff P"), ctx, now=later)
    assert outcome == "late_prompt"
    assert task(ctx, task_id).owner == "Staff P"
    assert "proposed owner is now Staff P" in ctx.wa.last_text(A)
    backend = ctx.store.backend
    assert backend.column("AuditLog", "Task ID")[-1] == task_id
    assert backend.column("AuditLog", "Action")[-1] == "PROMPT applied"


def test_late_prompt_does_not_touch_an_approved_task():
    ctx = context()
    handle_message(text("w1", REQUEST), ctx)
    finalize("w1", ctx)
    task_id = logged(ctx)
    ctx.store.update_task(task_id, {"Status": "Not Started"})
    handle_message(text("w2", "PROMPT: assign to Staff P"), ctx, now=now() + timedelta(minutes=1))
    assert "already approved" in ctx.wa.last_text(A)
    assert task(ctx, task_id).owner != "Staff P"


def test_deadline_yes_clears_the_flag():
    ctx = context()
    handle_message(text("w1", REQUEST), ctx)
    finalize("w1", ctx)
    task_id = logged(ctx)
    later = now() + timedelta(minutes=3)
    assert handle_message(text("w2", "Ya betul"), ctx, now=later) == "confirmed"
    assert DEADLINE_FLAG not in task(ctx, task_id).flags
    assert "confirmed" in ctx.wa.last_text(A)
    assert all(r.status == "done" for r in pending(ctx))


def test_deadline_correction_changes_the_due_date():
    ctx = context()
    handle_message(text("w1", REQUEST), ctx)
    finalize("w1", ctx)
    task_id = logged(ctx)
    later = now() + timedelta(minutes=3)
    assert handle_message(text("w2", "no, 27/11/2026"), ctx, now=later) == "date_changed"
    row = task(ctx, task_id)
    assert row.due_date == date(2026, 11, 27) and DEADLINE_FLAG not in row.flags
    backend = ctx.store.backend
    last = [backend.column("AuditLog", h)[-1] for h in ("Action", "Old value", "New value")]
    assert last == ["Deadline corrected", "Fri 20 Nov 2026", "Fri 27 Nov 2026"]


def test_a_new_request_with_a_date_is_not_taken_as_a_deadline_answer():
    ctx = context()
    handle_message(text("w1", REQUEST), ctx)
    finalize("w1", ctx)
    later = now() + timedelta(minutes=3)
    outcome = handle_message(
        text("w2", "Laporan ATMR kuartal ini paling lambat 30/11/2026"), ctx, now=later
    )
    assert outcome == "draft_opened"


def test_yes_with_nothing_to_confirm():
    ctx = context()
    assert handle_message(text("w1", "ok"), ctx) == "nothing_to_confirm"
    assert pending(ctx) == []


def test_duplicate_message_is_ignored():
    ctx = context()
    handle_message(text("w1", REQUEST), ctx)
    assert handle_message(text("w1", REQUEST), ctx) == "duplicate"
    assert len(ctx.queue.tasks) == 1


def test_unsupported_message_type():
    ctx = context()
    sticker = WAMessage(id="w1", sender=A, received_at=now(), kind="other")
    assert handle_message(sticker, ctx) == "unsupported"
    assert pending(ctx) == []


def test_finalize_runs_once():
    ctx = context()
    handle_message(text("w1", REQUEST), ctx)
    assert finalize("w1", ctx) == "logged"
    assert finalize("w1", ctx) == "skipped"
    assert len([t for t in ctx.store.tasks() if t.source == "WhatsApp"]) == 1


def test_finalize_failure_reopens_the_draft_for_a_retry(monkeypatch):
    ctx = context()
    handle_message(text("w1", REQUEST), ctx)
    monkeypatch.setattr(wa_module, "process_request", lambda *a: (_ for _ in ()).throw(OSError()))
    with pytest.raises(OSError):
        finalize("w1", ctx)
    [draft] = pending(ctx)
    assert draft.status == "open"


def test_queue_failure_still_keeps_the_draft():
    ctx = context()
    ctx.queue.fail = True
    assert handle_message(text("w1", REQUEST), ctx) == "draft_opened"
    assert [r.status for r in pending(ctx)] == ["open"]


def test_missing_pending_tab_is_a_schema_error_but_email_still_loads():
    backend = FakeSheetsBackend.from_fixtures()
    del backend.tabs["_wa_pending"]
    ctx = context(backend=backend)  # load() does not need the tab
    with pytest.raises(SchemaError, match="missing tab _wa_pending"):
        handle_message(text("w1", REQUEST), ctx)


# --- jobs ---------------------------------------------------------------------------------------


def test_drafts_job_logs_a_draft_whose_task_never_ran_and_prunes_old_rows():
    ctx = context()
    handle_message(text("w1", REQUEST), ctx)
    run = run_wa_drafts(ctx, now=now() + timedelta(minutes=5))
    assert run.finalized == 1 and logged(ctx)
    run = run_wa_drafts(ctx, now=now() + timedelta(days=wa_module.KEEP_DAYS + 2))
    assert run.pruned >= 2 and ctx.store.wa_pending() == []  # draft, question, sent rows


def test_drafts_job_leaves_a_fresh_draft_alone():
    ctx = context()
    handle_message(text("w1", REQUEST), ctx)
    assert run_wa_drafts(ctx, now=now() + timedelta(seconds=10)).finalized == 0


def open_task_for(ctx, doer: str, due: date) -> None:
    from app.models import Task

    ctx.store.append_task(
        Task(
            title="Quarterly report",
            doer=doer,
            owner=doer,
            status="Not Started",
            due_date=due,
            source="Manual",
            source_id=f"manual-{doer}",
        )
    )


def test_reminders_once_a_day_after_reminder_time():
    ctx = context()
    open_task_for(ctx, "Staff A", date(2026, 10, 28))  # due within 7 days
    wa = ctx.wa
    early = now().replace(hour=8, minute=30)
    assert run_wa_reminders(ctx.store, wa, "https://sheet", now=early).due is False
    run = run_wa_reminders(ctx.store, wa, "https://sheet", now=now())
    assert (run.due, run.sent) == (True, 1)
    assert wa.templates == [
        (A, "grc_daily_reminder", "en", ["Staff A", "0", "1", "0", "https://sheet"])
    ]
    ctx.store.load()
    assert run_wa_reminders(ctx.store, wa, "https://sheet", now=now()).sent == 0  # once a day
    saturday = datetime(2026, 10, 31, 10, 0, tzinfo=clock.TZ)
    assert run_wa_reminders(ctx.store, wa, "https://sheet", now=saturday).due is False


# --- Graph API and Cloud Tasks clients ----------------------------------------------------------


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def test_graph_client_request_shapes():
    calls = []

    def urlopen(request, timeout):
        calls.append(request)
        if request.full_url.endswith("/media-1"):
            return FakeResponse(json.dumps({"url": "https://lookaside.example/abc"}).encode())
        if request.full_url == "https://lookaside.example/abc":
            return FakeResponse(b"%PDF")
        return FakeResponse(b"{}")

    from pydantic import SecretStr

    client = GraphWhatsApp("PNID", SecretStr("token-not-real"), "v99.0", urlopen=urlopen)
    client.send_text(A, "hi")
    client.send_template(A, "grc_daily_reminder", "en", ["Staff A", "1"])
    assert client.download_media("media-1") == b"%PDF"
    sent, template, info, data = calls
    assert sent.full_url == "https://graph.facebook.com/v99.0/PNID/messages"
    assert sent.get_header("Authorization") == "Bearer token-not-real"
    assert json.loads(sent.data)["text"]["body"] == "hi"
    body = json.loads(template.data)["template"]
    assert body["name"] == "grc_daily_reminder" and body["language"] == {"code": "en"}
    assert body["components"][0]["parameters"][1] == {"type": "text", "text": "1"}
    assert info.data is None and data.get_header("Authorization") == "Bearer token-not-real"


def test_graph_errors_carry_the_status_only():
    def urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "secret body", {}, None)

    from pydantic import SecretStr

    client = GraphWhatsApp("PNID", SecretStr("t"), "v99.0", urlopen=urlopen)
    with pytest.raises(WhatsAppError) as raised:
        client.send_text(A, "hi")
    assert raised.value.status_code == 401 and "secret" not in str(raised.value)


def test_cloud_tasks_queue_builds_an_authenticated_delayed_task():
    from unittest.mock import MagicMock

    queue = wa_module.CloudTasksQueue(
        "proj",
        "asia-southeast2",
        "wa-process",
        "https://svc/tasks/wa-process",
        "run@proj.iam",
        "https://svc",
    )
    service = MagicMock()
    queue._service = service
    queue.enqueue({"kind": "finalize", "key": "w1"}, delay_seconds=30)
    kwargs = service.projects().locations().queues().tasks().create.call_args.kwargs
    assert kwargs["parent"] == "projects/proj/locations/asia-southeast2/queues/wa-process"
    task = kwargs["body"]["task"]
    request = task["httpRequest"]
    assert json.loads(base64.b64decode(request["body"])) == {"kind": "finalize", "key": "w1"}
    assert request["oidcToken"] == {
        "serviceAccountEmail": "run@proj.iam",
        "audience": "https://svc",
    }
    assert task["scheduleTime"] == "2026-10-26T01:00:30Z"  # 09:00:30 HKT


# --- endpoints ----------------------------------------------------------------------------------

WA_SETTINGS = Settings(
    sheet_id="sheet-1",
    gcp_project="proj",
    wa_phone_number_id="PNID",
    wa_graph_version="v99.0",
    wa_verify_token="verify-me",
    wa_access_token="token",
    wa_app_secret="app-secret",
    runtime_sa_email="run@proj.iam.gserviceaccount.com",
)


def client(links: WaLinks, runtime=None):
    app.dependency_overrides[get_wa_links] = lambda: links
    if runtime is not None:
        app.dependency_overrides[get_runtime] = lambda: runtime
    app.dependency_overrides[require_task] = lambda: None
    app.dependency_overrides[require_scheduler] = lambda: None
    return TestClient(app)


def teardown_function():
    app.dependency_overrides.clear()


def test_webhook_verification_endpoint():
    http = client(WaLinks(WA_SETTINGS, FakeWhatsApp(), FakeQueue()))
    params = {"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "777"}
    response = http.get("/wa/webhook", params=params)
    assert response.status_code == 200 and response.text == "777"
    bad = http.get("/wa/webhook", params={**params, "hub.verify_token": "x"})
    assert bad.status_code == 403
    disabled = client(WaLinks(Settings(), None, None))
    assert disabled.get("/wa/webhook", params=params).status_code == 404


def test_webhook_queues_signed_messages_only():
    queue = FakeQueue()
    http = client(WaLinks(WA_SETTINGS, FakeWhatsApp(), queue))
    body = json.dumps(
        webhook_payload(
            [
                {
                    "id": "w1",
                    "from": A,
                    "timestamp": "1793000000",
                    "type": "text",
                    "text": {"body": "x"},
                }
            ]
        )
    ).encode()
    unsigned = http.post("/wa/webhook", content=body, headers={"x-hub-signature-256": "sha256=0"})
    assert unsigned.status_code == 401 and queue.tasks == []
    headers = {"x-hub-signature-256": sign("app-secret", body)}
    assert http.post("/wa/webhook", content=body, headers=headers).status_code == 200
    [(payload, delay)] = queue.tasks
    assert payload["kind"] == "message" and payload["message"]["id"] == "w1" and delay == 0
    queue.fail = True  # Meta must resend
    assert http.post("/wa/webhook", content=body, headers=headers).status_code == 500


def test_task_endpoint_runs_a_message():
    ctx = context()
    runtime = Runtime(
        settings=WA_SETTINGS,
        store=ctx.store,
        drive=FakeDriveStore(),
        gmail=FakeGmailClient(),
        chat=FakeChatClient(),
        wa=ctx.wa,
        queue=ctx.queue,
    )
    http = client(WaLinks(WA_SETTINGS, ctx.wa, ctx.queue), runtime)
    message = text("w1", REQUEST).model_dump(mode="json")
    response = http.post("/tasks/wa-process", json={"kind": "message", "message": message})
    assert response.json() == {"ok": True, "outcome": "draft_opened"}
    bad = http.post("/tasks/wa-process", json={"kind": "message", "message": {"id": 1}})
    assert bad.status_code == 200 and bad.json() == {"ok": False}  # not retried


def test_wa_jobs_skip_when_whatsapp_is_not_configured():
    ctx = context()
    runtime = Runtime(
        settings=Settings(),
        store=ctx.store,
        drive=FakeDriveStore(),
        gmail=FakeGmailClient(),
        chat=FakeChatClient(),
    )
    http = client(WaLinks(Settings(), None, None), runtime)
    assert "skipped" in http.post("/jobs/wa-reminders").json()
    assert "skipped" in http.post("/jobs/wa-drafts").json()


def test_logs_never_carry_numbers_or_message_text(caplog):
    import logging

    from app.logs import JsonFormatter

    ctx = context(ai=FakeAIProvider(AI_RESULT))
    with caplog.at_level(logging.DEBUG, logger="grc"):
        handle_message(text("w1", REQUEST), ctx)
        handle_message(text("w2", "PROMPT: assign to Staff P"), ctx)
        finalize("w1", ctx)
        handle_message(text("w3", "yes"), ctx)
        handle_message(text("w4", REQUEST, sender=STRANGER), ctx)
    formatted = "".join(JsonFormatter().format(record) for record in caplog.records)
    for secret in (A, STRANGER, "stress test", "Staff P", "PROMPT"):
        assert secret not in formatted
