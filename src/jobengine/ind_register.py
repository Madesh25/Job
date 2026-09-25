"""IND public register of recognised sponsors (Netherlands): name matching.

`ind_name` makes a canonical organisation name (legal forms and country words stripped) and
`IndRegister.match` fuzzy-matches a company against the register names with rapidfuzz.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from jobengine.sweep.normalize import canon

STRIP_WORDS = frozenset({"b v", "bv", "n v", "nv", "holding", "holdings", "nederland",
                         "netherlands", "the"})
MIN_TOKEN_SET = 90
# token_set_ratio gives 100 whenever one name's words are a subset of the other's
# ("Tulip" vs "Tulip Data"). A second, looser whole-name check keeps those out.
MIN_TOKEN_SORT = 75


def ind_name(name: str | None) -> str:
    """Canonical register name: "Tulip Data Nederland B.V." -> "tulip data"."""
    text = f" {canon(name)} "
    for word in sorted(STRIP_WORDS, key=len, reverse=True):
        text = text.replace(f" {word} ", " ")
        while f" {word} " in text:
            text = text.replace(f" {word} ", " ")
    return " ".join(text.split())


def name_score(a: str, b: str) -> tuple[float, float]:
    return fuzz.token_set_ratio(a, b), fuzz.token_sort_ratio(a, b)


def names_match(a: str, b: str) -> bool:
    if not a or not b:
        return False
    token_set, token_sort = name_score(a, b)
    return token_set >= MIN_TOKEN_SET and token_sort >= MIN_TOKEN_SORT


@dataclass
class IndRegister:
    names: list[str]
    _canon: list[tuple[str, str]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._canon = [(ind_name(n), n) for n in self.names if ind_name(n)]

    @classmethod
    def from_names(cls, names: Iterable[str]) -> IndRegister:
        return cls(list(names))

    def match(self, company: str | None) -> str | None:
        """The register name that matches `company`, or None."""
        wanted = ind_name(company)
        if not wanted:
            return None
        best: tuple[float, str] | None = None
        for key, original in self._canon:
            if names_match(wanted, key):
                score = sum(name_score(wanted, key))
                if best is None or score > best[0]:
                    best = (score, original)
        return best[1] if best else None
