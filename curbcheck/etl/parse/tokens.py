"""Lexical layer: turn one raw `sign_description` into a clean token stream.

Everything that is not grammar happens here — case, whitespace, the arrow
glyph, the parenthetical symbol tokens, and the trailing `(SUPERSEDES …)` tail.
The grammar never sees a parenthesis or an arrow.

Symbol parentheticals are written with the symbol's name in front of them
(`TRUCK (SYMBOL) TRUCK LOADING ONLY`, `MOON & STARS (SYMBOLS) NO STANDING`), so
removing the parenthetical alone leaves a dangling noun. We therefore also drop
the run of symbol-naming words immediately preceding it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from curbcheck.model import Arrow

# The longest live Manhattan description is 168 characters (docs/DATA.md §1.4).
# Anything past this is not a sign string; refusing it keeps parse time bounded
# on hostile input (SPEC §3.3).
MAX_INPUT_CHARS = 400

# Words that name the pictogram in front of a `(SYMBOL)` parenthetical. Measured
# from the corpus: every word that occurs immediately before `(SYMBOL[S])`, minus
# the ones that also carry rule meaning (`STOP`, `HANDICAP` before `BUS STOP`).
_SYMBOL_NAME_WORDS = frozenset(
    {
        "&",
        "ACCESS-A-RIDE",
        "BIKE",
        "BUS",
        "CAB",
        "CAR",
        "CROSS",
        "DIAMOND",
        "FHV",
        "HAIL",
        "HAILING",
        "HANDICAP",
        "HARDHAT",
        "HAT",
        "HARD",
        "HORSE",
        "DRAWN",
        "IDLING",
        "MICROHUB",
        "MOON",
        "NLZ",
        "PRESS",
        "STAR",
        "STARS",
        "TAXI",
        "TRUCK",
        "TRUCKS",
        "VAN",
        "VEHICLE",
        "W/",
        "WET",
        "WITH",
    }
)

_PARENTHETICAL = re.compile(r"\(([^()]*)\)?")

_ARROW_BOTH = re.compile(r"<-+>")
_ARROW_SINGLE_GLYPH = re.compile(r"<-+|-+>")

# `SINGLE ARROW`, `(SINGLE ARROW)`, `W/SINGLE ARROW`, `W/ 7 O'CLOCK ARROW`.
# docs/DATA.md §1.6: 1,666 active rows spell the arrow out instead of drawing it.
_ARROW_WORDS = re.compile(
    r"\(?\s*(?:W/\s*)?SINGLE\s+ARROW\s*\)?"
    r"|W/\s*\d+M?\s*O'CLOCK\s+ARROW"
    r"|\(\s*ARROW\s*\)"
)

# A parenthetical that draws something rather than saying something. Only these
# pull the noun in front of them out with them (`TRUCK (SYMBOL)`).
_PICTOGRAM = re.compile(r"SYMBO|LOGO|BROOM")

_SUPERSEDES = re.compile(r"^S[UY]PERSED")
_NON_ASCII = re.compile(r"[^ -~]")
_PUNCTUATION_ONLY = re.compile(r"^[^A-Z0-9]+$")
_DASHES = re.compile(r"^-+$")


@dataclass(frozen=True)
class Lexed:
    """A description reduced to tokens plus the facts carried by its non-word parts."""

    raw: str
    text: str
    tokens: tuple[str, ...]
    arrow: Arrow = Arrow.NONE
    street_cleaning: bool = False
    snow_emergency: bool = False
    supersedes: tuple[str, ...] = ()
    asides: tuple[str, ...] = ()
    had_non_ascii: bool = False


def lex(raw: str) -> Lexed:
    """Normalize and tokenize one description. Never raises; never loops on input size."""
    text = raw[:MAX_INPUT_CHARS]
    had_non_ascii = bool(_NON_ASCII.search(text))
    if had_non_ascii:
        text = _NON_ASCII.sub(" ", text)
    text = text.upper()

    text, arrow = _extract_arrow(text)
    text, symbols, supersedes, asides = _extract_parentheticals(text)
    text = _collapse(text)

    return Lexed(
        raw=raw,
        text=text,
        tokens=tuple(tokenize(text)),
        arrow=arrow,
        street_cleaning=any("BROOM" in symbol for symbol in symbols),
        snow_emergency=any("SNOW" in symbol for symbol in symbols),
        supersedes=supersedes,
        asides=asides,
        had_non_ascii=had_non_ascii,
    )


def _extract_arrow(text: str) -> tuple[str, Arrow]:
    """Strip every arrow form and return the arity it implies (docs/DECISIONS.md D3 refined).

    A single arrow becomes `Arrow.FORWARD` as a placeholder only: the glyph never
    says "left", so the snapping step replaces it with the bearing from the sign
    row's `arrow_direction` column.
    """
    arrow = Arrow.NONE
    if _ARROW_BOTH.search(text):
        arrow = Arrow.BOTH
        text = _ARROW_BOTH.sub(" ", text)
    if _ARROW_WORDS.search(text):
        arrow = Arrow.BOTH if arrow is Arrow.BOTH else Arrow.FORWARD
        text = _ARROW_WORDS.sub(" ", text)
    if _ARROW_SINGLE_GLYPH.search(text):
        arrow = Arrow.BOTH if arrow is Arrow.BOTH else Arrow.FORWARD
        text = _ARROW_SINGLE_GLYPH.sub(" ", text)
    return text, arrow


def _extract_parentheticals(text: str) -> tuple[str, list[str], tuple[str, ...], tuple[str, ...]]:
    """Remove every parenthetical, sorting it into symbol, supersedes tail or aside.

    Unbalanced opening parens occur in the live data, so a parenthetical runs to
    the closing paren or to the end of the string, whichever comes first.
    """
    kept: list[str] = []
    symbols: list[str] = []
    supersedes: list[str] = []
    asides: list[str] = []
    position = 0

    for match in _PARENTHETICAL.finditer(text):
        kept.append(text[position : match.start()])
        position = match.end()
        inner = _collapse(match.group(1) or "")
        if _SUPERSEDES.match(inner):
            supersedes.append(inner)
            continue
        symbols.append(inner)
        if _PICTOGRAM.search(inner):
            kept = _drop_trailing_symbol_words(kept)
        else:
            asides.append(inner)
    kept.append(text[position:])
    return " ".join(kept), symbols, tuple(supersedes), tuple(asides)


def _drop_trailing_symbol_words(kept: list[str]) -> list[str]:
    words = " ".join(kept).split()
    while words and words[-1].lstrip("/") in _SYMBOL_NAME_WORDS:
        words.pop()
    return [" ".join(words)]


def normalize(raw: str) -> str:
    """Uppercase, ASCII-only, whitespace-collapsed, length-bounded. No tokens removed."""
    text = raw[:MAX_INPUT_CHARS]
    return _collapse(_NON_ASCII.sub(" ", text).upper())


def _collapse(text: str) -> str:
    return " ".join(text.split())


def tokenize(text: str) -> list[str]:
    """Split on whitespace, dropping punctuation that carries no meaning.

    A bare hyphen is kept: it is the range joiner in `MONDAY - FRIDAY` and
    `MAY 15 - SEPT 30`, where DOT put spaces around it.
    """
    tokens = []
    for word in text.split():
        # DOT separates two stacked panels with a slash that sticks to the next
        # word (`NO STANDING <-->/COMMUTER VAN STOP`).
        token = word.lstrip("/").strip(".,;:!+*")
        if _DASHES.match(word):
            tokens.append("-")
        elif token and not _PUNCTUATION_ONLY.match(token):
            tokens.append(token)
    return tokens
