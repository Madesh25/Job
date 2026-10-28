"""Gemini on Google Cloud Vertex AI (28 Oct): the Google Cloud credit pays for these calls (it
does not pay for the Gemini API). The PC uses Gemini for every AI job; Haiku answers only when
a Gemini call fails, and "Rebuild with Claude" stays Claude."""

import json

import httpx
import pytest
from test_llm_gemini import GOOD, Recorder

from jobengine import http
from jobengine.config_store import ConfigStore
from jobengine.llm import (
    DEFAULT_MODEL,
    GeminiLLM,
    ModelRouter,
    gemini_key_problem,
    gemini_route,
    vertex_host,
)
from jobengine.safety import assert_fetch_allowed
from jobengine.settings import load_settings
from jobengine.track.commands import gemini_line

MODEL = "gemini-3.5-flash-lite"
VERTEX_PATH = ("/v1/projects/job-search-engine-509510/locations/global/publishers/google/"
               f"models/{MODEL}:generateContent")


def local(**environ):
    return load_settings("local", {"ANTHROPIC_API_KEY": "a-key", **environ})


@pytest.fixture
def google(monkeypatch):
    seen, queue = [], []

    def handler(request):
        seen.append(request)
        status, body = queue.pop(0)
        return httpx.Response(status, json=body)

    http.set_transport(httpx.MockTransport(handler))
    monkeypatch.setattr(http, "_sleep", lambda _s: None)
    yield seen, queue
    http.set_transport(None)


def test_vertex_is_used_before_the_gemini_api(google):
    seen, queue = google
    queue.append((200, GOOD))
    s = local(VERTEX_API_KEY="v-key", GEMINI_API_KEY="g-key")
    assert gemini_route(s) == "vertex"
    got = GeminiLLM(s).complete_json("score", "rules", "job text", model=MODEL)
    assert got == {"ok": True}
    request = seen[0]
    assert (request.url.host, request.url.path) == ("aiplatform.googleapis.com", VERTEX_PATH)
    assert request.headers["x-goog-api-key"] == "v-key"
    assert "key=" not in str(request.url)  # the key never goes in the address
    body = json.loads(request.content)
    assert body["contents"][0]["role"] == "user"
    assert body["generationConfig"]["responseMimeType"] == "application/json"


def test_web_search_on_vertex(google):
    seen, queue = google
    queue.append((200, {"candidates": [{"content": {"parts": [{"text": '{"people": []}'}]},
                                        "finishReason": "STOP"}]}))
    raw, urls = GeminiLLM(local(VERTEX_API_KEY="v-key")).complete_json_with_search(
        "people", "rules", "who", model=MODEL)
    assert raw == {"people": []} and urls == []
    assert json.loads(seen[0].content)["tools"] == [{"googleSearch": {}}]


def test_without_vertex_key_the_gemini_api_is_used():
    s = local(GEMINI_API_KEY="g-key")
    assert gemini_route(s) == "gemini api"
    assert GeminiLLM(s)._url(MODEL).startswith("https://generativelanguage.googleapis.com/")
    assert gemini_route(local()) is None


def test_hosts_are_allowed():
    s = local(VERTEX_API_KEY="v-key")
    assert vertex_host("europe-west1") == "europe-west1-aiplatform.googleapis.com"
    assert_fetch_allowed(GeminiLLM(s)._url(MODEL), s)


def test_the_pc_uses_gemini_and_falls_back_to_haiku():
    gemini, claude = Recorder(fail=True), Recorder()
    llm = ModelRouter(local(VERTEX_API_KEY="v-key"), ConfigStore.from_values({}),
                      claude=claude, gemini=gemini)
    assert llm.model("score") == MODEL and llm.model("people") == MODEL
    assert llm.complete_json("score", "s", "u") == {"from": DEFAULT_MODEL}
    assert gemini.calls == [("score", MODEL)] and claude.calls == [("score", DEFAULT_MODEL)]
    assert not llm.batch_ok("score")  # the half-price batch is Claude only
    llm.overrides["resume"] = "claude-sonnet-5"  # Rebuild with Claude
    assert llm.model("resume") == DEFAULT_MODEL  # Claude, as Haiku on the PC


def test_health_says_where_gemini_runs_and_if_google_answers():
    s = local(VERTEX_API_KEY="v-key")
    calls = []

    def ok(url, **kwargs):
        calls.append((url, kwargs["headers"]))

    assert gemini_key_problem(s, post=ok) is None
    assert calls[0][0].endswith(VERTEX_PATH) and calls[0][1] == {"x-goog-api-key": "v-key"}

    def refused(url, **kwargs):
        raise http.HttpError("POST aiplatform.googleapis.com failed: HTTP 403", status=403)

    assert gemini_key_problem(s, post=refused).startswith("refused by Google (403)")
    assert gemini_key_problem(local()).startswith("no key")
    assert gemini_line(s, lambda: None) == (
        f"{MODEL}, Vertex AI, paid by your Google Cloud credit: OK")
