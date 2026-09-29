"""The only way to call an LLM. This is the only file under src/ that imports `anthropic`.

The model for a stage comes from Config `model.<stage>` and goes through
safety.resolve_model, so local and dev always use haiku. Prompts are never logged (they hold
the job description and profile data); only stage, model and token counts are.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
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


# US dollars per million tokens (input, output), from Anthropic's price list. Cache reads cost
# a tenth of the input price, cache writes (5 minutes) a quarter more.
PRICES_PER_MTOK = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-5-5": (4.0, 20.0),
}
CACHE_READ_FACTOR = 0.1
CACHE_WRITE_FACTOR = 1.25
BATCH_FACTOR = 0.5  # the Message Batches API bills every token at half price


@dataclass
class Usage:
    """Tokens and estimated cost of the LLM calls of one run (one /screen, one resume)."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cost: float = 0.0
    unpriced: set[str] = field(default_factory=set)  # models missing from PRICES_PER_MTOK

    def add(self, model: str, usage: Any, factor: float = 1.0) -> None:
        def count(name: str) -> int:
            value = getattr(usage, name, None)
            return value if isinstance(value, int) else 0

        fresh, out = count("input_tokens"), count("output_tokens")
        read, write = count("cache_read_input_tokens"), count("cache_creation_input_tokens")
        self.calls += 1
        self.input_tokens += fresh + read + write
        self.output_tokens += out
        self.cache_read += read
        price = PRICES_PER_MTOK.get(model)
        if price is None:
            self.unpriced.add(model)
            return
        per_in, per_out = price[0] / 1e6, price[1] / 1e6
        self.cost += factor * (fresh * per_in + read * per_in * CACHE_READ_FACTOR
                               + write * per_in * CACHE_WRITE_FACTOR + out * per_out)

    def line(self) -> str | None:
        """"AI used: 3 calls, 5,210 tokens in, 1,340 out, about $0.0120", or None when no
        call was made."""
        if not self.calls:
            return None
        text = (f"AI used: {self.calls} call{'s' if self.calls != 1 else ''}, "
                f"{self.input_tokens:,} tokens in, {self.output_tokens:,} out, "
                f"about ${self.cost:.4f}")
        if self.unpriced:
            text += f" (no price for {', '.join(sorted(self.unpriced))})"
        return text


class LLMError(Exception):
    """An LLM call could not produce a usable JSON object."""


class LLMAuthError(LLMError):
    """The API key was refused (HTTP 401 or 403): every further call would fail too."""


@dataclass
class BatchStatus:
    """Where a submitted batch is: `ended` once every request has an answer."""

    ended: bool
    done: int
    total: int


class LLMClient(Protocol):
    def complete_json(
        self, stage: str, system: str, user: str, *, max_tokens: int = 2000,
        key: str | None = None,
    ) -> dict[str, Any]: ...


class SearchLLM(LLMClient, Protocol):
    def complete_json_with_search(
        self, stage: str, system: str, user: str, *, max_tokens: int = 4000,
        max_uses: int = 8, key: str | None = None,
    ) -> tuple[dict[str, Any], list[str]]: ...


class BatchLLM(LLMClient, Protocol):
    """Half-price, asynchronous calls (Message Batches API): submit now, collect later."""

    def submit_batch(
        self, stage: str, system: str, items: list[tuple[str, str]], *, max_tokens: int = 2000,
    ) -> str: ...

    def batch_status(self, batch_id: str) -> BatchStatus: ...

    def batch_results(self, batch_id: str, stage: str) -> dict[str, dict[str, Any] | str]: ...


# Anthropic's server-side web search tool (basic version: works on every current model,
# including the haiku that local and dev are forced to use).
WEB_SEARCH_TOOL = "web_search_20250305"
SEARCH_STAGES = ("strategy",)  # the only stage that may search the web
MAX_CONTINUATIONS = 4


def _field(obj: Any, name: str) -> Any:
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def search_urls(content: list[Any]) -> list[str]:
    """Every URL in the web search results and citations of a response, in order."""
    urls: list[str] = []
    for block in content:
        kind = _field(block, "type")
        if kind == "web_search_tool_result":
            results = _field(block, "content")
            for item in results if isinstance(results, list) else []:
                if _field(item, "url"):
                    urls.append(_field(item, "url"))
        elif kind == "text":
            for citation in _field(block, "citations") or []:
                if _field(citation, "url"):
                    urls.append(_field(citation, "url"))
    return list(dict.fromkeys(urls))


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
        self.usage = Usage()

    def _create(self, **kwargs: Any) -> Any:
        import anthropic

        try:
            return self._client.messages.create(**kwargs)
        except anthropic.APIStatusError as exc:
            if exc.status_code in (401, 403):
                raise LLMAuthError(
                    f"Anthropic refused ANTHROPIC_API_KEY (HTTP {exc.status_code}): the key "
                    "may be wrong or revoked, or the account may have no credit. The key is "
                    "read only when the bot starts: put the new key in .env, then stop the bot "
                    "(Ctrl+C) and start it again. On Cloud Run: add a new version of the "
                    "Anthropic secret and redeploy."
                ) from None
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
            # SDK 1.x removed the temperature keyword; the API still takes it for these models.
            kwargs["extra_body"] = {"temperature": 0}
        for attempt in (1, 2):
            response = self._create(messages=messages, **kwargs)
            usage = getattr(response, "usage", None)
            self.usage.add(model, usage)
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

    def _batch_call(self, call: Any) -> Any:
        import anthropic

        try:
            return call()
        except anthropic.APIStatusError as exc:
            if exc.status_code in (401, 403):
                raise LLMAuthError(f"Anthropic refused ANTHROPIC_API_KEY (HTTP {exc.status_code})"
                                   ) from None
            raise LLMError(f"LLM batch call failed: HTTP {exc.status_code}") from None
        except anthropic.APIConnectionError:
            raise LLMError("LLM batch call failed: connection error") from None

    def submit_batch(
        self, stage: str, system: str, items: list[tuple[str, str]], *, max_tokens: int = 2000,
    ) -> str:
        """Send (key, user prompt) pairs as one batch; the key comes back with each answer.
        Returns the batch ID. Keys: letters, digits, - and _, at most 64 characters."""
        _check_stage(stage)
        model = model_for(stage, self.config, self.s)
        requests = []
        for key, user in items:
            params: dict[str, Any] = {
                "model": model,
                "max_tokens": max_tokens,
                "system": [{"type": "text", "text": system,
                            "cache_control": {"type": "ephemeral"}}],
                "messages": [{"role": "user", "content": user}],
            }
            if supports_temperature(model):
                params["temperature"] = 0
            requests.append({"custom_id": key, "params": params})
        batch = self._batch_call(lambda: self._client.messages.batches.create(requests=requests))
        log.info("llm batch stage=%s model=%s requests=%d id=%s", stage, model, len(items),
                 batch.id)
        return batch.id

    def batch_status(self, batch_id: str) -> BatchStatus:
        batch = self._batch_call(lambda: self._client.messages.batches.retrieve(batch_id))
        counts = batch.request_counts
        done = counts.succeeded + counts.errored + counts.canceled + counts.expired
        return BatchStatus(ended=batch.processing_status == "ended", done=done,
                           total=done + counts.processing)

    def batch_results(self, batch_id: str, stage: str) -> dict[str, dict[str, Any] | str]:
        """key -> the parsed JSON object, or an error message for that request."""
        _check_stage(stage)
        out: dict[str, dict[str, Any] | str] = {}
        items = self._batch_call(lambda: list(self._client.messages.batches.results(batch_id)))
        for item in items:
            result = item.result
            if result.type != "succeeded":
                out[item.custom_id] = f"batch request {result.type}"
                continue
            message = result.message
            self.usage.add(message.model, message.usage, BATCH_FACTOR)
            if getattr(message, "stop_reason", None) == "refusal":
                out[item.custom_id] = f"LLM declined the {stage} request"
                continue
            text = "".join(b.text for b in message.content if getattr(b, "type", "") == "text")
            try:
                out[item.custom_id] = parse_json_object(text)
            except ValueError:
                out[item.custom_id] = f"LLM reply for {stage} was not valid JSON"
        return out

    def complete_json_with_search(
        self, stage: str, system: str, user: str, *, max_tokens: int = 4000,
        max_uses: int = 8, key: str | None = None,
    ) -> tuple[dict[str, Any], list[str]]:
        """complete_json with the web search tool enabled (at most `max_uses` searches).
        Returns the JSON object and the URLs the searches returned."""
        _check_stage(stage)
        if stage not in SEARCH_STAGES:
            raise LLMError(f"web search is not allowed for stage {stage}")
        model = model_for(stage, self.config, self.s)
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system}],
            "tools": [{"type": WEB_SEARCH_TOOL, "name": "web_search", "max_uses": max_uses}],
        }
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        urls: list[str] = []
        retried = False
        for _ in range(MAX_CONTINUATIONS + 2):
            response = self._create(messages=messages, **kwargs)
            usage = getattr(response, "usage", None)
            self.usage.add(model, usage)  # web searches are billed on top, not counted here
            log.info("llm stage=%s model=%s input_tokens=%s output_tokens=%s searches=%s",
                     stage, model, getattr(usage, "input_tokens", None),
                     getattr(usage, "output_tokens", None),
                     getattr(getattr(usage, "server_tool_use", None), "web_search_requests",
                             None))
            content = list(response.content)
            urls.extend(search_urls(content))
            stop = getattr(response, "stop_reason", None)
            if stop == "refusal":
                raise LLMError(f"LLM declined the {stage} request")
            if stop == "pause_turn":  # a long search turn: send it back to continue
                messages = [*messages, {"role": "assistant", "content": content}]
                continue
            text = "".join(_field(b, "text") or "" for b in content
                           if _field(b, "type") == "text")
            try:
                return parse_json_object(text), list(dict.fromkeys(urls))
            except ValueError:
                if retried:
                    raise LLMError(f"LLM reply for {stage} was not valid JSON twice") from None
                retried = True
                messages = [*messages, {"role": "assistant", "content": content},
                            {"role": "user", "content": RETRY_INSTRUCTION}]
        raise LLMError(f"LLM {stage} research did not finish")


class FakeLLM:
    """Returns fixtures/llm/<stage>/<key>.json and records every call."""

    def __init__(self, base: Path = FIXTURES, default: dict[str, Any] | None = None):
        """`default` answers keys without a fixture (the fake bot uses {}: nothing stated)."""
        self.base = base
        self.default = default
        self.calls: list[dict[str, Any]] = []
        self.usage = Usage()  # stays empty: fixtures cost nothing
        self.batches: dict[str, tuple[str, str, list[tuple[str, str]]]] = {}
        self.batch_ended = True  # tests set False to see a batch still processing

    def complete_json(
        self, stage: str, system: str, user: str, *, max_tokens: int = 2000,
        key: str | None = None,
    ) -> dict[str, Any]:
        _check_stage(stage)
        self.calls.append({"stage": stage, "key": key, "system": system, "user": user})
        if not key:
            raise LLMError(f"FakeLLM needs a fixture key for stage {stage}")
        path = self.base / stage / f"{key}.json"
        if not path.exists() and self.default is not None:
            return json.loads(json.dumps(self.default))
        if not path.exists():
            raise LLMError(f"FakeLLM has no fixture {stage}/{key}.json")
        return json.loads(path.read_text(encoding="utf-8"))

    def complete_json_with_search(
        self, stage: str, system: str, user: str, *, max_tokens: int = 4000,
        max_uses: int = 8, key: str | None = None,
    ) -> tuple[dict[str, Any], list[str]]:
        """fixtures/llm/<stage>/<key>.json holds {"reply": {...}, "search_urls": [...]}."""
        _check_stage(stage)
        if stage not in SEARCH_STAGES:
            raise LLMError(f"web search is not allowed for stage {stage}")
        self.calls.append({"stage": stage, "key": key, "system": system, "user": user,
                           "max_uses": max_uses, "search": True})
        path = self.base / stage / f"{key}.json"
        if not key or not path.exists():
            raise LLMError(f"FakeLLM has no fixture {stage}/{key}.json")
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("reply") or {}, list(data.get("search_urls") or [])

    def submit_batch(
        self, stage: str, system: str, items: list[tuple[str, str]], *, max_tokens: int = 2000,
    ) -> str:
        _check_stage(stage)
        batch_id = f"fakebatch_{len(self.batches) + 1}"
        self.batches[batch_id] = (stage, system, list(items))
        return batch_id

    def batch_status(self, batch_id: str) -> BatchStatus:
        total = len(self.batches[batch_id][2])
        return BatchStatus(ended=self.batch_ended, done=total if self.batch_ended else 0,
                           total=total)

    def batch_results(self, batch_id: str, stage: str) -> dict[str, dict[str, Any] | str]:
        """Each key answers like complete_json would (fixture, default, or an error)."""
        _, system, items = self.batches[batch_id]
        out: dict[str, dict[str, Any] | str] = {}
        for key, user in items:
            try:
                out[key] = self.complete_json(stage, system, user, key=key)
            except LLMError as exc:
                out[key] = str(exc)
        return out
