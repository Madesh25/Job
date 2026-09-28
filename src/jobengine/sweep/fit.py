"""Free checks for jobs you cannot take, run before the daily 30 are picked and again before
screening spends tokens. Pure functions, no I/O.

- Language: the posting requires Polish or Dutch, or it is written in Polish or Dutch and
  does not ask for English.
- Contract: a job in Poland that offers only a B2B contract.

They only fire on clear wording; anything unclear is left for the AI screening.
"""

from __future__ import annotations

import re

from jobengine.sweep.normalize import canon, segments

# A line that only wishes for the language ("Polish is a plus") is not a requirement.
_OPTIONAL = re.compile(
    r"nice[\s-]to[\s-]have|\ba plus\b|\bpreferred\b|\bpreferabl|\bideally\b"
    r"|\badvantage|\bbonus\b|\bdesirable\b|\bwelcome\b|mile widziane|\batut|pluspunt"
    r"|\been pr[eé]\b|\bis a pre\b"
)

_LEVEL = r"(?:fluent|native|excellent|very good|good|strong|business|professional|proficient|" \
         r"advanced|perfect)"
# "Polish clients", "the Dutch market": the country, not the language.
_NOT_LANGUAGE = r"(?!\s+(?:clients?|customers?|market|compan(?:y|ies)|team|office|law|bank|" \
                r"branch|entity|subsidiar(?:y|ies)|business|government|operations|citizens?|" \
                r"residents?|employers?))"
_REQUIRED = r"(?:required|mandatory|a must|must|essential|necessary|needed|obligatory)"


def _language_patterns(english: str, own: list[str]) -> list[re.Pattern[str]]:
    return [
        re.compile(rf"\b{_LEVEL}\s+(?:\w+\s+){{0,3}}{english}\b{_NOT_LANGUAGE}"),
        re.compile(rf"\b{english}\s+(?:language\s+)?(?:skills\s+)?(?:is\s+|are\s+)?{_REQUIRED}"),
        re.compile(rf"\b{english}\s*(?:language)?\s*[(:\-]?\s*(?:at\s+)?(?:b2|c1|c2)\b"),
        re.compile(rf"\b(?:knowledge|command|fluency|proficiency)\s+(?:of|in)\s+(?:the\s+)?"
                   rf"{english}\b"),
        re.compile(rf"\b{english}\s+(?:and|&)\s+english\b|\benglish\s+(?:and|&)\s+{english}\b"),
        *(re.compile(p) for p in own),
    ]


_REQUIRES = {
    "Polish": _language_patterns("polish", [
        r"j[eę]zyk(?:a|iem)?\s+polsk",
        r"znajomo[sś][cć]\s+(?:\w+\s+){0,2}polskiego",
        r"\bpolski(?:ego)?\s+(?:na\s+poziomie|[(:\-]?\s*(?:b2|c1|c2))",
    ]),
    "Dutch": _language_patterns("dutch", [
        r"\bnederlands(?:e taal)?\s+(?:\w+\s+){0,2}(?:vereist|verplicht|noodzakelijk|een must)",
        r"vloeiend\s+(?:\w+\s+){0,2}nederlands",
        r"beheersing\s+van\s+(?:de|het)\s+nederlands",
        r"\bnederlandstalig",
        r"\bnederlands\s+op\s+(?:b2|c1|c2)",
    ]),
}
_ASKS_ENGLISH = re.compile(r"english|angielski|engels")

# Common short words: a posting that uses many more of one language's words than English
# words is written in that language.
_STOPWORDS = {
    "English": frozenset("the and to of you with for our we are is your in will on".split()),
    "Polish": frozenset("i w z na do oraz jest sie dla lub jak od po przy nasz twoj".split()),
    "Dutch": frozenset("de het een en van voor met je wij jouw bij ons naar zijn is".split()),
}
MIN_WORDS = 15  # fewer common words than this: too short to tell


def written_in(text: str | None) -> str | None:
    """"Polish" or "Dutch" when the text is clearly written in it, else None."""
    words = canon(text).split()
    counts = {lang: sum(w in stop for w in words) for lang, stop in _STOPWORDS.items()}
    for lang in ("Polish", "Dutch"):
        if counts[lang] >= MIN_WORDS and counts[lang] >= 2 * counts["English"]:
            return lang
    return None


def _required_segments(text: str) -> list[str]:
    return [seg for seg in segments(text) if not _OPTIONAL.search(seg)]


def language_block(description: str | None) -> str | None:
    """The language the job needs that is not English ("Polish", "Dutch"), else None."""
    if not description:
        return None
    segments = _required_segments(description)
    for lang, patterns in _REQUIRES.items():
        if any(p.search(seg) for seg in segments for p in patterns):
            return lang
    lang = written_in(description)
    if lang and not _ASKS_ENGLISH.search(description.lower()):
        return lang
    return None


_B2B = re.compile(r"\bb2b\b|\+\s*vat\b|\bnetto\s*\+|\bnet\s*\+\s*vat\b")
_EMPLOYMENT = re.compile(
    r"umow[aąęy]\s+o\s+prac|\buop\b|employment contract|contract of employment|"
    r"permanent contract|permanent (?:role|position|employment)|full[\s-]time employment|"
    r"umow[aąęy]\s+zlecen|any form of cooperation|dowoln[aą]\s+form[aąe]\s+wsp[oó]lpracy|"
    r"forma zatrudnienia do wyboru|b2b\s*(?:or|lub|/|,)\s*(?:uop|umowa|employment)|"
    r"(?:uop|umowa o prac[eę]|employment)\s*(?:or|lub|/|,)\s*b2b"
)


def b2b_only(country: str | None, description: str | None, salary: str | None = None) -> bool:
    """True for a job in Poland that clearly offers only a B2B contract."""
    if country != "Poland":
        return False
    text = f"{description or ''}\n{salary or ''}".lower()
    return bool(_B2B.search(text)) and not _EMPLOYMENT.search(text)


def cannot_take(country: str | None, description: str | None,
                salary: str | None = None) -> str | None:
    """A Job Opportunities "Skip reason" option when a free check rules the job out."""
    lang = language_block(description)
    if lang:
        return f"{lang} required"
    if b2b_only(country, description, salary):
        return "B2B only"
    return None
