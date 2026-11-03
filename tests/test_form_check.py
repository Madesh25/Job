"""3 Nov, your decision: Claude checks every filled form right before Submit. Wrong, or the
check cannot run = not submitted (Apply by hand with the reason). Free code checks first,
only the answers that are not yours for sure go to Claude, an OK answer is never checked
again. Made-up forms and answers."""

import pytest
from test_form_bot import FORM, SAVED, VALUES, _serve, browser
from test_form_submit import _thanks
from test_telegram_bot import settings

from jobengine import llm
from jobengine.apply import form_bot, form_check, form_submit
from jobengine.apply.form_bot import Answer, Field
from jobengine.bot_state import FakeBotState
from jobengine.config_store import ConfigStore

FACTS = {"experience": "4 years overall, 3 years in DevOps", "notice_period": "90 days"}
NAME = Answer(Field("1", "text", "First name", required=True), value="Madeshwaran",
              source="profile")
EMAIL = Answer(Field("2", "email", "Email"), value="me@example.com", source="profile")
CV = Answer(Field("3", "file", "Resume/CV"), value="cv.pdf", source="resume")
SPONSOR = Answer(Field("4", "select", "Will you require visa sponsorship?",
                       ("Select...", "Yes", "No")), value="Yes", option=1, source="profile")
TERRAFORM = Answer(Field("5", "text", "Years of Terraform?"), value="3 years in production",
                   source="saved")
SALARY = Answer(Field("6", "text", "Expected salary (gross per year)"), value="65000 EUR",
                source="saved")
NOTICE = Answer(Field("7", "text", "What is your notice period?"), value="90 days",
                source="profile")
SALARY_SAVED = [*SAVED, {"q": "Expected salary", "a": "65000 EUR"}]


class Claude:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def complete_json(self, stage, system, user, **kwargs):
        self.calls.append((stage, system, user))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def checker(reply, state=None, saved=SALARY_SAVED):
    claude = Claude(reply)
    check = form_check.Checker(claude, profile=VALUES, facts=FACTS, saved=saved,
                               job="Northwind Cloud, DevOps Engineer",
                               state=state if state is not None else FakeBotState(),
                               day="2026-11-03")
    return check, claude


def test_the_free_code_checks():
    bad = [Answer(Field("a", "email", "Email"), value="me at example", source="saved"),
           Answer(Field("b", "tel", "Phone"), value="12", source="saved"),
           Answer(Field("c", "url", "LinkedIn"), value="linkedin/me", source="saved"),
           Answer(Field("d", "number", "Years of experience"), value="five", source="saved"),
           Answer(Field("e", "text", "Total years of experience"), value="6", source="saved"),
           Answer(Field("f", "radio", "Do you need sponsorship?", ("Yes", "No")),
                  value="No", option=1, source="saved"),
           Answer(Field("g", "select", "Country", ("Poland", "Ireland")), value="Spain",
                  option=None, source="saved")]
    assert form_check.code_checks(bad, VALUES, FACTS) == [
        '"Email": not an email address', '"Phone": not a phone number',
        '"LinkedIn": not a link', '"Years of experience": not a number',
        '"Total years of experience": more years than you said (4 years overall, 3 years in '
        'DevOps)',
        '"Do you need sponsorship?": your sponsorship answer is "Yes"',
        '"Country": the answer is not one of its options']
    assert form_check.code_checks([NAME, EMAIL, CV, SPONSOR, TERRAFORM], VALUES, FACTS) == []


def test_only_answers_not_yours_for_sure_go_to_claude():
    items = form_check.to_check([NAME, EMAIL, CV, SPONSOR, TERRAFORM, SALARY, NOTICE],
                                SALARY_SAVED, {})
    # Name, email: your profile word for word. The resume: approved. Terraform: your saved
    # answer to that very question. Salary: the saved question "Expected salary" is all in
    # the label too. The sponsorship dropdown and the notice field: the rule may have
    # matched a field that asks something else.
    assert items == [SPONSOR, NOTICE]
    partly = [{"q": "Expected salary in Poland per month", "a": "65000 EUR"}]
    assert form_check.to_check([SALARY], partly, {}) == [SALARY]  # matches only partly


def test_claude_ok_submits_and_is_remembered():
    state = FakeBotState()
    check, claude = checker({"wrong": []}, state)
    assert check([NAME, SPONSOR, NOTICE]) == []
    [(stage, system, user)] = claude.calls
    assert stage == "check"
    assert "sponsorship_needed: Yes" in system and "experience: 4 years overall" in system
    assert "cover_letter" not in system and "why:" not in system
    assert user == ("Fields:\n1. [select] Will you require visa sponsorship? (options: "
                    "Select... | Yes | No) = Yes\n2. [text] What is your notice period? = "
                    "90 days")
    again, claude2 = checker({"wrong": []}, state)
    assert again([NAME, SPONSOR, NOTICE]) == []
    assert claude2.calls == []  # both answers were OK before: no call at all


def test_claude_wrong_stops_the_submit_and_is_not_remembered():
    state = FakeBotState()
    check, claude = checker({"wrong": [{"n": 2, "why": "asks for the start date, not the "
                                        "notice period"}]}, state)
    assert check([SPONSOR, NOTICE]) == [
        '"What is your notice period?": asks for the start date, not the notice period '
        "(Claude's check)"]
    memory = state.get(form_check.MEMORY_KEY)["items"]
    assert form_check.digest(SPONSOR.field.label, "Yes") in memory
    assert form_check.digest(NOTICE.field.label, "90 days") not in memory


@pytest.mark.parametrize("reply", [{}, {"wrong": "no"}, {"wrong": [{"n": 9, "why": "x"}]},
                                   llm.LLMPausedError("Claude paused: cap reached")])
def test_no_clear_verdict_is_never_an_ok(reply):
    check, _ = checker(reply)
    with pytest.raises((ValueError, llm.LLMError)):
        check([SPONSOR])


def test_a_code_problem_needs_no_claude_call():
    check, claude = checker({"wrong": []})
    bad = Answer(Field("a", "email", "Email"), value="nope", source="saved")
    assert check([bad, SPONSOR]) == ['"Email": not an email address']
    assert claude.calls == []


def test_the_router_sends_the_check_to_claude_only():
    from test_claude_cap import prod_settings
    from test_llm_gemini import Recorder

    claude, gemini, deepseek = Recorder(), Recorder(), Recorder()
    route = llm.ModelRouter(prod_settings(), ConfigStore.from_values(
        {"model.check": "deepseek-v4-flash"}), claude=claude, gemini=gemini, deepseek=deepseek)
    assert route.complete_json("check", "s", "u") == {"from": llm.DEFAULT_MODEL}
    assert gemini.calls == [] and deepseek.calls == []
    assert "check" not in llm.DEEPSEEK_STAGES


@browser
def test_a_wrong_answer_is_never_sent(tmp_path, monkeypatch):
    monkeypatch.setattr(form_bot, "SETTLE_MS", 100)
    seen = {}

    def prepare(context):
        _serve(FORM)(context)
        _thanks(context)

    def check(page):
        seen["sent"] = page.evaluate("() => !!window.sent")

    verify, claude = checker({"wrong": [{"n": 1, "why": "contradicts the facts"}]})
    result = form_bot.dry_run(settings(), "https://jobs.example.com/apply", VALUES, SAVED,
                              folder=tmp_path, prepare=prepare, check=check,
                              resume=("cv.pdf", b"%PDF-1.4 x"),
                              finish=form_submit.finisher(True, verify))
    assert result.submitted.startswith("not submitted: \"")
    assert "(Claude's check)" in result.submitted
    assert seen["sent"] is False
    assert claude.calls


@browser
def test_claude_not_reachable_is_never_sent(tmp_path, monkeypatch):
    monkeypatch.setattr(form_bot, "SETTLE_MS", 100)
    verify, _ = checker(llm.LLMAuthError("Anthropic refused ANTHROPIC_API_KEY (HTTP 401)"))
    result = form_bot.dry_run(settings(), "https://jobs.example.com/apply", VALUES, SAVED,
                              folder=tmp_path, prepare=_serve(FORM),
                              resume=("cv.pdf", b"%PDF-1.4 x"),
                              finish=form_submit.finisher(True, verify))
    assert result.submitted == ("not submitted: the check before Submit could not run "
                                "(Anthropic refused ANTHROPIC_API_KEY (HTTP 401))")
