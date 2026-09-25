import json

import pytest

from jobengine.drive_client import DriveError, FakeDrive, GoogleDrive, file_id
from jobengine.gmail_client import (
    GMAIL_MODIFY,
    NO_MODIFY,
    FakeGmail,
    GmailClient,
    GmailError,
    check_sender_token,
)
from jobengine.settings import load_settings

READONLY = "https://www.googleapis.com/auth/gmail.readonly"


def token(*scopes):
    return json.dumps({"client_id": "id.example", "client_secret": "not-a-secret",
                       "refresh_token": "invented", "token_uri": "https://oauth2.example.com",
                       "scopes": list(scopes)})


class Recorder:
    """A fake Google service: records the chain of calls."""

    def __init__(self, result=None):
        self.calls = []
        self.result = result or {}

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, kwargs))
            return self
        return call

    def execute(self):
        return self.result


def test_token_without_modify_fails_before_any_api_call(monkeypatch):
    def no_build(*args, **kwargs):
        raise AssertionError("the API client must not be built")

    monkeypatch.setattr("googleapiclient.discovery.build", no_build)
    s = load_settings("local", {"GMAIL_SENDER_TOKEN_JSON": token(READONLY)})
    with pytest.raises(GmailError) as exc:
        GmailClient.from_settings(s)
    assert str(exc.value) == NO_MODIFY
    with pytest.raises(GmailError, match="not set"):
        check_sender_token(None)
    assert check_sender_token(token(GMAIL_MODIFY))["refresh_token"] == "invented"
    space = json.dumps({"scope": f"{READONLY} {GMAIL_MODIFY}"})
    assert check_sender_token(space)


def test_create_draft_uses_drafts_create():
    service = Recorder({"id": "d-1", "message": {"id": "m-1", "threadId": "t-1"}})
    draft = GmailClient(service).create_draft("cmF3")
    assert (draft.draft_id, draft.thread_id) == ("d-1", "t-1")
    names = [name for name, _ in service.calls]
    assert names == ["users", "drafts", "create"]
    assert service.calls[-1][1] == {"userId": "me", "body": {"message": {"raw": "cmF3"}}}


def test_fake_gmail_records():
    gmail = FakeGmail()
    assert gmail.create_draft("x").draft_id == "r-fake-draft-1"
    assert len(gmail.drafts) == 1


def test_drive_download():
    assert file_id("https://drive.google.com/file/d/1AbCdEfGhIjKlMn/view?usp=drivesdk") == \
        "1AbCdEfGhIjKlMn"
    assert file_id("https://drive.google.com/open?id=1AbCdEfGhIjKlMn") == "1AbCdEfGhIjKlMn"
    with pytest.raises(DriveError):
        file_id("https://example.com/nothing")
    service = Recorder(b"%PDF")
    drive = GoogleDrive(service, "folder")
    assert drive.download("https://drive.google.com/file/d/1AbCdEfGhIjKlMn/view") == b"%PDF"
    assert ("get_media", {"fileId": "1AbCdEfGhIjKlMn"}) in service.calls
    fake = FakeDrive()
    link = fake.upload_pdf("a.pdf", b"123")
    assert fake.download(link) == b"123"
