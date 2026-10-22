"""20 Oct: AI web search for contacts (contacts/web_search.py and finder._ai_step)."""

from datetime import date

import pytest

from jobengine.config_store import ConfigStore
from jobengine.contacts import finder
from jobengine.contacts import web_search as ai
from jobengine.contacts.finder import fake_deps, find_contacts
from jobengine.contacts.providers.base import ProviderDeps
from jobengine.llm import LLMError, attach_sources
from jobengine.settings import load_settings

TODAY = date(2026, 10, 20)
PL = "fixture-clean-pl"
DOMAIN = "vistula.example.com"
TEAM = "https://vistula.example.com/team"
TALK = "https://events.example.org/devopsdays-krakow-2026"
LINKEDIN = "https://www.linkedin.com/in/someone"


def person(name, title, sources):
    return {"name": name, "title": title, "country": "Poland", "sources": sources,
            "quote": "x"}


ANSWER = {"people": [
    person("Łukasz Wójcik", "Platform Engineering Lead", [TEAM]),
    person("Marta Nowak", "Head of Infrastructure", [TEAM]),
    person("Zofia Kowal", "Talent Acquisition Partner", [TEAM]),
    person("Jan Zielinski", "Site Reliability Engineer", [TALK]),
    person("Ghost Person", "DevOps Engineer", [TEAM]),  # not on the page: made up
    person("Linked Only", "DevOps Engineer", [LINKEDIN]),  # LinkedIn is never used
    person("Madonna", "Recruiter", [TEAM]),  # one name cannot be looked up
    person("Elsewhere Name", "SRE", ["https://blog.example.net/post"]),  # 7th: over the cap
]}
EMAILS = {"Łukasz": ("lukasz.wojcik@vistula.example.com", "valid"),
          "Marta": ("marta.nowak@vistula.example.com", "accept_all"),
          "Zofia": ("zofia.kowal@vistula.example.com", "invalid"),
          "Jan": ("", None)}


class SearchLLM:
    def __init__(self, answer=ANSWER, urls=(TEAM, TALK, LINKEDIN), fail=False, titles=None):
        self.answer, self.urls, self.fail, self.calls = answer, list(urls), fail, []
        self.titles = titles or {}

    def complete_json_with_search(self, stage, system, user, **kwargs):
        self.calls.append((stage, user, kwargs.get("max_uses"), system))
        if self.fail:
            raise LLMError("call failed: HTTP 503")
        if kwargs.get("titles") is not None:
            kwargs["titles"].update(self.titles)
        return self.answer, self.urls


class Providers:
    """Hunter's email finder and Prospeo's enrich by name, recorded."""

    def __init__(self, prospeo=None, locations=None):
        self.calls, self.prospeo, self.locations = [], prospeo or {}, locations or {}

    def __call__(self, method, url, *, params=None, headers=None, json_body=None):
        if "email-finder" in url:
            self.calls.append(("hunter", params["first_name"], params["last_name"]))
            email, status = EMAILS.get(params["first_name"], ("", None))
            return {"data": {"email": email or None,
                             "verification": {"status": status} if status else None}}
        if "email" in json_body["data"]:  # Prospeo locate: where is this person?
            email = json_body["data"]["email"]
            self.calls.append(("locate", email))
            if email in self.locations:
                return {"person": {"location": {"country": self.locations[email]}}}
            return {"error": True, "error_code": "NO_MATCH"}
        first = json_body["data"]["first_name"]
        self.calls.append(("prospeo", first, json_body["data"]["company_website"]))
        if first in self.prospeo:
            return {"person": {"email": {"email": self.prospeo[first], "status": "VERIFIED"}}}
        return {"error": True, "error_code": "NO_MATCH"}


def deps(live=True, switch="on", llm=None, providers=None):
    environ = {"HUNTER_API_KEY": "hk", "PROSPEO_API_KEY": "pk"}
    if live:
        environ["CONTACTS_LIVE"] = "true"
    d = fake_deps(load_settings("local", environ))
    d.today = lambda: TODAY
    rows = {k: r.value for k, r in ConfigStore.fake()._rows.items()}
    rows.update({"contacts.ai_search": switch, "credits.prospeo": "0 / 75 per month"})
    config = ConfigStore.from_values(rows)
    d.config = lambda: config
    d.contacts.rows.clear()
    d.jobs.rows[PL]["body"] = ["Description source: ats", "We run EKS in Krakow."]
    d.llm = lambda config: llm
    d.request = lambda provider: providers
    return d


@pytest.fixture
def no_waterfall(monkeypatch):
    monkeypatch.setattr(finder, "WATERFALL", ())


def test_ai_search_fills_the_open_slots(no_waterfall):
    llm, providers = SearchLLM(), Providers(prospeo={"Jan": "jan.zielinski@vistula.example.com"})
    d = deps(llm=llm, providers=providers)
    result = find_contacts(d, PL)
    assert llm.calls[0][0] == "people" and "COUNTRY: Poland" in llm.calls[0][1]
    got = {c.name: c for c in result.contacts}
    assert set(got) == {"Łukasz Wójcik", "Marta Nowak", "Jan Zielinski"}
    lukasz = got["Łukasz Wójcik"]
    assert (lukasz.source, lukasz.verified, lukasz.country) == ("Hunter", True, "Poland")
    assert "found by AI web search on vistula.example.com" in lukasz.notes
    assert got["Jan Zielinski"].source == "Prospeo"  # Hunter knew no email for him
    # The invalid address is never used; the made-up, LinkedIn and unsearched people and
    # the single name never reach an email look-up.
    asked = {c[1] for c in providers.calls}
    assert asked == {"Łukasz", "Marta", "Zofia", "Jan"}
    assert ("AI search: 6 names on public pages and LinkedIn results, 4 checked, 3 with an "
            "email from Hunter or Prospeo (dropped: 1 without a page from the search; 1 not "
            "named with the company on their page)") in result.message
    assert "Zofia" not in result.message
    assert d.contacts.rows  # written to Contacts like any other contact
    assert any(row["Source"] == "Hunter" and "AI web search" in row["Notes"]
               for row in d.contacts.rows.values())


def test_test_data_never_meets_real_names(no_waterfall):
    llm = SearchLLM()
    result = find_contacts(deps(live=False, llm=llm, providers=Providers()), PL)
    assert llm.calls == []
    assert "AI search: off while contact look-ups use test data" in result.message


def test_switch_off_means_no_ai_search(no_waterfall):
    llm = SearchLLM()
    d = deps(switch="", llm=llm, providers=Providers())
    find_contacts(d, PL)
    assert llm.calls == [] and "ai search" not in d.calls


def test_a_failed_ai_search_is_a_note(no_waterfall):
    result = find_contacts(deps(llm=SearchLLM(fail=True), providers=Providers()), PL)
    assert "AI search: call failed: HTTP 503" in result.message


def test_a_page_without_the_country_is_location_unknown():
    people = ai.parse_people({"people": [person("Anna Lis", "SRE", [TEAM])]})
    kept, _ = ai.check(people, [TEAM], "Vistula Cloud", DOMAIN, "Ireland",
                       lambda url: "Anna Lis, SRE at Vistula Cloud in Krakow")
    assert kept[0].page == TEAM and not kept[0].placed
    kept, _ = ai.check(people, [TEAM], "Vistula Cloud", DOMAIN, "Poland",
                       lambda url: "Anna Lis, SRE at Vistula Cloud in Krakow")
    assert kept[0].placed


def test_blocked_sites_are_not_read():
    people = ai.parse_people({"people": [person("Anna Lis", "SRE", ["https://pracuj.pl/x"])]})
    read = []
    kept, notes = ai.check(people, ["https://pracuj.pl/x"], "Vistula Cloud", DOMAIN, "Poland",
                           lambda url: read.append(url) or "", blocked=("pracuj.pl",))
    assert kept == [] and read == [] and notes == ["1 without a page from the search"]


def test_hunter_invalid_never_falls_through_to_another_address():
    hunter = ProviderDeps(s=load_settings("local", {"HUNTER_API_KEY": "k"}),
                          request=Providers(), search_titles={})
    found = ai.find_email(ai.Person("Zofia Kowal", "TA", "Poland"), DOMAIN, hunter, None)
    assert found == ("", False, "", 1, 0)


def test_polish_letters_match_plain_text():
    assert ai._words_in(ai._plain("Łukasz Wójcik").split(), ai._plain("by Lukasz Wojcik"))


def test_gemini_sources_are_attached_to_people():
    raw = {"people": [{"name": "Marta Nowak", "title": "Head of Infrastructure"}]}
    attach_sources(raw, [{"segment": {"text": '"name": "Marta Nowak", "title": "Head'},
                          "groundingChunkIndices": [0]}], [TEAM])
    assert raw["people"][0]["sources"] == [TEAM]


# ---------------------------------------------------------------- 22 Oct: LinkedIn titles

EN = chr(0x2013)  # titles use an en dash; the repo itself never contains one
NL_PROFILE = "https://nl.linkedin.com/in/anna-lis-123"
WWW_PROFILE = "https://www.linkedin.com/in/piotr-nowy-9"


def test_linkedin_result_is_checked_by_its_title_only(no_waterfall):
    answer = {"people": [person("Anna Lis", "Platform Engineer", [NL_PROFILE]),
                         person("Piotr Nowy", "Engineering Manager", [WWW_PROFILE]),
                         person("Zed Fake", "SRE", [NL_PROFILE])]}
    titles = {NL_PROFILE: "Anna Lis - Platform Engineer - Vistula Cloud | LinkedIn",
              WWW_PROFILE: f"Piotr Nowy {EN} Engineering Manager {EN} Vistula Cloud | LinkedIn"}
    read = []
    llm = SearchLLM(answer=answer, urls=(NL_PROFILE, WWW_PROFILE), titles=titles)
    providers = Providers(locations={"piotr.nowy@vistula.example.com": "Netherlands"})
    d = deps(llm=llm, providers=providers)
    d.read_page = lambda url: read.append(url) or ""
    d.jobs.rows[PL]["Country"] = "Netherlands"
    EMAILS["Anna"] = ("anna.lis@vistula.example.com", "valid")
    EMAILS["Piotr"] = ("piotr.nowy@vistula.example.com", "valid")
    try:
        result = find_contacts(d, PL)
    finally:
        del EMAILS["Anna"], EMAILS["Piotr"]
    assert read == []  # LinkedIn is never opened
    assert "site:linkedin.com/in" in llm.calls[0][3]
    got = {c.name: c for c in result.contacts}
    # nl.linkedin.com places Anna in the Netherlands; Piotr's www. address says nothing, so
    # Prospeo is asked where he is (Netherlands); Zed is not in any title.
    assert set(got) == {"Anna Lis", "Piotr Nowy"}
    assert "found by AI web search on a LinkedIn search result (title only)" in got[
        "Anna Lis"].notes
    assert "location from Prospeo" in got["Piotr Nowy"].notes
    assert ("locate", "piotr.nowy@vistula.example.com") in providers.calls
    assert "1 not named with the company on their page" in result.message


def test_linkedin_titles_can_be_switched_off(no_waterfall):
    answer = {"people": [person("Anna Lis", "Platform Engineer", [NL_PROFILE])]}
    llm = SearchLLM(answer=answer, urls=(NL_PROFILE,),
                    titles={NL_PROFILE: "Anna Lis - Vistula Cloud | LinkedIn"})
    d = deps(llm=llm, providers=Providers())
    d.s = d.s.model_copy(update={"contacts": {**d.s.contacts, "ai_linkedin_titles": False}})
    result = find_contacts(d, PL)
    assert "Never use LinkedIn" in llm.calls[0][3]
    assert result.contacts == [] or all(c.name != "Anna Lis" for c in result.contacts)


def test_junior_names_are_dropped_before_any_lookup():
    people = ai.parse_people({"people": [person("Anna Lis", "Junior DevOps Engineer", [TEAM])]})
    kept, notes = ai.check(people, [TEAM], "Vistula Cloud", DOMAIN, "Poland",
                           lambda url: "Anna Lis at Vistula Cloud, Krakow")
    assert kept == [] and notes == ["1 junior"]


def test_linkedin_country_from_the_address():
    assert ai.linkedin_country("nl.linkedin.com") == "Netherlands"
    assert ai.linkedin_country("pl.linkedin.com") == "Poland"
    assert ai.linkedin_country("www.linkedin.com") is None
    assert ai.linkedin_country("nl.example.com") is None
