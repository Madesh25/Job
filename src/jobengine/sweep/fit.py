"""Free checks for jobs you cannot take, run before the daily 30 are picked and again before
screening spends tokens. Pure functions, no I/O.

- Language: the posting requires a language other than English (Polish, Dutch, German,
  French and so on, in the description or the title: "German-Speaking"), or it is written in
  Polish or Dutch (even when it also asks for English: the team works in that language).
- Contract: a job in Poland that offers only a B2B contract.
- Visa: the posting says there is no visa sponsorship or relocation, or that the right to
  work in the EU (or the country) is required.

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


# Other languages than Polish and Dutch: the English wording only.
OTHER_LANGUAGES = ("German", "French", "Spanish", "Italian", "Portuguese", "Czech", "Slovak",
                   "Hungarian", "Romanian", "Swedish", "Danish", "Norwegian", "Finnish",
                   "Russian", "Ukrainian", "Turkish", "Greek", "Hebrew", "Arabic", "Japanese",
                   "Korean", "Chinese", "Mandarin")
# The Job Opportunities "Skip reason" options for languages; any other language is "Other".
LANGUAGE_REASONS = ("Polish required", "Dutch required")

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
    **{lang: _language_patterns(lang.lower(), []) for lang in OTHER_LANGUAGES},
}
_TITLE_LANGUAGES = {lang.lower(): lang for lang in ("Polish", "Dutch", *OTHER_LANGUAGES)}

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


def title_language(title: str | None) -> str | None:
    """A language named in the title ("macOS Engineer (German-Speaking)", "L1, French")."""
    for word in canon(title).split():
        if word in _TITLE_LANGUAGES:
            return _TITLE_LANGUAGES[word]
    return None


def language_block(description: str | None, title: str | None = None) -> str | None:
    """The language the job needs that is not English ("Polish", "German"), else None."""
    lang = title_language(title)
    if lang or not description:
        return lang
    segments = _required_segments(description)
    for lang, patterns in _REQUIRES.items():
        if any(p.search(seg) for seg in segments for p in patterns):
            return lang
    return written_in(description)


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


_WORK_RIGHT = r"(?:right to work|work permit|work authori[sz]ation|work visa|" \
              r"eligib\w* to work|authori[sz]ed to work|permission to work)"
_NO_SPONSORSHIP = re.compile(
    r"\bno\s+(?:visa\s+)?sponsor(?:ship|ing)\b"
    r"|\b(?:visa\s+)?sponsorship\s+(?:is\s+)?(?:not|un)\s*(?:available|provided|offered|possible)"
    r"|\b(?:unable|not able|cannot|can\s*not|can't|do not|don't|will not|won't|does not|"
    r"doesn't)\s+(?:to\s+)?(?:offer\s+|provide\s+|support\s+)?(?:visa\s+)?sponsor"
    r"|\bwithout\s+(?:the\s+need\s+for\s+|requiring\s+)?(?:visa\s+|employer\s+)?sponsorship"
    rf"|\bmust\s+(?:already\s+)?(?:have|hold|possess|be)\s+(?:\w+\s+){{0,4}}{_WORK_RIGHT}"
    rf"|\b(?:eu|eea|eu/eea|eu\s+eea|european)\s+{_WORK_RIGHT}\s+(?:is\s+)?(?:required|mandatory|"
    r"needed|a must)"
    rf"|\b{_WORK_RIGHT}\s+(?:in\s+the\s+(?:eu|eea)\s+)?(?:is\s+)?(?:required|mandatory|a must)"
    r"|\brelocation\s+(?:provided|assistance|support|package|offered)\W{0,12}"
    r"(?:none|no|not\s+(?:provided|available|offered))\b"
)


def no_sponsorship(description: str | None) -> bool:
    """True when the posting says there is no visa sponsorship or relocation, or that you
    must already have the right to work there."""
    if not description:
        return False
    text = re.sub(r"<br\s*/?>", "\n", description.lower())
    return bool(_NO_SPONSORSHIP.search(text))


NO_SPONSORSHIP = "No visa sponsorship"
B2B_ONLY = "B2B only"


def cannot_take(country: str | None, description: str | None, salary: str | None = None,
                title: str | None = None) -> str | None:
    """Why a free check rules the job out ("German required", "B2B only", "No visa
    sponsorship"), else None. skip_reason() gives the Job Opportunities option for it."""
    lang = language_block(description, title)
    if lang:
        return f"{lang} required"
    if b2b_only(country, description, salary):
        return B2B_ONLY
    if no_sponsorship(description):
        return NO_SPONSORSHIP
    return None


def skip_reason(label: str) -> str:
    """The Job Opportunities "Skip reason" option for a cannot_take() label: the language
    and B2B options exist, anything else is "Other" (the schema is never changed here)."""
    return label if label in (*LANGUAGE_REASONS, B2B_ONLY) else "Other"
