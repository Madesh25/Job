import json
from datetime import date

import httpx
import pytest

from jobengine import http
from jobengine.bot_state import FakeBotState
from jobengine.config_store import ConfigStore
from jobengine.llm import (
    DEFAULT_MODEL,
    GeminiLLM,
    LLMError,
    ModelRouter,
    Usage,
    model_for,
    thinking_for,
)
from jobengine.screen import batch
from jobengine.screen.runner import fake_deps, screen_pending
from jobengine.settings import load_settings
from jobengine.track import costs

GOOD = {"candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]},
                        "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 1000, "cachedContentTokenCount": 200,
                          "candidatesTokenCount": 100, "thoughtsTokenCount": 50}}


def settings(env="prod", **environ):
    return load_settings(env, {"ANTHROPIC_API_KEY": "a-key", "GEMINI_API_KEY": "g-key",
                               **environ})


@pytest.fixture
def google(monkeypatch):
    """Answers each Gemini request with the next queued (status, body); keeps the requests."""
    seen, queue = [], []

    def handler(request):
        seen.append(request)
        status, body = queue.pop(0)
        return httpx.Response(status, json=body)

    http.set_transport(httpx.MockTransport(handler))
    monkeypatch.setattr(http, "_sleep", lambda _s: None)
    yield seen, queue
    http.set_transport(None)


def test_gemini_call_shape_and_cost(google):
    seen, queue = google
    queue.append((200, GOOD))
    usage = Usage()
    llm = GeminiLLM(settings(), usage)
    got = llm.complete_json("score", "rules", "job text", model="gemini-3.5-flash-lite",
                            max_tokens=1500, thinking="low")
    assert got == {"ok": True}
    request = seen[0]
    assert request.url.host == "generativelanguage.googleapis.com"
    assert request.url.path == "/v1beta/models/gemini-3.5-flash-lite:generateContent"
    assert request.headers["x-goog-api-key"] == "g-key"
    body = json.loads(request.content)
    assert body["systemInstruction"] == {"parts": [{"text": "rules"}]}
    assert body["contents"] == [{"role": "user", "parts": [{"text": "job text"}]}]
    assert body["generationConfig"] == {"maxOutputTokens": 3500,
                                        "responseMimeType": "application/json",
                                        "thinkingConfig": {"thinkingLevel": "low"}}
    # 800 fresh at $0.30, 200 cached at a tenth, 150 out (thinking included) at $2.50
    assert usage.cost == pytest.approx((800 * 0.30 + 200 * 0.03 + 150 * 2.50) / 1e6)
    assert (usage.input_tokens, usage.output_tokens, usage.cache_read) == (1000, 150, 200)


def test_no_thinking_setting_keeps_the_model_default(google):
    seen, queue = google
    queue.append((200, GOOD))
    GeminiLLM(settings()).complete_json("email", "s", "u", model="gemini-3.5-flash-lite",
                                        max_tokens=50)
    config = json.loads(seen[0].content)["generationConfig"]
    assert "thinkingConfig" not in config and config["maxOutputTokens"] == 2050


def test_broken_json_is_asked_again_once(google):
    seen, queue = google
    bad = {"candidates": [{"content": {"parts": [{"text": "Sure! here"}]}}]}
    queue.extend([(200, bad), (200, GOOD)])
    assert GeminiLLM(settings()).complete_json("score", "s", "u",
                                               model="gemini-3.5-flash-lite") == {"ok": True}
    second = json.loads(seen[1].content)["contents"]
    assert [c["role"] for c in second] == ["user", "model", "user"]
    queue.extend([(200, bad), (200, bad)])
    with pytest.raises(LLMError, match="not valid JSON twice"):
        GeminiLLM(settings()).complete_json("score", "s", "u", model="gemini-3.5-flash-lite")


def test_thought_parts_are_not_the_answer(google):
    _seen, queue = google
    queue.append((200, {"candidates": [{"content": {"parts": [
        {"text": "thinking {not json", "thought": True}, {"text": '{"a": 1}'}]}}]}))
    assert GeminiLLM(settings()).complete_json("score", "s", "u",
                                               model="gemini-3.5-flash-lite") == {"a": 1}


def test_google_error_text_is_shown(google):
    _seen, queue = google
    queue.append((400, {"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                  "message": "API key not valid. Please pass a valid key."}}))
    with pytest.raises(LLMError) as err:
        GeminiLLM(settings()).complete_json("score", "s", "u", model="gemini-3.5-flash-lite")
    assert "HTTP 400 INVALID_ARGUMENT API key not valid" in str(err.value)
    assert "g-key" not in str(err.value)


def test_blocked_answer_is_a_decline(google):
    _seen, queue = google
    queue.append((200, {"promptFeedback": {"blockReason": "SAFETY"}}))
    with pytest.raises(LLMError, match="declined"):
        GeminiLLM(settings()).complete_json("score", "s", "u", model="gemini-3.5-flash-lite")


def test_no_key_no_gemini():
    with pytest.raises(LLMError, match="GEMINI_API_KEY is not set"):
        GeminiLLM(load_settings("prod", {"ANTHROPIC_API_KEY": "a"}))


# ---------------------------------------------------------------- choosing the model


def test_new_stages_inherit_tailor_but_replies_stay_on_haiku():
    config = ConfigStore.from_values({"model.tailor": "claude-sonnet-5",
                                      "model.score": "gemini-3.5-flash-lite",
                                      "model.cover": "gemini-3.8-flash thinking:low"})
    s = settings()
    assert model_for("resume", config, s) == "claude-sonnet-5"
    assert model_for("prep", config, s) == "claude-sonnet-5"
    assert model_for("cover", config, s) == "gemini-3.8-flash"
    assert thinking_for("cover", config) == "low"
    assert thinking_for("score", config) is None
    assert model_for("reply", config, s) == DEFAULT_MODEL


def test_local_and_dev_still_use_haiku():
    config = ConfigStore.from_values({"model.score": "gemini-3.5-flash-lite"})
    assert model_for("score", config, settings("dev")) == DEFAULT_MODEL


class Recorder:
    """Stands in for AnthropicLLM or GeminiLLM: records the calls, answers or fails."""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail
        self.usage = Usage()

    def complete_json(self, stage, system, user, **kwargs):
        self.calls.append((stage, kwargs.get("model")))
        if self.fail:
            raise LLMError("Gemini call failed: HTTP 503 UNAVAILABLE overloaded")
        return {"from": kwargs.get("model")}

    def complete_json_with_search(self, stage, system, user, **kwargs):
        self.calls.append((stage, kwargs.get("model"), "search"))
        return {}, []

    def submit_batch(self, stage, system, items, **kwargs):
        return "batch_1"


def router(config, gemini_fails=False):
    claude, gemini = Recorder(), Recorder(fail=gemini_fails)
    return ModelRouter(settings(), ConfigStore.from_values(config), claude=claude,
                       gemini=gemini), claude, gemini


def test_each_stage_goes_to_its_provider():
    llm, claude, gemini = router({"model.score": "gemini-3.5-flash-lite"})
    assert llm.complete_json("score", "s", "u") == {"from": "gemini-3.5-flash-lite"}
    assert llm.complete_json("reply", "s", "u") == {"from": DEFAULT_MODEL}
    assert gemini.calls == [("score", "gemini-3.5-flash-lite")]
    assert claude.calls == [("reply", DEFAULT_MODEL)]


def test_a_failed_gemini_call_is_answered_by_haiku():
    llm, claude, gemini = router({"model.cover": "gemini-3.8-flash"}, gemini_fails=True)
    assert llm.complete_json("cover", "s", "u") == {"from": DEFAULT_MODEL}
    assert gemini.calls == [("cover", "gemini-3.8-flash")]
    assert claude.calls == [("cover", DEFAULT_MODEL)]
    assert llm.usage.fallbacks == 1
    llm.usage.calls = 1
    assert "Gemini failed 1 time, claude-haiku-4-5 answered" in llm.usage.line()


def test_missing_gemini_key_falls_back_too():
    claude = Recorder()
    llm = ModelRouter(load_settings("prod", {"ANTHROPIC_API_KEY": "a"}),
                      ConfigStore.from_values({"model.score": "gemini-3.5-flash-lite"}),
                      claude=claude)
    assert llm.complete_json("score", "s", "u") == {"from": DEFAULT_MODEL}
    assert llm.usage.fallbacks == 1


def test_web_search_and_batch_stay_on_claude():
    llm, claude, _gemini = router({"model.strategy": "gemini-3.8-flash",
                                   "model.score": "gemini-3.5-flash-lite"})
    llm.complete_json_with_search("strategy", "s", "u")
    assert claude.calls == [("strategy", DEFAULT_MODEL, "search")]
    assert not llm.batch_ok("score")
    with pytest.raises(LLMError, match="needs a Claude model"):
        llm.submit_batch("score", "s", [("a", "u")])
    claude_only, _c, _g = router({})
    assert claude_only.batch_ok("score")
    assert claude_only.submit_batch("score", "s", [("a", "u")]) == "batch_1"


def test_batch_screens_at_once_when_screening_runs_on_gemini():
    s = load_settings("local", environ={})
    normal = {r.page_id: r.verdict for r in screen_pending(s, fake_deps(s), date(2026, 10, 1))
              .results}
    deps = fake_deps(s)
    deps.llm(None).batch_ok = lambda _stage: False
    got = batch.submit(s, deps, date(2026, 10, 1))
    assert got.errors[0] == batch.NO_GEMINI_BATCH
    assert {r.page_id: r.verdict for r in got.results} == normal
    assert deps.state.get(batch.STATE_KEY) is None  # no batch waits


def test_month_line_adds_old_and_new_resume_stages():
    state = FakeBotState()
    day = date(2026, 10, 20)
    for stage, dollars in (("tailor", 0.2), ("resume", 0.3), ("cover", 0.1), ("reply", 0.05)):
        costs.record(state, day, stage, dollars)
    line = costs.month_line(state, day)
    assert "(resumes $0.50, cover letters $0.10, replies $0.05)" in line
