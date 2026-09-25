"""Google Drive upload for approved resumes, with the `drive.file` scope only.

Only files.list (find the folder or a name clash), files.create (folder and PDF) and files.get
(metadata) are used. Nothing is deleted, overwritten, shared or given new permissions. The
app can only see the files and folders it created, which is what `drive.file` allows. Like
gmail_reader.py, this module talks to Google through the Google API client, the allowed
exception to jobengine.http.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from jobengine.settings import Settings

DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
FOLDER_MIME = "application/vnd.google-apps.folder"
PDF_MIME = "application/pdf"
NO_SCOPE = "Sender token has no drive.file scope. Regenerate the token (Module 06 section 1)."
NO_TOKEN = "GMAIL_SENDER_TOKEN_JSON is not set, so the resume cannot be saved to Drive."
DEFAULT_FOLDER = "Job Engine Resumes"


class DriveError(Exception):
    """The upload cannot happen. The message is shown to you."""


class Drive(Protocol):
    def upload_pdf(self, name: str, data: bytes) -> str:
        """Upload into the resume folder and return the web view link."""
        ...


def folder_name(s: Settings, configured: str | None = None) -> str:
    name = configured or s.drive.get("resume_folder") or DEFAULT_FOLDER
    return name if s.app_env == "prod" else f"{name} (DEV)"


def free_name(name: str, taken: set[str]) -> str:
    """`name`, or `stem (2).pdf`, `stem (3).pdf`... when a file with that name exists."""
    if name not in taken:
        return name
    stem, dot, ext = name.rpartition(".")
    stem, ext = (stem, f".{ext}") if dot else (name, "")
    n = 2
    while f"{stem} ({n}){ext}" in taken:
        n += 1
    return f"{stem} ({n}){ext}"


def _quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


class GoogleDrive:
    def __init__(self, service: Any, folder: str):
        self.service = service
        self.folder = folder
        self._folder_id: str | None = None

    @classmethod
    def from_settings(cls, s: Settings, configured_folder: str | None = None) -> GoogleDrive:
        if not s.gmail_sender_token_json:
            raise DriveError(NO_TOKEN)
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        info = json.loads(s.gmail_sender_token_json)
        scopes = info.get("scopes") or info.get("scope") or []
        if isinstance(scopes, str):
            scopes = scopes.split()
        if DRIVE_FILE_SCOPE not in scopes:
            raise DriveError(NO_SCOPE)
        creds = Credentials.from_authorized_user_info(info, [DRIVE_FILE_SCOPE])
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        return cls(service, folder_name(s, configured_folder))

    def _list(self, query: str) -> list[dict[str, Any]]:
        result = self.service.files().list(
            q=query, spaces="drive", fields="files(id, name)", pageSize=100
        ).execute()
        return result.get("files") or []

    def folder_id(self) -> str:
        if self._folder_id:
            return self._folder_id
        found = self._list(f"name = '{_quote(self.folder)}' and mimeType = '{FOLDER_MIME}' "
                           "and trashed = false")
        if found:
            self._folder_id = found[0]["id"]
        else:
            created = self.service.files().create(
                body={"name": self.folder, "mimeType": FOLDER_MIME}, fields="id"
            ).execute()
            self._folder_id = created["id"]
        return self._folder_id

    def upload_pdf(self, name: str, data: bytes) -> str:
        from googleapiclient.http import MediaInMemoryUpload

        folder = self.folder_id()
        stem = re.sub(r"\.pdf$", "", name)
        taken = {f["name"] for f in self._list(
            f"'{folder}' in parents and name contains '{_quote(stem)}' and trashed = false")}
        media = MediaInMemoryUpload(data, mimetype=PDF_MIME, resumable=False)
        created = self.service.files().create(
            body={"name": free_name(name, taken), "parents": [folder]},
            media_body=media, fields="id",
        ).execute()
        meta = self.service.files().get(fileId=created["id"], fields="webViewLink").execute()
        return meta["webViewLink"]


@dataclass
class FakeDrive:
    """In-memory Drive: stores bytes and returns fake links."""

    folder: str = f"{DEFAULT_FOLDER} (DEV)"
    files: dict[str, bytes] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def upload_pdf(self, name: str, data: bytes) -> str:
        name = free_name(name, set(self.files))
        self.files[name] = data
        self.calls.append(name)
        return f"https://drive.example.com/{len(self.files)}/{name}"
