from datetime import date

from jobengine.track.stats import compute, stats_text

TODAY = date(2026, 10, 15)


def job(status, applied, country="Poland", board="LinkedIn", verdict="Apply high",
        sponsorship="Not mentioned"):
    return {"Status": status, "Applied date": applied, "Country": country, "Board": board,
            "Screen verdict": verdict, "Sponsorship": sponsorship}


JOBS = [
    job("Applied", date(2026, 10, 10)),
    job("Replied", date(2026, 10, 1)),
    job("Interview", date(2026, 9, 25), country="Ireland"),
    job("Offer", date(2026, 9, 20), country="Ireland", board="Company site"),
    job("Rejected", date(2026, 9, 18)),
    job("Ghosted", date(2026, 8, 1)),
    job("Resume built", None),
]
CONTACTS = [
    {"Type": "Hiring", "Status": "Replied", "Last contacted": date(2026, 10, 2)},
    {"Type": "Hiring", "Status": "Ghosted", "Last contacted": date(2026, 9, 1)},
    {"Type": "Peer engineer", "Status": "Contacted", "Last contacted": date(2026, 10, 12)},
    {"Type": "Peer engineer", "Status": "Drafted", "Last contacted": None},
]


def test_last_30_days():
    s = compute(JOBS, CONTACTS, TODAY, 30, "Last 30 days")
    f = s.funnel
    assert (f.applied, f.replied, f.screening, f.interview, f.offer, f.rejected, f.ghosted) == \
        (5, 3, 2, 2, 1, 1, 0)
    assert round(f.rate, 2) == 0.6
    assert s.splits["Country"]["Ireland"].replied == 2
    assert s.splits["Country"]["Poland"].applied == 3
    assert s.contacts["Hiring"].contacted == 1 and s.contacts["Hiring"].replied == 1
    assert s.contacts["Peer engineer"].contacted == 1


def test_all_time_and_text():
    s = compute(JOBS, CONTACTS, TODAY, None, "All time")
    assert s.funnel.applied == 6 and s.funnel.ghosted == 1
    assert s.contacts["Hiring"].contacted == 2
    text = stats_text(JOBS, CONTACTS, TODAY)
    assert "Last 30 days: applied 5, replied 3" in text
    assert "reply rate 60%" in text
    assert "All time: applied 6, replied 3" in text and "reply rate 50%" in text
    assert "by Country: Poland 1/3 (33%), Ireland 2/2 (100%)" in text
    assert "contacts replied by Type: Hiring 1/2 (50%), Peer engineer 0/1 (0%)" in text
