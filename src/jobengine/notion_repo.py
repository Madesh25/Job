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
    # Written by screening (Module 03).
    "Skip reason": "select",
    "Language required": "select",
    "Contract type": "select",
    "Sponsorship": "select",
    "Work mode": "select",
    "Expires": "date",
    "Visa flags": "multi_select",
    "Tech stack": "multi_select",
    "Gaps": "rich_text",
    # Written by the resume builder (Module 04).
    "Resume": "relation",
    "Applied date": "date",
    "Last activity date": "date",
}

# Resume Log property name -> Notion property type.
RESUME_LOG_PROPERTY_TYPES = {
    "Resume name": "title",
    "Date": "date",
    "Job": "relation",
    "Skills focus": "rich_text",
    "Experience focus": "rich_text",
    "Summary focus": "rich_text",
    "Diff score": "rich_text",
    "Page fill": "rich_text",
    "Engine version": "rich_text",
    "Evidence used": "relation",
    "Approved": "checkbox",
    "Revision": "number",
    "File": "rich_text",
}

# Properties screening and /pending read.
ROW_PROPERTIES = (
    "Company",
    "Role",
    "City",
    "Country",
    "Board",
    "URL",
    "Dedupe key",
    "Posting IDs",
    "Status",
    "Screen verdict",
    "Years required",
    "Expires",
    "Posted date",
    "Ghost job risk",
    "Salary",
    "Visa flags",
    "Contract type",
    "Sponsorship",
    "Gaps",
    "Swept date",
)

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

    def query_rows(self, where: dict[str, Any] | None = None) -> list[tuple[str, dict]]: ...

    def get_values(self, page_id: str) -> dict[str, Any] | None: ...

    def read_body(self, page_id: str) -> list[str]: ...


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
    if kind == "relation":
        return {"relation": [{"id": str(v)} for v in value or []]}
    if kind == "checkbox":
        return {"checkbox": bool(value)}
    if kind == "multi_select":
        return [item.get("name") for item in value or [] if item.get("name")]
    if kind in ("number", "url", "checkbox"):
        return value
    if kind == "relation":
        return [item.get("id") for item in value or [] if item.get("id")]
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
    if kind == "multi_select":
        # Option names cannot contain commas; new options are created by the API.
        names = [str(v).replace(",", " ").strip()[:100] for v in value or []]
        return {"multi_select": [{"name": n} for n in dict.fromkeys(names) if n]}
    raise ValueError(f"unsupported property type {kind}")


def job_properties(plan: dict[str, Any]) -> dict[str, Any]:
    return {name: notion_value(JOB_PROPERTY_TYPES[name], value) for name, value in plan.items()}


def rich_text(text: str) -> list[dict[str, Any]]:
    """Rich text items of at most 2000 characters each (Notion's limit per item)."""
    return [{"type": "text", "text": {"content": text[i:i + 2000]}}
            for i in range(0, len(text), 2000)] or [{"type": "text", "text": {"content": ""}}]


def typed_blocks(blocks: list[tuple[str, ...]]) -> list[dict[str, Any]]:
    """Blocks from ("heading_2", text), ("paragraph", text) or ("code", text, language)."""
    out = []
    for block in blocks:
        kind, text = block[0], block[1]
        body: dict[str, Any] = {"rich_text": rich_text(text)}
        if kind == "code":
            body["language"] = block[2] if len(block) > 2 else "plain text"
        out.append({"object": "block", "type": kind, kind: body})
    return out


def paragraph_blocks(texts: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]},
        }
        for text in texts
    ]


def select_filter(prop: str, *values: str) -> dict[str, Any]:
    """Notion filter: select `prop` equals any of `values`."""
    conditions = [{"property": prop, "select": {"equals": v}} for v in values]
    return conditions[0] if len(conditions) == 1 else {"or": conditions}


def matches(values: dict[str, Any], flt: dict[str, Any] | None) -> bool:
    """Evaluate the subset of Notion filters used here against plain values (for fakes)."""
    if not flt:
        return True
    if "and" in flt:
        return all(matches(values, f) for f in flt["and"])
    if "or" in flt:
        return any(matches(values, f) for f in flt["or"])
    value = values.get(flt["property"])
    for kind in ("select", "rich_text", "url", "title"):
        if kind in flt:
            cond = flt[kind]
            if "equals" in cond:
                return value == cond["equals"]
            if "contains" in cond:
                return cond["contains"] in (value or "")
    raise ValueError(f"unsupported filter {flt}")


def block_text(block: dict[str, Any]) -> str | None:
    """Plain text of a block that holds rich text (paragraph, heading, list item...)."""
    body = block.get(block.get("type") or "") or {}
    if isinstance(body, dict) and "rich_text" in body:
        return _text(body["rich_text"])
    return None


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


    def query_rows(self, where: dict[str, Any] | None = None) -> list[tuple[str, dict]]:
        body = {"filter": where} if where else None
        return [(page["id"], page_values(page))
                for page in self.client.query(self.data_source_id, body, ROW_PROPERTIES)]

    def get_values(self, page_id: str) -> dict[str, Any] | None:
        try:
            page = self.client.request("GET", f"/pages/{page_id}")
        except http.HttpError as exc:
            if exc.status == 404:
                return None
            raise
        if page.get("in_trash") or page.get("archived"):
            return None
        return page_values(page)

    def read_body(self, page_id: str) -> list[str]:
        texts: list[str] = []
        cursor = None
        while True:
            params: list[tuple[str, Any]] = [("page_size", 100)]
            if cursor:
                params.append(("start_cursor", cursor))
            data = self.client.request("GET", f"/blocks/{page_id}/children", params=params)
            for block in data.get("results") or []:
                text = block_text(block)
                if text is not None:
                    texts.append(text)
            if not data.get("has_more"):
                return texts
            cursor = data.get("next_cursor")


class NotionReader:
    """Read-only access to the reference data sources (notion_read)."""

    def __init__(self, client: NotionClient, s: Settings):
        self.client = client
        self.s = s

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
            for key in ("First seen", "Posted date", "Swept date", "Expires"):
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

    def query_rows(self, where: dict[str, Any] | None = None) -> list[tuple[str, dict]]:
        return [(pid, {k: v for k, v in row.items() if k != "body"})
                for pid, row in self.rows.items() if matches(row, where)]

    def get_values(self, page_id: str) -> dict[str, Any] | None:
        row = self.rows.get(page_id)
        return {k: v for k, v in row.items() if k != "body"} if row else None

    def read_body(self, page_id: str) -> list[str]:
        return list(self.rows[page_id].get("body") or [])


# ---------------------------------------------------------------- Resume Log


class ResumeLogRepo(Protocol):
    def create(self, props: dict[str, Any], blocks: list[tuple[str, ...]]) -> str: ...

    def update(self, page_id: str, props: dict[str, Any]) -> None: ...

    def get(self, page_id: str) -> dict[str, Any] | None: ...

    def rows_for_job(self, job_id: str) -> list[tuple[str, dict[str, Any]]]: ...

    def approved_rows(self) -> list[tuple[str, dict[str, Any]]]: ...

    def all_rows(self) -> list[tuple[str, dict[str, Any]]]: ...

    def read_body(self, page_id: str) -> list[str]: ...


def _hex(page_id: str) -> str:
    return page_id.replace("-", "").lower()


class NotionResumeLogRepo:
    """Resume Log (one row per resume revision). Written only through the target from
    safety.notion_write_target("resume_log")."""

    def __init__(self, client: NotionClient, data_source_id: str):
        self.client = client
        self.data_source_id = data_source_id
        self._types: dict[str, str] | None = None

    def _props(self, props: dict[str, Any]) -> dict[str, Any]:
        if self._types is None:
            # "Evidence used" may be a text field in an older schema: write the IDs as text.
            schema = self.client.request("GET", f"/data_sources/{self.data_source_id}")
            self._types = {name: (value or {}).get("type", "")
                           for name, value in (schema.get("properties") or {}).items()}
        out = {}
        for name, value in props.items():
            kind = RESUME_LOG_PROPERTY_TYPES[name]
            if name == "Evidence used" and self._types.get(name) == "rich_text":
                kind, value = "rich_text", ", ".join(value or [])
            out[name] = notion_value(kind, value)
        return out

    def create(self, props: dict[str, Any], blocks: list[tuple[str, ...]]) -> str:
        body = {
            "parent": {"type": "data_source_id", "data_source_id": self.data_source_id},
            "properties": self._props(props),
            "children": typed_blocks(blocks),
        }
        return self.client.request("POST", "/pages", body)["id"]

    def update(self, page_id: str, props: dict[str, Any]) -> None:
        self.client.request("PATCH", f"/pages/{page_id}", {"properties": self._props(props)})

    def get(self, page_id: str) -> dict[str, Any] | None:
        return NotionJobsRepo(self.client, self.data_source_id).get_values(page_id)

    def _query(self, body: dict[str, Any] | None) -> list[tuple[str, dict[str, Any]]]:
        return [(page["id"], page_values(page))
                for page in self.client.query(self.data_source_id, body)]

    def rows_for_job(self, job_id: str) -> list[tuple[str, dict[str, Any]]]:
        return self._query({"filter": {"property": "Job", "relation": {"contains": job_id}}})

    def approved_rows(self) -> list[tuple[str, dict[str, Any]]]:
        return self._query({"filter": {"property": "Approved", "checkbox": {"equals": True}}})

    def all_rows(self) -> list[tuple[str, dict[str, Any]]]:
        return self._query(None)

    def read_body(self, page_id: str) -> list[str]:
        return NotionJobsRepo(self.client, self.data_source_id).read_body(page_id)


class FakeResumeLogRepo:
    """In-memory Resume Log. Page IDs look like Notion UUIDs so short references work."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.writes: list[tuple[str, str, Any]] = []

    def _id(self, page_id: str) -> str:
        return next((k for k in self.rows if _hex(k) == _hex(page_id)), page_id)

    def create(self, props: dict[str, Any], blocks: list[tuple[str, ...]]) -> str:
        n = len(self.rows) + 1
        page_id = f"{n:08x}-0000-4000-8000-{n:012x}"
        self.rows[page_id] = {**props, "body": [b[1] for b in blocks]}
        self.writes.append(("create", page_id, dict(props)))
        return page_id

    def update(self, page_id: str, props: dict[str, Any]) -> None:
        page_id = self._id(page_id)
        self.rows[page_id].update(props)
        self.writes.append(("update", page_id, dict(props)))

    def get(self, page_id: str) -> dict[str, Any] | None:
        row = self.rows.get(self._id(page_id))
        return {k: v for k, v in row.items() if k != "body"} if row else None

    def _rows(self, keep) -> list[tuple[str, dict[str, Any]]]:
        return [(pid, {k: v for k, v in row.items() if k != "body"})
                for pid, row in self.rows.items() if keep(row)]

    def rows_for_job(self, job_id: str) -> list[tuple[str, dict[str, Any]]]:
        return self._rows(lambda r: _hex(job_id) in {_hex(j) for j in r.get("Job") or []})

    def approved_rows(self) -> list[tuple[str, dict[str, Any]]]:
        return self._rows(lambda r: bool(r.get("Approved")))

    def all_rows(self) -> list[tuple[str, dict[str, Any]]]:
        return self._rows(lambda r: True)

    def read_body(self, page_id: str) -> list[str]:
        return list(self.rows[self._id(page_id)].get("body") or [])


def resume_log_for(s: Settings, client: NotionClient) -> NotionResumeLogRepo | None:
    target = notion_write_target("resume_log", s)
    if target is None:
        log.warning("DRY RUN: would write to resume_log")
        return None
    return NotionResumeLogRepo(client, target)
