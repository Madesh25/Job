import logging
from types import SimpleNamespace

import pytest

from jobengine.config_store import ConfigStore
from jobengine.llm import (
    AnthropicLLM,
    FakeLLM,
    LLMError,
    model_for,
    parse_json_object,
    supports_temperature,
)
from jobengine.safety import llm_allowed
from jobengine.settings import load_settings

CONFIG = ConfigStore.from_values(
    {"model.score": "claude-sonnet-5", "model.strategy": "claude-sonnet-5 + web search"}
)


def reply(text, stop="end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)], stop_reason=stop,
        usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0),
    )


class FakeSDK:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.replies.pop(0)


def settings(env="dev", **environ):
    return load_settings(env, {"ANTHROPIC_API_KEY": "test-key", **environ})


def test_dev_forces_haiku_even_when_config_says_sonnet():
    sdk = FakeSDK([reply('{"ok": true}')])
    llm = AnthropicLLM(settings("dev"), CONFIG, client=sdk)
    assert llm.complete_json("score", "rules", "job text") == {"ok": True}
    call = sdk.calls[0]
    assert call["model"] == "claude-haiku-4-5"
    assert call["temperature"] == 0
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert call["messages"] == [{"role": "user", "content": "job text"}]


def test_prod_uses_config_model_without_temperature_for_new_models():
    sdk = FakeSDK([reply('{"ok": 1}')])
    AnthropicLLM(settings("prod"), CONFIG, client=sdk).complete_json("strategy", "s", "u")
    assert sdk.calls[0]["model"] == "claude-sonnet-5"
    assert "temperature" not in sdk.calls[0]
    assert model_for("score", ConfigStore.from_values({}), settings("prod")) == "claude-haiku-4-5"
    assert supports_temperature("claude-haiku-4-5")
    assert not supports_temperature("claude-opus-5")


def test_code_fences_are_stripped():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('Here it is: {"a": [1, 2]} done') == {"a": [1, 2]}
    with pytest.raises(ValueError):
        parse_json_object("[1, 2]")


def test_invalid_json_is_retried_once_then_fails():
    sdk = FakeSDK([reply("not json"), reply('{"fixed": true}')])
    llm = AnthropicLLM(settings(), CONFIG, client=sdk)
    assert llm.complete_json("score", "s", "u") == {"fixed": True}
    retry_messages = sdk.calls[1]["messages"]
    assert retry_messages[-1]["role"] == "user"
    assert "Return only valid JSON" in retry_messages[-1]["content"]

    sdk = FakeSDK([reply("nope"), reply("still nope")])
    with pytest.raises(LLMError, match="not valid JSON twice"):
        AnthropicLLM(settings(), CONFIG, client=sdk).complete_json("score", "s", "u")


def test_refusal_and_unknown_stage():
    sdk = FakeSDK([reply("", stop="refusal")])
    with pytest.raises(LLMError, match="declined"):
        AnthropicLLM(settings(), CONFIG, client=sdk).complete_json("score", "s", "u")
    with pytest.raises(LLMError, match="unknown LLM stage"):
        AnthropicLLM(settings(), CONFIG, client=FakeSDK([])).complete_json("chat", "s", "u")


def test_missing_key_is_a_clear_error_and_dry_run_does_not_block():
    no_key = load_settings("dev", {})
    assert not llm_allowed(no_key)
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY is not set"):
        AnthropicLLM(no_key, CONFIG, client=FakeSDK([]))
    dry = settings("dev", DRY_RUN="true")
    assert dry.dry_run and llm_allowed(dry)


def test_prompts_are_never_logged(caplog):
    sdk = FakeSDK([reply('{"ok": true}')])
    with caplog.at_level(logging.DEBUG):
        AnthropicLLM(settings(), CONFIG, client=sdk).complete_json(
            "score", "SECRET RULES", "SECRET JOB TEXT"
        )
    assert "SECRET" not in caplog.text
    assert "stage=score model=claude-haiku-4-5" in caplog.text


def test_fake_llm_reads_fixtures_and_records_calls(tmp_path):
    (tmp_path / "score").mkdir()
    (tmp_path / "score" / "job1.json").write_text('{"x": 1}')
    fake = FakeLLM(base=tmp_path)
    assert fake.complete_json("score", "s", "u", key="job1") == {"x": 1}
    assert fake.calls[0]["key"] == "job1"
    with pytest.raises(LLMError, match="no fixture"):
        fake.complete_json("score", "s", "u", key="missing")
