"""The two rungs that are not streets: a named place, and a ZIP centre.

A place is matched word by word against the `place_token` index — every typed
word has to prefix-match a word of the name, in any order — and scored by where
those words landed (`geocode.names`). A ZIP is the coarsest answer the geocoder
gives and says so in its second line.
"""

from __future__ import annotations

import sqlite3

from curbcheck.db import placeholders
from curbcheck.engine.labels import single_spaced
from curbcheck.etl.addresses import name_words
from curbcheck.geocode.candidates import (
    CONFIDENCE_ZIP,
    DEFAULT_SECONDARY,
    PLACE_NAME_CONFIDENCE,
    GeocodeCandidate,
    GeocodeKind,
)
from curbcheck.geocode.names import search_names

_PLACES_BY_ID_SQL = "SELECT place_id, display, lon, lat FROM place WHERE place_id IN ("
_ZIP_SQL = "SELECT lon, lat, address_points FROM zip_centroid WHERE zipcode = ?"
# S105 reads "token" in this table and column name as a credential; it is a
# word of a place name.
_PLACE_TOKEN_SQL = (
    "SELECT place_id, search_name FROM place_token"  # noqa: S105
    " WHERE token >= ? AND token < ? ORDER BY token, position LIMIT ?"
)


def place_candidates(conn: sqlite3.Connection, cleaned: str, limit: int) -> list[GeocodeCandidate]:
    """Places every one of whose typed words prefix-matches a word of the name.

    Best first: the name typed whole, then the names it starts, then the names
    one of its words starts, then the names that contain them. Only the rows
    that survive that ordering are read out of `place`, because reading the
    candidates instead is what a one-letter query would pay for.
    """
    tokens = name_words(cleaned)
    if not tokens:
        return []
    hits = search_names(conn, _PLACE_TOKEN_SQL, tokens, limit)
    if not hits:
        return []
    keys = [int(hit.name_id) for hit in hits]
    rows = {
        int(row[0]): row
        for row in conn.execute(_PLACES_BY_ID_SQL + placeholders(len(keys)) + ")", keys)
    }
    return [
        GeocodeCandidate(
            label=single_spaced(str(rows[key][1])),
            lat=float(rows[key][3]),
            lon=float(rows[key][2]),
            kind=GeocodeKind.PLACE,
            confidence=PLACE_NAME_CONFIDENCE[hit.match],
        )
        for hit, key in zip(hits, keys, strict=True)
        if key in rows
    ]


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
