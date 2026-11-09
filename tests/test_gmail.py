import base64
import email
import logging
import os
from datetime import datetime
from email.message import EmailMessage
from unittest.mock import MagicMock

import pytest

from app import clock
from app.extract.ai import NullProvider
from app.gmail import (
    FAILED_LABEL,
    IGNORED_LABEL,
    LOGGED_LABEL,
    REPLY_LABEL,
    GmailAuthError,
    GoogleGmailClient,
    OutgoingEmail,
    build_mime,
    is_automatic,
    parse_message,
    process_inbox,
)
from app.pipeline import Services
from app.sheets import SchemaError
from tests.fakes import FakeDriveStore, FakeGmailClient, FakeSheetsBackend, FakeSheetStore

READ_AND_ARCHIVED = {"INBOX", "UNREAD"}


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


# --- parse_message / build_mime ----------------------------------------------------------------


def gmail_json(**header_overrides):
    headers = {
        "From": "=?UTF-8?B?R1JDIE1hbmFnZXI=?= <Mgr@Example.com>",
        "Subject": "Fwd: Data request",
        "Message-ID": "<abc@mail.example.com>",
        "References": "<older@mail.example.com>",
        **header_overrides,
    }
    return {
        "id": "m1",
        "threadId": "t1",
        "internalDate": str(int(datetime(2026, 10, 26, 3, 0, tzinfo=clock.TZ).timestamp() * 1000)),
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [{"name": k, "value": v} for k, v in headers.items()],
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {
                            "mimeType": "text/plain",
                            "headers": [
                                {
                                    "name": "Content-Type",
                                    "value": 'text/plain; charset="ISO-8859-1"',
                                }
                            ],
                            "body": {"data": b64("Caf\xe9 data".encode("latin-1"))},
                        },
                        {"mimeType": "text/html", "body": {"data": b64(b"<p>Cafe</p>")}},
                    ],
                },
                {
                    "mimeType": "application/pdf",
                    "filename": "Form.pdf",
                    "body": {"attachmentId": "att-1", "size": 3},
                },
                {"mimeType": "image/png", "filename": "logo.png", "body": {"data": b64(b"png")}},
            ],
        },
    }


def test_parse_message():
    fetched = []

    def fetch(message_id, attachment_id):
        fetched.append((message_id, attachment_id))
        return b"pdf"

    message = parse_message(gmail_json(), fetch)
    assert (message.id, message.thread_id) == ("m1", "t1")
    assert (message.sender_name, message.sender_email) == ("GRC Manager", "mgr@example.com")
    assert message.subject == "Fwd: Data request"
    assert message.body_text == "Café data"
    assert message.body_html == "<p>Cafe</p>"
    assert [(a.filename, a.data) for a in message.attachments] == [
        ("Form.pdf", b"pdf"),
        ("logo.png", b"png"),
    ]
    assert fetched == [("m1", "att-1")]
    assert message.received_at == datetime(2026, 10, 26, 3, 0, tzinfo=clock.TZ)
    assert message.rfc_message_id == "<abc@mail.example.com>"
    assert message.references == "<older@mail.example.com>"
    assert message.automatic is False


@pytest.mark.parametrize(
    ("headers", "sender", "automatic"),
    [
        ({"auto-submitted": "auto-replied"}, "a@example.com", True),
        ({"auto-submitted": "no"}, "a@example.com", False),
        ({"x-autoreply": "yes"}, "a@example.com", True),
        ({"precedence": "bulk"}, "a@example.com", True),
        ({}, "mailer-daemon@example.com", True),
        ({}, "noreply@example.com", True),
        ({}, "a@example.com", False),
    ],
)
def test_is_automatic(headers, sender, automatic):
    assert is_automatic(headers, sender) is automatic


def test_build_mime_reply_headers():
    out = OutgoingEmail(to=["a@example.com"], subject="Re: x", text="Hi", html="<p>Hi</p>")
    raw = build_mime(out, "grc-bot@example.com", "GRC Tracker", "<m@x>", "<old@x>")
    parsed = email.message_from_bytes(base64.urlsafe_b64decode(raw))
    assert parsed["From"] == "GRC Tracker <grc-bot@example.com>"
    assert parsed["In-Reply-To"] == "<m@x>"
    assert parsed["References"] == "<old@x> <m@x>"
    assert [part.get_content_type() for part in parsed.get_payload()] == ["text/plain", "text/html"]


# --- process_inbox -----------------------------------------------------------------------------


def services(backend=None):
    store = FakeSheetStore(backend or FakeSheetsBackend.from_fixtures())
    store.load()
    return Services(store=store, drive=FakeDriveStore(), ai=NullProvider())


def g_tasks(svc):
    return [task for task in svc.store.tasks() if task.id.startswith("G-")]


def test_allowed_sender_is_logged_acknowledged_in_thread_then_archived(fixture_text):
    gmail = FakeGmailClient()
    gmail.add("m1", body_text=fixture_text("manpower_planning.txt"), subject="Fwd: Manpower")
    svc = services()
    run = process_inbox(gmail, svc)
    assert (run.messages, run.created, run.ignored, run.failed) == (1, 1, 0, 0)

    [task] = g_tasks(svc)
    assert task.source_id == "m1" and task.source == "Email"
    assert task.notes == "Manager note: Tolong dicek ya"  # forward unwrap still holds
    assert task.requester == "BRI"

    [(original_id, ack, sender_name)] = gmail.replies
    assert original_id == "m1"
    assert ack.to == ["grc-manager@example.com"]  # the person who emailed the bot
    assert ack.subject == "Re: Fwd: Manpower"
    assert ack.text.startswith(f"Logged as {task.id}: ")
    for expected in ("Proposed owner: Staff F", "Due: 06 Nov 2026", "Weekday mismatch", "Files: "):
        assert expected in ack.text
    assert sender_name == "GRC Tracker"
    assert gmail.labels("m1") == {LOGGED_LABEL}  # read and archived


@pytest.mark.parametrize(
    "sender",
    ["outsider@other.example.net", "grc-manager@example.co", "someone@sub.example.com"],
)
def test_sender_not_allowed_is_ignored(sender):
    gmail = FakeGmailClient()
    gmail.add("m1", body_text="Mohon kirim laporan ATMR, deadline 15/11/2026.", sender_email=sender)
    svc = services()
    run = process_inbox(gmail, svc)
    assert (run.ignored, run.created) == (1, 0)
    assert gmail.labels("m1") == {"INBOX", IGNORED_LABEL}  # marked read, kept in inbox
    assert g_tasks(svc) == [] and gmail.replies == []
    assert svc.drive.folders == {}


def test_allowed_domain_and_exact_address():
    gmail = FakeGmailClient()
    gmail.add("m1", body_text="Tolong cek printer", sender_email="Staff.X@Example.com")
    gmail.add("m2", body_text="Tolong cek printer lagi", sender_email="grc-manager@example.com")
    assert process_inbox(gmail, services()).created == 2


def test_auto_reply_and_own_messages_are_ignored(caplog):
    gmail = FakeGmailClient()
    gmail.add("m1", body_text="Out of office until Monday", automatic=True)
    gmail.add("m2", body_text="copy to self", sender_email="grc-bot@example.com")
    gmail.add("m3", body_text="Tolong cek", sender_email="outsider@other.example.net")
    svc = services()
    with caplog.at_level(logging.INFO, logger="grc"):
        run = process_inbox(gmail, svc)
    assert run.ignored == 3 and g_tasks(svc) == []
    reasons = [r.reason for r in caplog.records if r.getMessage() == "message_ignored"]
    assert sorted(reasons) == ["automatic", "sender_not_allowed", "sent_by_bot_mailbox"]


def test_reply_in_a_logged_thread_goes_to_notes_not_a_new_task():
    gmail = FakeGmailClient()
    gmail.add("m1", body_text="Laporan stress test, deadline 20 November 2026.", thread_id="t")
    svc = services()
    process_inbox(gmail, svc)
    gmail.add(
        "m2",
        thread_id="t",
        body_text="Terima kasih, noted.\n\nOn Mon wrote:\n> old",
        sender_email="staff.a@example.com",
        received_at=datetime(2026, 10, 26, 11, 15),
    )
    run = process_inbox(gmail, svc)
    assert (run.replies, run.created) == (1, 0)
    [task] = g_tasks(svc)
    assert task.notes.endswith(
        "Reply from staff.a@example.com on 26 Oct 2026 11:15: Terima kasih, noted."
    )
    assert "old" not in task.notes  # the quoted chain is dropped
    assert gmail.labels("m2") == {REPLY_LABEL}
    assert len(gmail.replies) == 1  # no acknowledgement for the reply
    audit = svc.store.backend.tabs["AuditLog"]
    assert audit[-1][audit[0].index("Action")] == "'Reply noted"


def test_nothing_is_archived_or_acknowledged_when_the_row_write_fails():
    gmail = FakeGmailClient()
    gmail.add("m1", body_text="Laporan stress test, deadline 20 November 2026.")
    svc = services()
    svc.store.append_task = MagicMock(side_effect=OSError("sheets down"))
    run = process_inbox(gmail, svc)
    assert run.failed == 1
    assert gmail.labels("m1") == {"INBOX", "UNREAD"}
    assert gmail.replies == []


def test_label_failure_is_retried_without_a_second_row(fixture_text):
    gmail = FakeGmailClient()
    gmail.add("m1", body_text=fixture_text("manpower_planning.txt"))
    svc = services()
    gmail.fail_labelling = True
    assert process_inbox(gmail, svc).created == 1
    assert gmail.labels("m1") == {"INBOX", "UNREAD"}

    gmail.fail_labelling = False
    run = process_inbox(gmail, svc)
    assert (run.created, run.duplicates) == (0, 1)
    assert gmail.labels("m1") == {LOGGED_LABEL}
    assert len(g_tasks(svc)) == 1
    assert gmail.replies[-1][1].text.startswith("Already logged as G-0001")


def test_acknowledgement_failure_still_archives():
    gmail = FakeGmailClient()
    gmail.add("m1", body_text="Laporan stress test, deadline 20 November 2026.")
    gmail.fail_reply = True
    run = process_inbox(gmail, services())
    assert (run.created, run.ack_failed) == (1, 1)
    assert gmail.labels("m1") == {LOGGED_LABEL}


def test_failure_stays_unread_then_gives_up_after_three():
    gmail = FakeGmailClient()
    gmail.add("m1", body_text="Mohon kirim laporan.")
    svc = services()
    svc.drive.fail_upload = "original.txt"
    for attempt in (1, 2):
        run = process_inbox(gmail, svc)
        assert run.failed == 1 and run.gave_up == 0
        assert gmail.labels("m1") == {"INBOX", "UNREAD"}
        assert svc.store.failures()["m1"].count == attempt
    run = process_inbox(gmail, svc)
    assert run.gave_up == 1
    assert gmail.labels("m1") == {"INBOX", FAILED_LABEL}
    assert g_tasks(svc) == []


def test_one_bad_message_does_not_stop_the_others(fixture_text):
    gmail = FakeGmailClient()
    gmail.add("m1", body_text="x")
    gmail.add("m2", body_text=fixture_text("printer.txt"))
    gmail.fail_reading.add("m1")
    svc = services()
    run = process_inbox(gmail, svc)
    assert (run.failed, run.created) == (1, 1)


def test_auth_failure_stops_and_leaves_messages_unread():
    gmail = FakeGmailClient()
    gmail.add("m1", body_text="x")
    gmail.auth_broken = True
    with pytest.raises(GmailAuthError):
        process_inbox(gmail, services())
    gmail.auth_broken = False
    assert gmail.labels("m1") == {"INBOX", "UNREAD"}


def test_schema_error_stops_the_run(monkeypatch):
    gmail = FakeGmailClient()
    gmail.add("m1", body_text="x")

    def broken(*args, **kwargs):
        raise SchemaError(["Master is missing column(s): Source ID"])

    monkeypatch.setattr("app.gmail.process_request", broken)
    with pytest.raises(SchemaError):
        process_inbox(gmail, services())
    assert gmail.labels("m1") == {"INBOX", "UNREAD"}


def test_read_or_archived_messages_are_not_processed_and_limit_is_20():
    gmail = FakeGmailClient()
    gmail.add("read", body_text="x", labels=("INBOX",))
    gmail.add("archived", body_text="x", labels=("UNREAD",))
    for i in range(25):
        gmail.add(f"m{i}", body_text="Tolong cek printer")
    run = process_inbox(gmail, services())
    assert run.messages == 20
    assert gmail.labels("read") == {"INBOX"}


def test_empty_allowed_senders_ignores_everyone(caplog):
    backend = FakeSheetsBackend.from_fixtures()
    for row in backend.tabs["Config"]:
        if row[0] == "Allowed senders":
            row[1] = ""
    gmail = FakeGmailClient()
    gmail.add("m1", body_text="Tolong cek printer")
    with caplog.at_level(logging.WARNING, logger="grc"):
        assert process_inbox(gmail, services(backend)).ignored == 1
    assert "no_allowed_senders" in [r.getMessage() for r in caplog.records]


# --- GoogleGmailClient (API client mocked) -----------------------------------------------------


def google_client():
    client = GoogleGmailClient(credentials=object())
    users = MagicMock()
    client._local.service = MagicMock()
    client._local.service.users.return_value = users
    users.labels.return_value.list.return_value.execute.return_value = {
        "labels": [{"name": LOGGED_LABEL, "id": "L1"}]
    }
    users.labels.return_value.create.return_value.execute.return_value = {"id": "L2"}
    users.getProfile.return_value.execute.return_value = {"emailAddress": "GRC-Bot@example.com"}
    return client, users


def test_unread_inbox_query_and_oldest_first():
    client, users = google_client()
    users.messages.return_value.list.return_value.execute.return_value = {
        "messages": [{"id": "new"}, {"id": "old"}]
    }
    assert client.unread_inbox(20) == ["old", "new"]
    kwargs = users.messages.return_value.list.call_args.kwargs
    assert kwargs["labelIds"] == ["INBOX", "UNREAD"] and kwargs["maxResults"] == 20


def test_modify_uses_system_labels_and_creates_missing_ones():
    client, users = google_client()
    client.modify("m1", ["GRC/Ignored"], ["UNREAD"])
    client.modify("m2", ["GRC/Ignored"], ["UNREAD", "INBOX"])
    users.labels.return_value.create.assert_called_once()
    body = users.messages.return_value.modify.call_args.kwargs["body"]
    assert body == {"addLabelIds": ["L2"], "removeLabelIds": ["UNREAD", "INBOX"]}


def test_logged_message_ids_in_thread():
    client, users = google_client()
    users.threads.return_value.get.return_value.execute.return_value = {
        "messages": [{"id": "a", "labelIds": ["L1"]}, {"id": "b", "labelIds": ["INBOX"]}]
    }
    assert client.logged_message_ids("t1") == ["a"]


def test_reply_is_sent_in_the_thread_with_threading_headers():
    client, users = google_client()
    original = parse_message(gmail_json(), lambda *a: b"")
    out = OutgoingEmail(to=["mgr@example.com"], subject="Re: x", text="t", html="<p>t</p>")
    client.reply(original, out, "GRC Tracker")
    client.send(out, "GRC Tracker")
    users.getProfile.assert_called_once()
    first = users.messages.return_value.send.call_args_list[0].kwargs["body"]
    assert first["threadId"] == "t1"
    raw = email.message_from_bytes(base64.urlsafe_b64decode(first["raw"]))
    assert raw["In-Reply-To"] == "<abc@mail.example.com>"
    assert raw["From"] == "GRC Tracker <grc-bot@example.com>"
    assert "threadId" not in users.messages.return_value.send.call_args_list[1].kwargs["body"]


def test_refresh_error_becomes_gmail_auth_error():
    from google.auth.exceptions import RefreshError

    client, users = google_client()
    users.getProfile.return_value.execute.side_effect = RefreshError("invalid_grant")
    with pytest.raises(GmailAuthError):
        client.address()


# --- A real MIME email with binary attachments, through the real client code -------------------

MINIMAL_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


def _gmail_json_from_mime(mime_message, stored):
    """MIME -> Gmail API `format=full` JSON; attachment bodies by attachmentId, like Gmail."""

    def part(node):
        out = {
            "mimeType": node.get_content_type(),
            "filename": node.get_filename() or "",
            "headers": [{"name": k, "value": str(v)} for k, v in node.items()],
        }
        if node.is_multipart():
            out["body"] = {"size": 0}
            out["parts"] = [part(child) for child in node.iter_parts()]
            return out
        data = node.get_payload(decode=True) or b""
        if out["filename"]:
            key = f"att-{len(stored) + 1}"
            stored[key] = data
            out["body"] = {"attachmentId": key, "size": len(data)}
        else:
            out["body"] = {"data": b64(data), "size": len(data)}
        return out

    return {"id": "m-mime", "threadId": "t-mime", "internalDate": "1793000000000",
            "payload": part(mime_message)}  # fmt: skip


def test_binary_attachments_arrive_byte_identical(fixture_text):
    files = {
        "Laporan Q3 – Ringkasan ✓.pdf": ("application/pdf", MINIMAL_PDF),
        "data.bin": ("application/octet-stream", os.urandom(50_000)),
    }
    mail = EmailMessage()
    mail["From"] = "GRC Manager <grc-manager@example.com>"
    mail["Subject"] = "Fwd: Manpower Planning"
    mail["Message-ID"] = "<mime@mail.example.com>"
    mail.set_content(fixture_text("manpower_planning.txt"))
    for name, (mime, data) in files.items():
        maintype, subtype = mime.split("/")
        mail.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    stored: dict[str, bytes] = {}
    api_message = _gmail_json_from_mime(mail, stored)

    client, users = google_client()
    users.messages.return_value.list.return_value.execute.return_value = {
        "messages": [{"id": "m-mime"}]
    }
    users.messages.return_value.get.return_value.execute.return_value = api_message
    users.threads.return_value.get.return_value.execute.return_value = {
        "messages": [{"id": "m-mime", "labelIds": ["INBOX", "UNREAD"]}]
    }

    def attachment(userId, messageId, id):
        request = MagicMock()
        request.execute.return_value = {"data": b64(stored[id])}
        return request

    users.messages.return_value.attachments.return_value.get.side_effect = attachment
    svc = services()
    run = process_inbox(client, svc)
    assert run.created == 1
    saved = svc.drive.only_folder()["files"]
    for name, (_, data) in files.items():
        assert saved[name] == data
    [task] = g_tasks(svc)
    assert task.original_message == fixture_text("manpower_planning.txt")
    sent = users.messages.return_value.send.call_args.kwargs["body"]
    assert sent["threadId"] == "t-mime"  # acknowledgement in the same thread
    modify = users.messages.return_value.modify.call_args.kwargs
    assert modify["id"] == "m-mime"
    assert modify["body"]["removeLabelIds"] == ["UNREAD", "INBOX"]


def test_refresh_error_reason_is_kept_without_secrets():
    from google.auth.exceptions import RefreshError

    client, users = google_client()
    users.getProfile.return_value.execute.side_effect = RefreshError(
        "invalid_grant: Token has been expired or revoked.", {"error": "invalid_grant"}
    )
    with pytest.raises(GmailAuthError, match=r"RefreshError: invalid_grant$"):
        client.address()


def test_acknowledgement_without_file_storage():
    from app.models import TaskResult
    from app.notify import acknowledgement_email

    result = TaskResult(task_id="G-0001", created=True, title="T", files_url="")
    original = type("M", (), {"subject": "Req", "sender_email": "a@example.com"})()
    assert "Files: none" in acknowledgement_email(result, original).text


def test_run_starts_no_new_message_after_the_time_budget():
    gmail = FakeGmailClient()
    for i in range(3):
        gmail.add(f"m{i}", body_text=f"Tolong cek printer {i}")
    ticks = iter([0.0, 0.0, 10.0, 200.0])  # start, before m0, before m1, before m2
    run = process_inbox(gmail, services(), budget_seconds=150, monotonic=lambda: next(ticks))
    assert (run.messages, run.created) == (2, 2)
    assert "UNREAD" in gmail.labels("m2")  # left for the next run
