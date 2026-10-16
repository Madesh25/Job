"""16 Oct, first real lookups (State Street, Dublin): 1 of 4 found. Snov answered HTTP 422
(11 titles; it takes 10), Prospeo HTTP 400 shown only as "True", the second Prospeo key had
a bad character, and Hunter's 9 other people (bank titles such as "Vice President") filled
no slot. Fixes: Snov at most 10 titles as query parameters, Prospeo's error codes read,
keys cleaned, and a wider search for senior tech people when slots stay open."""

from datetime import date

import httpx
from test_contacts_free_providers import DOMAIN, pdeps, settings

from jobengine import http
from jobengine.contacts.classify import WIDE_NOTE, fill_slots, wide_kind
from jobengine.contacts.models import HIRING, OTHER, PEER, RECRUITER, Candidate
from jobengine.contacts.providers import prospeo, snov
from jobengine.settings import load_settings


def person(title, email, department=None, country="Ireland"):
    return Candidate(name=email.split("@")[0], first_name="X", title=title, email=email,
                     country=country, provider_verified=True, source="Hunter",
                     department=department)


def test_wide_kind_needs_a_tech_word_or_department():
    assert wide_kind("Vice President", "it") == HIRING
    assert wide_kind("Assistant Vice President, Cloud Engineering") == HIRING
    assert wide_kind("Senior Software Engineer") == PEER
    assert wide_kind("Principal Architect") == PEER
    assert wide_kind("Senior Associate", "hr") == RECRUITER
    for title, dept in (("Vice President", "sales"), ("Sales Director", None),
                        ("Managing Director", "management"), ("Data Analyst", None), ("", "it")):
        assert wide_kind(title, dept) == OTHER, title


def test_senior_people_fill_open_slots_after_the_usual_titles():
    people = [person("Vice President", "vp@statestreet.com", "it"),
              person("Senior Software Engineer", "sse@statestreet.com"),
              person("DevOps Engineer", "devops@statestreet.com"),
              person("Sales Director", "sales@statestreet.com")]
    chosen = fill_slots(people, {PEER: 2, HIRING: 1, RECRUITER: 1}, "Ireland",
                        "statestreet.com", set())
    got = [(c.email, kind, note) for c, kind, note in chosen]
    # The DevOps engineer comes first; senior people only fill what is left.
    assert got == [("devops@statestreet.com", PEER, ""),
                   ("sse@statestreet.com", PEER, WIDE_NOTE),
                   ("vp@statestreet.com", HIRING, WIDE_NOTE)]


def test_snov_sends_at_most_ten_positions_as_query_parameters():
    titles = {"peer": [f"Peer {n}" for n in range(6)], "hiring": [f"Lead {n}" for n in range(6)]}
    deps, _ = pdeps(s=settings(SNOV_CLIENT_ID="i", SNOV_CLIENT_SECRET="s"))
    deps = deps.__class__(**{**deps.__dict__, "search_titles": titles})
    params = snov.prospects_params("statestreet.com", snov._positions({PEER: 1, HIRING: 1}, deps))
    assert params[:2] == [("domain", "statestreet.com"), ("page", 1)]
    assert len([p for p in params if p[0] == "positions[]"]) == snov.MAX_POSITIONS


def error_body(status, body):
    def handler(request):
        return httpx.Response(status, json=body)
    http.set_transport(httpx.MockTransport(handler))
    try:
        http.request_json("POST", "https://api.prospeo.io/search-person", json={},
                          s=load_settings("local", {}))
    except http.HttpError as exc:
        return str(exc)
    finally:
        http.set_transport(None)


def test_error_text_names_the_code_not_true():
    assert error_body(400, {"error": True, "error_code": "INVALID_FILTERS",
                            "filter_error": "person_job_title"}).endswith(
        "HTTP 400 INVALID_FILTERS person_job_title")
    assert error_body(422, {"message": "Invalid data", "errors": {"positions": ["max 10"]}}) \
        .endswith('HTTP 422 Invalid data {"positions": ["max 10"]}')


def test_keys_are_cleaned_of_spaces_quotes_and_line_ends():
    s = load_settings("local", {"PROSPEO_API_KEY_2": ' "abc123"\r\n', "HUNTER_API_KEY": "  "})
    assert s.prospeo_api_key_2 == "abc123"
    assert s.hunter_api_key is None


def answers(*replies):
    calls = []

    def request(method, url, **kwargs):
        calls.append((url, kwargs.get("json_body")))
        reply = replies[len(calls) - 1]
        if isinstance(reply, Exception):
            raise reply
        return reply

    return request, calls


def test_prospeo_no_results_as_http_400_is_normal_and_the_wider_search_runs():
    no_results = http.HttpError("POST api.prospeo.io/search-person failed: HTTP 400 NO_RESULTS",
                                400)
    wide = {"results": [{"person": {"person_id": "p-vp", "current_job_title": "Vice President",
                                    "full_name": "Vera Lead"}},
                        {"person": {"person_id": "p-sales",
                                    "current_job_title": "Sales Director"}}]}
    enriched = {"person": {
        "person_id": "p-vp", "full_name": "Vera Lead", "first_name": "Vera",
        "email": {"email": "vera.lead@statestreet.com", "status": "VERIFIED"},
        "location": {"country": "Ireland"}}}
    request, calls = answers(no_results, wide, {"results": []}, enriched)
    deps, _ = pdeps(request=request)
    result = prospeo.search("State Street", "statestreet.com", "Ireland",
                            {PEER: 1, HIRING: 1}, deps)
    assert result.skipped_reason is None
    assert calls[1][1]["filters"]["person_seniority"] == {"include": prospeo.WIDE_SENIORITY}
    assert calls[1][1]["filters"]["person_department"] == {
        "include": ["Engineering & Technical", "Information Technology"]}
    assert calls[2][1]["filters"] == {"company": {"websites": {"include": ["statestreet.com"]}},
                                      "person_location_search": {"include": ["Ireland"]}}
    assert calls[3][1]["data"] == {"person_id": "p-vp"}  # one call, and not the sales one
    assert result.credits_used == 2  # the wider page and one email; NO_RESULTS is free
    (vera,) = result.candidates
    assert (vera.email, vera.department, wide_kind(vera.title, vera.department)) == (
        "vera.lead@statestreet.com", "engineering", HIRING)


def test_prospeo_other_refusals_are_named():
    refused = http.HttpError("POST api.prospeo.io/search-person failed: HTTP 400 "
                             "PLAN_REQUIRED person_job_title", 400)
    request, _ = answers(refused)
    deps, _ = pdeps(request=request)
    result = prospeo.search("V", DOMAIN, "Poland", {PEER: 1}, deps)
    assert result.skipped_reason == ("prospeo: POST api.prospeo.io/search-person failed: "
                                     "HTTP 400 PLAN_REQUIRED person_job_title")


def test_hunter_drops_invalid_addresses_and_keeps_the_department():
    from jobengine.contacts.providers import hunter

    data = {"data": {"emails": [
        {"value": "vp@statestreet.com", "type": "personal", "position": "Vice President",
         "department": "it", "verification": {"status": "valid"}},
        {"value": "gone@statestreet.com", "type": "personal", "position": "Senior Engineer",
         "department": "it", "verification": {"status": "invalid"}}]}}
    request, _ = answers(data)
    deps, _ = pdeps(s=settings(HUNTER_API_KEY="h"), request=request)
    result = hunter.search("State Street", "statestreet.com", "Ireland", {HIRING: 1}, deps)
    assert [(c.email, c.department) for c in result.candidates] == [("vp@statestreet.com", "it")]


# ---------------------------------------------------------------- 16 Oct, second real run


def hunter_page(*people):
    return {"data": {"emails": [
        {"value": email, "type": "personal", "position": title, "department": dept,
         "verification": {"status": "valid"}} for email, title, dept in people]}}


def test_hunter_asks_hr_and_the_next_page_when_slots_stay_open():
    from jobengine.contacts.providers import hunter

    first = hunter_page(*[(f"p{n}@statestreet.com", "Analyst", "finance") for n in range(9)],
                        ("ciso@statestreet.com", "Director of Cybersecurity", "it"))
    hr = hunter_page(("tara@statestreet.com", "Talent Acquisition Partner", "hr"))
    page2 = hunter_page(("sre@statestreet.com", "Site Reliability Engineer", "it"))
    queries = []

    def request(method, url, **kwargs):
        queries.append(kwargs.get("json_body") or kwargs["params"])  # filters in the body
        return (first, hr, page2)[len(queries) - 1]

    deps, _ = pdeps(s=settings(HUNTER_API_KEY="h"), request=request)
    result = hunter.search("State Street", "statestreet.com", "Ireland",
                           {PEER: 1, HIRING: 1, RECRUITER: 1}, deps)
    assert [q.get("department") for q in queries] == [hunter.DEPARTMENTS, "hr",
                                                       hunter.DEPARTMENTS]
    assert queries[2]["offset"] == hunter.LIMIT
    assert result.credits_used == 3
    emails = [c.email for c in result.candidates]
    assert {"tara@statestreet.com", "sre@statestreet.com", "ciso@statestreet.com"} <= set(emails)


def test_hunter_extra_searches_stop_when_credits_run_out():
    from jobengine.contacts.providers import hunter

    request, calls = answers(hunter_page(("a@statestreet.com", "Analyst", "finance")))
    deps, _ = pdeps(s=settings(HUNTER_API_KEY="h"), request=request, credits_left=1)
    result = hunter.search("S", "statestreet.com", "Ireland", {RECRUITER: 1}, deps)
    assert len(calls) == 1 and result.credits_used == 1
    assert "hunter: no credits left for the HR department search" in result.notes


def test_snov_waits_for_its_task_and_says_what_it_saw():
    waits = []
    replies = [{"access_token": "t"}, {"meta": {"task_hash": "h1"}},
               {"status": "in_progress"},
               {"status": "completed", "data": [{"position": "Accountant"}]}]

    def request(method, url, **kwargs):
        return replies.pop(0)

    deps, _ = pdeps(s=settings(SNOV_CLIENT_ID="i", SNOV_CLIENT_SECRET="s"), request=request)
    deps = deps.__class__(**{**deps.__dict__, "sleep": waits.append})
    result = snov.search("S", "statestreet.com", "Ireland", {PEER: 1}, deps)
    assert waits == [snov.POLL_SECONDS]  # it waited once, then the task was ready
    assert result.notes == ["snov: 1 people at statestreet.com, none fit the open slots"]


def test_real_lookups_really_wait():
    import time

    from jobengine.contacts.finder import ContactDeps

    assert ContactDeps.__dataclass_fields__["sleep"].default(1) is None  # tests: no wait
    import inspect

    from jobengine.contacts import finder
    assert "sleep=time.sleep" in inspect.getsource(finder.real_deps)
    assert time.sleep  # the real one is passed to the providers


def test_apollo_free_plan_is_named_as_the_plan():
    from jobengine.contacts.providers import apollo

    exc = http.HttpError("POST api.apollo.io/api/v1/mixed_people/api_search failed: HTTP 403 "
                         "API_INACCESSIBLE", 403)
    assert apollo.refused(exc) == (
        "apollo: your Apollo plan has no API people search (API_INACCESSIBLE); only a paid "
        "plan can search. Remove APOLLO_API_KEY from .env to skip Apollo.")


def test_rate_limit_waits_grow_without_retry_after():
    response = httpx.Response(429)
    assert [http._retry_after(response, n) for n in range(4)] == [2.0, 5.0, 15.0, 15.0]
    assert http._retry_after(httpx.Response(429, headers={"Retry-After": "7"}), 0) == 7.0


def test_a_wider_search_person_is_marked_in_telegram():
    from jobengine.contacts.models import Chosen

    chosen = Chosen(name="Marion", title="Director of Cybersecurity", email="m@s.com",
                    type=HIRING, source="Hunter", country="Ireland", verified=True,
                    notes=WIDE_NOTE)
    assert chosen.label() == ("Hiring: Marion (Director of Cybersecurity) "
                              "[Hunter, verified, wider search, Ireland]")


def test_snov_uses_its_own_links_and_never_calls_an_empty_start():
    link = f"{snov.EMAILS_START}/abc123"
    assert snov.email_start_url({"search_emails_start": link}) == link
    assert snov.email_start_url({"prospect_hash": "h1"}) == f"{snov.EMAILS_START}/h1"
    assert snov.email_start_url({"first_name": "A", "position": "SRE"}) is None
    assert snov.email_start_url({"search_emails_start": "https://evil.example/x"}) is None
    assert snov._result_url({"links": {"result": f"{snov.BASE}/v2/x/result/t9"}},
                            snov.EMAILS_RESULT) == f"{snov.BASE}/v2/x/result/t9"
    assert snov._result_url({"meta": {"task_hash": "t1"}}, snov.EMAILS_RESULT) == (
        f"{snov.EMAILS_RESULT}/t1")


def test_snov_without_an_email_link_names_the_fields_and_spends_nothing():
    calls = []
    replies = [{"access_token": "t"}, {"meta": {"task_hash": "h1"}},
               {"status": "completed", "data": [{"first_name": "Ann", "last_name": "B",
                                                 "position": "SRE"}]}]

    def request(method, url, **kwargs):
        calls.append(url)
        return replies.pop(0)

    deps, _ = pdeps(s=settings(SNOV_CLIENT_ID="i", SNOV_CLIENT_SECRET="s"), request=request)
    result = snov.search("S", "statestreet.com", "Ireland", {PEER: 1}, deps)
    assert len(calls) == 3 and result.credits_used == 0
    assert result.notes == ["snov: no email-search link in Snov's answer "
                            "(fields: first_name, last_name, position)"]


# ---------------------------------------------------------------- 16 Oct: strict location


def test_strict_rule_keeps_only_people_placed_in_the_country():
    people = [person("DevOps Engineer", "in@hcl.com", country="Netherlands"),
              person("DevOps Engineer", "india@hcl.com", country="India"),
              Candidate(name="u", first_name="u", title="Cloud Engineer", email="u@hcl.com",
                        country=None, provider_verified=True, source="Hunter",
                        country_unverified=True)]
    left_out = []
    chosen = fill_slots(people, {PEER: 3}, "Netherlands", "hcl.com", set(), strict=True,
                        left_out=left_out)
    assert [c.email for c, _, _ in chosen] == ["in@hcl.com"]
    assert {c.email for c in left_out} == {"india@hcl.com", "u@hcl.com"}
    # The other rule still fills with them, after the person in the country.
    loose = fill_slots(people, {PEER: 3}, "Netherlands", "hcl.com", set())
    assert [c.email for c, _, _ in loose] == ["in@hcl.com", "u@hcl.com", "india@hcl.com"]


def test_strict_is_the_default_for_our_countries_only():
    from jobengine.contacts.finder import _left_out_note, strict_location

    s = settings()
    assert strict_location(s, "Ireland") and strict_location(s, "Poland")
    assert not strict_location(s, "Other")  # remote jobs: no country to check
    loose = s.model_copy(update={"contacts": {**s.contacts, "location": "country_first"}})
    assert not strict_location(loose, "Ireland")
    note = _left_out_note([person("x", "a@b.com", country="India"),
                           Candidate(name="u", first_name="u", title="", email="u@b.com",
                                     country=None, provider_verified=False, source="Hunter",
                                     country_unverified=True)], "Netherlands")
    assert note == ("location rule strict: left out 1 outside Netherlands and 1 with unknown "
                    "location (Config contacts.location)")


def test_cached_rows_with_unknown_location_are_not_reused_under_strict():
    from jobengine.contacts.cache import CachedRow, in_country

    assert in_country(CachedRow("p1", {"Country": "Ireland", "Notes": ""}), "Ireland")
    assert not in_country(CachedRow("p2", {"Country": "Ireland",
                                           "Notes": "country unverified"}), "Ireland")
    assert not in_country(CachedRow("p3", {"Country": "India"}), "Ireland")


def test_hunter_country_filter_counts_only_when_hunter_confirms_it():
    from jobengine.contacts.providers import hunter

    page = hunter_page(("nl@hcl.com", "DevOps Engineer", "it"))
    confirmed = {**page, "meta": {"params": {"location": {"include": [{"country": "NL"}]}}}}
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, kwargs.get("json_body")))
        return confirmed

    deps, _ = pdeps(s=settings(HUNTER_API_KEY="h"), request=request)
    result = hunter.search("HCL", "hcl.com", "Netherlands", {PEER: 1}, deps)
    assert calls[0] == ("POST", {"domain": "hcl.com", "type": "personal",
                                 "department": hunter.DEPARTMENTS, "limit": hunter.LIMIT,
                                 "location": {"include": [{"country": "NL"}]}})
    (nl,) = result.candidates
    assert (nl.country, nl.country_unverified) == ("Netherlands", False)

    request2, _ = answers(page)  # the filter is not echoed back
    deps, _ = pdeps(s=settings(HUNTER_API_KEY="h"), request=request2)
    result = hunter.search("HCL", "hcl.com", "Netherlands", {PEER: 1}, deps)
    assert result.candidates[0].country_unverified
    assert result.notes[0].startswith("hunter: its answer did not confirm the Netherlands")


def test_prospeo_searches_the_country_and_retries_without_a_refused_filter():
    assert prospeo.search_body("hcl.com", [], "Netherlands")["filters"][
        "person_location_search"] == {"include": ["Netherlands"]}
    assert "person_location_search" not in prospeo.search_body("hcl.com", [], "Other")["filters"]
    refused = http.HttpError("POST api.prospeo.io/search-person failed: HTTP 400 "
                             "INVALID_FILTERS person_location_search", 400)
    request, calls = answers(refused, *[{"results": []}] * 6)
    deps, _ = pdeps(request=request)
    result = prospeo.search("HCL", "hcl.com", "Netherlands", {PEER: 1}, deps)
    assert "person_location_search" in calls[0][1]["filters"]
    assert "person_location_search" not in calls[1][1]["filters"]
    assert "prospeo: its country filter was refused, searched without it" in result.notes


# ---------------------------------------------------------------- 16 Oct, fifth real run


def test_hunter_free_plan_page_limit_is_a_short_note():
    from jobengine.contacts.providers import hunter

    first = hunter_page(*[(f"p{n}@s.com", "Analyst", "finance") for n in range(10)])
    limited = http.HttpError('POST api.hunter.io/v2/domain-search failed: HTTP 400 [{"id": '
                             '"pagination_error", "code": 400}]', 400)
    request, _ = answers(first, hunter_page(), limited)
    deps, _ = pdeps(s=settings(HUNTER_API_KEY="h"), request=request)
    result = hunter.search("S", "s.com", "Ireland", {PEER: 1, RECRUITER: 1}, deps)
    assert "hunter: your plan shows only 10 people per search, no next page" in result.notes
    assert result.notes[0].startswith("hunter: its answer did not confirm the Ireland filter "
                                      "(it answered with: nothing)")


def test_snov_reads_a_place_field_and_names_its_fields_when_none():
    assert snov.prospect_country({"country": "Ireland"}) == "Ireland"
    assert snov.prospect_country({"locality": "Dublin, Ireland"}) == "Ireland"
    assert snov.prospect_country({"location": "Amsterdam, North Holland, Netherlands"}) == \
        "Netherlands"
    assert snov.prospect_country({"first_name": "A"}) is None


def test_prospeo_no_match_is_quiet_and_costs_nothing():
    rows = {"results": [{"person": {"person_id": "p1", "current_job_title": "DevOps Engineer"}}]}
    no_match = http.HttpError("POST api.prospeo.io/enrich-person failed: HTTP 400 NO_MATCH", 400)
    request, calls = answers(rows, no_match)
    deps, _ = pdeps(request=request)
    result = prospeo.search("S", "s.com", "Ireland", {PEER: 1}, deps)
    assert [url for url, _ in calls] == [prospeo.SEARCH, prospeo.ENRICH]
    assert (result.credits_used, result.candidates, result.notes) == (1, [], [])


# ---------------------------------------------------------------- 16 Oct, sixth real run


def test_prospeo_asks_hr_then_the_whole_company_while_slots_stay_open():
    empty = {"results": []}
    hr = {"results": [{"person": {"person_id": "p-hr", "current_job_title": "HR Business "
                                  "Partner"}}]}
    anyone = {"results": [{"person": {"person_id": "p-sre",
                                      "current_job_title": "Site Reliability Engineer"}},
                          {"person": {"person_id": "p-sales",
                                      "current_job_title": "Account Executive"}}]}
    mail = {"person": {"email": {"email": "x@hcl.com", "status": "VERIFIED"},
                       "location": {"country": "Netherlands"}}}
    request, calls = answers(empty, empty, hr, anyone, mail, mail)
    deps, _ = pdeps(request=request)
    prospeo.search("HCL", "hcl.com", "Netherlands", {PEER: 1, RECRUITER: 1}, deps)
    filters = [c[1]["filters"] for c in calls[:4]]
    assert filters[1]["person_seniority"] == {"include": prospeo.WIDE_SENIORITY}
    assert filters[2]["person_department"] == {"include": ["Human Resources"]}
    assert set(filters[3]) == {"company", "person_location_search"}  # anyone, in the country
    assert [c[1]["data"]["person_id"] for c in calls[4:]] == ["p-hr", "p-sre"]  # no sales


def test_prospeo_locate_by_email():
    mail = {"person": {"location": {"country": "Netherlands"}}}
    request, calls = answers(mail)
    deps, _ = pdeps(request=request)
    assert prospeo.locate("a@hcl.com", deps) == ("Netherlands", 1)
    assert calls[0][1] == {"data": {"email": "a@hcl.com"}, "only_verified_email": False,
                           "enrich_mobile": False}
    no_match = http.HttpError("POST api.prospeo.io/enrich-person failed: HTTP 400 NO_MATCH", 400)
    request, _ = answers(no_match)
    deps, _ = pdeps(request=request)
    assert prospeo.locate("b@hcl.com", deps) == (None, 0)


def test_snov_searches_the_whole_domain_when_the_titles_find_nobody():
    replies = [{"access_token": "t"}, {"meta": {"task_hash": "h1"}},
               {"status": "completed", "data": []},
               {"meta": {"task_hash": "h2"}},
               {"status": "completed", "data": [{"position": "Accountant"}]}]
    params = []

    def request(method, url, **kwargs):
        params.append(kwargs.get("params"))
        return replies.pop(0)

    deps, _ = pdeps(s=settings(SNOV_CLIENT_ID="i", SNOV_CLIENT_SECRET="s"), request=request)
    result = snov.search("HCL", "hcltech.com", "Netherlands", {PEER: 1}, deps)
    assert any(p[0] == "positions[]" for p in params[1])
    assert not any(p[0] == "positions[]" for p in params[3])  # the whole domain
    assert result.notes == ["snov: 1 people at hcltech.com, none fit the open slots"]


def test_hunter_people_are_placed_through_prospeo_under_strict():
    from test_contacts_free_providers import config_with

    from jobengine.contacts import finder
    from jobengine.contacts.providers.base import FixtureHttp

    d = finder.fake_deps(settings(HUNTER_API_KEY="h", PROSPEO_API_KEY="p-key"))
    d.today = lambda: date(2026, 10, 1)
    config = config_with(apollo="75 / 75 per month", snov="50 / 50 per month",
                         prospeo="0 / 75 per month")
    d.config = lambda: config
    fixture = FixtureHttp(d.s)
    located = []

    def prospeo_request(method, url, **kwargs):
        email = ((kwargs.get("json_body") or {}).get("data") or {}).get("email")
        if email:  # the location check
            located.append(email)
            return {"person": {"location": {"country": "Poland"}}}
        return {"results": []}  # Prospeo's own searches find nobody here

    d.request = lambda provider: prospeo_request if provider == "prospeo" else fixture
    result = finder.find_contacts(d, "fixture-clean-pl")
    assert located and len(located) <= finder.MAX_CONFIRM
    assert any("location from Prospeo" in (c.notes or "") for c in result.contacts)
    assert f"prospeo: checked where {len(located)} people without a location are: " \
           f"{len(located)} in Poland" in result.message
