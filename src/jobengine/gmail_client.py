"""The only module that writes to Gmail: it creates drafts and (Module 07) changes labels.

Allowed calls: users.drafts.create, users.drafts.get, users.drafts.list, users.messages.get,
users.threads.get, users.labels.list and users.messages.modify (labels only). Nothing is ever
sent, removed or edited after creation; tests/test_repo_rules.py checks this file's text.
Like gmail_reader.py, it uses the Google API client, the allowed exception to jobengine.http.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from jobengine.settings import Settings

GMAIL_MODIFY = "https://www.googleapis.com/auth/gmail.modify"
DRIVE_FILE = "https://www.googleapis.com/auth/drive.file"
SENDER_SCOPES = [GMAIL_MODIFY, DRIVE_FILE]
NO_MODIFY = "Sender token lacks gmail.modify. Regenerate it with python -m jobengine.gmail_auth."
NO_TOKEN = "GMAIL_SENDER_TOKEN_JSON is not set, so no draft can be created."
USER = "me"


class GmailError(Exception):
    """Gmail cannot be used. The message is shown to you."""


@dataclass(frozen=True)
class Draft:
    draft_id: str
    thread_id: str
    message_id: str = ""


class Gmail(Protocol):
    def create_draft(self, raw: str) -> Draft: ...


def token_scopes(token_json: str) -> list[str]:
    info = json.loads(token_json)
    scopes = info.get("scopes") or info.get("scope") or []
    return scopes.split() if isinstance(scopes, str) else list(scopes)


def check_sender_token(token_json: str | None) -> dict[str, Any]:
    """The token info, or GmailError before any API call is made."""
    if not token_json:
        raise GmailError(NO_TOKEN)
    try:
        scopes = token_scopes(token_json)
    except (ValueError, AttributeError):
        raise GmailError("GMAIL_SENDER_TOKEN_JSON is not valid JSON.") from None
    if GMAIL_MODIFY not in scopes:
        raise GmailError(NO_MODIFY)
    return json.loads(token_json)


class GmailClient:
    def __init__(self, service: Any):
        self.service = service

    @classmethod
    def from_settings(cls, s: Settings) -> GmailClient:
        info = check_sender_token(s.gmail_sender_token_json)
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials.from_authorized_user_info(info, [GMAIL_MODIFY])
        return cls(build("gmail", "v1", credentials=creds, cache_discovery=False))

    def _drafts(self) -> Any:
        return self.service.users().drafts()

    def create_draft(self, raw: str) -> Draft:
        result = self._drafts().create(userId=USER, body={"message": {"raw": raw}}).execute()
        message = result.get("message") or {}
        return Draft(draft_id=result["id"], thread_id=message.get("threadId", ""),
                     message_id=message.get("id", ""))

    def get_draft(self, draft_id: str) -> dict[str, Any]:
        return self._drafts().get(userId=USER, id=draft_id, format="minimal").execute()

    def list_drafts(self, query: str | None = None) -> list[dict[str, Any]]:
        return self._drafts().list(userId=USER, q=query).execute().get("drafts") or []

    def get_message(self, message_id: str) -> dict[str, Any]:
        return self.service.users().messages().get(userId=USER, id=message_id,
                                                   format="metadata").execute()

    def get_thread(self, thread_id: str) -> dict[str, Any]:
        return self.service.users().threads().get(userId=USER, id=thread_id,
                                                  format="metadata").execute()

    def list_labels(self) -> list[dict[str, Any]]:
        return self.service.users().labels().list(userId=USER).execute().get("labels") or []

    def modify_labels(self, message_id: str, add: list[str], remove: list[str]) -> None:
        """Labels only (Module 07)."""
        self.service.users().messages().modify(
            userId=USER, id=message_id,
            body={"addLabelIds": add, "removeLabelIds": remove}).execute()


@dataclass
class FakeGmail:
    """In-memory Gmail: records every draft and returns invented IDs."""

    drafts: list[dict[str, str]] = field(default_factory=list)

    def create_draft(self, raw: str) -> Draft:
        n = len(self.drafts) + 1
        draft = Draft(draft_id=f"r-fake-draft-{n}", thread_id=f"fake-thread-{n}",
                      message_id=f"fake-message-{n}")
        self.drafts.append({"raw": raw, "id": draft.draft_id, "thread": draft.thread_id})
        return draft
