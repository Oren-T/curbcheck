"""Street-name normalization and the centerline graph."""

from __future__ import annotations

import pytest
from test_etl_fixtures import (
    AVENUE_LENGTH_FT,
    AVENUE_LENGTHS_FT,
    AVENUE_LON,
    CROSS_LATS,
    STUB_LAT,
    STUB_LONS,
    centerline_row,
    dead_end_graph,
    dead_end_rows,
    grid_graph,
    grid_rows,
)

from curbcheck.etl.stage import stage_centerline
from curbcheck.etl.streets import (
    DEFAULT_STREET_WIDTH_FT,
    NameMatch,
    build_graph,
    lonlat_to_feet,
    normalize_street_name,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # docs/DATA.md §1.9: the sign data pads numbers and spells suffixes out.
        ("EAST   85 STREET", "E 85 ST"),
        ("E  85 ST", "E 85 ST"),
        ("west 42 street", "W 42 ST"),
        ("SEVENTH AVENUE", "7 AVE"),
        ("PARK AVENUE*WEST RDWY", "PARK AVE"),
        ("ST. NICHOLAS AVE", "ST NICHOLAS AVE"),
        # Whole-name aliases the word map cannot reach.
        ("6 AVENUE", "AVE OF THE AMERICAS"),
        ("FDR DRIVE", "FRANKLIN D ROOSEVELT DR"),
        ("ADAM C POWELL BLVD", "ADAM CLAYTON POWELL JR BLVD"),
        ("FRED DOUGLAS BLVD", "FREDERICK DOUGLASS BLVD"),
        ("WEST 110 STREET", "CATHEDRAL PKWY"),
        ("MALCOLM X BLVD", "LENOX AVE"),
        ("MACDOUGAL ST", "MAC DOUGAL ST"),
        ("LAGUARDIA PL", "LA GUARDIA PL"),
        ("VAN DAM ST", "VANDAM ST"),
        ("WILLETT ST", "BIALYSTOKER PL"),
        ("QUEENSBORO BRG", "ED KOCH QUEENSBORO BRG"),
        ("BLEEKER ST", "BLEECKER ST"),
        ("CUMMINGS ST", "CUMMING ST"),
        ("THEATRE ALY", "THEATER ALY"),
    ],
)
def test_normalize_street_name_matches_the_centerline_spelling(raw, expected):
    assert normalize_street_name(raw) == expected


def test_normalize_street_name_is_idempotent():
    for raw in ("EAST   85 STREET", "6 AVENUE", "FDR DRIVE"):
        once = normalize_street_name(raw)
        assert normalize_street_name(once) == once


def test_graph_keys_segments_by_physicalid_and_rebuilds_nodes():
    graph = grid_graph()

    assert {segment_id for segment_id in graph.segments if segment_id.startswith("avenue")} == {
        "avenue-0",
        "avenue-1",
        "avenue-2",
    }
    avenue = graph.segments["avenue-0"]
    assert graph.node_streets(avenue.from_node) == {"BROAD AVE", "E 1 ST"}
    assert {segment.segment_id for segment in graph.segments_for_street("BROAD AVE")} == {
        "avenue-0",
        "avenue-1",
        "avenue-2",
    }


def test_segment_length_comes_from_the_geometry_not_the_declared_length():
    graph = grid_graph()

    for segment_id, expected in zip(
        ("avenue-0", "avenue-1", "avenue-2"), AVENUE_LENGTHS_FT, strict=True
    ):
        assert graph.segments[segment_id].length_ft == pytest.approx(expected, rel=0.01)


def test_missing_street_width_falls_back_to_the_manhattan_median_and_is_recorded():
    graph = grid_graph()

    assert graph.segments["main"].width_ft == DEFAULT_STREET_WIDTH_FT
    assert graph.segments["main"].width_defaulted is True
    assert graph.segments["avenue-0"].width_defaulted is False
    assert graph.report.width_defaulted == 1


def test_find_block_returns_the_single_segment_that_spans_one_block():
    graph = grid_graph()

    block = graph.find_block("BROAD AVENUE", "E 1 STREET", "E 2 STREET")

    assert block is not None
    assert [segment.segment_id for segment in block.segments] == ["avenue-0"]
    assert block.is_single_segment
    assert block.is_unique
    assert block.length_ft == pytest.approx(AVENUE_LENGTHS_FT[0], rel=0.01)


def test_find_block_walks_a_three_segment_chain_between_distant_cross_streets():
    graph = grid_graph()

    block = graph.find_block("BROAD AVENUE", "E 1 STREET", "E 4 STREET")

    assert block is not None
    assert [segment.segment_id for segment in block.segments] == [
        "avenue-0",
        "avenue-1",
        "avenue-2",
    ]
    assert block.length_ft == pytest.approx(AVENUE_LENGTH_FT, rel=0.01)
    assert block.line_ft.length == pytest.approx(AVENUE_LENGTH_FT, rel=0.01)


def test_find_block_orients_the_chain_from_the_from_street_node():
    graph = grid_graph()

    forward = graph.find_block("BROAD AVENUE", "E 1 STREET", "E 4 STREET")
    reverse = graph.find_block("BROAD AVENUE", "E 4 STREET", "E 1 STREET")

    assert forward is not None
    assert reverse is not None
    assert forward.from_node == reverse.to_node
    assert [s.segment_id for s in reverse.segments] == ["avenue-2", "avenue-1", "avenue-0"]
    assert forward.line_ft.coords[0] == reverse.line_ft.coords[-1]


def test_find_block_width_is_length_weighted_over_the_chain():
    graph = grid_graph()

    block = graph.find_block("BROAD AVENUE", "E 1 STREET", "E 4 STREET")

    assert block is not None
    assert block.width_ft == pytest.approx(60.0)


def test_segment_at_locates_the_segment_a_distance_falls_on():
    graph = grid_graph()
    block = graph.find_block("BROAD AVENUE", "E 1 STREET", "E 4 STREET")

    assert block is not None
    assert block.segment_at(10.0).segment_id == "avenue-0"
    assert block.segment_at(500.0).segment_id == "avenue-1"
    assert block.segment_at(AVENUE_LENGTH_FT).segment_id == "avenue-2"


@pytest.mark.parametrize(
    ("on", "from_", "to", "reason"),
    [
        ("NOWHERE AVENUE", "E 1 STREET", "E 2 STREET", "on_street_not_in_centerline"),
        ("BROAD AVENUE", "DEAD END", "DEAD END", "cross_street_is_dead_end"),
        ("BROAD AVENUE", "E 1 STREET", "NOWHERE STREET", "cross_street_not_in_centerline"),
        ("BROAD AVENUE", "E 1 STREET", "MAIN STREET", "cross_street_does_not_meet_on_street"),
    ],
)
def test_find_block_reports_why_a_lookup_missed(on, from_, to, reason):
    graph = grid_graph()

    lookup = graph.find_block_detail(on, from_, to)

    assert lookup.match is None
    assert lookup.reason == reason


def test_a_dead_end_resolves_to_the_streets_own_terminal_node():
    # docs/VALIDATION.md §4 D2: 604 sign rows name `DEAD END` as one end of the
    # block. The end DOT means is where the named street itself stops.
    graph = dead_end_graph()

    lookup = graph.find_block_detail("STUB STREET", "E 3 STREET", "DEAD END")

    assert lookup.match is not None
    assert [segment.segment_id for segment in lookup.match.segments] == [
        "stub-0",
        "stub-1",
        "stub-2",
    ]
    assert lookup.to.match is NameMatch.DEAD_END
    # The far node is the dangling one, so nothing else meets the chain there.
    assert len(graph.nodes[lookup.match.to_node].segment_ids) == 1


def test_a_dead_end_named_first_still_measures_from_the_dead_end():
    # `distance_from_intersection` is measured from the from_street end, so the
    # chain has to start at the dead end when DOT names it first.
    graph = dead_end_graph()

    from_dead = graph.find_block_detail("STUB STREET", "DEAD END", "E 3 STREET")
    from_named = graph.find_block_detail("STUB STREET", "E 3 STREET", "DEAD END")

    assert from_dead.match is not None and from_named.match is not None
    assert from_dead.match.from_node == from_named.match.to_node
    assert from_dead.match.to_node == from_named.match.from_node
    assert from_dead.match.length_ft == pytest.approx(from_named.match.length_ft)


def test_a_dead_end_walk_refuses_a_fork():
    # Which branch of a fork dead-ends is not knowable from the names, so the
    # sign stays unmatched rather than landing on a guess.
    rows = dead_end_rows()
    rows.append(
        centerline_row(
            "stub-branch",
            "STUB ST",
            [[STUB_LONS[1], STUB_LAT], [STUB_LONS[1], STUB_LAT + 0.0010]],
        )
    )
    graph = build_graph(stage_centerline(rows))

    lookup = graph.find_block_detail("STUB STREET", "E 3 STREET", "DEAD END")

    assert lookup.match is None
    assert lookup.reason == "cross_street_is_dead_end"


def test_a_cross_street_missing_from_the_centerline_is_inferred_from_the_coordinate():
    # docs/VALIDATION.md §4 D4: 739 sign rows name a cross street CSCL does not
    # carry. The block is one segment from the corner that did resolve, and the
    # published point says which way.
    graph = grid_graph()
    north = lonlat_to_feet(AVENUE_LON, (CROSS_LATS[1] + CROSS_LATS[2]) / 2)
    south = lonlat_to_feet(AVENUE_LON, (CROSS_LATS[0] + CROSS_LATS[1]) / 2)

    towards_north = graph.find_block_detail(
        "BROAD AVENUE", "E 2 STREET", "HIDDEN PLAZA", near_ft=north
    )
    towards_south = graph.find_block_detail(
        "BROAD AVENUE", "E 2 STREET", "HIDDEN PLAZA", near_ft=south
    )

    assert towards_north.match is not None and towards_south.match is not None
    assert [s.segment_id for s in towards_north.match.segments] == ["avenue-1"]
    assert [s.segment_id for s in towards_south.match.segments] == ["avenue-0"]
    assert towards_north.to.match is NameMatch.INFERRED
    # Distance is still measured from the corner DOT named first, whichever way
    # the inferred block runs.
    assert towards_north.match.from_node == towards_south.match.from_node


def test_a_missing_cross_street_falls_back_to_the_next_corners_own_name():
    graph = grid_graph()

    lookup = graph.find_block_detail("BROAD AVENUE", "E 2 STREET", "EAST 3 STREET NORTH")

    assert lookup.match is not None
    assert [segment.segment_id for segment in lookup.match.segments] == ["avenue-1"]


def test_a_missing_cross_street_with_nothing_to_point_the_way_stays_unmatched():
    # A coin flip would put the sign on the wrong block half the time.
    graph = grid_graph()

    lookup = graph.find_block_detail("BROAD AVENUE", "E 2 STREET", "HIDDEN PLAZA")

    assert lookup.match is None
    assert lookup.reason == "cross_street_not_in_centerline"


def test_resolve_street_distinguishes_exact_alias_and_fuzzy_matches():
    graph = grid_graph()

    assert graph.resolve_street("BROAD AVENUE").match is NameMatch.EXACT
    assert graph.resolve_street("BROADE AVENUE").match is NameMatch.FUZZY
    assert graph.resolve_street("NOWHERE AVENUE").match is NameMatch.MISSING
    assert graph.resolve_street("DEAD END").match is NameMatch.NOT_A_STREET


def test_fuzzy_matching_never_confuses_adjacent_numbered_streets():
    graph = grid_graph()

    # E 1 ST and E 2 ST differ by one character; taking one for the other would
    # move a sign a whole block, so the cutoff must reject it.
    assert graph.resolve_street("E 5 STREET").match is NameMatch.MISSING


def test_ambiguous_block_with_two_parallel_segments_is_counted_not_hidden():
    rows = grid_rows()
    rows.append(
        {
            "physicalid": "avenue-0-twin",
            "full_street_name": "BROAD AVE",
            "rw_type": "1",
            "streetwidth": "60",
            "the_geom": {
                "type": "MultiLineString",
                "coordinates": [[[-73.9880, 40.7490], [-73.9879, 40.7495], [-73.9880, 40.7500]]],
            },
        }
    )
    graph = build_graph(stage_centerline(rows))

    block = graph.find_block("BROAD AVENUE", "E 1 STREET", "E 2 STREET")

    assert block is not None
    assert block.chain_count == 2
    assert block.is_unique is False


def test_non_snappable_road_types_are_left_out_of_the_graph():
    rows = grid_rows()
    rows.append(
        {
            "physicalid": "sidewalk",
            "full_street_name": "BROAD AVE",
            "rw_type": "6",
            "the_geom": {
                "type": "MultiLineString",
                "coordinates": [[[-73.9881, 40.7490], [-73.9881, 40.7500]]],
            },
        }
    )
    graph = build_graph(stage_centerline(rows))

    assert "sidewalk" not in graph.segments
    assert graph.report.skipped_rw_type == 1


def test_multipart_geometry_is_merged_when_the_parts_are_contiguous():
    rows = [
        {
            "physicalid": "split",
            "full_street_name": "BROAD AVE",
            "rw_type": "1",
            "the_geom": {
                "type": "MultiLineString",
                "coordinates": [
                    [[-73.9880, 40.7490], [-73.9880, 40.7495]],
                    [[-73.9880, 40.7495], [-73.9880, 40.7500]],
                ],
            },
        }
    ]
    graph = build_graph(stage_centerline(rows))

    assert len(graph.segments["split"].line_deg.coords) == 3
    assert graph.report.non_contiguous_multiparts == 0
