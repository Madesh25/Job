"""Form autofill for company application forms (flow feature 9, 9 Oct).

The bot never opens a company form and never submits anything. Each Apply pack ends with an
autofill code: your answers for this job (name, email, phone, links, permit, salary, notice,
the cover letter and your /answer list) packed into one line that starts with CODE_PREFIX.
On the form page (Greenhouse, Lever, Workday and most others) you click the "Job Engine
autofill" bookmark once; it asks for the code and fills the empty fields whose label it
recognises, marks them in orange, and tells you what is still empty. You check every field,
attach the resume and press Submit yourself. /autofill sends the bookmark and how to add it.

The bookmark is plain JavaScript (AUTOFILL_JS below) that runs only in your own browser on
the page you are on: no AI, no network call, no click on any button. Nothing reaches the
site until you press Submit. (The /dryrun form bot, apply/form_bot.py, opens a form in a
browser for a dry run: it fills it and still never submits.)
"""

from __future__ import annotations

import base64
import html as htmllib
import json
import zlib
from typing import Any
from urllib.parse import quote

from jobengine.apply.answers import for_country, saved_salary
from jobengine.apply.answers import items as saved_answers
from jobengine.apply.pack import MISSING, answer
from jobengine.config_store import ConfigStore
from jobengine.settings import Settings

CODE_PREFIX = "JE1:"
MAX_CODE = 3600  # one Telegram message (4096) with the line before it
PROFILE = {"email": "profile.email", "phone": "profile.phone",
           "linkedin": "profile.linkedin_url", "github": "profile.github",
           "website": "profile.website_url"}
ANSWERS = ("city", "sponsorship_needed",
           "authorized_without_sponsorship", "expected_salary", "current_salary",
           "notice_period", "earliest_start", "relocation", "english", "gender")
HOW = ("Autofill: on the form page click your \"Job Engine autofill\" bookmark and paste this "
       "whole message. It fills the empty fields it knows, marks them in orange and never "
       "presses Submit. No bookmark yet? Send /autofill.")


def fields(s: Settings, config: ConfigStore, values: dict[str, Any], *,
           letter: str | None = None, why: str | None = None,
           salary: str | None = None) -> dict[str, str]:
    """Your answers for one job's form, by field name. A value that is not set is left out,
    so the form field stays empty for you to fill (nothing is guessed)."""
    out: dict[str, str] = {}
    full = " ".join(str(config.get("profile.name") or "").split())
    first, _, last = full.partition(" ")
    first = str(config.get("apply.first_name") or first)
    last = str(config.get("apply.last_name") or last)
    if config.get("apply.first_name") and config.get("apply.last_name"):
        # "Madeshwaran Manikam" on a form, not the short "Madeshwaran M" (2 Nov)
        full = f"{first} {last}"
    for key, value in (("full_name", full), ("first_name", first), ("last_name", last)):
        if value:
            out[key] = " ".join(str(value).split())
    for key in ANSWERS:
        value = answer(s, config, key)
        if value != MISSING.format(key=key):
            out[key] = value
    if salary:  # your saved amount for this job's country wins (Phase 23 4-04)
        out["expected_salary"] = " ".join(salary.split())
    for name, key in PROFILE.items():
        if config.get(key):
            out[name] = " ".join(str(config.get(key)).split())
    if why:
        out["why"] = why
    if letter:
        out["cover_letter"] = letter
    return out


def encode(data: dict[str, Any]) -> str:
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    packed = base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode("ascii").rstrip("=")
    return CODE_PREFIX + packed


def decode(code: str) -> dict[str, Any]:
    """The other way (tests and support); the bookmark does the same in the browser."""
    packed = code.strip().split(CODE_PREFIX, 1)[-1].split()[0]
    packed += "=" * (-len(packed) % 4)
    return json.loads(zlib.decompress(base64.urlsafe_b64decode(packed)).decode("utf-8"))


def code(s: Settings, config: ConfigStore, state: Any, values: dict[str, Any], *,
         letter: str | None = None, why: str | None = None) -> str:
    """The code for one job, at most MAX_CODE characters: the oldest saved answers go first
    when it is too long, then the cover letter (it is in its own message anyway)."""
    data: dict[str, Any] = {
        "job": f"{values.get('Company') or ''}, {values.get('Role') or ''}".strip(", "),
        "fields": fields(s, config, values, letter=letter, why=why,
                         salary=saved_salary(state, values.get("Country"))),
        "answers": [[r["q"], r["a"]] for r in for_country(saved_answers(state),
                                                          values.get("Country"))],
    }
    text = encode(data)
    while len(text) > MAX_CODE and data["answers"]:
        data["answers"] = data["answers"][1:]
        text = encode(data)
    if len(text) > MAX_CODE:
        data["fields"].pop("cover_letter", None)
        text = encode(data)
    return text


def message(s: Settings, config: ConfigStore, state: Any, values: dict[str, Any], *,
            letter: str | None = None, why: str | None = None) -> str:
    return f"{HOW}\n{code(s, config, state, values, letter=letter, why=why)}"


def bookmarklet() -> str:
    """The bookmark's address: javascript:void plus AUTOFILL_JS on one line. "void" (Phase 19
    A-05, 3 Nov): Firefox replaces the page with what a javascript: address returns, so the
    script's own messages never showed there."""
    one_line = " ".join(line.strip() for line in AUTOFILL_JS.strip().splitlines() if line.strip())
    return "javascript:void%20" + quote(one_line, safe="()[]{};,.:=!?&|<>+*/'\"_-$ ").replace(
        " ", "%20")


SETUP_FILE = "job-engine-autofill.html"
SETUP = ("Job Engine autofill, set up once per browser (Chrome, Edge or Firefox):\n"
         "1. Open the file below (job-engine-autofill.html) in that browser.\n"
         "2. Show the bookmarks bar: Ctrl+Shift+B (Firefox: Ctrl+Shift+B too, or View, "
         "Toolbars, Bookmarks Toolbar).\n"
         "3. Drag the orange \"Job Engine autofill\" button from the page onto the bar. "
         "Firefox: or right-click it, Bookmark Link.\n"
         "Use: open the job's APPLICATION FORM (not the job description: press Apply first), "
         "click the bookmark, paste the autofill code from the job's Apply pack. Every field it "
         "filled is marked in orange; check them, fill the rest, attach the resume and press "
         "Submit yourself. On Workday, click it again on each page (it remembers the code for "
         "2 hours in that tab). It always answers with a message: what it filled, or why not.")


def setup_page() -> bytes:
    """The page to add the bookmark from: one link to drag (Phase 19 A-05: the address came in
    3 Telegram messages, each cut inside a word) and the address in one box to copy."""
    mark = htmllib.escape(bookmarklet(), quote=True)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Job Engine autofill</title><style>
body{{font-family:system-ui,sans-serif;max-width:720px;margin:40px auto;padding:0 16px;
line-height:1.5}}a.mark{{display:inline-block;background:#f59e0b;color:#111;padding:10px 18px;
border-radius:8px;font-weight:600;text-decoration:none}}textarea{{width:100%;height:120px}}
</style></head><body><h1>Job Engine autofill</h1>
<p>Drag this button onto your bookmarks bar (Firefox: or right-click it, Bookmark Link):</p>
<p><a class="mark" href="{mark}">Job Engine autofill</a></p>
<p>Clicking it here does nothing useful: use it on a job's application form. It fills empty
fields from your Apply pack code, never presses a button and never submits.</p>
<p>No bookmarks bar? Add a bookmark by hand and paste this whole box as its address:</p>
<textarea readonly onclick="this.select()">{mark}</textarea>
</body></html>""".encode()


def setup_text() -> list[str]:
    """/autofill: how to add the bookmark (once per browser); the page comes as a file."""
    return [SETUP]


# The bookmark. Rules: no // comments (it is joined into one line), every statement ends
# with a semicolon, no em or en dashes. It never clicks a button and never submits.
AUTOFILL_JS = r"""
(async () => { try {
  const KEY = 'je-autofill';
  const norm = s => String(s || '').replace(/([a-z])([A-Z])/g, '$1 $2').toLowerCase()
    .replace(/[^a-z0-9]+/g, ' ').trim();
  const shown = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const inputs = () => [...document.querySelectorAll('input, textarea, select')]
    .filter(el => shown(el) && !el.disabled && !el.readOnly &&
      !/^(hidden|file|submit|button|reset|image|password|checkbox|search)$/i.test(el.type || ''));
  const frame = [...document.querySelectorAll('iframe')].map(f => f.src || '')
    .find(src => /greenhouse|lever\.co|workday|smartrecruiters|ashby|recruitee|personio/i
      .test(src));
  if (frame && inputs().length < 3) {
    if (confirm('The form is inside a frame from another site. Open it on its own page? ' +
      'Then click the bookmark again.')) { location.href = frame; }
    return;
  }
  if (!inputs().length) {
    alert('Job Engine autofill: no form fields on this page. Open the application form ' +
      '(press Apply on the job page first), then click the bookmark again.');
    return;
  }
  let data = null;
  try {
    const kept = JSON.parse(sessionStorage.getItem(KEY) || 'null');
    if (kept && Date.now() - kept.at < 7200000 &&
      confirm('Use the autofill code for ' + kept.data.job + ' again?')) { data = kept.data; }
  } catch (e) { data = null; }
  if (!data) {
    const raw = prompt('Job Engine autofill: paste the code from the Apply pack ' +
      '(the whole message is fine).');
    if (!raw) { return; }
    const m = raw.match(/JE1:([A-Za-z0-9_-]+)/);
    if (!m) { alert('No Job Engine code found: it starts with JE1:'); return; }
    try {
      let b = m[1].replace(/-/g, '+').replace(/_/g, '/');
      while (b.length % 4) { b += '='; }
      const bytes = Uint8Array.from(atob(b), c => c.charCodeAt(0));
      const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('deflate'));
      data = JSON.parse(await new Response(stream).text());
    } catch (e) { alert('The code could not be read: copy it again from Telegram.'); return; }
    try { sessionStorage.setItem(KEY, JSON.stringify({at: Date.now(), data: data})); }
    catch (e) { console.warn('autofill code not kept', e); }
  }
  const f = data.fields || {};
  const RULES = [
    [/\b(first|given) ?name\b|\bforename\b/, 'first_name', /\b(company|employer|refer)/],
    [/\b(last|family) ?name\b|\bsurname\b/, 'last_name', /\b(company|employer|refer)/],
    [/^(full |your |legal )?name$|\bfull name\b/, 'full_name', null],
    [/\be ?mail\b/, 'email', null],
    [/\b(phone|mobile|telephone)\b/, 'phone', /\b(type|code|extension|device)\b/],
    [/\blinked ?in\b/, 'linkedin', null],
    [/\bgit ?hub\b/, 'github', null],
    [/\b(website|portfolio|personal (site|page)|blog)\b/, 'website', /\b(linked ?in|git ?hub)\b/],
    [/\bsponsor/, 'sponsorship_needed', null],
    [/\bauthori[sz]|\bright to work\b|\beligible to work\b/, 'authorized_without_sponsorship',
      /\bsponsor/],
    [/\blegally (allowed|able|eligible|entitled)/,
      'authorized_without_sponsorship', /\bsponsor/],
    [/\bwork permit\b/, 'authorized_without_sponsorship', /\bsponsor/],
    [/\bcurrent (salary|compensation|pay)\b/, 'current_salary', null],
    [/\b(salary|compensation|remuneration|pay expectation|expected pay)\b/, 'expected_salary',
      null],
    [/\bnotice\b/, 'notice_period', null],
    [/\bstart date\b|\bearliest start\b|\bwhen can you start\b|\bavailable to start\b/,
      'earliest_start', null],
    [/\bjoining date\b/, 'earliest_start', null],
    [/\brelocat/, 'relocation', null],
    [/\benglish\b/, 'english', null],
    [/\bgender\b/, 'gender', null],
    [/\b(city|town)\b|\bcurrent location\b|^location$|\bwhere are you (based|located)\b/, 'city',
      /\b(relocat|prefer|willing|office|desired)/],
    [/\bwhy (do you want|are you interested|this (company|role)|us|join)\b|\bmotivation\b/, 'why',
      null],
    [/\bcover ?letter\b|\badditional information\b|\banything else\b|\bmessage to (the )?hiring\b/,
      'cover_letter', null]
  ];
  const STOP = new Set(['the', 'and', 'you', 'your', 'are', 'for', 'with', 'have', 'what',
    'how', 'many', 'any', 'this', 'that', 'our', 'please', 'did', 'does', 'will', 'can']);
  const words = s => norm(s).split(' ').filter(w => w.length > 2 && !STOP.has(w));
  const savedFor = parts => {
    let best = null;
    let bestScore = 0;
    for (const [q, a] of (data.answers || [])) {
      const want = words(q);
      if (!want.length) { continue; }
      for (const p of parts) {
        const have = new Set(words(p));
        const hit = want.filter(w => have.has(w)).length;
        const score = hit / want.length;
        if (score >= 0.75 && (hit >= 2 || want.length === 1) && score > bestScore) {
          best = a; bestScore = score;
        }
      }
    }
    return best;
  };
  const partsOf = el => {
    const parts = [];
    if (el.labels) { for (const l of el.labels) { parts.push(l.innerText); } }
    const by = el.getAttribute('aria-labelledby');
    if (by) {
      for (const id of by.split(/\s+/)) {
        const n = document.getElementById(id);
        if (n) { parts.push(n.innerText); }
      }
    }
    parts.push(el.getAttribute('aria-label'), el.getAttribute('placeholder'),
      el.getAttribute('data-automation-id'), el.name, el.id);
    if (!parts.slice(0, -2).join('').trim() || el.type === 'radio') {
      const box = el.closest('fieldset, [role=radiogroup], [role=group], .application-question, ' +
        '.field, [data-automation-id^=formField]');
      const legend = box && box.querySelector('legend, label, .application-label, .text');
      if (legend) { parts.push(legend.innerText); }
    }
    return parts.map(norm).filter(Boolean);
  };
  const valueFor = el => {
    const parts = partsOf(el);
    const saved = savedFor(parts);
    if (saved) { return saved; }
    for (const [re, key, not] of RULES) {
      if (!f[key]) { continue; }
      if (parts.some(p => re.test(p) && !(not && not.test(p)))) {
        if (key === 'cover_letter' && el.tagName !== 'TEXTAREA') { continue; }
        return f[key];
      }
    }
    return null;
  };
  const fire = el => {
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    el.dispatchEvent(new Event('blur', {bubbles: true}));
  };
  const setText = (el, v) => {
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype :
      HTMLInputElement.prototype;
    el.focus();
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, v);
    fire(el);
  };
  const choice = (texts, v) => {
    const want = norm(v);
    const first = want.split(' ')[0];
    let i = texts.findIndex(t => t === want);
    if (i < 0) { i = texts.findIndex(t => want.length > 2 && t.startsWith(want)); }
    if (i < 0 && /^(yes|no)$/.test(first)) { i = texts.findIndex(t => t.split(' ')[0] === first); }
    return i;
  };
  const filled = [];
  const mark = (el, name) => {
    el.style.outline = '3px solid #f59e0b';
    filled.push(name);
  };
  const doneRadios = new Set();
  for (const el of inputs()) {
    if (el.type === 'radio') {
      if (doneRadios.has(el.name)) { continue; }
      doneRadios.add(el.name);
      const group = [...document.querySelectorAll('input[type=radio]')]
        .filter(r => r.name === el.name);
      if (group.some(r => r.checked)) { continue; }
      const v = valueFor(el);
      if (!v) { continue; }
      const texts = group.map(r => norm((r.labels && r.labels[0] && r.labels[0].innerText) ||
        r.value));
      const i = choice(texts, v);
      if (i >= 0) { group[i].click(); mark(group[i].parentElement || group[i], texts[i]); }
      continue;
    }
    if (el.tagName === 'SELECT') {
      if (el.selectedIndex > 0) { continue; }
      const v = valueFor(el);
      if (!v) { continue; }
      const i = choice([...el.options].map(o => norm(o.text)), v);
      if (i > 0 || (i === 0 && el.options[0].value)) {
        el.selectedIndex = i; fire(el); mark(el, norm(el.options[i].text));
      }
      continue;
    }
    if ((el.value || '').trim()) { continue; }
    const v = valueFor(el);
    if (v) { setText(el, v); mark(el, partsOf(el)[0] || el.name || 'field'); }
  }
  const empty = inputs().filter(el =>
    (el.required || el.getAttribute('aria-required') === 'true') &&
    !(el.value || '').trim() && el.type !== 'radio').length;
  alert('Job Engine autofill (' + data.job + '): filled ' + filled.length + ' fields, marked in ' +
    'orange. ' + (empty === 1 ? '1 required field is still empty. ' :
    empty ? empty + ' required fields are still empty. ' : '') +
    'Check every field, attach your resume and press Submit yourself.');
} catch (e) { alert('Job Engine autofill stopped: ' + ((e && e.message) || e)); } })();
"""
