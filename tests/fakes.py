"""Fakes for Google services. Tests never touch the network."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app import clock
from app.drive import Folder, folder_url
from app.extract.ai import NotConfigured
from app.models import WA_PENDING_COLUMNS, AIResult, Attachment, CatalogEntry
from app.sheets import SheetStore, column_letter

FIXTURES = Path(__file__).parent / "fixtures"
_SERIAL_EPOCH = datetime(1899, 12, 30)
_RANGE = re.compile(
    r"^'(?P<tab>(?:[^']|'')+)'(?:!(?P<c1>[A-Z]*)(?P<r1>\d*)(?::(?P<c2>[A-Z]*)(?P<r2>\d*))?)?$"
)
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
_NUMBER = re.compile(r"^[+-]?\d+(\.\d+)?$")


def _column_index(letters: str) -> int:
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - 64)
    return index - 1


def _serial(value: date | datetime) -> float:
    if not isinstance(value, datetime):
        value = datetime(value.year, value.month, value.day)
    return (value.replace(tzinfo=None) - _SERIAL_EPOCH).total_seconds() / 86400


class FakeSheetsBackend:
    """In-memory spreadsheet that mimics USER_ENTERED parsing and API read behaviour.

    Cells hold what was entered. Reads apply Sheets' rules: `'text` is text, ISO dates become
    serial numbers, bare numeric text becomes a number, formulas read as `formula_value` (or the
    formula itself when `formulas=True`). Trailing empty cells and rows are dropped like the API.
    """

    def __init__(self, tabs: dict[str, list[list[Any]]], formula_value: Any = None):
        self.tabs = {name: [list(row) for row in rows] for name, rows in tabs.items()}
        self.formula_value = formula_value
        self.calls: list[tuple[str, Any]] = []

    @classmethod
    def from_fixtures(cls, directory: Path = FIXTURES) -> FakeSheetsBackend:
        files = {
            "Master": "master.json",
            "Staff": "staff.json",
            "Catalog": "catalog.json",
            "LetterCodes": "lettercodes.json",
            "Config": "config.json",
        }
        tabs: dict[str, list[list[Any]]] = {}
        for tab, filename in files.items():
            data = json.loads((directory / filename).read_text(encoding="utf-8"))
            columns = data["columns"]
            tabs[tab] = [columns] + [
                [_stored(row.get(column)) for column in columns] for row in data["rows"]
            ]
        tabs["AuditLog"] = [
            ["Timestamp", "User", "Task ID", "Action", "Field", "Old value", "New value", "Reason"]
        ]
        tabs["_state"] = [["ID", "Status", "Owner", "Doer", "Notified doer", "Notified at"]]
        tabs["_failures"] = [["Key", "Count", "Last error", "Last at"]]
        tabs["_wa_pending"] = [list(WA_PENDING_COLUMNS)]
        return cls(tabs)

    # Helpers for tests ---------------------------------------------------------------------

    def column(self, tab: str, header: str) -> list[Any]:
        rows = self.tabs[tab]
        index = rows[0].index(header)
        return [self._read(row[index] if index < len(row) else None) for row in rows[1:]]

    def raw(self, tab: str, row_number: int, header: str) -> Any:
        rows = self.tabs[tab]
        return rows[row_number - 1][rows[0].index(header)]

    # SheetsBackend -------------------------------------------------------------------------

    def sheet_titles(self) -> list[str]:
        self.calls.append(("titles", None))
        return list(self.tabs)

    def get(self, ranges: list[str], formulas: bool = False) -> list[list[list[Any]]]:
        self.calls.append(("get", tuple(ranges)))
        return [self._get(range_, formulas) for range_ in ranges]

    def update(self, data: list[tuple[str, list[list[Any]]]]) -> None:
        self.calls.append(("update", [range_ for range_, _ in data]))
        for range_, values in data:
            tab, first_row, first_col, _, _ = self._parse(range_)
            for r, row in enumerate(values):
                for c, value in enumerate(row):
                    if value is not None:
                        self._set(tab, first_row + r, first_col + c, value)

    def append(self, range_: str, rows: list[list[Any]]) -> None:
        self.calls.append(("append", range_))
        tab = self._parse(range_)[0]
        grid = self.tabs[tab]
        while grid and all(cell in (None, "") for cell in grid[-1]):
            grid.pop()
        for row in rows:
            grid.append([value if value is not None else "" for value in row])

    def clear(self, range_: str) -> None:
        self.calls.append(("clear", range_))
        tab, first_row, first_col, last_row, last_col = self._parse(range_)
        grid = self.tabs[tab]
        for r in range(first_row, min(last_row, len(grid) - 1) + 1):
            for c in range(first_col, min(last_col, len(grid[r]) - 1) + 1):
                grid[r][c] = ""

    # Internals -----------------------------------------------------------------------------

    def _parse(self, range_: str) -> tuple[str, int, int, int, int]:
        """-> (tab, first row index, first col index, last row index, last col index), 0-based."""
        match = _RANGE.match(range_)
        if not match:
            raise ValueError(f"unsupported range {range_}")
        tab = match["tab"].replace("''", "'")
        if tab not in self.tabs:
            raise ValueError(f"no tab {tab}")
        first_row = int(match["r1"]) - 1 if match["r1"] else 0
        first_col = _column_index(match["c1"]) if match["c1"] else 0
        if match["c2"] is None and match["r2"] is None:
            last_row = first_row if match["r1"] else 10**6
            last_col = first_col if match["c1"] else 10**6
        else:
            last_row = int(match["r2"]) - 1 if match["r2"] else 10**6
            last_col = _column_index(match["c2"]) if match["c2"] else 10**6
        return tab, first_row, first_col, last_row, last_col

    def _get(self, range_: str, formulas: bool) -> list[list[Any]]:
        tab, first_row, first_col, last_row, last_col = self._parse(range_)
        out = []
        for row in self.tabs[tab][first_row : last_row + 1]:
            values = [self._read(cell, formulas) for cell in row[first_col : last_col + 1]]
            while values and values[-1] in (None, ""):
                values.pop()
            out.append(values)
        while out and not out[-1]:
            out.pop()
        return out

    def _set(self, tab: str, row: int, col: int, value: Any) -> None:
        grid = self.tabs[tab]
        while len(grid) <= row:
            grid.append([])
        while len(grid[row]) <= col:
            grid[row].append("")
        grid[row][col] = value

    def _read(self, cell: Any, formulas: bool = False) -> Any:
        if not isinstance(cell, str):
            return cell
        if cell.startswith("'"):
            return cell[1:]
        if cell.startswith("="):
            return cell if formulas else self.formula_value
        if _ISO_DATE.match(cell):
            return _serial(date.fromisoformat(cell))
        if _ISO_DATETIME.match(cell):
            return _serial(datetime.fromisoformat(cell))
        if _NUMBER.match(cell):
            return float(cell) if "." in cell else int(cell)
        return cell


def _stored(value: Any) -> Any:
    """Fixture JSON value -> what the sheet holds (dates as serials, like typed-in dates)."""
    if isinstance(value, str) and _ISO_DATE.match(value):
        return _serial(date.fromisoformat(value))
    return value


class FakeSheetStore(SheetStore):
    """The real SheetStore over an in-memory sheet loaded from tests/fixtures/*.json."""

    def __init__(self, backend: FakeSheetsBackend | None = None):
        self.backend = backend or FakeSheetsBackend.from_fixtures()
        super().__init__(self.backend)


class FakeDriveStore:
    """In-memory Shared Drive: folders keyed by Source ID, files by name."""

    def __init__(self, fail_upload: str | None = None):
        self.folders: dict[str, dict[str, Any]] = {}  # folder id -> {name, source_id, files}
        self.fail_upload = fail_upload

    def task_folder(self, source_id: str, name: str) -> Folder:
        for folder_id, folder in self.folders.items():
            if folder["source_id"] == source_id:
                return Folder(folder_id, folder_url(folder_id))
        folder_id = f"folder-{len(self.folders) + 1}"
        self.folders[folder_id] = {"name": name, "source_id": source_id, "files": {}}
        return Folder(folder_id, folder_url(folder_id))

    def rename_folder(self, folder_id: str, name: str) -> None:
        self.folders[folder_id]["name"] = name

    def file_names(self, folder_id: str) -> set[str]:
        return set(self.folders[folder_id]["files"])

    def upload(self, folder_id: str, filename: str, mime: str, data: bytes) -> None:
        if filename == self.fail_upload:
            raise OSError("upload failed")
        self.folders[folder_id]["files"][filename] = data

    def only_folder(self) -> dict[str, Any]:
        [folder] = self.folders.values()
        return folder


class FakeAIProvider:
    """Returns a fixed AIResult (or raises / sleeps) and records what it was given."""

    name = "Fake"
    model = "fake-model"

    def __init__(
        self,
        result: AIResult | dict[str, Any] | None = None,
        error: Exception | None = None,
        delay: float = 0.0,
    ):
        if isinstance(result, dict):
            result = AIResult.model_validate_json(json.dumps(result, default=str))
        self.result = result
        self.error = error
        self.delay = delay
        self.calls: list[dict[str, Any]] = []

    def extract(
        self,
        masked_text: str,
        catalog: list[CatalogEntry],
        attachment_names: list[str],
        documents: Sequence[Attachment] = (),
    ) -> AIResult | None:
        import time

        self.calls.append(
            {
                "text": masked_text,
                "catalog": catalog,
                "attachments": attachment_names,
                "documents": [document.filename for document in documents],
            }
        )
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.result


class FakeGmailClient:
    """The bot mailbox in memory: messages with labels (INBOX/UNREAD included), threads,
    replies and sent mail. New messages default to an allowed sender, unread in the inbox."""

    def __init__(self, address: str = "grc-bot@example.com") -> None:
        from app.gmail import GmailMessage

        self._message_type = GmailMessage
        self.bot = address
        self.messages: dict[str, dict[str, Any]] = {}  # id -> {"message", "labels"}
        self.sent: list[tuple[Any, str]] = []
        self.replies: list[tuple[str, Any, str]] = []  # (original id, email, sender name)
        self.fail_reading: set[str] = set()
        self.fail_labelling = False
        self.fail_reply = False
        self.auth_broken = False
        self.stored: dict[str, list[Attachment]] = {}  # task ID -> files kept (GRC/Files)
        self.watched: list[str] = []  # Pub/Sub topics passed to watch()

    def add(self, message_id: str, labels: tuple[str, ...] = ("INBOX", "UNREAD"), **message: Any):
        values = {
            "id": message_id,
            "thread_id": f"thread-{message_id}",
            "received_at": datetime(2026, 10, 26, 10, 0),
            "sender_name": "GRC Manager",
            "sender_email": "grc-manager@example.com",
            "subject": "Request",
            "rfc_message_id": f"<{message_id}@mail.example.com>",
            **message,
        }
        self.messages[message_id] = {
            "message": self._message_type(**values),
            "labels": set(labels),
        }

    def labels(self, message_id: str) -> set[str]:
        return self.messages[message_id]["labels"]

    def _check(self) -> None:
        if self.auth_broken:
            from app.gmail import GmailAuthError

            raise GmailAuthError("RefreshError")

    def address(self) -> str:
        self._check()
        return self.bot

    def unread_inbox(self, limit: int) -> list[str]:
        self._check()
        unread = [mid for mid, m in self.messages.items() if {"INBOX", "UNREAD"} <= m["labels"]]
        return unread[:limit]

    def get_message(self, message_id: str) -> Any:
        self._check()
        if message_id in self.fail_reading:
            raise OSError("cannot read")
        return self.messages[message_id]["message"]

    def logged_message_ids(self, thread_id: str) -> list[str]:
        self._check()
        return [
            mid
            for mid, m in self.messages.items()
            if m["message"].thread_id == thread_id and "GRC/Logged" in m["labels"]
        ]

    def modify(self, message_id: str, add: list[str], remove: list[str]) -> None:
        self._check()
        if self.fail_labelling:
            raise OSError("cannot label")
        labels = self.messages[message_id]["labels"]
        labels.difference_update(remove)
        labels.update(add)

    def reply(self, original: Any, email: Any, sender_name: str) -> None:
        self._check()
        if self.fail_reply:
            raise OSError("cannot send")
        self.replies.append((original.id, email, sender_name))

    def send(self, email: Any, sender_name: str) -> None:
        self._check()
        self.sent.append((email, sender_name))

    def store_files(self, task_id: str, attachments: list[Attachment]) -> None:
        self._check()
        self.stored.setdefault(task_id, []).extend(attachments)

    def task_files(self, task_id: str, source_id: str = "") -> list[Attachment]:
        self._check()
        files: list[Attachment] = []
        if source_id in self.messages:
            files += list(self.messages[source_id]["message"].attachments)
        for mid, item in self.messages.items():
            message = item["message"]
            if mid != source_id and task_id in message.subject:
                files += list(message.attachments)
        return [*files, *self.stored.get(task_id, [])]

    def watch(self, topic: str) -> datetime | None:
        self._check()
        self.watched.append(topic)
        return datetime(2026, 11, 16, 9, 0, tzinfo=clock.TZ)


class FakeChatClient:
    """Chat attachment downloads from a dict of resource name -> bytes."""

    def __init__(self, files: dict[str, bytes] | None = None):
        self.files = {"resource-1": b"xlsx-bytes"} if files is None else files

    def download_attachment(self, resource_name: str) -> bytes:
        if resource_name not in self.files:
            raise OSError("download failed")
        return self.files[resource_name]


__all__ = [
    "FakeAIProvider",
    "FakeChatClient",
    "FakeDriveStore",
    "FakeGmailClient",
    "FakeSheetStore",
    "FakeSheetsBackend",
    "NotConfigured",
    "column_letter",
]


class FakeWhatsApp:
    """The WhatsApp Cloud API: records texts and templates; media bytes by ID."""

    def __init__(self, media: dict[str, bytes] | None = None):
        self.texts: list[tuple[str, str]] = []
        self.templates: list[tuple[str, str, str, list[str]]] = []
        self.media = media or {}
        self.fail_send = False

    def send_text(self, to: str, text: str) -> str | None:
        if self.fail_send:
            raise RuntimeError("send failed")
        self.texts.append((to, text))
        return f"bot-{len(self.texts)}"

    def send_template(self, to: str, name: str, language: str, params: list[str]) -> None:
        if self.fail_send:
            raise RuntimeError("send failed")
        self.templates.append((to, name, language, params))

    def download_media(self, media_id: str) -> bytes:
        if media_id not in self.media:
            raise KeyError(media_id)
        return self.media[media_id]

    def last_text(self, to: str) -> str:
        return [text for number, text in self.texts if number == to][-1]


class FakeQueue:
    """Cloud Tasks: records what was queued (tests run the tasks themselves)."""

    def __init__(self) -> None:
        self.tasks: list[tuple[dict[str, Any], float]] = []
        self.fail = False

    def enqueue(self, payload: dict[str, Any], delay_seconds: float = 0) -> None:
        if self.fail:
            raise RuntimeError("queue down")
        self.tasks.append((payload, delay_seconds))
