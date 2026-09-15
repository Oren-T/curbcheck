"""The corner rung: a node both named streets meet at.

The pairs are pre-written by the ETL, so a query that names two streets is two
street resolutions and one indexed lookup per pair, never a graph walk.
"""

from __future__ import annotations

import sqlite3

from curbcheck.engine.labels import single_spaced
from curbcheck.geocode.candidates import (
    CONFIDENCE_INTERSECTION,
    FUZZY_CONFIDENCE_PENALTY,
    GeocodeCandidate,
    GeocodeKind,
)
from curbcheck.geocode.query import IntersectionQuery
from curbcheck.geocode.street import MAX_STREETS_PER_SIDE, resolve_street

_INTERSECTION_SQL = (
    "SELECT display, lon, lat FROM intersection WHERE a_norm = ? AND b_norm = ? LIMIT ?"
)


def intersection_candidates(
    conn: sqlite3.Connection, query: IntersectionQuery, limit: int
) -> list[GeocodeCandidate]:
    """Nodes where both streets meet, from the pre-paired `intersection` table."""
    firsts = resolve_street(conn, query.first)[:MAX_STREETS_PER_SIDE]
    seconds = resolve_street(conn, query.second)[:MAX_STREETS_PER_SIDE]
    found = []
    for a_norm, a_fuzzy in firsts:
        for b_norm, b_fuzzy in seconds:
            penalty = FUZZY_CONFIDENCE_PENALTY if (a_fuzzy or b_fuzzy) else 1.0
            for display, lon, lat in conn.execute(_INTERSECTION_SQL, (a_norm, b_norm, limit)):
                found.append(
                    GeocodeCandidate(
                        label=single_spaced(str(display)),
                        lat=float(lat),
                        lon=float(lon),
                        kind=GeocodeKind.INTERSECTION,
                        confidence=CONFIDENCE_INTERSECTION * penalty,
                    )
                )
    return found
