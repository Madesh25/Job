"""Read-only Gmail access for job alert emails (the alerts inbox, madeshwaranm02).

Only users.messages.list and users.messages.get are used, with the gmail.readonly scope.
Nothing here labels, changes, sends or removes mail. The Google API client makes its own
HTTP calls, which is the one allowed exception to jobengine.http.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


@dataclass(frozen=True)
class GmailMessage:
    id: str
    sender: str
    subject: str
    html: str


def build_service(token_json: str) -> Any:
    """Gmail API service from an authorized-user token JSON (GMAIL_ALERTS_TOKEN_JSON)."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
    return build("gmail", "v1", credentials=creds, cache_discovery=False, static_discovery=True)


def _header(payload: dict[str, Any], name: str) -> str:
    for header in payload.get("headers") or []:
        if header.get("name", "").lower() == name.lower():
            return header.get("value", "")
    return ""


def _decode(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")


def html_part(payload: dict[str, Any]) -> str | None:
    """The first text/html body in a message payload, decoded."""
    if payload.get("mimeType") == "text/html" and (payload.get("body") or {}).get("data"):
        return _decode(payload["body"]["data"])
    for part in payload.get("parts") or []:
        found = html_part(part)
        if found is not None:
            return found
    return None


def to_message(raw: dict[str, Any]) -> GmailMessage | None:
    payload = raw.get("payload") or {}
    html = html_part(payload)
    if html is None:
        return None
    return GmailMessage(
        id=raw.get("id", ""),
        sender=_header(payload, "From"),
        subject=_header(payload, "Subject"),
        html=html,
    )


def fetch_messages(service: Any, query: str, max_messages: int = 100) -> list[GmailMessage]:
    """List messages matching query (newest first) and read their HTML bodies."""
    messages_api = service.users().messages()
    ids: list[str] = []
    page_token = None
    while len(ids) < max_messages:
        listing = messages_api.list(
            userId="me", q=query, maxResults=min(100, max_messages - len(ids)),
            pageToken=page_token,
        ).execute()
        ids.extend(item["id"] for item in listing.get("messages") or [])
        page_token = listing.get("nextPageToken")
        if not page_token:
            break
    result = []
    for message_id in ids[:max_messages]:
        raw = messages_api.get(userId="me", id=message_id, format="full").execute()
        message = to_message(raw)
        if message is not None:
            result.append(message)
    return result
