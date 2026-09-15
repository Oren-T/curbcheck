"""Arrow extrapolation: sign posts on a blockface-side become curb spans."""

from __future__ import annotations

import pytest
from test_etl_fixtures import (
    AVENUE_LON,
    avenue_chain_length_ft,
    first_block_length_ft,
    grid_graph,
    staged_sign,
)

from curbcheck.config import CAR_LENGTH_FT
from curbcheck.etl.segments import Arity, arrow_arity, regulation_family, resolve_segments
from curbcheck.etl.snap import snap_sign

NO_PARKING = "NO PARKING ANYTIME"
NO_STANDING = "NO STANDING ANYTIME"

WHOLE_SIDE_FT = round(avenue_chain_length_ft())


def resolve(*signs):
    graph = grid_graph()
    snaps = [snap_sign(sign, graph) for sign in signs]
    return resolve_segments(snaps)


def spans(segments):
    return sorted((round(s.start_ft), round(s.end_ft)) for s in segments)


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("NO PARKING ANYTIME", Arity.NONE),
        ("NO PARKING ANYTIME <->", Arity.DOUBLE),
        # docs/DATA.md §1.6: arrows are drawn with a variable number of dashes.
        ("NO PARKING ANYTIME <----->", Arity.DOUBLE),
        ("NO PARKING ANYTIME -->", Arity.SINGLE),
        ("NO PARKING ANYTIME ----->", Arity.SINGLE),
        ("NO PARKING ANYTIME <--", Arity.SINGLE),
        ("NO PARKING SINGLE ARROW", Arity.SINGLE),
        # The supersedes tail comes off before the arrow is read.
        ("NO PARKING <-> (SUPERSEDES SP-1B & SP-2B)", Arity.DOUBLE),
        ("NO PARKING --> (SUPERSEDES SP-1BA)", Arity.SINGLE),
    ],
)
def test_arrow_arity_is_read_off_the_description_glyph(description, expected):
    assert arrow_arity(description) is expected


def test_regulation_family_groups_the_signs_that_subdivide_a_blockface():
    no_parking = staged_sign("a", description="NO PARKING 8AM-6PM -->", sign_code="PS-40A")
    no_parking_other = staged_sign("b", description="NO PARKING ANYTIME <->", sign_code="PS-1G")
    no_standing = staged_sign("c", description="NO STANDING ANYTIME <->", sign_code="PS-2G")
    two_hour = staged_sign("d", description="2 HMP SATURDAY 8AM-7PM <->", sign_code="PS-127C")
    three_hour = staged_sign("e", description="3 HOUR PARKING 9AM-7PM <->", sign_code="PS-55C")

    assert regulation_family(no_parking) == regulation_family(no_parking_other)
    assert regulation_family(no_parking) != regulation_family(no_standing)
    assert regulation_family(two_hour) == regulation_family(three_hour)


def test_a_sign_with_no_arrow_governs_the_whole_blockface_side():
    segments, report = resolve(staged_sign("a", description=NO_PARKING, distance_ft=100.0))

    assert len(segments) == 1
    assert segments[0].start_ft == 0.0
    assert segments[0].end_ft == pytest.approx(WHOLE_SIDE_FT, abs=1.0)
    assert report.whole_side_spans == 1


def test_a_double_arrow_alone_also_governs_the_whole_side():
    # 34 RCNY 4-08: one authorized sign governs the block unless another
    # same-type sign bounds it.
    segments, _ = resolve(staged_sign("a", description=f"{NO_PARKING} <->", distance_ft=100.0))

    assert spans(segments) == [(0, WHOLE_SIDE_FT)]


def test_a_double_arrow_bounded_by_same_family_posts_stops_at_them():
    segments, _ = resolve(
        staged_sign("a", description=NO_PARKING, distance_ft=100.0),
        staged_sign("b", description=f"{NO_PARKING} <->", distance_ft=500.0),
        staged_sign("c", description=NO_PARKING, distance_ft=900.0),
    )

    assert (100, 900) in spans(segments)


def test_a_single_arrow_extends_to_the_next_post_of_the_same_family():
    segments, report = resolve(
        staged_sign(
            "a", description=f"{NO_PARKING} -->", arrow_direction="North", distance_ft=100.0
        ),
        staged_sign("b", description=NO_PARKING, distance_ft=500.0),
    )

    assert (100, 500) in spans(segments)
    assert report.arrow_extended_spans == 1


def test_a_single_arrow_ignores_a_post_of_a_different_family():
    segments, _ = resolve(
        staged_sign(
            "a", description=f"{NO_PARKING} -->", arrow_direction="North", distance_ft=100.0
        ),
        staged_sign("b", description=NO_STANDING, sign_code="PS-2G", distance_ft=500.0),
    )

    assert (100, WHOLE_SIDE_FT) in spans(segments)


def test_a_single_arrow_with_nothing_ahead_runs_to_the_corner():
    forward, _ = resolve(
        staged_sign(
            "a", description=f"{NO_PARKING} -->", arrow_direction="North", distance_ft=100.0
        )
    )
    backward, _ = resolve(
        staged_sign(
            "b", description=f"{NO_PARKING} -->", arrow_direction="South", distance_ft=100.0
        )
    )

    assert spans(forward) == [(100, WHOLE_SIDE_FT)]
    assert spans(backward) == [(0, 100)]


def test_a_single_arrow_without_a_bearing_falls_back_to_the_whole_side():
    # docs/DECISIONS.md D3 (refined): the glyph gives arity, arrow_direction
    # gives the bearing. Without the bearing there is nothing to extrapolate.
    segments, _ = resolve(
        staged_sign("a", description=f"{NO_PARKING} -->", arrow_direction=None, distance_ft=100.0)
    )

    assert spans(segments) == [(0, WHOLE_SIDE_FT)]


def test_signs_on_one_post_with_the_same_span_merge_into_one_segment():
    segments, _ = resolve(
        staged_sign("a", description=NO_PARKING, distance_ft=100.0),
        staged_sign("b", description=NO_STANDING, sign_code="PS-2G", distance_ft=100.0),
    )

    assert len(segments) == 1
    assert segments[0].derived_from == ("a", "b")


def test_different_spans_on_one_post_stay_separate():
    segments, _ = resolve(
        staged_sign("a", description=NO_PARKING, distance_ft=100.0),
        staged_sign(
            "b",
            description=f"{NO_STANDING} -->",
            sign_code="PS-2G",
            arrow_direction="North",
            distance_ft=100.0,
        ),
    )

    assert len(segments) == 2
    assert {segment.derived_from for segment in segments} == {("a",), ("b",)}


def test_the_two_sides_of_a_block_resolve_independently():
    segments, report = resolve(
        staged_sign("w", description=NO_PARKING, side="W"),
        staged_sign("e", description=NO_PARKING, side="E"),
    )

    assert report.blockface_sides == 2
    assert {segment.side for segment in segments} == {"E", "W"}


def test_capacity_is_a_car_count_and_is_always_flagged_approximate():
    segments, _ = resolve(staged_sign("a", description=NO_PARKING))

    segment = segments[0]
    assert segment.capacity_cars == int(segment.length_ft // CAR_LENGTH_FT)
    # v1 knows nothing about hydrant, driveway or crosswalk setbacks.
    assert segment.capacity_approximate is True


def test_confidence_is_the_weakest_snap_that_contributed():
    segments, _ = resolve(
        staged_sign("a", description=NO_PARKING, distance_ft=100.0),
        staged_sign("b", description=NO_PARKING, distance_ft=100.0, on_street="BROADE AVENUE"),
    )

    assert len(segments) == 1
    assert segments[0].confidence < 0.9


def test_geometry_is_the_curb_line_offset_towards_the_named_side():
    west, _ = resolve(staged_sign("w", description=NO_PARKING, side="W"))
    east, _ = resolve(staged_sign("e", description=NO_PARKING, side="E"))

    west_lons = [position[0] for position in west[0].geometry["coordinates"]]
    east_lons = [position[0] for position in east[0].geometry["coordinates"]]
    assert max(west_lons) < AVENUE_LON < min(east_lons)
    assert west[0].geometry["type"] == "LineString"
    assert west[0].bbox[0] == pytest.approx(min(west_lons))


def test_non_regulation_panels_never_produce_a_segment():
    segments, report = resolve(
        staged_sign(
            "panel",
            description="LOCAL MTA BUS ROUTE PANEL",
            panel_class="panel:mta_route",
        )
    )

    assert segments == []
    assert report.signs_used == 0


def test_unsnapped_signs_never_produce_a_segment():
    segments, _ = resolve(staged_sign("x", on_street="NOWHERE AVENUE", description=NO_PARKING))

    assert segments == []


# docs/VALIDATION.md §4 D1: the posts on 8 AVE side E, Bleecker -> W 12 St. A
# bus-stop pair (a `<->` post and a single arrow pointing back at it) brackets
# the bus stop, and the metered post beyond it starts a new regime.
BUS_STOP_DOUBLE = "BUS STOP SIGN (BUS & HANDICAP SYMBOLS) NO STANDING <----->"
BUS_STOP_SINGLE = "BUS STOP SIGN (BUS & HANDICAP SYMBOLS) NO STANDING W/ SINGLE ARROW"
TWO_HOUR_METER = "2 HMP 8AM-7PM EXCEPT SUNDAY <->"
FIRST_BLOCK_FT = round(first_block_length_ft())


def eight_avenue_posts():
    return (
        staged_sign(
            "bus-double",
            to_street="E 2 STREET",
            distance_ft=99.0,
            description=BUS_STOP_DOUBLE,
            sign_code="SP-477B",
        ),
        staged_sign(
            "bus-single",
            to_street="E 2 STREET",
            distance_ft=209.0,
            arrow_direction="South",
            description=BUS_STOP_SINGLE,
            sign_code="SP-477BA",
        ),
        staged_sign(
            "meter",
            to_street="E 2 STREET",
            distance_ft=232.0,
            description=TWO_HOUR_METER,
            sign_code="PS-65C",
        ),
    )


def test_a_double_arrow_stops_where_another_familys_post_starts():
    segments, _ = resolve(*eight_avenue_posts())

    metered = [segment for segment in segments if "meter" in segment.derived_from]
    assert spans(metered) == [(209, FIRST_BLOCK_FT)]
    # The bus stop no longer swallows the curb the meter governs.
    assert spans(segments) == [(0, 209), (99, 209), (209, FIRST_BLOCK_FT)]


def test_a_double_arrow_reaches_the_corner_only_where_no_post_lies_beyond():
    # Same three posts, with a second metered post past the corner end: the
    # bus stop keeps its far bound, and the meter now stops at its own family.
    segments, _ = resolve(
        *eight_avenue_posts(),
        staged_sign(
            "meter-far",
            to_street="E 2 STREET",
            distance_ft=300.0,
            description=TWO_HOUR_METER,
            sign_code="PS-65C",
        ),
    )

    assert spans(segments) == [(0, 209), (99, 209), (209, 300), (232, FIRST_BLOCK_FT)]
