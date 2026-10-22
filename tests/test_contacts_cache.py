from datetime import date

from jobengine.contacts.cache import company_rows, months_before, pick, usable
from jobengine.contacts.classify import parse_mix
from jobengine.contacts.finder import fake_deps
from jobengine.settings import load_settings

TODAY = date(2026, 10, 1)
MIX = parse_mix("peer=2, hiring=1, recruiter=1", "4")


def rows():
    return fake_deps(load_settings("local", {})).contacts.all_rows()


def test_months_before():
    assert months_before(TODAY, 6) == date(2026, 4, 1)
    assert months_before(date(2026, 3, 31), 1) == date(2026, 2, 28)
    assert months_before(date(2026, 1, 15), 2) == date(2025, 11, 15)


def test_company_rows_use_canonical_names():
    names = {r.values["Name"] for r in company_rows(rows(), "Vistula Cloud")}
    assert names == {"Kasia Example", "Stary Example", "Bob Example", "Dora Example"}


def test_old_bounced_and_do_not_contact_are_never_used():
    vistula = {r.values["Name"]: r for r in company_rows(rows(), "Vistula Cloud")}
    assert usable(vistula["Kasia Example"], TODAY, 6)
    assert not usable(vistula["Stary Example"], TODAY, 6)  # 7 months old
    assert not usable(vistula["Bob Example"], TODAY, 6)  # Bounced
    assert not usable(vistula["Dora Example"], TODAY, 6)  # Do not contact
    chosen = pick(list(vistula.values()), MIX, "Poland", TODAY, 6)
    assert [c.name for c in chosen] == ["Kasia Example"]
    assert chosen[0].cached and chosen[0].page_id == "ct-fresh-peer"


def test_cache_fills_the_whole_mix():
    northwind = company_rows(rows(), "Northwind Cloud")
    chosen = pick(northwind, MIX, "Ireland", TODAY, 6)
    assert [c.type for c in chosen] == ["Peer engineer", "Peer engineer", "Hiring",
                                        "Recruiter/TA"]


def test_saved_people_that_break_the_title_rules_are_not_reused():
    from jobengine.contacts.cache import CachedRow

    def saved(name, title, kind):
        return CachedRow(f"ct-{name}", {"Name": name, "Title": title, "Type": kind,
                                        "Email": f"{name}@murata.example.com",
                                        "Country": "Netherlands", "Date found": TODAY})

    # 22 Oct: Murata's contacts were saved before #137 and came back from the cache.
    murata = [saved("alsu", "SCM Senior Project Engineer", "Peer engineer"),
              saved("che", "Senior Product Engineer", "Peer engineer"),
              saved("christine", "Legal Team Leader/Legal Counsel", "Hiring"),
              saved("joost", "HR Teamleader", "Recruiter/TA"),
              saved("jan", "Junior DevOps Engineer", "Peer engineer")]
    chosen = pick(murata, MIX, "Netherlands", TODAY, 6)
    assert [c.name for c in chosen] == ["che", "joost"]
