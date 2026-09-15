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
from dataclasses import dataclass
from pathlib import Path

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

# Both sides of the box are constrained, including the lower bound on the
# segment's own `min_lon` / `min_lat`, so SQLite can seek into
# `ix_street_segment_lon` / `_lat` instead of scanning every row whose box
# starts anywhere before the query box. The lower bound is the widest single
# segment in the file: a segment whose box reaches the query box cannot begin
# more than one segment-width before it. On the 2026-09-15 database (11,102
# segments) the widest is 0.0115 deg of longitude and 0.0169 of latitude, so
# the scan is a ~2% slice of the index rather than half of it.
_PREFILTER_SQL = (
    "SELECT geom FROM street_segment"
    " WHERE min_lon >= ? AND min_lon <= ? AND max_lon >= ?"
    " AND min_lat >= ? AND min_lat <= ? AND max_lat >= ?"
)
# Written as two statements rather than one so each is answered from the
# covering index it fits, which is the difference between reading the index and
# reading all 94 MB of the table's geometry.
_LON_EXTENT_SQL = "SELECT min(min_lon), max(max_lon), max(max_lon - min_lon) FROM street_segment"
_LAT_EXTENT_SQL = "SELECT min(min_lat), max(max_lat), max(max_lat - min_lat) FROM street_segment"


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
    extent = coverage_extent(conn)
    rows = conn.execute(
        _PREFILTER_SQL,
        (
            lon - lon_pad - extent.lon_span,
            lon + lon_pad,
            lon - lon_pad,
            lat - lat_pad - extent.lat_span,
            lat + lat_pad,
            lat - lat_pad,
        ),
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
    return coverage_extent(conn).bbox


@dataclass(frozen=True)
class CoverageExtent:
    """What the centerline table says about its own reach, in degrees.

    `bbox` is the edge of what this database knows. `lon_span` and `lat_span`
    are the widest single segment's bounding box, which is what lets the
    prefilter bound `min_lon` and `min_lat` from below.
    """

    bbox: tuple[float, float, float, float] | None
    lon_span: float
    lat_span: float


def coverage_extent(conn: sqlite3.Connection) -> CoverageExtent:
    """The extent of `street_segment`, read once per database file.

    Two aggregate index scans, which on the data mount cost ~110 ms together —
    worth paying once and never per request, because every coverage check and
    `/api/health` want the same two numbers. The cache is keyed on the file's
    inode, mtime and size, since `curbcheck sync` renames a new database over
    the old one under a running server (`db.swap_in`) and the answer changes
    with it. A database with no file behind it — `:memory:`, i.e. the tests —
    is measured every time, because there is nothing stable to key on.
    """
    global _CACHED_EXTENT
    key = _file_key(conn)
    if key is not None and _CACHED_EXTENT is not None and _CACHED_EXTENT[0] == key:
        return _CACHED_EXTENT[1]

    lon_row = conn.execute(_LON_EXTENT_SQL).fetchone()
    lat_row = conn.execute(_LAT_EXTENT_SQL).fetchone()
    empty = lon_row is None or lon_row[0] is None or lat_row is None or lat_row[0] is None
    extent = (
        CoverageExtent(bbox=None, lon_span=0.0, lat_span=0.0)
        if empty
        else CoverageExtent(
            bbox=(float(lon_row[0]), float(lat_row[0]), float(lon_row[1]), float(lat_row[1])),
            lon_span=float(lon_row[2]),
            lat_span=float(lat_row[2]),
        )
    )
    if key is not None:
        _CACHED_EXTENT = (key, extent)
    return extent


# One slot, not a dict: a server reads one database, and a cache that cannot
# grow cannot leak across the thousands of temporary databases the tests build.
_CACHED_EXTENT: tuple[tuple[str, int, int, int], CoverageExtent] | None = None


def _file_key(conn: sqlite3.Connection) -> tuple[str, int, int, int] | None:
    """Identity of the file behind `main`, or None when there is no file."""
    filename = ""
    for row in conn.execute("PRAGMA database_list"):
        if str(row[1]) == "main":
            filename = str(row[2])
    if not filename:
        return None
    try:
        stamp = Path(filename).stat()
    except OSError:
        return None
    return (filename, stamp.st_ino, stamp.st_mtime_ns, stamp.st_size)
