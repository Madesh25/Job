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

Auto-submit (step 5, 27 Oct, your OK) lives in apply/form_submit.py, never here: this module
fills and reads only. A run may get a `finish` step (form_submit.finisher) that runs after
the form is filled; only that step may open `Sending` (the guard then lets the page send to
public https hosts) and press the form's Submit button.

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
from urllib.parse import urlsplit

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
# 16 Nov: the corrections found on real forms (bookmark #330, #333) apply here too.
OTHER_PERSON = (r"\b(company|employer|refer\w*|their|connections?|contact person|reference|"
                r"emergency|manager|recruiter)\b")
NOT_SPONSOR = (r"\bauthori[sz]|\beligib|\bright to work\b|\blegally\b|\bpermitted to work\b|"
               r"\bwithout (a |any |visa )*sponsor")
NEEDS_SPONSOR = r"\b(requir|need)\w*\b.*\bsponsor"
NOT_PAY = r"\b(currency|bonus|deferred|claw ?back|equity|stock|commission)\b"
RULES: tuple[tuple[re.Pattern[str], str, re.Pattern[str] | None], ...] = tuple(
    (re.compile(p), key, re.compile(n) if n else None) for p, key, n in (
        (r"\b(first|given) ?name\b|\bforename\b", "first_name", OTHER_PERSON),
        (r"\b(last|family) ?name\b|\bsurname\b", "last_name", OTHER_PERSON),
        (r"^(full |your |legal )?name$|\bfull name\b", "full_name", OTHER_PERSON),
        (r"\be ?mail\b", "email", None),
        (r"\b(phone|mobile|telephone)\b", "phone", r"\b(type|code|extension|device)\b"),
        (r"\blinked ?in\b", "linkedin", None),
        (r"\bgit ?hub\b", "github", None),
        (r"\b(website|portfolio|personal (site|page)|blog)\b", "website",
         r"\b(linked ?in|git ?hub)\b"),
        (r"\bsponsor", "sponsorship_needed", NOT_SPONSOR),
        (r"\bauthori[sz]|\bright to work\b|\beligible to work\b|\b(allowed|permitted) to work\b",
         "authorized_without_sponsorship", NEEDS_SPONSOR),
        (r"\blegally (allowed|able|eligible|entitled)", "authorized_without_sponsorship",
         NEEDS_SPONSOR),
        (r"\bwork permit\b", "authorized_without_sponsorship", NEEDS_SPONSOR),
        # 16 Nov, real Greenhouse form (Qube): "Base Salary Currency" and "deferred
        # compensation or claw back" are not a salary amount.
        (r"\bcurrent (salary|compensation|pay)\b", "current_salary", NOT_PAY),
        (r"\b(salary|compensation|remuneration|pay expectation|expected pay)\b",
         "expected_salary", NOT_PAY),
        (r"\bnotice\b", "notice_period",
         r"\b(privacy|data protection|policy|information notice)\b"),
        (r"\bstart date\b|\bearliest start\b|\bwhen can you start\b|\bavailable to start\b",
         "earliest_start", None),
        (r"\bavailable from\b|\bwhen are you available\b", "earliest_start", None),
        (r"\bjoining date\b", "earliest_start", None),
        (r"\brelocat", "relocation", None),
        (r"\benglish\b", "english", None),
        (r"\bgender\b", "gender", None),
        (r"\b(city|town)\b|\bcurrent location\b|^(your )?location$"
         r"|\bwhere are you (currently )?based\b",
         "city", r"\b(relocat|prefer|willing|office|desired)"),
        (r"\bwhere are you (currently )?located\b", "city", r"\b(relocat|prefer|willing|office)"),
        (r"\bwhy (do you want|are you interested|this (company|role)|us|join)\b|\bmotivat",
         "why", None),
        (r"\bwhy you (are|would be) interested\b|\bwhy are you applying\b", "why", None),
        (r"\bcover ?letter\b|\badditional information\b|\banything else\b"
         r"|\bmessage to (the )?hiring\b", "cover_letter", None),
    ))
CONSENT = re.compile(r"\b(consent|privacy|gdpr|terms|agree|policy|data protection|"
                     r"marketing|newsletter|talent (pool|community))\b")
RESUME = re.compile(r"\b(resume|cv|curriculum)\b")
AUTOFILL_UPLOAD = re.compile(r"\b(autofill|auto fill|import|parse|prefill|pre fill)\b")
STOP = {"the", "and", "you", "your", "are", "for", "with", "have", "what", "how", "many",
        "any", "this", "that", "our", "please", "did", "does", "will", "can"}
SOURCES = {"profile": "your profile and Apply pack", "saved": "your saved answer",
           "form": "shown on the form already",
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
    hint: str = ""  # the field's own name, id and autocomplete ("first_name", "resume")


def words_of(f: Field) -> str:
    """The label and the field's own name: 16 Nov, real forms asked "First", "Last" (Personio)
    and "Attach" (Greenhouse's resume) with names first_name, last_name, resume."""
    return norm(f"{f.label} {f.hint}")


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
    opened: str = ""  # the Apply link the dry run followed from the job's page (3 Nov)
    # Auto-submit only (apply/form_submit.py): "" not tried, "confirmed", "unclear", or
    # "not submitted: <why>".
    submitted: str = ""


@dataclass
class Sending:
    """Closed in every dry run. Only apply/form_submit.py opens it, right before it presses
    Submit on a form that passed every check."""

    on: bool = False


JOB_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\d{5,}", re.I)


def closed(asked: str, opened: str) -> bool:
    """The job's link now opens another page: the job's own id is gone from the address
    (16 Nov, real Greenhouse jobs: a closed job goes to "<board>?error=true", the company's
    job list)."""
    ids = JOB_ID.findall(urlsplit(asked).path)
    return bool(ids) and not any(i.lower() in opened.lower() for i in ids)


def _words(text: str) -> list[str]:
    return [w for w in norm(text).split() if len(w) > 2 and w not in STOP]


def saved_for(label: str, saved: Sequence[Mapping[str, str]]) -> str | None:
    """Your saved answer whose question words are (nearly) all in the label. A one-word
    question ("School", "Degree") only answers a short label (17 Nov: "Degree" must not
    answer "What is your expected degree of travel?")."""
    have = set(_words(label))
    best, best_score = None, 0.0
    for row in saved:
        want = _words(row.get("q") or "")
        if not want or not row.get("a"):
            continue
        hit = sum(1 for w in want if w in have)
        score = hit / len(want)
        if score >= 0.75 and (hit >= 2 or (len(want) == 1 and len(have) <= 2)) \
                and score > best_score:
            best, best_score = row["a"], score
    return best


def rule_for(label: str, values: Mapping[str, str], kind: str) -> str | None:
    # 16 Nov, real Personio form: "Location* (required)" missed the "location" rule.
    text = re.sub(r"( required)+$", "", norm(label))
    for pattern, key, unless in RULES:
        if not values.get(key) or not pattern.search(text):
            continue
        if unless is not None and unless.search(text):
            continue
        if key == "cover_letter" and kind != "textarea":
            continue
        return values[key]
    return None


NEEDS_PERMIT = re.compile(r"\b(need|require)\w*\b.*\b(permit|sponsor\w*|visa)\b|^not yet\b")


def permit_choice(label: str, options: Sequence[str], value: str) -> int | None:
    """16 Nov, real Personio form (UbiOps): "Allowed to work?" offers statements ("Yes, I am
    a NL/EU citizen", "Not yet, I need a residence permit for highly skilled migrant"), not
    Yes or No. Your answer "No" (not allowed without sponsorship) is the one option that says
    a permit or sponsorship is still needed; none or more than one: no choice."""
    text = norm(label)
    key = next((k for pattern, k, unless in RULES if pattern.search(text)
                and not (unless is not None and unless.search(text))), None)
    first = (norm(value).split(" ") or [""])[0]
    if not ((key == "authorized_without_sponsorship" and first == "no")
            or (key == "sponsorship_needed" and first == "yes")):
        return None
    hits = [i for i, option in enumerate(options) if NEEDS_PERMIT.search(norm(option))]
    return hits[0] if len(hits) == 1 else None


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
    two = " ".join(want.split(" ")[:2])
    if " " in two and len(two) > 5:  # "Prefer not to say" picks "Prefer not to answer"
        for i, text in enumerate(texts):
            if text.startswith(two):
                return i
    words = set(want.split(" "))
    if len(words) > 1:  # "Chennai, India" picks "Chennai, Tamil Nadu, India" (16 Nov)
        for i, text in enumerate(texts):
            if words <= set(text.split(" ")):
                return i
    return None


def plan(fields: Sequence[Field], values: Mapping[str, str],
         saved: Sequence[Mapping[str, str]], resume: str | None = None) -> list[Answer]:
    """An answer for every field, or the reason it stays empty. Nothing is guessed.
    `resume` is the file name of the job's approved resume (attached to a resume field)."""
    out = []
    attached = False
    # 16 Nov, real Ashby forms: "Autofill from resume" is an upload that reads a CV to fill
    # the form, not the resume field; it gets the resume only when the form has no other.
    real = [f for f in fields if f.kind == "file" and RESUME.search(words_of(f))
            and not AUTOFILL_UPLOAD.search(words_of(f))]
    for f in fields:
        if f.kind == "file":
            if (RESUME.search(words_of(f)) and resume and not attached
                    and (f in real or not real)):
                out.append(Answer(f, value=resume, source="resume"))
                attached = True
                continue
            if not RESUME.search(words_of(f)):
                note = "a file: you attach it"
            elif not resume:
                note = "your resume: no approved resume for this job"
            elif real and f not in real:  # 16 Nov: was "no approved resume" on Ashby forms
                note = "an upload that fills the form from a resume: left empty (the resume " \
                       "goes to the resume field)"
            else:
                note = "left empty: the resume is attached once"
            out.append(Answer(f, note=note))
            continue
        if f.kind == "checkbox" and CONSENT.search(norm(f.label)):
            out.append(Answer(f, note="a consent box: you tick it yourself"))
            continue
        if f.kind == "combobox" and f.options:
            out.append(Answer(f, value=f.options[0], source="form"))
            continue
        value, source = saved_for(f.label, saved), "saved"
        if value is None:
            value, source = rule_for(f.label, values, f.kind), "profile"
        if value is None and f.hint and not _words(f.label)[1:]:
            # A one-word label ("First", "Last"): the field's own name says what it asks.
            value = rule_for(norm(f.hint), values, f.kind)
        if value is None:
            out.append(Answer(f, note="no answer yet"))
            continue
        if f.kind == "number" and not re.search(r"\d", value):
            # 16 Nov, real Teamtailor form: a salary box that takes only a number got the
            # words of your salary answer, and "the form did not take the answer".
            out.append(Answer(f, note=f"no answer yet (a number field: \"{_short(value, 40)}\" "
                                      "has no number)"))
            continue
        if f.kind == "checkbox":
            yes = norm(value).split(" ")[0] == "yes"
            out.append(Answer(f, value="yes" if yes else "no (left unticked)",
                              option=0 if yes else None, source=source))
            continue
        if f.kind in ("select", "radio"):
            options = f.options
            index = choice(options, value)
            if index is None and source == "profile":
                index = permit_choice(f.label, options, value)
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
    if result.opened:
        head += f"\nOpened its Apply link: {result.opened}"
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
                  f"/answer {_short(question(open_questions[0].field.label))} = <your answer>"]
    if result.blocked:
        lines += ["", f"🛡 Blocked {len(result.blocked)} request(s) the page tried to send "
                  "(nothing reaches the company in a dry run)."]
    return "\n".join(lines)


def question(label: str) -> str:
    """A label as a question for /answer, without the form's required marks (16 Nov:
    "/answer School* = ..." and "Phone*Required")."""
    text = re.sub(r"\s*(\*\s*)?(\(required\)|required)?\s*$", "", " ".join(label.split()),
                  flags=re.I)
    return text.rstrip("* ").strip() or label


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
  // Phase 19 (3 Nov): a field hidden with CSS (class "invisible", visibility, opacity) is not
  // one you can fill, even when it takes up room on the page.
  const shown = el => {
    if (!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)) { return false; }
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      const st = getComputedStyle(n);
      if (st.display === 'none' || st.visibility === 'hidden' || st.visibility === 'collapse' ||
          st.opacity === '0') { return false; }
    }
    return true;
  };
  // The site's own job search, menus and header are not the application form.
  const siteSearch = el => !!el.closest('[role=search], nav, header, footer') ||
    // 16 Nov: "search" as a word of the address, not inside a name ("quberesearch...").
    (el.form && /(^|[\/?&=_.-])(search|zoek|zoeken|szukaj|suche)($|[\/?&=_.-])/i.test(
      el.form.getAttribute('action') || '')) ||
    /search (for )?jobs?|search job|keyword/i.test(el.getAttribute('aria-label') || '') ||
    /search (for )?jobs?|search job|keyword/i.test(el.getAttribute('placeholder') || '');
  const text = n => (n && (n.innerText || n.textContent) || '').replace(/\s+/g, ' ').trim();
  const labelOf = el => {
    const parts = [];
    if (el.labels) { for (const l of el.labels) { parts.push(text(l)); } }
    const by = el.getAttribute('aria-labelledby');
    if (by) { for (const id of by.split(/\s+/)) { parts.push(text(document.getElementById(id))); } }
    parts.push(el.getAttribute('aria-label') || '');
    let label = parts.filter(Boolean)[0] || '';
    if (!label || el.type === 'radio') {
      // 16 Nov, real Ashby form: "Location" is the label of the field's box, the field itself
      // only has the hint "Start typing...".
      const box = el.closest('fieldset, [role=radiogroup], [role=group], .application-question, ' +
        '.field, [class*=fieldEntry], [class*=field-entry], [data-automation-id^=formField]');
      const many = box && box.querySelectorAll('input:not([type=hidden]), textarea, select')
        .length > 2 && el.type !== 'radio';
      const legend = box && !many && box.querySelector('legend, label, .application-label, .text');
      if (legend && text(legend)) { label = text(legend); }
    }
    if (!label) { label = el.getAttribute('placeholder') || ''; }
    if (!label && el.type === 'file') {  // an upload with no name: the first line of its box
      const near = el.closest('div');
      const lines = near ? (near.innerText || '').split('\n').map(s => s.trim()).filter(Boolean)
        : [];
      label = (lines[0] || '').slice(0, 60);
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
  // A sign-in page that asks for the email first (the password comes on the next step).
  const heading = text(document.querySelector('h1')) + ' ' + text(document.querySelector('h2'));
  if (!blockers.includes('login') && /\b(log ?in|sign ?in|create (an |your )?account)\b/i.test(
      (document.title || '') + ' ' + heading) &&
      document.querySelectorAll('input:not([type=hidden])').length <= 3) {
    blockers.push('login');
  }
  // "Apply" links (never pressed: the dry run opens the link's address, a plain GET).
  const applyLinks = [];
  for (const a of document.querySelectorAll('a[href]')) {
    const words = text(a) || a.getAttribute('aria-label') || '';
    const apply = new RegExp('^\\s*(apply( now| for this (job|role|position)| here| online)?|' +
      'application|aplikuj( teraz)?|solliciteer( nu| direct)?|bewerben|jetzt bewerben|' +
      'postuler)\\s*$', 'i');
    if (shown(a) && apply.test(words) && /^https?:/i.test(a.href)) {
      applyLinks.push(a.href);
    }
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
    if (siteSearch(el)) { continue; }
    const label = labelOf(el);
    const hint = [el.name, el.id, el.getAttribute('autocomplete')].filter(Boolean).join(' ');
    // 16 Nov, real forms: Personio and Teamtailor mark required questions only in the text
    // ("Email* (required)", "Phone*Required").
    const required = el.required || el.getAttribute('aria-required') === 'true' ||
      /\(required\)|\*\s*(required)?\s*$/i.test(label);
    if (type === 'radio') {
      const key = el.name || ('je-radio-' + n);
      if (!radios.has(key)) {
        const ref = String(n++);
        radios.set(key, {ref: ref, kind: 'radio', label: label, options: [],
          required: required, hint: hint});
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
    const combo = el.getAttribute('role') === 'combobox' ||
      el.getAttribute('aria-autocomplete') === 'list';
    const kind = el.tagName === 'SELECT' ? 'select' : el.tagName === 'TEXTAREA' ? 'textarea' :
      combo ? 'combobox' : (type || 'text');
    let options = kind === 'select' ? [...el.options].map(o => text(o)) : [];
    if (combo) {  // the answer it shows already (chosen by you, or set from the phone number)
      for (let p = el.parentElement, d = 0; p && d < 6; p = p.parentElement, d++) {
        if (p.querySelectorAll('[role=combobox]').length > 1) { break; }
        const shownValue = p.querySelector('[class*=single-value], [class*=singleValue]');
        if (shownValue) { options = text(shownValue) ? [text(shownValue)] : []; break; }
      }
    }
    fields.push({ref: ref, kind: kind, label: label, options: options, required: required,
      hint: hint});
  }
  return {fields: fields, blockers: blockers, title: document.title || '',
          applyLinks: applyLinks};
}
"""


# A custom dropdown (16 Nov, real forms: Greenhouse, Ashby): a text box whose answer is one
# of a list that shows while you type. Typed text alone is cleared when the box loses focus,
# so the bot chooses the matching option, the way it ticks a radio button. The one exception
# to "no click" in this module (tests/test_repo_rules.py): only an element with
# role="option" of this dropdown's own list; never a button, never a key, never Submit.
# Greenhouse opens its list on key-up only, so a key-up event is sent to the text box.
CHOOSE_JS = r"""
async ({ref, value}) => {
  const el = document.querySelector('[data-je-field="' + ref + '"]');
  if (!el) { return null; }
  const norm = s => String(s || '').replace(/([a-z])([A-Z])/g, '$1 $2').toLowerCase()
    .replace(/[^a-z0-9]+/g, ' ').trim();
  const shown = n => !!(n.offsetWidth || n.offsetHeight || n.getClientRects().length);
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const setText = v => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, v);
    el.dispatchEvent(new Event('input', {bubbles: true}));
  };
  const options = () => {
    let opts = [];
    const ids = [el.getAttribute('aria-controls'), el.getAttribute('aria-owns')]
      .filter(Boolean).join(' ').split(/\s+/).filter(Boolean);
    for (const id of ids) {
      const list = document.getElementById(id);
      if (list) { opts = opts.concat([...list.querySelectorAll('[role=option]')]); }
    }
    if (!opts.length && el.id) {
      opts = [...document.querySelectorAll('[role=option]')]
        .filter(o => o.id.startsWith('react-select-' + el.id + '-option'));
    }
    if (!opts.length) {
      opts = [...document.querySelectorAll('[role=listbox] [role=option]')]
        .filter(o => !o.closest('.iti') && !o.hasAttribute('data-dial-code'));
    }
    return opts.filter(o => o.getAttribute('role') === 'option' && shown(o));
  };
  const choice = (texts, v) => {
    const want = norm(v);
    const first = want.split(' ')[0];
    let i = texts.findIndex(t => t === want);
    if (i < 0) { i = texts.findIndex(t => want.length > 2 && t.startsWith(want)); }
    if (i < 0 && /^(yes|no)$/.test(first)) { i = texts.findIndex(t => t.split(' ')[0] === first); }
    const two = want.split(' ').slice(0, 2).join(' ');
    if (i < 0 && two.includes(' ') && two.length > 5) {
      i = texts.findIndex(t => t.startsWith(two));
    }
    const words = want.split(' ');
    if (i < 0 && words.length > 1) {
      i = texts.findIndex(t => words.every(w => t.split(' ').includes(w)));
    }
    return i;
  };
  el.focus();
  setText(value);
  el.dispatchEvent(new KeyboardEvent('keyup', {key: 'Unidentified', bubbles: true}));
  for (let n = 0; n < 12; n++) {
    await sleep(150);
    const opts = options();
    if (!opts.length) { continue; }
    const i = choice(opts.map(o => norm(o.innerText)), value);
    if (i < 0) { break; }
    const chosen = opts[i].innerText.trim();
    opts[i].click();
    await sleep(200);
    return chosen;
  }
  setText('');
  return null;
}
"""


# The answer a custom dropdown shows (reads only): Greenhouse sets "Country" from the phone
# number once the phone is typed, after the form was read (16 Nov).
SHOWN_JS = r"""
(ref) => {
  const el = document.querySelector('[data-je-field="' + ref + '"]');
  for (let p = el && el.parentElement, d = 0; p && d < 6; p = p.parentElement, d++) {
    if (p.querySelectorAll('[role=combobox]').length > 1) { break; }
    const v = p.querySelector('[class*=single-value], [class*=singleValue]');
    if (v) { return (v.innerText || '').trim(); }
  }
  return '';
}
"""


def _launch(playwright: Any) -> Any:
    path = os.environ.get(CHROMIUM_ENV) or None
    return playwright.chromium.launch(headless=True, executable_path=path)


# Requests that only read the job and build the form, though they are POSTs (16 Nov, real
# forms: Ashby showed no form while they were blocked). Exactly these, nothing else: they are
# sent when the page opens, before anything is filled, and carry none of your answers.
READ_ONLY_POSTS = frozenset({
    "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting",
    "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiOrganizationFromHostedJobsPageName",
})


def _guard(s: Settings, blocked: list[str],
           sending: Sending | None = None) -> Callable[[Any], None]:
    """Every request of the page: a plain GET to a public https host, else blocked. While
    `sending` is open (auto-submit only) the page may also send to public https hosts."""

    def handle(route: Any) -> None:
        request = route.request
        url = request.url
        if url.startswith(("data:", "blob:", "about:")):
            route.continue_()
            return
        if (request.method.upper() not in SAFE_METHODS and url not in READ_ONLY_POSTS
                and not (sending and sending.on)):
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


# What only an application form asks (Phase 19, 3 Nov): a page without these is a job
# description, a search page or a menu, not the form.
NAME_FIELD = re.compile(r"\b(first|last|full|given|family|sur) ?name\b|^name$|\bimi[eę]\b|"
                        r"\bnazwisko\b|\bvoornaam\b|\bachternaam\b")
CONTACT_FIELD = re.compile(r"\be ?mail\b|\bphone\b|\bmobile\b")
PAPERS_FIELD = re.compile(r"\bresume\b|\bcv\b|\bcover ?letter\b|\blinked ?in\b")


def application_form(fields: Sequence[Field]) -> bool:
    """A form that asks for you: a name, or a way to reach you together with your papers or
    a few more questions. 16 Nov, real forms: a job page's hidden upload field (Recruitee)
    and its job-alert email box (Teamtailor) were taken for the application form, so the
    bot never opened the real one."""
    seen = [f for f in fields if f.kind != "file"]
    if any(NAME_FIELD.search(words_of(f)) for f in seen):
        return True
    contact = any(f.kind in ("email", "tel") or CONTACT_FIELD.search(words_of(f))
                  for f in seen)
    papers = any(PAPERS_FIELD.search(norm(f.label)) for f in seen)  # upload fields hide
    return contact and (papers or len(seen) >= 3)


# A job board's own application page (16 Nov, real forms): Recruitee and Teamtailor open
# their form with a button the bot never presses, but the form has its own address.
FORM_PAGES = (
    (re.compile(r"^(https://[^/]+\.recruitee\.com/o/[^/?#]+)/?(?:[?#].*)?$"), r"\1/c/new"),
    (re.compile(r"^(https://[^/]+\.teamtailor\.com/(?:[a-z]{2}/)?jobs/[^/?#]+)/?(?:[?#].*)?$"),
     r"\1/applications/new"),
    (re.compile(r"^(https://jobs\.ashbyhq\.com/[^/]+/[0-9a-f-]{36})/?(?:[?#].*)?$"),
     r"\1/application"),
    (re.compile(r"^(https://jobs\.(?:eu\.)?lever\.co/[^/]+/[0-9a-f-]{36})/?(?:[?#].*)?$"),
     r"\1/apply"),
    (re.compile(r"^(https://apply\.workable\.com/[^/]+/j/[0-9A-F]+)/?(?:[?#].*)?$"), r"\1/apply/"),
)
READ_MORE = 6  # a form built by JavaScript after the page loads (Ashby): wait up to 6 s more


def form_page(url: str) -> str | None:
    """The application form's own address for a job page on a known board, else None."""
    for pattern, form in FORM_PAGES:
        if pattern.match(url):
            return pattern.sub(form, url)
    return None


def _read_settled(page: Any, links: list[str] | None = None
                  ) -> tuple[list[Field], list[str], str]:
    """_read, waiting a little more while the page shows no field yet (Ashby builds its form
    a few seconds after the page loads)."""
    found = _read(page, links)
    for _ in range(READ_MORE):
        if found[0] or found[1]:
            break
        page.wait_for_timeout(1_000)
        found = _read(page, links)
    return found


def _read(page: Any, links: list[str] | None = None) -> tuple[list[Field], list[str], str]:
    """The fields of the frame with the most fields (a form inside an iframe counts).
    `links` gets the page's "Apply" link addresses."""
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
        if links is not None:
            links += [str(u) for u in found.get("applyLinks") or [] if u not in links]
        if index == 0:
            title = found.get("title") or ""
        fields = [Field(ref=f["ref"], kind=f["kind"], label=f["label"],
                        options=tuple(f.get("options") or ()), required=bool(f.get("required")),
                        frame=index, hint=str(f.get("hint") or ""))
                  for f in found.get("fields") or []]
        if len(fields) > len(best):
            best = fields
    return best, sorted(blockers), title


def _tick(box: Any) -> None:
    """Tick a radio button or a checkbox. 16 Nov, real Recruitee form: its real radio buttons
    are 1 px boxes clipped to nothing under styled ones, so a normal tick waits for a visible
    box that never comes. Its own label is ticked instead (the browser ticks the box the label
    names), then the box itself without that wait. A tick, never a button."""
    try:
        box.check(timeout=3_000)
        return
    except Exception:
        pass
    ident = (box.get_attribute("id", timeout=3_000) or "").replace("'", "")
    label = box.locator(f"xpath=(ancestor::label | ancestor::*[last()]//label[@for='{ident}'])"
                        if ident else "xpath=ancestor::label").first
    try:
        label.check(timeout=3_000)
    except Exception:
        box.check(force=True, timeout=3_000)
    if not box.is_checked():
        raise RuntimeError("the box stayed unticked")


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
                _tick(frame.locator(f'[data-je-field="{f.ref}-{a.option}"]'))
            elif f.kind == "checkbox":
                if a.option is None:  # your answer is no: it stays unticked
                    continue
                _tick(frame.locator(f'[data-je-field="{f.ref}"]'))
            elif f.kind == "select":
                frame.locator(f'[data-je-field="{f.ref}"]').select_option(index=a.option,
                                                                          timeout=3_000)
            elif f.kind == "combobox":
                if a.source == "form":
                    continue  # it shows an answer already: left as it is
                chosen = frame.evaluate(CHOOSE_JS, {"ref": f.ref, "value": a.value or ""})
                if chosen:
                    a.value = str(chosen)
                else:  # 16 Nov: typed text alone is lost; it is not an answer
                    a.note = f'no option matches "{a.value}"'
            else:
                frame.locator(f'[data-je-field="{f.ref}"]').fill(a.value or "", timeout=3_000)
        except Exception as exc:  # one field that will not take a value: listed, go on
            log.info("dry run: %s not filled: %s", f.label, exc)
            a.note = "the form did not take the answer"
    for a in answers:  # a dropdown the form filled by itself meanwhile (Country from the phone)
        if a.field.kind == "combobox" and a.value is None:
            try:
                shown = page.frames[a.field.frame].evaluate(SHOWN_JS, a.field.ref)
            except Exception:
                continue
            if shown:
                a.value, a.source, a.note = str(shown), "form", ""


def dry_run(s: Settings, url: str, values: Mapping[str, str],
            saved: Sequence[Mapping[str, str]], *, name: str = "form",
            folder: Path = OUT_DIR, prepare: Callable[[Any], None] | None = None,
            check: Callable[[Any], None] | None = None,
            ai: Callable[[list[Answer]], int] | None = None,
            resume: tuple[str, bytes] | None = None,
            finish: Callable[[Any, Result, Sending], None] | None = None,
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
                    sync_playwright=sync_playwright, finish=finish)
    finally:
        if resume_path is not None:
            resume_path.unlink(missing_ok=True)
            resume_path.parent.rmdir()


def _run(s: Settings, url: str, values: Mapping[str, str], saved: Sequence[Mapping[str, str]],
         result: Result, *, folder: Path, prepare: Callable[[Any], None] | None,
         check: Callable[[Any], None] | None, ai: Callable[[list[Answer]], int] | None,
         resume_path: Path | None, name: str, now: Callable[[], datetime],
         sync_playwright: Any,
         finish: Callable[[Any, Result, Sending], None] | None = None) -> Result:
    sending = Sending()
    with sync_playwright() as p:
        browser = _launch(p)
        try:
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            context.route("**/*", _guard(s, result.blocked, sending))
            if prepare is not None:
                prepare(context)
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=LOAD_TIMEOUT_MS)
            page.wait_for_timeout(SETTLE_MS)
            links: list[str] = []
            fields, blockers, result.title = _read_settled(page, links)
            landed = page.url
            known = form_page(page.url)
            if known and known not in links:
                links.append(known)  # 16 Nov: its own form page, when no Apply link leads there
            if not blockers and not application_form(fields) and links:
                # The job's description page (Phase 19, 3 Nov): open its Apply link (a plain
                # GET, nothing pressed) and read the form there.
                target = links[0]
                try:
                    safety.assert_page_fetch_allowed(target, s)
                    page.goto(target, wait_until="domcontentloaded", timeout=LOAD_TIMEOUT_MS)
                    page.wait_for_timeout(SETTLE_MS)
                    result.opened = target
                    fields, blockers, result.title = _read_settled(page)
                except safety.SafetyError as exc:
                    log.info("dry run: apply link %s not opened: %s", target, exc)
            if blockers:
                result.stopped = (f"the page asks for a {' and a '.join(blockers)}. The bot does "
                                  "not go past it: apply on this one yourself.")
            elif not application_form(fields) and closed(url, landed):
                result.stopped = (f"the job looks closed: its link now opens {landed} (not "
                                  "this job's page). Nothing to fill.")
            elif not fields:
                result.stopped = ("no form fields found on this page (the form may be behind an "
                                  "Apply button or on another page: send its own link).")
            elif not application_form(fields):
                names = ", ".join(f"\"{_short(f.label, 40)}\"" for f in fields[:3])
                result.stopped = (f"this page has no application form: its fields ({names}) are "
                                  "not ones a job form asks (no name, email, phone or resume). "
                                  "It may be the job description: send the form's own link, "
                                  "/dryrun <job> <form link>.")
            else:
                result.answers = plan(fields, values, saved,
                                      resume_path.name if resume_path else None)
                if ai is not None:
                    ai(result.answers)
                _fill(page, result.answers, resume_path)
                if check is not None:
                    check(page)
                if finish is not None:  # auto-submit (apply/form_submit.py); never a dry run
                    finish(page, result, sending)
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
