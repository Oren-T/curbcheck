"""Address suggestions and reverse lookup, served entirely from the local index.

No network, by design: sending a destination to a third-party geocoder would
leak exactly the thing this app exists to keep local, and an autocomplete that
fires per keystroke would leak the typing rather than just the answer (SPEC
§3.1 threat T6, §10; `docs/ux/AUTOCOMPLETE_RESEARCH.md` §3 surveys the online
options and rejects all of them).

The index `etl.addresses` builds is a *vocabulary* index, so this module parses
first and looks up second. The only hard part of a Manhattan query is the
street name — the house number is an integer and the grammar is tiny — so the
ETL pre-expands every street into the spellings a person might type and a
typed street resolves in one prefix range-scan. Tolerance falls out of that:
`1519 third avenue`, `1519 3rd ave` and `1519 3rd av` fold to one key, `86 & 3`
and `lex & 86` reach the same node, and a typo costs an edit-distance scan that
only runs when the exact and prefix passes came back empty.

The answer ladder, best first, with what each rung is worth:

| rung | confidence | what it is |
|---|---|---|
| address point | 0.98 | a surveyed door from OTI AddressPoint |
| intersection | 0.95 | a centerline node both streets meet at |
| place | 0.85 scaled by name coverage | a CommonPlace feature name |
| interpolated | 0.75 | between two surveyed same-parity neighbours |
| near a door | 0.60 | the closest surveyed number on that street |
| address range | 0.50 | CSCL's own range, for a street with no doors |
| street | 0.70 / 0.45 / 0.30 | named exactly, reached by prefix, or half of a failed corner |
| ZIP | 0.25 | the centre of a ZIP code |

This module is the seam between the ETL's street-name knowledge and the API:
it imports the ETL's `fold` (one direction only, never the reverse) so a name
typed by the user is folded exactly the way the ETL folded the data.
"""

from __future__ import annotations

import math
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from shapely.geometry import LineString, Point

from curbcheck.db import json_string_list, placeholders, read_snapshot
from curbcheck.engine.coverage import COVERAGE_AREA, within_coverage
from curbcheck.engine.geo import M_PER_DEG_LAT, degree_padding, meters_per_degree_lon
from curbcheck.engine.labels import single_spaced
from curbcheck.engine.ranges import Blockface, blockfaces, line_of, point_along
from curbcheck.etl.addresses import fold, place_words

# Eight is the longest list a person scans without reading it as a search
# result page rather than as a disambiguation (docs/ux/UX_AUDIT.md P1-5 asks
# for candidates to always be offered, which only works if the list stays
# glanceable).
MAX_CANDIDATES = 8

# Everything in the database is in one borough, so the second line of a
# candidate says so when it has nothing more specific to say.
DEFAULT_SECONDARY = COVERAGE_AREA

# A pin closer than this to a surveyed door is answered with that door.
# 60 m is about one and a half Manhattan lots: further out, the corner is the
# honest answer, because a door across the street is not where the pin is.
REVERSE_ADDRESS_MAX_M = 60.0

# How far a reverse lookup will look for a corner or a block at all. Beyond
# `engine.coverage.COVERAGE_RADIUS_M` the point is not in coverage anyway.
REVERSE_SEARCH_M = 250.0

# Bounding boxes the reverse lookup tries in turn, in degrees (~110 m, ~440 m,
# ~1.8 km). The first covers `REVERSE_ADDRESS_MAX_M` with room to spare; the
# wider two only ever run over the rivers and the middle of the parks.
_REVERSE_BOX_DEGREES = (0.001, 0.004, 0.016)

# Longest query we will parse. A Manhattan address is never close to this; the
# cap keeps a pathological string out of the regexes and out of the index.
MAX_QUERY_CHARS = 120

# How far the street resolver will fan out. A half-typed street matches many
# variants ("3" is a prefix of 3 AVE, 3 ST, 30 AVE…); these bound the work a
# single keystroke can cause, and the confidence ladder sorts out what survives.
MAX_PREFIX_VARIANTS = 40
MAX_STREETS_PER_ADDRESS = 6
MAX_STREETS_PER_SIDE = 8
MAX_FUZZY_STREETS = 10
MAX_PLACE_CANDIDATES = 60

CONFIDENCE_ADDRESS_POINT = 0.98
CONFIDENCE_INTERSECTION = 0.95
CONFIDENCE_PLACE = 0.85
CONFIDENCE_ADDRESS_INTERPOLATED = 0.75
CONFIDENCE_NEAR_ADDRESS = 0.60
CONFIDENCE_ADDRESS_RANGE = 0.50
# A street whose spelling the query matches exactly ("5 ave", "broadway") is
# what the user is naming, and has to outrank a place whose name merely
# contains those words: at 0.45 the query "5 ave" answered 5 AVE SYNAGOGUE
# (0.85 x 2 of its 3 words) before Fifth Avenue. A street reached by prefix
# ("broadwa") is still a guess about what is being typed, and one offered
# because half of an unmatched intersection hit it is a consolation prize that
# ranks below a partial place-name match.
CONFIDENCE_STREET_EXACT = 0.70
CONFIDENCE_STREET_WHOLE_QUERY = 0.45
CONFIDENCE_STREET_HALF_QUERY = 0.30
CONFIDENCE_ZIP = 0.25

# A place name matching only some of its own words is a weak hit ("86 ST"
# matches "CTL PK W DR OV 86 ST TRNVS RD"), so the score scales with how much
# of the place's name the query accounted for, and weak hits are dropped.
MIN_PLACE_COVERAGE = 0.5

# A fuzzy street match is a guess about what the user meant, so it costs a
# fifth of the candidate's confidence rather than being silently as good.
FUZZY_CONFIDENCE_PENALTY = 0.8

# A bracket wider than one hundred-block is two different blocks with a gap
# between them, not a run of missing doors; interpolating across it would
# invent a location rather than fill one in.
MAX_INTERPOLATION_SPAN = 100

# "&", "and", "at", "@" or a slash between two street names.
_INTERSECTION_SPLIT = re.compile(r"\s+(?:&|AND|AT)\s+|\s*[&@/]\s*")

# A leading house number, optionally hyphenated (Queens style, rare in
# Manhattan) or with a letter suffix ("123A REAR"), then the street.
_HOUSE_NUMBER = re.compile(r"^(\d{1,6})(?:-\d{1,6})?[A-Z]?\s+(.+)$")

_ZIP = re.compile(r"^\d{5}$")

# C0 and C1 controls, including the NULs and bidi-adjacent bytes the fuzzer
# splices in. Replaced with a space rather than removed, so "A\x00B" cannot
# become the word "AB".
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_WHITESPACE = re.compile(r"\s+")

_VARIANT_EXACT_SQL = "SELECT street_norm FROM street_variant WHERE variant = ? ORDER BY street_norm"
_VARIANT_PREFIX_SQL = (
    "SELECT DISTINCT street_norm FROM street_variant WHERE variant >= ? AND variant < ?"
    " ORDER BY variant, street_norm LIMIT ?"
)
_VARIANTS_SQL = "SELECT variant, street_norm FROM street_variant"
_STREET_SQL = "SELECT display, lon, lat FROM street WHERE street_norm = ?"
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
_PLACES_BY_ID_SQL = "SELECT display, lon, lat, token_count FROM place WHERE place_id IN ("
_ADDRESS_IN_BOX_SQL = (
    "SELECT display, lon, lat FROM address_point WHERE lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?"
)
_INTERSECTION_SQL = (
    "SELECT display, lon, lat FROM intersection WHERE a_norm = ? AND b_norm = ? LIMIT ?"
)
_ZIP_SQL = "SELECT lon, lat, address_points FROM zip_centroid WHERE zipcode = ?"
# S105 reads "token" in these table and column names as a credential; it is
# a word of a place name.
_PLACE_TOKEN_EXACT_SQL = "SELECT place_id FROM place_token WHERE token = ?"  # noqa: S105
_PLACE_TOKEN_PREFIX_SQL = "SELECT place_id FROM place_token WHERE token >= ? AND token < ?"  # noqa: S105
_SEGMENTS_IN_BBOX_SQL = (
    "SELECT ss.segment_id, ss.street_name, ss.geom FROM street_segment ss"
    " WHERE ss.max_lon >= ? AND ss.min_lon <= ? AND ss.max_lat >= ? AND ss.min_lat <= ?"
)
_NODES_IN_BBOX_SQL = (
    "SELECT node_id, lon, lat, street_names FROM street_node"
    " WHERE lon >= ? AND lon <= ? AND lat >= ? AND lat <= ?"
)


class GeocodeKind(StrEnum):
    """What a candidate *is*, which is what decides the icon and the second line.

    PIN is the one value this module never returns: it is what the frontend
    labels a crosshair the user has not dropped yet. Everything else here is
    produced by `suggest` or `reverse_geocode`.
    """

    ADDRESS = "address"
    INTERSECTION = "intersection"
    STREET = "street"
    ZIP = "zip"
    PLACE = "place"
    PIN = "pin"


@dataclass(frozen=True)
class GeocodeCandidate:
    """One place the query might mean. `confidence` is 0-1, higher is better.

    `label` is the line the user reads and `secondary` the muted line under it:
    the ZIP, how the point was arrived at, or the borough when there is nothing
    narrower to say. Both are the city's own capitals and are untrusted text.
    """

    label: str
    lat: float
    lon: float
    kind: GeocodeKind
    confidence: float
    secondary: str | None = DEFAULT_SECONDARY


@dataclass(frozen=True)
class ReverseMatch:
    """What a dropped pin is nearest to. `distance_m` is to the returned point."""

    label: str
    secondary: str | None
    kind: GeocodeKind
    lat: float
    lon: float
    distance_m: float


@dataclass(frozen=True)
class AddressQuery:
    """A house number on a named street, the street already folded."""

    house_number: int
    street: str


@dataclass(frozen=True)
class IntersectionQuery:
    """Two folded street names that should meet at a node."""

    first: str
    second: str


@dataclass(frozen=True)
class StreetQuery:
    """A bare folded street name, with no house number. Also the place-name case."""

    street: str


@dataclass(frozen=True)
class ZipQuery:
    """Five digits, which in Manhattan can only be a ZIP code."""

    zipcode: str


ParsedQuery = AddressQuery | IntersectionQuery | StreetQuery | ZipQuery


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
    than in either.
    """
    return _in_coverage(conn, suggest(conn, text, limit=limit), limit)


def clean_query(text: str) -> str:
    """Upper-cased, control-free, single-spaced query text, or "" when there is none.

    Over `MAX_QUERY_CHARS` returns "", which every caller reads as "no query":
    a 400-character paste is not an address and is not worth a regex.
    """
    cleaned = _WHITESPACE.sub(" ", _CONTROL_CHARS.sub(" ", str(text))).strip().upper()
    return "" if len(cleaned) > MAX_QUERY_CHARS else cleaned


def parse_query(text: str) -> ParsedQuery | None:
    """Classify a query as a ZIP, an intersection, an address, or a bare street.

    Returns None when the query is empty, over `MAX_QUERY_CHARS`, or has no
    street name left after folding. The classification is the *primary* reading
    only: `suggest` falls back to the street and place passes when an address
    reading finds nothing, because "350 5th" and "1 police plaza" both parse as
    addresses and only one of them is one.
    """
    cleaned = clean_query(text)
    if not cleaned:
        return None

    if _ZIP.match(cleaned):
        return ZipQuery(zipcode=cleaned)

    parts = [part.strip() for part in _INTERSECTION_SPLIT.split(cleaned) if part.strip()]
    if len(parts) >= 2:
        first, second = fold(parts[0]), fold(parts[1])
        return IntersectionQuery(first=first, second=second) if first and second else None

    house_match = _HOUSE_NUMBER.match(cleaned)
    if house_match:
        street = fold(house_match.group(2))
        if street:
            return AddressQuery(house_number=int(house_match.group(1)), street=street)

    street = fold(cleaned)
    return StreetQuery(street=street) if street else None


def _lookup(
    conn: sqlite3.Connection, query: ParsedQuery, cleaned: str, limit: int
) -> list[GeocodeCandidate]:
    if isinstance(query, ZipQuery):
        return _zip_candidates(conn, query.zipcode)
    if isinstance(query, IntersectionQuery):
        corners = _intersection_candidates(conn, query, limit)
        if corners:
            return corners
        # Two streets that never share a centerline node. Offering each one
        # separately is honest; claiming a corner that is not in the data
        # would not be.
        return [
            *_street_candidates(conn, query.first, limit, CONFIDENCE_STREET_HALF_QUERY),
            *_street_candidates(conn, query.second, limit, CONFIDENCE_STREET_HALF_QUERY),
        ]
    whole = fold(cleaned)
    named_exactly = _names_a_street(conn, whole)
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
        *_street_candidates(conn, whole, limit, base),
        *_place_candidates(conn, cleaned, limit),
    ]


def _names_a_street(conn: sqlite3.Connection, folded: str) -> bool:
    """Whether the whole query is, exactly, a spelling of a street we index."""
    return bool(folded) and conn.execute(_VARIANT_EXACT_SQL, (folded,)).fetchone() is not None


def _address_or_its_street(
    conn: sqlite3.Connection, query: AddressQuery, cleaned: str, limit: int
) -> list[GeocodeCandidate]:
    """The house number if anything can place it, otherwise the street it named.

    The streets are resolved once and reused, so a query that matches nothing
    pays for the edit-distance scan once rather than twice.
    """
    streets = resolve_street(conn, query.street)[:MAX_STREETS_PER_ADDRESS]
    found = _address_candidates(conn, streets, query.house_number, limit)
    if found:
        return found
    # "200 E 85 ST" on a street with neither a surveyed door nor a published
    # range is E 85 ST, not nothing.
    return [
        *_streets_from(conn, streets, CONFIDENCE_STREET_HALF_QUERY),
        *_place_candidates(conn, cleaned, limit),
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
        if within_coverage(conn, lon=candidate.lon, lat=candidate.lat):
            kept.append(candidate)
    return kept


# --- resolving the street a user typed -----------------------------------


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
            _VARIANT_PREFIX_SQL, (folded, _prefix_bound(folded), MAX_PREFIX_VARIANTS)
        )
    ]
    seen = set(exact)
    ordered = exact + [norm for norm in prefixed if norm not in seen]
    if ordered:
        return [(norm, False) for norm in ordered]
    return [(norm, True) for norm in _fuzzy_streets(conn, folded)]


def _prefix_bound(prefix: str) -> str:
    """The exclusive upper end of a `LIKE prefix%` range, so it can be a range scan.

    A range scan has no pattern language in it, which is the point: there is no
    metacharacter for a user to escape or forget to escape (threat T3).
    """
    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


def _fuzzy_streets(conn: sqlite3.Connection, folded: str) -> list[str]:
    """Street norms whose variant is within one edit of `folded`.

    A linear scan over the ~2,800 variants, filtered on length first. Measured
    at about 3 ms, and it only runs when the exact and prefix passes found
    nothing at all.
    """
    matches: set[str] = set()
    for variant, street_norm in conn.execute(_VARIANTS_SQL):
        if abs(len(variant) - len(folded)) > 1:
            continue
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


def _street_display(conn: sqlite3.Connection, street_norm: str) -> str:
    row = conn.execute(_STREET_SQL, (street_norm,)).fetchone()
    return single_spaced(str(row[0])) if row is not None else street_norm


# --- the address ladder ---------------------------------------------------


def _address_candidates(
    conn: sqlite3.Connection,
    streets: Sequence[tuple[str, bool]],
    house_number: int,
    limit: int,
) -> list[GeocodeCandidate]:
    """The best rung each candidate street can offer for this house number."""
    found: list[GeocodeCandidate] = []
    for street_norm, fuzzy in streets:
        penalty = FUZZY_CONFIDENCE_PENALTY if fuzzy else 1.0
        rung = _best_rung(conn, street_norm, house_number, limit)
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


def _best_rung(
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
            secondary=_zip_secondary(zipcode),
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
        label=f"{house_number} {_street_display(conn, street_norm)}",
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


# --- the other kinds ------------------------------------------------------


def _intersection_candidates(
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


def _street_candidates(
    conn: sqlite3.Connection, folded: str, limit: int, base: float
) -> list[GeocodeCandidate]:
    """The street itself. `base` drops when the street is only half of what was typed."""
    return _streets_from(conn, resolve_street(conn, folded)[:limit], base)


def _streets_from(
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


def _place_candidates(conn: sqlite3.Connection, cleaned: str, limit: int) -> list[GeocodeCandidate]:
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
            (token, _prefix_bound(token)) if last else (token,),
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


def _zip_candidates(conn: sqlite3.Connection, zipcode: str) -> list[GeocodeCandidate]:
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


def _zip_secondary(zipcode: object) -> str:
    text = str(zipcode or "").strip()
    return f"{DEFAULT_SECONDARY} {text}" if text else DEFAULT_SECONDARY


# --- reverse --------------------------------------------------------------


def reverse_geocode(conn: sqlite3.Connection, *, lon: float, lat: float) -> ReverseMatch | None:
    """What a dropped pin is nearest to, or None when nothing is near it at all.

    The nearest surveyed door within `REVERSE_ADDRESS_MAX_M`, otherwise the
    nearest corner, otherwise the street the pin is on. A pin is the one
    destination the user cannot read back to themselves, and
    "40.778830, -73.953985" in the status line is not a place (UX audit P1-4,
    P1-6).
    """
    if not (math.isfinite(lon) and math.isfinite(lat)):
        return None
    with read_snapshot(conn):
        door = _nearest_address_point(conn, lon=lon, lat=lat)
        if door is not None:
            return door
        corner = _nearest_corner(conn, lon=lon, lat=lat)
        if corner is not None:
            return corner
        return _nearest_street(_segments_near(conn, lon=lon, lat=lat), lon=lon, lat=lat)


def _nearest_address_point(
    conn: sqlite3.Connection, *, lon: float, lat: float
) -> ReverseMatch | None:
    """The closest AddressPoint within `REVERSE_ADDRESS_MAX_M`, as "near <door>".

    A bounding-box prefilter over the `(lon, lat)` covering index so the scan
    touches a few hundred rows rather than 63,245. The box is widened twice,
    which only matters over the rivers and the middle of the parks, and the
    distance test at the end is what enforces the 60 m rule.
    """
    scale_lon = meters_per_degree_lon(lat)
    for span in _REVERSE_BOX_DEGREES:
        rows = conn.execute(
            _ADDRESS_IN_BOX_SQL, (lon - span, lon + span, lat - span, lat + span)
        ).fetchall()
        if not rows:
            continue
        display, best_lon, best_lat = min(
            rows, key=lambda row: _distance_m(lon, lat, float(row[1]), float(row[2]), scale_lon)
        )
        distance_m = _distance_m(lon, lat, float(best_lon), float(best_lat), scale_lon)
        if distance_m > REVERSE_ADDRESS_MAX_M:
            return None
        return ReverseMatch(
            label=f"near {single_spaced(str(display))}",
            secondary=DEFAULT_SECONDARY,
            kind=GeocodeKind.ADDRESS,
            lat=float(best_lat),
            lon=float(best_lon),
            distance_m=round(distance_m, 1),
        )
    return None


def _nearest_corner(conn: sqlite3.Connection, *, lon: float, lat: float) -> ReverseMatch | None:
    """The nearest centerline node that names at least one street."""
    lon_pad, lat_pad = degree_padding(lat, REVERSE_SEARCH_M)
    scale_lon = meters_per_degree_lon(lat)
    best: ReverseMatch | None = None
    for row in conn.execute(
        _NODES_IN_BBOX_SQL, (lon - lon_pad, lon + lon_pad, lat - lat_pad, lat + lat_pad)
    ):
        names = json_string_list(row["street_names"])
        if not names:
            continue
        node_lon, node_lat = float(row["lon"]), float(row["lat"])
        distance_m = _distance_m(lon, lat, node_lon, node_lat, scale_lon)
        if distance_m > REVERSE_SEARCH_M or (best is not None and distance_m >= best.distance_m):
            continue
        best = ReverseMatch(
            label=_node_label(names),
            secondary=DEFAULT_SECONDARY,
            kind=GeocodeKind.INTERSECTION,
            lat=node_lat,
            lon=node_lon,
            distance_m=round(distance_m, 1),
        )
    return best


@dataclass(frozen=True)
class _NearbySegment:
    """One centerline segment in local metres, with the row it came from."""

    row: sqlite3.Row
    line: LineString
    distance_m: float


def _segments_near(conn: sqlite3.Connection, *, lon: float, lat: float) -> list[_NearbySegment]:
    """Every centerline within `REVERSE_SEARCH_M`, nearest first, in local metres."""
    lon_pad, lat_pad = degree_padding(lat, REVERSE_SEARCH_M)
    rows = conn.execute(
        _SEGMENTS_IN_BBOX_SQL, (lon - lon_pad, lon + lon_pad, lat - lat_pad, lat + lat_pad)
    ).fetchall()

    origin = Point(0.0, 0.0)
    nearby = []
    for row in rows:
        line = _local_line(str(row["geom"]), lon0=lon, lat0=lat)
        if line is None:
            continue
        distance_m = float(line.distance(origin))
        if distance_m <= REVERSE_SEARCH_M:
            nearby.append(_NearbySegment(row=row, line=line, distance_m=distance_m))
    nearby.sort(key=lambda segment: (segment.distance_m, str(segment.row["segment_id"])))
    return nearby


def _nearest_street(
    nearby: Sequence[_NearbySegment], *, lon: float, lat: float
) -> ReverseMatch | None:
    """The street the pin is on, for a block with no door and no corner near it."""
    if not nearby:
        return None
    segment = nearby[0]
    point = segment.line.interpolate(segment.line.project(Point(0.0, 0.0)))
    return ReverseMatch(
        label=single_spaced(str(segment.row["street_name"])),
        secondary=DEFAULT_SECONDARY,
        kind=GeocodeKind.STREET,
        lat=lat + point.y / M_PER_DEG_LAT,
        lon=lon + point.x / meters_per_degree_lon(lat),
        distance_m=round(segment.distance_m, 1),
    )


def _local_line(geom: str, *, lon0: float, lat0: float) -> LineString | None:
    line = line_of(geom)
    if line is None:
        return None
    scale_lon = meters_per_degree_lon(lat0)
    return LineString(
        [((x - lon0) * scale_lon, (y - lat0) * M_PER_DEG_LAT) for x, y in line.coords]
    )


def _node_label(names: Sequence[str]) -> str:
    if len(names) >= 2:
        return " & ".join(single_spaced(name) for name in names[:2])
    return single_spaced(names[0]) if names else ""


def _distance_m(lon_a: float, lat_a: float, lon_b: float, lat_b: float, scale_lon: float) -> float:
    """Flat-earth metres between two nearby points. Under a metre of error over 1 km."""
    return math.hypot((lon_a - lon_b) * scale_lon, (lat_a - lat_b) * M_PER_DEG_LAT)
