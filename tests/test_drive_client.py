import json
import re

import pytest

from jobengine.drive_client import (
    DRIVE_FILE_SCOPE,
    NO_SCOPE,
    NO_TOKEN,
    DriveError,
    FakeDrive,
    GoogleDrive,
    folder_name,
    free_name,
)
from jobengine.settings import load_settings


class FakeRequest:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


class FakeFiles:
    def __init__(self, existing_folder=None, names=()):
        self.calls = []
        self.existing_folder = existing_folder
        self.names = list(names)

    def list(self, **kwargs):
        self.calls.append(("list", kwargs))
        if "mimeType" in kwargs["q"]:
            found = [{"id": self.existing_folder, "name": "f"}] if self.existing_folder else []
            return FakeRequest({"files": found})
        return FakeRequest({"files": [{"id": "x", "name": n} for n in self.names]})

    def create(self, **kwargs):
        self.calls.append(("create", kwargs))
        return FakeRequest({"id": "folder-1" if "media_body" not in kwargs else "file-1"})

    def get(self, **kwargs):
        self.calls.append(("get", kwargs))
        return FakeRequest({"webViewLink": f"https://drive.example.com/{kwargs['fileId']}"})


class FakeService:
    def __init__(self, files):
        self._files = files

    def files(self):
        return self._files


def test_free_name():
    assert free_name("a.pdf", set()) == "a.pdf"
    assert free_name("a.pdf", {"a.pdf"}) == "a (2).pdf"
    assert free_name("a.pdf", {"a.pdf", "a (2).pdf"}) == "a (3).pdf"


def test_folder_name_has_dev_suffix_outside_prod():
    assert folder_name(load_settings("local", {})) == "Job Engine Resumes (DEV)"
    assert folder_name(load_settings("prod", {})) == "Job Engine Resumes"
    assert folder_name(load_settings("dev", {}), "Other") == "Other (DEV)"


def test_upload_creates_folder_once_and_never_overwrites():
    files = FakeFiles(names=["Alex_Devops_X.pdf"])
    drive = GoogleDrive(FakeService(files), "Job Engine Resumes (DEV)")
    link = drive.upload_pdf("Alex_Devops_X.pdf", b"%PDF")
    assert link == "https://drive.example.com/file-1"
    creates = [c for c in files.calls if c[0] == "create"]
    assert creates[0][1]["body"] == {"name": "Job Engine Resumes (DEV)",
                                     "mimeType": "application/vnd.google-apps.folder"}
    assert creates[1][1]["body"] == {"name": "Alex_Devops_X (2).pdf", "parents": ["folder-1"]}
    assert {c[0] for c in files.calls} <= {"list", "create", "get"}


def test_existing_folder_is_reused():
    files = FakeFiles(existing_folder="folder-9")
    GoogleDrive(FakeService(files), "Job Engine Resumes").upload_pdf("a.pdf", b"x")
    assert [c[1]["body"] for c in files.calls if c[0] == "create"] == [
        {"name": "a.pdf", "parents": ["folder-9"]}]


def test_token_checks():
    with pytest.raises(DriveError, match=re.escape(NO_TOKEN)):
        GoogleDrive.from_settings(load_settings("local", {}))
    token = json.dumps({"scopes": ["https://www.googleapis.com/auth/gmail.compose"],
                        "client_id": "x", "client_secret": "y", "refresh_token": "z"})
    s = load_settings("local", {"GMAIL_SENDER_TOKEN_JSON": token})
    with pytest.raises(DriveError, match=re.escape(NO_SCOPE)):
        GoogleDrive.from_settings(s)
    assert DRIVE_FILE_SCOPE.endswith("/drive.file")


def test_fake_drive():
    drive = FakeDrive()
    assert drive.upload_pdf("a.pdf", b"1") == "https://drive.example.com/1/a.pdf"
    assert drive.upload_pdf("a.pdf", b"2") == "https://drive.example.com/2/a (2).pdf"
