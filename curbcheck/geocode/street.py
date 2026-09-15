"""Which street the user means, and the street itself as an answer.

The only hard part of a Manhattan query is the street name, so the ETL
pre-expands every street into the spellings a person might type
(`etl.addresses`) and this module resolves a typed one in a prefix range-scan.
Every other rung asks it first: an address, a corner and the street rung all
start from the same `resolve_street` result.

`street_name_candidates` is the second way in, for the word a person remembers
rather than the one the name starts with: "americas" is Avenue of the Americas
and "king" is the boulevard the centerline files as W 125 ST. It reads the
word index instead of the variant table and shares its scoring with the place
rung (`geocode.names`).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from curbcheck.db import placeholders
from curbcheck.engine.labels import single_spaced
from curbcheck.etl.addresses import name_words
from curbcheck.geocode.candidates import (
    FUZZY_CONFIDENCE_PENALTY,
    STREET_NAME_CONFIDENCE,
    GeocodeCandidate,
    GeocodeKind,
)
from curbcheck.geocode.names import search_names
from curbcheck.geocode.query import prefix_bound

# How far the street resolver will fan out. A half-typed street matches many
# variants ("3" is a prefix of 3 AVE, 3 ST, 30 AVE…); these bound the work a
# single keystroke can cause, and the confidence ladder sorts out what survives.
MAX_PREFIX_VARIANTS = 40
MAX_STREETS_PER_ADDRESS = 6
MAX_STREETS_PER_SIDE = 8
MAX_FUZZY_STREETS = 10
# Below this, an edit-distance-1 guess is not a correction: nearly every
# short variant is within one edit of every other, so "3 A" would "correct"
# to dozens of streets the user did not type.
MIN_FUZZY_CHARS = 4

_VARIANT_EXACT_SQL = "SELECT street_norm FROM street_variant WHERE variant = ? ORDER BY street_norm"
_VARIANT_PREFIX_SQL = (
    "SELECT DISTINCT street_norm FROM street_variant WHERE variant >= ? AND variant < ?"
    " ORDER BY variant, street_norm LIMIT ?"
)
_VARIANTS_SQL = (
    "SELECT variant, street_norm FROM street_variant"
    " WHERE length(variant) >= ? AND length(variant) <= ?"
)
_STREET_SQL = "SELECT display, lon, lat FROM street WHERE street_norm = ?"
_STREETS_BY_NORM_SQL = "SELECT street_norm, display, lon, lat FROM street WHERE street_norm IN ("
# S105 reads "token" in this table and column name as a credential; it is a
# word of a street name.
_STREET_TOKEN_SQL = (
    "SELECT street_norm, search_name FROM street_token"  # noqa: S105
    " WHERE token >= ? AND token < ? ORDER BY token, position LIMIT ?"
)


def names_a_street(conn: sqlite3.Connection, folded: str) -> bool:
    """Whether the whole query is, exactly, a spelling of a street we index."""
    return bool(folded) and conn.execute(_VARIANT_EXACT_SQL, (folded,)).fetchone() is not None


def resolve_street(conn: sqlite3.Connection, folded: str) -> list[tuple[str, bool]]:
    """Street norms `folded` could mean, as (street_norm, is_fuzzy), best first.

    Exact variant match first, then prefix, then — only if both came back
    empty — an edit-distance-1 scan over the whole variant vocabulary. Keeping
    the fuzzy pass last is what holds the common case inside the latency
    budget: a typo is rare, and a correct prefix must never pay for one.
    """
    if not folded:
        return []
    exact = [str(row[0]) for row in conn.execute(_VARIANT_EXACT_SQL, (folded,))]
    prefixed = [
        str(row[0])
        for row in conn.execute(
            _VARIANT_PREFIX_SQL, (folded, prefix_bound(folded), MAX_PREFIX_VARIANTS)
        )
    ]
    seen = set(exact)
    ordered = exact + [norm for norm in prefixed if norm not in seen]
    if ordered:
        return [(norm, False) for norm in ordered]
    return [(norm, True) for norm in _fuzzy_streets(conn, folded)]


def _fuzzy_streets(conn: sqlite3.Connection, folded: str) -> list[str]:
    """Street norms whose variant is within one edit of `folded`.

    A scan over the ~2,800 variants, narrowed in SQL to the ones whose length
    could be within one edit. It only runs when the exact and prefix passes
    found nothing at all, which is what keeps a correctly-typed prefix from
    ever paying for someone else's typo.
    """
    if len(folded) < MIN_FUZZY_CHARS:
        return []
    matches: set[str] = set()
    for variant, street_norm in conn.execute(_VARIANTS_SQL, (len(folded) - 1, len(folded) + 1)):
        if _within_one_edit(str(variant), folded):
            matches.add(str(street_norm))
    return sorted(matches)[:MAX_FUZZY_STREETS]


def _within_one_edit(left: str, right: str) -> bool:
    """True when the two differ by at most one insert, delete or substitution."""
    if left == right:
        return True
    if len(left) == len(right):
        return sum(1 for a, b in zip(left, right, strict=True) if a != b) <= 1
    longer, shorter = (left, right) if len(left) > len(right) else (right, left)
    return any(longer[:index] + longer[index + 1 :] == shorter for index in range(len(longer)))


def street_display(conn: sqlite3.Connection, street_norm: str) -> str:
    row = conn.execute(_STREET_SQL, (street_norm,)).fetchone()
    return single_spaced(str(row[0])) if row is not None else street_norm


def street_candidates(
    conn: sqlite3.Connection, folded: str, limit: int, base: float
) -> list[GeocodeCandidate]:
    """The street itself. `base` drops when the street is only half of what was typed."""
    return streets_from(conn, resolve_street(conn, folded)[:limit], base)


def street_name_candidates(
    conn: sqlite3.Connection, cleaned: str, limit: int
) -> list[GeocodeCandidate]:
    """Streets every one of whose typed words prefix-matches a word of some spelling.

    The rung that answers a name the user knows a word of rather than the
    start of. It overlaps `street_candidates` on purpose — a street found both
    ways keeps the better of the two scores, which `suggest._rank` does when it
    folds the duplicate labels together.
    """
    tokens = name_words(cleaned)
    if not tokens:
        return []
    hits = search_names(conn, _STREET_TOKEN_SQL, tokens, limit)
    if not hits:
        return []
    keys = [hit.name_id for hit in hits]
    rows = {
        str(row[0]): row
        for row in conn.execute(_STREETS_BY_NORM_SQL + placeholders(len(keys)) + ")", keys)
    }
    return [
        GeocodeCandidate(
            label=single_spaced(str(rows[key][1])),
            lat=float(rows[key][3]),
            lon=float(rows[key][2]),
            kind=GeocodeKind.STREET,
            confidence=STREET_NAME_CONFIDENCE[hit.match],
        )
        for hit, key in zip(hits, keys, strict=True)
        if key in rows
    ]


def streets_from(
    conn: sqlite3.Connection, streets: Sequence[tuple[str, bool]], base: float
) -> list[GeocodeCandidate]:
    found = []
    for street_norm, fuzzy in streets:
        row = conn.execute(_STREET_SQL, (street_norm,)).fetchone()
        if row is None:
            continue
        penalty = FUZZY_CONFIDENCE_PENALTY if fuzzy else 1.0
        found.append(
            GeocodeCandidate(
                label=single_spaced(str(row[0])),
                lat=float(row[2]),
                lon=float(row[1]),
                kind=GeocodeKind.STREET,
                confidence=base * penalty,
            )
        )
    return found
