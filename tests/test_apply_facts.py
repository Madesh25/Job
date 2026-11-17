"""Phase 9 (P9-15, P9-23, 16 Oct): the cover letter and the interview hints follow your own
answers for years and notice, and gaps count by their short terms."""

from test_cover_letter import LETTER, LLM, RESUME
from test_telegram_bot import settings

from jobengine.apply import cover, interview
from jobengine.apply import facts as f
from jobengine.config_store import ConfigStore

FACTS = {"experience": "4 years overall, 3 years in DevOps",
         "notice_period": "90 days, can be shortened to 30 to 40 days"}


def test_facts_come_from_the_apply_answers():
    values = f.facts(settings(), ConfigStore.from_values({"apply.notice_period": "30 days"}))
    assert values["notice_period"] == "30 days"  # Notion Config wins
    assert values["experience"] == "4 years overall, 3 years in DevOps"
    assert "EXPERIENCE: 4 years overall" in f.block(values)
    assert f.block({}) == "FACTS (the candidate's own answers):\nnone"


def test_years_and_start_follow_your_answers():
    text, problem = f.fix("I bring 4+ years of DevOps. I am available to commence "
                          "immediately. Thanks.", FACTS)
    assert problem is None
    assert text == ("I bring 4 years of DevOps. My notice period is 90 days, can be shortened "
                    "to 30 to 40 days. Thanks.")
    text, problem = f.fix("6 years of Linux.", FACTS)
    assert problem == 'it gave 6 years, but your answer is "4 years overall, 3 years in DevOps"'
    # No notice answer: nothing to put instead, the sentence stays.
    assert f.fix("I can start immediately.", {}) == ("I can start immediately.", None)


def test_gap_terms_are_short_and_not_on_the_resume():
    gaps = ["Dynamo, Lambda, Bedrock, Generative AI technologies, prompt engineering",
            "Experience with CI/CD pipelines and Jenkins", "AWS (Organizations, SCPs)"]
    assert f.gap_terms(gaps, "AWS, Jenkins, Terraform") == [
        "Dynamo", "Lambda", "Bedrock", "Generative AI technologies", "prompt engineering",
        "CI/CD pipelines", "Organizations", "SCPs"]
    assert f.names("Lambda", "AWS Lambda functions")
    assert not f.names("Organizations", "it helps organizations grow")  # a capital needed
    assert f.names("prompt engineering", "Prompt Engineering")


def test_the_letter_gets_your_facts_and_is_fixed():
    llm = LLM({"letter": LETTER.replace("every day.", "every day for 4+ years.", 1)
               + " I am available to commence immediately."})
    letter = cover.write(llm, company="Tulip", role="DevOps Engineer", country="Netherlands",
                         jd="We need Kubernetes.", resume_text=RESUME, details=[], gaps=[],
                         key="job1", facts=FACTS)
    assert "4 years." in letter.text and "4+" not in letter.text
    assert letter.text.endswith("My notice period is 90 days, can be shortened to 30 to 40 "
                                "days.")
    assert "NOTICE PERIOD: 90 days" in llm.calls[0][2]


def test_a_letter_with_other_years_or_a_gap_term_is_left_out():
    llm = LLM({"letter": LETTER + " I have 6 years of AWS."})
    letter = cover.write(llm, company="T", role="R", country="NL", jd="x", resume_text=RESUME,
                         details=[], gaps=[], key="k", facts=FACTS)
    assert letter.text is None and "it gave 6 years" in letter.note
    llm = LLM({"letter": LETTER + " I use AWS Lambda."})
    letter = cover.write(llm, company="T", role="R", country="NL", jd="x", resume_text=RESUME,
                         details=[], gaps=["Dynamo, Lambda, Bedrock"], key="k", facts=FACTS)
    assert letter.note == "it named skills you do not have (Lambda)"


def test_hints_follow_gap_terms_and_your_years():
    terms = f.gap_terms(["Dynamo, Lambda, Bedrock"])
    gaps = ["Dynamo, Lambda, Bedrock"]
    assert interview.checked_hint("Built Lambda functions.", gaps, terms) == interview.HONEST
    assert interview.checked_hint("4+ years with EKS.", gaps, terms, FACTS) == "4 years with EKS."
    assert interview.checked_hint("6 years AWS.", gaps, terms, FACTS) == (
        "(give your real experience, 4 years overall, 3 years in DevOps, and one project "
        "from your resume)")


def test_n_plus_before_in_is_your_number():
    """26 Oct, /prep: "4 years professional experience, 3+ in DevOps"."""
    text, problem = f.fix("4 years professional experience, 3+ in DevOps, on C++ 24+7.", FACTS)
    assert (text, problem) == ("4 years professional experience, 3 in DevOps, on C++ 24+7.",
                               None)
