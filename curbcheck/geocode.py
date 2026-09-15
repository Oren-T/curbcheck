"""Local address and intersection lookup over the centerline.

No network, by design: sending a destination address to a third-party geocoder
would leak exactly the thing this app exists to keep local (SPEC §3.1 threat
T6, §10). Everything here reads `street_segment` and `street_node`, which the
geometry ETL builds from CSCL.

Coverage is the honest limit of this module. `docs/DATA.md` §2.2 measures
address ranges (`l_low_hn`/`l_high_hn`/`r_low_hn`/`r_high_hn`) as present on
only **54.0%** of Manhattan centerline segments, so nearly half of all house
numbers cannot be interpolated at all. Those degrade to the nearest
hundred-block corner, and then to the street's midpoint, each at a lower
confidence, rather than to a confident wrong point.

The direction assumption: CSCL states the low house number at a segment's
first geometry vertex, so a house number's position within its range is used
directly as the normalized position along the line. Where a block was
digitized against its address direction the interpolated point lands at the
wrong end of that one block — under 300 ft of error, and never on the wrong
street.

This module is the seam between the ETL's street-name knowledge and the API:
it imports the ETL normalizer (one direction only, never the reverse) so that
a name typed by the user is folded exactly the way the ETL folded the data.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from shapely.geometry import LineString, MultiLineString, shape
from shapely.ops import linemerge

from curbcheck.db import json_string_list
from curbcheck.etl.streets import normalize_street_name

MAX_CANDIDATES = 5

# Longest query we will parse. A destination address is never this long; the
# cap keeps a pathological string out of the regexes.
MAX_QUERY_CHARS = 200

# Confidence by how the point was found, so the UI can rank and warn. An
# interpolated address is the only result worth calling precise; everything
# below 0.6 is "the right neighbourhood".
CONFIDENCE_INTERPOLATED = 0.9
CONFIDENCE_INTERPOLATED_TIED = 0.7
CONFIDENCE_INTERSECTION = 0.95
CONFIDENCE_NEAREST_BLOCK = 0.5
CONFIDENCE_STREET_MIDPOINT = 0.3

# "&", "and", "at", or a slash between two street names.
_INTERSECTION_SPLIT = re.compile(r"\s+(?:&|AND|AT)\s+|\s*/\s*")

# A leading house number, optionally hyphenated (Queens style, rare in
# Manhattan) or with a letter suffix ("123A REAR"), then the street.
_HOUSE_NUMBER = re.compile(r"^(\d{1,6})(?:-\d{1,6})?[A-Z]?\s+(.+)$")

# "85TH" -> "85". The ETL normalizer folds the spelled-out ordinals (FIFTH ->
# 5) but not the digit ones, because the source datasets never write those;
# people typing into the address box do.
_DIGIT_ORDINAL = re.compile(r"\b(\d+)(?:ST|ND|RD|TH)\b")

# "86 ST": a numbered cross street with the E/W the user did not type.
_BARE_NUMBERED_STREET = re.compile(r"^\d+ ST$")

_LEADING_DIGITS = re.compile(r"^\s*(\d+)")
_WHITESPACE = re.compile(r"\s+")

_SEGMENTS_BY_NAME_SQL = (
    "SELECT segment_id, street_name, geom, left_low_address, left_high_address,"
    " right_low_address, right_high_address FROM street_segment WHERE street_norm = ?"
)
_NODES_SQL = "SELECT node_id, lon, lat, street_names FROM street_node"


class GeocodeKind(StrEnum):
    """How a candidate was located. The UI shows intersections as approximate."""

    ADDRESS = "address"
    INTERSECTION = "intersection"


@dataclass(frozen=True)
class GeocodeCandidate:
    """One place the query might mean. `confidence` is 0-1, higher is better."""

    label: str
    lat: float
    lon: float
    kind: GeocodeKind
    confidence: float


@dataclass(frozen=True)
class AddressQuery:
    """A house number on a named street, both already normalized."""

    house_number: int
    street: str


@dataclass(frozen=True)
class IntersectionQuery:
    """Two normalized street names that should meet at a node."""

    first: str
    second: str


@dataclass(frozen=True)
class StreetQuery:
    """A bare normalized street name, with no house number."""

    street: str


ParsedQuery = AddressQuery | IntersectionQuery | StreetQuery


def geocode(
    conn: sqlite3.Connection, text: str, *, limit: int = MAX_CANDIDATES
) -> list[GeocodeCandidate]:
    """Best guesses at what `text` means, best first, at most `limit` of them.

    Returns an empty list for anything unparseable or unmatched: not finding an
    address is an answer, not an error. Never raises on hostile input.
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")
    query = parse_query(text)
    if query is None:
        return []

    candidates = _lookup(conn, query)
    if not candidates and isinstance(query, AddressQuery):
        # "5 AVE" parses as house 5 on street "AVE"; when that finds nothing,
        # the whole string was a street name after all.
        candidates = _lookup(conn, StreetQuery(street=_normalize(text)))

    candidates.sort(key=lambda candidate: (-candidate.confidence, candidate.label))
    return candidates[:limit]


def parse_query(text: str) -> ParsedQuery | None:
    """Classify a typed query as an address, an intersection, or a bare street.

    Returns None when the query is empty, over `MAX_QUERY_CHARS`, or has no
    street name left after normalization.
    """
    cleaned = _WHITESPACE.sub(" ", str(text)).strip().upper()
    if not cleaned or len(cleaned) > MAX_QUERY_CHARS:
        return None

    parts = [part for part in _INTERSECTION_SPLIT.split(cleaned) if part.strip()]
    if len(parts) >= 2:
        first, second = _normalize(parts[0]), _normalize(parts[1])
        return IntersectionQuery(first=first, second=second) if first and second else None

    house_match = _HOUSE_NUMBER.match(cleaned)
    if house_match:
        street = _normalize(house_match.group(2))
        if street:
            return AddressQuery(house_number=int(house_match.group(1)), street=street)

    street = _normalize(cleaned)
    return StreetQuery(street=street) if street else None


def _lookup(conn: sqlite3.Connection, query: ParsedQuery) -> list[GeocodeCandidate]:
    if isinstance(query, IntersectionQuery):
        return _intersection_candidates(conn, query)
    if isinstance(query, AddressQuery):
        return _address_candidates(conn, query)
    return _street_midpoint_candidates(conn, query.street)


def _normalize(raw: str) -> str:
    """Fold a user-typed street name into the ETL's canonical spelling."""
    return normalize_street_name(_DIGIT_ORDINAL.sub(r"\1", raw.upper()))


def _street_variants(street: str) -> list[str]:
    """The names to try for one typed street.

    Every Manhattan cross street is named `E 86 ST` or `W 86 ST`, but people
    type "86th St" and mean whichever side of Fifth Avenue they are on. When
    the directional is missing, try both and let the caller keep whatever the
    data actually has.
    """
    if _BARE_NUMBERED_STREET.match(street):
        return [street, f"E {street}", f"W {street}"]
    return [street]


@dataclass(frozen=True)
class _Blockface:
    """One side of one centerline segment, with its house-number range."""

    segment_id: str
    street_name: str
    geom: str
    low: int
    high: int

    def contains(self, house_number: int) -> bool:
        """Whether this side carries `house_number`, parity included.

        NYC puts odd numbers on one side of the street and even on the other,
        so a range whose ends agree on parity only claims numbers of that
        parity. Ranges whose ends disagree are data errors; those claim both.
        """
        if not self.low <= house_number <= self.high:
            return False
        if self.low % 2 != self.high % 2:
            return True
        return house_number % 2 == self.low % 2

    def position(self, house_number: int) -> float:
        """Where in [0, 1] `house_number` falls within the range."""
        if self.high == self.low:
            return 0.5
        return (house_number - self.low) / (self.high - self.low)

    @property
    def midpoint_number(self) -> float:
        return (self.low + self.high) / 2


def _address_candidates(conn: sqlite3.Connection, query: AddressQuery) -> list[GeocodeCandidate]:
    blockfaces = _blockfaces(conn, query.street)
    hits = [face for face in blockfaces if face.contains(query.house_number)]

    # Two blockfaces claiming one house number means the source ranges overlap.
    # Offer both rather than picking, and say we are less sure.
    confidence = CONFIDENCE_INTERPOLATED if len(hits) == 1 else CONFIDENCE_INTERPOLATED_TIED
    candidates = []
    for face in hits:
        point = _point_along(face.geom, face.position(query.house_number))
        if point is None:
            continue
        lon, lat = point
        candidates.append(
            GeocodeCandidate(
                label=f"{query.house_number} {face.street_name}",
                lat=lat,
                lon=lon,
                kind=GeocodeKind.ADDRESS,
                confidence=confidence,
            )
        )
    if candidates:
        return candidates

    nearest = _nearest_block_candidates(query, blockfaces)
    return nearest or _street_midpoint_candidates(conn, query.street)


def _nearest_block_candidates(
    query: AddressQuery, blockfaces: list[_Blockface]
) -> list[GeocodeCandidate]:
    """The corner of the block whose numbers come closest to the house number.

    The fallback for the 46% of segments with no published range: if some other
    block of the same street carries numbers near this one, its nearer end is
    the right hundred-block corner even though this exact number is unlisted.
    """
    if not blockfaces:
        return []
    closest = min(blockfaces, key=lambda face: abs(face.midpoint_number - query.house_number))
    line = _line(closest.geom)
    if line is None:
        return []
    # Take the end of the block that the house number is counting towards.
    lon, lat = (line.coords[0] if query.house_number < closest.low else line.coords[-1])[:2]
    return [
        GeocodeCandidate(
            label=f"near {query.house_number} {closest.street_name}",
            lat=float(lat),
            lon=float(lon),
            kind=GeocodeKind.INTERSECTION,
            confidence=CONFIDENCE_NEAREST_BLOCK,
        )
    ]


def _street_midpoint_candidates(conn: sqlite3.Connection, street: str) -> list[GeocodeCandidate]:
    """The middle of the street, for a street we know but a number we cannot place."""
    midpoints = []
    display_name = street
    for row in _segment_rows(conn, street):
        line = _line(str(row["geom"]))
        if line is None:
            continue
        display_name = str(row["street_name"])
        midpoints.append(line.interpolate(0.5, normalized=True))
    if not midpoints:
        return []

    mean_lon = sum(point.x for point in midpoints) / len(midpoints)
    mean_lat = sum(point.y for point in midpoints) / len(midpoints)
    # Snap to a real segment so the pin lands on the street and not in the
    # river, which the mean of a curving street can easily do.
    centre = min(midpoints, key=lambda point: (point.x - mean_lon) ** 2 + (point.y - mean_lat) ** 2)
    return [
        GeocodeCandidate(
            label=display_name,
            lat=float(centre.y),
            lon=float(centre.x),
            kind=GeocodeKind.INTERSECTION,
            confidence=CONFIDENCE_STREET_MIDPOINT,
        )
    ]


def _intersection_candidates(
    conn: sqlite3.Connection, query: IntersectionQuery
) -> list[GeocodeCandidate]:
    """Nodes where both streets meet.

    The whole node table is scanned and filtered in Python: `street_names` is a
    JSON list, Manhattan has 6,676 nodes (docs/DATA.md §2.3), and a scan avoids
    pattern-matching user text against a JSON blob in SQL.
    """
    first = set(_street_variants(query.first))
    second = set(_street_variants(query.second))
    candidates = []
    for row in conn.execute(_NODES_SQL):
        names = json_string_list(row["street_names"])
        normalized = {_normalize(name) for name in names}
        if not (first & normalized) or not (second & normalized):
            continue
        candidates.append(
            GeocodeCandidate(
                label=" & ".join(names[:2]) if len(names) >= 2 else (names[0] if names else ""),
                lat=float(row["lat"]),
                lon=float(row["lon"]),
                kind=GeocodeKind.INTERSECTION,
                confidence=CONFIDENCE_INTERSECTION,
            )
        )
    return candidates


def _blockfaces(conn: sqlite3.Connection, street: str) -> list[_Blockface]:
    """Both sides of every segment of `street` that publishes a house-number range."""
    faces = []
    for row in _segment_rows(conn, street):
        for low_column, high_column in (
            ("left_low_address", "left_high_address"),
            ("right_low_address", "right_high_address"),
        ):
            low = _house_int(row[low_column])
            high = _house_int(row[high_column])
            if low is None or high is None:
                continue
            faces.append(
                _Blockface(
                    segment_id=str(row["segment_id"]),
                    street_name=str(row["street_name"]),
                    geom=str(row["geom"]),
                    low=min(low, high),
                    high=max(low, high),
                )
            )
    return faces


def _segment_rows(conn: sqlite3.Connection, street: str) -> list[sqlite3.Row]:
    rows: list[sqlite3.Row] = []
    for variant in _street_variants(street):
        rows.extend(conn.execute(_SEGMENTS_BY_NAME_SQL, (variant,)).fetchall())
    return rows


def _point_along(geom: str, position: float) -> tuple[float, float] | None:
    line = _line(geom)
    if line is None:
        return None
    point = line.interpolate(min(max(position, 0.0), 1.0), normalized=True)
    return (float(point.x), float(point.y))


def _line(geom: str) -> LineString | None:
    """The segment as one line. CSCL publishes MultiLineStrings (docs/DATA.md §2.2)."""
    try:
        parsed = json.loads(geom)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    try:
        geometry = shape(parsed)
    except (AttributeError, KeyError, TypeError, ValueError):
        return None

    if isinstance(geometry, LineString):
        return geometry if not geometry.is_empty else None
    if isinstance(geometry, MultiLineString):
        merged = linemerge(geometry)
        if isinstance(merged, LineString):
            return merged
        # A segment whose parts do not touch: the longest part is the block.
        parts = list(merged.geoms)
        return max(parts, key=lambda part: part.length) if parts else None
    return None


def _house_int(value: Any) -> int | None:
    """Leading digits of a house-number cell. `'1510'`, `'1510 REAR'`, `''`, None."""
    if value is None:
        return None
    match = _LEADING_DIGITS.match(str(value))
    return int(match.group(1)) if match else None
