"""Which sign strings carry no curb regulation at all (docs/DECISIONS.md D10).

About 22% of active Manhattan rows are MTA route and destination panels,
pay-by-cell locator plates and blank location panels. They are kept in the
`sign` table for audit but must never produce a regulation row, and they must
not count against parser coverage.

Two passes, because the two kinds of evidence are not equally strong:

- `panel_class` runs *before* the grammar. Its rules key on phrases that cannot
  appear on a regulatory sign (`ROUTE PANEL`, `PAY-BY-CELL`, the `XYY`/`ZZZ`
  placeholders DOT uses in its template drawings).
- `advisory_class` runs *after* the grammar, and only when the grammar found no
  rule head. Its vocabulary (`KEEP LEFT`, `NO TRESPASSING`, `IDLING LAW`) also
  occurs as a rider on real regulations, so it is only trustworthy once we know
  the string states no rule.

`etl/stage.py` calls `panel_class` here and stores its label verbatim, so
staging and the parser cannot disagree about what a panel is
(docs/DECISIONS.md D19). There is no second, coarser classifier.
"""

from __future__ import annotations

import re

from curbcheck.etl.parse.tokens import lex, normalize, tokenize
from curbcheck.etl.parse.vocabulary import HEAD_PHRASES, LONGEST_HEAD

# DOT's blank template drawings use these placeholders for the text a work order
# fills in, so a string containing one describes no particular regulation.
_PLACEHOLDER = re.compile(r"\bXYY\b|\bZZZ\b|XX:XX|\bXXXX\b|\b999999\b|\bTO BE (?:SPECIFIED|DET)")

_PRE_GRAMMAR: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("panel:pay_by_cell", re.compile(r"PAY-BY-(?:CELL|APP)|PARKNYC APP|MUNI-METER|PARKING CARD")),
    ("panel:mta_route", re.compile(r"\b(?:ROUTE|DESTINATION) PANEL\b|\bBUS RIDER\b")),
    ("panel:location", re.compile(r"\bLOCATION PANEL\b")),
    ("panel:parking_geometry", re.compile(r"\b(?:ANGLE|DEG|PARALLEL) PARKING ONLY\b")),
)

# Signs that tell a driver something without regulating the curb.
_ADVISORY = re.compile(
    r"\bKEEP (?:LEFT|RIGHT)\b"
    r"|\bMUST EXIT\b"
    r"|\bNEXT (?:LEFT|RIGHT)\b"
    r"|\bNO TRESPASSING\b"
    r"|\bSLIPPERY\b"
    r"|\bREVERSE CURVE\b"
    r"|\bDELINEATOR\b"
    r"|\bIDLING LAW\b"
    r"|\bNO ENGINE IDLING\b"
    r"|\bWEIGHT LIMIT\b"
    r"|\bINFORMATION (?:SIGN|BOX)\b"
    r"|\bPLAZA RULES\b"
    r"|\bOPEN STREETS\b"
    r"|\bDISPATCHERS\b"
    r"|\bCOMPLAINTS\b"
    r"|\bCHARGING (?:SIGN|STATION)\b"
    r"|\bMICROHUB SIGN\b"
    r"|\bNO (?:TRUCKS|BUSES)\b"
    r"|\bCODING RIDER\b"
    r"|\bTEMPORARY CONSTRUCTION REGULATION\b"
    r"|\bDESCRIPTION NOT AVAILABLE\b"
    # Bike-path guide signs name the greenway they point along and regulate no curb.
    r"|\bGREENWAY\b"
)


def panel_class(raw: str) -> str | None:
    """The panel kind this description is, or None if it may state a regulation.

    Returns a stable `panel:<kind>` label suitable for `ParsedSign.notes`.
    """
    lexed = lex(raw)
    if not lexed.tokens:
        return "panel:supersedes_only" if lexed.supersedes else "panel:blank"
    # Matched against the whole normalized string, because the phrases that name
    # a panel often sit inside a parenthetical the lexer strips out.
    text = normalize(raw)
    if _PLACEHOLDER.search(text):
        # A blank template that still names a rule ("DAY - DAY XYY-XYY (FOR BUS
        # STOP ONLY)") describes a real regulation whose days and hours are
        # unspecified. Calling it a panel would tell the engine the curb is
        # free; it goes to the grammar, and from there to `unparsed` (SPEC §11).
        return None if _names_a_rule(text) else "panel:template"
    for label, pattern in _PRE_GRAMMAR:
        if pattern.search(text):
            return label
    return None


def _names_a_rule(text: str) -> bool:
    """True when a rule head phrase appears anywhere in the full description.

    Read from the whole normalized string rather than the lexer's tokens,
    because DOT parks the rule a template rider belongs to inside a
    parenthetical the lexer strips out.
    """
    tokens = tokenize(text)
    return any(
        tuple(tokens[start : start + length]) in HEAD_PHRASES
        for start in range(len(tokens))
        for length in range(1, LONGEST_HEAD + 1)
    )


def advisory_class(text: str) -> str | None:
    """The advisory kind of a description the grammar found no rule in, or None."""
    return "panel:advisory" if _ADVISORY.search(text) else None
