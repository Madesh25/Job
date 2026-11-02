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


CUT = ("At Accenture the Netherlands\n, we're seeking\nDevOps\nEngineers\nwho bring deep "
       "expertise.\nWhat you'll do:\nI\nmplement the secure integration of AI models.\nH\n"
       "ands-on development.\nContribute to\nthe\ndesign of\nDevOps standards.\nEducation\n:\n"
       "BSC, Computer Science or\nrelated field\nExperience\n:\n3\n+\nyears\nSkills\n:\n"
       "Proven experience implementing CI/CD pipelines\nExperienced in\nintegrating automated "
       "testing\nC\nloud implementation experience\nBonus points for\n:\nDutch\nLanguage\n"
       "skills\n, B1+\nAgile\nways of working experience\nCloud\n,\nKubernetes certifications")


def test_sentences_cut_over_many_lines_are_joined_back():
    """Accenture (Workday) on 2 Nov: a line break after every <span> of the page."""
    assert textblocks.tidy(CUT).splitlines() == [
        "At Accenture the Netherlands, we're seeking DevOps Engineers who bring deep "
        "expertise.",
        "What you'll do:",
        "Implement the secure integration of AI models.",
        "Hands-on development.",
        "Contribute to the design of DevOps standards.",
        "Education:",
        "BSC, Computer Science or related field",
        "Experience:",
        "3+ years",
        "Skills:",
        "Proven experience implementing CI/CD pipelines",
        "Experienced in integrating automated testing",
        "Cloud implementation experience",
        "Bonus points for:",
        "Dutch Language skills, B1+",
        "Agile ways of working experience",
        "Cloud, Kubernetes certifications",
    ]
    assert textblocks.tidy(textblocks.tidy(CUT)) == textblocks.tidy(CUT)


def test_a_text_with_its_own_short_lines_is_left_alone():
    """Lists without bullets and lower case items are not cut sentences."""
    lists = "Our stack:\npython\ndocker\nkubernetes\nNice to have\nGo\nRust\nI am a team player"
    assert textblocks.tidy(lists) == lists


def test_html_lines_break_at_paragraphs_not_inside_a_sentence():
    page = ("<div><p><span>At Accenture</span>, we're seeking <b>DevOps</b> Engineers\n"
            "who bring</p><ul><li><span>I</span>mplement CI</li><li>Run <a href='#'>k8s"
            "</a></li></ul><p>Education<b>:</b> BSC<br>Experience: 3+ years</p>"
            "<script>var x = 1;</script></div>")
    assert textblocks.text_of(page) == (
        "At Accenture, we're seeking DevOps Engineers who bring\n• Implement CI\n• Run k8s\n"
        "Education: BSC\nExperience: 3+ years")
    assert textblocks.text_of("Plain text\nkeeps its lines") == "Plain text\nkeeps its lines"


def test_workday_and_job_pages_are_read_with_the_same_lines():
    from bs4 import BeautifulSoup

    from jobengine.sweep import fulltext
    from jobengine.sweep.sources.ats import html_to_text
    workday = "<p><span>At Accenture</span>, we're seeking <b>DevOps</b> Engineers</p>"
    assert html_to_text(workday) == "At Accenture, we're seeking DevOps Engineers"
    ld = ('<script type="application/ld+json">{"@type": "JobPosting", "description": '
          '"<p><span>I</span>mplement <b>secure</b> pipelines</p><p>Second</p>"}</script>')
    assert fulltext.from_json_ld(BeautifulSoup(ld, "html.parser")) == (
        "Implement secure pipelines\nSecond")


def test_a_bullet_on_its_own_line_is_joined_with_its_text():
    """2 Nov: Roche's list items hold a paragraph, so "•" stood alone above each item."""
    assert textblocks.text_of("<ul><li><p>Design AWS</p></li><li><p>Build EKS</p></li></ul>") == (
        "• Design AWS\n• Build EKS")
    old = "The opportunity:\n•\nDesign and implement\n•\nBuild EKS"
    assert textblocks.tidy(old) == "The opportunity:\n• Design and implement\n• Build EKS"
