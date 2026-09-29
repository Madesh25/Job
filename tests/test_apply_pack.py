"""PR 11: the Apply pack and the per-job resume header."""

import tempfile
from datetime import date
from pathlib import Path

import pytest
from test_telegram_bot import ButtonTelegram, Clock, fake_rendering, settings, update

from jobengine import telegram_bot as tb
from jobengine.apply import pack
from jobengine.config_store import ConfigRow, ConfigStore
from jobengine.resume import header
from jobengine.resume.models import MasterResume
from jobengine.screen.desk import fake_desk
from jobengine.sweep.fakes import FAKE_TODAY

S = settings()
TITLES = header.approved_titles(S, ConfigStore.fake())
TODAY = date(2026, 10, 1)


@pytest.mark.parametrize("role, title", [
    ("Senior Site Reliability Engineer (m/f/d)", "Site Reliability Engineer"),
    ("SRE II", "Site Reliability Engineer"),
    ("DevOps Engineer", "DevOps Engineer"),
    ("Cloud Platform Engineer", "Platform Engineer"),
    ("Kubernetes Engineer, Data", "Kubernetes Engineer"),
    ("Java Developer", None),
])
def test_headline_title_is_one_of_your_titles(role, title):
    assert header.headline_title(role, TITLES) == title


@pytest.mark.parametrize("values, where", [
    ({"City": "Warsaw", "Country": "Poland"}, "Warsaw, Poland"),
    ({"City": "Remote", "Country": "Ireland"}, "Ireland"),
    ({"City": "", "Country": "Netherlands"}, "Netherlands"),
])
def test_place(values, where):
    assert header.place(values) == where


MASTER = MasterResume(
    html='<p class="headline">DevOps Engineer&nbsp; | &nbsp;Cloud &amp; Platform Engineering</p>'
         '<p class="reloc">Snapshot</p>',
    frozen_html='<p class="headline">DevOps Engineer&nbsp; | &nbsp;Cloud &amp; Platform '
                'Engineering</p><p class="reloc">Snapshot</p>{{SKILLS_ZONE}}',
    skills_main=[], skills_also=[], subhead_html="", bullets=[], companies=[], version="x")


def test_header_changes_per_job_and_keeps_the_rest():
    job = {"Role": "Site Reliability Engineer", "City": "Amsterdam", "Country": "Netherlands"}
    out = header.for_job(MASTER, S, ConfigStore.fake(), job)
    for html in (out.html, out.frozen_html):
        assert ('<p class="headline">Site Reliability Engineer&nbsp; | &nbsp;Cloud &amp; '
                'Platform Engineering</p>') in html
        assert '<p class="reloc">Chennai, India (relocating to Amsterdam, Netherlands)</p>' in html
    assert out.frozen_html.endswith("{{SKILLS_ZONE}}")
    assert header.headline_text(out) == ("Site Reliability Engineer | Cloud & Platform "
                                         "Engineering")
    # No approved title in the role: the headline stays; no template: the line stays.
    s = S.model_copy(update={"resume": {**S.resume, "relocation_template": ""}})
    same = header.for_job(MASTER, s, ConfigStore.fake(), {"Role": "Java Developer"})
    assert same.html == MASTER.html


def test_config_overrides_the_titles_and_template():
    config = ConfigStore.fake()
    rows = dict(config._rows)
    rows["resume.headline_titles"] = ConfigRow(key="resume.headline_titles",
                                               value="Platform Engineer")
    rows["resume.relocation_template"] = ConfigRow(key="resume.relocation_template",
                                                   value="Moving to {place}")
    config = ConfigStore(rows)
    out = header.for_job(MASTER, S, config, {"Role": "DevOps Engineer", "Country": "Ireland"})
    assert "DevOps Engineer&nbsp; |" in out.html  # DevOps is no longer on your list
    assert '<p class="reloc">Moving to Ireland</p>' in out.html


# ---------------------------------------------------------------- the pack


def job(country, **extra):
    base = {"Company": "Example Co", "Role": "Senior DevOps Engineer", "City": "Dublin",
            "Country": country, "URL": "https://jobs.example.com/1"}
    base.update(extra)
    return base


@pytest.mark.parametrize("country, permit, floor", [
    ("Poland", "Polish work permit (type A)", "No visa salary minimum"),
    ("Netherlands", "Highly Skilled Migrant", "EUR 4,357 a month gross"),
    ("Ireland", "General Employment Permit needs no labour market test", "EUR 36,605 a year"),
])
def test_pack_per_country(country, permit, floor):
    text = pack.build(S, ConfigStore.fake(), job(country), ["running Argo CD"], TODAY)
    assert text.startswith("Apply pack (2026-10-01): Example Co, Senior DevOps Engineer")
    assert permit in text and floor in text
    assert "- Need visa sponsorship? Yes" in text
    assert "- Notice period: 90 days, can be shortened to 30 to 40 days" in text
    assert "- Current salary: Prefer not to say" in text
    assert "- Experience: 4 years overall, 3 years in DevOps" in text
    assert "- Education: Mechanical Engineering" in text
    assert "- Gender: Male" in text
    assert ("- The role's focus on running Argo CD is close to my day-to-day work at "
            "Xerago.") in text
    assert "- Headline: DevOps Engineer" in text  # never "Senior"
    assert chr(0x2014) not in text and chr(0x2013) not in text


def test_pack_details_and_overrides():
    text = pack.build(S, ConfigStore.fake(), job("Netherlands", Salary="EUR 70k"), [], TODAY)
    assert "- Authorized to work in the Netherlands without sponsorship? No" in text
    assert "- The posting states: EUR 70k" in text
    assert "(no specific detail stored for this job: write your own line)" in text
    config = ConfigStore(dict(ConfigStore.fake()._rows) | {
        "apply.notice_period": ConfigRow(key="apply.notice_period", value="30 days")})
    s = S.model_copy(update={"apply_pack": {k: v for k, v in S.apply_pack.items()
                                            if k != "gender"}})
    text = pack.build(s, config, job("Poland"), [], TODAY)
    assert "- Notice period: 30 days" in text
    assert "- Gender: (not set: add Config apply.gender)" in text


def test_blocks_stay_under_notion_limits():
    text = "\n".join(f"line {i} " + "x" * 90 for i in range(100))
    chunks = pack.blocks(text)
    assert all(len(c) < 2000 for c in chunks)
    assert "\n".join(chunks) == text


# ---------------------------------------------------------------- in the bot


def ready_desk():
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    fake_rendering(d, Path(tempfile.mkdtemp()))
    d.screen()
    return d


def test_resume_approval_sends_and_saves_the_pack():
    d = ready_desk()
    rendered = []
    d.resume.render = lambda html: rendered.append(html) or b"%PDF-fake"
    d.tap("ap:pl-clean")
    log_id = d.resume.resume_log.all_rows()[0][0]
    replies = d.tap("ra:" + log_id.replace("-", ""))
    assert replies[1].text.startswith("Apply pack (2026-10-01): Vistula Cloud, DevOps Engineer")
    body = d.repo.read_body("pl-clean")
    assert any(b.startswith("Apply pack (2026-10-01)") for b in body)
    # The resume itself carries the job's headline and relocation line.
    assert "relocating to Krak" in rendered[-1]


def test_applypack_command():
    d = ready_desk()
    fake = ButtonTelegram([[update(1, "/applypack https://jobs.example.com/pl-clean")],
                           [update(2, "/applypack")]])
    client = tb.TelegramClient(fake)
    offset = tb.poll_once(client, settings(), None, desk=d)
    tb.poll_once(client, settings(), offset, desk=d)
    texts = [t for _, t in fake.sent]
    assert texts[0].startswith("[LOCAL] Apply pack (")
    assert texts[1] == "[LOCAL] Send /applypack <job URL or page id>."
    assert "applypack" in [c["command"] for c in tb.bot_commands()]


def test_autopilot_sends_a_pack_per_saved_resume():
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    fake_rendering(d, Path(tempfile.mkdtemp()))
    replies = d.autopilot(None)
    packs = [r.text for r in replies if r.text.startswith("Apply pack (")]
    saved = [r for r in replies if r.document]
    assert len(packs) == len(saved) > 0
