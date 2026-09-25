from datetime import date

from jobengine.track.fakes import load_threads
from jobengine.track.linking import candidates, query

THREADS = load_threads()
ALL = [m for msgs in THREADS.values() for m in msgs]
ATS = ["greenhouse.io", "teamtailor.com"]


def test_query():
    q = query(["northwind.example.org", *ATS], date(2026, 10, 5))
    assert q == ("after:1791158400 from:(northwind.example.org OR greenhouse.io OR "
                 "teamtailor.com) -in:chats")


def test_one_candidate():
    found = candidates(ALL, "Northwind Cloud", ATS)
    assert found.thread_id == "t-northwind-ats"


def test_two_candidates_ask():
    found = candidates(ALL, "Fjord Data", ATS)
    assert found.thread_id is None
    assert sorted(m.thread_id for m in found.candidates) == ["t-fjord-a", "t-fjord-b"]


def test_company_name_must_match_and_sender_must_be_known():
    assert candidates(ALL, "Unknown Example", ATS).candidates == []
    assert candidates(ALL, "Canal Payments", ATS).candidates == []  # not an ATS or its domain
    assert candidates(ALL, "Canal Payments", ["canal.example.com"]).thread_id == "t-canal-job"
