"""LinkedIn connections, the monthly import (flow feature 1, step 1, 23 Oct). The CSV below
has the shape of LinkedIn's Connections.csv (Notes lines, then the header) with made-up
people."""

import io
import sys
import zipfile
from datetime import date

import pytest

from jobengine import telegram_bot as tb
from jobengine.bot_state import FakeBotState
from jobengine.contacts import connections as conn
from jobengine.contacts.connections import Connection, FakeConnections
from jobengine.settings import load_settings

TODAY = date(2026, 10, 23)
NOTES = ('Notes:\n"When exporting your connection data, you may notice that some of the email '
         'addresses are missing."\n\n')
HEADER = "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
ANNA = "Anna,Test,https://www.linkedin.com/in/anna-test,,Acme Cloud,DevOps Engineer,20 Oct 2026\n"
JAN = ("Jan,Sample,https://www.linkedin.com/in/jan-sample,jan@example.com,\"Northwind, Inc.\","
       "Engineering Manager,01 Mar 2024\n")
EVA = "Eva,Demo,https://www.linkedin.com/in/eva-demo,,Odra,Talent Partner,15 Jan 2023\n"


def csv_bytes(*rows, notes=True):
    return ((NOTES if notes else "") + HEADER + "".join(rows)).encode("utf-8")


def zipped(data, member="Connections.csv"):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("Company Follows.csv", "Organization,Followed On\n")
        archive.writestr(member, data)
    return out.getvalue()


def test_the_notes_lines_are_skipped_and_quoted_commas_kept():
    people = conn.read_export(csv_bytes(ANNA, JAN), "Connections.csv")
    assert [p.name for p in people] == ["Anna Test", "Jan Sample"]
    assert people[1].company == "Northwind, Inc." and people[1].email == "jan@example.com"
    assert people[0].key == "in/anna-test"


def test_the_zip_is_read_and_a_wrong_file_is_refused():
    assert len(conn.read_export(zipped(csv_bytes(ANNA)), "Basic_LinkedInDataExport.zip")) == 1
    with pytest.raises(conn.ExportError, match="no Connections.csv"):
        conn.read_export(zipped(csv_bytes(ANNA), member="Invitations.csv"), "export.zip")
    with pytest.raises(conn.ExportError, match="not LinkedIn's Connections.csv"):
        conn.read_export(b"Name,Date\nx,y\n", "other.csv")


def test_first_import_adds_everyone_and_the_second_changes_nothing():
    store, state = FakeConnections(), FakeBotState()
    text = conn.run_import(store, state, csv_bytes(ANNA, JAN), "Connections.csv", TODAY)
    assert "- New: 2 (Anna Test, Jan Sample)" in text
    assert "Next update due 22 Nov 2026." in text
    assert state.get(conn.STAMP_KEY) == {"date": "2026-10-23", "count": 2}
    assert {row["first_seen"] for row in store.table.values()} == {TODAY}
    again = conn.plan(store.rows(), conn.read_export(csv_bytes(ANNA, JAN), "c.csv"))
    assert again.writes == 0 and again.same == 2


def test_new_job_new_person_and_people_who_left():
    store = FakeConnections([conn.read_export(csv_bytes(ANNA), "c.csv")[0],
                             conn.read_export(csv_bytes(JAN), "c.csv")[0],
                             conn.read_export(csv_bytes(EVA), "c.csv")[0]])
    moved = ANNA.replace("Acme Cloud,DevOps Engineer", "Delft Digital,Senior SRE")
    export = conn.read_export(csv_bytes(moved, JAN.replace("jan@example.com", ""),
                                        "Ola,Nowak,https://www.linkedin.com/in/ola,,X,SRE,1 Oct\n"),
                              "c.csv")
    todo = conn.plan(store.rows(), export)
    assert [p.name for p in todo.new] == ["Ola Nowak"]
    assert [(r.person.name, fields) for r, _p, fields in todo.changed] == [
        ("Anna Test", ["company", "position"])]  # an empty email never wipes the saved one
    assert [r.person.name for r in todo.removed] == ["Eva Demo"]
    conn.apply(store, todo, TODAY)
    rows = {r.person.name: r for r in store.rows()}
    assert rows["Anna Test"].person.company == "Delft Digital"
    assert rows["Eva Demo"].removed and rows["Jan Sample"].person.email == "jan@example.com"
    text = conn.report(todo, len(export), TODAY)
    assert "New job or company: 1: Anna Test (now Senior SRE at Delft Digital)" in text
    assert "No longer connected (ticked Removed): 1 (Eva Demo)" in text
    back = conn.plan(store.rows(), conn.read_export(csv_bytes(EVA, ANNA, JAN), "c.csv"))
    assert [r.person.name for r, _p in back.back] == ["Eva Demo"]


def test_a_cut_file_changes_nothing():
    people = [Connection(f"P{i}", "X", f"https://www.linkedin.com/in/p{i}") for i in range(10)]
    store, state = FakeConnections(people), FakeBotState()
    text = conn.run_import(store, state, csv_bytes(ANNA), "Connections.csv", TODAY)
    assert text.startswith("The file has 1 people but the table has 10")
    assert not any(r.removed for r in store.rows()) and state.get(conn.STAMP_KEY) is None


def test_a_big_import_says_how_long_first():
    said = []
    rows = "".join(f"P{i},X,https://www.linkedin.com/in/p{i},,Co,SRE,1 Oct\n" for i in range(150))
    conn.run_import(FakeConnections(), FakeBotState(), csv_bytes(rows), "c.csv", TODAY,
                    said.append)
    assert said == ["Writing 150 rows to 🤝 LinkedIn Connections: about 1 minute. The summary "
                    "comes when it is done."]


def test_the_reminder_comes_monthly_then_weekly():
    state = FakeBotState()
    assert conn.reminder(state, TODAY) is None  # never imported through the bot
    state.set(conn.STAMP_KEY, {"date": "2026-09-20", "count": 3})
    assert conn.reminder(state, date(2026, 10, 19)) is None  # 29 days
    first = conn.reminder(state, date(2026, 10, 20))
    assert first.startswith("⏰ Monthly LinkedIn connections update: the last export was sent "
                            "on 20 Sep 2026.")
    assert conn.reminder(state, date(2026, 10, 26)) is None
    assert conn.reminder(state, date(2026, 10, 27)) is not None
    assert "next update due 20 Oct 2026" not in conn.status(state, date(2026, 10, 27), True)
    assert "the monthly update is due now" in conn.status(state, date(2026, 10, 27), True)


def test_notion_rows_are_written_with_the_csv_column_names():
    calls = []

    class Client:
        def request(self, method, path, body=None):
            calls.append((method, path, body))

    store = conn.NotionConnections(Client(), "ds-1")
    [anna] = conn.read_export(csv_bytes(ANNA), "c.csv")
    store.create(anna, TODAY)
    store.update("page-1", {"Removed": ("checkbox", True)})
    (_m, path, body), (method, page, patch) = calls
    props = body["properties"]
    assert path == "/pages" and body["parent"]["data_source_id"] == "ds-1"
    assert props["First Name"]["title"][0]["text"]["content"] == "Anna"
    assert props["URL"] == {"url": "https://www.linkedin.com/in/anna-test"}
    assert props["Email Address"] == {"email": None}  # an empty email is cleared, not ""
    assert props["First seen"] == {"date": {"start": "2026-10-23"}}
    assert (method, page, patch) == ("PATCH", "/pages/page-1",
                                     {"properties": {"Removed": {"checkbox": True}}})


def test_one_table_for_every_env():
    ids = {load_settings(env, {}).notion_read["linkedin_connections"]
           for env in ("local", "dev", "prod")}
    assert ids == {"d660c1d1-3ca9-41d9-b24a-88b2c0fb78c3"}


def test_sending_the_export_to_the_bot_updates_the_table(monkeypatch, capsys, tmp_path):
    export = tmp_path / "Basic_LinkedInDataExport_10-22-2026.zip"
    export.write_bytes(zipped(csv_bytes(ANNA, JAN)))
    other = tmp_path / "photo.png"
    other.write_bytes(b"png")
    monkeypatch.setattr(tb, "get_settings", lambda: load_settings("local", {}))
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"file {export}\nfile {other}\n/connections\n"))
    assert tb.main(["--fake"]) == 0
    out = capsys.readouterr().out
    assert "LinkedIn connections updated from your export (2 people):" in out
    assert "photo.png: the bot only reads your LinkedIn export" in out
    assert "LinkedIn connections: 2 people from the export sent on" in out


def test_the_file_download_never_shows_the_token(monkeypatch):
    def fail(url, timeout):
        raise OSError(f"refused {url}")

    monkeypatch.setattr(tb.urllib.request, "urlopen", fail)
    transport = tb.http_transport("secret-token")
    with pytest.raises(tb.TelegramNetworkError) as info:
        transport(tb.FILE_METHOD, {"file_path": "documents/file_1.zip"}, 5)
    assert "secret-token" not in str(info.value) and "<token>" in str(info.value)
