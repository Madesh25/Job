import json
import logging
from datetime import date

import httpx
import pytest

from jobengine import http
from jobengine.bot_state import FakeBotState, bot_state_for
from jobengine.notion_repo import NotionClient
from jobengine.settings import load_settings

BOT_STATE_DEV = "41fca14e-5236-4b78-8f74-0fe9d8199a41"


def value_text(value):
    items = value["rich_text"]
    assert all(len(item["text"]["content"]) <= 2000 for item in items)
    return "".join(item["text"]["content"] for item in items)


class FakeNotionState:
    def __init__(self):
        self.pages = {}
        self.requests = []

    def __call__(self, request):
        body = json.loads(request.content) if request.content else {}
        self.requests.append((request.method, request.url.path, body))
        path = request.url.path
        if path.endswith("/query"):
            key = body["filter"]["title"]["equals"]
            results = [
                {"id": pid, "properties": {
                    "Key": {"type": "title", "title": [{"plain_text": k}]},
                    "Value": {"type": "rich_text", "rich_text": [
                        {"plain_text": v[i:i + 2000]} for i in range(0, len(v), 2000)]},
                }}
                for pid, (k, v) in self.pages.items() if k == key
            ]
            return httpx.Response(200, json={"results": results, "has_more": False})
        if request.method == "POST" and path == "/v1/pages":
            pid = f"p{len(self.pages) + 1}"
            props = body["properties"]
            self.pages[pid] = (props["Key"]["title"][0]["text"]["content"],
                               value_text(props["Value"]))
            return httpx.Response(200, json={"id": pid})
        if request.method == "PATCH":
            pid = path.rsplit("/", 1)[1]
            if body.get("in_trash"):
                self.pages.pop(pid, None)
            else:
                key = self.pages[pid][0]
                self.pages[pid] = (key, value_text(body["properties"]["Value"]))
            return httpx.Response(200, json={"id": pid})
        return httpx.Response(404, json={})


@pytest.fixture
def notion():
    fake = FakeNotionState()
    http.set_transport(httpx.MockTransport(fake))
    yield fake
    http.set_transport(None)


def test_notion_bot_state_round_trip(notion):
    s = load_settings("dev", {})
    state = bot_state_for(s, NotionClient("t", s, sleep=lambda _: None))
    assert state.data_source_id == BOT_STATE_DEV
    state._today = lambda: date(2026, 10, 1)
    assert state.get("jd_capture") is None
    state.set("jd_capture", {"page_id": "abc", "chars": 10})
    assert state.get("jd_capture") == {"page_id": "abc", "chars": 10}
    state.set("jd_capture", {"page_id": "abc", "chars": 25})
    assert state.get("jd_capture")["chars"] == 25
    assert len(notion.pages) == 1  # updated in place, not duplicated
    create = [r for r in notion.requests if r[0] == "POST" and r[1] == "/v1/pages"][0]
    assert create[2]["parent"]["data_source_id"] == BOT_STATE_DEV
    assert create[2]["properties"]["Updated"] == {"date": {"start": "2026-10-01"}}
    state.delete("jd_capture")
    assert state.get("jd_capture") is None


def test_prod_has_no_bot_state_yet(caplog):
    s = load_settings("prod", {})
    with caplog.at_level(logging.WARNING):
        assert bot_state_for(s, NotionClient("t", s)) is None
    assert "DRY RUN: would write to bot_state" in caplog.text


def test_fake_bot_state_copies_values():
    state = FakeBotState()
    value = {"a": [1]}
    state.set("k", value)
    value["a"].append(2)
    assert state.get("k") == {"a": [1]}
    state.delete("k")
    state.delete("k")
    assert state.get("k") is None


def test_long_values_are_split_into_2000_character_items(notion):
    s = load_settings("dev", {})
    state = bot_state_for(s, NotionClient("t", s, sleep=lambda _: None))
    parts = ["x" * 3000, "y" * 3000]
    state.set("jd_capture", {"parts": parts})
    assert state.get("jd_capture") == {"parts": parts}
    with pytest.raises(ValueError, match="too long"):
        state.set("jd_capture", {"parts": ["z" * 200_001]})
