"""A tiny key/value store for bot state that must survive between Telegram updates.

Cloud Run keeps nothing in memory, so state lives in the Notion data source Bot State (DEV)
in local and dev: Key (title), Value (text, a JSON string), Updated (date). Writes go through
safety.notion_write_target("bot_state"); prod has no Bot State database until Module 09.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any, Protocol

from jobengine.notion_repo import NotionClient, notion_value, page_values
from jobengine.safety import notion_write_target
from jobengine.settings import Settings

TEXT_ITEM_CHARS = 2000
MAX_TEXT_ITEMS = 100

log = logging.getLogger("jobengine.bot_state")


class BotState(Protocol):
    def get(self, key: str) -> dict[str, Any] | None: ...

    def set(self, key: str, value: dict[str, Any]) -> None: ...

    def delete(self, key: str) -> None: ...


class NotionBotState:
    def __init__(self, client: NotionClient, data_source_id: str, today=date.today):
        self.client = client
        self.data_source_id = data_source_id
        self._today = today

    def _find(self, key: str) -> dict[str, Any] | None:
        body = {"filter": {"property": "Key", "title": {"equals": key}}}
        for page in self.client.query(self.data_source_id, body):
            return page
        return None

    def get(self, key: str) -> dict[str, Any] | None:
        page = self._find(key)
        if page is None:
            return None
        raw = page_values(page).get("Value") or ""
        try:
            value = json.loads(raw)
        except ValueError:
            log.warning("bot_state %s holds invalid JSON, ignoring it", key)
            return None
        return value if isinstance(value, dict) else None

    def set(self, key: str, value: dict[str, Any]) -> None:
        props = {
            "Value": _long_text(json.dumps(value, sort_keys=True, ensure_ascii=False)),
            "Updated": notion_value("date", self._today()),
        }
        page = self._find(key)
        if page is None:
            props["Key"] = notion_value("title", key)
            self.client.request("POST", "/pages", {
                "parent": {"type": "data_source_id", "data_source_id": self.data_source_id},
                "properties": props,
            })
        else:
            self.client.request("PATCH", f"/pages/{page['id']}", {"properties": props})

    def delete(self, key: str) -> None:
        page = self._find(key)
        if page is not None:
            self.client.request("PATCH", f"/pages/{page['id']}", {"in_trash": True})


def _long_text(text: str) -> dict[str, Any]:
    """A rich text value longer than 2000 characters: Notion takes up to 100 items of 2000."""
    items = [text[i:i + TEXT_ITEM_CHARS] for i in range(0, len(text), TEXT_ITEM_CHARS)]
    if len(items) > MAX_TEXT_ITEMS:
        raise ValueError(f"bot_state value too long ({len(text)} characters)")
    return {"rich_text": [{"type": "text", "text": {"content": item}} for item in items]}


class FakeBotState:
    """In memory, for tests and --fake runs."""

    def __init__(self) -> None:
        self.values: dict[str, dict[str, Any]] = {}

    def get(self, key: str) -> dict[str, Any] | None:
        value = self.values.get(key)
        return dict(value) if value is not None else None

    def set(self, key: str, value: dict[str, Any]) -> None:
        self.values[key] = json.loads(json.dumps(value))

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


def bot_state_for(s: Settings, client: NotionClient) -> NotionBotState | None:
    """The Bot State store for this env, or None (with a DRY RUN log) if not writable."""
    target = notion_write_target("bot_state", s)
    if target is None:
        log.warning("DRY RUN: would write to bot_state")
        return None
    return NotionBotState(client, target)
