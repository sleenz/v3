from datetime import date, datetime

import pytest

from app import clock
from app.models import AuditEntry, FailureRow, StateRow, Task
from app.sheets import (
    Formula,
    SchemaError,
    TabData,
    column_letter,
    from_cell,
    shift_formula_row,
    to_cell,
)
from tests.fakes import FakeSheetsBackend, FakeSheetStore

WEIGHT_2 = (
    '=IFERROR(VLOOKUP("Effort: "&IF(S2="","Medium",S2),Config!$A$2:$B$4,2,FALSE()),Config!$B$3)'
)


@pytest.fixture
def store() -> FakeSheetStore:
    store = FakeSheetStore()
    store.load()
    return store


# --- helpers -----------------------------------------------------------------------------------


@pytest.mark.parametrize(("index", "letters"), [(0, "A"), (25, "Z"), (26, "AA"), (34, "AI")])
def test_column_letter(index, letters):
    assert column_letter(index) == letters


def test_shift_formula_row_keeps_absolute_rows_and_strings():
    shifted = shift_formula_row(WEIGHT_2, 2, 57)
    assert shifted == WEIGHT_2.replace("S2", "S57")
    assert shift_formula_row('=IF(A2="A2",LOG10(B2),$C$2)', 2, 3) == '=IF(A3="A2",LOG10(B3),$C$2)'


def test_to_cell_escapes_text():
    assert to_cell("=HYPERLINK(evil)") == "'=HYPERLINK(evil)"
    assert to_cell("+62 812") == "'+62 812"
    assert to_cell("0012") == "'0012"
    assert to_cell("") == ""
    assert to_cell(None) is None
    assert to_cell(Formula("=A1")) == "=A1"
    assert to_cell(date(2026, 11, 6)) == "2026-11-06"
    assert to_cell(datetime(2026, 10, 26, 1, 0, tzinfo=clock.TZ)) == "2026-10-26 01:00:00"
    assert to_cell(3) == 3
    assert to_cell(True) == "Yes"


def test_from_cell_dates():
    assert from_cell("Due date", 46332) == date(2026, 11, 6)
    assert from_cell("Last updated", 46321.375) == datetime(2026, 10, 26, 9, 0, tzinfo=clock.TZ)
    assert from_cell("Title", 46332) == 46332
    assert from_cell("Title", None) == ""


def test_tab_data_pads_short_rows_and_skips_blank():
    tab = TabData.from_values("T", [["A", "B"], ["x"], [], ["", None], ["y", "z"]])
    assert [record for _, record in tab.records()] == [
        {"A": "x", "B": ""},
        {"A": "y", "B": "z"},
    ]
    assert [number for number, _ in tab.records()] == [2, 5]
    assert tab.last_row_number() == 5


# --- loading -----------------------------------------------------------------------------------


def test_load_is_one_batch_read(store):
    gets = [call for call in store.backend.calls if call[0] == "get"]
    assert len(gets) == 1
    store.staff(), store.catalog(), store.config(), store.tasks(), store.state()
    assert len([call for call in store.backend.calls if call[0] == "get"]) == 1


def test_typed_reads(store):
    assert len(store.staff()) == 18
    assert store.config().overload_threshold == 10
    hr = next(entry for entry in store.catalog() if entry.catalog_id == "HR-DATA")
    assert hr.known_contacts == ["Hana Setyaningsih"]
    assert hr.typical_due_day == "15"
    assert store.letter_codes()[0].letter_code == "DIR/ASQ"
    [example] = store.tasks()
    assert example.due_date == date(2026, 10, 15)
    assert store.state() == {}


def test_missing_tab():
    backend = FakeSheetsBackend.from_fixtures()
    del backend.tabs["_state"]
    with pytest.raises(SchemaError, match="missing tab _state"):
        FakeSheetStore(backend).load()


def test_missing_columns_named():
    backend = FakeSheetsBackend.from_fixtures()
    backend.tabs["Master"][0] = [h for h in backend.tabs["Master"][0] if h != "Source ID"]
    backend.tabs["Catalog"][0] = [
        "Known contacts (old)" if h == "Known contacts" else h for h in backend.tabs["Catalog"][0]
    ]
    with pytest.raises(SchemaError) as error:
        FakeSheetStore(backend).load()
    assert "Master is missing column(s): Source ID" in str(error.value)
    assert "Catalog is missing column(s): Known contacts" in str(error.value)


def test_columns_found_by_name_not_position():
    backend = FakeSheetsBackend.from_fixtures()
    staff = backend.tabs["Staff"]
    for row in staff:
        row.reverse()
    store = FakeSheetStore(backend)
    store.load()
    assert store.staff()[0].name == "Staff A"
    assert store.staff()[0].main_division == "Policy and Risk"


def test_missing_config_setting():
    backend = FakeSheetsBackend.from_fixtures()
    backend.tabs["Config"] = [
        row for row in backend.tabs["Config"] if row[0] != "Default due (days)"
    ]
    with pytest.raises(SchemaError, match="Config is missing setting"):
        FakeSheetStore(backend).load()


def test_optional_config_defaults():
    backend = FakeSheetsBackend.from_fixtures()
    backend.tabs["Config"] = [
        row
        for row in backend.tabs["Config"]
        if row[0] not in {"AI provider", "Sender name", "Mask emails for AI", "Known requesters"}
    ]
    store = FakeSheetStore(backend)
    store.load()
    config = store.config()
    assert (config.ai_provider, config.sender_name, config.mask_emails_for_ai) == (
        "Off",
        "GRC Tracker",
        False,
    )
    assert config.known_requesters == []


def test_bad_value_is_a_schema_error():
    backend = FakeSheetsBackend.from_fixtures()
    staff = backend.tabs["Staff"]
    staff[1][staff[0].index("Role")] = "Boss"
    store = FakeSheetStore(backend)
    store.load()
    with pytest.raises(SchemaError, match=r"Staff: bad value in row 2, column\(s\) Role"):
        store.staff()


# --- writing tasks -----------------------------------------------------------------------------


def _task(**overrides) -> Task:
    values = {
        "title": "=cmd|' /C calc'!A0",
        "status": "Proposed",
        "doer": "Staff F",
        "owner": "Staff F",
        "due_date": date(2026, 11, 6),
        "last_updated": clock.now(),
        "source": "Email",
        "source_id": "msg-1",
        "letter_no": "0012",
    }
    values.update(overrides)
    return Task(**values)


def test_append_task_first_id_weight_formula_and_escaping(store):
    saved, created = store.append_task(_task())
    assert created
    assert saved.id == "G-0001"
    backend = store.backend
    assert backend.raw("Master", 3, "ID") == "'G-0001"
    assert backend.raw("Master", 3, "Weight") == WEIGHT_2.replace("S2", "S3")
    assert backend.raw("Master", 3, "Title") == "'=cmd|' /C calc'!A0"
    assert backend.column("Master", "Title")[1] == "=cmd|' /C calc'!A0"
    assert backend.column("Master", "Letter no.")[1] == "0012"
    assert backend.column("Master", "Due date")[1] == pytest.approx(46332)
    reread = FakeSheetStore(backend)
    reread.load()
    task = reread.task_by_source_id("msg-1")
    assert task is not None
    assert task.due_date == date(2026, 11, 6)
    assert task.last_updated == clock.now()


def test_append_task_next_id_uses_highest(store):
    store.append_task(_task())
    master = store.backend.tabs["Master"]
    master[2][master[0].index("ID")] = "'G-0041"
    second, _ = store.append_task(_task(source_id="msg-2"))
    assert second.id == "G-0042"


def test_append_task_duplicate_source_id_writes_nothing(store):
    first, _ = store.append_task(_task())
    rows_before = len(store.backend.tabs["Master"])
    again, created = store.append_task(_task(title="Other"))
    assert not created
    assert again.id == first.id
    assert len(store.backend.tabs["Master"]) == rows_before


def test_append_task_without_template_row():
    backend = FakeSheetsBackend.from_fixtures()
    backend.tabs["Master"] = backend.tabs["Master"][:1]
    store = FakeSheetStore(backend)
    store.load()
    with pytest.raises(SchemaError, match="Weight formula"):
        store.append_task(_task())


def test_append_task_updates_cached_load_and_tasks(store):
    before = next(p for p in store.staff() if p.name == "Staff F").weighted_open_load
    store.append_task(_task(effort="Large"))
    after = next(p for p in store.staff() if p.name == "Staff F").weighted_open_load
    assert after == before + 4
    assert store.task_by_source_id("msg-1") is not None


def test_update_task(store):
    saved, _ = store.append_task(_task())
    store.update_task(saved.id, {"Status": "Not Started", "Notes": "+ ok"})
    assert store.backend.column("Master", "Status")[1] == "Not Started"
    assert store.backend.column("Master", "Notes")[1] == "+ ok"
    with pytest.raises(KeyError):
        store.update_task("G-9999", {"Status": "Done"})
    with pytest.raises(SchemaError):
        store.update_task(saved.id, {"Nope": 1})


# --- audit, state, failures --------------------------------------------------------------------


def test_append_audit(store):
    entry = AuditEntry(
        timestamp=clock.now(), user="service", task_id="G-0001", action="Created", reason="Rule 1"
    )
    store.append_audit([entry])
    audit = store.backend.tabs["AuditLog"]
    assert len(audit) == 2
    assert audit[1][audit[0].index("Action")] == "'Created"


def test_write_state_rewrites_and_clears_leftovers(store):
    rows = [StateRow(id=f"G-000{i}", status="Proposed") for i in range(1, 4)]
    store.write_state(rows)
    assert set(store.state()) == {"G-0001", "G-0002", "G-0003"}
    store.write_state(rows[:1])
    assert set(store.state()) == {"G-0001"}
    reread = FakeSheetStore(store.backend)
    reread.load()
    assert set(reread.state()) == {"G-0001"}
    calls = [call[0] for call in store.backend.calls]
    assert calls.index("clear") > calls.index("update")


def test_upsert_failure(store):
    store.upsert_failure(
        FailureRow(key="msg-1", count=1, last_error="Timeout", last_at=clock.now())
    )
    store.upsert_failure(
        FailureRow(key="msg-1", count=2, last_error="Timeout", last_at=clock.now())
    )
    store.upsert_failure(FailureRow(key="alert:schema_error", count=1))
    reread = FakeSheetStore(store.backend)
    reread.load()
    failures = reread.failures()
    assert failures["msg-1"].count == 2
    assert failures["msg-1"].last_at == clock.now()
    assert set(failures) == {"msg-1", "alert:schema_error"}


# --- GoogleSheetsBackend (no network: the API client is a mock) ---------------------------------


def _http_error(status: int):
    from googleapiclient.errors import HttpError
    from httplib2 import Response

    return HttpError(Response({"status": status}), b"{}")


def _backend_with(request):
    from unittest.mock import MagicMock

    from app.sheets import GoogleSheetsBackend

    sleeps: list[float] = []
    backend = GoogleSheetsBackend("sheet-1", credentials=object(), sleep=sleeps.append)
    service = MagicMock()
    service.spreadsheets.return_value.values.return_value.batchGet.return_value = request
    backend._local.service = service
    return backend, service, sleeps


def test_google_backend_get_params_and_retry_on_429():
    from unittest.mock import MagicMock

    request = MagicMock()
    request.execute.side_effect = [_http_error(429), {"valueRanges": [{"values": [["A"]]}, {}]}]
    backend, service, sleeps = _backend_with(request)
    assert backend.get(["'Master'", "'Staff'"]) == [[["A"]], []]
    kwargs = service.spreadsheets.return_value.values.return_value.batchGet.call_args.kwargs
    assert kwargs["valueRenderOption"] == "UNFORMATTED_VALUE"
    assert kwargs["dateTimeRenderOption"] == "SERIAL_NUMBER"
    assert sleeps == [1]


def test_google_backend_gives_up_after_three_tries():
    from unittest.mock import MagicMock

    from googleapiclient.errors import HttpError

    request = MagicMock()
    request.execute.side_effect = [_http_error(429)] * 3
    backend, _, sleeps = _backend_with(request)
    with pytest.raises(HttpError):
        backend.get(["'Master'"])
    assert sleeps == [1, 2]


def test_google_backend_does_not_retry_client_errors():
    from unittest.mock import MagicMock

    from googleapiclient.errors import HttpError

    request = MagicMock()
    request.execute.side_effect = [_http_error(403)]
    backend, _, sleeps = _backend_with(request)
    with pytest.raises(HttpError):
        backend.get(["'Master'"])
    assert sleeps == []


def test_update_task_keeps_cache_without_reload(store):
    saved, _ = store.append_task(_task())
    store.update_task(saved.id, {"Status": "In Progress"})
    gets = len([call for call in store.backend.calls if call[0] == "get"])
    assert store.task_by_source_id("msg-1").status == "In Progress"
    assert len([call for call in store.backend.calls if call[0] == "get"]) == gets


def test_all_schema_problems_reported_at_once():
    backend = FakeSheetsBackend.from_fixtures()
    del backend.tabs["_state"]
    del backend.tabs["_failures"]
    backend.tabs["Master"][0] = [h for h in backend.tabs["Master"][0] if h != "Source ID"]
    backend.tabs["Catalog"][0] = [h for h in backend.tabs["Catalog"][0] if h != "Known contacts"]
    with pytest.raises(SchemaError) as error:
        FakeSheetStore(backend).load()
    assert error.value.problems == [
        "missing tab _state",
        "missing tab _failures",
        "Master is missing column(s): Source ID",
        "Catalog is missing column(s): Known contacts",
    ]
