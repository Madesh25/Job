"""LLM extraction (stage `score`): prompt, schema validation and the quote check.

Rule: every extracted value must carry a verbatim quote that is found in the job description
(whitespace-normalised, case-insensitive). A field whose quote is not found is dropped and
treated as "not stated". Nothing is inferred.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import ValidationError

from jobengine.llm import LLMClient
from jobengine.screen.models import Extraction, Language, Quoted, Requirement

STAGE = "score"
MAX_DETAILS = 3
MAX_DETAIL_WORDS = 8
DETAIL_MIN_OVERLAP = 0.6
STOPWORDS = frozenset(
    "a an and are as at be by for from in into is it of on or our the their this to we with "
    "you your will using use".split()
)

SYSTEM_PROMPT = """You extract facts from a job description for a job search assistant.
Return one JSON object only, with exactly these keys:

{
  "years_required_min": {"value": <integer or null>, "quote": <string or null>},
  "seniority_title_flag": {"value": "lead" | "principal" | "none", "quote": <string or null>},
  "languages": [{"language": <string>, "level": "mandatory" | "preferred", "quote": <string>}],
  "contract_types": [{"value": <contract>, "quote": <string>}],
  "mandatory_requirements": [<requirement>],
  "nice_to_have": [<requirement>],
  "sponsorship": {"value": "stated_yes" | "stated_no" | "not_mentioned", "quote": <string or null>},
  "work_mode": {"value": "Remote" | "Hybrid" | "Office" | "unknown", "quote": <string or null>},
  "salary_text": {"value": <string or null>, "quote": <string or null>},
  "expires": {"value": <"YYYY-MM-DD" or null>, "quote": <string or null>},
  "agency_posting": {"value": true | false | null, "quote": <string or null>},
  "specific_details": [<string>]
}

<contract> is one of "UoP", "B2B", "permanent", "contract", "unknown".
<requirement> is {"text": <string>, "terms": [<string>], "kind": <kind>, "quote": <string>}
with <kind> one of "tool", "practice", "education", "certification", "other".

Rules:
- Extract only what is written. Never guess, never infer, never use outside knowledge.
- When something is not stated use null, "none", "not_mentioned" or "unknown".
- Every non-null value needs "quote": a short verbatim copy of the words in the description
  that state it. Copy the words exactly; do not paraphrase inside a quote.
- "preferred", "nice to have", "a plus", "an advantage" or "bonus" mean level "preferred" and
  belong in nice_to_have, never in mandatory_requirements.
- "terms" are the concrete technology or skill names in a requirement, for example
  ["Kubernetes"] or ["AWS", "Terraform"]. Kind "tool" is a named technology or product.
- years_required_min is the smallest number of years of experience the description requires.
- Salary, sponsorship and dates only when the description states them explicitly.
- sponsorship: "stated_yes" only when visa sponsorship or relocation with a work permit is
  offered; "stated_no" only when the description says there is no sponsorship.
- agency_posting: true only when the description says it is posted by a recruitment agency on
  behalf of a client.
- specific_details: up to 3 short phrases (at most 8 words each) about concrete work in this
  role that could follow "the part about" in a message, using the description's own words.
"""


def normalise(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().casefold()


def quote_found(quote: str | None, jd: str) -> bool:
    q = normalise(quote)
    return bool(q) and q in normalise(jd)


def _content_words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9+#]+", text.casefold()) if w not in STOPWORDS]


def detail_ok(detail: str, jd: str) -> bool:
    words = _content_words(detail)
    if not words or len(detail.split()) > MAX_DETAIL_WORDS:
        return False
    jd_words = set(_content_words(jd))
    return sum(1 for w in words if w in jd_words) / len(words) >= DETAIL_MIN_OVERLAP


def user_prompt(title: str, company: str, country: str, jd: str) -> str:
    return (
        f"Job title: {title}\nCompany: {company}\nCountry: {country}\n\n"
        f"Job description:\n<<<\n{jd}\n>>>"
    )


def _quoted(raw: Any, jd: str, empty: Any, dropped: list[str], name: str) -> Quoted:
    try:
        item = Quoted.model_validate(raw) if isinstance(raw, dict) else Quoted(value=None)
    except ValidationError:
        item = Quoted(value=None)
    if item.value in (None, "", empty, "unknown", "not_mentioned", "none"):
        return Quoted(value=empty)
    if not quote_found(item.quote, jd):
        dropped.append(name)
        return Quoted(value=empty)
    return item


def _items(raw: Any, model: type, jd: str, dropped: list[str], name: str) -> list:
    out = []
    for i, entry in enumerate(raw if isinstance(raw, list) else []):
        try:
            item = model.model_validate(entry)
        except ValidationError:
            dropped.append(f"{name}[{i}] invalid")
            continue
        if not quote_found(getattr(item, "quote", None), jd):
            dropped.append(f"{name}[{i}]")
            continue
        out.append(item)
    return out


def check_quotes(raw: dict[str, Any], jd: str) -> tuple[Extraction, list[str]]:
    """Validate the raw LLM JSON and drop every value whose quote is not in the text.
    Returns the checked extraction and the names of the dropped fields."""
    dropped: list[str] = []
    years = _quoted(raw.get("years_required_min"), jd, None, dropped, "years_required_min")
    if years.value is not None:
        try:
            years = Quoted(value=int(years.value), quote=years.quote)
        except (TypeError, ValueError):
            dropped.append("years_required_min")
            years = Quoted(value=None)
    details = [
        d for d in (raw.get("specific_details") or [])[:MAX_DETAILS]
        if isinstance(d, str) and detail_ok(d, jd)
    ]
    extraction = Extraction(
        years_required_min=years,
        seniority_title_flag=_quoted(raw.get("seniority_title_flag"), jd, "none", dropped,
                                     "seniority_title_flag"),
        languages=_items(raw.get("languages"), Language, jd, dropped, "languages"),
        contract_types=_items(raw.get("contract_types"), Quoted, jd, dropped, "contract_types"),
        mandatory_requirements=_items(raw.get("mandatory_requirements"), Requirement, jd,
                                      dropped, "mandatory_requirements"),
        nice_to_have=_items(raw.get("nice_to_have"), Requirement, jd, dropped, "nice_to_have"),
        sponsorship=_quoted(raw.get("sponsorship"), jd, "not_mentioned", dropped, "sponsorship"),
        work_mode=_quoted(raw.get("work_mode"), jd, "unknown", dropped, "work_mode"),
        salary_text=_quoted(raw.get("salary_text"), jd, None, dropped, "salary_text"),
        expires=_quoted(raw.get("expires"), jd, None, dropped, "expires"),
        agency_posting=_quoted(raw.get("agency_posting"), jd, None, dropped, "agency_posting"),
        specific_details=details,
    )
    # "preferred" languages never count as mandatory, whatever the model said.
    for lang in extraction.languages:
        if lang.level not in ("mandatory", "preferred"):
            lang.level = "preferred"
    return extraction, dropped


def extract(
    llm: LLMClient, *, title: str, company: str, country: str, jd: str, key: str | None = None,
) -> tuple[Extraction, list[str]]:
    """One LLM call per job, then the quote check. The LLM gets only the job text: never
    contacts, emails or phone numbers."""
    raw = llm.complete_json(
        STAGE, SYSTEM_PROMPT, user_prompt(title, company, country, jd), key=key
    )
    return check_quotes(raw, jd)
