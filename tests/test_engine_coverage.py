"""Coverage: which points this database can answer about at all.

A destination outside Manhattan used to be answered with "nothing within that
walk radius. Try a longer walk", which advises widening a radius that was never
the problem (docs/ux/UX_AUDIT.md P0-3).
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from curbcheck import db
from curbcheck.engine.coverage import COVERAGE_RADIUS_M, coverage_bbox, within_coverage

# 3 AVE between E 85 ST and E 86 ST, the worked example in docs/DATA.md §2.3.
NODE_85 = (-73.9544835, 40.7781469)
NODE_86 = (-73.9539850, 40.7788304)

# One degree of latitude is 111,132 m here, so 0.0100 deg is 1,111 m north.
FAR_NORTH = (NODE_85[0], NODE_85[1] + 0.0100)


def add_segment(
    conn: sqlite3.Connection,
    segment_id: str,
    start: tuple[float, float],
    end: tuple[float, float],
) -> None:
    geom = json.dumps({"type": "LineString", "coordinates": [list(start), list(end)]})
    min_lon, min_lat, max_lon, max_lat = db.geojson_bbox(geom)
    conn.execute(
        "INSERT INTO street_segment (segment_id, street_name, street_norm, geom,"
        " min_lon, min_lat, max_lon, max_lat) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (segment_id, "3 AVE", "3 AVE", geom, min_lon, min_lat, max_lon, max_lat),
    )


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = db.connect(":memory:")
    db.create_schema(connection)
    add_segment(connection, "3681", NODE_85, NODE_86)
    connection.commit()
    return connection


def test_a_point_on_the_street_is_in_coverage(conn: sqlite3.Connection) -> None:
    assert within_coverage(conn, lon=NODE_85[0], lat=NODE_85[1])


def test_a_point_just_inside_the_radius_is_in_coverage(conn: sqlite3.Connection) -> None:
    """200 m off the block: a park interior or a river pier, still answerable."""
    assert within_coverage(conn, lon=NODE_85[0], lat=NODE_85[1] - 200 / 111_132.0)


def test_a_point_past_the_radius_is_out_of_coverage(conn: sqlite3.Connection) -> None:
    assert not within_coverage(conn, lon=NODE_85[0], lat=NODE_85[1] - 300 / 111_132.0)


def test_a_point_a_kilometre_away_is_out_of_coverage(conn: sqlite3.Connection) -> None:
    """The New Jersey case: nothing in the prefilter box at all, so no geometry is read."""
    assert not within_coverage(conn, lon=FAR_NORTH[0], lat=FAR_NORTH[1])


def test_the_radius_is_the_one_the_module_publishes(conn: sqlite3.Connection) -> None:
    inside = COVERAGE_RADIUS_M - 5
    outside = COVERAGE_RADIUS_M + 5

    assert within_coverage(conn, lon=NODE_85[0], lat=NODE_85[1] - inside / 111_132.0)
    assert not within_coverage(conn, lon=NODE_85[0], lat=NODE_85[1] - outside / 111_132.0)


def test_an_unreadable_geometry_does_not_put_a_point_in_coverage(conn: sqlite3.Connection) -> None:
    """data/ is untrusted at read time (CLAUDE.md): a broken row answers nothing, not 500."""
    conn.execute("UPDATE street_segment SET geom = ? WHERE segment_id = ?", ("{", "3681"))

    assert not within_coverage(conn, lon=NODE_85[0], lat=NODE_85[1])


def test_an_empty_database_covers_nothing(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM street_segment")

    assert not within_coverage(conn, lon=NODE_85[0], lat=NODE_85[1])
    assert coverage_bbox(conn) is None


def test_the_bbox_spans_every_centerline(conn: sqlite3.Connection) -> None:
    add_segment(conn, "north", NODE_86, FAR_NORTH)

    assert coverage_bbox(conn) == (
        min(NODE_85[0], NODE_86[0]),
        min(NODE_85[1], NODE_86[1]),
        max(NODE_85[0], NODE_86[0], FAR_NORTH[0]),
        max(FAR_NORTH[1], NODE_86[1]),
    )
