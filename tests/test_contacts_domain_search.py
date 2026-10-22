"""22 Oct (Phase 11 O-03): the bot finds a company's email domain itself."""

from datetime import date

from jobengine.contacts import domain_search as ds
from jobengine.contacts.finder import (
    _not_company,
    base_domain,
    fake_deps,
    find_contacts,
    state_key,
    valid_domain,
)
from jobengine.llm import LLMError
from jobengine.settings import load_settings

NO_DOMAIN = "fixture-no-domain"


class DomainLLM:
    def __init__(self, domain, urls, titles=None, fail=False):
        self.domain, self.urls, self.titles, self.fail = domain, urls, titles or {}, fail
        self.calls = []

    def complete_json_with_search(self, stage, system, user, **kwargs):
        self.calls.append((stage, user, kwargs.get("key")))
        if self.fail:
            raise LLMError("call failed: HTTP 503")
        if kwargs.get("titles") is not None:
            kwargs["titles"].update(self.titles)
        return {"domain": self.domain, "sources": self.urls}, self.urls


def ai(llm, company="State Street (IE)"):
    return ds.from_ai(llm, company, "Ireland", valid_domain, base_domain, _not_company)


def test_job_pages_give_the_one_named_domain():
    assert ds.from_job_pages("Acme Cloud", ["acme.com", "kubernetes.io"]) == "acme.com"
    assert ds.from_job_pages("Acme Cloud", ["acme.com", "acme-cloud.pl"]) is None  # unsure
    assert ds.from_job_pages("Acme Solutions", ["solutions.io"]) is None  # common word only


def test_ai_domain_needs_a_search_result_on_it():
    found, note = ai(DomainLLM("statestreet.com", ["https://www.statestreet.com/us/en"]))
    assert found == "statestreet.com"
    assert note == "domain statestreet.com found by AI web search (www.statestreet.com)"
    found, note = ai(DomainLLM("statestreet.com", ["https://en.wikipedia.org/wiki/State"]))
    assert found is None and "not confirmed" in note


def test_ai_domain_never_a_job_board_or_personal_domain():
    for bad in ("statestreet.wd1.myworkdayjobs.com", "gmail.com", "linkedin.com", ""):
        found, _ = ai(DomainLLM(bad, [f"https://{bad or 'x.example.com'}/"]))
        assert found is None


def test_title_can_confirm_a_domain_without_the_name():
    llm = DomainLLM("ssga.com", ["https://www.ssga.com/"],
                    titles={"https://www.ssga.com/": "State Street Global Advisors"})
    assert ai(llm)[0] == "ssga.com"


def test_ai_failure_is_a_note():
    assert ai(DomainLLM("", [], fail=True)) == (None, "AI domain search: call failed: HTTP 503")


def test_finder_uses_a_found_domain_and_saves_it():
    d = fake_deps(load_settings("local", {}))
    d.today = lambda: date(2026, 10, 22)
    saved = []
    d.save_domain = lambda company, domain: saved.append((company, domain))
    d.llm = lambda config: DomainLLM("mystery.example.com", ["https://mystery.example.com/"],
                                     titles={"https://mystery.example.com/":
                                             "Mystery Example Labs - home"})
    result = find_contacts(d, NO_DOMAIN)
    assert result.status == "done"  # no domain question
    assert d.calls == ["apollo", "hunter", "snov"]
    assert d.state.get(state_key("Mystery Example Labs"))["domain"] == "mystery.example.com"
    assert saved == [("Mystery Example Labs", "mystery.example.com")]
    assert "found by AI web search" in result.message and "saved for next time" in result.message


def test_finder_still_asks_when_nothing_is_sure():
    d = fake_deps(load_settings("local", {}))
    d.llm = lambda config: DomainLLM("", [])
    result = find_contacts(d, NO_DOMAIN)
    assert result.status == "waiting_domain"
    assert "What is the email domain for Mystery Example Labs?" in result.message
