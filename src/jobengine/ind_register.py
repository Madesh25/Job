"""IND public register of recognised sponsors (Netherlands): name matching.

`ind_name` makes a canonical organisation name (legal forms and country words stripped) and
`IndRegister.match` fuzzy-matches a company against the register names with rapidfuzz.

Phase 7 (9 Oct): 14 of 28 Verified companies did not match their register name. Target
Companies call them "Infosys (NL)", "TomTom", "ING", "HCLTech"; the register has the legal
names ("Infosys Limited", "TomTom International B.V.", "ING Bank N.V.", "HCL Technologies").
So: "(NL)", "Limited", "Coöperatieve ... U.A." and similar are stripped; a register name
that starts with the company's name and adds only generic words (Bank, Technologies,
Services, ...) matches; a few known short names have aliases (ALIASES and the Config row
ind_register.aliases, one "Company = Register name" a line); and `closest` shows the nearest
register names for a company that still does not match, so a wrong miss can be seen.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from jobengine.sweep.normalize import canon

STRIP_WORDS = frozenset({"b v", "bv", "n v", "nv", "holding", "holdings", "nederland",
                         "netherlands", "the", "nl", "limited", "ltd", "inc", "plc", "llc",
                         "gmbh", "u a", "ua", "cooperatieve", "cooperative",
                         # Irish legal forms and country words (ie_permits.py uses this too)
                         "ireland", "ie", "dac", "uc", "clg"})
MIN_TOKEN_SET = 90
# token_set_ratio gives 100 whenever one name's words are a subset of the other's
# ("Tulip" vs "Tulip Data"). A second, looser whole-name check keeps those out.
MIN_TOKEN_SORT = 75
# Words a register name may add after the company's own name and still be the same company
# ("ING" = "ING Bank", "Picnic" = "Picnic Technologies"). "Delta" vs "Delta Cloud" stays a
# miss: "cloud" is not generic.
GENERIC_WORDS = frozenset({"bank", "technologies", "technology", "services", "solutions",
                           "international", "europe", "benelux", "group", "digital", "it"})
MAX_GENERIC = 3
# Short or brand names whose register name is different (public facts, not guesses).
ALIASES = {
    "hcltech": ("hcl technologies",),
    "tcs": ("tata consultancy services",),
    "ltimindtree": ("larsen toubro infotech",),
    "just eat takeaway com": ("takeaway com",),
    "miro": ("realtimeboard",),
}


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


def starts_with(wanted: str, key: str) -> bool:
    """`key` is `wanted` plus at most MAX_GENERIC generic words ("ing" / "ing bank")."""
    words, other = wanted.split(), key.split()
    rest = other[len(words):]
    return (other[:len(words)] == words and 0 < len(rest) <= MAX_GENERIC
            and all(w in GENERIC_WORDS for w in rest))


def names_match(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b or starts_with(a, b):
        return True
    token_set, token_sort = name_score(a, b)
    return token_set >= MIN_TOKEN_SET and token_sort >= MIN_TOKEN_SORT


def parse_aliases(text: str | None) -> dict[str, tuple[str, ...]]:
    """Config ind_register.aliases: "Company = Register name" lines."""
    out: dict[str, tuple[str, ...]] = {}
    for line in (text or "").splitlines():
        name, eq, value = line.partition("=")
        if eq and ind_name(name) and ind_name(value):
            out[ind_name(name)] = (*out.get(ind_name(name), ()), ind_name(value))
    return out


@dataclass
class IndRegister:
    names: list[str]
    aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)
    _canon: list[tuple[str, str]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._canon = [(ind_name(n), n) for n in self.names if ind_name(n)]

    @classmethod
    def from_names(cls, names: Iterable[str]) -> IndRegister:
        return cls(list(names))

    def add_aliases(self, text: str | None) -> None:
        """Your own aliases (Config ind_register.aliases) on top of ALIASES."""
        for key, values in parse_aliases(text).items():
            self.aliases[key] = (*self.aliases.get(key, ()), *values)

    def _wanted(self, company: str | None) -> list[str]:
        wanted = ind_name(company)
        if not wanted:
            return []
        return [wanted, *ALIASES.get(wanted, ()), *self.aliases.get(wanted, ())]

    def match(self, company: str | None) -> str | None:
        """The register name that matches `company`, or None."""
        best: tuple[float, str] | None = None
        for wanted in self._wanted(company):
            for key, original in self._canon:
                if names_match(wanted, key):
                    score = 400.0 if key == wanted else sum(name_score(wanted, key))
                    if best is None or score > best[0]:
                        best = (score, original)
        return best[1] if best else None

    def closest(self, company: str | None, limit: int = 2) -> list[str]:
        """The register names nearest to `company` (for a report line), best first."""
        wanted = ind_name(company)
        if not wanted:
            return []
        scored = sorted(((fuzz.token_set_ratio(wanted, key) + fuzz.ratio(wanted, key), name)
                         for key, name in self._canon), reverse=True)
        return [name for _, name in scored[:limit]]


# ---------------------------------------------------------------- download and parse

NAME_HEADERS = ("organisation", "organization", "organisatie", "name", "naam")


def _letters(value: str) -> bool:
    return sum(ch.isalpha() for ch in value) > len(value) / 2


def parse_register_html(html: str) -> list[str]:
    """Organisation names from the register page.

    The page lists the organisations in a table. The name column is found by its header
    (Organisation, Organisatie or Name). A row's first cell may be a <th> (a row header, as on
    the IND page: name in <th>, KvK number in <td>); reading only <td> cells gave 13,020 KvK
    numbers and no match at all (Phase 6, 8 Oct). So every cell counts, and a column that
    holds mostly numbers is never taken for the names: the column with the most letters is.
    Raises ValueError when no names are found, so a changed page is reported, not ignored.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    names: list[str] = []
    for table in soup.find_all("table"):
        rows = [[c.get_text(" ", strip=True) for c in tr.find_all(("td", "th"))]
                for tr in table.find_all("tr")]
        header: list[str] = []
        body = []
        for tr, cells in zip(table.find_all("tr"), rows, strict=True):
            if not header and cells and not tr.find_all("td"):
                header = [c.lower() for c in cells]  # the first all-<th> row
            elif cells:
                body.append(cells)
        if not body:
            continue
        width = max(len(cells) for cells in body)
        wordy = [sum(1 for cells in body if len(cells) > i and _letters(cells[i]))
                 for i in range(width)]
        column = next((i for i, h in enumerate(header)
                       if any(h.startswith(n) for n in NAME_HEADERS)), None)
        if column is None or column >= width or wordy[column] < len(body) / 2:
            column = max(range(width), key=lambda i: wordy[i])
        # The page doubles quotes inside a name (""Aa-Dee"" Machinefabriek).
        names += [cells[column].replace('""', '"') for cells in body
                  if len(cells) > column and cells[column]]
    if not names:
        raise ValueError("no organisation names found on the IND register page")
    return names


def load_register(url: str, get_text: Callable[[str], str]) -> IndRegister:
    """Download and parse the register once. Raises ValueError or http.HttpError on failure."""
    return IndRegister.from_names(parse_register_html(get_text(url)))
