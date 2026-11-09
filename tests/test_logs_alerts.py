import contextlib
import json
import logging
from datetime import timedelta

from app import alerts, clock
from app.logs import JsonFormatter
from app.main import Runtime
from tests.fakes import FakeDriveStore, FakeGmailClient, FakeSheetsBackend, FakeSheetStore

# --- JSON logs ---------------------------------------------------------------------------------


def format_record(**extra):
    record = logging.makeLogRecord(
        {"name": "grc.test", "levelname": "ERROR", "levelno": 40, "msg": "request_failed", **extra}
    )
    return json.loads(JsonFormatter().format(record))


def test_json_log_keeps_safe_fields():
    entry = format_record(
        task_id="G-0001", error_type="OSError", n_created=3, success=True, latency_ms=12
    )
    assert entry["severity"] == "ERROR" and entry["message"] == "request_failed"
    assert entry["task_id"] == "G-0001" and entry["error_type"] == "OSError"
    assert entry["n_created"] == 3 and entry["success"] is True and entry["latency_ms"] == 12


def test_json_log_drops_personal_text_even_if_passed():
    entry = format_record(
        body="Selamat siang 0812-0000-0000",
        email="fajar@example.co.id",
        sender_name="Fajar",
        subject="x",
    )
    dumped = json.dumps(entry)
    assert "0812" not in dumped and "fajar" not in dumped.lower() and "subject" not in entry


def test_json_log_exception_reduced_to_type():
    try:
        raise ValueError("Mohon kirim laporan ke fajar@example.co.id")
    except ValueError:
        import sys

        record = logging.makeLogRecord(
            {"msg": "x", "levelname": "ERROR", "exc_info": sys.exc_info()}
        )
    entry = json.loads(JsonFormatter().format(record))
    assert entry["error_type"] == "ValueError"
    assert "fajar" not in json.dumps(entry)


# --- alerts ------------------------------------------------------------------------------------


def store_with(**config):
    backend = FakeSheetsBackend.from_fixtures()
    for setting, value in config.items():
        backend.tabs["Config"].append([setting, value])
    store = FakeSheetStore(backend)
    store.load()
    return store


def setup_function():
    alerts._memory.clear()


def test_alert_goes_to_manager_and_builder_once_per_hour():
    store = store_with(**{"Builder email": "builder@example.com"})
    gmail = FakeGmailClient()
    assert alerts.send_alert("schema_error", ["Master is missing column(s): Files"], gmail, store)
    [(email, _sender)] = gmail.sent
    assert email.to == ["builder@example.com", "manager@example.com"]
    assert email.subject == "[GRC tracker] Alert: schema_error"
    assert "- Master is missing column(s): Files" in email.text
    assert not alerts.send_alert("schema_error", ["again"], gmail, store)
    assert alerts.send_alert("request_gave_up", ["1 request(s)"], gmail, store)  # other kind
    assert len(gmail.sent) == 2

    with clock.freeze(clock.now() + timedelta(minutes=61)):
        assert alerts.send_alert("schema_error", ["later"], gmail, store)
    assert len(gmail.sent) == 3


def test_throttle_survives_a_new_instance():
    store = store_with()
    alerts.send_alert("schema_error", ["x"], FakeGmailClient(), store)
    alerts._memory.clear()  # new instance after scale-to-zero
    reread = FakeSheetStore(store.backend)
    reread.load()
    assert not alerts.send_alert("schema_error", ["x"], FakeGmailClient(), reread)


def test_falls_back_to_memory_when_failures_tab_is_missing():
    backend = FakeSheetsBackend.from_fixtures()
    del backend.tabs["_failures"]
    store = FakeSheetStore(backend)
    with contextlib.suppress(Exception):
        store.load()
    gmail = FakeGmailClient()
    assert alerts.send_alert("schema_error", ["missing tab _failures"], gmail, store)
    assert not alerts.send_alert("schema_error", ["missing tab _failures"], gmail, store)
    assert gmail.sent[0][0].to == ["manager@example.com"]


def test_no_recipients_and_send_failure_never_raise():
    store = store_with()
    for row in store.backend.tabs["Config"]:
        if row[0] == "Manager email":
            row[1] = ""
    store.load()
    assert not alerts.send_alert("schema_error", ["x"], FakeGmailClient(), store)

    gmail = FakeGmailClient()
    gmail.auth_broken = True
    assert not alerts.send_alert("schema_error", ["x"], gmail, store_with())


def test_gmail_job_alerts_when_requests_give_up():
    from fastapi.testclient import TestClient

    from app.auth import require_scheduler
    from app.main import app, get_runtime
    from app.settings import Settings
    from tests.fakes import FakeChatClient

    backend = FakeSheetsBackend.from_fixtures()
    backend.tabs["_failures"].append(["m1", 2, "OSError", ""])
    gmail = FakeGmailClient()
    gmail.add("m1", body_text="x")
    drive = FakeDriveStore(fail_upload="original.txt")
    runtime = Runtime(
        settings=Settings(), store=FakeSheetStore(backend), drive=drive, gmail=gmail,
        chat=FakeChatClient(),
    )  # fmt: skip
    app.dependency_overrides[get_runtime] = lambda: runtime
    app.dependency_overrides[require_scheduler] = lambda: None
    try:
        body = TestClient(app).post("/jobs/gmail").json()
    finally:
        app.dependency_overrides.clear()
    assert body["gave_up"] == 1
    assert gmail.sent[-1][0].subject == "[GRC tracker] Alert: request_gave_up"
