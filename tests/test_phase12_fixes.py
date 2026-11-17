"""Phase 12 round 1 fixes (28 Oct, from Sonnet's test results): /drafts only writes drafts,
"nothing found" is remembered, a job row without a Country gets one, the Missing line shows
at 0 found, and /outreach counts only the providers that can be asked."""

from datetime import date, timedelta

from test_mail_sender import ready_desk
from test_telegram_bot import settings

from jobengine.bot_state import FakeBotState
from jobengine.config_store import ConfigStore
from jobengine.contacts import finder
from jobengine.contacts.models import ContactsResult
from jobengine.outreach import planner
from jobengine.reference import Reference

TODAY = date(2026, 10, 28)


def test_drafts_command_never_sends_in_send_mode():
    d = ready_desk()  # mail mode send
    replies = d.drafts_command("pl-clean")
    assert d.mail.gmail().sent == []
    assert replies[-1].text.startswith("Mail mode is send, but /drafts only writes drafts")


def test_nothing_found_is_remembered_for_the_same_domain_only():
    state = FakeBotState()
    finder.remember_none(state, "Zowie", "getzowie.com", TODAY)
    assert finder.recent_none(state, "Zowie", "getzowie.com", TODAY + timedelta(days=6)) == TODAY
    assert finder.recent_none(state, "Zowie", "getzowie.com", TODAY + timedelta(days=7)) is None
    assert finder.recent_none(state, "Zowie", "zowie.ai", TODAY) is None  # a new domain
    assert finder.recent_none(state, "Pixaera", "getzowie.com", TODAY) is None


def test_a_job_without_a_country_gets_one_from_its_city_or_company():
    s = settings()
    ref = Reference.fake()
    assert finder.job_country({"City": "Kraków"}, ref, s) == "Poland"
    known = next(c for c in ref.companies if c.region in finder.CHECKED_COUNTRIES)
    assert finder.job_country({"Company": known.name}, ref, s) == known.region
    assert finder.job_country({"Company": "Nobody Known"}, ref, s) is None


def test_the_missing_line_shows_when_nothing_was_found():
    result = ContactsResult(job_id="j", company="Zowie", role="SRE", country="Poland",
                            target=2)
    result.missing = {"Peer engineer": 1, "Hiring": 1}
    text = finder._summary(result, [], "Credits: Prospeo 5/75")
    assert "No contacts found." in text
    assert "0 of 2 found. Missing: 1 peer, 1 hiring." in text
    assert text.rstrip().endswith("Credits: Prospeo 5/75")


def test_outreach_counts_only_the_providers_that_can_be_asked():
    config = ConfigStore.from_values({
        "credits.apollo": "0 / 75 per month", "credits.hunter": "5 / 50 per month",
        "credits.snov": "0 / 50 per month", "credits.prospeo": "21 / 75 per month"})
    state = FakeBotState()
    every = planner.status_text(state, config, TODAY)
    some = planner.status_text(FakeBotState(), config, TODAY, usable={"hunter", "prospeo"})
    assert "Apollo 75 left" in every
    assert "Apollo not used (no key or no API this month)" in some
    assert "Hunter 45 left" in some and "Prospeo 54 left" in some


def test_the_desk_knows_which_providers_it_may_ask():
    d = ready_desk()
    assert d.usable_providers() is None  # test files answer for every provider here
