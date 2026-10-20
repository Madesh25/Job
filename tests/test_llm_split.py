"""20 Oct: DeepSeek for screening only, Gemini with Google Search for /update, and the
"Rebuild with Claude" button under a resume Gemini wrote."""

import json

import httpx
import pytest
from test_telegram_bot import desk, talk, tap  # noqa: F401  (desk is a fixture)

from jobengine import http
from jobengine.config_store import ConfigStore
from jobengine.llm import (
    DEFAULT_MODEL,
    DeepSeekLLM,
    GeminiLLM,
    LLMError,
    ModelRouter,
    Usage,
    attach_sources,
    rebuild_model,
)
from jobengine.settings import load_settings

REDIRECT = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc"
REAL = "https://www.ind.nl/en/residence-permits/work/highly-skilled-migrant"


def settings(env="prod", **environ):
    return load_settings(env, {"ANTHROPIC_API_KEY": "a-key", "GEMINI_API_KEY": "g-key",
                               "DEEPSEEK_API_KEY": "d-key", **environ})


@pytest.fixture
def web(monkeypatch):
    """Answers each request with the next queued httpx.Response; keeps the requests."""
    seen, queue = [], []

    def handler(request):
        seen.append(request)
        return queue.pop(0)

    http.set_transport(httpx.MockTransport(handler))
    monkeypatch.setattr(http, "_sleep", lambda _s: None)
    yield seen, queue
    http.set_transport(None)


def deepseek_answer(text='{"ok": true}'):
    return httpx.Response(200, json={
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1300, "completion_tokens": 350,
                  "prompt_cache_hit_tokens": 300}})


def test_deepseek_call_shape_and_cost(web):
    seen, queue = web
    queue.append(deepseek_answer())
    usage = Usage()
    got = DeepSeekLLM(settings(), usage).complete_json("score", "rules", "job text",
                                                        model="deepseek-v4-flash")
    assert got == {"ok": True}
    request = seen[0]
    assert str(request.url) == "https://api.deepseek.com/chat/completions"
    assert request.headers["authorization"] == "Bearer d-key"
    body = json.loads(request.content)
    assert body["model"] == "deepseek-v4-flash"
    assert body["messages"] == [{"role": "system", "content": "rules"},
                                {"role": "user", "content": "job text"}]
    assert body["response_format"] == {"type": "json_object"}
    assert usage.cost == pytest.approx((1000 * 0.14 + 300 * 0.014 + 350 * 0.28) / 1e6)
    assert usage.line().endswith("with deepseek-v4-flash")


def test_deepseek_never_gets_your_resume_or_mail():
    for stage in ("resume", "cover", "prep", "reply", "email", "strategy"):
        with pytest.raises(LLMError, match="DeepSeek is only used for score"):
            DeepSeekLLM(settings()).complete_json(stage, "s", "u", model="deepseek-v4-flash")


class Recorder:
    def __init__(self, fail=False, search=None):
        self.calls, self.fail, self.search, self.usage = [], fail, search, Usage()

    def complete_json(self, stage, system, user, **kwargs):
        self.calls.append((stage, kwargs.get("model")))
        if self.fail:
            raise LLMError("call failed: HTTP 503")
        return {"from": kwargs.get("model")}

    def complete_json_with_search(self, stage, system, user, **kwargs):
        self.calls.append((stage, kwargs.get("model"), "search"))
        if self.fail:
            raise LLMError("call failed: HTTP 503")
        return {"from": kwargs.get("model")}, ["https://example.com/a"]


def router(config, **clients):
    claude = Recorder()
    llm = ModelRouter(settings(), ConfigStore.from_values(config), claude=claude, **clients)
    return llm, claude


def test_deepseek_in_another_stage_is_replaced_by_haiku():
    deepseek = Recorder()
    llm, claude = router({"model.score": "deepseek-v4-flash",
                          "model.cover": "deepseek-v4-flash"}, deepseek=deepseek)
    assert llm.complete_json("score", "s", "u") == {"from": "deepseek-v4-flash"}
    assert llm.complete_json("cover", "s", "u") == {"from": DEFAULT_MODEL}
    assert deepseek.calls == [("score", "deepseek-v4-flash")]
    assert claude.calls == [("cover", DEFAULT_MODEL)]
    assert not llm.batch_ok("score")


def test_a_failed_deepseek_call_is_answered_by_haiku():
    llm, claude = router({"model.score": "deepseek-v4-flash"}, deepseek=Recorder(fail=True))
    assert llm.complete_json("score", "s", "u") == {"from": DEFAULT_MODEL}
    assert llm.usage.fallbacks == 1


def test_update_runs_on_gemini_with_haiku_as_the_safety_net():
    gemini = Recorder()
    llm, claude = router({"model.strategy": "gemini-3.5-flash-lite"}, gemini=gemini)
    assert llm.complete_json_with_search("strategy", "s", "u")[0] == {
        "from": "gemini-3.5-flash-lite"}
    assert claude.calls == []
    broken, claude = router({"model.strategy": "gemini-3.5-flash-lite"},
                            gemini=Recorder(fail=True))
    broken.complete_json_with_search("strategy", "s", "u")
    assert claude.calls == [("strategy", DEFAULT_MODEL, "search")]
    deep, claude = router({"model.strategy": "deepseek-v4-flash"})
    deep.complete_json_with_search("strategy", "s", "u")
    assert claude.calls == [("strategy", DEFAULT_MODEL, "search")]  # DeepSeek cannot search


def test_gemini_search_gives_real_addresses_and_sources(web):
    seen, queue = web
    tips = {"tips": [{"tip": "Mention the highly skilled migrant permit in the letter.",
                      "why": "Dutch employers must be recognised sponsors.", "sources": []}]}
    queue.append(httpx.Response(200, json={
        "candidates": [{"content": {"parts": [{"text": json.dumps(tips)}]},
                        "finishReason": "STOP",
                        "groundingMetadata": {
                            "webSearchQueries": ["highly skilled migrant permit 2026"],
                            "groundingChunks": [{"web": {"uri": REDIRECT, "title": "ind.nl"}}],
                            "groundingSupports": [{"segment": {
                                "text": "Mention the highly skilled migrant permit"},
                                "groundingChunkIndices": [0]}]}}],
        "usageMetadata": {"promptTokenCount": 2000, "candidatesTokenCount": 500}}))
    queue.append(httpx.Response(302, headers={"location": REAL}))
    usage = Usage()
    raw, urls = GeminiLLM(settings(), usage).complete_json_with_search(
        "strategy", "rules", "research", model="gemini-3.5-flash-lite")
    body = json.loads(seen[0].content)
    assert body["tools"] == [{"google_search": {}}]
    assert "responseMimeType" not in body["generationConfig"]
    assert seen[1].url.host == "vertexaisearch.cloud.google.com"  # only the redirect is read
    assert len(seen) == 2
    assert urls == [REAL]
    assert raw["tips"][0]["sources"] == [REAL]
    assert usage.cost == pytest.approx((2000 * 0.30 + 500 * 2.50) / 1e6)  # search: month total


def test_a_short_or_unrelated_segment_adds_no_source():
    raw = {"tips": [{"tip": "Use the job title from the posting.", "why": "ATS match."}]}
    attach_sources(raw, [{"segment": {"text": "posting"}, "groundingChunkIndices": [0]},
                         {"segment": {"text": "something else entirely here"},
                          "groundingChunkIndices": [0]}], [REAL])
    assert "sources" not in raw["tips"][0]


def test_not_a_redirect_gives_no_address(web):
    _seen, queue = web
    queue.append(httpx.Response(200, text="hello"))
    assert http.redirect_location(REDIRECT, settings()) is None


def test_rebuild_model_is_always_claude():
    assert rebuild_model(ConfigStore.from_values({})) == DEFAULT_MODEL
    assert rebuild_model(ConfigStore.from_values(
        {"model.resume_rebuild": "claude-sonnet-5"})) == "claude-sonnet-5"
    assert rebuild_model(ConfigStore.from_values(
        {"model.resume_rebuild": "gemini-3.8-flash"})) == DEFAULT_MODEL


class GeminiResumes:
    """The fake desk's LLM, answering like a router whose resume model is Gemini."""

    def __init__(self, inner):
        self.inner, self.usage, self.overrides = inner, inner.usage, {}

    def model(self, stage):
        return self.overrides.get(stage, "gemini-3.5-flash-lite")

    def complete_json(self, *args, **kwargs):
        return self.inner.complete_json(*args, **kwargs)


def test_rebuild_with_claude_button(desk):  # noqa: F811
    inner = desk.resume.llm
    used = []

    def llm(config):
        client = GeminiResumes(inner(config))
        used.append(client)
        return client

    desk.resume.llm = llm
    fake = talk(desk, tap(1, "ap:pl-clean"))
    assert any("cl:pl-clean" in row for row in fake.buttons)
    fake = talk(desk, tap(2, "cl:pl-clean"))
    assert fake.sent[0][1] == "[LOCAL] Building resume for Vistula Cloud with Claude..."
    assert "(version 2)" in fake.sent[1][1]
    assert used[-1].overrides == {"resume": DEFAULT_MODEL}
    assert not any("cl:pl-clean" in row for row in fake.buttons)  # Claude wrote this one
