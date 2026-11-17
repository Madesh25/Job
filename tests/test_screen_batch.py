from datetime import date
from types import SimpleNamespace

import pytest

from jobengine.config_store import ConfigStore
from jobengine.llm import AnthropicLLM, BatchStatus
from jobengine.screen import batch
from jobengine.screen.runner import fake_deps, screen_pending
from jobengine.settings import load_settings

TODAY = date(2026, 10, 1)


@pytest.fixture
def s():
    return load_settings("local", environ={})


def verdicts(summary):
    return {r.page_id: (r.verdict, r.skip_reason) for r in summary.results}


def test_batch_then_collect_gives_the_same_verdicts_as_a_normal_run(s):
    normal = verdicts(screen_pending(s, fake_deps(s), TODAY))
    deps = fake_deps(s)
    sent = batch.submit(s, deps, TODAY)
    assert any("Sent" in line and "half-price batch" in line for line in sent.errors)
    free = verdicts(sent)  # free skips and rows waiting for a description, written at once
    assert all(v == ("Skip", v[1]) or v[0] == "Unscreened" for v in free.values())
    got = verdicts(batch.collect(s, deps, TODAY))
    assert {**free, **got} == normal
    assert deps.state.get(batch.STATE_KEY) is None  # collected: nothing waits any more


def test_rows_in_a_waiting_batch_are_not_screened_twice(s):
    deps = fake_deps(s)
    llm = deps.llm(None)
    llm.batch_ended = False
    batch.submit(s, deps, TODAY)
    waiting = batch.waiting_rows(deps)
    assert waiting
    again = screen_pending(s, deps, TODAY)
    assert not waiting & {r.page_id for r in again.results}
    assert any("wait in a half-price batch" in line for line in again.errors)
    assert "already waiting" in batch.submit(s, deps, TODAY).errors[0]
    early = batch.collect(s, deps, TODAY)
    assert "still working" in early.errors[0] and not early.results
    assert batch.waiting_rows(deps) == waiting


def test_a_failed_answer_leaves_the_row_unscreened(s):
    deps = fake_deps(s)
    llm = deps.llm(None)
    batch.submit(s, deps, TODAY)
    batch_id = deps.state.get(batch.STATE_KEY)["id"]
    first = llm.batches[batch_id][2][0][0]
    real = llm.batch_results
    llm.batch_results = lambda bid, stage: {**real(bid, stage), first: "batch request expired"}
    got = batch.collect(s, deps, TODAY)
    assert any("batch request expired" in line and "still Unscreened" in line
               for line in got.errors)
    assert deps.repo.rows[first]["Screen verdict"] == "Unscreened"


def test_nothing_is_sent_without_a_place_to_remember_the_batch(s):
    deps = fake_deps(s)
    deps.state = None
    assert "needs Bot State" in batch.submit(s, deps, TODAY).errors[0]
    assert batch.collect(s, fake_deps(s), TODAY).errors == [batch.NO_BATCH]


# ---------------------------------------------------------------- the real client, faked SDK


class FakeBatches:
    def __init__(self):
        self.created = None

    def create(self, *, requests):
        self.created = requests
        return SimpleNamespace(id="msgbatch_1")

    def retrieve(self, batch_id):
        counts = SimpleNamespace(processing=0, succeeded=2, errored=0, canceled=0, expired=1)
        return SimpleNamespace(processing_status="ended", request_counts=counts)

    def results(self, batch_id):
        def ok(key, text):
            message = SimpleNamespace(
                model="claude-haiku-4-5", stop_reason="end_turn",
                content=[SimpleNamespace(type="text", text=text)],
                usage=SimpleNamespace(input_tokens=1_000_000, output_tokens=0,
                                      cache_read_input_tokens=0, cache_creation_input_tokens=0))
            return SimpleNamespace(custom_id=key,
                                   result=SimpleNamespace(type="succeeded", message=message))
        return iter([ok("a", '{"work_mode": null}'), ok("b", "not json"),
                     SimpleNamespace(custom_id="c", result=SimpleNamespace(type="expired"))])


def test_anthropic_client_sends_and_reads_a_batch():
    fake = FakeBatches()
    sdk = SimpleNamespace(messages=SimpleNamespace(batches=fake))
    llm = AnthropicLLM(load_settings("dev", {"ANTHROPIC_API_KEY": "k"}),
                       ConfigStore.from_values({}), client=sdk)
    assert llm.submit_batch("score", "rules", [("a", "job a"), ("b", "job b")]) == "msgbatch_1"
    params = fake.created[0]["params"]
    assert fake.created[0]["custom_id"] == "a" and params["model"] == "claude-haiku-4-5"
    assert params["temperature"] == 0 and params["system"][0]["cache_control"]
    assert llm.batch_status("msgbatch_1") == BatchStatus(ended=True, done=3, total=3)
    out = llm.batch_results("msgbatch_1", "score")
    assert out["a"] == {"work_mode": None}
    assert "not valid JSON" in out["b"] and out["c"] == "batch request expired"
    assert round(llm.usage.cost, 2) == 1.0  # 2M input tokens at half the $1 price
