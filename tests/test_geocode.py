"""Local geocoder: query parsing, parity, interpolation, intersections, and the fallbacks."""

from __future__ import annotations

import json
import sqlite3

import pytest

from curbcheck.db import create_schema
from curbcheck.geocode import (
    AddressQuery,
    GeocodeKind,
    IntersectionQuery,
    StreetQuery,
    geocode,
    parse_query,
)

# A synthetic Upper East Side block, modelled on the worked example in
# docs/DATA.md §2.3: 3 AVE runs south to north between E 85 ST and E 86 ST,
# odd house numbers on the east side and even on the west.
NODE_85 = (-73.9544835, 40.7781469)
NODE_86 = (-73.9539850, 40.7788304)


def _line(start: tuple[float, float], end: tuple[float, float]) -> str:
    return json.dumps({"type": "LineString", "coordinates": [list(start), list(end)]})


def _add_segment(
    conn: sqlite3.Connection,
    *,
    segment_id: str,
    street_name: str,
    start: tuple[float, float],
    end: tuple[float, float],
    left: tuple[str, str] | None = None,
    right: tuple[str, str] | None = None,
) -> None:
    geom = _line(start, end)
    conn.execute(
        "INSERT INTO street_segment (segment_id, street_name, street_norm, geom,"
        " min_lon, min_lat, max_lon, max_lat, left_low_address, left_high_address,"
        " right_low_address, right_high_address)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            segment_id,
            street_name,
            street_name,
            geom,
            min(start[0], end[0]),
            min(start[1], end[1]),
            max(start[0], end[0]),
            max(start[1], end[1]),
            left[0] if left else None,
            left[1] if left else None,
            right[0] if right else None,
            right[1] if right else None,
        ),
    )


def _add_node(conn: sqlite3.Connection, lonlat: tuple[float, float], names: list[str]) -> None:
    conn.execute(
        "INSERT INTO street_node (node_id, lon, lat, street_names) VALUES (?,?,?,?)",
        (f"{lonlat[0]:.7f},{lonlat[1]:.7f}", lonlat[0], lonlat[1], json.dumps(names)),
    )


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    create_schema(connection)
    _add_segment(
        connection,
        segment_id="3681",
        street_name="3 AVE",
        start=NODE_85,
        end=NODE_86,
        left=("1510", "1528"),
        right=("1509", "1525"),
    )
    _add_segment(
        connection,
        segment_id="3682",
        street_name="3 AVE",
        start=NODE_86,
        end=(-73.953, 40.7795),
        left=("1530", "1548"),
        right=("1529", "1545"),
    )
    # A street with no published address range at all: 46% of Manhattan
    # segments look like this (docs/DATA.md §2.2).
    _add_segment(
        connection,
        segment_id="9001",
        street_name="E 85 ST",
        start=(-73.9560, 40.7779),
        end=NODE_85,
    )
    _add_segment(
        connection,
        segment_id="9002",
        street_name="E 85 ST",
        start=NODE_85,
        end=(-73.9530, 40.7784),
    )
    _add_node(connection, NODE_85, ["3 AVE", "E 85 ST"])
    _add_node(connection, NODE_86, ["3 AVE", "E 86 ST"])
    _add_node(connection, (-73.9560, 40.7779), ["E 85 ST", "LEXINGTON AVE"])
    connection.commit()
    return connection


# --- parsing -------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("123 E 85 St", AddressQuery(house_number=123, street="E 85 ST")),
        ("123 East 85th Street", AddressQuery(house_number=123, street="E 85 ST")),
        ("1500 3rd Ave", AddressQuery(house_number=1500, street="3 AVE")),
        ("  1500   THIRD   AVENUE ", AddressQuery(house_number=1500, street="3 AVE")),
        ("123a E 85 St", AddressQuery(house_number=123, street="E 85 ST")),
    ],
)
def test_address_forms_parse_to_the_same_normalized_street(text, expected):
    assert parse_query(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Lexington Ave & 86th St", IntersectionQuery("LEXINGTON AVE", "86 ST")),
        ("E 86 St and 3 Ave", IntersectionQuery("E 86 ST", "3 AVE")),
        ("E 86 St / 3 Ave", IntersectionQuery("E 86 ST", "3 AVE")),
        ("E 86 St at 3 Ave", IntersectionQuery("E 86 ST", "3 AVE")),
    ],
)
def test_intersection_forms_parse_to_two_streets(text, expected):
    assert parse_query(text) == expected


def test_bare_street_name_parses_as_a_street():
    assert parse_query("Third Avenue") == StreetQuery(street="3 AVE")


@pytest.mark.parametrize("text", ["", "   ", "x" * 201])
def test_empty_and_oversized_queries_parse_to_none(text):
    assert parse_query(text) is None


# --- address interpolation ----------------------------------------------


def test_even_house_number_interpolates_along_the_segment(conn):
    # 1519 is the midpoint of the odd range 1509-1525.
    [candidate] = geocode(conn, "1519 3rd Ave")
    assert candidate.kind == GeocodeKind.ADDRESS
    assert candidate.label == "1519 3 AVE"
    fraction = (1519 - 1509) / (1525 - 1509)
    assert candidate.lon == pytest.approx(
        NODE_85[0] + fraction * (NODE_86[0] - NODE_85[0]), abs=1e-9
    )
    assert candidate.lat == pytest.approx(
        NODE_85[1] + fraction * (NODE_86[1] - NODE_85[1]), abs=1e-9
    )


def test_low_and_high_of_a_range_land_on_the_segment_ends(conn):
    [low] = geocode(conn, "1510 3 Ave")
    [high] = geocode(conn, "1528 3 Ave")
    assert (low.lon, low.lat) == pytest.approx(NODE_85, abs=1e-9)
    assert (high.lon, high.lat) == pytest.approx(NODE_86, abs=1e-9)


def test_parity_keeps_an_odd_number_off_the_even_side(conn):
    # 1511 is inside the even range 1510-1528 numerically but belongs to the
    # odd side, so exactly one blockface may claim it.
    candidates = geocode(conn, "1511 3 Ave")
    assert len(candidates) == 1
    assert candidates[0].kind == GeocodeKind.ADDRESS


def test_house_number_in_the_second_block_picks_that_block(conn):
    [candidate] = geocode(conn, "1548 3 Ave")
    assert candidate.lon == pytest.approx(-73.953, abs=1e-9)


# --- fallbacks -----------------------------------------------------------


def test_house_number_past_every_range_falls_back_to_the_nearest_block_corner(conn):
    [candidate] = geocode(conn, "1600 3 Ave")
    assert candidate.kind == GeocodeKind.INTERSECTION
    assert candidate.confidence < 0.6
    # 1530-1548 is the nearest range and 1600 counts past its far end.
    assert candidate.lon == pytest.approx(-73.953, abs=1e-9)


def test_house_number_below_every_range_falls_back_to_the_near_corner(conn):
    [candidate] = geocode(conn, "1400 3 Ave")
    assert candidate.kind == GeocodeKind.INTERSECTION
    assert (candidate.lon, candidate.lat) == pytest.approx(NODE_85, abs=1e-9)


def test_street_with_no_address_ranges_falls_back_to_its_midpoint(conn):
    [candidate] = geocode(conn, "200 E 85 St")
    assert candidate.kind == GeocodeKind.INTERSECTION
    assert candidate.confidence < 0.6
    assert -73.9560 < candidate.lon < -73.9530


def test_unknown_street_returns_no_candidates(conn):
    assert geocode(conn, "123 NOWHERE BLVD") == []


# --- intersections -------------------------------------------------------


def test_intersection_returns_the_shared_node(conn):
    [candidate] = geocode(conn, "3 Ave & E 86 St")
    assert candidate.kind == GeocodeKind.INTERSECTION
    assert (candidate.lon, candidate.lat) == pytest.approx(NODE_86, abs=1e-9)
    assert candidate.confidence > 0.9


def test_intersection_is_order_independent_and_survives_spelled_out_names(conn):
    [written_out] = geocode(conn, "East 86th Street and Third Avenue")
    [abbreviated] = geocode(conn, "3 AVE & E 86 ST")
    assert (written_out.lon, written_out.lat) == (abbreviated.lon, abbreviated.lat)


def test_streets_that_do_not_meet_return_nothing(conn):
    assert geocode(conn, "LEXINGTON AVE & E 86 ST") == []


# --- hostile input -------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "<script>alert(1)</script>",
        "=1+1",
        "+SUM(A1:A9)",
        "'; DROP TABLE street_segment; --",
        "123 E 85 St'; DELETE FROM street_node; --",
        "%",
        "_",
        "\\",
        "123 " + "A" * 190,
        "\x00\x01",
        "\uff13 AVE",  # fullwidth digit three: not a house number
    ],
)
def test_hostile_queries_never_raise_and_never_mutate(conn, text):
    assert isinstance(geocode(conn, text), list)
    assert conn.execute("SELECT count(*) FROM street_segment").fetchone()[0] == 4
    assert conn.execute("SELECT count(*) FROM street_node").fetchone()[0] == 3


def test_limit_bounds_the_candidate_list(conn):
    assert geocode(conn, "1519 3rd Ave", limit=1) == geocode(conn, "1519 3rd Ave")[:1]
    with pytest.raises(ValueError, match="limit"):
        geocode(conn, "1519 3rd Ave", limit=0)


def test_missing_directional_on_a_numbered_street_tries_east_and_west(conn):
    # People type "86th St"; every Manhattan cross street is "E 86 ST" or
    # "W 86 ST", so both are tried and only the one in the data matches.
    [candidate] = geocode(conn, "3 Ave & 86th St")
    assert (candidate.lon, candidate.lat) == pytest.approx(NODE_86, abs=1e-9)
