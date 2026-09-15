"""House numbers placed along a centerline segment's published address ranges.

The last rung of the address ladder. OTI's AddressPoint files doors on 784 of
the 1,017 streets the centerline knows (docs/DATA.md §5.1); for the other 233
the only thing that has an opinion about where number 12 is, is CSCL's own
`l_low_hn`/`l_high_hn` pair, and 36 of those streets publish one. Accuracy is
the reason it ranks last: over 800 random doors this lands a median 95 ft from
the surveyed point, p95 1,634 ft, against 0 ft for a door AddressPoint has.

The direction assumption: CSCL states the low house number at a segment's first
geometry vertex, so a house number's position within its range is used directly
as the normalized position along the line. Where a block was digitized against
its address direction the point lands at the wrong end of that one block —
under 300 ft of error, and never on the wrong street.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from shapely.geometry import LineString, MultiLineString, shape
from shapely.ops import linemerge

from curbcheck.engine.labels import between_phrase, single_spaced

_LEADING_DIGITS = re.compile(r"^\s*(\d+)")

# The cross-street names come along on every segment read: they are the second
# line under a candidate, and a second query per candidate to fetch them would
# be one per keystroke once the box autocompletes.
_SEGMENTS_BY_NAME_SQL = (
    "SELECT ss.segment_id, ss.street_name, ss.geom, ss.left_low_address, ss.left_high_address,"
    " ss.right_low_address, ss.right_high_address, fn.street_names AS from_names,"
    " tn.street_names AS to_names FROM street_segment ss"
    " LEFT JOIN street_node fn ON fn.node_id = ss.from_node"
    " LEFT JOIN street_node tn ON tn.node_id = ss.to_node"
    " WHERE ss.street_norm = ?"
    " AND (ss.left_low_address IS NOT NULL OR ss.right_low_address IS NOT NULL)"
)


@dataclass(frozen=True)
class Blockface:
    """One side of one centerline segment, with the house-number range it publishes."""

    segment_id: str
    street_name: str
    geom: str
    low: int
    high: int
    cross_streets: str | None

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


def blockfaces(conn: sqlite3.Connection, street_norm: str) -> list[Blockface]:
    """Both sides of every segment of `street_norm` that publishes a house-number range."""
    faces = []
    for row in conn.execute(_SEGMENTS_BY_NAME_SQL, (street_norm,)):
        for low_column, high_column in (
            ("left_low_address", "left_high_address"),
            ("right_low_address", "right_high_address"),
        ):
            low = house_int(row[low_column])
            high = house_int(row[high_column])
            if low is None or high is None:
                continue
            faces.append(
                Blockface(
                    segment_id=str(row["segment_id"]),
                    street_name=str(row["street_name"]),
                    geom=str(row["geom"]),
                    low=min(low, high),
                    high=max(low, high),
                    cross_streets=_cross_streets(row),
                )
            )
    return faces


def point_along(geom: str, position: float) -> tuple[float, float] | None:
    """(lon, lat) a fraction of the way along a segment's geometry."""
    line = line_of(geom)
    if line is None:
        return None
    point = line.interpolate(min(max(position, 0.0), 1.0), normalized=True)
    return (float(point.x), float(point.y))


def line_of(geom: str) -> LineString | None:
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


def house_int(value: Any) -> int | None:
    """Leading digits of a house-number cell. `'1510'`, `'1510 REAR'`, `''`, None."""
    if value is None:
        return None
    match = _LEADING_DIGITS.match(str(value))
    return int(match.group(1)) if match else None


def _cross_streets(row: sqlite3.Row) -> str | None:
    """The block's cross streets as a phrase, or None when neither node names one."""
    return between_phrase(
        single_spaced(str(row["street_name"])), row["from_names"], row["to_names"]
    )
