"""The alerts token keeps its own scopes: a gmail.modify token reads alerts too."""

import json

import pytest

from jobengine import gmail_reader

BASE = {"client_id": "id.apps.example.com", "client_secret": "not-a-secret",
        "refresh_token": "invented", "token_uri": "https://oauth2.googleapis.com/token"}


def token(scopes):
    return json.dumps({**BASE, "scopes": scopes})


def test_modify_token_is_used_with_its_own_scopes(monkeypatch):
    seen = {}
    from google.oauth2 import credentials

    real = credentials.Credentials.from_authorized_user_info

    def spy(info, scopes=None):
        seen["scopes"] = scopes
        return real(info, scopes)

    monkeypatch.setattr(credentials.Credentials, "from_authorized_user_info", spy)
    modify = ["https://www.googleapis.com/auth/gmail.modify",
              "https://www.googleapis.com/auth/drive.file"]
    gmail_reader.build_service(token(modify))
    assert seen["scopes"] is None  # never asks Google for gmail.readonly


def test_readonly_and_space_separated_scopes():
    gmail_reader.build_service(token("https://www.googleapis.com/auth/gmail.readonly"))
    assert gmail_reader.token_scopes({"scopes": "a b"}) == ["a", "b"]


def test_a_token_that_cannot_read_mail_says_so():
    with pytest.raises(gmail_reader.TokenScopeError, match="cannot read mail"):
        gmail_reader.build_service(token(["https://www.googleapis.com/auth/drive.file"]))
