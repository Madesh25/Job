from types import SimpleNamespace

from jobengine.jdtrim import trim_jd
from jobengine.llm import Usage

RODO = (
    "Klikajac w przycisk Aplikuj, zgadzasz sie na przetwarzanie Twoich danych osobowych przez "
    "Example Sp. z o.o. z siedziba w Gdyni (Pracodawca), jako administratora danych osobowych "
    "w celu przeprowadzenia rekrutacji na stanowisko wskazane w ogloszeniu."
)
EEO = (
    "Example Hosting is an Equal Opportunity Employer. All qualified applicants will receive "
    "consideration for employment without regard to race, color, religion or belief, sex."
)
BODY = "\n".join([
    "Your responsibilities",
    "Run our Kubernetes clusters on AWS and keep them healthy.",
    "Build Terraform modules and GitLab CI pipelines for the product teams.",
    "Our requirements",
    "3+ years of experience with Linux and Kubernetes.",
    "Knowledge of GDPR and data protection rules.",  # a short requirement line stays
    "What we offer",
    "Employment contract (umowa o prace), hybrid work in Krakow.",
])


def test_legal_boilerplate_is_left_out_for_the_ai():
    text = f"{BODY}\n{RODO}\n{EEO}"
    trimmed = trim_jd(text)
    assert "danych osobowych" not in trimmed and "Equal Opportunity" not in trimmed
    assert "Knowledge of GDPR and data protection rules." in trimmed
    assert "umowa o prace" in trimmed
    assert len(trimmed) < len(text) * 0.7


def test_every_kept_line_is_word_for_word_from_the_description():
    text = f"{BODY}<br>{RODO}<br>{BODY}"  # a repeated block is kept once
    trimmed = trim_jd(text)
    assert trimmed.count("Run our Kubernetes clusters") == 1
    assert all(line in text for line in trimmed.splitlines())


def test_a_short_description_is_never_trimmed_to_nothing():
    assert trim_jd(RODO) == RODO
    assert trim_jd(None) == "" and trim_jd("") == ""


def usage(fresh, out, read=0, write=0):
    return SimpleNamespace(input_tokens=fresh, output_tokens=out,
                           cache_read_input_tokens=read, cache_creation_input_tokens=write)


def test_usage_adds_tokens_and_prices_them():
    u = Usage()
    assert u.line() is None
    u.add("claude-haiku-4-5", usage(1_000_000, 0))
    u.add("claude-haiku-4-5", usage(0, 200_000, read=1_000_000))
    # $1 for fresh input, $1 for 200k output, $0.10 for cached input
    assert round(u.cost, 4) == 2.1
    assert u.line() == "AI used: 2 calls, 2,000,000 tokens in, 200,000 out, about $2.1000"


def test_unknown_models_are_counted_but_named_unpriced():
    u = Usage()
    u.add("claude-future-9", usage(10, 5))
    assert u.cost == 0 and u.line().endswith("(no price for claude-future-9)")
    u.add("claude-haiku-4-5", None)  # a fake SDK without usage counts as a call
    assert u.calls == 2


def test_the_ai_reads_the_trimmed_description_but_quotes_check_the_full_one():
    from jobengine.screen.extract import extract

    class Spy:
        def complete_json(self, stage, system, user, **kwargs):
            self.user = user
            return {"work_mode": {"value": "Hybrid", "quote": "hybrid work in Krakow"}}

    spy = Spy()
    ext, dropped = extract(spy, title="SRE", company="Example Hosting", country="Poland",
                           jd=f"{BODY}\n{RODO}")
    assert "danych osobowych" not in spy.user and "Kubernetes clusters" in spy.user
    assert ext.work_mode.value == "Hybrid" and dropped == []


def test_resume_caption_shows_the_ai_cost():
    from jobengine.resume import gate
    from jobengine.resume.builder import caption

    changes = gate.Changes(words=3, bullets=1, skills=["-A"], bullet_notes=[])
    plan = SimpleNamespace(gaps_reported=[], forced_skills=[])
    line = "AI used: 1 call, 6,000 tokens in, 900 out, about $0.0105"
    text = caption(1, {"Company": "C", "Role": "R"}, 90.0, changes, plan, None, usage=line)
    assert line in text.splitlines()
    assert "AI used" not in caption(1, {"Company": "C", "Role": "R"}, 90.0, changes, plan, None)
