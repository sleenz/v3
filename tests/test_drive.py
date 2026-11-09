from unittest.mock import MagicMock

from app.drive import SOURCE_PROPERTY, GoogleDriveStore, folder_name


def test_folder_name():
    assert folder_name("G-0012", "Data request") == "G-0012 – Data request"
    assert folder_name("G-0012", "A/B\nC") == "G-0012 – A-B C"
    long = folder_name("G-0012", "x" * 300)
    assert len(long) == 120 and long.endswith("…")


def _store(found):
    store = GoogleDriveStore("parent-1", credentials=object())
    service = MagicMock()
    files = service.files.return_value
    files.list.return_value.execute.return_value = {"files": found}
    files.create.return_value.execute.return_value = {"id": "new-1"}
    store._local.service = service
    return store, files


def test_existing_folder_is_reused():
    store, files = _store([{"id": "old-1"}])
    folder = store.task_folder("msg-'1", "Pending – x")
    assert folder.id == "old-1"
    query = files.list.call_args.kwargs["q"]
    assert f"key='{SOURCE_PROPERTY}' and value='msg-\\'1'" in query
    assert "'parent-1' in parents" in query
    files.create.assert_not_called()


def test_missing_folder_is_created_with_source_property():
    store, files = _store([])
    folder = store.task_folder("msg-1", "Pending – x")
    assert folder.id == "new-1"
    assert folder.url == "https://drive.google.com/drive/folders/new-1"
    body = files.create.call_args.kwargs["body"]
    assert body["appProperties"] == {SOURCE_PROPERTY: "msg-1"}
    assert body["parents"] == ["parent-1"]
    assert files.create.call_args.kwargs["supportsAllDrives"] is True
