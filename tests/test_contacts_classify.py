import pytest

from jobengine.contacts.classify import (
    classify_title,
    fill_slots,
    keep,
    parse_mix,
)
from jobengine.contacts.jd_emails import extract
from jobengine.contacts.models import HIRING, OTHER, PEER, RECRUITER, Candidate


@pytest.mark.parametrize("title, kind", [
    ("Senior DevOps Engineer", PEER),
    ("Site Reliability Engineer II", PEER),
    ("Kubernetes Platform Engineer", PEER),
    ("Engineering Manager, Infrastructure", HIRING),
    ("Head of Platform", HIRING),
    ("DevOps Team Lead", HIRING),
    ("Technical Recruiter", RECRUITER),
    ("Talent Acquisition Partner", RECRUITER),
    ("Specjalista ds. rekrutacji", RECRUITER),
    ("Recruitment Lead, Engineering", RECRUITER),  # recruiter is checked before hiring
    ("Office Manager", OTHER),
    ("", OTHER),
])
def test_classify_title(title, kind):
    assert classify_title(title) == kind


def cand(name, title, email, country="Poland", source="Apollo", verified=True, **kw):
    return Candidate(name=name, first_name=name.split()[0], title=title, email=email,
                     country=country, provider_verified=verified, source=source, **kw)


def test_keep_filters_personal_and_off_domain():
    assert keep(cand("A B", "SRE", "a.b@vistula.example.com"), "vistula.example.com")
    assert keep(cand("A B", "SRE", "a@eu.vistula.example.com"), "vistula.example.com")
    assert not keep(cand("A B", "SRE", "a.b@gmail.com"), "vistula.example.com")
    assert not keep(cand("A B", "SRE", "a.b@other.example.org"), "vistula.example.com")
    posting = cand("A B", "Recruiter", "a.b@agency.example.org", source="Job posting")
    assert keep(posting, "vistula.example.com")  # kept as written
    assert not keep(cand("A B", "SRE", "not-an-email"), "vistula.example.com")


def test_fill_slots_prefers_country_dedupes_and_stops_at_the_mix():
    domain = "vistula.example.com"
    people = [
        cand("Out Side", "DevOps Engineer", "out.side@vistula.example.com", country="Germany"),
        cand("Anna One", "DevOps Engineer", "anna.one@vistula.example.com"),
        cand("Anna Dup", "DevOps Engineer", "ANNA.ONE@vistula.example.com"),
        cand("Bea Two", "Cloud Engineer", "bea.two@vistula.example.com"),
        cand("Cid Three", "SRE", "cid.three@vistula.example.com"),
        cand("Hugo Hire", "Engineering Manager", "hugo@vistula.example.com", country=None,
             country_unverified=True),
        cand("Priv Ate", "Recruiter", "priv.ate@gmail.com"),
        cand("Rex Far", "Recruiter", "rex.far@vistula.example.com", country="Ireland"),
        cand("Otto Other", "Office Manager", "otto@vistula.example.com"),
    ]
    seen = set()
    chosen = fill_slots(people, {PEER: 2, HIRING: 1, RECRUITER: 1}, "Poland", domain, seen)
    got = [(c.name, kind, note) for c, kind, note in chosen]
    assert got == [
        ("Anna One", PEER, ""),
        ("Bea Two", PEER, ""),
        ("Hugo Hire", HIRING, "country unverified"),
        ("Rex Far", RECRUITER, "outside Poland"),
    ]
    assert "anna.one@vistula.example.com" in seen


def test_fill_slots_skips_emails_already_seen():
    seen = {"anna.one@vistula.example.com"}
    people = [cand("Anna One", "DevOps Engineer", "anna.one@vistula.example.com")]
    assert fill_slots(people, {PEER: 1}, "Poland", "vistula.example.com", seen) == []


def test_parse_mix():
    mix = parse_mix("peer=2, hiring=1, recruiter=1", "4")
    assert (mix.peer, mix.hiring, mix.recruiter, mix.total, mix.note) == (2, 1, 1, 4, None)
    assert parse_mix("peer=3, hiring=1, recruiter=1", "4").total == 5
    assert parse_mix("peer=1, hiring=1, recruiter=1", "4").total == 4
    bad = parse_mix("2 Peer engineer · 1 Hiring manager", "4")
    assert (bad.peer, bad.hiring, bad.recruiter, bad.total) == (2, 1, 1, 4)
    assert "could not be read" in bad.note
    assert parse_mix(None).total == 4


def test_jd_emails():
    text = (
        "We are hiring a DevOps Engineer.\n"
        "Questions? Contact Ola Example (ola.example@vistula.example.com).\n"
        "Or send your CV to careers@vistula.example.com or jobs@vistula.example.com.\n"
        "Kasia Nowak-Example\n"
        "kasia@vistula.example.com\n"
        "private: someone@gmail.com\n"
        "no name here: team@vistula.example.com\n"
    )
    found = {e.email: e for e in extract(text)}
    assert found["ola.example@vistula.example.com"].name == "Ola Example"
    assert found["careers@vistula.example.com"].generic
    assert found["careers@vistula.example.com"].name == "careers@vistula.example.com"
    assert found["jobs@vistula.example.com"].generic
    assert found["kasia@vistula.example.com"].name == "Kasia Nowak-Example"
    assert "someone@gmail.com" not in found
    assert "team@vistula.example.com" not in found  # no name and not a generic mailbox


# ---------------------------------------------------------------- 22 Oct: any mix, no juniors


def person(name, title, country="Poland"):
    from jobengine.contacts.models import Candidate
    return Candidate(name=name, first_name=name.split()[0], title=title,
                     email=f"{name.split()[0].lower()}@acme.example.com", country=country,
                     provider_verified=True, source="Hunter")


def test_any_mix_fills_open_slots_with_any_type():
    from jobengine.contacts.classify import fill_slots
    from jobengine.contacts.models import HIRING, PEER, RECRUITER
    people = [person("Ann One", "DevOps Engineer"), person("Bob Two", "Platform Engineer"),
              person("Cid Three", "Cloud Engineer"), person("Dee Four", "SRE")]
    slots = {PEER: 2, HIRING: 1, RECRUITER: 1}
    fixed = fill_slots(people, slots, "Poland", "acme.example.com", set())
    assert len(fixed) == 2  # the old rule: only the 2 peer slots
    flexible = fill_slots(people, slots, "Poland", "acme.example.com", set(), flexible=True)
    assert [c.name for c, _, _ in flexible] == ["Ann One", "Bob Two", "Cid Three", "Dee Four"]
    assert {kind for _, kind, _ in flexible} == {PEER}  # their type (and template) stays


def test_junior_people_are_never_taken():
    from jobengine.contacts.classify import fill_slots, is_junior
    from jobengine.contacts.models import PEER
    assert is_junior("Junior DevOps Engineer") and is_junior("DevOps Intern")
    assert is_junior("Stażysta DevOps") and not is_junior("Senior DevOps Engineer")
    got = fill_slots([person("Ann One", "Junior DevOps Engineer"),
                      person("Bob Two", "Graduate Cloud Engineer")], {PEER: 2}, "Poland",
                     "acme.example.com", set(), flexible=True)
    assert got == []


def test_left_slots_counts_only_the_total_in_any_mix():
    from jobengine.contacts.classify import left_slots
    from jobengine.contacts.models import HIRING, PEER, RECRUITER
    slots = {PEER: 2, HIRING: 1, RECRUITER: 1}
    taken = [PEER, PEER, PEER]
    assert sum(left_slots(slots, taken, flexible=True).values()) == 1
    assert left_slots(slots, taken, flexible=False) == {HIRING: 1, RECRUITER: 1}


def test_people_outside_it_never_fill_a_slot():
    from jobengine.contacts.classify import fill_slots, wide_kind
    # 22 Oct, Murata: a legal team leader took the Hiring slot through "team lead", and the
    # wider search took a supply-chain project engineer through "engineer".
    for title in ("Legal Team Leader/Legal Counsel", "SCM Senior Project Engineer",
                  "Senior Sales Engineer", "Head of Finance", "Electrical Engineer"):
        assert classify_title(title) == OTHER, title
        assert wide_kind(title, "engineering") == OTHER, title
    assert classify_title("HR Teamleader") == RECRUITER  # HR still fills the recruiter slot
    assert classify_title("DevOps Team Lead") == HIRING
    got = fill_slots([person("Cara Law", "Legal Team Leader/Legal Counsel"),
                      person("Dan Chain", "SCM Senior Project Engineer"),
                      person("Eve Ops", "Senior DevOps Engineer")],
                     {PEER: 2, HIRING: 1}, "Poland", "acme.example.com", set(), flexible=True)
    assert [c.name for c, _, _ in got] == ["Eve Ops"]
