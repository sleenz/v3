import logging

from fastapi.testclient import TestClient

from app.auth import require_chat, require_scheduler
from app.main import Runtime, app, get_runtime
from app.settings import Settings
from tests.fakes import (
    FakeChatClient,
    FakeDriveStore,
    FakeGmailClient,
    FakeSheetsBackend,
    FakeSheetStore,
)


def runtime(gmail=None, backend=None):
    return Runtime(
        settings=Settings(sheet_id="sheet-1", gcp_project="proj"),
        store=FakeSheetStore(backend or FakeSheetsBackend.from_fixtures()),
        drive=FakeDriveStore(),
        gmail=gmail or FakeGmailClient(),
        chat=FakeChatClient(),
    )


def client_with(rt):
    app.dependency_overrides[get_runtime] = lambda: rt
    app.dependency_overrides[require_scheduler] = lambda: None  # token checks: tests/test_auth.py
    app.dependency_overrides[require_chat] = lambda: None
    return TestClient(app)


def teardown_function():
    app.dependency_overrides.clear()


def test_health_endpoints_have_no_data():
    client = TestClient(app)
    for path in ("/health", "/healthz"):
        response = client.get(path)
        assert response.status_code == 200 and response.json() == {"status": "ok"}


def test_gmail_job(fixture_text):
    gmail = FakeGmailClient()
    gmail.add("m1", body_text=fixture_text("manpower_planning.txt"))
    gmail.add("m2", body_text="spam", sender_email="someone@elsewhere.example.net")
    response = client_with(runtime(gmail)).post("/jobs/gmail")
    assert response.status_code == 200
    assert response.json() == {
        "job": "gmail", "messages": 2, "created": 1, "duplicates": 0, "ignored": 1, "replies": 0,
        "failed": 0, "gave_up": 0, "ack_failed": 0, "commands": 0,
    }  # fmt: skip
    assert len(gmail.replies) == 1


def test_gmail_job_schema_error_is_500():
    backend = FakeSheetsBackend.from_fixtures()
    del backend.tabs["_failures"]
    response = client_with(runtime(backend=backend)).post("/jobs/gmail")
    assert response.status_code == 500
    assert response.json() == {"job": "gmail", "error": "schema_error"}


def test_schema_problems_are_logged(caplog):
    backend = FakeSheetsBackend.from_fixtures()
    del backend.tabs["_failures"]
    with caplog.at_level(logging.ERROR, logger="grc"):
        client_with(runtime(backend=backend)).post("/jobs/gmail")
    reasons = [r.reason for r in caplog.records if r.getMessage() == "schema_problem"]
    assert reasons == ["missing tab _failures"]


def test_gmail_job_auth_error_is_500(caplog):
    gmail = FakeGmailClient()
    gmail.add("m1", body_text="x")
    gmail.auth_broken = True
    with caplog.at_level(logging.ERROR, logger="grc"):
        response = client_with(runtime(gmail)).post("/jobs/gmail")
    assert response.status_code == 500
    assert response.json()["error"] == "gmail_auth_error"
    [record] = [r for r in caplog.records if r.getMessage() == "gmail_auth_error"]
    assert record.reason.startswith("RefreshError")


def test_changes_recurring_and_digest_endpoints():
    rt = runtime()
    client = client_with(rt)
    changes = client.post("/jobs/changes").json()
    assert changes["job"] == "changes" and changes["baseline"] is True
    assert client.post("/jobs/recurring").json()["job"] == "recurring"
    digest = client.post("/jobs/digest").json()
    assert digest == {"job": "digest", "personal_sent": 0, "manager_sent": False, "failed": 0}


def test_chat_endpoint(fixture_text):
    import json

    event = json.loads(fixture_text("chat_message.json"))
    response = client_with(runtime()).post("/chat", json=event)
    assert response.status_code == 200
    assert response.json()["text"].startswith("Logged as G-0001")


def test_chat_endpoint_when_sheet_is_broken(fixture_text):
    import json

    backend = FakeSheetsBackend.from_fixtures()
    del backend.tabs["_state"]
    event = json.loads(fixture_text("chat_message.json"))
    response = client_with(runtime(backend=backend)).post("/chat", json=event)
    assert response.status_code == 200
    assert "not available" in response.json()["text"]


# --- startup check and /health ----------------------------------------------------------------

FULL = {
    "gcp_project": "proj", "gcp_region": "asia-southeast2", "sheet_id": "sheet-1",
    "service_url": "https://svc", "scheduler_sa_email": "s@proj", "chat_project_number": "1",
    "gmail_refresh_token": "1//token", "oauth_client_secret": '{"installed": {}}',
    "gemini_api_key": "key", "shared_drive_folder_id": "folder-1",
}  # fmt: skip


def test_startup_check_names_missing_variables_and_never_their_values(caplog):
    from app import main as main_module

    settings = Settings(**{**FULL, "sheet_id": "", "gmail_refresh_token": ""})
    with caplog.at_level(logging.ERROR, logger="grc"):
        health = main_module.check_startup(settings, store_factory=lambda s: 1 / 0)
    assert health.missing == ["SHEET_ID", "GMAIL_REFRESH_TOKEN"] and health.sheet == []
    reasons = [r.reason for r in caplog.records if r.getMessage() == "startup_missing_setting"]
    assert reasons == ["SHEET_ID", "GMAIL_REFRESH_TOKEN"]


def test_startup_check_whatsapp_is_all_or_nothing():
    from app import main as main_module

    store = lambda s: FakeSheetStore(FakeSheetsBackend.from_fixtures())  # noqa: E731
    assert main_module.check_startup(Settings(**FULL), store).ok
    # Every deploy fills RUNTIME_SA_EMAIL and the queue name; that alone is not "WhatsApp on".
    deployed = Settings(**FULL, runtime_sa_email="run@proj", wa_tasks_queue="wa-process")
    assert main_module.check_startup(deployed, store).ok
    assert not deployed.wa_enabled
    partly = Settings(**FULL, wa_phone_number_id="PNID")
    assert "WA_ACCESS_TOKEN" in main_module.check_startup(partly, store).missing


def test_startup_check_reports_sheet_problems_and_config_needs():
    from app import main as main_module

    backend = FakeSheetsBackend.from_fixtures()
    del backend.tabs["_failures"]
    health = main_module.check_startup(Settings(**FULL), lambda s: FakeSheetStore(backend))
    assert health.sheet == ["missing tab _failures"]
    # The fixture sheet uses File storage = Drive; with the Gemini key provider both are needed.
    gemini = FakeSheetsBackend.from_fixtures()
    for row in gemini.tabs["Config"]:
        if row[0] == "AI provider":
            row[1] = "Gemini API key"
    bare = Settings(**{**FULL, "gemini_api_key": "", "shared_drive_folder_id": ""})
    missing = main_module.check_startup(bare, lambda s: FakeSheetStore(gemini)).missing
    assert missing == ["GEMINI_API_KEY", "SHARED_DRIVE_FOLDER_ID"]
    unreachable = main_module.check_startup(Settings(**FULL), lambda s: 1 / 0)
    assert unreachable.sheet == ["sheet not readable: ZeroDivisionError"]


def test_health_is_503_with_the_missing_names(monkeypatch):
    from app import main as main_module

    broken = main_module.Health(missing=["SHEET_ID"], sheet=[], checked_at=0.0)
    monkeypatch.setattr(main_module, "_health", broken)
    for path in ("/health", "/healthz"):
        response = TestClient(app).get(path)
        assert response.status_code == 503
        assert response.json() == {"status": "unhealthy", "missing": ["SHEET_ID"], "sheet": []}


def test_sheet_problems_are_rechecked_without_a_redeploy(monkeypatch):
    from app import main as main_module

    stale = main_module.Health(missing=[], sheet=["missing tab _failures"], checked_at=-1e9)
    monkeypatch.setattr(main_module, "_health", stale)
    fixed = main_module.Health(missing=[], sheet=[], checked_at=0.0)
    monkeypatch.setattr(main_module, "check_startup", lambda settings: fixed)
    assert TestClient(app).get("/health").status_code == 200


# --- instant email: Gmail push via Pub/Sub (DESIGN section 20) --------------------------------


def test_gmail_push_answers_at_once_then_reads_the_inbox(fixture_text):
    gmail = FakeGmailClient()
    gmail.add("m1", body_text=fixture_text("manpower_planning.txt"))
    rt = runtime(gmail)
    push = {"message": {"data": "eyJlbWFpbEFkZHJlc3MiOiAiYm90QGV4YW1wbGUuY29tIn0="}}
    response = client_with(rt).post("/jobs/gmail-push", json=push)
    assert response.status_code == 204
    # TestClient runs the background work before returning: the request is logged.
    assert len(gmail.replies) == 1
    assert "G-0001" in [t.id for t in rt.store.tasks()]


def test_gmail_push_with_nothing_new_does_not_read_the_sheet():
    backend = FakeSheetsBackend.from_fixtures()
    rt = runtime(backend=backend)
    assert client_with(rt).post("/jobs/gmail-push", json={}).status_code == 204
    assert backend.calls == []  # no Sheets call at all


def test_gmail_push_failure_is_logged_and_never_resent(caplog):
    gmail = FakeGmailClient()
    gmail.add("m1", body_text="x")
    gmail.auth_broken = True
    with caplog.at_level(logging.ERROR, logger="grc"):
        response = client_with(runtime(gmail)).post("/jobs/gmail-push", json={})
    assert response.status_code == 204  # Pub/Sub must not resend: the mail stays unread
    assert any(r.getMessage() == "gmail_auth_error" for r in caplog.records)


def test_gmail_watch_renews_the_push():
    gmail = FakeGmailClient()
    rt = runtime(gmail)
    rt.settings = Settings(sheet_id="sheet-1", gmail_push_topic="projects/p/topics/grc-gmail-push")
    body = client_with(rt).post("/jobs/gmail-watch").json()
    assert body == {"job": "gmail-watch", "expires": "2026-11-16T09:00:00+08:00"}
    assert gmail.watched == ["projects/p/topics/grc-gmail-push"]


def test_gmail_watch_without_a_topic_is_skipped():
    body = client_with(runtime()).post("/jobs/gmail-watch").json()
    assert body == {"job": "gmail-watch", "skipped": "GMAIL_PUSH_TOPIC not set"}


def test_gmail_watch_refused_by_google_is_500(caplog):
    class Refused(Exception):
        class resp:  # mimics googleapiclient's HttpError.resp
            status = 403

    gmail = FakeGmailClient()
    gmail.watch = lambda topic: (_ for _ in ()).throw(Refused())
    rt = runtime(gmail)
    rt.settings = Settings(sheet_id="sheet-1", gmail_push_topic="projects/p/topics/t")
    with caplog.at_level(logging.ERROR, logger="grc"):
        response = client_with(rt).post("/jobs/gmail-watch")
    assert response.status_code == 500
    [record] = [r for r in caplog.records if r.getMessage() == "gmail_watch_failed"]
    assert record.status == 403


def test_inbox_runs_never_overlap_and_a_late_push_runs_again():
    import threading

    from app.gmail import OneRunAtATime

    gate = OneRunAtATime()
    started, release = threading.Event(), threading.Event()
    calls = []

    def slow():
        calls.append("run")
        if len(calls) == 1:
            started.set()
            release.wait(5)
        return len(calls)

    worker = threading.Thread(target=lambda: calls.append(("first", gate.run(slow))))
    worker.start()
    started.wait(5)
    assert gate.run(lambda: "second") is None  # in progress: no second run at the same time
    release.set()
    worker.join(5)
    # The first caller went round once more for the mail that arrived during its run.
    assert calls == ["run", "run", ("first", 2)]
    assert gate.run(lambda: "free") == "free"
