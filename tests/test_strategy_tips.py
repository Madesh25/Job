"""Adopted strategy tips on top of V16 (7 Oct): resume and ATS tips go to the tailoring AI
(the V16 gate still decides), application, outreach and interview tips are shown where they
help. V16 itself never changes."""

from datetime import date

from test_apply_pack import ready_desk

from jobengine.resume import builder, plan
from jobengine.screen import desk as desk_module
from jobengine.strategy import tips
from jobengine.track import ping


def row(text, category, countries="All", day=1, status="Adopted"):
    return (f"p{day}", {"Tip / rule": text, "Category": category, "Status": status,
                        "Date added": date(2026, 10, day),
                        "Notes": f"countries: {countries}; why: x; run r1"})


ROWS = [row("Put Kubernetes first in Skills", "Resume", day=1),
        row("Use the job's exact title", "ATS", "Poland", day=3),
        row("Mention the EU Blue Card", "Application", "Netherlands", day=2),
        row("Write to the hiring manager first", "Outreach", day=4),
        row("Prepare a STAR story on incidents", "Interview", day=5),
        row("Old idea", "Resume", day=6, status="Rejected")]


def test_adopted_tips_by_category_and_country_newest_first():
    adopted = tips.adopted(ROWS)
    assert [t.text for t in adopted][0] == "Prepare a STAR story on incidents"
    assert tips.pick(adopted, tips.RESUME, "Poland") == [
        "Use the job's exact title", "Put Kubernetes first in Skills"]
    assert tips.pick(adopted, tips.RESUME, "Ireland") == ["Put Kubernetes first in Skills"]
    assert tips.pick(adopted, tips.APPLICATION, "Poland") == []
    assert tips.lines("Tips", []) == ""
    assert "V16 rules come first" in tips.lines("Tips", ["a"])


def test_resume_tips_reach_the_tailoring_ai_after_the_rules(tmp_path, monkeypatch):
    from test_resume_builder import JOB, make

    deps, _ = make(tmp_path)
    deps.tips = lambda: tips.adopted(ROWS)
    seen = []
    real = builder.make_plan

    def spy(*args, **kwargs):
        seen.append(kwargs.get("tips"))
        return real(*args, **kwargs)

    monkeypatch.setattr(builder, "make_plan", spy)
    builder.build_resume(deps, JOB)  # a Poland job
    assert seen and seen[0] == ["Use the job's exact title", "Put Kubernetes first in Skills"]
    assert "The rules above always come\nfirst" in plan.SYSTEM_PROMPT


def test_without_adopted_tips_the_prompt_stays_as_before(tmp_path):
    from test_resume_builder import make

    deps, _ = make(tmp_path)
    assert deps.tips() == []  # the fake has no tips: prompts stay as before


def test_apply_pack_shows_the_application_tips_for_the_country(monkeypatch):
    d = ready_desk()
    d.strategy.existing = lambda: [row("Attach a cover letter in English", "Application",
                                       "Poland"),
                                   row("Mention the EU Blue Card", "Application",
                                       "Netherlands", day=2)]
    job = next(pid for pid, v in d.repo.rows.items() if v.get("Country") == "Poland")
    text = d.apply_pack(job)[0].text
    assert "Tips for this application (your adopted tips; V16 rules come first):" in text
    assert "- Attach a cover letter in English" in text
    assert "Blue Card" not in text


def test_tips_never_break_a_reply_when_strategy_cannot_be_read():
    d = ready_desk()

    def broken():
        raise RuntimeError("notion down")

    d.strategy.existing = broken
    assert d.tips(tips.APPLICATION, "Poland") == []


def test_interview_replies_get_the_interview_tips(monkeypatch):
    d = ready_desk()
    d.strategy.existing = lambda: [row("Prepare a STAR story on incidents", "Interview")]
    monkeypatch.setattr(ping, "due", lambda track, now: True)
    monkeypatch.setattr(ping, "check", lambda track, now: [
        f"New reply from Ana\n{ping.INTERVIEW_HINT}", "New reply from Bo\nThanks"])
    out = [r.text for r in d.reply_ping_tick()]
    assert "- Prepare a STAR story on incidents" in out[0]
    assert "Interview tips" not in out[1]
    assert desk_module.tips_mod is tips
