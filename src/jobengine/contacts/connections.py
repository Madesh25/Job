"""Your LinkedIn connections (flow feature 1, step 1, 23 Oct): the monthly import.

LinkedIn is never opened by the bot. Once a month you download your data export (LinkedIn:
Settings, Data privacy, Get a copy of your data) and send the zip, or only its
Connections.csv, to the bot. The bot reads Connections.csv and brings the Notion table
🤝 LinkedIn Connections up to date:

- a person not in the table yet is added (First seen = today);
- a person whose company, position or email changed is updated;
- a person in the table but not in the export is ticked Removed (you are no longer connected);
  a Removed person who is back is unticked;
- everyone else is left alone, so your own columns (Referral, Last asked, Notes) are kept.

People are matched by their profile URL (by name and company when the export has no URL).
The table's first seven columns have the CSV's own names, so the first import can also be
done in Notion (Merge with CSV); Kind is a Notion formula from Position.

One table for every env (your own reference table, like Skills Inventory): it is written
only when you send the export, through safety.connections_write_target. Your decision of
23 Oct; using the table for referrals is the next step.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import PurePosixPath
from typing import Any, Protocol

EXPORT_FILE = "connections.csv"
HEADER_START = "First Name"
COLUMNS = {  # Connection field -> (Notion column, Notion type); the CSV's own names
    "first": ("First Name", "title"),
    "last": ("Last Name", "rich_text"),
    "url": ("URL", "url"),
    "email": ("Email Address", "email"),
    "company": ("Company", "rich_text"),
    "position": ("Position", "rich_text"),
    "connected_on": ("Connected On", "rich_text"),
}
# A change in these is written; a name or date change alone is not worth a Notion write.
WATCHED = ("company", "position", "email")
STAMP_KEY = "connections.imported"  # bot_state: {"date": <iso>, "count": <people>}
REMINDED_KEY = "connections.reminded"  # bot_state: {"date": <iso>}
REFRESH_DAYS = 30
REMIND_EVERY_DAYS = 7
# An export with less than half of the people in the table is refused: a wrong or cut file
# would otherwise tick most of your connections Removed.
MIN_SHARE = 0.5
MAX_BYTES = 20_000_000  # Telegram lets a bot download files up to 20 MB
MAX_NAMES = 10
SECONDS_PER_WRITE = 0.35  # the Notion client keeps about 3 requests a second
HOW_TO = ("To update: LinkedIn > Me > Settings & Privacy > Data privacy > Get a copy of your "
          "data > Connections (or the full archive) > Request archive. When LinkedIn's email "
          "comes, download the zip and send it here (the zip or only Connections.csv).")


class ExportError(ValueError):
    """The file is not a LinkedIn connections export."""


@dataclass(frozen=True)
class Connection:
    first: str
    last: str
    url: str = ""
    email: str = ""
    company: str = ""
    position: str = ""
    connected_on: str = ""

    @property
    def name(self) -> str:
        return f"{self.first} {self.last}".strip()

    @property
    def key(self) -> str:
        return person_key(self.url, self.name, self.company)


@dataclass(frozen=True)
class StoredRow:
    page_id: str
    person: Connection
    removed: bool = False


@dataclass
class ImportPlan:
    new: list[Connection] = field(default_factory=list)
    changed: list[tuple[StoredRow, Connection, list[str]]] = field(default_factory=list)
    removed: list[StoredRow] = field(default_factory=list)
    back: list[tuple[StoredRow, Connection]] = field(default_factory=list)
    same: int = 0

    @property
    def writes(self) -> int:
        return len(self.new) + len(self.changed) + len(self.removed) + len(self.back)


class ConnectionsStore(Protocol):
    def rows(self) -> list[StoredRow]: ...

    def create(self, person: Connection, today: date) -> None: ...

    def update(self, page_id: str, props: dict[str, Any]) -> None: ...


def person_key(url: str, name: str, company: str) -> str:
    """linkedin.com/in/<id> in lower case; "name|company" when there is no profile URL."""
    match = re.search(r"linkedin\.com/in/([^/?#\s]+)", url or "", re.I)
    if match:
        return "in/" + match.group(1).lower()
    return "name/" + " ".join(f"{name}|{company}".casefold().split())


# ---------------------------------------------------------------- reading the export


def _csv_text(data: bytes, filename: str) -> str:
    if data[:2] == b"PK" or filename.lower().endswith(".zip"):
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise ExportError(f"{filename} is not a zip file that can be opened") from None
        members = [m for m in archive.namelist()
                   if PurePosixPath(m).name.lower() == EXPORT_FILE]
        if not members:
            raise ExportError(f"{filename} has no Connections.csv: send the LinkedIn data "
                              "export zip (or only its Connections.csv)")
        data = archive.read(members[0])
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def read_export(data: bytes, filename: str) -> list[Connection]:
    """The people in a LinkedIn export: the zip or Connections.csv. LinkedIn puts a few
    "Notes:" lines before the header; they are skipped."""
    lines = _csv_text(data, filename).splitlines()
    start = next((i for i, line in enumerate(lines)
                  if line.lstrip('"').startswith(HEADER_START)), None)
    if start is None:
        raise ExportError(f"{filename} has no \"First Name,Last Name,...\" header: it is not "
                          "LinkedIn's Connections.csv")
    reader = csv.DictReader(lines[start:])
    people: dict[str, Connection] = {}
    for row in reader:
        values = {attr: " ".join(str(row.get(column) or "").split())
                  for attr, (column, _kind) in COLUMNS.items()}
        person = Connection(**values)
        if person.name:
            people.setdefault(person.key, person)
    return list(people.values())


# ---------------------------------------------------------------- the plan


def plan(stored: Iterable[StoredRow], export: list[Connection]) -> ImportPlan:
    out = ImportPlan()
    by_key: dict[str, StoredRow] = {}
    for row in stored:
        by_key.setdefault(row.person.key, row)
    seen = set()
    for person in export:
        row = by_key.get(person.key)
        seen.add(person.key)
        if row is None:
            out.new.append(person)
            continue
        fields = [attr for attr in WATCHED
                  if getattr(person, attr) and getattr(person, attr) != getattr(row.person, attr)]
        if row.removed:
            out.back.append((row, person))
        elif fields:
            out.changed.append((row, person, fields))
        else:
            out.same += 1
    out.removed = [row for key, row in by_key.items() if key not in seen and not row.removed]
    return out


def check_size(stored: list[StoredRow], export: list[Connection]) -> str | None:
    """Why this export is refused, or None."""
    if not export:
        return "The file has no people in it. Nothing was changed."
    current = sum(1 for row in stored if not row.removed)
    if current and len(export) < current * MIN_SHARE:
        return (f"The file has {len(export)} people but the table has {current}: it looks cut "
                "or it is another file, so nothing was changed (most people would have been "
                "ticked Removed). Send the full Connections.csv from the newest export.")
    return None


def changed_props(person: Connection, fields: list[str]) -> dict[str, Any]:
    return {COLUMNS[attr][0]: (COLUMNS[attr][1], getattr(person, attr)) for attr in fields}


def apply(store: ConnectionsStore, todo: ImportPlan, today: date) -> None:
    for person in todo.new:
        store.create(person, today)
    for row, person, fields in todo.changed:
        store.update(row.page_id, changed_props(person, fields))
    for row in todo.removed:
        store.update(row.page_id, {"Removed": ("checkbox", True)})
    for row, person in todo.back:
        fields = [attr for attr in WATCHED if getattr(person, attr)]
        store.update(row.page_id, {**changed_props(person, fields),
                                   "Removed": ("checkbox", False)})


def _names(people: list[str]) -> str:
    shown = ", ".join(people[:MAX_NAMES])
    more = len(people) - MAX_NAMES
    return f"{shown} and {more} more" if more > 0 else shown


def report(todo: ImportPlan, total: int, today: date) -> str:
    lines = [f"LinkedIn connections updated from your export ({total} people):"]
    lines.append(f"- New: {len(todo.new)}" + (f" ({_names([p.name for p in todo.new])})"
                                               if todo.new else ""))
    moved = [f"{p.name} (now {p.position or '?'} at {p.company or '?'})"
             for _row, p, fields in todo.changed if {"company", "position"} & set(fields)]
    mails = sum(1 for _row, _p, fields in todo.changed if fields == ["email"])
    lines.append(f"- New job or company: {len(moved)}" + (f": {_names(moved)}" if moved else ""))
    if mails:
        lines.append(f"- New email address: {mails}")
    lines.append(f"- No longer connected (ticked Removed): {len(todo.removed)}"
                 + (f" ({_names([r.person.name for r in todo.removed])})" if todo.removed
                    else ""))
    if todo.back:
        lines.append(f"- Connected again (Removed unticked): {len(todo.back)}")
    lines.append(f"- Unchanged: {todo.same}")
    lines.append(f"Next update due {(today + timedelta(days=REFRESH_DAYS)):%d %b %Y}.")
    return "\n".join(lines)


def run_import(store: ConnectionsStore, state: Any, data: bytes, filename: str, today: date,
               notify: Callable[[str], None] | None = None) -> str:
    """Read the export, write the changes, remember the date. Returns the answer text."""
    if len(data) > MAX_BYTES:
        return f"{filename} is over 20 MB. Send only Connections.csv from the zip."
    try:
        export = read_export(data, filename)
    except ExportError as exc:
        return str(exc)
    stored = store.rows()
    refused = check_size(stored, export)
    if refused:
        return refused
    todo = plan(stored, export)
    if notify and todo.writes > 100:
        minutes = max(1, round(todo.writes * SECONDS_PER_WRITE / 60))
        notify(f"Writing {todo.writes} rows to 🤝 LinkedIn Connections: about {minutes} "
               f"minute{'s' if minutes != 1 else ''}. The summary comes when it is done.")
    apply(store, todo, today)
    if state is not None:
        state.set(STAMP_KEY, {"date": today.isoformat(), "count": len(export)})
    return report(todo, len(export), today)


# ---------------------------------------------------------------- status and reminder


def last_import(state: Any) -> tuple[date | None, int | None]:
    value = (state.get(STAMP_KEY) if state is not None else None) or {}
    try:
        when = date.fromisoformat(str(value.get("date")))
    except ValueError:
        when = None
    count = value.get("count")
    return when, int(count) if isinstance(count, int) else None


def status(state: Any, today: date, writable: bool) -> str:
    when, count = last_import(state)
    if not writable:
        return ("The 🤝 LinkedIn Connections table is not set up for this bot "
                "(notion.read.linkedin_connections).")
    if when is None:
        head = ("LinkedIn connections: not imported through the bot yet. Send the export once "
                "(even if you imported it in Notion): the bot checks every row and starts the "
                "monthly reminder.")
    else:
        due = when + timedelta(days=REFRESH_DAYS)
        left = (due - today).days
        state_text = (f"next update due {due:%d %b %Y}" if left > 0
                      else "the monthly update is due now")
        head = (f"LinkedIn connections: {count if count is not None else '?'} people from the "
                f"export sent on {when:%d %b %Y}; {state_text}.")
    return f"{head}\n{HOW_TO}"


def reminder(state: Any, today: date) -> str | None:
    """Once a week from the day the monthly update is due (30 days after the last import).
    Nothing before the first import through the bot."""
    when, _count = last_import(state)
    if state is None or when is None or (today - when).days < REFRESH_DAYS:
        return None
    sent = (state.get(REMINDED_KEY) or {}).get("date")
    try:
        if sent and (today - date.fromisoformat(sent)).days < REMIND_EVERY_DAYS:
            return None
    except ValueError:
        pass
    state.set(REMINDED_KEY, {"date": today.isoformat()})
    return (f"⏰ Monthly LinkedIn connections update: the last export was sent on "
            f"{when:%d %b %Y}. New connections are not in the table yet.\n{HOW_TO}")


# ---------------------------------------------------------------- Notion and the fake


def _person(values: dict[str, Any]) -> Connection:
    return Connection(**{attr: str(values.get(column) or "")
                         for attr, (column, _kind) in COLUMNS.items()})


class NotionConnections:
    """The 🤝 LinkedIn Connections table (data source from safety.connections_write_target)."""

    def __init__(self, client: Any, data_source_id: str):
        self.client = client
        self.data_source_id = data_source_id

    def rows(self) -> list[StoredRow]:
        from jobengine.notion_repo import page_values

        columns = tuple(column for column, _kind in COLUMNS.values()) + ("Removed",)
        out = []
        for page in self.client.query(self.data_source_id, properties=columns):
            values = page_values(page)
            out.append(StoredRow(page["id"], _person(values), bool(values.get("Removed"))))
        return out

    def _props(self, props: dict[str, Any]) -> dict[str, Any]:
        from jobengine.notion_repo import notion_value

        out = {}
        for column, (kind, value) in props.items():
            if kind in ("url", "email") and not value:
                out[column] = {kind: None}
            else:
                out[column] = notion_value(kind, value)
        return out

    def create(self, person: Connection, today: date) -> None:
        props = {column: (kind, getattr(person, attr))
                 for attr, (column, kind) in COLUMNS.items()}
        props["First seen"] = ("date", today)
        body = {"parent": {"type": "data_source_id", "data_source_id": self.data_source_id},
                "properties": self._props(props)}
        self.client.request("POST", "/pages", body)

    def update(self, page_id: str, props: dict[str, Any]) -> None:
        self.client.request("PATCH", f"/pages/{page_id}", {"properties": self._props(props)})


class FakeConnections:
    """In-memory table for --fake and the tests."""

    def __init__(self, people: Iterable[Connection] = ()):
        self.table: dict[str, dict[str, Any]] = {}
        for i, person in enumerate(people):
            self.table[f"conn-{i}"] = {"person": person, "removed": False, "first_seen": None}

    def rows(self) -> list[StoredRow]:
        return [StoredRow(pid, row["person"], row["removed"]) for pid, row in self.table.items()]

    def create(self, person: Connection, today: date) -> None:
        self.table[f"conn-{len(self.table)}"] = {"person": person, "removed": False,
                                                 "first_seen": today}

    def update(self, page_id: str, props: dict[str, Any]) -> None:
        row = self.table[page_id]
        values = {attr: getattr(row["person"], attr) for attr in COLUMNS}
        for column, (_kind, value) in props.items():
            if column == "Removed":
                row["removed"] = bool(value)
                continue
            attr = next(a for a, (c, _k) in COLUMNS.items() if c == column)
            values[attr] = value
        row["person"] = Connection(**values)


def store_for(s: Any, client: Any) -> NotionConnections | None:
    from jobengine.safety import connections_write_target

    target = connections_write_target(s)
    return NotionConnections(client, target) if target else None
