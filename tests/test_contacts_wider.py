"""16 Oct, first real lookups (State Street, Dublin): 1 of 4 found. Snov answered HTTP 422
(11 titles; it takes 10), Prospeo HTTP 400 shown only as "True", the second Prospeo key had
a bad character, and Hunter's 9 other people (bank titles such as "Vice President") filled
no slot. Fixes: Snov at most 10 titles as query parameters, Prospeo's error codes read,
keys cleaned, and a wider search for senior tech people when slots stay open."""

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
    enriched = {"matched": [{"identifier": "p-vp", "person": {
        "person_id": "p-vp", "full_name": "Vera Lead", "first_name": "Vera",
        "email": {"email": "vera.lead@statestreet.com", "status": "VERIFIED"},
        "location": {"country": "Ireland"}}}], "total_cost": 1}
    request, calls = answers(no_results, wide, enriched)
    deps, _ = pdeps(request=request)
    result = prospeo.search("State Street", "statestreet.com", "Ireland",
                            {PEER: 1, HIRING: 1}, deps)
    assert result.skipped_reason is None
    assert calls[1][1]["filters"]["person_seniority"] == {"include": prospeo.WIDE_SENIORITY}
    assert calls[1][1]["filters"]["person_department"] == {
        "include": ["Engineering & Technical", "Information Technology"]}
    assert calls[2][1]["data"] == [{"identifier": "p-vp", "person_id": "p-vp"}]  # not sales
    assert result.credits_used == 2  # the wider page and one email; NO_RESULTS is free
    (vera,) = result.candidates
    assert (vera.email, vera.department, wide_kind(vera.title, vera.department)) == (
        "vera.lead@statestreet.com", "engineering", HIRING)


def test_prospeo_other_refusals_are_named():
    refused = http.HttpError("POST api.prospeo.io/search-person failed: HTTP 400 "
                             "INVALID_FILTERS person_job_title", 400)
    request, _ = answers(refused)
    deps, _ = pdeps(request=request)
    result = prospeo.search("V", DOMAIN, "Poland", {PEER: 1}, deps)
    assert result.skipped_reason == ("prospeo: POST api.prospeo.io/search-person failed: "
                                     "HTTP 400 INVALID_FILTERS person_job_title")


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
        queries.append(kwargs["params"])
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
    assert result.notes == ["hunter: no credits left for the HR department search"]


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
                              "[Hunter, verified, wider search]")
