"""Sixty geocoder answers from the Python engine, as JSON on stdout.

A spot check, not the differential harness: `queries.py` and `make_fixtures.py`
cover the whole engine over hundreds of queries and are the deploy gate. This
runs only `geocode` and `reverse_geocode`, over a query set small enough to read
by eye, so the browser geocoder can be checked against the reference while the
rest of the harness is still being built — and so a difference has forty typed
strings around it rather than two hundred.

The queries are literals rather than samples: every one is here because it
picks a particular rung of the ladder (`curbcheck.geocode.__init__` lists them)
or a particular edge of the query grammar. The payloads are the ones
`api/routes._candidate_payload` and `_reverse_payload` build, so the output can
be compared with the worker's without a translation step.

    python site/tests/harness/geocode_spot.py --db data/curbcheck.sqlite

The output is a build artifact (STYLE_GUIDE §6): redirect it somewhere
gitignored, never into the tree.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from curbcheck import db
from curbcheck.geocode import geocode, reverse_geocode

# Forty typed strings, one rung or one grammar edge each.
GEOCODE_QUERIES: tuple[str, ...] = (
    # A surveyed door, and the same door spelled the four ways a person types it.
    "1517 3rd Ave",
    "1517 THIRD AVENUE",
    "1517 3 av",
    "350 5th Ave",
    # A house number with no door of its own: the interpolation rung.
    "1519 3rd ave",
    "1518 3 Ave",
    # Outside every run of doors, and a street with no door at all.
    "9999 3 Ave",
    "1 Chisum Pl",
    # Corners, in both orders and with either half abbreviated.
    "Lexington Ave & 86th St",
    "86th St and Lexington Ave",
    "lex & 86",
    "E 86 St @ 3 Ave",
    "Broadway / W 72 St",
    # Two streets that share no node, which degrades to the two streets.
    "Lexington Ave & 3 Ave",
    # Bare streets, whole and half-typed.
    "Broadway",
    "broadwa",
    "5 Ave",
    "Ave of the Americas",
    "americas",
    "lex",
    "fdr",
    "E Houston St",
    "houston",
    # A street reached by a word buried in its name.
    "king",
    "malcolm",
    # Places, whole, half-typed and by a word in the middle.
    "Gracie Mansion",
    "gracie mans",
    "fashion",
    "hs fashion",
    "moma",
    "port authority",
    "grand central",
    # ZIPs.
    "10028",
    "10001",
    # Typos the edit-distance pass exists for, and one too short to correct.
    "Bleeker St",
    "Broadwya",
    "3 A",
    # What fingers do: a single letter, and a string that is not an address.
    "p",
    "c",
    "<script>alert(1)</script>",
)

# Twenty dropped pins: doors, corners, block middles, the parks, the rivers and
# one point outside coverage altogether.
REVERSE_PINS: tuple[tuple[float, float], ...] = (
    (-73.953985, 40.7788304),  # a corner on the Upper East Side
    (-73.9544835, 40.7781469),
    (-73.95385, 40.77883),  # a few metres off it
    (-73.9857, 40.7484),  # the Empire State Building
    (-73.9772, 40.7527),  # Grand Central
    (-73.9819, 40.7681),  # Lincoln Center
    (-74.0134, 40.7127),  # the World Trade Center
    (-73.9903, 40.7359),  # Union Square
    (-73.9665, 40.7812),  # the Met
    (-73.9654, 40.7829),  # the middle of Central Park
    (-73.9581, 40.7812),  # the Great Lawn
    (-73.9662, 40.7857),  # the reservoir, which is out of coverage
    (-74.0170, 40.7050),  # Battery Park
    (-73.9350, 40.8200),  # Harlem
    (-73.9262, 40.8687),  # Inwood
    (-73.9550, 40.7500),  # the East River off midtown
    (-74.0100, 40.7500),  # the Hudson off midtown
    (-73.9210, 40.8050),  # Randalls Island
    (-73.9600, 40.7600),  # Sutton Place
    (-74.0324, 40.7440),  # Hoboken, outside coverage entirely
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/curbcheck.sqlite"))
    args = parser.parse_args(argv)

    if not args.db.is_file():
        parser.error(f"no database at {args.db}")
    conn = db.connect(args.db, readonly=True)
    try:
        payload = {
            "geocode": [
                {"q": text, "candidates": [_candidate(c) for c in geocode(conn, text)]}
                for text in GEOCODE_QUERIES
            ],
            "reverse": [
                {"lon": lon, "lat": lat, "match": _reverse(conn, lon=lon, lat=lat)}
                for lon, lat in REVERSE_PINS
            ],
        }
    finally:
        conn.close()
    json.dump(payload, sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def _candidate(candidate: object) -> dict[str, object]:
    """`routes._candidate_payload`: the dataclass, with `kind` as its string value."""
    payload = asdict(candidate)
    payload["kind"] = payload["kind"].value
    return payload


def _reverse(conn: object, *, lon: float, lat: float) -> dict[str, object] | None:
    match = reverse_geocode(conn, lon=lon, lat=lat)
    if match is None:
        return None
    payload = asdict(match)
    payload["kind"] = payload["kind"].value
    return payload


if __name__ == "__main__":
    raise SystemExit(main())
