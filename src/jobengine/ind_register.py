"""IND public register of recognised sponsors (Netherlands): name matching.

`ind_name` makes a canonical organisation name (legal forms and country words stripped) and
`IndRegister.match` fuzzy-matches a company against the register names with rapidfuzz.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
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


# ---------------------------------------------------------------- download and parse

NAME_HEADERS = ("organisation", "organization", "organisatie", "name", "naam")


def parse_register_html(html: str) -> list[str]:
    """Organisation names from the register page.

    The page lists the organisations in a table. The name column is found by its header
    (Organisation, Organisatie or Name); without a matching header the first column is used.
    Raises ValueError when no names are found, so a changed page is reported, not ignored.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    names: list[str] = []
    for table in soup.find_all("table"):
        headers = [th.get_text(" ", strip=True).lower() for th in table.find_all("th")]
        column = next(
            (i for i, h in enumerate(headers) if any(h.startswith(n) for n in NAME_HEADERS)), 0
        )
        for tr in table.find_all("tr"):
            cells = tr.find_all("td")
            if len(cells) > column:
                name = cells[column].get_text(" ", strip=True)
                if name:
                    names.append(name)
    if not names:
        raise ValueError("no organisation names found on the IND register page")
    return names


def load_register(url: str, get_text: Callable[[str], str]) -> IndRegister:
    """Download and parse the register once. Raises ValueError or http.HttpError on failure."""
    return IndRegister.from_names(parse_register_html(get_text(url)))
