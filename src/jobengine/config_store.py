"""The Notion Config data source (read only), shared by every module.

Each row has Key, Value, Notes, Type and Updated. ConfigStore.load reads them through the
Notion client; ConfigStore.fake reads fixtures/config.json for tests and --fake runs.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from jobengine.settings import ROOT_DIR, Settings

if TYPE_CHECKING:
    from jobengine.notion_repo import NotionClient

FIXTURE = ROOT_DIR / "fixtures" / "config.json"
PROPERTIES = ("Key", "Value", "Notes", "Type", "Updated")


@dataclass(frozen=True)
class ConfigRow:
    key: str
    value: str
    notes: str = ""
    type: str | None = None
    updated: date | None = None
    page_id: str | None = None


class ConfigStore:
    def __init__(self, rows: dict[str, ConfigRow]):
        self._rows = rows

    # ------------------------------------------------------------ builders

    @classmethod
    def load(cls, client: NotionClient, s: Settings) -> ConfigStore:
        from jobengine.notion_repo import page_values

        rows = {}
        for page in client.query(s.notion_read["config"], properties=PROPERTIES):
            values = page_values(page)
            key = (values.get("Key") or "").strip()
            if key:
                rows[key] = ConfigRow(
                    key=key,
                    value=values.get("Value") or "",
                    notes=values.get("Notes") or "",
                    type=values.get("Type"),
                    updated=values.get("Updated"),
                    page_id=page.get("id"),
                )
        return cls(rows)

    @classmethod
    def fake(cls, path: Path = FIXTURE) -> ConfigStore:
        rows = {}
        for item in json.loads(path.read_text(encoding="utf-8")):
            updated = item.get("Updated")
            rows[item["Key"]] = ConfigRow(
                key=item["Key"],
                value=item.get("Value") or "",
                notes=item.get("Notes") or "",
                type=item.get("Type"),
                updated=date.fromisoformat(updated) if updated else None,
            )
        return cls(rows)

    @classmethod
    def from_values(cls, values: dict[str, str]) -> ConfigStore:
        return cls({key: ConfigRow(key=key, value=value) for key, value in values.items()})

    # ------------------------------------------------------------ readers

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self._rows.get(key)
        if row is None or row.value.strip() == "":
            return default
        return row.value.strip()

    def get_int(self, key: str, default: int | None = None) -> int | None:
        value = self.get(key)
        if value is None:
            return default
        match = re.match(r"\s*(-?\d+)", value)
        return int(match.group(1)) if match else default

    def get_date_prefix(self, key: str) -> date | None:
        """The leading YYYY-MM-DD of a value like "2026-09-23 (initial setup)"."""
        match = re.match(r"\s*(\d{4}-\d{2}-\d{2})", self.get(key) or "")
        if not match:
            return None
        try:
            return date.fromisoformat(match.group(1))
        except ValueError:
            return None

    def page_id(self, key: str) -> str | None:
        row = self._rows.get(key)
        return row.page_id if row else None

    def updated(self, key: str) -> date | None:
        row = self._rows.get(key)
        return row.updated if row else None

    def notes(self, key: str) -> str:
        row = self._rows.get(key)
        return row.notes if row else ""

    def all(self) -> dict[str, str]:
        return {key: row.value for key, row in self._rows.items()}

    def __contains__(self, key: object) -> bool:
        return key in self._rows
