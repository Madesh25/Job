"""Flow feature 15 (9 Oct): /prep (interview prep pack) and /thanks (thank-you mail)."""

from test_telegram_bot import desk, settings, talk  # noqa: F401  (desk is a fixture)

from jobengine import telegram_bot as tb
from jobengine.apply import interview
from jobengine.llm import LLMError

S = settings()


def test_a_hint_that_claims_a_gap_is_replaced():
    assert interview.checked_hint("Yes, Azure AKS in production.", ["Azure"]) == interview.HONEST
    honest = "I have not used Azure in production; I run EKS on AWS."
    assert interview.checked_hint(honest, ["Azure"]) == honest
    assert interview.checked_hint("Terraform modules for EKS.", ["Azure"]) == \
        "Terraform modules for EKS."


class Llm:
    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.calls = answer, error, []

    def complete_json(self, stage, system, user, *, max_tokens, key):
        self.calls.append((stage, key, user))
        if self.error:
            raise self.error
        return self.answer


def test_questions_are_checked_and_failures_are_notes():
    em = chr(0x2014)
    llm = Llm({"questions": [{"q": f"Why us {em} and why now?", "hint": "Azure daily."},
                             {"q": "", "hint": "dropped"}, "junk"]})
    qs = interview.questions(llm, company="Acme", role="SRE", jd="Kubernetes, Azure",
                             resume_text="EKS, Terraform", gaps=["Azure"], key="abc")
    assert qs.items == [("Why us , and why now?", interview.HONEST)]
    assert llm.calls[0][:2] == ("tailor", "prep-abc") and "GAPS: Azure" in llm.calls[0][2]
    failed = interview.questions(Llm(error=LLMError("down")), company="A", role="B", jd="x",
                                 resume_text="y", gaps=[], key="k")
    assert failed.items == [] and failed.note == "the AI call failed (LLMError)"
    assert interview.questions(Llm({}), company="A", role="B", jd="", resume_text="y",
                               gaps=[], key="k").note == "the job has no description"


def test_prep_command(desk):  # noqa: F811
    text = talk(desk, "/prep pl-clean").sent[0][1]
    assert text.startswith("[LOCAL] Interview prep: Vistula Cloud, DevOps Engineer "
                           "(Kraków, Poland)\nJob: https://jobs.example.com/pl-clean")
    assert "They ask for and you have: argo cd, aws, eks, kubernetes" in text
    assert "1. How did you build the EKS platform at your last job?\n   Hint: Describe" in text
    assert "3. Have you used Azure?\n   Hint: Yes, Azure AKS in production." in text  # no gap
    assert "Questions to ask them:\n- What would a successful first six months" in text
    assert text.endswith("/thanks <job link> <interviewer first name> gives the thank-you "
                         "mail to send the same day.")
    assert talk(desk, "/prep").sent[0][1] == "[LOCAL] Send /prep <job URL or page id>."


def test_thanks_command(desk):  # noqa: F811
    text = talk(desk, "/thanks pl-clean Piotr").sent[0][1]
    assert text.startswith("[LOCAL] Thank-you mail for Vistula Cloud (send it today")
    assert "Subject: Thank you for the DevOps Engineer interview\n\nHi Piotr,\n\n" in text
    assert ("about the DevOps Engineer role at Vistula Cloud. I especially enjoyed hearing "
            "about moving your workloads to EKS. I am even more") in text
    assert text.endswith("Kind regards,\nAlex Example")
    assert "Hello," in talk(desk, "/thanks pl-clean").sent[0][1]
    assert talk(desk, "/thanks nothing-here").sent[0][1] == \
        "[LOCAL] No Job Opportunities row found for nothing-here"


def test_thanks_names_a_detail_that_is_no_gap():
    text = interview.thanks_text(S.interview["thanks"], first=None, company="Acme",
                                 role="SRE", detail="their move to GitOps.", name="Alex")
    assert "role at Acme. I especially enjoyed hearing about their move to GitOps. I am" in text


def test_commands_in_the_menu():
    names = [c["command"] for c in tb.bot_commands()]
    assert "prep" in names and "thanks" in names
