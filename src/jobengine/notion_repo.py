"""Notion access for the sweep: Job Opportunities (read and write), Target Companies and
Config (read only).

NotionJobsRepo talks to the Notion API (data sources, Notion-Version 2025-09-03) through
jobengine.http. FakeJobsRepo keeps rows in memory with the same interface and records every
write, for tests and --fake runs. Neither ever changes a Notion schema.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterator
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from jobengine import http
from jobengine.safety import notion_write_target
from jobengine.settings import Settings
from jobengine.sweep.dedupe import IndexRow, parse_posting_ids
from jobengine.sweep.models import TargetCompany

log = logging.getLogger("jobengine.notion_repo")

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"
MIN_INTERVAL_SECONDS = 0.34  # about 3 requests per second

# Job Opportunities property name -> Notion property type, for the properties the sweep uses.
JOB_PROPERTY_TYPES = {
    "Company": "title",
    "Role": "rich_text",
    "City": "rich_text",
    "Country": "select",
    "Board": "select",
    "URL": "url",
    "Posted date": "date",
    "Salary": "rich_text",
    "Seniority": "select",
    "Years required": "number",
    "Dedupe key": "rich_text",
    "Posting IDs": "rich_text",
    "First seen": "date",
    "Swept date": "date",
    "Times seen": "number",
    "Status": "select",
    "Screen verdict": "select",
    "Ghost job risk": "select",
}

# Properties read when building the dedupe index.
INDEX_PROPERTIES = (
    "Company",
    "Role",
    "City",
    "Dedupe key",
    "Posting IDs",
    "Times seen",
    "First seen",
    "Posted date",
    "URL",
    "Salary",
    "Years required",
)


class JobsRepo(Protocol):
    def load_index(self) -> list[IndexRow]: ...

    def create(self, plan: dict[str, Any], blocks: list[str]) -> str: ...

    def update(self, page_id: str, props: dict[str, Any]) -> None: ...

    def has_body(self, page_id: str) -> bool: ...

    def append_body(self, page_id: str, blocks: list[str]) -> None: ...


# ---------------------------------------------------------------- value conversion


def _text(items: list[dict[str, Any]] | None) -> str:
    return "".join(item.get("plain_text") or item.get("text", {}).get("content", "")
                   for item in items or [])


def plain_value(prop: dict[str, Any]) -> Any:
    """Plain Python value of a Notion property object."""
    kind = prop.get("type")
    value = prop.get(kind)
    if kind in ("title", "rich_text"):
        return _text(value) or None
    if kind == "select":
        return (value or {}).get("name")
    if kind == "date":
        start = (value or {}).get("start")
        return date.fromisoformat(start[:10]) if start else None
    if kind in ("number", "url", "checkbox"):
        return value
    return None


def notion_value(kind: str, value: Any) -> dict[str, Any]:
    """Notion property payload for a plain value."""
    if kind in ("title", "rich_text"):
        return {kind: [{"type": "text", "text": {"content": str(value)[:2000]}}]}
    if kind == "select":
        return {"select": {"name": str(value)}}
    if kind == "date":
        return {"date": {"start": value.isoformat() if isinstance(value, date) else str(value)}}
    if kind == "number":
        return {"number": value}
    if kind == "url":
        return {"url": value}
    raise ValueError(f"unsupported property type {kind}")


def job_properties(plan: dict[str, Any]) -> dict[str, Any]:
    return {name: notion_value(JOB_PROPERTY_TYPES[name], value) for name, value in plan.items()}


def paragraph_blocks(texts: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]},
        }
        for text in texts
    ]


def index_row(page_id: str, values: dict[str, Any]) -> IndexRow | None:
    key = values.get("Dedupe key")
    if not key:
        return None
    years = values.get("Years required")
    return IndexRow(
        page_id=page_id,
        dedupe_key=key,
        posting_ids=parse_posting_ids(values.get("Posting IDs")),
        times_seen=int(values.get("Times seen") or 0),
        first_seen=values.get("First seen"),
        posted_date=values.get("Posted date"),
        url=values.get("URL"),
        salary=values.get("Salary"),
        years_required=int(years) if years is not None else None,
        company=values.get("Company") or "",
        role=values.get("Role") or "",
        city=values.get("City"),
    )


# ---------------------------------------------------------------- real Notion client


class NotionClient:
    """Thin Notion API client: auth headers, pacing and pagination."""

    def __init__(
        self,
        token: str,
        s: Settings,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }
        self._s = s
        self._sleep = sleep
        self._clock = clock
        self._last = -MIN_INTERVAL_SECONDS

    def request(
        self,
        method: str,
        path: str,
        json_body: Any = None,
        params: list[tuple[str, Any]] | None = None,
    ) -> Any:
        wait = self._last + MIN_INTERVAL_SECONDS - self._clock()
        if wait > 0:
            self._sleep(wait)
        self._last = self._clock()
        return http.request_json(
            method, f"{NOTION_API}{path}", params=params, headers=self._headers,
            json=json_body, s=self._s,
        )

    def property_ids(self, data_source_id: str, names: tuple[str, ...]) -> list[str]:
        schema = self.request("GET", f"/data_sources/{data_source_id}")
        props = schema.get("properties") or {}
        return [props[name]["id"] for name in names if name in props and "id" in props[name]]

    def query(
        self,
        data_source_id: str,
        body: dict[str, Any] | None = None,
        properties: tuple[str, ...] = (),
    ) -> Iterator[dict[str, Any]]:
        """Yield every page of a data source query, reading only `properties` when given."""
        params = None
        if properties:
            params = [("filter_properties", pid)
                      for pid in self.property_ids(data_source_id, properties)]
        cursor = None
        while True:
            payload = dict(body or {})
            payload["page_size"] = 100
            if cursor:
                payload["start_cursor"] = cursor
            path = f"/data_sources/{data_source_id}/query"
            try:
                data = self.request("POST", path, payload, params)
            except http.HttpError as exc:
                if not params or exc.status != 400:
                    raise
                log.warning("filter_properties rejected, reading all properties instead")
                params = None
                data = self.request("POST", path, payload, params)
            yield from data.get("results") or []
            if not data.get("has_more"):
                return
            cursor = data.get("next_cursor")


def page_values(page: dict[str, Any]) -> dict[str, Any]:
    return {name: plain_value(prop) for name, prop in (page.get("properties") or {}).items()}


class NotionJobsRepo:
    def __init__(self, client: NotionClient, data_source_id: str):
        self.client = client
        self.data_source_id = data_source_id

    def load_index(self) -> list[IndexRow]:
        rows = []
        for page in self.client.query(self.data_source_id, properties=INDEX_PROPERTIES):
            row = index_row(page["id"], page_values(page))
            if row:
                rows.append(row)
        return rows

    def create(self, plan: dict[str, Any], blocks: list[str]) -> str:
        body: dict[str, Any] = {
            "parent": {"type": "data_source_id", "data_source_id": self.data_source_id},
            "properties": job_properties(plan),
        }
        if blocks:
            body["children"] = paragraph_blocks(blocks)
        return self.client.request("POST", "/pages", body)["id"]

    def update(self, page_id: str, props: dict[str, Any]) -> None:
        self.client.request("PATCH", f"/pages/{page_id}", {"properties": job_properties(props)})

    def has_body(self, page_id: str) -> bool:
        data = self.client.request(
            "GET", f"/blocks/{page_id}/children", params=[("page_size", 1)]
        )
        return bool(data.get("results"))

    def append_body(self, page_id: str, blocks: list[str]) -> None:
        self.client.request(
            "PATCH", f"/blocks/{page_id}/children", {"children": paragraph_blocks(blocks)}
        )


class NotionReader:
    """Read-only access to the reference data sources (notion_read)."""

    def __init__(self, client: NotionClient, s: Settings):
        self.client = client
        self.s = s

    def config(self) -> dict[str, str]:
        values = {}
        for page in self.client.query(self.s.notion_read["config"], properties=("Key", "Value")):
            row = page_values(page)
            if row.get("Key"):
                values[row["Key"]] = row.get("Value") or ""
        return values

    def target_companies(self) -> list[TargetCompany]:
        body = {"filter": {"property": "Active", "checkbox": {"equals": True}}}
        names = ("Company", "Careers URL", "ATS platform", "Active", "Region")
        companies = []
        for page in self.client.query(self.s.notion_read["target_companies"], body, names):
            row = page_values(page)
            companies.append(
                TargetCompany(
                    name=row.get("Company") or "",
                    careers_url=row.get("Careers URL") or "",
                    ats_platform=row.get("ATS platform") or "Unknown",
                    active=bool(row.get("Active")),
                    region=row.get("Region"),
                )
            )
        return companies


def jobs_repo_for(s: Settings, client: NotionClient) -> NotionJobsRepo | None:
    """The Job Opportunities repo for this env, or None (with a DRY RUN log) if not writable."""
    target = notion_write_target("job_opportunities", s)
    if target is None:
        log.warning("DRY RUN: would write to job_opportunities")
        return None
    return NotionJobsRepo(client, target)


# ---------------------------------------------------------------- fake


class FakeJobsRepo:
    """In-memory Job Opportunities with the NotionJobsRepo interface.

    Rows are dicts of property name -> plain value, plus "page_id" and "body".
    Every write is recorded in `writes` as (action, page_id, payload).
    """

    def __init__(self, rows: list[dict[str, Any]] | None = None):
        self.rows: dict[str, dict[str, Any]] = {}
        self.writes: list[tuple[str, str, Any]] = []
        self._next = 1
        for row in rows or []:
            row = dict(row)
            self.rows[row.pop("page_id")] = row

    @classmethod
    def from_fixture(cls, path: Path) -> FakeJobsRepo:
        rows = json.loads(path.read_text(encoding="utf-8"))
        for row in rows:
            for key in ("First seen", "Posted date", "Swept date"):
                if row.get(key):
                    row[key] = date.fromisoformat(row[key])
        return cls(rows)

    def load_index(self) -> list[IndexRow]:
        rows = []
        for page_id, values in self.rows.items():
            row = index_row(page_id, values)
            if row:
                rows.append(row)
        return rows

    def create(self, plan: dict[str, Any], blocks: list[str]) -> str:
        page_id = f"fake-page-{self._next}"
        self._next += 1
        self.rows[page_id] = {**plan, "body": list(blocks)}
        self.writes.append(("create", page_id, dict(plan)))
        return page_id

    def update(self, page_id: str, props: dict[str, Any]) -> None:
        self.rows[page_id].update(props)
        self.writes.append(("update", page_id, dict(props)))

    def has_body(self, page_id: str) -> bool:
        return bool(self.rows[page_id].get("body"))

    def append_body(self, page_id: str, blocks: list[str]) -> None:
        self.rows[page_id].setdefault("body", []).extend(blocks)
        self.writes.append(("append_body", page_id, list(blocks)))
