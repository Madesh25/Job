"""Flow features 4 and 11 (9 Oct): a cover letter from the approved resume, and how many of
the job's keywords you have the final resume shows."""

from weasyprint import HTML

from jobengine.apply import cover

RESUME = ("Madeshwaran M. DevOps Engineer at Xerago. Kubernetes, Terraform, AWS, Jenkins. "
          "Open to relocate. Notice 90 days.")
LETTER = ("Dear Hiring Team,\n\n" + "I run Kubernetes and Terraform on AWS every day. " * 14
          + "\n\nKind regards,\nMadeshwaran M")


class LLM:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def complete_json(self, stage, system, user, *, max_tokens=2000, key=None):
        self.calls.append((stage, key, user))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def write(llm, gaps=()):
    return cover.write(llm, company="Tulip", role="DevOps Engineer", country="Netherlands",
                       jd="We need Kubernetes and Terraform.", resume_text=RESUME,
                       details=["platform team"], gaps=list(gaps), key="job1")


def test_letter_from_the_resume_and_the_job():
    llm = LLM({"letter": LETTER.replace("every day.", "every day — really.")})
    letter = write(llm)
    assert letter.text.startswith("Dear Hiring Team,") and "—" not in letter.text
    stage, key, user = llm.calls[0]
    assert stage == "tailor" and key == "cover-job1"
    assert "RESUME:\n" + RESUME in user and "COMPANY DETAILS: platform team" in user
    assert cover.block(letter).startswith("Cover letter (paste it if the form asks")


def test_a_letter_naming_a_gap_is_left_out():
    letter = write(LLM({"letter": LETTER + " I also know Bedrock."}), gaps=["Bedrock"])
    assert letter.text is None and "Bedrock" in letter.note
    assert cover.block(letter) == ("Cover letter: not written (it named skills you do not "
                                   "have (Bedrock)).")


def test_short_answers_and_failures_give_a_note():
    assert write(LLM({"letter": "Too short."})).note == "it was too short (2 words)"
    assert "the AI call failed" in write(LLM(RuntimeError("down"))).note
    long = write(LLM({"letter": "Word. " * 400}))
    assert len(long.text.split()) <= cover.MAX_WORDS


def test_resume_match_line_from_the_final_pdf():
    pdf = HTML(string=f"<p>{RESUME}</p>").write_pdf()
    text = cover.pdf_text(pdf)
    line = cover.cover_line(["Kubernetes", "Terraform", "Ansible", "Go"], text)
    assert line == ("Resume match: shows 2 of 4 job keywords you have (not on it: Ansible, "
                    "Go; Rebuild to add them).")
    assert cover.cover_line(["Kubernetes"], text) == \
        "Resume match: shows 1 of 1 job keywords you have."
    assert cover.cover_line([], text) is None and cover.pdf_text(b"not a pdf") == ""
