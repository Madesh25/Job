from datetime import date

import pytest

from jobengine.settings import load_settings
from jobengine.sweep.models import Job, RawPosting, Skipped
from jobengine.sweep.normalize import (
    Rules,
    canon,
    canon_city,
    canon_company,
    canon_title,
    dedupe_key,
    detect_location,
    normalize,
    parse_active_countries,
    seniority,
    title_scope,
    years_required,
)

RULES = Rules.from_config(load_settings("local", {}).sweep)
ACTIVE = ["Poland", "Netherlands", "Ireland"]


def raw(title="Senior DevOps Engineer", company="Acme Sp. z o.o.", location="Warszawa", **kw):
    base = dict(
        source="adzuna",
        board="Adzuna",
        title=title,
        company=company,
        location_text=location,
        url="https://example.com/job/1",
        posting_id="1",
    )
    base.update(kw)
    return RawPosting(**base)


def test_canon_strips_accents_and_polish_l():
    assert canon("  Łódź,  Wrocław!! ") == "lodz wroclaw"
    assert canon("C++ / C# Dev") == "c++ c# dev"


@pytest.mark.parametrize("city", ["Kraków", "Krakow", "Cracow"])
def test_krakow_spellings_give_same_key(city):
    country, detected = detect_location(city, RULES)
    assert (country, detected) == ("Poland", "Kraków")
    assert dedupe_key("Acme", "DevOps Engineer", detected, country) == (
        "acme|devops engineer|krakow"
    )
    assert canon_city(city) == "krakow"


@pytest.mark.parametrize("city", ["Den Haag", "The Hague", "'s-Gravenhage"])
def test_den_haag_aliases_give_same_key(city):
    assert canon_city(city) == "den haag"
    country, detected = detect_location(f"{city}, Netherlands", RULES)
    assert (country, detected) == ("Netherlands", "Den Haag")


def test_wroclaw_polish_spelling():
    assert detect_location("Wrocław, dolnośląskie", RULES) == ("Poland", "Wrocław")
    assert canon_city("Wrocław") == canon_city("Wroclaw") == "wroclaw"


@pytest.mark.parametrize(
    "name, expected",
    [
        ("Acme Sp. z o.o.", "acme"),
        ("Acme sp. z o.o.", "acme"),
        ("Tulip Cloud B.V.", "tulip cloud"),
        ("Shamrock Systems Ltd", "shamrock systems"),
        ("Example Group S.A.", "example"),
        ("Contoso N.V.", "contoso"),
        ("Group", "group"),
    ],
)
def test_company_suffixes_stripped(name, expected):
    assert canon_company(name) == expected


@pytest.mark.parametrize(
    "title",
    [
        "Senior DevOps Engineer (m/f/d)",
        "Senior DevOps Engineer",
        "Senior DevOps Engineer (K/M)",
        "Senior DevOps Engineer m/w/d",
        "Senior DevOps Engineer (f/m/x) (Remote)",
        "Senior DevOps Engineer [hybrid]",
    ],
)
def test_gender_markers_do_not_change_key(title):
    assert canon_title(title) == "senior devops engineer"


def test_title_scope():
    assert title_scope("Senior Site Reliability Engineer", RULES) is None
    assert title_scope("Kubernetes Platform Engineer", RULES) is None
    assert title_scope("Lead DevOps Engineer", RULES) == "title excluded (lead)"
    assert title_scope("DevOps Team Manager", RULES) == "title excluded (manager)"
    assert title_scope("Frontend Developer", RULES) == "title not in scope"
    # Whole words only: "internal" is not "intern".
    assert title_scope("Internal Platform Engineer", RULES) is None


@pytest.mark.parametrize(
    "title, expected",
    [
        ("Junior DevOps Engineer", "Junior"),
        ("Jr. SRE", "Junior"),
        ("Regular DevOps Engineer", "Mid"),
        ("Medior Cloud Engineer", "Mid"),
        ("Mid DevOps", "Mid"),
        ("Senior SRE", "Senior"),
        ("Sr Platform Engineer", "Senior"),
        ("DevOps Engineer", "Unknown"),
    ],
)
def test_seniority(title, expected):
    assert seniority(title) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("You have 3+ years of experience with Kubernetes", 3),
        ("at least 4 years in operations", 4),
        ("Minimum 3 years of AWS", 3),
        ("3-5 years of Terraform, 2+ years of Go", 2),
        ("4\u20136 years with Linux", 4),
        ("Wymagamy: 3 lata doswiadczenia", 3),
        ("min. 4 lat doswiadczenia", 4),
        ("No experience numbers here", None),
        (None, None),
    ],
)
def test_years_required(text, expected):
    assert years_required(text) == expected


def test_location_remote_with_country_and_unknown():
    assert detect_location("Remote, Poland", RULES) == ("Poland", "Remote")
    assert detect_location("Dublin, Co. Dublin", RULES) == ("Ireland", "Dublin")
    assert detect_location("Berlin, Germany", RULES) == (None, None)
    assert detect_location("", RULES) == (None, None)


def test_adzuna_area_used_first():
    assert detect_location("Mazowieckie", RULES, area=("Polska", "Mazowieckie", "Warszawa")) == (
        "Poland",
        "Warszawa",
    )


def test_normalize_builds_job():
    job = normalize(
        raw(description="3+ years with Kubernetes", salary_text="20000 - 25000 PLN (Adzuna)"),
        RULES,
        ACTIVE,
    )
    assert isinstance(job, Job)
    assert job.dedupe_key == "acme|senior devops engineer|warszawa"
    assert job.posting_ref == "adzuna:1"
    assert job.seniority == "Senior"
    assert job.years_required == 3
    assert job.city == "Warszawa" and job.country == "Poland"


def test_lead_title_and_foreign_job_are_out_of_scope():
    lead = normalize(raw(title="Lead DevOps Engineer"), RULES, ACTIVE)
    abroad = normalize(raw(location="Praha, Czechia"), RULES, ACTIVE)
    inactive = normalize(raw(location="Dublin"), RULES, ["Poland"])
    assert isinstance(lead, Skipped) and "lead" in lead.reason
    assert isinstance(abroad, Skipped) and "location" in abroad.reason
    assert isinstance(inactive, Skipped) and "not active" in inactive.reason


def test_normalize_never_infers_salary_or_date():
    job = normalize(raw(), RULES, ACTIVE)
    assert job.salary is None and job.posted_date is None
    dated = normalize(raw(posted_date=date(2026, 9, 1)), RULES, ACTIVE)
    assert dated.posted_date == date(2026, 9, 1)


def test_parse_active_countries():
    assert parse_active_countries("Poland, Netherlands, Ireland") == ACTIVE
    assert parse_active_countries("") == []
