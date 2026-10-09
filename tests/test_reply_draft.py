"""Flow feature 3 (9 Oct): a draft answer under the reply ping, from your Apply pack answers."""

from datetime import date

from test_reply_ping import make_desk, texts
from test_telegram_bot import settings

from jobengine.config_store import ConfigRow, ConfigStore
from jobengine.track import reply_draft

S = settings()
FRIDAY = date(2026, 10, 9)
JOB = {"Company": "Acme", "Role": "DevOps Engineer", "Country": "Netherlands"}


def test_topics_only_from_the_new_part():
    text = ("What is your notice period and salary expectation? Do you need a visa?\n\n"
            "On Mon, 5 Oct 2026 at 10:00, Alex <alex@example.com> wrote:\n"
            "> I am open to relocate and available for a call.")
    assert reply_draft.topics(text) == ["notice", "salary", "visa"]
    assert reply_draft.topics("Thanks, I will forward your CV.") == []


def test_next_working_days_skip_the_weekend():
    assert reply_draft.next_days(FRIDAY) == ("Monday 12 Oct, Tuesday 13 Oct or "
                                             "Wednesday 14 Oct")


def test_draft_answers_each_question_with_your_answers():
    text = ("Hi Alex, can we schedule a call? Also, what is your notice period, your salary "
            "expectation and do you need sponsorship? Would you relocate?")
    out = reply_draft.draft(S, ConfigStore.fake(), text, today=FRIDAY, first="Piotr", job=JOB)
    assert out.splitlines() == [
        "Hi Piotr,",
        "",
        "Thank you for your reply about the DevOps Engineer role at Acme.",
        "I would be glad to talk. I am available any time, for example on Monday 12 Oct, "
        "Tuesday 13 Oct or Wednesday 14 Oct.",
        "My notice period is 90 days, can be shortened to 30 to 40 days.",
        "On salary: open, any range at or above the visa minimum.",
        "On the work permit: I qualify as a Highly Skilled Migrant; a recognised IND sponsor "
        "can hire me without a labour market test.",
        "On relocation: open to relocate at my own cost; relocation support is welcome.",
        "",
        "Kind regards,",
        "Alex Example",
    ]
    assert chr(0x2014) not in out and chr(0x2013) not in out


def test_unknown_country_and_unknown_sender():
    out = reply_draft.draft(S, ConfigStore.fake(), "Do you need a visa?", today=FRIDAY,
                            first=None, job=None)
    assert out.startswith("Hello,\n\nThank you for your reply.\nI need visa sponsorship: Yes.")


def test_nothing_guessed_and_config_wins():
    s = S.model_copy(update={"apply_pack": {k: v for k, v in S.apply_pack.items()
                                            if k != "notice_period"}})
    assert reply_draft.draft(s, ConfigStore.fake(), "Your notice period?", today=FRIDAY,
                             first="Ana", job=JOB) is None
    config = ConfigStore(dict(ConfigStore.fake()._rows) | {
        "reply.notice": ConfigRow(key="reply.notice", value="Notice: {notice_period}.")})
    out = reply_draft.draft(S, config, "Your notice period?", today=FRIDAY, first="Ana", job=JOB)
    assert "Notice: 90 days, can be shortened to 30 to 40 days." in out


def test_the_ping_carries_the_draft():
    pings = texts(make_desk())
    piotr = next(t for t in pings if "Piotr Example" in t)
    assert "Draft answer (copy it into your reply in Gmail, edit it, send it yourself):\n" \
           "Hi Piotr," in piotr
    assert piotr.endswith("The daily check records it; send /today to record it now.")
    rita = next(t for t in pings if "Rita Example" in t)  # "remove me" asks nothing
    assert "Draft answer" not in rita
