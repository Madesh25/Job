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

KEY = "apply.answers"  # bot_state: {"items": [{"q": question, "a": answer}, ...]}
MAX_ANSWERS = 60
MAX_IN_PACK = 25
USAGE = ("Save an answer for application forms: /answer <question> = <answer>\n"
         "Example: /answer Years of Terraform? = 3 years in production\n"
         "/answers lists them, /answer delete 2 removes the second one.")


def _key(question: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", question.lower()).strip()


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
    replaced = any(_key(r["q"]) == _key(question) for r in rows)
    rows = [r for r in rows if _key(r["q"]) != _key(question)] + [{"q": question, "a": answer}]
    _save(state, rows)
    verb = "Replaced" if replaced else "Saved"
    return (f"{verb}: {question} = {answer}\nIt is in every Apply pack from now on "
            f"({len(rows)} saved). /answers lists them.")


def listing(state: Any) -> str:
    rows = items(state)
    if not rows:
        return f"No saved answers yet.\n{USAGE}"
    lines = [f"Your saved answers ({len(rows)}), shown in every Apply pack:"]
    lines += [f"{n}. {r['q']} = {r['a']}" for n, r in enumerate(rows, 1)]
    return "\n".join(lines)


def pack_lines(state: Any) -> list[str]:
    """The Apply pack part: your saved answers, or a one-line hint how to add one."""
    rows = items(state)
    if not rows:
        return ["", "Your saved answers", "- none yet: a new form question? Save it once with "
                "/answer <question> = <answer>"]
    lines = ["", "Your saved answers (/answers)"]
    lines += [f"- {r['q']} {r['a']}" for r in rows[-MAX_IN_PACK:]]
    return lines
