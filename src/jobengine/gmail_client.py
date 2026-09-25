"""The only module that writes to Gmail: it creates drafts and (Module 07) changes labels.

Allowed calls: users.drafts.create, users.drafts.get, users.drafts.list, users.messages.list,
users.messages.get, users.threads.get, users.labels.list and users.messages.modify (labels
only). Nothing is ever sent, removed or edited after creation, and labels are never created;
tests/test_repo_rules.py checks this file's text.
Like gmail_reader.py, it uses the Google API client, the allowed exception to jobengine.http.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parseaddr
from typing import Any, Protocol

from bs4 import BeautifulSoup

from jobengine.settings import Settings
from jobengine.track.models import Message

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
    def create_draft(self, raw: str, thread_id: str | None = None) -> Draft: ...


class TrackGmail(Gmail, Protocol):
    """What the daily tracking run reads and changes (Module 07)."""

    def draft_exists(self, draft_id: str) -> bool: ...

    def thread(self, thread_id: str) -> list[Message]: ...

    def search(self, query: str, limit: int = 100) -> list[Message]: ...

    def labels(self) -> dict[str, str]: ...

    def label_thread(self, thread_id: str, label_id: str) -> None: ...


# ---------------------------------------------------------------- message parsing


def _decode(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")


def _parts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out = [payload]
    for part in payload.get("parts") or []:
        out.extend(_parts(part))
    return out


def body_text(payload: dict[str, Any]) -> str:
    """text/plain parts (and delivery status parts), else text/html turned into text."""
    parts = _parts(payload)
    plain = [_decode(p["body"]["data"]) for p in parts
             if p.get("mimeType") in ("text/plain", "message/delivery-status")
             and (p.get("body") or {}).get("data")]
    if plain:
        return "\n".join(plain)
    html = [_decode(p["body"]["data"]) for p in parts
            if p.get("mimeType") == "text/html" and (p.get("body") or {}).get("data")]
    return "\n".join(BeautifulSoup(h, "html.parser").get_text("\n") for h in html)


def to_message(raw: dict[str, Any]) -> Message:
    """A Gmail API message (format full) as a tracking Message."""
    payload = raw.get("payload") or {}
    headers = {h.get("name", "").lower(): h.get("value", "")
               for h in payload.get("headers") or []}
    when = datetime.fromtimestamp(int(raw.get("internalDate") or 0) / 1000, tz=UTC)
    return Message(
        id=raw.get("id", ""), thread_id=raw.get("threadId", ""), when=when,
        sender=parseaddr(headers.get("from", ""))[1].lower(), subject=headers.get("subject", ""),
        snippet=raw.get("snippet", ""), text=body_text(payload),
        labels=tuple(raw.get("labelIds") or ()), headers=headers,
    )


def refresh_error(token_json: str | None) -> str | None:
    """None when the token refreshes, otherwise a short reason. No Gmail call is made."""
    if not token_json:
        return "not set"
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        creds = Credentials.from_authorized_user_info(json.loads(token_json))
        creds.refresh(Request())
    except Exception as exc:  # any failure is reported, never raised
        reason = re.sub(r"\s+", " ", str(exc) or type(exc).__name__)
        return reason[:200]
    return None


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

    def create_draft(self, raw: str, thread_id: str | None = None) -> Draft:
        message: dict[str, Any] = {"raw": raw}
        if thread_id:
            message["threadId"] = thread_id  # a reply in that thread (follow-ups)
        result = self._drafts().create(userId=USER, body={"message": message}).execute()
        created = result.get("message") or {}
        return Draft(draft_id=result["id"], thread_id=created.get("threadId", ""),
                     message_id=created.get("id", ""))

    def get_draft(self, draft_id: str) -> dict[str, Any]:
        return self._drafts().get(userId=USER, id=draft_id, format="minimal").execute()

    def draft_exists(self, draft_id: str) -> bool:
        from googleapiclient.errors import HttpError

        try:
            self.get_draft(draft_id)
        except HttpError as exc:
            if getattr(exc.resp, "status", None) in (404, 400):
                return False
            raise
        return True

    def thread(self, thread_id: str) -> list[Message]:
        data = self.service.users().threads().get(userId=USER, id=thread_id,
                                                  format="full").execute()
        return sorted((to_message(m) for m in data.get("messages") or []),
                      key=lambda m: m.when)

    def search(self, query: str, limit: int = 100) -> list[Message]:
        ids: list[str] = []
        token = None
        while len(ids) < limit:
            data = self.service.users().messages().list(
                userId=USER, q=query, maxResults=min(100, limit - len(ids)),
                pageToken=token).execute()
            ids.extend(m["id"] for m in data.get("messages") or [])
            token = data.get("nextPageToken")
            if not token:
                break
        return [to_message(self.service.users().messages().get(
            userId=USER, id=i, format="full").execute()) for i in ids]

    def labels(self) -> dict[str, str]:
        return {label["name"]: label["id"] for label in self.list_labels()}

    def label_thread(self, thread_id: str, label_id: str) -> None:
        for message in self.thread(thread_id):
            self.modify_labels(message.id, [label_id], [])

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
    """In-memory Gmail: records every draft and returns invented IDs. For tracking it also
    holds threads of Messages, the open draft IDs and the existing labels."""

    drafts: list[dict[str, str]] = field(default_factory=list)
    threads: dict[str, list[Message]] = field(default_factory=dict)
    open_drafts: set[str] = field(default_factory=set)
    label_ids: dict[str, str] = field(default_factory=dict)
    label_calls: list[tuple[str, str]] = field(default_factory=list)  # (thread, label id)
    queries: list[str] = field(default_factory=list)

    def create_draft(self, raw: str, thread_id: str | None = None) -> Draft:
        n = len(self.drafts) + 1
        draft = Draft(draft_id=f"r-fake-draft-{n}", thread_id=thread_id or f"fake-thread-{n}",
                      message_id=f"fake-message-{n}")
        self.drafts.append({"raw": raw, "id": draft.draft_id, "thread": draft.thread_id})
        self.open_drafts.add(draft.draft_id)
        return draft

    def draft_exists(self, draft_id: str) -> bool:
        return draft_id in self.open_drafts

    def thread(self, thread_id: str) -> list[Message]:
        return sorted(self.threads.get(thread_id, []), key=lambda m: m.when)

    def search(self, query: str, limit: int = 100) -> list[Message]:
        self.queries.append(query)
        found = [m for msgs in self.threads.values() for m in msgs if matches_query(m, query)]
        return sorted(found, key=lambda m: m.when)[:limit]

    def labels(self) -> dict[str, str]:
        return dict(self.label_ids)

    def label_thread(self, thread_id: str, label_id: str) -> None:
        self.label_calls.append((thread_id, label_id))


def matches_query(message: Message, query: str) -> bool:
    """The small part of Gmail search the fake needs: after:<epoch>, from:(a OR b) and
    -in:chats."""
    after = re.search(r"after:(\d+)", query)
    if after and message.when.timestamp() < int(after.group(1)):
        return False
    sender = re.search(r"from:\(([^)]*)\)", query)
    if sender:
        terms = [t.strip().casefold() for t in sender.group(1).split(" OR ") if t.strip()]
        if not any(term in message.sender for term in terms):
            return False
    return True
