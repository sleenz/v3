"""Task folders in the Shared Drive and file uploads (DESIGN section 4 step 8).

A folder carries the request's Source ID as a Drive app property, so a retried request reuses its
folder instead of creating a second one, and a file already in the folder is not uploaded again.
"""

from __future__ import annotations

import io
import threading
from dataclasses import dataclass
from typing import Any, Protocol

FOLDER_MIME = "application/vnd.google-apps.folder"
SOURCE_PROPERTY = "grcSourceId"
MAX_FOLDER_NAME = 120


@dataclass(frozen=True)
class Folder:
    id: str
    url: str


class DriveStore(Protocol):
    def task_folder(self, source_id: str, name: str) -> Folder:
        """The folder for `source_id`, created with `name` if it does not exist yet."""
        ...

    def rename_folder(self, folder_id: str, name: str) -> None: ...

    def file_names(self, folder_id: str) -> set[str]: ...

    def upload(self, folder_id: str, filename: str, mime: str, data: bytes) -> None: ...


def folder_name(prefix: str, title: str) -> str:
    """`G-0012 – <title>`, kept to a sensible length and free of slashes."""
    name = f"{prefix} – {title}".replace("/", "-").replace("\n", " ").strip()
    return name if len(name) <= MAX_FOLDER_NAME else name[: MAX_FOLDER_NAME - 1].rstrip() + "…"


def folder_url(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}"


class NoFileStorage:
    """Config `File storage = None`: the pipeline never calls Drive; any call is a bug."""

    def task_folder(self, source_id: str, name: str) -> Folder:
        raise RuntimeError("File storage is None")

    def rename_folder(self, folder_id: str, name: str) -> None:
        raise RuntimeError("File storage is None")

    def file_names(self, folder_id: str) -> set[str]:
        raise RuntimeError("File storage is None")

    def upload(self, folder_id: str, filename: str, mime: str, data: bytes) -> None:
        raise RuntimeError("File storage is None")


class GoogleDriveStore:
    """Drive API v3 through google-api-python-client, one client per thread, Shared Drive aware."""

    # drive.file is enough: the service only touches folders and files it created itself.
    SCOPES = ("https://www.googleapis.com/auth/drive.file",)

    def __init__(self, parent_folder_id: str, credentials: Any = None):
        self._parent = parent_folder_id
        self._credentials = credentials
        self._local = threading.local()

    def _files(self) -> Any:
        if not hasattr(self._local, "service"):
            import google.auth
            from googleapiclient.discovery import build

            credentials = self._credentials
            if credentials is None:
                credentials, _ = google.auth.default(scopes=list(self.SCOPES))
            self._local.service = build(
                "drive", "v3", credentials=credentials, cache_discovery=False
            )
        return self._local.service.files()

    def task_folder(self, source_id: str, name: str) -> Folder:
        escaped = source_id.replace("\\", "\\\\").replace("'", "\\'")
        query = (
            f"mimeType = '{FOLDER_MIME}' and trashed = false and '{self._parent}' in parents "
            f"and appProperties has {{ key='{SOURCE_PROPERTY}' and value='{escaped}' }}"
        )
        found = (
            self._files()
            .list(
                q=query,
                fields="files(id)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                corpora="allDrives",
                pageSize=1,
            )
            .execute()
            .get("files", [])
        )
        if found:
            return Folder(found[0]["id"], folder_url(found[0]["id"]))
        created = (
            self._files()
            .create(
                body={
                    "name": name,
                    "mimeType": FOLDER_MIME,
                    "parents": [self._parent],
                    "appProperties": {SOURCE_PROPERTY: source_id},
                },
                fields="id",
                supportsAllDrives=True,
            )
            .execute()
        )
        return Folder(created["id"], folder_url(created["id"]))

    def rename_folder(self, folder_id: str, name: str) -> None:
        self._files().update(
            fileId=folder_id, body={"name": name}, supportsAllDrives=True
        ).execute()

    def file_names(self, folder_id: str) -> set[str]:
        names: set[str] = set()
        page_token = None
        while True:
            response = (
                self._files()
                .list(
                    q=f"'{folder_id}' in parents and trashed = false",
                    fields="nextPageToken, files(name)",
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                    corpora="allDrives",
                    pageToken=page_token,
                )
                .execute()
            )
            names.update(item["name"] for item in response.get("files", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                return names

    def upload(self, folder_id: str, filename: str, mime: str, data: bytes) -> None:
        from googleapiclient.http import MediaIoBaseUpload

        media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime or "application/octet-stream")
        self._files().create(
            body={"name": filename, "parents": [folder_id]},
            media_body=media,
            fields="id",
            supportsAllDrives=True,
        ).execute()
