"""PR 16: engineers from the company's public GitHub organisation (free)."""

from datetime import date

from jobengine import http
from jobengine.config_store import ConfigRow, ConfigStore
from jobengine.contacts import finder
from jobengine.contacts.finder import fake_deps, find_contacts
from jobengine.contacts.providers import github
from jobengine.contacts.providers.base import FixtureHttp, ProviderDeps
from jobengine.settings import load_settings

TODAY = date(2026, 10, 1)
PL = "fixture-clean-pl"
DOMAIN = "vistula.example.com"


def settings(**environ):
    return load_settings("local", environ)


def pdeps(s=None, request=None):
    s = s or settings()
    fixture = request or FixtureHttp(s)
    return ProviderDeps(s=s, request=fixture, search_titles={}), fixture


def test_org_is_accepted_only_on_the_company_domain():
    deps, fixture = pdeps()
    org, calls = github.find_org("Vistula Cloud", DOMAIN, deps, lambda name: None)
    assert org == "vistula-cloud" and calls == 3  # search, then two organisations
    # Vistula Games' website is another domain: never taken.
    assert github.find_org("Vistula", "vistulagames.example.net", deps,
                           lambda name: None)[0] is None
    assert github.find_org("X", DOMAIN, deps, lambda name: "configured-org") == (
        "configured-org", 0)
    assert github.find_org("X", DOMAIN, deps, lambda name: "") == (None, 0)  # known: none


def test_only_public_emails_on_the_company_domain():
    deps, fixture = pdeps(settings(GITHUB_TOKEN="ghp-test"))
    seen = {}

    def request(method, url, *, params=None, headers=None, json_body=None):
        seen.setdefault("headers", headers)
        return fixture(method, url, params=params, headers=headers, json_body=json_body)

    deps.request = request
    result = github.search("Vistula Cloud", DOMAIN, "vistula-cloud", deps)
    assert seen["headers"]["Authorization"] == "Bearer ghp-test"
    assert result.credits_used == 6  # members + 5 profiles
    assert [(c.name, c.email, c.title, c.country, c.source) for c in result.candidates] == [
        ("Ewa Sample", "ewa.sample@vistula.example.com",
         "Site Reliability Engineer, Kubernetes and Terraform", "Poland", "GitHub"),
        ("Tomek Example", "tomek@vistula.example.com", "", None, "GitHub"),
        ("Kasia Sample", "kasia.sample@vistula.example.com", "HR Business Partner", "Poland",
         "GitHub"),
    ]  # the gmail.com address and the member without a public email are left out
    assert github.search("V", DOMAIN, "vistula-cloud", deps, max_profiles=1).credits_used == 2


def test_rate_limit_is_a_skip_reason():
    def limited(*args, **kwargs):
        raise http.HttpError("GET https://api.github.com failed: 403", 403)

    deps, _ = pdeps(request=limited)
    assert github.search("V", DOMAIN, "vistula-cloud", deps).skipped_reason.startswith(
        "github: rate limit reached")


def test_country_from_location():
    assert github.country_of("Kraków, Poland") == "Poland"
    assert github.country_of("Amsterdam") == "Netherlands"
    assert github.country_of("Dublin, IE") == "Ireland"
    assert github.country_of("Remote") is None
    assert github.country_of("Corkscrew Street, London") is None


def make(**config):
    d = fake_deps(settings())
    d.today = lambda: TODAY
    rows = dict(ConfigStore.fake()._rows)
    for key, value in config.items():
        rows[key] = ConfigRow(key=key, value=value, updated=TODAY)
    store = ConfigStore(rows)
    d.config = lambda: store
    return d


def test_off_unless_config_says_on():
    d = make()
    find_contacts(d, PL)
    assert "github" not in d.calls


def test_github_fills_the_peer_slots_before_paid_lookups():
    d = make(**{finder.GITHUB_SWITCH: "on"})
    result = find_contacts(d, PL)
    assert d.calls == ["github", "apollo"]  # the open peer slot went to GitHub first
    assert [(c.name, c.type, c.source, c.notes) for c in result.contacts
            if c.source == "GitHub"] == [
        ("Ewa Sample", "Peer engineer", "GitHub", "public GitHub profile, org vistula-cloud")]
    assert "Peer engineer: Ewa Sample (Site Reliability Engineer, Kubernetes and Terraform) " \
        "[GitHub]" in result.message
    assert d.state.get("github_org:vistula cloud") == {"org": "vistula-cloud"}
    written = {v.get("Email"): v for _, v in d.contacts.all_rows()}
    assert written["ewa.sample@vistula.example.com"]["Source"] == "GitHub"
    assert written["ewa.sample@vistula.example.com"]["Status"] == "Unverified"


def test_more_slots_country_unverified_and_hr_by_bio():
    d = make(**{finder.GITHUB_SWITCH: "on", "contacts.mix": "peer=3, hiring=1, recruiter=2"})
    result = find_contacts(d, PL)
    people = {c.name: (c.type, c.notes) for c in result.contacts if c.source == "GitHub"}
    assert people["Tomek Example"] == (
        "Peer engineer", "country unverified; public GitHub profile, org vistula-cloud")
    # HR by bio is a Recruiter/TA contact (cold mail), never a referral ask.
    assert people["Kasia Sample"][0] == "Recruiter/TA"
    assert "Lena Example" not in people  # a gmail.com address is never used


def test_configured_org_and_remembered_none():
    d = make(**{finder.GITHUB_SWITCH: "on",
                finder.GITHUB_ORGS: "Vistula Cloud = vistula-cloud"})
    assert finder.github_org_known(d, d.config(), "Vistula Cloud Sp. z o.o.") == "vistula-cloud"
    d = make(**{finder.GITHUB_SWITCH: "on"})
    d.state.set("github_org:vistula cloud", {"org": ""})
    result = find_contacts(d, PL)
    assert not any(c.source == "GitHub" for c in result.contacts)
    assert "github: no public organisation on vistula.example.com for Vistula Cloud" in \
        result.notes
