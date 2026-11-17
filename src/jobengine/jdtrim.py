"""Job description text for the LLM, without the parts that never change an answer.

Postings often end with long legal text: privacy and data-processing notices (GDPR, the
Polish RODO clause), equal-opportunity statements and "click Apply" instructions. They can be
a third of the text and they are paid for as input tokens on every screening and resume.
`trim_jd` drops those lines and repeated lines; everything else is kept word for word, so
the quote check (screen/extract.py) still finds every quote in the full description.

Only the LLM prompt is trimmed. The free checks (sweep/fit.py, screen/gates.py) and the
Notion page keep the full text.
"""

from __future__ import annotations

import re

# A line is one requirement, sentence group or paragraph of the description.
_LINE = re.compile(r"\n|<br\s*/?>")
_BOILERPLATE = re.compile(
    r"dane osobowe|danych osobowych|\brodo\b|administrator(?:em)? danych|przetwarzani[ae] "
    r"(?:danych|twoich)|klauzul[ai] informacyjn|zgod[ae] na przetwarzanie"
    r"|\bgdpr\b|general data protection|personal data|data protection|privacy (?:policy|notice"
    r"|statement)|data controller|processing of (?:your )?(?:personal )?data"
    r"|equal (?:employment )?opportunit|without regard to (?:race|age|sex|gender|religion)"
    r"|\beeo\b|reasonable accommodation|affirmative action"
    r"|persoonsgegevens|privacyverklaring|acquisitie naar aanleiding",
    re.IGNORECASE,
)
MIN_KEPT = 200  # never trim a description down to less than this many characters
# Legal notices are long paragraphs; a short line naming GDPR ("Knowledge of GDPR and data
# protection") is a requirement and stays.
MIN_LEGAL_CHARS = 120


def trim_jd(text: str | None) -> str:
    """The description without legal boilerplate lines and repeated lines."""
    if not text:
        return ""
    kept: list[str] = []
    seen: set[str] = set()
    for line in _LINE.split(text):
        stripped = line.strip()
        key = " ".join(stripped.lower().split())
        legal = len(stripped) >= MIN_LEGAL_CHARS and _BOILERPLATE.search(stripped)
        if not stripped or key in seen or legal:
            continue
        seen.add(key)
        kept.append(stripped)
    trimmed = "\n".join(kept)
    return trimmed if len(trimmed) >= MIN_KEPT else text.strip()
