"""SheetStore: header-based reads and writes to the GRC Google Sheet.

Columns are found by header text, never by position. A job calls `load()` once (one batch read for
all tabs it needs) and then uses the cached, typed accessors. Writes go through `WRITE_LOCK` and
re-read Master inside the lock, so IDs and duplicate checks are never based on a stale cache.

The Google API sits behind `SheetsBackend`; `GoogleSheetsBackend` is the real one and
`tests/fakes.py` has an in-memory one.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from app import clock
from app.models import (
    RECURRING_REQUIRED_COLUMNS,
    REQUIRED_COLUMNS,
    REQUIRED_CONFIG_SETTINGS,
    WA_PENDING_COLUMNS,
    AppConfig,
    AuditEntry,
    CatalogEntry,
    FailureRow,
    LetterCode,
    RecurringRule,
    StaffMember,
    StateRow,
    Task,
    WaPending,
)

WRITE_LOCK = threading.RLock()

MASTER = "Master"
LOADED_TABS = ("Master", "Staff", "Catalog", "LetterCodes", "Config", "_state", "_failures")

DATE_COLUMNS = frozenset(
    {"Start / received", "Due date", "Submitted date", "Internship end date", "First due date"}
)
DATETIME_COLUMNS = frozenset(
    {"Last updated", "Notified at", "Timestamp", "Last at", "Created at", "Updated at"}
)
WA_PENDING = "_wa_pending"
RECURRING = "Recurring"
_SERIAL_EPOCH = datetime(1899, 12, 30)
_ID_PATTERN = re.compile(r"^G-(\d+)$")


class SchemaError(Exception):
    """The sheet is missing tabs, columns, settings or the Weight template row."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("Sheet schema problem: " + "; ".join(problems))


class Formula(str):
    """A cell value written as a formula (not escaped as text)."""


# --- Backend -----------------------------------------------------------------------------------


class SheetsBackend(Protocol):
    def sheet_titles(self) -> list[str]: ...

    def get(self, ranges: list[str], formulas: bool = False) -> list[list[list[Any]]]:
        """Values per range. `formulas=True` returns formulas instead of computed values.
        Dates come back as serial numbers. Trailing empty cells/rows may be missing."""
        ...

    def update(self, data: list[tuple[str, list[list[Any]]]]) -> None:
        """Write as if typed by a user (USER_ENTERED). `None` leaves a cell unchanged."""
        ...

    def append(self, range_: str, rows: list[list[Any]]) -> None: ...

    def clear(self, range_: str) -> None: ...


class GoogleSheetsBackend:
    """Sheets API v4 through google-api-python-client, one client per thread."""

    SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)
    MAX_TRIES = 3

    def __init__(
        self,
        sheet_id: str,
        credentials: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._sheet_id = sheet_id
        self._credentials = credentials
        self._sleep = sleep
        self._local = threading.local()

    def _values(self) -> Any:
        # httplib2 is not thread-safe, so every thread builds its own client (DESIGN D1).
        if not hasattr(self._local, "service"):
            import google.auth
            from googleapiclient.discovery import build

            credentials = self._credentials
            if credentials is None:
                credentials, _ = google.auth.default(scopes=list(self.SCOPES))
            self._local.service = build(
                "sheets", "v4", credentials=credentials, cache_discovery=False
            )
        return self._local.service.spreadsheets()

    def _execute(self, request: Any) -> Any:
        from googleapiclient.errors import HttpError

        for attempt in range(self.MAX_TRIES):
            try:
                return request.execute()
            except HttpError as error:
                status = getattr(error.resp, "status", None)
                if status not in (429, 500, 503) or attempt == self.MAX_TRIES - 1:
                    raise
                self._sleep(2**attempt)
        raise AssertionError("unreachable")

    def sheet_titles(self) -> list[str]:
        response = self._execute(
            self._values().get(spreadsheetId=self._sheet_id, fields="sheets.properties.title")
        )
        return [sheet["properties"]["title"] for sheet in response.get("sheets", [])]

    def get(self, ranges: list[str], formulas: bool = False) -> list[list[list[Any]]]:
        response = self._execute(
            self._values()
            .values()
            .batchGet(
                spreadsheetId=self._sheet_id,
                ranges=ranges,
                valueRenderOption="FORMULA" if formulas else "UNFORMATTED_VALUE",
                dateTimeRenderOption="SERIAL_NUMBER",
                majorDimension="ROWS",
            )
        )
        return [value_range.get("values", []) for value_range in response.get("valueRanges", [])]

    def update(self, data: list[tuple[str, list[list[Any]]]]) -> None:
        body = {
            "valueInputOption": "USER_ENTERED",
            "data": [{"range": range_, "values": values} for range_, values in data],
        }
        self._execute(self._values().values().batchUpdate(spreadsheetId=self._sheet_id, body=body))

    def append(self, range_: str, rows: list[list[Any]]) -> None:
        self._execute(
            self._values()
            .values()
            .append(
                spreadsheetId=self._sheet_id,
                range=range_,
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body={"values": rows},
            )
        )

    def clear(self, range_: str) -> None:
        self._execute(
            self._values().values().clear(spreadsheetId=self._sheet_id, range=range_, body={})
        )


# --- Cell encoding -----------------------------------------------------------------------------


def quote_tab(tab: str) -> str:
    return "'" + tab.replace("'", "''") + "'"


def column_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def to_cell(value: Any) -> Any:
    """Python value -> USER_ENTERED input.

    Text is prefixed with `'` so Sheets never turns it into a formula, number or date (a message
    starting with `=` or `+62` stays text). `None` is passed through and leaves the cell as it is.
    """
    if value is None or isinstance(value, Formula):
        return value
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(clock.TZ)
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, int | float):
        return value
    text = str(value)
    return "'" + text if text else ""


def from_cell(header: str, value: Any) -> Any:
    """Raw API value -> Python value. Date columns turn serial numbers into dates."""
    if value is None:
        return ""
    if isinstance(value, int | float) and not isinstance(value, bool):
        if header in DATE_COLUMNS:
            return (_SERIAL_EPOCH + timedelta(days=value)).date()
        if header in DATETIME_COLUMNS:
            moment = _SERIAL_EPOCH + timedelta(days=value)
            return moment.replace(microsecond=0, tzinfo=clock.TZ)
    return value


_CELL_REF = re.compile(r"(?<![A-Za-z0-9_$.])(\$?[A-Z]{1,3})(\d+)(?![\d(A-Za-z_])")


def shift_formula_row(formula: str, from_row: int, to_row: int) -> str:
    """Rewrite relative references to `from_row` (e.g. `S2`) to `to_row`; `$`-rows stay."""
    parts = re.split(r'("[^"]*")', formula)  # leave string literals alone
    for i in range(0, len(parts), 2):
        parts[i] = _CELL_REF.sub(
            lambda m: m.group(1) + str(to_row) if int(m.group(2)) == from_row else m.group(0),
            parts[i],
        )
    return "".join(parts)


# --- Tab data ----------------------------------------------------------------------------------


@dataclass
class TabData:
    name: str
    header: list[str]
    rows: list[list[Any]] = field(default_factory=list)  # data rows, sheet row = index + 2

    @classmethod
    def from_values(cls, name: str, values: list[list[Any]]) -> TabData:
        header = [str(cell).strip() for cell in values[0]] if values else []
        width = len(header)
        rows = [list(row) + [None] * (width - len(row)) for row in values[1:]]
        return cls(name, header, rows)

    def index(self, header: str) -> int:
        return self.header.index(header)

    def records(self) -> Iterable[tuple[int, dict[str, Any]]]:
        """(sheet row number, {header: value}) for every non-blank row."""
        for offset, row in enumerate(self.rows):
            if all(cell in (None, "") for cell in row):
                continue
            record = {name: from_cell(name, row[i]) for i, name in enumerate(self.header) if name}
            yield offset + 2, record

    def last_row_number(self) -> int:
        """Sheet row number of the last non-blank row (1 = header only)."""
        for offset in range(len(self.rows) - 1, -1, -1):
            if any(cell not in (None, "") for cell in self.rows[offset]):
                return offset + 2
        return 1

    def encode(self, values: dict[str, Any]) -> list[Any]:
        """{header: value} -> a row in header order; headers not given stay unchanged."""
        return [to_cell(values[name]) if name in values else None for name in self.header]


def _parse[Model: BaseModel](
    model: type[Model], tab: TabData, records: Iterable[tuple[int, dict[str, Any]]]
) -> list[Model]:
    """Rows -> models. A bad cell names its tab, row number and column (never the value)."""
    parsed = []
    for row, record in records:
        try:
            parsed.append(model.model_validate(record))
        except ValidationError as error:
            fields = sorted({str(problem["loc"][0]) for problem in error.errors()})
            raise SchemaError(
                [f"{tab.name}: bad value in row {row}, column(s) {', '.join(fields)}"]
            ) from None
    return parsed


# --- Store -------------------------------------------------------------------------------------


class SheetStore:
    def __init__(self, backend: SheetsBackend, lock: threading.RLock = WRITE_LOCK):
        self._backend = backend
        self._lock = lock
        self._tabs: dict[str, TabData] = {}
        self._audit_header: list[str] = []
        self._config: AppConfig | None = None
        self._staff: list[StaffMember] | None = None

    # Loading ---------------------------------------------------------------------------------

    def load(self) -> None:
        """One batch read of every tab a job needs, then a schema check (raises SchemaError).

        All problems are reported at once (missing tabs and the columns missing from the tabs
        that do exist), so the sheet can be fixed in one pass.
        """
        titles = set(self._backend.sheet_titles())
        present = [tab for tab in LOADED_TABS if tab in titles]
        ranges = [quote_tab(tab) for tab in present]
        if "AuditLog" in titles:
            ranges.append(quote_tab("AuditLog") + "!1:1")
        results = self._backend.get(ranges)
        self._tabs = {
            tab: TabData.from_values(tab, values)
            for tab, values in zip(present, results[: len(present)], strict=True)
        }
        audit = results[len(present)] if "AuditLog" in titles else []
        self._audit_header = [str(cell).strip() for cell in (audit[0] if audit else [])]
        self._config = None
        self._staff = None
        missing_tabs = [tab for tab in (*LOADED_TABS, "AuditLog") if tab not in titles]
        self.check_schema(missing_tabs)

    def invalidate(self) -> None:
        self._tabs = {}
        self._config = None
        self._staff = None

    def _tab(self, name: str) -> TabData:
        if name not in self._tabs:
            self.load()
        return self._tabs[name]

    def check_schema(self, missing_tabs: list[str] | None = None) -> None:
        problems = [f"missing tab {tab}" for tab in missing_tabs or []]
        headers = {name: tab.header for name, tab in self._tabs.items()}
        if self._audit_header:
            headers["AuditLog"] = self._audit_header
        for tab, required in REQUIRED_COLUMNS.items():
            if tab not in headers and tab in (missing_tabs or []):
                continue
            missing = [column for column in required if column not in headers.get(tab, [])]
            if missing:
                problems.append(f"{tab} is missing column(s): {', '.join(missing)}")
        if "Config" in self._tabs and not any("Config" in p for p in problems):
            settings = self._settings()
            missing = [name for name in REQUIRED_CONFIG_SETTINGS if name not in settings]
            if missing:
                problems.append(f"Config is missing setting(s): {', '.join(missing)}")
        if problems:
            raise SchemaError(problems)

    # Reading ---------------------------------------------------------------------------------

    def _settings(self) -> dict[str, Any]:
        tab = self._tabs["Config"]
        return {
            str(record["Setting"]).strip(): record["Value"]
            for _, record in tab.records()
            if str(record.get("Setting", "")).strip()
        }

    def config(self) -> AppConfig:
        if self._config is None:
            self._tab("Config")
            try:
                self._config = AppConfig.model_validate(self._settings())
            except ValidationError as error:
                names = sorted({str(problem["loc"][0]) for problem in error.errors()})
                raise SchemaError([f"Config: bad value for {', '.join(names)}"]) from None
        return self._config

    def staff(self) -> list[StaffMember]:
        if self._staff is None:
            tab = self._tab("Staff")
            records = ((n, r) for n, r in tab.records() if str(r.get("Name", "")).strip())
            self._staff = _parse(StaffMember, tab, records)
        return self._staff

    def catalog(self) -> list[CatalogEntry]:
        tab = self._tab("Catalog")
        records = ((n, r) for n, r in tab.records() if str(r.get("Catalog ID", "")).strip())
        return _parse(CatalogEntry, tab, records)

    def letter_codes(self) -> list[LetterCode]:
        tab = self._tab("LetterCodes")
        records = ((n, r) for n, r in tab.records() if str(r.get("Letter code", "")).strip())
        return _parse(LetterCode, tab, records)

    def recurring(self) -> tuple[list[RecurringRule], list[str]]:
        """The optional `Recurring` tab (v1.4 part 3), read fresh: (rules, problems).

        No tab: nothing to create. A missing column stops the whole tab; a bad row is skipped
        and named (tab, row and column, never the value), so one typo cannot hold up the rest.
        """
        if RECURRING not in set(self._backend.sheet_titles()):
            return [], []
        [values] = self._backend.get([quote_tab(RECURRING)])
        tab = TabData.from_values(RECURRING, values)
        missing = [column for column in RECURRING_REQUIRED_COLUMNS if column not in tab.header]
        if missing:
            return [], [f"{RECURRING} is missing column(s): {', '.join(missing)}"]
        rules: list[RecurringRule] = []
        problems: list[str] = []
        for row, record in tab.records():
            if not str(record.get("Name", "")).strip():
                continue
            try:
                rules.append(RecurringRule.model_validate(record))
            except ValidationError as error:
                fields = sorted({str(problem["loc"][0]) for problem in error.errors()})
                problems.append(f"{RECURRING} row {row}: bad value in {', '.join(fields)}")
        return rules, problems

    def tasks(self) -> list[Task]:
        tab = self._tab(MASTER)
        records = ((n, r) for n, r in tab.records() if str(r.get("ID", "")).strip())
        return _parse(Task, tab, records)

    def master_header(self) -> list[str]:
        return list(self._tab(MASTER).header)

    def task_by_source_id(self, source_id: str) -> Task | None:
        return next((task for task in self.tasks() if task.source_id == source_id), None)

    def state(self) -> dict[str, StateRow]:
        tab = self._tab("_state")
        records = ((n, r) for n, r in tab.records() if str(r.get("ID", "")).strip())
        return {row.id: row for row in _parse(StateRow, tab, records)}

    def failures(self) -> dict[str, FailureRow]:
        tab = self._tab("_failures")
        records = ((n, r) for n, r in tab.records() if str(r.get("Key", "")).strip())
        return {row.key: row for row in _parse(FailureRow, tab, records)}

    # Writing ---------------------------------------------------------------------------------

    def _fresh_master(self) -> TabData:
        """Master re-read inside the write lock, with formulas (IDs, Source IDs, Weight)."""
        [values] = self._backend.get([quote_tab(MASTER)], formulas=True)
        return TabData.from_values(MASTER, values)

    @staticmethod
    def _next_id(master: TabData) -> str:
        id_col = master.index("ID")
        numbers = [
            int(match.group(1))
            for row in master.rows
            if (match := _ID_PATTERN.match(str(row[id_col] or "").strip()))
        ]
        return f"G-{max(numbers, default=0) + 1:04d}"

    def next_id(self) -> str:
        with self._lock:
            return self._next_id(self._fresh_master())

    @staticmethod
    def _weight_formula(master: TabData, new_row: int) -> Formula:
        weight_col = master.index("Weight")
        for offset in range(len(master.rows) - 1, -1, -1):
            cell = master.rows[offset][weight_col]
            if isinstance(cell, str) and cell.startswith("="):
                return Formula(shift_formula_row(cell, offset + 2, new_row))
        raise SchemaError(["Master needs at least one data row with the Weight formula to copy"])

    def append_task(self, task: Task) -> tuple[Task, bool]:
        """Write `task` as a new Master row with the next ID and the Weight formula.

        Returns (task as saved, True). If a row with the same Source ID already exists, returns
        (that row, False) and writes nothing.
        """
        with self._lock:
            master = self._fresh_master()
            if task.source_id:
                for _, record in master.records():
                    if str(record.get("Source ID", "")) == task.source_id:
                        return Task.model_validate(record), False
            new_row = master.last_row_number() + 1
            saved = task.model_copy(update={"id": self._next_id(master)})
            values = saved.model_dump(by_alias=True)
            values["Weight"] = self._weight_formula(master, new_row)
            last_col = column_letter(len(master.header) - 1)
            self._backend.update(
                [(f"{quote_tab(MASTER)}!A{new_row}:{last_col}{new_row}", [master.encode(values)])]
            )
            self._after_append(saved)
            return saved, True

    def _after_append(self, task: Task) -> None:
        """Keep the cache usable for the rest of the job without another read."""
        cached = self._tabs.get(MASTER)
        if cached is not None:
            row = [None] * len(cached.header)
            for name, value in task.model_dump(by_alias=True).items():
                if name in cached.header:
                    row[cached.index(name)] = value
            cached.rows.append(row)
        if self._staff is not None and task.doer:
            # The sheet's Weighted open load formula counts the new row; mirror it locally so
            # several requests in one job do not all land on the same person.
            weight = self.config().effort_weight(task.effort)
            for person in self._staff:
                if person.name.casefold() == task.doer.casefold():
                    person.weighted_open_load += weight

    def update_task(self, task_id: str, changes: dict[str, Any]) -> None:
        """Set cells of one Master row, by header. Unknown headers raise SchemaError."""
        with self._lock:
            master = self._fresh_master()
            unknown = [name for name in changes if name not in master.header]
            if unknown:
                raise SchemaError([f"Master has no column(s) {', '.join(unknown)}"])
            id_col = master.index("ID")
            row_number = next(
                (i + 2 for i, row in enumerate(master.rows) if str(row[id_col] or "") == task_id),
                None,
            )
            if row_number is None:
                raise KeyError(task_id)
            self._backend.update(
                [
                    (
                        f"{quote_tab(MASTER)}!{column_letter(master.index(name))}{row_number}",
                        [[to_cell(value)]],
                    )
                    for name, value in changes.items()
                ]
            )
            cached = self._tabs.get(MASTER)
            if cached is not None:
                id_index = cached.index("ID")
                for row in cached.rows:
                    if str(row[id_index] or "") == task_id:
                        for name, value in changes.items():
                            row[cached.index(name)] = value

    def update_staff(self, name: str, changes: dict[str, Any]) -> None:
        """Set cells of one Staff row (matched by Name, case-insensitive), by header.
        A header the tab lacks raises SchemaError naming it."""
        with self._lock:
            [values] = self._backend.get([quote_tab("Staff")])
            tab = TabData.from_values("Staff", values)
            unknown = [header for header in changes if header not in tab.header]
            if unknown:
                raise SchemaError([f"Staff has no column(s) {', '.join(unknown)}"])
            name_col = tab.index("Name")
            wanted = name.strip().casefold()
            row_number = next(
                (
                    i + 2
                    for i, row in enumerate(tab.rows)
                    if str(row[name_col] or "").strip().casefold() == wanted
                ),
                None,
            )
            if row_number is None:
                raise KeyError(name)
            self._backend.update(
                [
                    (
                        f"{quote_tab('Staff')}!{column_letter(tab.index(header))}{row_number}",
                        [[to_cell(value)]],
                    )
                    for header, value in changes.items()
                ]
            )
            self._tabs.pop("Staff", None)
            self._staff = None

    def append_audit(self, entries: list[AuditEntry]) -> None:
        if not entries:
            return
        if not self._audit_header:
            self.load()
        audit = TabData("AuditLog", self._audit_header)
        rows = [audit.encode(entry.model_dump(by_alias=True)) for entry in entries]
        with self._lock:
            self._backend.append(quote_tab("AuditLog") + "!A1", rows)

    def write_state(self, rows: list[StateRow]) -> None:
        """Replace `_state` with `rows`. Rows are written before the leftovers are cleared, so a
        crash in between never leaves `_state` empty (an empty `_state` means first run)."""
        self._rewrite("_state", [row.model_dump(by_alias=True) for row in rows])

    def upsert_failure(self, row: FailureRow) -> None:
        failures = self.failures()
        failures[row.key] = row
        self._rewrite("_failures", [item.model_dump(by_alias=True) for item in failures.values()])

    # WhatsApp drafts and deadline questions (v1.3) ---------------------------------------------

    def _fresh_wa_pending(self) -> TabData:
        """`_wa_pending`, re-read: drafts change between messages that arrive seconds apart."""
        if WA_PENDING not in set(self._backend.sheet_titles()):
            raise SchemaError([f"missing tab {WA_PENDING}"])
        [values] = self._backend.get([quote_tab(WA_PENDING)])
        tab = TabData.from_values(WA_PENDING, values)
        missing = [column for column in WA_PENDING_COLUMNS if column not in tab.header]
        if missing:
            raise SchemaError([f"{WA_PENDING} is missing column(s): {', '.join(missing)}"])
        return tab

    def wa_pending(self) -> list[WaPending]:
        tab = self._fresh_wa_pending()
        records = ((n, r) for n, r in tab.records() if str(r.get("Key", "")).strip())
        return _parse(WaPending, tab, records)

    def edit_wa_pending[Result](self, edit: Callable[[list[WaPending]], Result]) -> Result:
        """Read `_wa_pending`, let `edit` change the list in place, write it back; all under the
        write lock, so two messages from one sender never both open a draft."""
        with self._lock:
            tab = self._fresh_wa_pending()
            records = ((n, r) for n, r in tab.records() if str(r.get("Key", "")).strip())
            rows = _parse(WaPending, tab, records)
            result = edit(rows)
            self._tabs[WA_PENDING] = tab
            self._rewrite(WA_PENDING, [row.model_dump(by_alias=True) for row in rows])
            return result

    def _rewrite(self, tab_name: str, records: list[dict[str, Any]]) -> None:
        with self._lock:
            tab = self._tab(tab_name)
            last_col = column_letter(len(tab.header) - 1)
            old_last = tab.last_row_number()
            if records:
                self._backend.update(
                    [
                        (
                            f"{quote_tab(tab_name)}!A2:{last_col}{len(records) + 1}",
                            [tab.encode(record) for record in records],
                        )
                    ]
                )
            if old_last > len(records) + 1:
                self._backend.clear(
                    f"{quote_tab(tab_name)}!A{len(records) + 2}:{last_col}{old_last}"
                )
            tab.rows = [[record.get(name) for name in tab.header] for record in records]
