"""Failure paths: a request is never lost and one failure never stops a whole job."""

from unittest.mock import MagicMock

import pytest

from app import clock
from app.extract.ai import NullProvider
from app.jobs import run_changes, run_digest
from app.models import AppConfig, RawRequest
from app.pipeline import Services, process_request
from app.sheets import GoogleSheetsBackend, SchemaError
from tests.fakes import FakeDriveStore, FakeGmailClient, FakeSheetsBackend, FakeSheetStore

SHEET = "https://docs.google.com/spreadsheets/d/sheet-1"


def make(backend=None):
    store = FakeSheetStore(backend or FakeSheetsBackend.from_fixtures())
    store.load()
    return Services(store=store, drive=FakeDriveStore(), ai=NullProvider())


def raw(text="Laporan stress test, deadline 20 November 2026.", source_id="m1"):
    return RawRequest(source="Email", source_id=source_id, received_at=clock.now(), body_text=text)


# --- pipeline after the row is written ---------------------------------------------------------


def test_audit_failure_after_write_does_not_lose_or_duplicate(caplog):
    svc = make()
    svc.store.append_audit = MagicMock(side_effect=OSError("quota"))
    result = process_request(raw(), svc)
    assert result.created
    assert svc.drive.only_folder()["name"].startswith("G-0001 – ")  # rename still happened
    assert any(r.getMessage() == "audit_write_failed" for r in caplog.records)
    again = process_request(raw(), svc)
    assert not again.created and again.task_id == result.task_id


def test_folder_rename_failure_after_write_still_succeeds(caplog):
    svc = make()
    svc.drive.rename_folder = MagicMock(side_effect=OSError("drive"))
    result = process_request(raw(), svc)
    assert result.created
    assert any(r.getMessage() == "folder_rename_failed" for r in caplog.records)


def test_row_write_failure_raises_so_the_caller_keeps_the_label():
    svc = make()
    svc.store.append_task = MagicMock(side_effect=OSError("sheets down"))
    with pytest.raises(OSError):
        process_request(raw(), svc)


def test_lost_race_reports_existing_task():
    svc = make()
    first = process_request(raw(), svc)
    svc.store.task_by_source_id = MagicMock(return_value=None)  # stale view at step 1
    second = process_request(raw(), svc)
    assert not second.created and second.task_id == first.task_id
    assert (
        len([t for t in svc.store.backend.column("Master", "ID") if str(t).startswith("G-")]) == 1
    )


# --- jobs keep going ---------------------------------------------------------------------------


def test_changes_audit_failure_does_not_resend(caplog):
    svc = make()
    gmail = FakeGmailClient()
    run_changes(svc.store, gmail, SHEET)
    result = process_request(raw(), svc)
    svc.store.update_task(result.task_id, {"Status": "Not Started"})
    svc.store.load()
    svc.store.append_audit = MagicMock(side_effect=OSError("quota"))
    assert run_changes(svc.store, gmail, SHEET).emailed == 1
    svc.store.load()
    assert run_changes(svc.store, gmail, SHEET).emailed == 0
    assert len(gmail.sent) == 1


def test_digest_send_failure_continues_with_others():
    svc = make()
    for i, text in enumerate(
        [
            "Laporan stress test, deadline 27 Oktober 2026.",
            "Laporan ATMR, deadline 28 Oktober 2026.",
        ]
    ):
        result = process_request(raw(text, f"m{i}"), svc)
        svc.store.update_task(result.task_id, {"Status": "Not Started"})
    svc.store.load()
    gmail = FakeGmailClient()
    sent = []

    def flaky(email, sender):
        if email.to == ["staff.a@example.com"]:
            raise OSError("smtp")
        sent.append(email.to)

    gmail.send = flaky
    run = run_digest(svc.store, gmail, SHEET)
    assert run.failed == 1
    assert ["staff.g@example.com"] in sent


# --- Config values as Sheets returns them ------------------------------------------------------


def config_values(**overrides):
    values = {
        "Effort: Small": 1, "Effort: Medium": 2, "Effort: Large": 4, "Support share": 0.5,
        "Overload threshold": 10, "Stale after (days)": 5, "Reminder time": "09:00",
        "Leaving warning (days)": 30, "Unknown letter code": "Needs review", "Manager email": "",
        "Allowed email domain": "", "Gmail intake label": "GRC/To log",
        "Gmail done label": "GRC/Logged", "Recurring lead (days)": 14, "Default due (days)": 7,
    }  # fmt: skip
    values.update(overrides)
    return values


def test_reminder_time_cell_as_day_fraction():
    assert AppConfig.model_validate(config_values(**{"Reminder time": 0.375})).reminder_time == (
        "09:00"
    )


def test_bad_config_value_is_schema_error():
    backend = FakeSheetsBackend.from_fixtures()
    for row in backend.tabs["Config"]:
        if row[0] == "Default due (days)":
            row[1] = "seven"
    store = FakeSheetStore(backend)
    store.load()
    with pytest.raises(SchemaError, match="Config: bad value for Default due"):
        store.config()


def test_effort_weight():
    config = AppConfig.model_validate(config_values())
    assert [config.effort_weight(e) for e in ("Small", "", "Medium", "Large", "Huge")] == [
        1, 2, 2, 4, 2,
    ]  # fmt: skip


# --- request shapes sent to Google (clients mocked) --------------------------------------------


def sheets_backend():
    backend = GoogleSheetsBackend("sheet-1", credentials=object(), sleep=lambda s: None)
    service = MagicMock()
    backend._local.service = service
    return backend, service.spreadsheets.return_value


def test_sheets_update_append_clear_titles():
    backend, sheets = sheets_backend()
    sheets.get.return_value.execute.return_value = {
        "sheets": [{"properties": {"title": "Master"}}, {"properties": {"title": "_state"}}]
    }
    assert backend.sheet_titles() == ["Master", "_state"]
    backend.update([("'Master'!A2:B2", [["'x", None]])])
    body = sheets.values.return_value.batchUpdate.call_args.kwargs["body"]
    assert body == {
        "valueInputOption": "USER_ENTERED",
        "data": [{"range": "'Master'!A2:B2", "values": [["'x", None]]}],
    }
    backend.append("'AuditLog'!A1", [["a"]])
    kwargs = sheets.values.return_value.append.call_args.kwargs
    assert (kwargs["valueInputOption"], kwargs["insertDataOption"]) == (
        "USER_ENTERED",
        "INSERT_ROWS",
    )
    backend.clear("'_state'!A5:F9")
    assert sheets.values.return_value.clear.call_args.kwargs["range"] == "'_state'!A5:F9"


def drive_store():
    from app.drive import GoogleDriveStore

    store = GoogleDriveStore("parent-1", credentials=object())
    service = MagicMock()
    store._local.service = service
    return store, service.files.return_value


def test_drive_file_names_follow_pages_and_upload_rename():
    store, files = drive_store()
    files.list.return_value.execute.side_effect = [
        {"files": [{"name": "a"}], "nextPageToken": "p2"},
        {"files": [{"name": "b"}]},
    ]
    assert store.file_names("f1") == {"a", "b"}
    store.upload("f1", "x.pdf", "", b"data")
    kwargs = files.create.call_args.kwargs
    assert kwargs["body"] == {"name": "x.pdf", "parents": ["f1"]}
    assert kwargs["supportsAllDrives"] is True
    assert kwargs["media_body"].mimetype() == "application/octet-stream"
    store.rename_folder("f1", "G-0001 – T")
    assert files.update.call_args.kwargs["body"] == {"name": "G-0001 – T"}


def test_gmail_send_uses_profile_address_once():
    from app.gmail import GoogleGmailClient, OutgoingEmail

    client = GoogleGmailClient(credentials=object())
    users = MagicMock()
    client._local.service = MagicMock()
    client._local.service.users.return_value = users
    users.getProfile.return_value.execute.return_value = {"emailAddress": "intake@example.com"}
    email = OutgoingEmail(to=["a@example.com"], subject="s", text="t", html="<p>t</p>")
    client.send(email, "GRC Tracker")
    client.send(email, "GRC Tracker")
    users.getProfile.assert_called_once()
    assert "raw" in users.messages.return_value.send.call_args.kwargs["body"]
