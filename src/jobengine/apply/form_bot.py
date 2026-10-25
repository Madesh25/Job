"""Auto-apply dry run (Autopilot H8, 25 Oct): the bot opens a job's application form in a
browser, fills it with your answers and takes a screenshot. It never submits.

Your decisions of 25 Oct: a dry run first (fill, never submit), as many rounds as you need,
until you say the answers are good; natural answers in your voice from true facts only; no
tricks to hide the bot (a plain browser, no captcha or login workaround).

How it fills:
- your profile and Apply pack answers (apply/autofill.fields): name, email, phone, links,
  permit, sponsorship, salary, notice, start, relocation, English, why this company;
- your saved form answers (/answer, apply/answers.py), matched to the question's words;
- the questions left: one AI call in your voice from your facts, each answer checked by code
  (apply/form_ai.py, step 2); what the facts do not answer stays empty;
- an empty field is listed, so you answer it once with /answer.

What it never does:
- press a button, press Enter or submit the form (tests/test_repo_rules.py checks the code);
- send anything: every request that is not a plain GET is blocked and counted, so nothing
  reaches the company even if the page tries (autosave, tracking);
- tick a consent or privacy box (you tick those yourself);
- open LinkedIn, an IP address or a private host (safety.assert_page_fetch_allowed);
- go past a captcha or a login: it stops and says so.

Screenshots go to out/dryrun/ and are deleted after KEEP_DAYS days (never saved in Notion).
Needs Playwright on the machine that runs it: pip install -e ".[formbot]" and
python -m playwright install chromium (docs/apply-pack.md).
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from jobengine import safety
from jobengine.settings import ROOT_DIR, Settings

log = logging.getLogger("jobengine.apply")

OUT_DIR = ROOT_DIR / "out" / "dryrun"
KEEP_DAYS = 7  # not reviewed: deleted after 7 days (3 after you approve, in the next step)
LOAD_TIMEOUT_MS = 30_000
SETTLE_MS = 2_000  # forms built by JavaScript appear after the page loads
CHROMIUM_ENV = "JOBENGINE_CHROMIUM"  # optional path to a Chromium the machine already has
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
NO_PLAYWRIGHT = ("The dry run needs Playwright on this machine: pip install -e \".[formbot]\" "
                 "then python -m playwright install chromium (docs/apply-pack.md).")

# Label words -> the answer key in apply/autofill.fields; the same rules as the bookmark
# (apply/autofill.py AUTOFILL_JS RULES): (pattern, key, not when this also matches).
RULES: tuple[tuple[re.Pattern[str], str, re.Pattern[str] | None], ...] = tuple(
    (re.compile(p), key, re.compile(n) if n else None) for p, key, n in (
        (r"\b(first|given) ?name\b|\bforename\b", "first_name", r"\b(company|employer|refer)"),
        (r"\b(last|family) ?name\b|\bsurname\b", "last_name", r"\b(company|employer|refer)"),
        (r"^(full |your |legal )?name$|\bfull name\b", "full_name", None),
        (r"\be ?mail\b", "email", None),
        (r"\b(phone|mobile|telephone)\b", "phone", r"\b(type|code|extension|device)\b"),
        (r"\blinked ?in\b", "linkedin", None),
        (r"\bgit ?hub\b", "github", None),
        (r"\b(website|portfolio|personal (site|page)|blog)\b", "website",
         r"\b(linked ?in|git ?hub)\b"),
        (r"\bsponsor", "sponsorship_needed", None),
        (r"\bauthori[sz]|\bright to work\b|\beligible to work\b",
         "authorized_without_sponsorship", r"\bsponsor"),
        (r"\blegally (allowed|able|eligible|entitled)", "authorized_without_sponsorship",
         r"\bsponsor"),
        (r"\bwork permit\b", "authorized_without_sponsorship", r"\bsponsor"),
        (r"\bcurrent (salary|compensation|pay)\b", "current_salary", None),
        (r"\b(salary|compensation|remuneration|pay expectation|expected pay)\b",
         "expected_salary", None),
        (r"\bnotice\b", "notice_period", None),
        (r"\bstart date\b|\bearliest start\b|\bwhen can you start\b|\bavailable to start\b",
         "earliest_start", None),
        (r"\bjoining date\b", "earliest_start", None),
        (r"\brelocat", "relocation", None),
        (r"\benglish\b", "english", None),
        (r"\bgender\b", "gender", None),
        (r"\b(city|town)\b|\bcurrent location\b|^location$|\bwhere are you (based|located)\b",
         "city", r"\b(relocat|prefer|willing|office|desired)"),
        (r"\bwhy (do you want|are you interested|this (company|role)|us|join)\b|\bmotivation\b",
         "why", None),
        (r"\bcover ?letter\b|\badditional information\b|\banything else\b"
         r"|\bmessage to (the )?hiring\b", "cover_letter", None),
    ))
CONSENT = re.compile(r"\b(consent|privacy|gdpr|terms|agree|policy|data protection|"
                     r"marketing|newsletter|talent (pool|community))\b")
RESUME = re.compile(r"\b(resume|cv|curriculum)\b")
STOP = {"the", "and", "you", "your", "are", "for", "with", "have", "what", "how", "many",
        "any", "this", "that", "our", "please", "did", "does", "will", "can"}
SOURCES = {"profile": "your profile and Apply pack", "saved": "your saved answer",
           "resume": "the approved resume for this job",
           "ai": "🤖 AI from your facts: check it"}


def norm(text: Any) -> str:
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(text or ""))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


@dataclass(frozen=True)
class Field:
    """One question on the form, as READ_JS found it."""

    ref: str  # data-je-field value of the element (radios: one per option, ref-k)
    kind: str  # text, email, tel, url, number, date, textarea, select, radio, checkbox, file
    label: str
    options: tuple[str, ...] = ()
    required: bool = False
    frame: int = 0  # index in page.frames (a form inside an iframe)


@dataclass
class Answer:
    field: Field
    value: str | None = None  # the text typed, or the option chosen
    option: int | None = None  # select and radio: the chosen option's index
    source: str = ""  # "profile", "saved" or "" (no answer)
    note: str = ""  # why it was left empty


@dataclass
class Result:
    url: str
    answers: list[Answer] = field(default_factory=list)
    stopped: str = ""  # captcha, login or an error: nothing was filled
    blocked: list[str] = field(default_factory=list)  # requests the page tried to send
    screenshot: Path | None = None
    title: str = ""


def _words(text: str) -> list[str]:
    return [w for w in norm(text).split() if len(w) > 2 and w not in STOP]


def saved_for(label: str, saved: Sequence[Mapping[str, str]]) -> str | None:
    """Your saved answer whose question words are (nearly) all in the label."""
    have = set(_words(label))
    best, best_score = None, 0.0
    for row in saved:
        want = _words(row.get("q") or "")
        if not want or not row.get("a"):
            continue
        hit = sum(1 for w in want if w in have)
        score = hit / len(want)
        if score >= 0.75 and (hit >= 2 or len(want) == 1) and score > best_score:
            best, best_score = row["a"], score
    return best


def rule_for(label: str, values: Mapping[str, str], kind: str) -> str | None:
    text = norm(label)
    for pattern, key, unless in RULES:
        if not values.get(key) or not pattern.search(text):
            continue
        if unless is not None and unless.search(text):
            continue
        if key == "cover_letter" and kind != "textarea":
            continue
        return values[key]
    return None


def choice(options: Sequence[str], value: str) -> int | None:
    """The option that matches the answer: the same text, then one starting with it, then
    Yes or No by the answer's first word ("No, I need sponsorship" picks "No")."""
    texts = [norm(o) for o in options]
    want = norm(value)
    for i, text in enumerate(texts):
        if text and text == want:
            return i
    for i, text in enumerate(texts):
        if len(want) > 2 and text.startswith(want):
            return i
    first = want.split(" ")[0] if want else ""
    if first in ("yes", "no"):
        for i, text in enumerate(texts):
            if text.split(" ")[0] == first:
                return i
    return None


def plan(fields: Sequence[Field], values: Mapping[str, str],
         saved: Sequence[Mapping[str, str]], resume: str | None = None) -> list[Answer]:
    """An answer for every field, or the reason it stays empty. Nothing is guessed.
    `resume` is the file name of the job's approved resume (attached to a resume field)."""
    out = []
    attached = False
    for f in fields:
        if f.kind == "file":
            if RESUME.search(norm(f.label)) and resume and not attached:
                out.append(Answer(f, value=resume, source="resume"))
                attached = True
                continue
            note = ("your resume: no approved resume for this job" if RESUME.search(
                norm(f.label)) else "a file: you attach it")
            out.append(Answer(f, note=note))
            continue
        if f.kind == "checkbox" and CONSENT.search(norm(f.label)):
            out.append(Answer(f, note="a consent box: you tick it yourself"))
            continue
        value, source = saved_for(f.label, saved), "saved"
        if value is None:
            value, source = rule_for(f.label, values, f.kind), "profile"
        if value is None:
            out.append(Answer(f, note="no answer yet"))
            continue
        if f.kind == "checkbox":
            yes = norm(value).split(" ")[0] == "yes"
            out.append(Answer(f, value="yes" if yes else "no (left unticked)",
                              option=0 if yes else None, source=source))
            continue
        if f.kind in ("select", "radio"):
            options = f.options
            index = choice(options, value)
            if index is None:
                out.append(Answer(f, value=value, source=source,
                                  note=f"no option matches \"{value}\""))
                continue
            out.append(Answer(f, value=options[index], option=index, source=source))
            continue
        out.append(Answer(f, value=value, source=source))
    return out


def filled(answers: Sequence[Answer]) -> list[Answer]:
    return [a for a in answers if a.value is not None and not a.note]


def report(result: Result, job: str = "") -> str:
    """The Telegram text: what was filled and from where, what is left for you."""
    head = f"🧪 Dry run{f' ({job})' if job else ''}: nothing was sent.\n{result.url}"
    if result.stopped:
        return f"{head}\nStopped: {result.stopped}"
    done = filled(result.answers)
    left = [a for a in result.answers if a not in done]
    lines = [head, f"Filled {len(done)} of {len(result.answers)} field(s). The screenshot "
             "shows the form as it would go."]
    if done:
        lines.append("")
        lines += [f"✅ {_short(a.field.label)}: {_short(a.value or '', 120)} "
                  f"({SOURCES.get(a.source, a.source)})" for a in done]
    if left:
        lines.append("")
        lines += [f"{'❗' if a.field.required else '❓'} {_short(a.field.label)}: {a.note}"
                  for a in left]
    open_questions = [a for a in left if a.note.startswith("no answer yet")]
    if open_questions:
        lines += ["", "Answer a question once and every later form gets it, in your words:",
                  f"/answer {_short(open_questions[0].field.label)} = <your answer>"]
    if result.blocked:
        lines += ["", f"🛡 Blocked {len(result.blocked)} request(s) the page tried to send "
                  "(nothing reaches the company in a dry run)."]
    return "\n".join(lines)


def _short(text: str, limit: int = 80) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


def prune(folder: Path = OUT_DIR, now: float | None = None, keep_days: int = KEEP_DAYS) -> int:
    """Delete screenshots older than keep_days. Returns how many were deleted."""
    if not folder.is_dir():
        return 0
    limit = (now if now is not None else time.time()) - keep_days * 86_400
    gone = 0
    for path in folder.glob("*.png"):
        try:
            if path.stat().st_mtime < limit:
                path.unlink()
                gone += 1
        except OSError as exc:
            log.warning("could not delete %s: %s", path, exc)
    return gone


# Runs in the page (and in each frame): lists the visible fields, marks each with
# data-je-field, and says whether a captcha or a login is in the way. It reads only.
READ_JS = r"""
() => {
  const shown = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const text = n => (n && (n.innerText || n.textContent) || '').replace(/\s+/g, ' ').trim();
  const labelOf = el => {
    const parts = [];
    if (el.labels) { for (const l of el.labels) { parts.push(text(l)); } }
    const by = el.getAttribute('aria-labelledby');
    if (by) { for (const id of by.split(/\s+/)) { parts.push(text(document.getElementById(id))); } }
    parts.push(el.getAttribute('aria-label') || '', el.getAttribute('placeholder') || '');
    let label = parts.filter(Boolean)[0] || '';
    if (!label || el.type === 'radio') {
      const box = el.closest('fieldset, [role=radiogroup], [role=group], .application-question, ' +
        '.field, [data-automation-id^=formField]');
      const legend = box && box.querySelector('legend, label, .application-label, .text');
      if (legend && (el.type === 'radio' || !label)) { label = text(legend); }
    }
    return label || el.name || el.id || '';
  };
  const blockers = [];
  if (document.querySelector('iframe[src*="recaptcha"], iframe[src*="hcaptcha"], ' +
    'iframe[src*="challenges.cloudflare"], .g-recaptcha, .h-captcha, .cf-turnstile')) {
    blockers.push('captcha');
  }
  if ([...document.querySelectorAll('input[type=password]')].some(shown)) {
    blockers.push('login');
  }
  const fields = [];
  const radios = new Map();
  let n = 0;
  for (const el of document.querySelectorAll('input, textarea, select')) {
    const type = (el.type || '').toLowerCase();
    if (el.disabled || /^(hidden|submit|button|reset|image|password|search)$/.test(type)) {
      continue;
    }
    if (type !== 'file' && !shown(el)) { continue; }
    const required = el.required || el.getAttribute('aria-required') === 'true';
    if (type === 'radio') {
      const key = el.name || ('je-radio-' + n);
      if (!radios.has(key)) {
        const ref = String(n++);
        radios.set(key, {ref: ref, kind: 'radio', label: labelOf(el), options: [],
          required: required});
        fields.push(radios.get(key));
      }
      const group = radios.get(key);
      el.setAttribute('data-je-field', group.ref + '-' + group.options.length);
      group.options.push(text(el.labels && el.labels[0]) || el.value || '');
      group.required = group.required || required;
      continue;
    }
    const ref = String(n++);
    el.setAttribute('data-je-field', ref);
    const kind = el.tagName === 'SELECT' ? 'select' : el.tagName === 'TEXTAREA' ? 'textarea' :
      (type || 'text');
    const options = kind === 'select' ? [...el.options].map(o => text(o)) : [];
    fields.push({ref: ref, kind: kind, label: labelOf(el), options: options, required: required});
  }
  return {fields: fields, blockers: blockers, title: document.title || ''};
}
"""


def _launch(playwright: Any) -> Any:
    path = os.environ.get(CHROMIUM_ENV) or None
    return playwright.chromium.launch(headless=True, executable_path=path)


def _guard(s: Settings, blocked: list[str]) -> Callable[[Any], None]:
    """Every request of the page: a plain GET to a public https host, else blocked."""

    def handle(route: Any) -> None:
        request = route.request
        url = request.url
        if url.startswith(("data:", "blob:", "about:")):
            route.continue_()
            return
        if request.method.upper() not in SAFE_METHODS:
            blocked.append(f"{request.method} {url}")
            route.abort()
            return
        try:
            safety.assert_page_fetch_allowed(url, s)
        except safety.SafetyError:
            blocked.append(f"{request.method} {url}")
            route.abort()
            return
        route.continue_()

    return handle


def _read(page: Any) -> tuple[list[Field], list[str], str]:
    """The fields of the frame with the most fields (a form inside an iframe counts)."""
    best: list[Field] = []
    blockers: set[str] = set()
    title = ""
    for index, frame in enumerate(page.frames):
        try:
            found = frame.evaluate(READ_JS)
        except Exception as exc:  # a frame from another site that went away
            log.info("dry run: frame %s not read: %s", index, exc)
            continue
        blockers.update(found.get("blockers") or [])
        if index == 0:
            title = found.get("title") or ""
        fields = [Field(ref=f["ref"], kind=f["kind"], label=f["label"],
                        options=tuple(f.get("options") or ()), required=bool(f.get("required")),
                        frame=index) for f in found.get("fields") or []]
        if len(fields) > len(best):
            best = fields
    return best, sorted(blockers), title


def _fill(page: Any, answers: Sequence[Answer], resume: Path | None = None) -> None:
    """Type, choose and attach; never a button, never Enter, never submit."""
    for a in filled(answers):
        f = a.field
        frame = page.frames[f.frame]
        try:
            if f.kind == "file":
                if resume is None:
                    continue
                frame.locator(f'[data-je-field="{f.ref}"]').set_input_files(str(resume),
                                                                            timeout=3_000)
            elif f.kind == "radio":
                frame.locator(f'[data-je-field="{f.ref}-{a.option}"]').check(timeout=3_000)
            elif f.kind == "checkbox":
                if a.option is None:  # your answer is no: it stays unticked
                    continue
                frame.locator(f'[data-je-field="{f.ref}"]').check(timeout=3_000)
            elif f.kind == "select":
                frame.locator(f'[data-je-field="{f.ref}"]').select_option(index=a.option,
                                                                          timeout=3_000)
            else:
                frame.locator(f'[data-je-field="{f.ref}"]').fill(a.value or "", timeout=3_000)
        except Exception as exc:  # one field that will not take a value: listed, go on
            log.info("dry run: %s not filled: %s", f.label, exc)
            a.note = "the form did not take the answer"


def dry_run(s: Settings, url: str, values: Mapping[str, str],
            saved: Sequence[Mapping[str, str]], *, name: str = "form",
            folder: Path = OUT_DIR, prepare: Callable[[Any], None] | None = None,
            check: Callable[[Any], None] | None = None,
            ai: Callable[[list[Answer]], int] | None = None,
            resume: tuple[str, bytes] | None = None,
            now: Callable[[], datetime] = datetime.now) -> Result:
    """Open the form, fill it, take a screenshot. `ai` answers the questions left open
    (apply/form_ai.py, step 2). `prepare` gets the browser context before the page opens and
    `check` the filled page (tests serve and read their own form)."""
    result = Result(url=url)
    try:
        safety.assert_page_fetch_allowed(url, s)
    except safety.SafetyError as exc:
        result.stopped = str(exc)
        return result
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        result.stopped = NO_PLAYWRIGHT
        return result
    prune(folder)
    resume_path = None
    if resume is not None:  # attached from a file with its own name; deleted after the run
        upload = folder / "upload" / f"{now():%Y%m%d-%H%M%S}-{os.getpid()}-{id(result)}"
        upload.mkdir(parents=True, exist_ok=True)
        resume_path = upload / (Path(resume[0]).name or "resume.pdf")
        resume_path.write_bytes(resume[1])
    try:
        return _run(s, url, values, saved, result, folder=folder, prepare=prepare, check=check,
                    ai=ai, resume_path=resume_path, name=name, now=now,
                    sync_playwright=sync_playwright)
    finally:
        if resume_path is not None:
            resume_path.unlink(missing_ok=True)
            resume_path.parent.rmdir()


def _run(s: Settings, url: str, values: Mapping[str, str], saved: Sequence[Mapping[str, str]],
         result: Result, *, folder: Path, prepare: Callable[[Any], None] | None,
         check: Callable[[Any], None] | None, ai: Callable[[list[Answer]], int] | None,
         resume_path: Path | None, name: str, now: Callable[[], datetime],
         sync_playwright: Any) -> Result:
    with sync_playwright() as p:
        browser = _launch(p)
        try:
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            context.route("**/*", _guard(s, result.blocked))
            if prepare is not None:
                prepare(context)
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=LOAD_TIMEOUT_MS)
            page.wait_for_timeout(SETTLE_MS)
            fields, blockers, result.title = _read(page)
            if blockers:
                result.stopped = (f"the page asks for a {' and a '.join(blockers)}. The bot does "
                                  "not go past it: apply on this one yourself.")
            elif not fields:
                result.stopped = ("no form fields found on this page (the form may be behind an "
                                  "Apply button or on another page: send its own link).")
            else:
                result.answers = plan(fields, values, saved,
                                      resume_path.name if resume_path else None)
                if ai is not None:
                    ai(result.answers)
                _fill(page, result.answers, resume_path)
                if check is not None:
                    check(page)
            folder.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "form"
            path = folder / f"{now():%Y%m%d-%H%M%S}-{safe}.png"
            page.screenshot(path=str(path), full_page=True)
            result.screenshot = path
        except Exception as exc:
            log.warning("dry run of %s failed: %s", url, exc)
            result.stopped = result.stopped or f"the page could not be read ({exc})"
        finally:
            browser.close()
    return result
