import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest

from app import chat as chat_module
from app.chat import DEFERRED_TEXT, FAILED_TEXT, HELP_TEXT, allowed, handle_event
from app.extract.ai import NullProvider
from app.pipeline import Services
from tests.fakes import (
    FakeChatClient,
    FakeDriveStore,
    FakeGmailClient,
    FakeSheetsBackend,
    FakeSheetStore,
)


@pytest.fixture
def svc():
    store = FakeSheetStore(FakeSheetsBackend.from_fixtures())
    store.load()
    return Services(store=store, drive=FakeDriveStore(), ai=NullProvider())


@pytest.fixture
def message_event(fixture_text):
    return json.loads(fixture_text("chat_message.json"))


def with_text(event, text, attachments=None):
    event = json.loads(json.dumps(event))
    event["message"]["text"] = text
    event["message"]["argumentText"] = text
    event["message"]["attachment"] = attachments or []
    return event


def handle(event, svc, chat=None, gmail=None, **kwargs):
    return handle_event(event, svc, chat or FakeChatClient(), gmail or FakeGmailClient(), **kwargs)


def test_allowed_domain():
    assert allowed("a@example.com", "example.com")
    assert allowed("a@example.com", "@Example.com")
    assert not allowed("a@evil-example.com", "example.com")
    assert not allowed("a@example.com.evil.io", "example.com")
    assert not allowed("a@example.com", "")


def test_added_to_space(fixture_text, svc):
    reply = handle(json.loads(fixture_text("chat_added.json")), svc)
    assert reply == {"text": "Hi! " + HELP_TEXT}


def test_removed_from_space(svc):
    assert handle({"type": "REMOVED_FROM_SPACE"}, svc) == {}


def test_other_domain_rejected(message_event, svc):
    event = json.loads(json.dumps(message_event))
    event["user"]["email"] = "someone@gmail.com"
    reply = handle(event, svc)
    assert "only for" in reply["text"]
    assert [t for t in svc.store.tasks() if t.id.startswith("G-")] == []


def test_help(message_event, svc):
    assert handle(with_text(message_event, " Help "), svc) == {"text": HELP_TEXT}
    assert handle(with_text(message_event, ""), svc) == {"text": HELP_TEXT}


def test_log_request_with_attachments(message_event, svc):
    reply = handle(message_event, svc)["text"]
    assert reply.startswith("Logged as G-0001: Mohon kirim hasil stress test")
    assert "Proposed owner: Staff A" in reply
    assert "Due: 13 Nov 2026" in reply
    assert "Files: https://drive.google.com/drive/folders/" in reply
    assert "I could not attach Shared from Drive" in reply  # Drive file: fallback message
    assert reply.endswith("Waiting for manager approval.")
    [task] = [t for t in svc.store.tasks() if t.id.startswith("G-")]
    assert task.source == "Google Chat"
    assert task.source_id == "spaces/AAAA/messages/BBBB.CCCC"
    assert task.catalog_id == "RM-STRESS"
    assert task.start == date(2026, 10, 26)
    assert "@GRC Tracker" not in task.original_message  # argumentText drops the @mention
    files = svc.drive.only_folder()["files"]
    assert files["Format_Stress_Test.xlsx"] == b"xlsx-bytes"


def test_failed_download_asks_for_files_link(message_event, svc):
    reply = handle(message_event, svc, chat=FakeChatClient(files={}))["text"]
    assert "I could not attach Format_Stress_Test.xlsx, Shared from Drive" in reply
    assert reply.startswith("Logged as G-0001")


def test_chat_retry_of_same_message_is_not_duplicated(message_event, svc):
    handle(message_event, svc)
    reply = handle(message_event, svc)["text"]
    assert reply.startswith("Already logged as G-0001")
    assert len([t for t in svc.store.tasks() if t.id.startswith("G-")]) == 1


def test_needs_review_reply(message_event, svc):
    reply = handle(with_text(message_event, "Tolong cek printer"), svc)["text"]
    assert "Proposed owner: none – needs review" in reply
    assert "Needs review: no owner" in reply


def test_my_tasks(message_event, svc):
    handle(message_event, svc)
    task_id = "G-0001"
    svc.store.update_task(task_id, {"Status": "In Progress"})
    svc.store.load()
    reply = handle(with_text(message_event, "my tasks"), svc)["text"]
    assert reply.startswith("Staff A, your open tasks (1):")
    assert f"• {task_id} – " in reply and "due 13 Nov 2026, In Progress" in reply


def test_my_tasks_unknown_sender_and_none_open(message_event, svc):
    event = with_text(message_event, "My tasks")
    assert handle(event, svc)["text"] == "Staff A, you have no open tasks."
    event["user"]["email"] = "new.person@example.com"
    assert "can't find your email" in handle(event, svc)["text"]


def test_slow_processing_replies_at_once_and_emails_later(message_event, svc, monkeypatch):
    release = threading.Event()
    original = chat_module.process_request

    def slow(raw, services):
        release.wait(5)
        return original(raw, services)

    monkeypatch.setattr(chat_module, "process_request", slow)
    gmail = FakeGmailClient()
    executor = ThreadPoolExecutor(max_workers=1)
    reply = handle(message_event, svc, gmail=gmail, wait_seconds=0.05, submit=executor.submit)
    assert reply == {"text": DEFERRED_TEXT}
    assert gmail.sent == []
    release.set()
    executor.shutdown(wait=True)
    [(email, sender)] = gmail.sent
    assert email.to == ["staff.a@example.com"]
    assert email.subject.startswith("[GRC] G-0001 – ")
    assert "Waiting for manager approval." in email.text
    assert sender == "GRC Tracker"


def test_slow_failure_emails_the_sender(message_event, svc, monkeypatch):
    release = threading.Event()

    def broken(raw, services):
        release.wait(5)
        raise OSError("sheets down")

    monkeypatch.setattr(chat_module, "process_request", broken)
    gmail = FakeGmailClient()
    executor = ThreadPoolExecutor(max_workers=1)
    assert handle(message_event, svc, gmail=gmail, wait_seconds=0.05, submit=executor.submit) == {
        "text": DEFERRED_TEXT
    }
    release.set()
    executor.shutdown(wait=True)
    assert gmail.sent[0][0].subject == "[GRC] Your request could not be logged"


def test_fast_failure_replies_with_apology(message_event, svc, monkeypatch):
    def broken(raw, services):
        raise OSError("sheets down")

    monkeypatch.setattr(chat_module, "process_request", broken)
    assert handle(message_event, svc) == {"text": FAILED_TEXT}


def test_no_file_storage_skips_chat_downloads(message_event):
    from app.drive import NoFileStorage

    backend = FakeSheetsBackend.from_fixtures()
    for row in backend.tabs["Config"]:
        if row[0] == "File storage":
            row[1] = "None"
    store = FakeSheetStore(backend)
    store.load()
    services = Services(store=store, drive=NoFileStorage(), ai=NullProvider())
    chat = FakeChatClient(files={})  # any download attempt would fail and be reported
    reply = handle(message_event, services, chat=chat)["text"]
    assert reply.startswith("Logged as G-0001")
    assert "I could not attach" not in reply
    assert "Chat attachments not stored" in reply
    assert "Files:" in reply and "Not stored: Format_Stress_Test.xlsx" in reply
