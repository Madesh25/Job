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
           "notice_period", "earliest_start", "relocation", "english", "gender",
           "other_diversity", "education")
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
    if values.get("Company"):  # 17 Nov: "Have you worked for <company> before?"
        out["company"] = " ".join(str(values["Company"]).split())
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
    .filter(el => (shown(el) || (el.type === 'radio' && el.labels &&
      [...el.labels].some(shown))) && !el.disabled && !el.readOnly &&
      el.getAttribute('aria-hidden') !== 'true' && !el.closest('[aria-hidden=true]') &&
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
  const NOT_SPONSOR = new RegExp('\\bauthori[sz]|\\beligib|\\bright to work\\b|\\blegally\\b|' +
    '\\bpermitted to work\\b|\\bwithout (a |any |visa )*sponsor');
  const OTHER_PERSON = new RegExp('\\b(company|employer|refer\\w*|their|connections?|' +
    'contact person|reference|emergency|manager|recruiter)\\b');
  /* 16 Nov, real Greenhouse form (Qube): "Base Salary Currency" is not a salary amount. */
  const NOT_PAY = /\b(currency|bonus|deferred|claw ?back|equity|stock|commission)\b/;
  const DECLINE = new RegExp('\\b(prefer not|decline|don t wish|do not wish|rather not|' +
    'do not want|don t want|not to (say|answer|disclose)|wish not)\\b');
  const NOT_ABLE = /\b(unable|not able|cannot|can t)\b/;
  const RULES = [
    [/\b(first|given) ?name\b|\bforename\b/, 'first_name', OTHER_PERSON],
    [/\b(last|family) ?name\b|\bsurname\b/, 'last_name', OTHER_PERSON],
    [/^(full |your |legal )?name$|\bfull name\b/, 'full_name', OTHER_PERSON],
    [/\be ?mail\b/, 'email', null],
    [/\b(phone|mobile|telephone)\b/, 'phone', /\b(type|code|extension|device)\b/],
    [/\blinked ?in\b/, 'linkedin', null],
    [/\bgit ?hub\b/, 'github', null],
    [/\b(website|portfolio|personal (site|page)|blog)\b/, 'website', /\b(linked ?in|git ?hub)\b/],
    [/\bsponsor/, 'sponsorship_needed',
      NOT_SPONSOR],
    [/\bauthori[sz]|\bright to work\b|\beligible to work\b|\b(allowed|permitted) to work\b/,
      'authorized_without_sponsorship',
      /\b(requir|need)\w*\b.*\bsponsor/],
    [/\blegally (allowed|able|eligible|entitled)/,
      'authorized_without_sponsorship', /\b(requir|need)\w*\b.*\bsponsor/],
    [/\bwork permit\b/, 'authorized_without_sponsorship', /\b(requir|need)\w*\b.*\bsponsor/],
    [/\bcurrent (salary|compensation|pay)\b/, 'current_salary', NOT_PAY],
    [/\b(salary|compensation|remuneration|pay expectation|expected pay)\b/, 'expected_salary',
      NOT_PAY],
    [/\bnotice\b/, 'notice_period', /\b(privacy|data protection|policy|information notice)\b/],
    [/\bstart date\b|\bearliest start\b|\bwhen can you start\b|\bavailable to start\b/,
      'earliest_start', null],
    [/\bavailable from\b|\bwhen are you available\b/, 'earliest_start', null],
    [/\bjoining date\b/, 'earliest_start', null],
    [/\brelocat/, 'relocation', null],
    [/\benglish\b/, 'english', null],
    [/\bgender\b/, 'gender', null],
    /* 17 Nov, real Greenhouse form (InterSystems): the US questions after Gender. */
    [/\b(hispanic|latin[oa]|veteran|disabilit\w*|race|ethnic\w*|sexual orientation)\b/,
      'other_diversity', null],
    [/\bjob location\b/, 'relocation', null],
    [/\bdiscipline\b|\bfield of study\b|^(your )?major$/, 'education', null],
    [/\b(city|town)\b|\bcurrent location\b|^(your )?location$|\bwhere are you (currently )?based\b/,
      'city',
      /\b(relocat|prefer|willing|office|desired)/],
    [/\bwhere are you (currently )?located\b/, 'city', /\b(relocat|prefer|willing|office)/],
    [/\bwhy (do you want|are you interested|this (company|role)|us|join)\b|\bmotivat/, 'why',
      null],
    [/\bwhy you (are|would be) interested\b|\bwhy are you applying\b/, 'why', null],
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
        /* 17 Nov: a one-word question ("Degree") only answers a short label. */
        if (score >= 0.75 && (hit >= 2 || (want.length === 1 && have.size <= 2)) &&
            score > bestScore) {
          best = a; bestScore = score;
        }
      }
    }
    return best;
  };
  /* 17 Nov, Keylane (Workable): the question of YES / NO buttons sits outside their group,
     so the first label in the box was the YES option itself. The question of a radio group:
     its aria-labelledby or legend, else the first line of the nearest box around the whole
     group (and no other field) that is not one of its options. */
  const txt = n => (n && (n.innerText || n.textContent) || '').replace(/\s+/g, ' ').trim();
  const groupQuestion = el => {
    const mates = el.name ? [...document.querySelectorAll('input[type=radio]')]
      .filter(r => r.name === el.name) : [el];
    const options = new Set(mates.map(r => norm(txt(r.labels && r.labels[0]) || r.value)));
    const group = el.closest('fieldset, [role=radiogroup], [role=group]');
    if (group) {
      const by = (group.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)
        .map(id => txt(document.getElementById(id))).filter(Boolean).join(' ');
      if (by) { return by; }
      if (group.getAttribute('aria-label')) { return group.getAttribute('aria-label'); }
      const legend = group.querySelector('legend');
      if (legend && txt(legend)) { return txt(legend); }
    }
    for (let p = group || el.parentElement, d = 0; p && d < 6; p = p.parentElement, d++) {
      if (mates.some(r => !p.contains(r))) { continue; }
      if (p.querySelectorAll('input:not([type=hidden]), textarea, select').length >
        mates.length) { break; }
      const line = (p.innerText || '').split('\n').map(s => s.trim()).find(s => {
        let rest = ' ' + norm(s) + ' ';
        for (const o of options) { if (o) { rest = rest.split(' ' + o + ' ').join(' '); } }
        return rest.trim();
      });
      if (line) { return line; }
    }
    return '';
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
    const asked = el.type === 'radio' ? groupQuestion(el) : '';
    if (asked) {
      parts.push(asked);
    } else if (!parts.slice(0, -2).join('').trim() || el.type === 'radio') {
      const box = el.closest('fieldset, [role=radiogroup], [role=group], .application-question, ' +
        '.field, [data-automation-id^=formField]');
      const legend = box && box.querySelector('legend, label, .application-label, .text');
      if (legend) { parts.push(legend.innerText); }
    }
    /* 16 Nov, real Personio form: "Location* (required)" missed the location rule. */
    return parts.map(p => norm(p).replace(/( required)+$/, '')).filter(Boolean);
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
    return workedHere(parts, el.type);
  };
  /* 17 Nov, real Greenhouse form (InterSystems): "Have you previously worked for
     InterSystems?" and the box "I have never worked at InterSystems": you apply to a company
     you have not worked for. Only a question naming the job's own company, never why or how. */
  const company = norm(f.company || String(data.job || '').split(',')[0]).replace(new RegExp(
    '( (b ?v|n ?v|gmbh|inc|ltd|llc|plc|ag|sa|sp z o ?o|group|ab|as|oy|srl|spa|limited|' +
    'corporation|corp))+$'), '');
  const workedHere = (parts, type) => {
    if (company.length < 3) { return null; }
    const text = parts.find(p => (' ' + p + ' ').includes(' ' + company + ' ') &&
      /\b(worked|employed|employee|intern)\b/.test(p) &&
      !/\b(why|how|what|when|where|which|who)\b/.test(p));
    if (!text) { return null; }
    if (type === 'checkbox') { return /\bnever\b/.test(text) ? 'Yes' : null; }
    return 'No, never';
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
  const isCombo = el => el.getAttribute('role') === 'combobox' ||
    el.getAttribute('aria-autocomplete') === 'list';
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const comboBox = el => el.closest('[class*=control]') || el.closest('[class*=container], ' +
    '.field, .application-question') || el.parentElement || el;
  const chosenIn = el => {
    for (let n = el.parentElement, d = 0; n && d < 6; n = n.parentElement, d++) {
      if (n.querySelectorAll('[role=combobox]').length > 1) { break; }
      const shownValue = n.querySelector('[class*=single-value], [class*=singleValue], ' +
        '[class*=multi-value], [class*=multiValue]');
      if (shownValue) { return !!shownValue.innerText.trim(); }
    }
    return false;
  };
  const optionsOf = el => {
    const ids = [el.getAttribute('aria-controls'), el.getAttribute('aria-owns')]
      .filter(Boolean).join(' ').split(/\s+/).filter(Boolean);
    let opts = [];
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
    return opts.filter(shown);
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
    /* 17 Nov, InterSystems: "Prefer not to say" is "Decline To Self Identify" there, and
       "Open to relocate" is "Able to relocate to job location". */
    for (const re of [DECLINE, /\brelocat/, /\bnever\b/]) {
      if (i < 0 && re.test(want)) {
        const hits = texts.map((t, k) => re.test(t) && !NOT_ABLE.test(t) ? k : -1)
          .filter(k => k >= 0);
        if (hits.length === 1) { i = hits[0]; }
      }
    }
    return i;
  };
  const loading = () => [...document.querySelectorAll('[class*=menu-notice--loading], ' +
      '[class*=loading-indicator], [class*=loadingIndicator], [class*=loadingMessage]')]
    .some(shown);
  const pick = async (el, v) => {
    /* 17 Nov, InterSystems: the list shows only options with the typed text, and "Prefer not
       to say" is "Decline To Self Identify" there: then the whole list is looked at. */
    /* 17 Nov, InterSystems "Location (City)": its places come from a search site, slower than
       1.8 s at times: waited for (up to 6 s) while the list says it is loading; "Chennai,
       India" is also searched as "Chennai". */
    const head = String(v).split(',')[0].trim();
    for (const typed of [v, ...(head && head !== String(v).trim() ? [head] : []), '']) {
      setText(el, typed);
      el.dispatchEvent(new KeyboardEvent('keyup', {key: 'Unidentified', bubbles: true}));
      for (let n = 0; n < 40; n++) {
        await sleep(150);
        const opts = optionsOf(el);
        if (!opts.length) { if (n >= 11 && !loading()) { break; } continue; }
        const i = choice(opts.map(o => norm(o.innerText)), v);
        if (i < 0) { break; }
        const text = norm(opts[i].innerText);
        opts[i].click();
        return text;
      }
    }
    setText(el, '');
    return null;
  };
  const filled = [];
  const toChoose = [];
  const refused = [];
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
      else { toChoose.push(partsOf(el).slice(-1)[0] || 'a choice'); }
      continue;
    }
    if (el.tagName === 'SELECT') {
      if (el.selectedIndex > 0) { continue; }
      const v = valueFor(el);
      if (!v) { continue; }
      const i = choice([...el.options].map(o => norm(o.text)), v);
      if (i > 0 || (i === 0 && el.options[0].value)) {
        el.selectedIndex = i; fire(el); mark(el, norm(el.options[i].text));
      } else { toChoose.push(partsOf(el)[0] || 'a list'); }
      continue;
    }
    if (isCombo(el)) {
      if ((el.value || '').trim() || chosenIn(el)) { continue; }
      const v = valueFor(el);
      if (!v) { continue; }
      const got = await pick(el, v);
      if (got) { mark(comboBox(el), got); } else { toChoose.push(partsOf(el)[0] || 'a list'); }
      continue;
    }
    const had = (el.value || '').trim();
    if (had && !(el.type === 'tel' && /^\+?\d{1,4}$/.test(had))) { continue; }
    let v = valueFor(el);
    if (v && el.type === 'number') {
      const n = String(v).replace(/(\d)[\s,.'](?=\d{3}\b)/g, '$1').match(/\d+(\.\d+)?/);
      v = n ? n[0] : null;
    }
    if (!v) { continue; }
    setText(el, v);
    if ((el.value || '').trim() && el.value !== had) {
      mark(el, partsOf(el)[0] || el.name || 'field');
    } else if (!had) {
      refused.push(partsOf(el)[0] || el.name || 'a field');
    }
  }
  /* 17 Nov, InterSystems: the box "I have never worked at InterSystems", when no box of its
     group is ticked yet. */
  for (const box of document.querySelectorAll('input[type=checkbox]')) {
    if (box.disabled || box.checked) { continue; }
    if (!shown(box) && !(box.labels && box.labels[0] && shown(box.labels[0]))) { continue; }
    const parts = partsOf(box);
    if (workedHere(parts, 'checkbox') !== 'Yes') { continue; }
    if ([...document.querySelectorAll('input[type=checkbox]')]
      .some(b => b.checked && b.name && b.name === box.name)) { continue; }
    box.click();
    if (box.checked) { mark(box.parentElement || box, parts[0] || 'never worked here'); }
  }
  await sleep(300);
  const empty = inputs().filter(el =>
    (el.required || el.getAttribute('aria-required') === 'true') &&
    !(el.value || '').trim() && el.type !== 'radio' && !isCombo(el)).length;
  const lists = inputs().filter(el => isCombo(el) && !chosenIn(el) &&
    !(el.value || '').trim() && (el.required || el.getAttribute('aria-required') === 'true'))
    .length;
  alert('Job Engine autofill (' + data.job + '): filled ' + filled.length + ' fields, marked in ' +
    'orange. ' + (empty === 1 ? '1 required field is still empty. ' :
    empty ? empty + ' required fields are still empty. ' : '') +
    (lists ? lists + ' required list question(s) still to choose. ' : '') +
    (toChoose.length ? 'No matching choice, choose yourself: ' +
      toChoose.slice(0, 5).join('; ') + '. ' : '') +
    (refused.length ? 'The form did not take the answer, fill yourself: ' +
      refused.slice(0, 5).join('; ') + '. ' : '') +
    'Check every field, attach your resume and press Submit yourself.');
} catch (e) { alert('Job Engine autofill stopped: ' + ((e && e.message) || e)); } })();
"""
