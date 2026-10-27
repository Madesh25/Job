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


def command(state: Any, args: str) -> str:
    """/answer: save, replace or delete one answer."""
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
    replaced = any(same(r, question) for r in rows)
    rows = [r for r in rows if not same(r, question)] + [{"q": question, "a": answer}]
    _save(state, rows)
    verb = "Replaced" if replaced else "Saved"
    return (f"{verb}: {question} = {answer}\nIt is in every Apply pack from now on "
            f"({len(rows)} saved). /answers lists them.")


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
