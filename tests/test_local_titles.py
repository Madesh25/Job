"""Fetch idea 5 (13 Nov): Polish job titles in the searches and the title filter."""

from test_telegram_bot import settings

from jobengine.sweep.normalize import Rules, title_scope
from jobengine.sweep.sources import adzuna, jooble


def test_polish_cloud_titles_are_in_scope_and_others_are_not():
    rules = Rules.from_config(settings().sweep)
    assert title_scope("Inżynier ds. Chmury", rules) is None
    assert title_scope("Inżynier Platformy Chmurowej", rules) is None
    assert title_scope("Inżynier DevOps", rules) is None
    assert title_scope("Kierownik zespołu chmury", rules) == "title excluded (kierownik)"
    assert title_scope("Stażysta DevOps", rules) == "title excluded (stazysta)"
    assert title_scope("Inżynier budowy", rules) == "title not in scope"
    assert title_scope("Cloud Architect", rules) == "title excluded (architect)"


def test_jooble_searches_the_polish_titles_in_poland_only():
    s = settings()
    asked = []

    def post(url, body):
        asked.append((body["location"], body["keywords"]))
        return {"jobs": []}

    jooble.fetch(s, post)
    polish = {term for country, term in asked if country == "Poland"}
    assert "inżynier devops" in polish and "devops engineer" in polish
    assert not any(t == "inżynier devops" for c, t in asked if c != "Poland")


def test_adzuna_adds_the_polish_words_in_poland_only():
    s = settings()
    asked = {}

    def get(url, params):
        asked[url] = params["what_or"]
        return {"results": []}

    adzuna.fetch(s, get)
    pl = next(v for u, v in asked.items() if "/pl/" in u)
    nl = next(v for u, v in asked.items() if "/nl/" in u)
    assert pl.endswith("chmury chmurowy") and "chmury" not in nl
