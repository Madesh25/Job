"""Answer bank for application form questions (flow feature 10, 9 Oct).

Company forms ask questions the Apply pack does not cover ("Years of Terraform?", "Do you
have a driving licence?", "Earliest interview date?"). You answer one once with
`/answer <question> = <answer>`; it is kept in bot_state and every later Apply pack ends with
"Your saved answers", so you never type it again. `/answers` lists them, `/answer delete N`
removes one. A question saved again replaces its old answer. Nothing is guessed: only what
you saved is shown.
"""

from __future__ import annotations

import re
from typing import Any

# bot_state: {"items": [{"q": question, "a": answer, "country": optional}, ...]}. An answer
# with a country (the starter pack's salaries, apply/starter.py) is used only for jobs there.
KEY = "apply.answers"
MAX_ANSWERS = 150
MAX_IN_PACK = 25
USAGE = ("Save an answer for application forms: /answer <question> = <answer>\n"
         "Example: /answer Years of Terraform? = 3 years in production\n"
         "/answers lists them, /answer delete 2 removes the second one.")


def _key(question: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", question.lower()).strip()


def same(row: dict[str, str], question: str, country: str | None = None) -> bool:
    """The same question (and the same country, or both for any country)."""
    return _key(row.get("q") or "") == _key(question) and \
        (row.get("country") or "") == (country or "")


def for_country(rows: list[dict[str, str]], country: str | None) -> list[dict[str, str]]:
    """The answers a job in `country` may use: the ones for that country first (they win a
    tie), then the ones for any country."""
    mine = [r for r in rows if r.get("country") and r.get("country") == country]
    return [*mine, *(r for r in rows if not r.get("country"))]


def items(state: Any) -> list[dict[str, str]]:
    value = (state.get(KEY) or {}).get("items") if state is not None else None
    return [i for i in value if isinstance(i, dict) and i.get("q")] if isinstance(value, list) \
        else []


def _save(state: Any, rows: list[dict[str, str]]) -> None:
    state.set(KEY, {"items": rows[-MAX_ANSWERS:]})


def command(state: Any, args: str, bank: Any = None, today: Any = None) -> str:
    """/answer: save, replace or delete one answer (also in the Notion Answer bank)."""
    text = args.strip()
    rows = items(state)
    delete = re.fullmatch(r"(?:delete|remove)\s+(\d+)", text, re.I)
    if delete:
        index = int(delete.group(1)) - 1
        if not 0 <= index < len(rows):
            return f"There is no answer {index + 1}. /answers lists them."
        gone = rows.pop(index)
        _save(state, rows)
        return f"Deleted: {gone['q']}"
    question, eq, answer = text.partition("=")
    question, answer = " ".join(question.split()), " ".join(answer.split())
    if not eq or not question or not answer:
        return USAGE
    answer = plain_years(question, answer)
    question, country = _country_of(question)
    names = _same_names(question, country)
    replaced = any(same(r, n, country) for r in rows for n in names)
    rows = [r for r in rows if not any(same(r, n, country) for n in names)] + [
        {"q": n, "a": answer, **({"country": country} if country else {})} for n in names]
    _save(state, rows)
    bank_write(bank, names[0], answer, country, today)
    verb = "Replaced" if replaced else "Saved"
    place = f" ({country})" if country else ""
    return (f"{verb}: {question}{place} = {answer}\nIt is in every Apply pack from now on "
            f"({len(rows)} saved). /answers lists them.")


COUNTRIES = ("Poland", "Netherlands", "Ireland")


def _country_of(question: str) -> tuple[str, str | None]:
    """"Expected salary (Poland)" is the Poland answer of "Expected salary", as /answers
    lists it (Phase 23 4-01, 8 Nov: the Poland salary could not be replaced)."""
    match = re.fullmatch(r"(.+?)\s*\((poland|netherlands|ireland)\)", question, re.I)
    if not match:
        return question, None
    return match.group(1).strip(), match.group(2).capitalize()


def _same_names(question: str, country: str | None) -> list[str]:
    """The names a starter question saves together ("Expected salary", "Salary
    expectations", "Desired salary"), else the question alone."""
    from jobengine.apply.starter import QUESTIONS

    for q in QUESTIONS:
        if q.country == country and any(_key(n) == _key(question) for n in q.saves):
            return list(q.saves)
    return [question]


# Phase 23 4-01 (8 Nov): "3+" was saved for "Years of experience with GCP"; a form wants a
# number. A years question keeps the number ("3+" and "3+ years" -> "3").
_YEARS_Q = re.compile(r"\byears?\b", re.I)
_N_PLUS = re.compile(r"^\s*(\d+)\s*\+\s*(?:years?)?\s*$", re.I)


def plain_years(question: str, answer: str) -> str:
    """The answer with an "N+" year count as N, for a question about years."""
    match = _N_PLUS.match(answer) if _YEARS_Q.search(question) else None
    return match.group(1) if match else answer


def bank_write(bank: Any, question: str, answer: str, country: str | None,
               today: Any) -> None:
    """Your answer in the Notion Answer bank (Source You): the row of the same question and
    country is updated, else one is added. Never raises (Bot State keeps the answer)."""
    if bank is None:
        return
    try:
        bank.upsert({"Question": question[:200], "Country": country or "Any"},
                     {"Answer": answer, "Source": "You", "Approved": True,
                      "Last used": today}, new={"Times used": 0})
    except Exception:
        import logging

        logging.getLogger("jobengine.apply").warning("could not write the answer bank",
                                                     exc_info=True)


def save(state: Any, question: str, answer: str, country: str | None = None) -> bool:
    """Keep one answer (a dry-run correction, an approved AI answer or a starter pack answer,
    which may be for one country). True when new."""
    question, answer = " ".join(question.split()), " ".join(answer.split())
    if not question or not answer:
        return False
    rows = items(state)
    new = not any(same(r, question, country) for r in rows)
    row = {"q": question, "a": answer, **({"country": country} if country else {})}
    _save(state, [r for r in rows if not same(r, question, country)] + [row])
    return new


def _place(row: dict[str, str]) -> str:
    return f" ({row['country']})" if row.get("country") else ""


def listing(state: Any) -> str:
    rows = items(state)
    if not rows:
        return f"No saved answers yet.\n{USAGE}"
    lines = [f"Your saved answers ({len(rows)}), shown in every Apply pack:"]
    lines += [f"{n}. {r['q']}{_place(r)} = {r['a']}" for n, r in enumerate(rows, 1)]
    return "\n".join(lines)


def pack_lines(state: Any, country: str | None = None) -> list[str]:
    """The Apply pack part: your saved answers for a job in `country`, or a one-line hint
    how to add one."""
    rows = for_country(items(state), country)
    if not rows:
        return ["", "Your saved answers", "- none yet: a new form question? Save it once with "
                "/answer <question> = <answer>"]
    lines = ["", "Your saved answers (/answers)"]
    lines += [f"- {r['q']}{_place(r)} {r['a']}" for r in rows[-MAX_IN_PACK:]]
    return lines
