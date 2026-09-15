"""The two rungs that are not streets: a named place, and a ZIP centre.

A place is matched word by word against the `place_token` index, with only the
last word treated as a prefix, because it is the one still being typed. A ZIP
is the coarsest answer the geocoder gives and says so in its second line.
"""

from __future__ import annotations

import sqlite3

from curbcheck.db import placeholders
from curbcheck.engine.labels import single_spaced
from curbcheck.etl.addresses import place_words
from curbcheck.geocode.candidates import (
    CONFIDENCE_PLACE,
    CONFIDENCE_ZIP,
    DEFAULT_SECONDARY,
    MIN_PLACE_COVERAGE,
    GeocodeCandidate,
    GeocodeKind,
)
from curbcheck.geocode.query import prefix_bound

MAX_PLACE_CANDIDATES = 60

_PLACES_BY_ID_SQL = "SELECT display, lon, lat, token_count FROM place WHERE place_id IN ("
_ZIP_SQL = "SELECT lon, lat, address_points FROM zip_centroid WHERE zipcode = ?"
# S105 reads "token" in these table and column names as a credential; it is
# a word of a place name.
_PLACE_TOKEN_EXACT_SQL = "SELECT place_id FROM place_token WHERE token = ?"  # noqa: S105
_PLACE_TOKEN_PREFIX_SQL = "SELECT place_id FROM place_token WHERE token >= ? AND token < ?"  # noqa: S105


def place_candidates(conn: sqlite3.Connection, cleaned: str, limit: int) -> list[GeocodeCandidate]:
    """Places every one of whose typed words prefix-matches a word of the name.

    Scored by how much of the place's own name the query accounted for, so
    "bryant park" (2 of 2 words) outranks the same two words buried inside
    "CTL PK W DR OV 86 ST TRNVS RD".
    """
    tokens = place_words(cleaned)
    if not tokens:
        return []
    hits: dict[int, int] = {}
    for position, token in enumerate(tokens):
        # Only the last token is treated as a prefix: it is the one still being
        # typed. Treating every token as a prefix pulled thousands of place ids
        # out of the token table for a query as short as "3".
        last = position == len(tokens) - 1
        rows = conn.execute(
            _PLACE_TOKEN_PREFIX_SQL if last else _PLACE_TOKEN_EXACT_SQL,
            (token, prefix_bound(token)) if last else (token,),
        )
        for place_id in {int(row[0]) for row in rows}:
            hits[place_id] = hits.get(place_id, 0) + 1

    keep = [place_id for place_id, count in hits.items() if count == len(tokens)]
    if not keep:
        return []
    keep = keep[:MAX_PLACE_CANDIDATES]
    places = conn.execute(_PLACES_BY_ID_SQL + placeholders(len(keep)) + ")", tuple(keep)).fetchall()
    found = []
    for display, lon, lat, token_count in places:
        coverage = min(len(tokens) / max(int(token_count), 1), 1.0)
        if coverage < MIN_PLACE_COVERAGE:
            continue
        found.append(
            GeocodeCandidate(
                label=single_spaced(str(display)),
                lat=float(lat),
                lon=float(lon),
                kind=GeocodeKind.PLACE,
                confidence=CONFIDENCE_PLACE * coverage,
            )
        )
    found.sort(key=lambda candidate: -candidate.confidence)
    return found[:limit]


def zip_candidates(conn: sqlite3.Connection, zipcode: str) -> list[GeocodeCandidate]:
    row = conn.execute(_ZIP_SQL, (zipcode,)).fetchone()
    if row is None:
        return []
    return [
        GeocodeCandidate(
            label=zipcode,
            lat=float(row[1]),
            lon=float(row[0]),
            kind=GeocodeKind.ZIP,
            confidence=CONFIDENCE_ZIP,
            # The count is what says how coarse this is: a ZIP centre is the
            # mean of a few thousand doors, not any one of them.
            secondary=f"{DEFAULT_SECONDARY} · ZIP centre of {int(row[2])} addresses",
        )
    ]
