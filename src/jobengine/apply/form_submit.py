"""Auto-submit (Autopilot H8 step 5, 27 Oct, your OK): the autopilot applies by itself on the
forms it can fill completely; the rest come to you as "Apply by hand".

It runs as the last step of a normal dry run (apply/form_bot.py `finish`), only when the gate
in apply/queue.py `submit_block` is open: prod with DRY_RUN false, Notion Config
`apply.submit` = true, at least `apply.approvals_needed` (10) dry runs approved and none
marked wrong in the last 10, and today's submits under `apply.daily_limit`.

Then the filled form is submitted only when every check passes (`blockers`):
- every required question has an answer from your saved answers or your profile;
- no AI answer in it you have not approved yet (a new AI answer: the dry run comes to you);
- the resume field, when there is one, has the job's approved resume;
- a required consent box: only a privacy or data-protection one, and only with Config
  `apply.tick_privacy` = true (marketing, newsletter and talent-pool boxes are never ticked);
- exactly one visible Submit button (Submit, Apply, Send, Wyślij, Aplikuj, Solliciteer ...).

A captcha or a login already stopped the dry run (the bot never goes past one). After the
press the page is watched for up to CONFIRM_WAIT_MS for a confirmation ("thank you for
applying", "application received" ...): "confirmed" marks the job Applied; "unclear" leaves it
(the "thank you for applying" mail marks it later, Autopilot A).

This is the only module that presses a button (tests/test_repo_rules.py), and it presses one:
the form's Submit.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from typing import Any

from jobengine.apply.form_bot import CONSENT, RESUME, Answer, Result, Sending, norm

log = logging.getLogger("jobengine.apply")

TICK_PRIVACY_KEY = "apply.tick_privacy"  # Notion Config: true lets it tick privacy boxes
PRIVACY = re.compile(r"\b(privacy|gdpr|data protection|personal data|data processing|"
                     r"rodo|przetwarzanie|privacyverklaring|persoonsgegevens)\b")
NEVER_TICK = re.compile(r"\b(marketing|newsletter|talent (pool|community)|future (roles|"
                        r"opportunities|vacancies)|sms|text messages?|offers)\b")
SUBMIT_NAME = re.compile(r"^\s*(submit( (my |your )?application)?|apply( now| for this job)?|"
                         r"send( (my )?application)?|wy[sś]lij( aplikacj[eę]| zg[lł]oszenie)?|"
                         r"aplikuj( teraz)?|solliciteer( nu)?|verstuur|versturen|verzenden)"
                         r"\s*$", re.I)
CONFIRMED = re.compile(
    r"thank(s| you) for (applying|your (application|interest))|application (has been |was )?"
    r"(received|submitted|sent)|we('ve| have) received your application|successfully "
    r"(submitted|applied)|your application is (in|complete)|dzi[eę]kujemy za (aplikacj|"
    r"zg[lł]oszen)|bedankt voor je sollicitatie|sollicitatie is (ontvangen|verstuurd)", re.I)
DONE_URL = re.compile(r"thank|confirm|success|submitted|applied", re.I)
CONFIRM_WAIT_MS = 15_000
STEP_MS = 500
CONFIRMED_STATE, UNCLEAR = "confirmed", "unclear"


def tick_privacy(config: Any) -> bool:
    return (config.get(TICK_PRIVACY_KEY) or "false").strip().lower() == "true"


def privacy_box(a: Answer, allowed: bool) -> bool:
    """A required privacy consent box the bot may tick (with Config apply.tick_privacy)."""
    label = norm(a.field.label)
    return (allowed and a.field.kind == "checkbox" and a.field.required
            and bool(PRIVACY.search(label)) and not NEVER_TICK.search(label))


def blockers(answers: Sequence[Answer], allowed_privacy: bool) -> list[str]:
    """Why this filled form may not be submitted ([] when it may)."""
    out = []
    for a in answers:
        f, label = a.field, " ".join(a.field.label.split())[:60] or "a field"
        if a.source == "ai" and a.value is not None:
            out.append(f"a new AI answer for \"{label}\" (approve the dry run first)")
            continue
        if f.kind == "file" and RESUME.search(norm(f.label)) and a.source != "resume":
            out.append("the resume is not attached")
            continue
        answered = a.value is not None and not a.note
        if not f.required or answered:
            continue
        if f.kind == "checkbox" and CONSENT.search(norm(f.label)):
            if not privacy_box(a, allowed_privacy):
                out.append(f"a required consent box: \"{label}\"")
            continue
        out.append(f"required: \"{label}\" ({a.note or 'no answer'})")
    return out


def _submit_button(frame: Any) -> Any | None:
    """The one visible Submit button of the form's frame, else None."""
    found = []
    for locator in (frame.get_by_role("button", name=SUBMIT_NAME),
                    frame.locator("input[type=submit]")):
        for i in range(locator.count()):
            item = locator.nth(i)
            try:
                if not item.is_visible():
                    continue
                name = item.get_attribute("value") or item.inner_text()
            except Exception:
                continue
            if SUBMIT_NAME.search(" ".join((name or "").split())):
                found.append(item)
    return found[0] if len(found) == 1 else None


def _confirmed(page: Any, before: str) -> bool:
    if page.url != before and DONE_URL.search(page.url):
        return True
    for frame in page.frames:
        try:
            if CONFIRMED.search(frame.inner_text("body", timeout=1_000)):
                return True
        except Exception:
            continue
    return False


def finisher(allowed_privacy: bool) -> Callable[[Any, Result, Sending], None]:
    """The finish step of form_bot.dry_run that submits a form ready to go."""

    def finish(page: Any, result: Result, sending: Sending) -> None:
        why = blockers(result.answers, allowed_privacy)
        if why:
            result.submitted = "not submitted: " + "; ".join(why[:3])
            return
        frame = page.frames[result.answers[0].field.frame] if result.answers else page.main_frame
        for a in result.answers:
            if a.value is None and privacy_box(a, allowed_privacy):
                frame.locator(f'[data-je-field="{a.field.ref}"]').check(timeout=3_000)
                a.value, a.source, a.note = "ticked (privacy consent)", "profile", ""
        button = _submit_button(frame)
        if button is None:
            result.submitted = "not submitted: no single clear Submit button on the form"
            return
        before = page.url
        sending.on = True
        button.click(timeout=5_000)  # the one press: the form's Submit
        for _ in range(CONFIRM_WAIT_MS // STEP_MS):
            page.wait_for_timeout(STEP_MS)
            if _confirmed(page, before):
                result.submitted = CONFIRMED_STATE
                return
        result.submitted = UNCLEAR

    return finish


def report(result: Result, job: str) -> str:
    """The Telegram line of a submit attempt ("" when none was made)."""
    if result.submitted == CONFIRMED_STATE:
        return (f"✅ Applied by the bot: {job}. The company confirmed it (screenshot). Marked "
                "Applied; contacts and mails follow as usual.")
    if result.submitted == UNCLEAR:
        return (f"⚠️ The bot pressed Submit for {job}, but no confirmation showed (screenshot). "
                "Not marked Applied: the \"thank you for applying\" mail marks it, or check the "
                "form yourself.")
    if result.submitted.startswith("not submitted"):
        why = result.submitted.removeprefix("not submitted: ")
        return f"📝 Apply by hand: {job}\n{result.url}\nWhy not by the bot: {why}."
    return ""
