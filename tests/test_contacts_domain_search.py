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


# ---------------------------------------------------------------- 7 Nov: the website by its name


def site(pages):
    def read(url):
        if url not in pages:
            raise OSError("no answer")
        return pages[url]
    return read


def guess(company, country, pages):
    return ds.from_site_guess(company, country, site(pages), valid_domain, _not_company)


def test_likely_addresses_country_first_then_com():
    assert ds.guesses("BNP Paribas Bank Polska", "Poland") == [
        "bnpparibas.pl", "bnp.pl", "bnpparibas.com", "bnp.com"]
    assert ds.guesses("ING", "Netherlands") == ["ing.nl", "ing.com"]
    assert ds.guesses("Polska", "Poland") == []  # nothing but a country word


def test_the_website_counts_only_when_it_names_the_company():
    pages = {"https://bnpparibas.pl/": "BNP Paribas Bank Polska S.A. | Bank dla Ciebie"}
    assert guess("BNP Paribas Bank Polska", "Poland", pages) == (
        "bnpparibas.pl", "domain bnpparibas.pl found on the company's website")
    other = {"https://bnpparibas.pl/": "Parked domain for sale"}
    found, note = guess("BNP Paribas Bank Polska", "Poland", other)
    assert found is None
    assert note == ("website check: bnpparibas.pl did not name the company; bnp.pl, "
                    "bnpparibas.com, bnp.com did not answer")


def test_a_short_name_must_be_a_whole_word():
    pages = {"https://ing.nl/": "Online banking and saving for everyone"}  # "ing" inside words
    assert guess("ING", "Netherlands", pages)[0] is None
    pages = {"https://ing.nl/": "ING Bank N.V. Particulier"}
    assert guess("ING", "Netherlands", pages)[0] == "ing.nl"


def test_finder_finds_the_domain_on_the_website_when_the_ai_is_not_sure():
    d = fake_deps(load_settings("local", {}))
    d.today = lambda: date(2026, 11, 7)
    saved = []
    d.save_domain = lambda company, domain: saved.append((company, domain))
    d.llm = lambda config: DomainLLM("", [])  # the AI is not sure
    d.read_page = site({"https://mystery.com/": "Mystery Example Labs: platform engineering"})
    result = find_contacts(d, NO_DOMAIN)
    assert result.status == "done"  # no domain question
    assert d.state.get(state_key("Mystery Example Labs"))["domain"] == "mystery.com"
    assert saved == [("Mystery Example Labs", "mystery.com")]
    assert "domain mystery.com found on the company's website, saved for next time" in (
        result.message)
