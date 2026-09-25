import json
import logging
from datetime import date

import httpx
import pytest

from jobengine import http
from jobengine.notion_repo import (
    NOTION_VERSION,
    FakeJobsRepo,
    NotionClient,
    NotionReader,
    job_properties,
    jobs_repo_for,
)
from jobengine.settings import load_settings

SANDBOX_JOBS = "2e31d687-5a7b-442b-87e2-4d8f8c6dea95"
PROD_JOBS = "0228ce56-475a-4b60-8d6b-fd2a59297b24"


class FakeNotion:
    """Mocked Notion API recording every request."""

    def __init__(self):
        self.requests = []
        self.query_pages = []
        self.children = {}

    def __call__(self, request):
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, request.url.params, body))
        path = request.url.path
        if request.method == "GET" and path.startswith("/v1/data_sources/"):
            return httpx.Response(200, json={"properties": {
                "Dedupe key": {"id": "dk"}, "Posting IDs": {"id": "pi"},
                "Company": {"id": "title"}, "Key": {"id": "k"}, "Value": {"id": "v"},
            }})
        if path.endswith("/query"):
            page = self.query_pages.pop(0) if self.query_pages else {"results": []}
            return httpx.Response(200, json=page)
        if request.method == "POST" and path == "/v1/pages":
            return httpx.Response(200, json={"id": "new-page"})
        if request.method == "GET" and path.endswith("/children"):
            page_id = path.split("/")[3]
            return httpx.Response(200, json={"results": self.children.get(page_id, [])})
        return httpx.Response(200, json={"id": path.split("/")[-1]})


@pytest.fixture
def notion():
    fake = FakeNotion()
    http.set_transport(httpx.MockTransport(fake))
    yield fake
    http.set_transport(None)


def client_for(s):
    return NotionClient("test-token", s, sleep=lambda _: None)


def page(page_id, key, ids="", times=1):
    return {
        "id": page_id,
        "properties": {
            "Dedupe key": {"type": "rich_text", "rich_text": [{"plain_text": key}]},
            "Posting IDs": {"type": "rich_text", "rich_text": [{"plain_text": ids}]},
            "Times seen": {"type": "number", "number": times},
            "First seen": {"type": "date", "date": {"start": "2026-09-01"}},
            "Company": {"type": "title", "title": [{"plain_text": "Acme"}]},
        },
    }


def test_dev_writes_go_to_sandbox(notion):
    s = load_settings("dev", {})
    repo = jobs_repo_for(s, client_for(s))
    repo.create({"Company": "Acme", "Status": "New"}, ["Description source: ats", "text"])
    method, path, _, body = notion.requests[-1]
    assert (method, path) == ("POST", "/v1/pages")
    assert body["parent"] == {"type": "data_source_id", "data_source_id": SANDBOX_JOBS}
    assert body["properties"]["Company"]["title"][0]["text"]["content"] == "Acme"
    assert body["children"][1]["paragraph"]["rich_text"][0]["text"]["content"] == "text"


def test_prod_writes_go_to_prod(notion):
    s = load_settings("prod", {})
    repo = jobs_repo_for(s, client_for(s))
    assert repo.data_source_id == PROD_JOBS


def test_missing_write_target_means_no_writes(notion, caplog):
    s = load_settings("dev", {}).model_copy(update={"notion_write": {}})
    with caplog.at_level(logging.WARNING):
        assert jobs_repo_for(s, client_for(s)) is None
    assert "DRY RUN: would write to job_opportunities" in caplog.text
    assert notion.requests == []


def test_headers_and_version(notion):
    seen = []

    def handler(request):
        seen.append(request.headers)
        return httpx.Response(200, json={"id": "p"})

    http.set_transport(httpx.MockTransport(handler))
    s = load_settings("dev", {})
    jobs_repo_for(s, client_for(s)).update("p", {"Swept date": date(2026, 10, 1)})
    assert seen[0]["Notion-Version"] == NOTION_VERSION == "2025-09-03"
    assert seen[0]["Authorization"] == "Bearer test-token"


def test_load_index_pages_through_results(notion):
    notion.query_pages = [
        {"results": [page("p1", "a|b|c", "adzuna:1")], "has_more": True, "next_cursor": "c2"},
        {"results": [page("p2", "d|e|f", "gmail:2, ats:3", 2), page("p3", "")],
         "has_more": False},
    ]
    s = load_settings("dev", {})
    rows = jobs_repo_for(s, client_for(s)).load_index()
    assert [r.page_id for r in rows] == ["p1", "p2"]  # p3 has no key
    assert rows[1].posting_ids == ["gmail:2", "ats:3"] and rows[1].times_seen == 2
    assert rows[0].first_seen == date(2026, 9, 1)
    queries = [r for r in notion.requests if r[1].endswith("/query")]
    assert queries[0][1] == f"/v1/data_sources/{SANDBOX_JOBS}/query"
    assert queries[1][3]["start_cursor"] == "c2"
    assert set(queries[0][2].get_list("filter_properties")) == {"dk", "pi", "title"}


def test_query_falls_back_when_filter_properties_rejected(notion):
    rejected = []

    def handler(request):
        if request.url.path.endswith("/query") and request.url.params.get("filter_properties"):
            rejected.append(1)
            return httpx.Response(400, json={"message": "bad filter_properties"})
        return notion(request)

    http.set_transport(httpx.MockTransport(handler))
    notion.query_pages = [{"results": [page("p1", "a|b|c")], "has_more": False}]
    s = load_settings("dev", {})
    assert len(jobs_repo_for(s, client_for(s)).load_index()) == 1
    assert rejected == [1]


def test_update_and_body_calls(notion):
    s = load_settings("dev", {})
    repo = jobs_repo_for(s, client_for(s))
    repo.update("p9", {"Times seen": 3, "Swept date": date(2026, 10, 1)})
    _, path, _, body = notion.requests[-1]
    assert path == "/v1/pages/p9"
    assert body["properties"]["Times seen"] == {"number": 3}
    assert body["properties"]["Swept date"] == {"date": {"start": "2026-10-01"}}
    assert repo.has_body("p9") is False
    notion.children["p9"] = [{"type": "paragraph"}]
    assert repo.has_body("p9") is True
    repo.append_body("p9", ["x"])
    assert notion.requests[-1][1] == "/v1/blocks/p9/children"


def test_client_paces_requests(notion):
    s = load_settings("dev", {})
    now = [100.0]
    waits = []
    client = NotionClient("t", s, sleep=waits.append, clock=lambda: now[0])
    client.request("GET", "/pages/a")
    client.request("GET", "/pages/b")
    assert waits and waits[0] == pytest.approx(0.34)


def test_reader_config_and_target_companies_are_read_only(notion):
    s = load_settings("dev", {})
    reader = NotionReader(client_for(s), s)
    notion.query_pages = [{"results": [{"id": "c1", "properties": {
        "Key": {"type": "title", "title": [{"plain_text": "strategy_refresh_days"}]},
        "Value": {"type": "rich_text", "rich_text": [{"plain_text": "30"}]},
    }}]}, {"results": [{"id": "t1", "properties": {
        "Company": {"type": "title", "title": [{"plain_text": "Acme"}]},
        "Careers URL": {"type": "url", "url": "https://boards.greenhouse.io/acme"},
        "ATS platform": {"type": "select", "select": {"name": "Greenhouse"}},
        "Active": {"type": "checkbox", "checkbox": True},
    }}]}]
    from jobengine.config_store import ConfigStore

    assert ConfigStore.load(reader.client, s).all() == {"strategy_refresh_days": "30"}
    companies = reader.target_companies()
    assert companies[0].name == "Acme" and companies[0].ats_platform == "Greenhouse"
    methods = {method for method, *_ in notion.requests}
    assert methods <= {"GET", "POST"}
    assert all(path.endswith("/query") for m, path, *_ in notion.requests if m == "POST")
    query = [r for r in notion.requests if r[1].endswith("/query")][1]
    assert query[3]["filter"] == {"property": "Active", "checkbox": {"equals": True}}


def test_job_properties_types():
    props = job_properties({"Country": "Poland", "URL": "https://x", "Years required": 3})
    assert props == {
        "Country": {"select": {"name": "Poland"}},
        "URL": {"url": "https://x"},
        "Years required": {"number": 3},
    }


def test_fake_repo_records_writes():
    repo = FakeJobsRepo([{"page_id": "p1", "Dedupe key": "k", "Status": "Applied",
                          "Posting IDs": "adzuna:1", "Times seen": 1}])
    assert repo.load_index()[0].posting_ids == ["adzuna:1"]
    new_id = repo.create({"Dedupe key": "k2"}, ["Description source: ats"])
    repo.update("p1", {"Times seen": 2})
    assert repo.has_body(new_id) and not repo.has_body("p1")
    repo.append_body("p1", ["x"])
    assert [w[0] for w in repo.writes] == ["create", "update", "append_body"]
    assert repo.rows["p1"]["Status"] == "Applied"


def test_multi_select_values_drop_commas_and_duplicates():
    props = job_properties({"Tech stack": ["AWS", "Go, Golang", "AWS"], "Visa flags": []})
    assert props["Tech stack"] == {"multi_select": [{"name": "AWS"}, {"name": "Go  Golang"}]}
    assert props["Visa flags"] == {"multi_select": []}


def test_read_body_reads_text_blocks(notion):
    s = load_settings("dev", environ={})
    notion.children["p1"] = [
        {"type": "paragraph",
         "paragraph": {"rich_text": [{"plain_text": "Description source: x"}]}},
        {"type": "image", "image": {}},
        {"type": "bulleted_list_item",
         "bulleted_list_item": {"rich_text": [{"plain_text": "Kubernetes"}]}},
    ]
    repo = jobs_repo_for(s, client_for(s))
    assert repo.read_body("p1") == ["Description source: x", "Kubernetes"]


def test_query_rows_sends_the_filter(notion):
    s = load_settings("dev", environ={})
    repo = jobs_repo_for(s, client_for(s))
    notion.query_pages = [{"results": [{"id": "p1", "properties": {
        "Company": {"type": "title", "title": [{"plain_text": "Vistula Cloud"}]},
        "Visa flags": {"type": "multi_select", "multi_select": [{"name": "On IND register"}]},
    }}]}]
    where = {"property": "Screen verdict", "select": {"equals": "Unscreened"}}
    rows = repo.query_rows(where)
    assert rows == [("p1", {"Company": "Vistula Cloud", "Visa flags": ["On IND register"]})]
    body = [b for m, p, _, b in notion.requests if p.endswith("/query")][0]
    assert body["filter"] == where
