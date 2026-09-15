"""Linear referencing a sign onto the curb, and the confidence that goes with it."""

from __future__ import annotations

import pytest
from shapely.geometry import LineString
from test_etl_fixtures import (
    AVENUE_LENGTHS_FT,
    AVENUE_LON,
    CROSS_LATS,
    dead_end_graph,
    divided_graph,
    grid_graph,
    staged_sign,
)

from curbcheck.etl.snap import (
    _COORD_UNKNOWN_QUALITY,
    _WEIGHT_CHAIN,
    _WEIGHT_COORD,
    _WEIGHT_DISTANCE,
    _WEIGHT_NAME,
    curb_point_ft,
    side_offset_sign,
    snap_signs,
)
from curbcheck.etl.snap import snap_sign as snap_one
from curbcheck.etl.streets import lonlat_to_feet

# Straight lines in EPSG:2263 feet: x grows east, y grows north.
EASTBOUND = LineString([(1000.0, 1000.0), (2000.0, 1000.0)])
WESTBOUND = LineString([(2000.0, 1000.0), (1000.0, 1000.0)])
NORTHBOUND = LineString([(1000.0, 1000.0), (1000.0, 2000.0)])
SOUTHBOUND = LineString([(1000.0, 2000.0), (1000.0, 1000.0)])


@pytest.mark.parametrize(
    ("line", "side", "expected"),
    [
        # Left of travel is +1. An eastbound block's left-hand curb faces north.
        (EASTBOUND, "N", 1),
        (EASTBOUND, "S", -1),
        (WESTBOUND, "N", -1),
        (WESTBOUND, "S", 1),
        # A northbound avenue's left-hand curb faces west.
        (NORTHBOUND, "W", 1),
        (NORTHBOUND, "E", -1),
        (SOUTHBOUND, "W", -1),
        (SOUTHBOUND, "E", 1),
    ],
)
def test_side_of_street_resolves_against_the_blocks_bearing(line, side, expected):
    offset_sign, ambiguous = side_offset_sign(line, side)

    assert offset_sign == expected
    assert ambiguous is False


def test_a_side_parallel_to_the_block_is_flagged_as_ambiguous():
    _, ambiguous = side_offset_sign(EASTBOUND, "E")

    assert ambiguous is True


def test_curb_point_sits_half_a_street_width_to_the_named_side():
    north = curb_point_ft(EASTBOUND, 500.0, 17.0, 1)
    south = curb_point_ft(EASTBOUND, 500.0, 17.0, -1)

    assert north == pytest.approx((1500.0, 1017.0))
    assert south == pytest.approx((1500.0, 983.0))


def test_snap_places_a_west_side_sign_west_of_the_centerline():
    graph = grid_graph()

    west = snap_one(staged_sign("w", to_street="E 2 STREET", side="W", distance_ft=50.0), graph)
    east = snap_one(staged_sign("e", to_street="E 2 STREET", side="E", distance_ft=50.0), graph)

    assert west.derived_lon is not None and east.derived_lon is not None
    assert west.derived_lon < AVENUE_LON < east.derived_lon
    assert west.segment_id == "avenue-0"


def test_distance_is_measured_from_the_from_street_node():
    graph = grid_graph()

    sign = staged_sign("a", to_street="E 2 STREET", distance_ft=50.0)
    result = snap_one(sign, graph)

    assert result.derived_lat is not None
    # 50 ft north of E 1 ST, which is the south end of the block.
    assert CROSS_LATS[0] < result.derived_lat < CROSS_LATS[1]
    assert result.distance_ft == 50.0


def test_reversing_from_and_to_measures_from_the_other_end():
    graph = grid_graph()

    forward = snap_one(
        staged_sign("f", from_street="E 1 STREET", to_street="E 2 STREET", distance_ft=50.0), graph
    )
    reverse = snap_one(
        staged_sign("r", from_street="E 2 STREET", to_street="E 1 STREET", distance_ft=50.0), graph
    )

    assert forward.derived_lat is not None and reverse.derived_lat is not None
    assert reverse.derived_lat > forward.derived_lat
    # Both are 50 ft from their own origin corner, so they straddle the middle.
    assert forward.derived_lat < (CROSS_LATS[0] + CROSS_LATS[1]) / 2 < reverse.derived_lat
    # The side letter names the same physical curb whichever way the block is read.
    assert forward.derived_lon == pytest.approx(reverse.derived_lon, abs=1e-7)


def test_a_distance_past_the_end_of_the_block_clamps_and_loses_confidence():
    graph = grid_graph()

    ok = snap_one(staged_sign("ok", to_street="E 2 STREET", distance_ft=100.0), graph)
    over = snap_one(staged_sign("over", to_street="E 2 STREET", distance_ft=5000.0), graph)

    assert over.distance_clamped is True
    assert over.distance_ft == pytest.approx(AVENUE_LENGTHS_FT[0], rel=0.01)
    assert over.snap_confidence < ok.snap_confidence
    assert any("clamped to the far corner" in note for note in over.snap_notes)


def test_confidence_blends_name_chain_distance_and_coordinate_quality():
    graph = grid_graph()

    clean = snap_one(staged_sign("clean", to_street="E 2 STREET", distance_ft=100.0), graph)

    # Exact names, one unique segment, a distance that fits, no published
    # coordinate to corroborate it.
    expected = (
        _WEIGHT_NAME + _WEIGHT_CHAIN + _WEIGHT_DISTANCE + _WEIGHT_COORD * (_COORD_UNKNOWN_QUALITY)
    )
    assert clean.snap_confidence == pytest.approx(expected, abs=1e-4)
    assert clean.snap_notes == ()


def test_a_published_coordinate_that_agrees_raises_confidence_to_one():
    graph = grid_graph()
    derived = snap_one(staged_sign("bare", to_street="E 2 STREET", distance_ft=100.0), graph)
    assert derived.derived_lon is not None and derived.derived_lat is not None
    x_ft, y_ft = lonlat_to_feet(derived.derived_lon, derived.derived_lat)

    agreeing = snap_one(
        staged_sign("agree", to_street="E 2 STREET", distance_ft=100.0, x_coord=x_ft, y_coord=y_ft),
        graph,
    )

    assert agreeing.snap_confidence == pytest.approx(1.0)


def test_a_published_coordinate_far_away_lowers_confidence_and_is_noted():
    graph = grid_graph()
    bare = snap_one(staged_sign("bare", to_street="E 2 STREET", distance_ft=100.0), graph)
    assert bare.derived_lon is not None and bare.derived_lat is not None
    x_ft, y_ft = lonlat_to_feet(bare.derived_lon, bare.derived_lat)

    disagreeing = snap_one(
        staged_sign(
            "far",
            to_street="E 2 STREET",
            distance_ft=100.0,
            x_coord=x_ft + 1000.0,
            y_coord=y_ft,
        ),
        graph,
    )

    assert disagreeing.snap_confidence < bare.snap_confidence
    assert any("published coordinate" in note for note in disagreeing.snap_notes)
    # The published pair never moves the sign, it only rates it (SPEC §B.1).
    assert disagreeing.derived_lon == pytest.approx(bare.derived_lon)


def test_a_multi_segment_chain_scores_below_a_single_segment_block():
    graph = grid_graph()

    single = snap_one(staged_sign("s", to_street="E 2 STREET", distance_ft=100.0), graph)
    chained = snap_one(staged_sign("c", to_street="E 4 STREET", distance_ft=100.0), graph)

    assert chained.snap_confidence < single.snap_confidence
    assert any("3 centerline segments" in note for note in chained.snap_notes)


def test_an_unmatched_sign_keeps_its_row_with_zero_confidence_and_a_reason():
    graph = grid_graph()

    result = snap_one(staged_sign("x", on_street="NOWHERE AVENUE"), graph)

    assert result.matched is False
    assert result.snap_confidence == 0.0
    assert result.derived_lon is None
    assert result.reason == "on_street_not_in_centerline"
    assert result.reason_class == "no_name_match"


def test_a_dead_end_blockface_snaps_at_a_modest_confidence_cost():
    graph = dead_end_graph()

    named = snap_one(
        staged_sign(
            "n",
            on_street="STUB STREET",
            from_street="E 3 STREET",
            to_street="DEAD END",
            side="N",
        ),
        graph,
    )

    assert named.matched is True
    assert named.segment_id == "stub-0"
    # Name quality 0.85 instead of 1.0 on a 0.45 weight, and nothing else lost.
    assert named.snap_confidence == pytest.approx(
        _WEIGHT_NAME * 0.85
        + _WEIGHT_CHAIN * 0.9
        + _WEIGHT_DISTANCE
        + _WEIGHT_COORD * _COORD_UNKNOWN_QUALITY,
        abs=1e-4,
    )
    assert any("dead end" in note for note in named.snap_notes)


def test_a_divided_roadway_picks_the_carriageway_whose_named_curb_faces_out():
    # docs/VALIDATION.md §4 D3: on PARK AVE a `Side: W` sign belongs to the west
    # curb of the *west* carriageway, not to the median of the east one.
    graph = divided_graph()

    west = snap_one(staged_sign("w", to_street="E 2 STREET", side="W"), graph)
    east = snap_one(staged_sign("e", to_street="E 2 STREET", side="E"), graph)

    assert west.segment_id == "avenue-0"
    assert east.segment_id == "avenue-0-east"
    assert any("tie broken by side" in note for note in west.snap_notes)
    assert any("2 equally short chains" in note for note in east.snap_notes)


def test_an_inferred_cross_street_lands_in_the_low_confidence_tier():
    # docs/VALIDATION.md §4 D4: the block came from one corner plus the
    # published point, so the row must read as low confidence, not as a match.
    graph = grid_graph()
    north = lonlat_to_feet(AVENUE_LON, (CROSS_LATS[1] + CROSS_LATS[2]) / 2)

    result = snap_one(
        staged_sign(
            "x",
            from_street="E 2 STREET",
            to_street="HIDDEN PLAZA",
            x_coord=north[0],
            y_coord=north[1],
        ),
        graph,
    )

    assert result.matched is True
    assert result.segment_id == "avenue-1"
    assert 0.55 < result.snap_confidence < 0.8
    assert any("in no centerline row" in note for note in result.snap_notes)


def test_coverage_report_separates_a_matching_gap_from_a_data_gap():
    graph = grid_graph()
    signs = [
        staged_sign("a", to_street="E 2 STREET"),
        staged_sign("b", to_street="E 2 STREET", side="E"),
        staged_sign("c", on_street="NOWHERE AVENUE"),
        staged_sign("d", to_street="MAIN STREET"),
    ]

    _, report = snap_signs(signs, graph)

    assert report.signs == 4
    assert report.matched == 2
    assert report.unmatched_reason_classes == {"no_chain": 1, "no_name_match": 1}
    # Four distinct (on, from, to, side) groups, two of which resolved.
    assert report.blockface_sides == 4
    assert report.blockface_sides_matched == 2
    assert report.blockface_side_share == pytest.approx(0.5)
