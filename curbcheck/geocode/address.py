"""The house-number rungs, best first.

A surveyed door, then the number placed between its two nearest same-parity
neighbours, then the nearest surveyed number on the street, then CSCL's own
published address range. Each rung is tried only when the one above it found
nothing, so a typed address costs the cheapest lookup that can answer it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from curbcheck.engine.labels import single_spaced
from curbcheck.engine.ranges import Blockface, blockfaces, point_along
from curbcheck.geocode.candidates import (
    CONFIDENCE_ADDRESS_INTERPOLATED,
    CONFIDENCE_ADDRESS_POINT,
    CONFIDENCE_ADDRESS_RANGE,
    CONFIDENCE_NEAR_ADDRESS,
    DEFAULT_SECONDARY,
    FUZZY_CONFIDENCE_PENALTY,
    GeocodeCandidate,
    GeocodeKind,
    zip_secondary,
)
from curbcheck.geocode.street import street_display

# A bracket wider than one hundred-block is two different blocks with a gap
# between them, not a run of missing doors; interpolating across it would
# invent a location rather than fill one in.
MAX_INTERPOLATION_SPAN = 100

_ADDRESS_EXACT_SQL = (
    "SELECT display, zipcode, lon, lat FROM address_point"
    " WHERE street_norm = ? AND house_number = ? ORDER BY display LIMIT ?"
)
# The four neighbour lookups are written out rather than built from a template:
# every SQL string in this package is a literal, which is what lets
# `tests/test_engine_sql_safety.py` enforce that none of them is ever formatted.
_ADDRESS_BELOW_SQL = (
    "SELECT house_number, lon, lat, display FROM address_point WHERE street_norm = ?"
    " AND house_number < ? AND house_number % 2 = ? ORDER BY house_number DESC LIMIT 1"
)
_ADDRESS_ABOVE_SQL = (
    "SELECT house_number, lon, lat, display FROM address_point WHERE street_norm = ?"
    " AND house_number > ? AND house_number % 2 = ? ORDER BY house_number ASC LIMIT 1"
)
_ADDRESS_AT_OR_BELOW_SQL = (
    "SELECT house_number, lon, lat, display FROM address_point WHERE street_norm = ?"
    " AND house_number <= ? ORDER BY house_number DESC LIMIT 1"
)
_ADDRESS_AT_OR_ABOVE_SQL = (
    "SELECT house_number, lon, lat, display FROM address_point WHERE street_norm = ?"
    " AND house_number >= ? ORDER BY house_number ASC LIMIT 1"
)


def address_candidates(
    conn: sqlite3.Connection,
    streets: Sequence[tuple[str, bool]],
    house_number: int,
    limit: int,
) -> list[GeocodeCandidate]:
    """The best rung each candidate street can offer for this house number."""
    found: list[GeocodeCandidate] = []
    for street_norm, fuzzy in streets:
        penalty = FUZZY_CONFIDENCE_PENALTY if fuzzy else 1.0
        rung = best_rung(conn, street_norm, house_number, limit)
        found.extend(
            GeocodeCandidate(
                label=candidate.label,
                lat=candidate.lat,
                lon=candidate.lon,
                kind=candidate.kind,
                confidence=candidate.confidence * penalty,
                secondary=candidate.secondary,
            )
            for candidate in rung
        )
    return found


def best_rung(
    conn: sqlite3.Connection, street_norm: str, house_number: int, limit: int
) -> list[GeocodeCandidate]:
    surveyed = _surveyed_doors(conn, street_norm, house_number, limit)
    if surveyed:
        return surveyed
    interpolated = _interpolated_door(conn, street_norm, house_number)
    if interpolated is not None:
        return [interpolated]
    near = _nearest_door(conn, street_norm, house_number)
    if near is not None:
        return [near]
    return _range_candidates(conn, street_norm, house_number)


def _surveyed_doors(
    conn: sqlite3.Connection, street_norm: str, house_number: int, limit: int
) -> list[GeocodeCandidate]:
    """Every AddressPoint row with this exact number on this street.

    A building with several doors is several rows and is offered as several
    candidates rather than deduplicated (docs/DATA.md §5.1).
    """
    return [
        GeocodeCandidate(
            label=single_spaced(str(display)),
            lat=float(lat),
            lon=float(lon),
            kind=GeocodeKind.ADDRESS,
            confidence=CONFIDENCE_ADDRESS_POINT,
            secondary=zip_secondary(zipcode),
        )
        for display, zipcode, lon, lat in conn.execute(
            _ADDRESS_EXACT_SQL, (street_norm, house_number, limit)
        )
    ]


def _interpolated_door(
    conn: sqlite3.Connection, street_norm: str, house_number: int
) -> GeocodeCandidate | None:
    """The number placed between its two nearest surveyed same-parity neighbours.

    NYC puts odd numbers on one side of the street and even on the other, so
    interpolating between two surveyed points of the same parity stays on the
    correct side. Only 24.4% of the house numbers the centerline implies have
    an address point (docs/DATA.md §5.1) — 1519 3 AVE is absent while 1517 and
    1529 are there — so this rung carries most typed addresses.
    """
    parity = house_number % 2
    below = conn.execute(_ADDRESS_BELOW_SQL, (street_norm, house_number, parity)).fetchone()
    above = conn.execute(_ADDRESS_ABOVE_SQL, (street_norm, house_number, parity)).fetchone()
    if below is None or above is None:
        return None
    low, high = int(below[0]), int(above[0])
    if high - low > MAX_INTERPOLATION_SPAN:
        return None
    position = (house_number - low) / (high - low)
    return GeocodeCandidate(
        label=f"{house_number} {street_display(conn, street_norm)}",
        lat=float(below[2]) + (float(above[2]) - float(below[2])) * position,
        lon=float(below[1]) + (float(above[1]) - float(below[1])) * position,
        kind=GeocodeKind.ADDRESS,
        confidence=CONFIDENCE_ADDRESS_INTERPOLATED,
        secondary=f"{DEFAULT_SECONDARY} · between {low} and {high}",
    )


def _nearest_door(
    conn: sqlite3.Connection, street_norm: str, house_number: int
) -> GeocodeCandidate | None:
    """The closest surveyed number on that street, either side. Two index seeks.

    The rung for a number outside every run of doors — 1 3 AVE, 9999 3 AVE.
    Against real address points this is both simpler and closer than reaching
    for a hundred-block corner, because the neighbouring number is surveyed.
    """
    neighbours = [
        conn.execute(statement, (street_norm, house_number)).fetchone()
        for statement in (_ADDRESS_AT_OR_BELOW_SQL, _ADDRESS_AT_OR_ABOVE_SQL)
    ]
    found = [row for row in neighbours if row is not None]
    if not found:
        return None
    nearest = min(found, key=lambda row: abs(int(row[0]) - house_number))
    return GeocodeCandidate(
        label=f"near {single_spaced(str(nearest[3]))}",
        lat=float(nearest[2]),
        lon=float(nearest[1]),
        kind=GeocodeKind.ADDRESS,
        confidence=CONFIDENCE_NEAR_ADDRESS,
        secondary=DEFAULT_SECONDARY,
    )


def _range_candidates(
    conn: sqlite3.Connection, street_norm: str, house_number: int
) -> list[GeocodeCandidate]:
    """The last rung: CSCL's own address range, for a street AddressPoint skipped.

    233 of the 1,017 centerline streets have no surveyed door and 36 of those
    publish a range (docs/DATA.md §5.1). Two blockfaces claiming one number
    means the source ranges overlap; both are offered rather than one picked.
    """
    faces = [face for face in blockfaces(conn, street_norm) if face.contains(house_number)]
    return [
        candidate
        for face in faces
        if (candidate := _range_candidate(face, house_number)) is not None
    ]


def _range_candidate(face: Blockface, house_number: int) -> GeocodeCandidate | None:
    point = point_along(face.geom, face.position(house_number))
    if point is None:
        return None
    lon, lat = point
    return GeocodeCandidate(
        label=f"{house_number} {single_spaced(face.street_name)}",
        lat=lat,
        lon=lon,
        kind=GeocodeKind.ADDRESS,
        confidence=CONFIDENCE_ADDRESS_RANGE,
        secondary=face.cross_streets or DEFAULT_SECONDARY,
    )
