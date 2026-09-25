"""The only way to call an LLM. This is the only file under src/ that imports `anthropic`.

The model for a stage comes from Config `model.<stage>` and goes through
safety.resolve_model, so local and dev always use haiku. Prompts are never logged (they hold
the job description and profile data); only stage, model and token counts are.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Protocol

from jobengine.config_store import ConfigStore
from jobengine.safety import llm_allowed, resolve_model
from jobengine.settings import ROOT_DIR, Settings

log = logging.getLogger("jobengine.llm")

STAGES = ("fetch", "score", "tailor", "email", "strategy")
DEFAULT_MODEL = "claude-haiku-4-5"
FIXTURES = ROOT_DIR / "fixtures" / "llm"
RETRY_INSTRUCTION = "Return only valid JSON: a single JSON object, no prose, no code fences."
# Newer models reject sampling parameters (temperature, top_p) with a 400.
NO_SAMPLING_PREFIXES = (
    "claude-opus-4-7", "claude-opus-4-8", "claude-opus-5", "claude-sonnet-5",
    "claude-fable", "claude-mythos",
)


class LLMError(Exception):
    """An LLM call could not produce a usable JSON object."""


class LLMClient(Protocol):
    def complete_json(
        self, stage: str, system: str, user: str, *, max_tokens: int = 2000,
        key: str | None = None,
    ) -> dict[str, Any]: ...


def _check_stage(stage: str) -> None:
    if stage not in STAGES:
        raise LLMError(f"unknown LLM stage {stage!r}; allowed: {', '.join(STAGES)}")


def model_for(stage: str, config: ConfigStore, s: Settings) -> str:
    """Config model.<stage> (first word only, e.g. "claude-sonnet-5 + web search"),
    then safety.resolve_model, which forces haiku outside prod."""
    raw = config.get(f"model.{stage}") or DEFAULT_MODEL
    return resolve_model(raw.split()[0], s)


def supports_temperature(model: str) -> bool:
    return not model.startswith(NO_SAMPLING_PREFIXES)


def parse_json_object(text: str) -> dict[str, Any]:
    """The single JSON object in a reply, tolerating code fences. Raises ValueError."""
    cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in reply")
    value = json.loads(cleaned[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("reply is not a JSON object")
    return value


class AnthropicLLM:
    """Real client over the official anthropic SDK."""

    def __init__(self, s: Settings, config: ConfigStore, client: Any = None):
        if not llm_allowed(s):
            raise LLMError("ANTHROPIC_API_KEY is not set, so LLM screening cannot run")
        self.s = s
        self.config = config
        if client is None:
            import anthropic

            client = anthropic.Anthropic(api_key=s.anthropic_api_key, max_retries=2, timeout=120)
        self._client = client

    def _create(self, **kwargs: Any) -> Any:
        import anthropic

        try:
            return self._client.messages.create(**kwargs)
        except anthropic.APIStatusError as exc:
            raise LLMError(f"LLM call failed: HTTP {exc.status_code}") from None
        except anthropic.APIConnectionError:
            raise LLMError("LLM call failed: connection error") from None

    def complete_json(
        self, stage: str, system: str, user: str, *, max_tokens: int = 2000,
        key: str | None = None,
    ) -> dict[str, Any]:
        _check_stage(stage)
        model = model_for(stage, self.config, self.s)
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            # The system prompt holds the static rules and reference text: cache it.
            # The job text goes in the user message, after the cached prefix.
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        }
        if supports_temperature(model):
            kwargs["temperature"] = 0
        for attempt in (1, 2):
            response = self._create(messages=messages, **kwargs)
            usage = getattr(response, "usage", None)
            log.info(
                "llm stage=%s model=%s input_tokens=%s output_tokens=%s cache_read=%s",
                stage, model, getattr(usage, "input_tokens", None),
                getattr(usage, "output_tokens", None),
                getattr(usage, "cache_read_input_tokens", None),
            )
            if getattr(response, "stop_reason", None) == "refusal":
                raise LLMError(f"LLM declined the {stage} request")
            text = "".join(
                block.text for block in response.content if getattr(block, "type", "") == "text"
            )
            try:
                return parse_json_object(text)
            except ValueError:
                if attempt == 2:
                    raise LLMError(f"LLM reply for {stage} was not valid JSON twice") from None
                messages = [
                    *messages,
                    {"role": "assistant", "content": text or "(empty)"},
                    {"role": "user", "content": RETRY_INSTRUCTION},
                ]
        raise LLMError("unreachable")  # pragma: no cover


class FakeLLM:
    """Returns fixtures/llm/<stage>/<key>.json and records every call."""

    def __init__(self, base: Path = FIXTURES):
        self.base = base
        self.calls: list[dict[str, Any]] = []

    def complete_json(
        self, stage: str, system: str, user: str, *, max_tokens: int = 2000,
        key: str | None = None,
    ) -> dict[str, Any]:
        _check_stage(stage)
        self.calls.append({"stage": stage, "key": key, "system": system, "user": user})
        if not key:
            raise LLMError(f"FakeLLM needs a fixture key for stage {stage}")
        path = self.base / stage / f"{key}.json"
        if not path.exists():
            raise LLMError(f"FakeLLM has no fixture {stage}/{key}.json")
        return json.loads(path.read_text(encoding="utf-8"))
