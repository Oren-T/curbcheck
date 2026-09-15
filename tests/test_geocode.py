"""The suggester over a synthetic index: the ladder, the folds, and hostile input.

Everything here runs against a hand-built Upper East Side block so the numbers
in the assertions can be checked by hand. `tests/test_geocode_real.py` holds the
cases that only mean something against the real database.
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics

import pytest

from curbcheck.db import create_schema
from curbcheck.engine.geo import M_PER_DEG_LAT, meters_per_degree_lon
from curbcheck.etl.addresses import build_address_index
from curbcheck.geocode import (
    MAX_QUERY_CHARS,
    REVERSE_ADDRESS_MAX_M,
    AddressQuery,
    GeocodeCandidate,
    GeocodeKind,
    IntersectionQuery,
    StreetQuery,
    ZipQuery,
    geocode,
    parse_query,
    reverse_geocode,
    suggest,
)
from curbcheck.geocode.suggest import _in_coverage

# A synthetic Upper East Side block, modelled on the worked example in
# docs/DATA.md §2.3: 3 AVE runs south to north between E 85 ST and E 86 ST,
# odd house numbers on the east side and even on the west.
NODE_85 = (-73.9544835, 40.7781469)
NODE_86 = (-73.9539850, 40.7788304)
NODE_87 = (-73.9530000, 40.7795000)
NODE_LEX_85 = (-73.9560000, 40.7779000)

# Surveyed doors on the odd (east) side. 1519 is deliberately absent: it is the
# real gap the interpolation rung exists for (docs/DATA.md §5.1).
ODD_DOORS = {1509: 0.0, 1517: 0.5, 1529: 1.0}
# The even (west) side, offset so the two sides are distinguishable. 1518 is
# absent and 1528 is present, so an even number interpolates between 1510 and
# 1528 and never snaps across the street to 1517.
EVEN_DOORS = {1510: 0.02, 1528: 0.98}
EVEN_SIDE_LON_OFFSET = -0.00005


def _line(start: tuple[float, float], end: tuple[float, float]) -> str:
    return json.dumps({"type": "LineString", "coordinates": [list(start), list(end)]})


def _between(start: tuple[float, float], end: tuple[float, float], fraction: float) -> list[float]:
    return [
        start[0] + (end[0] - start[0]) * fraction,
        start[1] + (end[1] - start[1]) * fraction,
    ]


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
    conn.execute(
        "INSERT INTO street_segment (segment_id, street_name, street_norm, from_node, to_node,"
        " geom, min_lon, min_lat, max_lon, max_lat, left_low_address, left_high_address,"
        " right_low_address, right_high_address) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            segment_id,
            street_name,
            street_name,
            _node_id(start),
            _node_id(end),
            _line(start, end),
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


def _node_id(lonlat: tuple[float, float]) -> str:
    return f"{lonlat[0]:.7f},{lonlat[1]:.7f}"


def _add_node(conn: sqlite3.Connection, lonlat: tuple[float, float], names: list[str]) -> None:
    conn.execute(
        "INSERT INTO street_node (node_id, lon, lat, street_names) VALUES (?,?,?,?)",
        (_node_id(lonlat), lonlat[0], lonlat[1], json.dumps(names)),
    )


def _address_row(house: str, street: str, position: list[float], zipcode: str = "10028") -> dict:
    return {
        "house_number": house,
        "full_street_name": street,
        "zipcode": zipcode,
        "the_geom": {"type": "Point", "coordinates": position},
    }


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
        end=NODE_87,
        left=("1530", "1548"),
        right=("1529", "1545"),
    )
    # A street with no published address range and no surveyed door: the shape
    # that degrades all the way to the street itself.
    _add_segment(
        connection, segment_id="9001", street_name="E 85 ST", start=NODE_LEX_85, end=NODE_85
    )
    _add_segment(
        connection,
        segment_id="9002",
        street_name="E 86 ST",
        start=(NODE_86[0] - 0.0020, NODE_86[1]),
        end=NODE_86,
    )
    _add_segment(
        connection,
        segment_id="9003",
        street_name="LEXINGTON AVE",
        start=(NODE_LEX_85[0], NODE_LEX_85[1] - 0.002),
        end=NODE_LEX_85,
    )
    # A street AddressPoint never files a door on, but which publishes a range:
    # the 36 streets the last rung exists for (docs/DATA.md §5.1).
    _add_segment(
        connection,
        segment_id="9100",
        street_name="CHISUM PL",
        start=(-73.9350, 40.8200),
        end=(-73.9340, 40.8210),
        left=("2", "20"),
        right=("1", "19"),
    )
    _add_node(connection, NODE_85, ["3 AVE", "E 85 ST"])
    _add_node(connection, NODE_86, ["3 AVE", "E 86 ST"])
    _add_node(connection, NODE_LEX_85, ["E 85 ST", "LEXINGTON AVE"])

    addresses = [
        _address_row(str(house), "3 AVE", _between(NODE_85, NODE_86, fraction))
        for house, fraction in ODD_DOORS.items()
    ]
    addresses.extend(
        _address_row(
            str(house),
            "3 AVE",
            [
                _between(NODE_85, NODE_86, fraction)[0] + EVEN_SIDE_LON_OFFSET,
                _between(NODE_85, NODE_86, fraction)[1],
            ],
        )
        for house, fraction in EVEN_DOORS.items()
    )
    places = [
        {
            "feature_name": "GRACIE MANSION",
            "the_geom": {"type": "Point", "coordinates": [-73.9432, 40.7760]},
        },
        # Its last word sorts before its first, which is how a place search
        # that lost the typed order shows up.
        {
            "feature_name": "WASHINGTON ARCH",
            "the_geom": {"type": "Point", "coordinates": [-73.9973, 40.7308]},
        },
    ]
    build_address_index(connection, address_rows=addresses, place_rows=places)
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
        ("1500 3rd av", AddressQuery(house_number=1500, street="3 AV")),
        ("123a E 85 St", AddressQuery(house_number=123, street="E 85 ST")),
    ],
)
def test_address_forms_parse_to_the_same_folded_street(text, expected):
    assert parse_query(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Lexington Ave & 86th St", IntersectionQuery("LEXINGTON AVE", "86 ST")),
        ("E 86 St and 3 Ave", IntersectionQuery("E 86 ST", "3 AVE")),
        ("E 86 St / 3 Ave", IntersectionQuery("E 86 ST", "3 AVE")),
        ("E 86 St at 3 Ave", IntersectionQuery("E 86 ST", "3 AVE")),
        ("E 86 St @ 3 Ave", IntersectionQuery("E 86 ST", "3 AVE")),
        ("86 & 3", IntersectionQuery("86", "3")),
    ],
)
def test_intersection_forms_parse_to_two_streets(text, expected):
    assert parse_query(text) == expected


def test_bare_street_name_parses_as_a_street():
    assert parse_query("Third Avenue") == StreetQuery(street="3 AVE")


def test_five_digits_parse_as_a_zip():
    assert parse_query("10021") == ZipQuery(zipcode="10021")


@pytest.mark.parametrize("text", ["", "   ", "x" * (MAX_QUERY_CHARS + 1), "\x00\x01"])
def test_empty_and_oversized_queries_parse_to_none(text):
    assert parse_query(text) is None


# --- the address ladder ---------------------------------------------------


def test_a_surveyed_door_is_the_top_answer_and_carries_its_zip(conn):
    [candidate] = [c for c in suggest(conn, "1517 3rd Ave") if c.kind is GeocodeKind.ADDRESS]

    assert candidate.label == "1517 3 AVE"
    assert candidate.confidence == pytest.approx(0.98)
    assert candidate.secondary == "Manhattan 10028"
    assert (candidate.lon, candidate.lat) == pytest.approx(_between(NODE_85, NODE_86, 0.5))


def test_a_missing_number_is_placed_between_its_two_surveyed_neighbours(conn):
    # 1519 is absent; 1517 and 1529 are present, so it lands a sixth of the way
    # between them and says so on its second line.
    [candidate] = [c for c in suggest(conn, "1519 3rd ave") if c.kind is GeocodeKind.ADDRESS]

    assert candidate.label == "1519 3 AVE"
    assert candidate.confidence == pytest.approx(0.75)
    assert candidate.secondary == "Manhattan · between 1517 and 1529"
    fraction = 0.5 + 0.5 * (1519 - 1517) / (1529 - 1517)
    assert (candidate.lon, candidate.lat) == pytest.approx(_between(NODE_85, NODE_86, fraction))


def test_interpolation_stays_on_the_side_the_parity_says(conn):
    """1518 is even, so it is placed between 1510 and 1528, never between the odd doors."""
    [candidate] = [c for c in suggest(conn, "1518 3 Ave") if c.kind is GeocodeKind.ADDRESS]

    assert candidate.label == "1518 3 AVE"
    assert candidate.secondary == "Manhattan · between 1510 and 1528"
    assert candidate.lon < _between(NODE_85, NODE_86, 0.5)[0] + EVEN_SIDE_LON_OFFSET / 2


def test_a_number_outside_every_run_of_doors_reads_as_near_the_closest_one(conn):
    [candidate] = [c for c in suggest(conn, "9999 3 Ave") if c.kind is GeocodeKind.ADDRESS]

    assert candidate.label == "near 1529 3 AVE"
    assert candidate.confidence == pytest.approx(0.60)


def test_a_street_with_no_doors_falls_back_to_the_published_range(conn):
    """The last rung: CSCL's own range, for one of the 36 streets with no door."""
    [candidate] = [c for c in suggest(conn, "10 Chisum Pl") if c.kind is GeocodeKind.ADDRESS]

    assert candidate.label == "10 CHISUM PL"
    assert candidate.confidence == pytest.approx(0.50)
    assert candidate.lon == pytest.approx(-73.9350 + 0.0010 * (10 - 2) / (20 - 2))


def test_a_street_with_neither_doors_nor_ranges_degrades_to_the_street(conn):
    [candidate] = suggest(conn, "200 E 85 St")

    assert candidate.kind is GeocodeKind.STREET
    assert candidate.label == "E 85 ST"
    assert candidate.confidence < 0.5


def test_an_unknown_street_returns_no_candidates(conn):
    assert suggest(conn, "123 NOWHERE BLVD") == []


# --- the other kinds ------------------------------------------------------


def test_an_intersection_returns_the_shared_node(conn):
    [candidate] = suggest(conn, "3 Ave & E 86 St")

    assert candidate.kind is GeocodeKind.INTERSECTION
    assert (candidate.lon, candidate.lat) == pytest.approx(NODE_86)
    assert candidate.confidence == pytest.approx(0.95)


def test_an_intersection_is_order_independent_and_survives_spelled_out_names(conn):
    [written_out] = suggest(conn, "East 86th Street and Third Avenue")
    [abbreviated] = suggest(conn, "3 AVE & E 86 ST")

    assert (written_out.lon, written_out.lat) == (abbreviated.lon, abbreviated.lat)


def test_a_numbered_cross_street_resolves_without_its_directional(conn):
    [candidate] = suggest(conn, "3 Ave & 86")

    assert (candidate.lon, candidate.lat) == pytest.approx(NODE_86)


def test_two_streets_that_never_meet_degrade_to_the_two_streets(conn):
    """Claiming a corner that is not in the data would be the dishonest answer."""
    candidates = suggest(conn, "LEXINGTON AVE & 3 AVE")

    assert [c.kind for c in candidates] == [GeocodeKind.STREET, GeocodeKind.STREET]
    assert {c.label for c in candidates} == {"LEXINGTON AVE", "3 AVE"}
    assert all(c.confidence == pytest.approx(0.30) for c in candidates)


def test_a_typo_in_a_street_name_still_resolves_at_a_lower_confidence(conn):
    [candidate] = [c for c in suggest(conn, "CHISM PL") if c.kind is GeocodeKind.STREET]

    assert candidate.label == "CHISUM PL"
    assert candidate.confidence == pytest.approx(0.45 * 0.8)


def test_a_query_too_short_to_correct_is_not_corrected(conn):
    """Nearly every short variant is within one edit of every other one."""
    assert suggest(conn, "3 A") == []


def test_a_zip_returns_its_centre_and_says_how_coarse_that_is(conn):
    [candidate] = suggest(conn, "10028")

    assert candidate.kind is GeocodeKind.ZIP
    assert candidate.label == "10028"
    assert candidate.secondary == "Manhattan · ZIP centre of 5 addresses"


def test_a_place_name_is_found_by_its_words(conn):
    [candidate] = suggest(conn, "gracie mansion")

    assert candidate.kind is GeocodeKind.PLACE
    assert candidate.label == "GRACIE MANSION"


@pytest.mark.parametrize(
    ("typed", "label"),
    [("gracie mans", "GRACIE MANSION"), ("washington ar", "WASHINGTON ARCH")],
)
def test_a_half_typed_place_name_still_matches_on_the_last_word(conn, typed, label):
    """Only the last word is a prefix, so the words have to stay in typed order."""
    assert suggest(conn, typed)[0].label == label


# --- hostile input --------------------------------------------------------


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
    assert isinstance(suggest(conn, text), list)
    assert conn.execute("SELECT count(*) FROM street_segment").fetchone()[0] == 6
    assert conn.execute("SELECT count(*) FROM address_point").fetchone()[0] == 5


def test_limit_bounds_the_candidate_list(conn):
    assert suggest(conn, "3 Ave", limit=1) == suggest(conn, "3 Ave")[:1]
    with pytest.raises(ValueError, match="limit"):
        suggest(conn, "3 Ave", limit=0)


def test_the_candidate_list_is_never_longer_than_eight(conn):
    for index in range(12):
        conn.execute(
            "INSERT INTO address_point (street_norm, house_number, display, zipcode, lon, lat)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("3 AVE", 1517, f"1517{chr(ord('A') + index)} 3 AVE", "10028", *NODE_85),
        )

    assert len(suggest(conn, "1517 3 Ave")) == 8


# --- coverage and the public entry point ----------------------------------


def test_geocode_is_suggest_filtered_to_what_the_app_can_answer_about(conn):
    assert geocode(conn, "1517 3rd Ave") == suggest(conn, "1517 3rd Ave")


def test_a_candidate_outside_coverage_is_never_offered(conn):
    """Offering a destination and then refusing to search it is UX audit P0-3.

    Written with `address` candidates because those are the kinds that come off
    a dataset other than the centerline, and so the only ones that can land
    outside coverage at all.
    """
    inside = GeocodeCandidate(
        label="1517 3 AVE", lat=NODE_85[1], lon=NODE_85[0], kind=GeocodeKind.ADDRESS, confidence=0.9
    )
    hoboken = GeocodeCandidate(
        label="1 WASHINGTON ST", lat=40.7440, lon=-74.0324, kind=GeocodeKind.ADDRESS, confidence=0.9
    )

    assert _in_coverage(conn, [hoboken, inside], 8) == [inside]


def test_a_street_is_in_coverage_without_being_asked(conn):
    """Its pin is a vertex of its own centerline, so the question has one answer."""
    far_away = GeocodeCandidate(
        label="3 AVE", lat=0.0, lon=0.0, kind=GeocodeKind.STREET, confidence=0.3
    )

    assert _in_coverage(conn, [far_away], 8) == [far_away]


# --- reverse --------------------------------------------------------------


def test_a_pin_beside_a_door_reads_back_as_that_door(conn):
    """A pin is the one destination the user cannot read back to themselves (P1-4)."""
    lon, lat = _between(NODE_85, NODE_86, 0.5)

    match = reverse_geocode(conn, lon=lon + 0.0001, lat=lat)

    assert match is not None
    assert match.kind is GeocodeKind.ADDRESS
    assert match.label == "near 1517 3 AVE"
    assert 0.0 < match.distance_m < REVERSE_ADDRESS_MAX_M
    assert match.distance_m == round(match.distance_m, 1)


def test_a_pin_far_from_any_door_falls_back_to_the_nearest_corner(conn):
    match = reverse_geocode(conn, lon=NODE_LEX_85[0], lat=NODE_LEX_85[1] + 0.0002)

    assert match is not None
    assert match.kind is GeocodeKind.INTERSECTION
    assert "&" in match.label


def test_a_pin_with_no_corner_nearby_reads_back_as_the_street(conn):
    conn.execute("DELETE FROM street_node")

    match = reverse_geocode(conn, lon=-73.95585, lat=40.77792)

    assert match is not None
    assert match.kind is GeocodeKind.STREET
    assert match.label == "E 85 ST"


def test_a_pin_with_nothing_around_it_reverses_to_nothing(conn):
    assert reverse_geocode(conn, lon=-74.0324, lat=40.7440) is None


def test_a_pin_at_a_nonsense_coordinate_reverses_to_nothing(conn):
    assert reverse_geocode(conn, lon=float("nan"), lat=float("inf")) is None


# --- accuracy -------------------------------------------------------------

# The fast twin of `test_geocode_real.py`'s accuracy regression, so a
# `make check` with no database still fails when the ladder drifts. One
# straight avenue with a surveyed door every 40 ft; a third of them are hidden
# from the index, and geocoding those exercises the interpolation rung against
# a location we know exactly.
ACCURACY_DOORS = 90
ACCURACY_SPACING_DEG = 0.00012
ACCURACY_MEDIAN_MAX_M = 10.0
ACCURACY_P95_MAX_M = 60.0


def _door_position(index: int) -> list[float]:
    return [-73.97, 40.75 + index * ACCURACY_SPACING_DEG]


@pytest.fixture
def straight_avenue() -> tuple[sqlite3.Connection, dict[int, list[float]]]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    create_schema(connection)
    start, end = _door_position(0), _door_position(ACCURACY_DOORS - 1)
    _add_segment(
        connection,
        segment_id="long",
        street_name="LONG AVE",
        start=(start[0], start[1]),
        end=(end[0], end[1]),
    )
    truth = {1 + 2 * index: _door_position(index) for index in range(ACCURACY_DOORS)}
    indexed = [
        _address_row(str(house), "LONG AVE", position)
        for order, (house, position) in enumerate(truth.items())
        if order % 3 != 1
    ]
    build_address_index(connection, address_rows=indexed, place_rows=[])
    connection.commit()
    return connection, truth


def test_every_door_on_a_street_geocodes_to_within_ten_metres(straight_avenue):
    connection, truth = straight_avenue
    errors = []
    for house, position in truth.items():
        [candidate] = suggest(connection, f"{house} LONG AVE", limit=1)
        errors.append(
            math.hypot(
                (candidate.lon - position[0]) * meters_per_degree_lon(position[1]),
                (candidate.lat - position[1]) * M_PER_DEG_LAT,
            )
        )
    errors.sort()

    assert statistics.median(errors) <= ACCURACY_MEDIAN_MAX_M
    assert errors[int(len(errors) * 0.95)] <= ACCURACY_P95_MAX_M
