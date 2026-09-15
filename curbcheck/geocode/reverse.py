"""What a dropped pin is nearest to.

The inverse of `suggest`, and the only geocoder path that starts from a point
rather than from text: a surveyed door if one is close enough to be the honest
answer, otherwise the corner, otherwise the street the pin is on.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from shapely.geometry import LineString, Point

from curbcheck.db import json_string_list, read_snapshot
from curbcheck.engine.geo import M_PER_DEG_LAT, degree_padding, meters_per_degree_lon
from curbcheck.engine.labels import single_spaced
from curbcheck.engine.ranges import line_of
from curbcheck.geocode.candidates import DEFAULT_SECONDARY, GeocodeKind, ReverseMatch

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

_ADDRESS_IN_BOX_SQL = (
    "SELECT display, lon, lat FROM address_point WHERE lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?"
)
_SEGMENTS_IN_BBOX_SQL = (
    "SELECT ss.segment_id, ss.street_name, ss.geom FROM street_segment ss"
    " WHERE ss.max_lon >= ? AND ss.min_lon <= ? AND ss.max_lat >= ? AND ss.min_lat <= ?"
)
_NODES_IN_BBOX_SQL = (
    "SELECT node_id, lon, lat, street_names FROM street_node"
    " WHERE lon >= ? AND lon <= ? AND lat >= ? AND lat <= ?"
)


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
