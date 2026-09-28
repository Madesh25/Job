from dataclasses import replace
from datetime import date

import pytest

from jobengine.settings import load_settings
from jobengine.sweep.fit import b2b_only, cannot_take, language_block, written_in
from jobengine.sweep.models import Job, SweepSummary
from jobengine.sweep.runner import _pick, fake_deps

TODAY = date(2026, 10, 1)
BASE = Job(
    source="adzuna", board="Adzuna", company="Example Hosting", role="DevOps Engineer",
    city="Krakow", country="Poland", url="https://jobs.example.com/1", posted_date=None,
    salary=None, seniority="Unknown", years_required=None, dedupe_key="k", posting_ref="adzuna:1",
    description="We run services.", description_is_snippet=True,
)

POLISH_TEXT = (
    "Twoj zakres obowiazkow: administracja i monitoring srodowisk chmurowych oraz "
    "automatyzacja procesow w firmie. Nasze wymagania: doswiadczenie w pracy z Azure i AWS, "
    "znajomosc Terraform i Ansible, praca w zespole. Oferujemy prace w biurze w Warszawie i "
    "na zdalnie, dla osob z doswiadczeniem, jest to projekt dla klienta z branzy finansowej, "
    "od zaraz, przy duzym projekcie z opieka medyczna, lub w trybie hybrydowym jak w zespole."
)


@pytest.mark.parametrize("text, lang", [
    ("Fluent Polish and English required.", "Polish"),
    ("Very good knowledge of Polish (C1).", "Polish"),
    ("Polish language is required.", "Polish"),
    ("Dutch: C1", "Dutch"),
    ("Vloeiend Nederlands en Engels.", "Dutch"),
    ("Wymagania: znajomosc jezyka polskiego.", "Polish"),
    ("Polish is a plus.", None),
    ("English B2, Polish nice to have.", None),
    ("You will work with Polish clients. Good English.", None),
    ("Excellent Dutch market knowledge.", None),
    ("Strong communication skills in English.", None),
    (None, None),
])
def test_language_block(text, lang):
    assert language_block(text) == lang


def test_written_in_polish_needs_english_to_pass():
    assert written_in(POLISH_TEXT) == "Polish"
    assert language_block(POLISH_TEXT) == "Polish"
    assert language_block(POLISH_TEXT + " Wymagany jezyk angielski B2.") is None
    assert written_in("We run Kubernetes and Terraform on AWS for our clients.") is None


@pytest.mark.parametrize("country, text, only", [
    ("Poland", "Cooperation is based on a B2B contract only.", True),
    ("Poland", "Rate: 185 PLN/h + VAT.", True),
    ("Poland", "B2B or UoP, you choose.", False),
    ("Poland", "Umowa o prace, 20 000 PLN brutto.", False),
    ("Poland", "Employment contract or B2B.", False),
    ("Netherlands", "B2B only.", False),
    ("Poland", "Kubernetes and Terraform.", False),
])
def test_b2b_only(country, text, only):
    assert b2b_only(country, text) is only


def test_cannot_take_names_the_skip_reason():
    assert cannot_take("Poland", "Fluent Polish is required.") == "Polish required"
    assert cannot_take("Poland", "B2B contract only.") == "B2B only"
    assert cannot_take("Poland", "Employment contract. English is required.") is None


def test_jobs_you_cannot_take_do_not_use_the_daily_places():
    s = load_settings("local", {})
    s.sweep["fulltext"] = {"lookahead": 0}
    deps = fake_deps(s)
    long = " ".join(["Kubernetes and Terraform on AWS every day."] * 20)
    pages = {"https://j.example.com/0": f"<main>{long} Fluent Polish is required.</main>",
             "https://j.example.com/1": f"<main>{long} B2B contract only.</main>",
             "https://j.example.com/2": f"<main>{long} 2-3 years required.</main>",
             "https://j.example.com/3": f"<main>{long} 3+ years required.</main>"}
    deps.page = lambda url: (url, pages[url])
    ranked = [[replace(BASE, dedupe_key=str(i), url=f"https://j.example.com/{i}")]
              for i in range(4)]
    summary = SweepSummary()
    picked = _pick(s, deps, ranked, 2, None, TODAY, summary, lambda line: None)
    assert [g[0].url for g in picked] == ["https://j.example.com/2", "https://j.example.com/3"]
    assert (summary.needs_language, summary.b2b_only) == (1, 1)
    text = summary.friendly_text()
    assert "Needs a language other than English (not saved): 1" in text
    assert "B2B contract only (not saved): 1" in text


@pytest.mark.parametrize("text, blocked", [
    ("Please note: EU/EEA work authorization is required (no visa sponsorship available for "
     "this position).", True),
    ("Travel Requirements<br>None<br>Relocation Provided<br>None<br>Position Type", True),
    ("You must have the right to work in Ireland.", True),
    ("Sponsorship is not available for this role.", True),
    ("We are unable to sponsor visas.", True),
    ("Candidates must be eligible to work without sponsorship.", True),
    ("We offer visa sponsorship and relocation support.", False),
    ("Relocation package offered.", False),
    ("We will help you obtain a work permit.", False),
    (None, False),
])
def test_no_sponsorship(text, blocked):
    from jobengine.sweep.fit import no_sponsorship

    assert no_sponsorship(text) is blocked


@pytest.mark.parametrize("title, lang", [
    ("macOS Platform Engineer (German-Speaking) | Enterprise Aviation Company", "German"),
    ("Infrastructure Engineer (L1/Shift Lead, French)", "French"),
    ("DevOps Engineer", None),
])
def test_a_language_in_the_title(title, lang):
    assert language_block("We run Kubernetes.", title) == lang


def test_other_languages_and_their_skip_reason():
    from jobengine.sweep.fit import skip_reason

    assert language_block("Fluent German is required.") == "German"
    assert cannot_take("Poland", "x", title="Engineer (French)") == "French required"
    assert skip_reason("French required") == "Other"
    assert skip_reason("Polish required") == "Polish required"
    assert skip_reason("B2B only") == "B2B only"
    assert skip_reason("No visa sponsorship") == "Other"
    assert cannot_take("Netherlands", "No visa sponsorship.") == "No visa sponsorship"


@pytest.mark.skill_match
def test_too_few_of_your_skills_is_not_saved():
    from jobengine.reference import Reference
    from jobengine.sweep.rank import Ranker
    from jobengine.sweep.runner import LOW_MATCH, FitRules, not_a_fit, skill_share

    rules = FitRules(max_years=4, ranker=Ranker(Reference.fake()))
    java = replace(BASE, description="Java, Spring Boot, Kafka and Jenkins with some Docker.")
    assert skill_share(java, rules) == 0.2  # docker of 5 tools
    assert not_a_fit(java, rules) == LOW_MATCH
    good = replace(BASE, description="Kubernetes, Terraform, AWS, Docker and a bit of Java.")
    assert skill_share(good, rules) == 0.8
    assert not_a_fit(good, rules) is None
    few = replace(BASE, description="Java and Docker.")  # 2 tools: too few to judge
    assert skill_share(few, rules) is None and not_a_fit(few, rules) is None
