"""The ranked suggestion engine: which rungs to try, in what order, and what survives.

`suggest` is the whole ladder and `geocode` is `suggest` minus anything the
rest of the app could not answer about. The order the rungs are tried in is
the latency budget: a correctly-typed prefix must never pay for a rung that
exists for a query which is not what was typed.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from curbcheck.db import read_snapshot
from curbcheck.engine.coverage import within_coverage
from curbcheck.etl.addresses import fold
from curbcheck.geocode.address import address_candidates
from curbcheck.geocode.candidates import (
    CONFIDENCE_STREET_EXACT,
    CONFIDENCE_STREET_HALF_QUERY,
    CONFIDENCE_STREET_WHOLE_QUERY,
    MAX_CANDIDATES,
    GeocodeCandidate,
    GeocodeKind,
)
from curbcheck.geocode.intersection import intersection_candidates
from curbcheck.geocode.places import place_candidates, zip_candidates
from curbcheck.geocode.query import (
    AddressQuery,
    IntersectionQuery,
    ParsedQuery,
    ZipQuery,
    clean_query,
    parse_query,
)
from curbcheck.geocode.street import (
    MAX_STREETS_PER_ADDRESS,
    names_a_street,
    resolve_street,
    street_candidates,
    street_name_candidates,
    streets_from,
)

# A street's pin is a vertex of one of its own centerline segments and a corner
# is a segment endpoint (`etl.addresses._street_points`, `_write_intersections`),
# so both are at distance zero from the centerline by construction and asking
# `within_coverage` costs a scan to learn nothing. The kinds read off another
# dataset -- a surveyed door, a place, a ZIP centroid -- are still checked. On
# the data mount this is the difference between 15 ms and 1,135 ms for
# "broadwa", whose eight street candidates are spread the length of the island.
_ON_THE_CENTERLINE = frozenset({GeocodeKind.STREET, GeocodeKind.INTERSECTION})


def suggest(
    conn: sqlite3.Connection, text: str, *, limit: int = MAX_CANDIDATES
) -> list[GeocodeCandidate]:
    """Ranked suggestions for a partly-typed query, best first, at most `limit`.

    Returns an empty list for anything unparseable or unmatched: not finding an
    address is an answer, not an error. Never raises on hostile input, and
    never spends more than one prefix scan on the common case — the fuzzy pass
    only runs when the exact and prefix passes found nothing.
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")
    cleaned = clean_query(text)
    query = parse_query(cleaned)
    if query is None:
        return []
    with read_snapshot(conn):
        found = _lookup(conn, query, cleaned, limit)
    return _rank(found, limit)


def geocode(
    conn: sqlite3.Connection, text: str, *, limit: int = MAX_CANDIDATES
) -> list[GeocodeCandidate]:
    """`suggest`, minus anything the rest of the app could not answer about.

    This is what `/api/geocode` and `/api/search` call. Offering a destination
    and then refusing to search it is the shape of failure the coverage rule
    exists to end (UX audit P0-3), so the filter lives between the two rather
    than in either. One read transaction covers the suggestion and all eight
    coverage checks, which on the data mount is the difference between one
    page-1 read and nine.
    """
    with read_snapshot(conn):
        return _in_coverage(conn, suggest(conn, text, limit=limit), limit)


def _lookup(
    conn: sqlite3.Connection, query: ParsedQuery, cleaned: str, limit: int
) -> list[GeocodeCandidate]:
    if isinstance(query, ZipQuery):
        return zip_candidates(conn, query.zipcode)
    if isinstance(query, IntersectionQuery):
        corners = intersection_candidates(conn, query, limit)
        if corners:
            return corners
        # Two streets that never share a centerline node. Offering each one
        # separately is honest; claiming a corner that is not in the data
        # would not be. The word rungs run too, because "and" separates two
        # streets and joins two words of a name — "art and design" is a school,
        # not a corner — and only an empty corner says which was meant.
        return [
            *street_candidates(conn, query.first, limit, CONFIDENCE_STREET_HALF_QUERY),
            *street_candidates(conn, query.second, limit, CONFIDENCE_STREET_HALF_QUERY),
            *street_name_candidates(conn, cleaned, limit),
            *place_candidates(conn, cleaned, limit),
        ]
    whole = fold(cleaned)
    named_exactly = names_a_street(conn, whole)
    # "5 AVE" and "86 ST" parse as a house number on a street called "AVE" or
    # "ST", and the prefix scan would answer with 5 AVE A and 86 ST NICHOLAS
    # AVE. When the whole string is itself a street name, that is what was
    # typed, so the house-number reading is not taken at all.
    if isinstance(query, AddressQuery) and not named_exactly:
        return _address_or_its_street(conn, query, cleaned, limit)
    # "1519 3rd ave" is neither a street name nor a place name, so these two
    # passes run only once the address reading has come back empty: running
    # them anyway makes every keystroke pay for the street pass's fuzzy scan.
    base = CONFIDENCE_STREET_EXACT if named_exactly else CONFIDENCE_STREET_WHOLE_QUERY
    return [
        *street_candidates(conn, whole, limit, base),
        *street_name_candidates(conn, cleaned, limit),
        *place_candidates(conn, cleaned, limit),
    ]


def _address_or_its_street(
    conn: sqlite3.Connection, query: AddressQuery, cleaned: str, limit: int
) -> list[GeocodeCandidate]:
    """The house number if anything can place it, otherwise the street it named.

    The streets are resolved once and reused, so a query that matches nothing
    pays for the edit-distance scan once rather than twice. "1 police plaza"
    and "350 5th" both parse as house numbers and only one of them is one, so
    the word-matching rungs run here too once the address reading is empty.
    """
    streets = resolve_street(conn, query.street)[:MAX_STREETS_PER_ADDRESS]
    found = address_candidates(conn, streets, query.house_number, limit)
    if found:
        return found
    # "200 E 85 ST" on a street with neither a surveyed door nor a published
    # range is E 85 ST, not nothing.
    return [
        *streets_from(conn, streets, CONFIDENCE_STREET_HALF_QUERY),
        *place_candidates(conn, cleaned, limit),
    ]


def _rank(candidates: Sequence[GeocodeCandidate], limit: int) -> list[GeocodeCandidate]:
    """Best first, one row per (kind, label), capped at `limit`."""
    best: dict[tuple[GeocodeKind, str], GeocodeCandidate] = {}
    for candidate in candidates:
        key = (candidate.kind, candidate.label)
        if key not in best or candidate.confidence > best[key].confidence:
            best[key] = candidate
    ordered = sorted(
        best.values(), key=lambda c: (-c.confidence, len(c.label), c.label, c.lon, c.lat)
    )
    return ordered[:limit]


def _in_coverage(
    conn: sqlite3.Connection, candidates: list[GeocodeCandidate], limit: int
) -> list[GeocodeCandidate]:
    """Drop anything the rest of the app could not answer about, then cap.

    Every candidate comes off a Manhattan dataset, so this never fires today.
    It is the guarantee rather than the filter (`docs/DECISIONS.md` D28).
    """
    kept: list[GeocodeCandidate] = []
    for candidate in candidates:
        if len(kept) >= limit:
            break
        if candidate.kind in _ON_THE_CENTERLINE or within_coverage(
            conn, lon=candidate.lon, lat=candidate.lat
        ):
            kept.append(candidate)
    return kept
