"""The AI comparison: the same jobs through two models, side by side.

python -m jobengine.llm_compare --model gemini-3.5-flash-lite [--base claude-haiku-4-5]
    [--jobs 20] [--tasks score,cover,prep,resume] [--job <page id> ...] [--thinking low]

It reads the newest screened jobs of this environment (Apply high, normal, low and Needs
review, with a full description) and runs each task with both models:
- score: the screening extraction. Counted: answers that agree with the base model, and
  quotes the quote check had to drop (a dropped quote is a made-up fact).
- cover: the cover letter (from your Golden Master text, not an approved PDF).
- prep: the /prep interview questions.
- resume: a full resume build without writing anything; both PDFs go to out/compare/.

Nothing is written to Notion, Gmail or Drive. The models are called by name, so this also
runs locally, where the bot itself always uses haiku (llm.force_model). A Gemini call is not
answered by Haiku here: a failure is counted as a failure. The report is
out/compare/compare-<date>.html; it ends with the time, the failures and the estimated cost
of each model. 20 jobs and all four tasks cost about $0.50 with haiku.
"""

from __future__ import annotations

import argparse
import html
import logging
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from jobengine.llm import (
    AnthropicLLM,
    GeminiLLM,
    LLMError,
    Usage,
    is_gemini,
)
from jobengine.settings import ROOT_DIR, Settings

log = logging.getLogger("jobengine.llm_compare")

TASKS = ("score", "cover", "prep", "resume")
DEFAULT_JOBS = 20
OUT_DIR = ROOT_DIR / "out" / "compare"
VERDICTS = ("Apply high", "Apply normal", "Apply low", "Needs review")


class Pinned:
    """An LLM client that always uses one model, for every stage, without a fallback."""

    def __init__(self, model: str, call: Callable[..., dict[str, Any]], usage: Usage):
        self.model = model
        self._call = call
        self.usage = usage

    def complete_json(
        self, stage: str, system: str, user: str, *, max_tokens: int = 2000,
        key: str | None = None,
    ) -> dict[str, Any]:
        return self._call(stage, system, user, max_tokens=max_tokens)


def pinned(s: Settings, config: Any, model: str, thinking: str | None = None) -> Pinned:
    """The real client for `model` (needs ANTHROPIC_API_KEY or GEMINI_API_KEY)."""
    if is_gemini(model):
        gemini = GeminiLLM(s)
        return Pinned(model, lambda stage, system, user, max_tokens: gemini.complete_json(
            stage, system, user, model=model, max_tokens=max_tokens, thinking=thinking),
            gemini.usage)
    claude = AnthropicLLM(s, config)
    return Pinned(model, lambda stage, system, user, max_tokens: claude.complete_json(
        stage, system, user, model=model, max_tokens=max_tokens), claude.usage)


@dataclass
class Job:
    page_id: str
    company: str
    role: str
    country: str
    jd: str
    details: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)


@dataclass
class Answer:
    """One task for one job with one model."""

    ok: bool
    seconds: float
    lines: list[str]  # what the model gave, shown in the report
    problem: str | None = None
    facts: dict[str, Any] = field(default_factory=dict)  # score: the compared answers
    dropped: int = 0  # score: quotes not found in the description


@dataclass
class Context:
    """What the cover, prep and resume tasks need besides the job."""

    master_text: str = ""
    facts: dict[str, str] = field(default_factory=dict)
    build: Callable[[Pinned, Job], Answer] | None = None  # the resume task


def _timed(fn: Callable[[], Answer]) -> Answer:
    start = time.monotonic()
    try:
        answer = fn()
    except Exception as exc:  # a failed call is a result, not a crash
        answer = Answer(False, 0.0, [], problem=f"{type(exc).__name__}: {exc}"[:300])
    answer.seconds = round(time.monotonic() - start, 1)
    return answer


def score_facts(ext: Any) -> dict[str, Any]:
    """The extraction answers that decide a verdict."""
    return {
        "years": ext.years,
        "sponsorship": ext.sponsorship_value,
        "work mode": ext.work_mode.value or "unknown",
        "contract": sorted(ext.contract_values),
        "languages": sorted(lang.language.lower() for lang in ext.languages
                            if lang.level == "mandatory"),
        "must-have terms": sorted({t.lower() for r in ext.mandatory_requirements
                                   for t in r.terms}),
    }


def run_score(llm: Pinned, job: Job, ctx: Context) -> Answer:
    from jobengine.screen.extract import extract

    ext, dropped = extract(llm, title=job.role, company=job.company, country=job.country,
                           jd=job.jd)
    facts = score_facts(ext)
    lines = [f"{k}: {', '.join(map(str, v)) if isinstance(v, list) else v}"
             for k, v in facts.items()]
    if dropped:
        lines.append(f"quotes not found (dropped): {', '.join(dropped)}")
    return Answer(True, 0.0, lines, facts=facts, dropped=len(dropped))


def run_cover(llm: Pinned, job: Job, ctx: Context) -> Answer:
    from jobengine.apply import cover

    letter = cover.write(llm, company=job.company, role=job.role, country=job.country,
                         jd=job.jd, resume_text=ctx.master_text, details=job.details,
                         gaps=job.gaps, key=job.page_id.replace("-", ""), facts=ctx.facts)
    if letter.text is None:
        return Answer(False, 0.0, [], problem=letter.note)
    return Answer(True, 0.0, letter.text.split("\n"))


def run_prep(llm: Pinned, job: Job, ctx: Context) -> Answer:
    from jobengine.apply import interview

    qs = interview.questions(llm, company=job.company, role=job.role, jd=job.jd,
                             resume_text=ctx.master_text, gaps=job.gaps,
                             key=job.page_id.replace("-", ""), facts=ctx.facts)
    if not qs.items:
        return Answer(False, 0.0, [], problem=qs.note)
    return Answer(True, 0.0, [f"{q} (hint: {h})" for q, h in qs.items])


def run_resume(llm: Pinned, job: Job, ctx: Context) -> Answer:
    if ctx.build is None:
        return Answer(False, 0.0, [], problem="no resume builder here")
    return ctx.build(llm, job)


RUNNERS: dict[str, Callable[[Pinned, Job, Context], Answer]] = {
    "score": run_score, "cover": run_cover, "prep": run_prep, "resume": run_resume,
}


@dataclass
class Report:
    models: list[str]
    tasks: list[str]
    jobs: list[Job]
    answers: dict[tuple[str, str, str], Answer] = field(default_factory=dict)  # job,task,model
    costs: dict[str, str] = field(default_factory=dict)  # model -> Usage.line()

    def get(self, job: Job, task: str, model: str) -> Answer | None:
        return self.answers.get((job.page_id, task, model))


def compare(jobs: list[Job], clients: list[Pinned], tasks: list[str], ctx: Context,
            progress: Callable[[str], None] = lambda _text: None) -> Report:
    report = Report([c.model for c in clients], list(tasks), list(jobs))
    for n, job in enumerate(jobs, 1):
        progress(f"Job {n} of {len(jobs)}: {job.company}, {job.role}")
        for task in tasks:
            for client in clients:
                report.answers[(job.page_id, task, client.model)] = _timed(
                    lambda c=client, t=task, j=job: RUNNERS[t](c, j, ctx))
    report.costs = {c.model: c.usage.line() or "no AI used" for c in clients}
    return report


# ---------------------------------------------------------------- summary


def agreement(report: Report) -> tuple[int, int]:
    """(score answers that agree, answers compared) between the first two models."""
    if len(report.models) < 2:
        return 0, 0
    base, other = report.models[:2]
    same = total = 0
    for job in report.jobs:
        a, b = report.get(job, "score", base), report.get(job, "score", other)
        if not (a and b and a.ok and b.ok):
            continue
        for key, value in a.facts.items():
            total += 1
            same += value == b.facts.get(key)
    return same, total


def summary_rows(report: Report) -> list[list[str]]:
    """[task, model, done, failed, average seconds, dropped quotes] per task and model."""
    rows = []
    for task in report.tasks:
        for model in report.models:
            answers = [a for job in report.jobs
                       if (a := report.get(job, task, model)) is not None]
            done = [a for a in answers if a.ok]
            seconds = sum(a.seconds for a in answers) / len(answers) if answers else 0.0
            dropped = str(sum(a.dropped for a in done)) if task == "score" else ""
            rows.append([task, model, str(len(done)), str(len(answers) - len(done)),
                         f"{seconds:.1f}", dropped])
    return rows


def summary_text(report: Report) -> str:
    lines = ["task | model | done | failed | avg seconds | dropped quotes"]
    lines += [" | ".join(row) for row in summary_rows(report)]
    same, total = agreement(report)
    if total:
        lines.append(f"Screening answers that agree: {same} of {total} "
                     f"({round(100 * same / total)}%)")
    lines += [f"Cost {model}: {line}" for model, line in report.costs.items()]
    return "\n".join(lines)


# ---------------------------------------------------------------- HTML report

STYLE = """
:root { --bg: #fff; --fg: #1d1d1f; --muted: #6b6b70; --line: #d9d9de; --bad: #b3261e;
  --head: #f3f3f6; }
@media (prefers-color-scheme: dark) { :root { --bg: #161618; --fg: #ececf0; --muted: #a0a0a8;
  --line: #34343a; --bad: #ff8a80; --head: #222226; } }
body { background: var(--bg); color: var(--fg); font: 15px/1.5 system-ui, sans-serif;
  margin: 0 auto; max-width: 1200px; padding: 16px; }
table { border-collapse: collapse; width: 100%; margin: 8px 0 24px; }
th, td { border: 1px solid var(--line); padding: 6px 8px; text-align: left;
  vertical-align: top; }
th { background: var(--head); }
.muted { color: var(--muted); } .bad { color: var(--bad); }
.pair td { width: 50%; white-space: pre-wrap; }
"""


def _cell(answer: Answer | None) -> str:
    if answer is None:
        return '<td class="muted">not run</td>'
    head = f'<span class="muted">{answer.seconds:.1f} s</span>'
    if not answer.ok:
        return f'<td>{head}<br><span class="bad">Failed: {html.escape(answer.problem or "")}' \
               '</span></td>'
    return f"<td>{head}<br>{html.escape(chr(10).join(answer.lines))}</td>"


def render_html(report: Report, today: date) -> str:
    out = [f"<!doctype html><html lang=en><head><meta charset=utf-8>"
           f"<meta name=viewport content='width=device-width, initial-scale=1'>"
           f"<title>AI comparison {today.isoformat()}</title><style>{STYLE}</style></head>"
           f"<body><h1>AI comparison, {today.isoformat()}</h1>",
           f"<p>{len(report.jobs)} jobs. Models: "
           f"{html.escape(' and '.join(report.models))}.</p><h2>Summary</h2><table><tr>"
           "<th>Task</th><th>Model</th><th>Done</th><th>Failed</th><th>Avg seconds</th>"
           "<th>Dropped quotes</th></tr>"]
    for row in summary_rows(report):
        out.append("<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in row) + "</tr>")
    out.append("</table>")
    same, total = agreement(report)
    if total:
        out.append(f"<p>Screening answers that agree: {same} of {total} "
                   f"({round(100 * same / total)}%).</p>")
    out.append("<ul>" + "".join(f"<li>{html.escape(m)}: {html.escape(line)}</li>"
                                for m, line in report.costs.items()) + "</ul>")
    for job in report.jobs:
        out.append(f"<h2>{html.escape(job.company)}, {html.escape(job.role)} "
                   f'<span class="muted">({html.escape(job.page_id)})</span></h2>')
        for task in report.tasks:
            out.append(f"<h3>{task}</h3><table class=pair><tr>"
                       + "".join(f"<th>{html.escape(m)}</th>" for m in report.models)
                       + "</tr><tr>"
                       + "".join(_cell(report.get(job, task, m)) for m in report.models)
                       + "</tr></table>")
    out.append("</body></html>")
    return "\n".join(out)


# ---------------------------------------------------------------- real wiring


def load_jobs(repo: Any, limit: int, ids: list[str]) -> list[Job]:
    """The given jobs, or the newest screened ones with a full description."""
    from jobengine.notion_repo import select_filter
    from jobengine.resume.builder import notion_id, specific_details
    from jobengine.screen.models import split_gaps
    from jobengine.screen.runner import description

    if ids:
        rows = [(notion_id(i), repo.get_values(notion_id(i)) or {}) for i in ids]
    else:
        rows = repo.query_rows(select_filter("Screen verdict", *VERDICTS))
        rows.sort(key=lambda r: r[1].get("Posted date") or date.min, reverse=True)
    jobs: list[Job] = []
    for page_id, values in rows:
        if len(jobs) >= limit:
            break
        body = repo.read_body(page_id)
        jd, kind = description(body)
        if not jd or (kind != "full" and not ids):
            continue
        jobs.append(Job(page_id=page_id, company=values.get("Company") or "",
                        role=values.get("Role") or "", country=values.get("Country") or "",
                        jd=jd, details=specific_details(body),
                        gaps=split_gaps(values.get("Gaps"))))
    return jobs


def resume_builder(s: Settings, out_dir: Path) -> Callable[[Pinned, Job], Answer]:
    """A resume build that writes nothing: the PDF goes to out/compare/."""
    from dataclasses import replace

    from jobengine.resume.builder import build_resume, real_deps

    base = real_deps(s, write=False)

    def build(llm: Pinned, job: Job) -> Answer:
        # The builder's own preview copy goes to builds/; the named copy is the one to read.
        deps = replace(base, llm=lambda _config: llm, out_dir=out_dir / "builds")
        outcome = build_resume(deps, job.page_id, force=True)
        if not outcome.ok or not outcome.pdf:
            return Answer(False, 0.0, [], problem=outcome.message)
        name = f"{job.page_id.replace('-', '')[:8]}-{llm.model}.pdf"
        (out_dir / name).write_bytes(outcome.pdf)
        fill = f"{outcome.fill:.1f}%" if outcome.fill is not None else "?"
        return Answer(True, 0.0, [f"PDF: out/compare/{name}", f"page fill {fill}",
                                  *outcome.changes])
    return build


def main(argv: list[str] | None = None) -> int:
    from jobengine.apply import facts as apply_facts
    from jobengine.config_store import ConfigStore
    from jobengine.main import banner
    from jobengine.notion_repo import NotionClient
    from jobengine.resume.master import load_master, normalised_text
    from jobengine.safety import SafetyError, check_startup
    from jobengine.settings import get_settings

    parser = argparse.ArgumentParser(prog="python -m jobengine.llm_compare")
    parser.add_argument("--model", required=True, help="the model to test, e.g. "
                        "gemini-3.5-flash-lite")
    parser.add_argument("--base", default="claude-haiku-4-5", help="the model it is "
                        "compared with (default claude-haiku-4-5)")
    parser.add_argument("--jobs", type=int, default=DEFAULT_JOBS)
    parser.add_argument("--job", action="append", default=[], help="a Job Opportunities page "
                        "ID (repeat for more); default: the newest screened jobs")
    parser.add_argument("--tasks", default=",".join(TASKS),
                        help=f"comma-separated, from {', '.join(TASKS)}")
    parser.add_argument("--thinking", help="Gemini thinking level (minimal, low, medium, "
                        "high); default: the model's own")
    args = parser.parse_args(argv)
    tasks = [t.strip() for t in args.tasks.split(",") if t.strip()]
    unknown = [t for t in tasks if t not in TASKS]
    if unknown:
        print(f"Unknown task: {', '.join(unknown)}. Choose from {', '.join(TASKS)}.")
        return 2

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S")
    try:
        s = get_settings()
        check_startup(s)
    except (SafetyError, ValueError) as exc:
        print(f"Job Engine comparison startup failed: {exc}", file=sys.stderr)
        return 1
    print(banner(s))
    if not s.notion_token:
        print("NOTION_TOKEN is missing: the jobs cannot be read.")
        return 1
    client = NotionClient(s.notion_token, s)
    config = ConfigStore.load(client, s)
    try:
        clients = [pinned(s, config, args.base), pinned(s, config, args.model, args.thinking)]
    except LLMError as exc:
        print(f"Cannot start: {exc}")
        return 1

    from jobengine.resume.builder import real_deps

    deps = real_deps(s, write=False)
    if deps.jobs is None:
        print("No Job Opportunities table for this environment.")
        return 1
    ctx = Context(facts=apply_facts.facts(s, config))
    if {"cover", "prep"} & set(tasks):
        ctx.master_text = normalised_text(load_master(deps.blocks(), config).html)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if "resume" in tasks:
        ctx.build = resume_builder(s, OUT_DIR)
    jobs = load_jobs(deps.jobs, args.jobs, args.job)
    if not jobs:
        print("No screened job with a full description found.")
        return 1
    report = compare(jobs, clients, tasks, ctx, progress=print)
    today = date.today()
    path = OUT_DIR / f"compare-{today.isoformat()}.html"
    path.write_text(render_html(report, today), encoding="utf-8")
    print(summary_text(report))
    shown = path.relative_to(ROOT_DIR) if path.is_relative_to(ROOT_DIR) else path
    print(f"Report: {shown.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
