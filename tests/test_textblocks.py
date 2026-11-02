"""Job descriptions as tidy paragraphs (2 Nov): your check of the DEV pages found HTML tags
as text, words cut in two and whole descriptions in one line. Shapes copied from those pages,
with the text shortened."""

from jobengine import textblocks
from jobengine.screen.runner import description
from jobengine.sweep.dedupe import description_blocks
from jobengine.sweep.models import Job


def test_html_tags_left_as_text_become_lines_and_bullets():
    """dotLinkers and Jobgether showed "<br />" all through the text."""
    escaped = ("Position:&lt;br /&gt;AI Platform Engineer&lt;br /&gt;Requirements&lt;ul&gt;"
               "&lt;li&gt;Kubernetes&lt;/li&gt;&lt;li&gt;Terraform&lt;/li&gt;&lt;/ul&gt;")
    assert textblocks.tidy(escaped) == (
        "Position:\nAI Platform Engineer\nRequirements\n• Kubernetes\n• Terraform")
    plain_tags = "Ensure protocol reliability:<br />Maintain critical <b>blockchain</b> infra"
    assert textblocks.tidy(plain_tags) == (
        "Ensure protocol reliability:\nMaintain critical blockchain infra")


def test_a_description_in_one_line_gets_breaks_before_its_headings():
    """PwC (Workday feed): the whole description was one paragraph."""
    one_line = ("PwC Poland's Digital&IPA team comprises almost 500 people. " * 25
                + "Your future role: Building new tools. Apply if you: Know Linux. "
                "Nice to have: Azure certificates.")
    lines = textblocks.tidy(one_line).splitlines()
    assert [line.split(":")[0] for line in lines[1:]] == [
        "Your future role", "Apply if you", "Nice to have"]
    short = "We offer: fruit. Requirements: Linux."
    assert textblocks.tidy(short) == short  # a short line is left alone


def test_blocks_are_cut_at_line_ends_and_never_inside_a_word():
    """"De" / "velop" and "networ" / "k" on 30 Oct."""
    text = "\n".join(f"Line {i}: " + " ".join(["develop"] * 30) for i in range(40))
    blocks = textblocks.chunks(text, 2000)
    assert all(len(b) <= 2000 for b in blocks) and len(blocks) > 1
    assert all(b.startswith("Line ") for b in blocks)
    assert "\n".join(blocks) == text
    long_line = "word " * 900
    parts = textblocks.chunks(long_line, 2000)
    assert all(p.endswith("word") and p.startswith("word") for p in parts)


def test_the_page_body_and_screening_read_the_same_text():
    text = "About the role:<br/>" + "<br/>".join(f"Duty {i}: " + "operate k8s " * 20
                                                 for i in range(30))
    job = Job(source="ats", board="Company site", company="Acme", role="SRE", city=None,
              country="Poland", url="https://acme.example/jobs/1", posted_date=None,
              salary=None, seniority="Unknown", years_required=None, dedupe_key="acme|sre|pl",
              posting_ref="ats:1", description=text, description_is_snippet=False)
    blocks = description_blocks(job)
    assert blocks[0] == "Description source: ats"
    assert not any("<br" in b for b in blocks)
    body, kind = description(blocks, 600)
    assert kind == "full" and body == textblocks.tidy(text)
