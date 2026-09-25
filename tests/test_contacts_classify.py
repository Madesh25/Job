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
