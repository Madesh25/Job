"""The answer starter pack (28 Oct, your OK): the common form questions asked once, saved
like /answer, salaries per country. Made-up answers."""

from datetime import datetime, timedelta

from test_telegram_bot import Clock, settings, talk

from jobengine.apply import answers, autofill, form_review, starter
from jobengine.apply.form_bot import saved_for
from jobengine.bot_state import FakeBotState
from jobengine.config_store import ConfigStore
from jobengine.notion_repo import FakeTable
from jobengine.screen.desk import fake_desk
from jobengine.sweep.fakes import FAKE_TODAY

NOW = datetime(2026, 10, 28, 10, 0)


def run(state, *replies, bank=None):
    out = [starter.start(state, NOW)]
    for text in replies:
        out.append(starter.reply(state, NOW, text, bank))
    return out


def test_the_first_question_with_an_intro_and_answers_are_saved_per_country():
    state, bank = FakeBotState(), FakeTable()
    first, saved, skipped = run(state, "60000 EUR", "skip", bank=bank)
    assert first.startswith("Starter pack: the questions forms ask most, once.")
    assert "Starter question 1 of " in first and "Netherlands" in first
    assert saved.startswith("Saved: Expected salary (Netherlands) = 60000 EUR\n\n📝 Starter "
                            "question 2 of ")
    assert skipped.startswith("Skipped.\n\n📝 Starter question 3 of ")  # Ireland skipped
    rows = answers.items(state)
    assert {(r["q"], r.get("country")) for r in rows} == {
        ("Expected salary", "Netherlands"), ("Salary expectations", "Netherlands"),
        ("Desired salary", "Netherlands")}
    assert bank.created[0] | {} == {
        "Question": "Expected salary", "Answer": "60000 EUR", "Source": "You",
        "Approved": True, "Country": "Netherlands", "Last used": NOW.date(), "Times used": 0}


def test_stop_and_start_again_asks_only_what_is_open():
    state = FakeBotState()
    run(state, "60000 EUR")
    assert starter.reply(state, NOW, "stop", None).startswith("Stopped.")
    assert starter.reply(state, NOW, "55000", None) is None  # nothing waits now
    again = starter.start(state, NOW)
    assert not again.startswith("Starter pack:")  # no intro the second time
    assert "Ireland" in again


def test_a_question_waits_one_hour():
    state = FakeBotState()
    starter.start(state, NOW)
    assert starter.reply(state, NOW + timedelta(minutes=61), "60000", None) is None
    assert answers.items(state) == []


def test_all_answered():
    state = FakeBotState()
    starter.start(state, NOW)
    last = None
    for _ in starter.QUESTIONS:
        last = starter.reply(state, NOW, "1", None)
    assert last.startswith("Saved: ") and "That was the last one." in last
    assert starter.start(state, NOW).startswith("All starter questions are answered.")


def test_each_answer_fills_its_own_field_only():
    state = FakeBotState()
    for country, pay in (("Netherlands", "60000 EUR"), ("Poland", "18000 PLN")):
        for q in starter.SALARY:
            answers.save(state, q, pay, country)
    answers.save(state, "Years of experience with AWS", "3")
    answers.save(state, "Current title", "DevOps Engineer")
    answers.save(state, "Current employer", "Xerago")
    nl = answers.for_country(answers.items(state), "Netherlands")
    pl = answers.for_country(answers.items(state), "Poland")
    assert saved_for("Salary expectations (EUR, gross per year)", nl) == "60000 EUR"
    assert saved_for("Expected salary", pl) == "18000 PLN"
    assert saved_for("Expected salary", answers.for_country(answers.items(state), "Ireland")) \
        is None  # the profile answer is used there
    assert saved_for("How many years of experience do you have with AWS?", nl) == "3"
    assert saved_for("Years of experience with Azure", nl) is None
    assert saved_for("Current job title", nl) == "DevOps Engineer"
    assert saved_for("Current employer name", nl) == "Xerago"


def test_the_same_question_for_two_countries_is_kept_twice_and_listed_with_the_country():
    state = FakeBotState()
    answers.save(state, "Expected salary", "60000 EUR", "Netherlands")
    answers.save(state, "Expected salary", "18000 PLN", "Poland")
    answers.save(state, "Expected salary", "65000 EUR", "Netherlands")  # replaces
    assert answers.listing(state).splitlines()[1:] == [
        "1. Expected salary (Poland) = 18000 PLN", "2. Expected salary (Netherlands) = 65000 EUR"]
    assert answers.pack_lines(state, "Poland")[2:] == ["- Expected salary (Poland) 18000 PLN"]
    assert answers.command(state, "Expected salary = Open").startswith("Saved:")  # any country
    assert len(answers.items(state)) == 3


def test_answer_bank_rows_keep_their_country():
    bank = FakeTable()
    bank.create({"Question": "Expected salary", "Answer": "55000", "Approved": True,
                 "Country": "Ireland"})
    bank.create({"Question": "Nationality", "Answer": "Indian", "Approved": True,
                 "Country": "Any"})
    assert form_review.bank_answers(bank) == [
        {"q": "Expected salary", "a": "55000", "country": "Ireland"},
        {"q": "Nationality", "a": "Indian"}]


def test_the_autofill_code_has_only_this_countrys_salary():
    state = FakeBotState()
    answers.save(state, "Expected salary", "60000 EUR", "Netherlands")
    answers.save(state, "Expected salary", "18000 PLN", "Poland")
    code = autofill.code(settings(), ConfigStore.from_values({}), state,
                         {"Company": "Acme", "Role": "SRE", "Country": "Poland"})
    assert autofill.decode(code)["answers"] == [["Expected salary", "18000 PLN"]]


def test_in_telegram():
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    sent = [t for _, t in talk(d, "/answers start", "60000 EUR").sent]
    assert "Starter question 1 of " in sent[0]
    assert sent[1].startswith("[LOCAL] Saved: Expected salary (Netherlands) = 60000 EUR")
    assert d.answer_bank.created[0]["Source"] == "You"
