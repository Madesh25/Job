"""Flow feature 7 (10 Oct): the recruitment agencies channel (/agencies)."""

from datetime import date

from test_telegram_bot import desk, settings, talk  # noqa: F401  (desk is a fixture)

from jobengine.bot_state import FakeBotState
from jobengine.config_store import ConfigRow, ConfigStore
from jobengine.contacts import agencies as ag

S = settings()
ALL = ag.load(S)
TODAY = date(2026, 10, 10)


def test_twelve_agencies_in_three_countries():
    assert len(ALL) == 12
    assert {a.country for a in ALL} == {"Poland", "Netherlands", "Ireland"}
    undutchables = next(a for a in ALL if a.name == "Undutchables")
    assert undutchables.focus == "international candidates, IT"
    assert undutchables.key == "undutchables"
    assert next(a for a in ALL if a.name == "Hays Poland").key == "hays_poland"
    assert [a.name for a in ag.find(ALL, "hays ireland")] == ["Hays Ireland"]
    assert len(ag.find(ALL, "hays")) == 3


def test_intro_is_the_template_filled_and_never_guesses_an_email():
    sigmar = ag.find(ALL, "sigmar")[0]
    text = ag.intro(S, ConfigStore.fake(), sigmar)
    assert "No email set: register on sigmarrecruitment.com" in text
    assert "Subject: DevOps Engineer open to relocate to Ireland" in text
    assert "Hello Sigmar Recruitment team," in text
    assert "I need an Irish employment permit." in text
    assert "My notice period is 90 days" in text
    assert text.endswith("When it is sent: /agencies sent Sigmar Recruitment")
    assert chr(0x2014) not in text and chr(0x2013) not in text
    config = ConfigStore(dict(ConfigStore.fake()._rows) | {
        "agency_email.sigmar_recruitment": ConfigRow(key="agency_email.sigmar_recruitment",
                                                     value="it@sigmar.example")})
    assert "Send it to it@sigmar.example with your resume attached." in \
        ag.intro(S, config, sigmar)


def test_contacts_and_the_digest_reminder():
    state = FakeBotState()
    assert len(ag.due(ALL, state, TODAY)) == 12
    for a in ALL:
        ag.mark(state, a, date(2026, 9, 1))
    assert ag.due(ALL, state, date(2026, 9, 20)) == []
    assert ag.digest_line(ALL, state, date(2026, 9, 20)) is None
    assert ag.digest_line(ALL, state, TODAY) == (
        "Agencies to contact or follow up (12 not in the last 30 days): /agencies")


def test_agencies_command(desk):  # noqa: F811
    text = talk(desk, "/agencies").sent[0][1]
    assert "Poland:\n- Hays Poland (IT and IT contracting): register on hays.pl; not " \
           "contacted yet" in text
    assert "Ireland:" in text and "Netherlands:" in text
    assert talk(desk, "/agencies hays").sent[0][1] == \
        "[LOCAL] Which agency? Hays Poland, Hays Netherlands, Hays Ireland"
    assert "Hello Cpl team," in talk(desk, "/agencies cpl").sent[0][1]
    assert talk(desk, "/agencies sent cpl").sent[0][1].startswith(
        "[LOCAL] Recorded: Cpl contacted on")
    assert "Cpl (Technology and IT): register on cpl.com; contacted 2026-" in \
        talk(desk, "/agencies").sent[0][1]
