"""Fixture threads for the fake Gmail (fixtures/track/gmail/*.json). Invented people only."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from jobengine.settings import ROOT_DIR
from jobengine.track.models import Message

FIXTURES = ROOT_DIR / "fixtures" / "track"
# The fixture threads and rows are written for this moment.
FAKE_NOW = datetime.fromisoformat("2026-10-15T08:00:00+05:30")


def message(thread_id: str, raw: dict[str, Any]) -> Message:
    return Message(
        id=raw["id"], thread_id=thread_id, when=datetime.fromisoformat(raw["when"]),
        sender=raw["from"].lower(), subject=raw.get("subject", ""),
        snippet=raw.get("text", "")[:200], text=raw.get("text", ""),
        labels=tuple(raw.get("labels") or ()),
        headers={k.lower(): v for k, v in (raw.get("headers") or {}).items()},
    )


def load_threads(folder: Path = FIXTURES / "gmail") -> dict[str, list[Message]]:
    threads: dict[str, list[Message]] = {}
    for path in sorted(folder.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        tid = data["thread_id"]
        threads.setdefault(tid, []).extend(message(tid, m) for m in data["messages"])
    return threads
