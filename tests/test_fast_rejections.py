"""#12 (28 Oct): learning from fast rejections. A rejection within 3 days is usually an
automatic filter; the weekly audit names what those jobs share more often. Made-up jobs."""

from datetime import date, timedelta

from test_weekly_audit import MONDAY, SUNDAY, data, lines

from jobengine.screen import visa
from jobengine.track import audit, rejections

HEAD = "Fast rejections: what the automatic filters reject (last 8 weeks)"


def job(company, applied, rejected_after=None, **kw):
    v = {"Company": company, "Role": "DevOps Engineer", "Applied date": applied,
         "Status": "Applied", "Country": "Poland", "Board": "Pracuj.pl",
         "Screen verdict": "Apply high", "Years required": "3 years", **kw}
    if rejected_after is not None:
        v["Status"] = "Rejected"
        v["Last activity date"] = applied + timedelta(days=rejected_after)
    return v


def jobs():
    day = SUNDAY - timedelta(days=10)
    fast = [job(f"Fast{i}", day, 1, **{"Years required": "5+ years", "Gaps": "Go; Kafka"})
            for i in range(3)]
    fast.append(job("Fast3", day, 2, Gaps="Istio"))
    slow = [job("Slow", day, 20, **{"Years required": "5 years"})]
    applied = [job(f"Open{i}", day) for i in range(5)]
    return [*fast, *slow, *applied]


def test_fast_and_other_applications():
    fast, others = rejections.split(jobs(), SUNDAY)
    assert [v["Company"] for v in fast] == ["Fast0", "Fast1", "Fast2", "Fast3"]
    assert len(others) == 6  # the slow rejection counts with the others
    old = job("Old", SUNDAY - timedelta(days=70), 1)
    assert rejections.split([old], SUNDAY) == ([], [])


def test_the_causes_the_fast_ones_share():
    got = rejections.review(jobs(), SUNDAY)
    names = [c.name for c in got.causes]
    assert names == ["2+ missing skills", "asked 4+ years"]  # the biggest difference first
    assert got.causes[1].line() == (
        "🔴 3 of 4 fast rejections asked 4+ years (75%; other applications 17%). Try: skip "
        "jobs asking 4+ years, or show your years more clearly at the top of the resume.")
    # Country Poland and board Pracuj.pl: all of them, but the others too, so not a cause.


def test_other_causes():
    day = SUNDAY - timedelta(days=5)
    fast = [job("A", day, 0, **{"Language required": "Polish"}),
            job("B", day, 1, Sponsorship="Stated no"),
            job("C", day, 1, **{"Visa flags": [visa.NOT_ON_IND], "Language required": "Dutch"}),
            job("D", day, 3, Country="Netherlands", **{"Visa flags": [visa.AGENCY_IE]})]
    names = [c.name for c in rejections.review([*fast, job("E", day)], SUNDAY).causes]
    assert "Polish or Dutch needed" in names  # 2 of 4 is half, the others none
    assert "no sponsorship sign" in names


def test_too_few_to_judge():
    day = SUNDAY - timedelta(days=5)
    assert rejections.audit_lines([job("A", day, 1), job("B", day)], SUNDAY) == [
        "- 1 rejection(s) within 3 days in the last 8 weeks: too few to see a pattern (needs "
        "3)."]


def test_in_the_weekly_audit():
    got = lines(audit.build(data(jobs=jobs()), MONDAY), HEAD)
    assert got[0].startswith("- 4 rejections within 3 days of applying in the last 8 weeks "
                             "(other applications: 6).")
    assert got[1].startswith("🔴 3 of 4 fast rejections 2+ missing skills")
    assert got[2].startswith("🔴 3 of 4 fast rejections asked 4+ years")
    assert "   Fast0, DevOps Engineer: rejected after 1 day(s); 5+ years; Poland; Apply high; "\
           "gaps: Go, Kafka" in got
    assert "6) from the fast rejections" in audit.OPUS_PROMPT


def test_split_handles_dates_as_text():
    v = job("T", date(2026, 10, 20), None)
    v.update({"Applied date": "2026-10-20", "Status": "Rejected",
              "Last activity date": "2026-10-21T09:00:00"})
    assert rejections.split([v], SUNDAY)[0] == [v]
