"""Auto-apply step 2 (Autopilot H8, 25 Oct): the AI answers the open form questions in your
voice from your facts only, and code checks every answer before it is typed."""

from jobengine.apply import form_ai
from jobengine.apply.form_bot import Field, plan

FACTS = {"experience": "4 years overall, 3 years in DevOps", "notice_period": "90 days"}
CTX = form_ai.Context(
    facts=FACTS, profile={"first_name": "Madeshwaran"},
    saved=[{"q": "Years of Terraform?", "a": "3 years in production"}],
    skills=["Kubernetes (Advanced)", "Terraform (Advanced)"],
    resume="DevOps Engineer at Xerago. Kubernetes, Terraform, AWS, GitHub Actions.",
    company="Northwind Cloud", role="DevOps Engineer", jd="We run EKS and Terraform.")


class LLM:
    def __init__(self, answers=None, fail=False):
        self.answers, self.fail, self.calls = answers or [], fail, []

    def complete_json(self, stage, system, user, **kwargs):
        self.calls.append((stage, system, user))
        if self.fail:
            raise RuntimeError("HTTP 503")
        return {"answers": self.answers}


FIELDS = [Field("0", "text", "First name"),
          Field("1", "textarea", "Describe your Kubernetes experience"),
          Field("2", "text", "How many years of DevOps experience?"),
          Field("3", "radio", "Do you have Lambda experience?", ("Yes", "No")),
          Field("4", "textarea", "When can you start?"),
          Field("5", "select", "Highest education", ("Select...", "Bachelor", "Master")),
          Field("6", "textarea", "Tell us about your Go projects"),
          Field("7", "file", "Resume")]


def test_the_ai_fills_only_open_questions_and_code_checks_each():
    answers = plan(FIELDS, {"first_name": "Madeshwaran"}, [])
    llm = LLM([
        {"n": 1, "answer": "I run Kubernetes clusters at Xerago every day."},
        {"n": 2, "answer": "5+ years"},  # not your experience answer: dropped
        {"n": 3, "answer": "Yes, I use Lambda daily"},  # a gap you do not have: dropped
        {"n": 4, "answer": "I can start immediately."},  # becomes your notice period
        {"n": 5, "answer": "PhD"},  # not an option: dropped
        {"n": 6, "answer": None}])  # not in the facts: stays for you
    filled = form_ai.fill(llm, answers, CTX, gaps=["Lambda"])
    by = {a.field.ref: a for a in answers}
    assert filled == 2
    stage, system, user = llm.calls[0]
    assert stage == "apply" and "own voice" in system and "OPTIONS: Bachelor | Master" in user
    assert "Years of Terraform? -> 3 years in production" in user  # your voice
    assert "First name" not in user.split("QUESTIONS:")[1]  # filled by code: never asked
    assert by["1"].value == "I run Kubernetes clusters at Xerago every day."
    assert by["1"].source == "ai"
    assert by["2"].value is None and by["2"].note.startswith("AI answer dropped: it gave 5")
    assert by["3"].note == ("AI answer dropped: it claims Lambda, which is not in your resume "
                            "or Skills Inventory")
    assert by["4"].value == "My notice period is 90 days."
    assert by["5"].note == 'AI answer dropped: "PhD" is not one of the options'
    assert by["6"].note == "no answer yet (not in your facts)"
    assert by["7"].note.startswith("your resume")


def test_an_ai_failure_leaves_the_questions_for_you():
    answers = plan(FIELDS[1:2], {}, [])
    assert form_ai.fill(LLM(fail=True), answers, CTX) == 0
    assert answers[0].note == "no answer yet (the AI call failed)"
    assert form_ai.fill(None, answers, CTX) == 0


def test_skills_lines():
    class S:
        def __init__(self, name, level):
            self.name, self.level = name, level

    assert form_ai.skills_lines([S("Kubernetes", "Advanced"), S("Go", None)]) == [
        "Kubernetes (Advanced)", "Go"]
