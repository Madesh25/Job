"""LOG-10 (24 Nov test log, T23-03 note 5): 19 Sii "Senior Technical Product Owner" postings in
19 cities were each counted as "title not DevOps-type", while "same job in another city"
counted only 7. A job left out for a reason now counts once; its other cities are "same job
in another city"."""

from datetime import date

from jobengine.notion_repo import FakeJobsRepo
from jobengine.settings import load_settings
from jobengine.sweep import fakes
from jobengine.sweep.models import RawPosting, SourceResult
from jobengine.sweep.runner import OTHER_CITY, SweepDeps, run_sweep

S = load_settings("local", {})
TODAY = date(2026, 10, 1)  # the fixtures' day (their strategy is from 23 Sep)
CITIES = ["Warszawa", "Kraków", "Wrocław", "Gdańsk", "Poznań", "Łódź", "Katowice", "Lublin",
          "Szczecin", "Bydgoszcz", "Rzeszów", "Białystok", "Toruń", "Kielce", "Opole",
          "Olsztyn", "Gliwice", "Częstochowa", "Radom"]


def posting(title, city, n, source="ats", company="Sii"):
    return RawPosting(source=source, board="Company site", title=title, company=company,
                      location_text=f"{city}, Poland", url=f"https://example.com/{n}",
                      posting_id=f"{source}-{n}", posted_date=TODAY)


def sweep(postings):
    deps = SweepDeps(
        config=fakes.config, companies=lambda: [], repo=FakeJobsRepo([]),
        gmail=lambda: SourceResult("gmail"),
        adzuna=lambda: SourceResult("adzuna"),
        ats=lambda companies, keep, state=None: SourceResult("ats", postings=postings),
    )
    return run_sweep(S, deps, TODAY)


def test_one_job_in_nineteen_cities_is_one_left_out_job():
    jobs = [posting("Senior Technical Product Owner", city, i)
            for i, city in enumerate(CITIES)]
    summary = sweep(jobs)
    counts = summary.loss.by_source["ats"]
    assert counts["title not DevOps-type"] == 1
    assert counts[OTHER_CITY] == 18
    assert summary.skipped == 1 and summary.other_cities == 18
    assert summary.not_for_you() == 19  # every posting is still counted once
    assert "\U0001F4CD Same job in another city (not saved again): 18" in (
        summary.friendly_text())


def test_other_jobs_are_not_merged_and_other_reasons_merge_on_their_own():
    jobs = [
        posting("Senior Technical Product Owner", "Warszawa", 1),
        posting("Senior Technical Product Owner", "Kraków", 2),
        posting("Product Owner", "Warszawa", 3),  # another title
        posting("Senior Technical Product Owner", "Gdańsk", 4, company="Capgemini"),
    ]
    abroad = [RawPosting(source="ats", board="Company site", title="DevOps Engineer",
                         company="Sii", location_text=f"{city}, Germany",
                         url=f"https://example.com/de{n}", posting_id=f"de-{n}",
                         posted_date=TODAY)
              for n, city in enumerate(["Berlin", "Munich"])]
    summary = sweep(jobs + abroad)
    counts = summary.loss.by_source["ats"]
    assert counts["title not DevOps-type"] == 3  # Sii, Product Owner, Capgemini
    other = [reason for reason in counts if reason not in ("title not DevOps-type", OTHER_CITY)]
    assert len(other) == 1 and counts[other[0]] == 1  # the German job, once
    assert counts[OTHER_CITY] == 2  # Sii in Kraków, the German job in Munich
