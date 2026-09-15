"""Is a point inside the only area this database can answer about?

The database holds Manhattan and nothing else, so a destination in New Jersey
is not a search that found nothing — it is a question this build cannot answer.
Reporting it as "nothing within that walk radius. Try a longer walk" told the
user to widen a radius that was never the problem (UX audit finding P0-3).

Coverage is measured against `street_segment`, the centerline every other table
hangs off, rather than against a hand-drawn borough polygon: the centerline is
the thing that actually decides whether a query can be answered, and it needs
no second dataset to stay in step with.
"""

from __future__ import annotations

import sqlite3

from shapely.geometry import Point

from curbcheck.engine.geo import degree_padding, measure

COVERAGE_AREA = "Manhattan"

# A point further than this from every centerline is out of coverage. Measured
# on the 2026-09-15 database: the deepest points of Central Park are 184 m
# (Great Lawn) and 177 m (the Ramble) from a centerline, so the park stays in;
# the Hudson midstream is 1,090 m and West New York, NJ has no segment within a
# kilometre, so they stay out. Two known edges: the centre of the Central Park
# reservoir is 287 m from anything and is refused, and the Manhattan-registered
# bridge centerlines reach far enough over the East River that DUMBO (240 m) is
# accepted. Both are wrong in the safe direction -- refusing a stretch of water,
# and answering about a block DOT does not letter for us, which returns nothing.
COVERAGE_RADIUS_M = 250.0

_PREFILTER_SQL = (
    "SELECT geom FROM street_segment"
    " WHERE max_lon >= ? AND min_lon <= ? AND max_lat >= ? AND min_lat <= ?"
)
_BBOX_SQL = "SELECT min(min_lon), min(min_lat), max(max_lon), max(max_lat) FROM street_segment"


def within_coverage(
    conn: sqlite3.Connection, *, lon: float, lat: float, radius_m: float = COVERAGE_RADIUS_M
) -> bool:
    """Whether (lon, lat) is within `radius_m` of any centerline segment.

    Two passes, the same shape as the radius query in `engine.search`: a bbox
    prefilter in SQL that decodes no geometry, then the exact distance in
    Python over whatever survives it. Returns on the first segment in range, so
    a point in the middle of the street costs one geometry.
    """
    lon_pad, lat_pad = degree_padding(lat, radius_m)
    rows = conn.execute(
        _PREFILTER_SQL, (lon - lon_pad, lon + lon_pad, lat - lat_pad, lat + lat_pad)
    ).fetchall()

    origin = Point(0.0, 0.0)
    for row in rows:
        measured = measure(row["geom"], lon=lon, lat=lat, origin=origin)
        if measured is not None and measured[1] <= radius_m:
            return True
    return False


def coverage_bbox(conn: sqlite3.Connection) -> tuple[float, float, float, float] | None:
    """(min_lon, min_lat, max_lon, max_lat) over every centerline, or None when there is none.

    The map draws this as the edge of what CurbCheck knows. It is the bounding
    box of the data, not of the borough: Roosevelt and Randalls Islands are in
    it because CSCL files them under Manhattan.
    """
    row = conn.execute(_BBOX_SQL).fetchone()
    if row is None or row[0] is None:
        return None
    return (float(row[0]), float(row[1]), float(row[2]), float(row[3]))
