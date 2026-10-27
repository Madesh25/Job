"""The answer starter pack (28 Oct, your OK): the common form questions, asked once.

`/answers start` asks the questions below one at a time in Telegram, in your words: salary
per country, years with each main tool, education, current title and employer, nationality,
visa status and the like. Each answer is saved like `/answer` (apply/answers.py), so the
dry runs and auto-submit fill those fields from your own answer, and more forms can be
completed without AI. An answer is also written to the Notion Answer bank (Source "You").

- Reply with the answer; "skip" leaves a question for later, "stop" ends the round.
- Only questions you have not answered yet are asked; `/answers start` again continues.
- Salary questions are saved per country: a Dutch form gets your Dutch salary.
- Answer only what is true: a tool you have not used is "0" or "skip". The bot never makes
  up an answer.

Each question's wording is chosen to match form labels by their words (form_bot.saved_for)
without matching a different question: "Years of experience with AWS" never fills an Azure
field. A round waits WAIT_MINUTES for your next answer; after that a message is plain text
again.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from jobengine.apply import answers

KEY = "apply.starter"  # bot_state: {"next": index, "at": iso datetime}
WAIT_MINUTES = 60
SKIP, STOP = "skip", "stop"


@dataclass(frozen=True)
class Question:
    asks: str  # what the bot asks
    saves: tuple[str, ...]  # the saved question(s): the words forms use
    country: str | None = None
    hint: str = ""


def _years(tool: str) -> Question:
    return Question(f"How many years have you worked with {tool}? (0 if never)",
                    (f"Years of experience with {tool}",), hint="for example: 3")


SALARY = ("Expected salary", "Salary expectations", "Desired salary")
QUESTIONS: tuple[Question, ...] = (
    Question("Expected salary for a job in the Netherlands (gross a year, EUR)?",
             SALARY, "Netherlands",
             "for example: 60000 EUR. The visa minimum is about EUR 52,300 a year."),
    Question("Expected salary for a job in Ireland (gross a year, EUR)?",
             SALARY, "Ireland",
             "for example: 55000 EUR. The Critical Skills permit needs EUR 40,904 or more."),
    Question("Expected salary for a job in Poland (gross a month, PLN, employment contract)?",
             SALARY, "Poland", "for example: 18000 PLN"),
    Question("How many years of professional experience do you have?",
             ("How many years of professional experience",), hint="for example: 4"),
    _years("AWS"), _years("Azure"), _years("GCP"), _years("Kubernetes"), _years("Docker"),
    _years("Terraform"), _years("Ansible"), _years("Linux"), _years("Python"),
    _years("Jenkins"), _years("GitLab"),
    Question("Your highest level of education?", ("Highest level of education",),
             hint="for example: Bachelor's degree in Mechanical Engineering"),
    Question("Your current job title?", ("Current title",), hint="for example: DevOps Engineer"),
    Question("Your current employer?", ("Current employer", "Current company"),
             hint="for example: Xerago"),
    Question("Your nationality?", ("Nationality", "Citizenship"), hint="for example: Indian"),
    Question("Your current visa or work permit status?", ("Visa status",),
             hint="for example: Indian citizen, I need a work permit and sponsorship"),
    Question("Which languages do you speak, and how well?", ("Languages you speak",),
             hint="for example: English (C1), Tamil (native)"),
    Question("How did you hear about a job (the usual answer)?", ("How did you hear about",),
             hint="for example: Company careers page"),
    Question("Are you willing to work in the office or hybrid as the role needs?",
             ("Willing to work in the office",), hint="for example: Yes, hybrid or office"),
    Question("Are you willing to travel for work?", ("Willing to travel",),
             hint="for example: Yes, occasionally"),
    Question("Do you have a driving licence?", ("Driving licence", "Driver license"),
             hint="for example: No"),
    Question("Do you hold a security clearance?", ("Security clearance",), hint="for example: No"),
    Question("When are you available for interviews?", ("Interview availability",),
             hint="for example: Any weekday, 09:00 to 18:00 CET"),
)


def _done(state: Any, q: Question) -> bool:
    saved = answers.items(state)
    return any(answers.same(r, q.saves[0], q.country) for r in saved)


def open_questions(state: Any) -> list[int]:
    return [i for i, q in enumerate(QUESTIONS) if not _done(state, q)]


def _ask(i: int, left: int) -> str:
    q = QUESTIONS[i]
    hint = f"\n({q.hint})" if q.hint else ""
    return (f"📝 Starter question {i + 1} of {len(QUESTIONS)} ({left} left): {q.asks}{hint}\n"
            "Reply with your answer, \"skip\" or \"stop\".")


def start(state: Any, now: datetime) -> str:
    """/answers start: the first question not answered yet."""
    left = open_questions(state)
    if not left:
        state.delete(KEY)
        return ("All starter questions are answered. /answers lists them; /answer <question> "
                "= <answer> changes one.")
    state.set(KEY, {"next": left[0], "at": now.isoformat(), "skipped": []})
    intro = ("Starter pack: the questions forms ask most, once. Your answers fill those "
             "fields in every form from now on. Answer only what is true.\n\n"
             if len(left) == len(QUESTIONS) else "")
    return intro + _ask(left[0], len(left))


def waiting(state: Any, now: datetime) -> dict[str, Any] | None:
    value = state.get(KEY) if state is not None else None
    if not value or "next" not in value:
        return None
    try:
        asked = datetime.fromisoformat(str(value.get("at")))
    except ValueError:
        asked = None
    if asked is None or now - asked > timedelta(minutes=WAIT_MINUTES):
        state.delete(KEY)
        return None
    return value


def reply(state: Any, now: datetime, text: str, bank: Any = None) -> str | None:
    """Your answer to the waiting question; None when no starter question is waiting."""
    value = waiting(state, now)
    if value is None:
        return None
    index = int(value["next"])
    q = QUESTIONS[index]
    answer = " ".join(text.split())
    if answer.lower() == STOP:
        state.delete(KEY)
        return ("Stopped. /answers start continues with the questions not answered yet.")
    skipped = list(value.get("skipped") or [])
    if answer.lower() == SKIP:
        skipped.append(index)
        said = "Skipped."
    else:
        for saved in q.saves:
            answers.save(state, saved, answer, q.country)
        _bank(bank, q, answer, now)
        place = f" ({q.country})" if q.country else ""
        said = f"Saved: {q.saves[0]}{place} = {answer}"
    left = [i for i in open_questions(state) if i not in skipped and i > index]
    if not left:
        state.delete(KEY)
        return (f"{said}\nThat was the last one. /answers lists your answers"
                + ("; /answers start asks the skipped ones again." if skipped else "."))
    state.set(KEY, {"next": left[0], "at": now.isoformat(), "skipped": skipped})
    return f"{said}\n\n{_ask(left[0], len(left))}"


def _bank(bank: Any, q: Question, answer: str, now: datetime) -> None:
    if bank is None:
        return
    try:
        bank.create({"Question": q.saves[0][:200], "Answer": answer, "Source": "You",
                     "Approved": True, "Country": q.country or "Any",
                     "Last used": now.date(), "Times used": 0})
    except Exception:  # the answer is still saved in Bot State
        import logging

        logging.getLogger("jobengine.apply").warning("could not write the answer bank",
                                                     exc_info=True)
