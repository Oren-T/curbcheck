"""Spans projected off their chain onto centerline segments (docs/DECISIONS.md D26).

DOT files some signs against `1 AVE, E 16 ST -> E 18 ST` and others against
`1 AVE, E 16 ST -> E 17 ST`. Those are two chains over one piece of curb, and
until the spans were re-expressed in each segment's own frame they were two
independent stacks: 175 pairs of them covered the same curb from different
chains (docs/VALIDATION.md §10.2).
"""

from __future__ import annotations

import random
from datetime import datetime
from itertools import pairwise

import pytest
from test_etl_fixtures import (
    NORTH_SEGMENT,
    SOUTH_SEGMENT,
    TWO_CHAIN_LON,
    staged_sign,
    two_chain_graph,
)

from curbcheck.config import NYC_TZ
from curbcheck.engine.resolve import RegulationWithMeta, Verdict, evaluate_segment
from curbcheck.engine.window import CalendarContext
from curbcheck.etl.parse import parse_description
from curbcheck.etl.segments import (
    LEFT_SIDE,
    RIGHT_SIDE,
    _local_side,
    reading_of,
    resolve_segments,
)
from curbcheck.etl.snap import snap_sign
from curbcheck.model import ParseMethod

BAN = "NO STANDING ANYTIME <->"
METER = "2 HMP 8AM-7PM EXCEPT SUNDAY <->"
NO_PARKING = "NO PARKING ANYTIME"

WEDNESDAY_MORNING = (
    datetime(2026, 9, 16, 10, 0, tzinfo=NYC_TZ),
    datetime(2026, 9, 16, 11, 0, tzinfo=NYC_TZ),
)


def avenue_sign(sign_id, *, to_street, **kwargs):
    return staged_sign(
        sign_id,
        on_street="1 AVENUE",
        from_street="E 16 STREET",
        to_street=to_street,
        **kwargs,
    )


def resolve(*signs, with_graph=False):
    graph = two_chain_graph()
    snaps = [snap_sign(sign, graph) for sign in signs]
    readings = {
        sign.sign_description: reading_of(parse_description(sign.sign_description))
        for sign in signs
    }
    return resolve_segments(snaps, readings=readings, graph=graph if with_graph else None)


def two_chain_posts():
    """A ban on the short chain and a meter on the long one, over the same curb.

    Each is the only post on its own blockface-side, so each governs the whole
    of it: the ban covers E 16 ST -> E 17 ST and the meter covers
    E 16 ST -> E 18 ST. The shared segment is under both.
    """
    return (
        avenue_sign("ban", to_street="E 17 STREET", description=BAN, sign_code="PS-2G"),
        avenue_sign("meter", to_street="E 18 STREET", description=METER, sign_code="PS-65C"),
    )


def test_two_chains_over_one_segment_reach_it_as_a_single_stack():
    segments, _ = resolve(*two_chain_posts())

    by_segment = {segment.segment_id: segment for segment in segments}
    assert len(segments) == 2
    assert by_segment[SOUTH_SEGMENT].derived_from == ("ban", "meter")
    assert by_segment[NORTH_SEGMENT].derived_from == ("meter",)
    assert by_segment[SOUTH_SEGMENT].start_ft == 0.0
    assert by_segment[SOUTH_SEGMENT].end_ft == pytest.approx(364.0, abs=2.0)


def test_the_shared_segment_reads_illegal_and_the_curb_past_it_stays_legal():
    """The whole point: `resolve` settles the ban and the meter in one stack.

    Before the projection the meter was a row of its own over the whole chain
    and read LEGAL over curb the ban covers, which is SPEC §8.6's P0 defect.
    """
    signs = two_chain_posts()
    segments, _ = resolve(*signs)
    descriptions = {sign.sign_id: sign.sign_description for sign in signs}

    verdicts = {
        segment.segment_id: evaluate_segment(
            [
                RegulationWithMeta(
                    regulation=rule,
                    parse_method=ParseMethod.GRAMMAR,
                    parse_confidence=0.98,
                    reg_seg_id=segment.reg_seg_id,
                    raw_sign_description=descriptions[sign_id],
                )
                for sign_id in segment.derived_from
                for rule in parse_description(descriptions[sign_id]).regulations
            ],
            *WEDNESDAY_MORNING,
            CalendarContext(),
        ).verdict
        for segment in segments
    }

    assert verdicts[SOUTH_SEGMENT] is Verdict.ILLEGAL
    assert verdicts[NORTH_SEGMENT] is Verdict.LEGAL


def test_a_chain_running_against_a_segment_measures_the_span_the_segments_own_way():
    # The chain from E 16 ST enters the south segment at that segment's *end*,
    # because CSCL digitized it from E 17 ST southwards. A span 100-250 ft along
    # the chain is therefore 114-264 ft along the segment.
    segments, _ = resolve(
        avenue_sign(
            "arrow",
            to_street="E 18 STREET",
            description=f"{NO_PARKING} -->",
            arrow_direction="North",
            distance_ft=100.0,
        ),
        avenue_sign("stop", to_street="E 18 STREET", description=NO_PARKING, distance_ft=250.0),
    )

    span = next(s for s in segments if s.derived_from == ("arrow", "stop"))
    assert span.segment_id == SOUTH_SEGMENT
    assert (span.start_ft, span.end_ft) == pytest.approx((114.0, 264.0), abs=2.0)


def test_one_compass_letter_names_opposite_curbs_of_two_oppositely_drawn_segments():
    # The south segment is digitized north-to-south and the north segment
    # south-to-north, so the same west curb is the right-hand one of the first
    # and the left-hand one of the second. That is why the stack key cannot be
    # the compass letter on its own.
    graph = two_chain_graph()
    south = graph.segments[SOUTH_SEGMENT].line_ft
    north = graph.segments[NORTH_SEGMENT].line_ft

    assert _local_side(south, "W") == RIGHT_SIDE
    assert _local_side(north, "W") == LEFT_SIDE
    assert _local_side(south, "E") == LEFT_SIDE
    assert _local_side(north, "E") == RIGHT_SIDE


def test_the_curb_line_of_a_west_sign_lies_west_of_the_avenue_either_way_round():
    segments, _ = resolve(
        avenue_sign("short", to_street="E 17 STREET", description=NO_PARKING),
        avenue_sign("long", to_street="E 18 STREET", description=NO_PARKING),
    )

    for segment in segments:
        lons = [position[0] for position in segment.geometry["coordinates"]]
        assert max(lons) < TWO_CHAIN_LON, segment.segment_id


CHAINS = ("E 17 STREET", "E 18 STREET")
DESCRIPTIONS = (
    NO_PARKING,
    BAN,
    METER,
    f"{NO_PARKING} <->",
    f"{NO_PARKING} -->",
    "NO STANDING 8AM-6PM <->",
)
CODES = ("PS-1G", "PS-2G", "PS-65C", "PS-40A")
BEARINGS = (None, "North", "South")


def random_posts(rng):
    return [
        avenue_sign(
            f"sign-{index}",
            to_street=rng.choice(CHAINS),
            side=rng.choice(("E", "W")),
            distance_ft=round(rng.uniform(0.0, 700.0), 1),
            description=rng.choice(DESCRIPTIONS),
            sign_code=rng.choice(CODES),
            arrow_direction=rng.choice(BEARINGS),
        )
        for index in range(rng.randint(2, 9))
    ]


@pytest.mark.parametrize("seed", range(40))
def test_random_posts_on_two_chains_never_leave_two_spans_over_one_foot(seed):
    # The invariant the projection exists to hold, over posts drawn from every
    # shape of span on both chains at once. `--overlaps` is the same check
    # borough-wide (scripts/validation_regress.py).
    segments, _ = resolve(*random_posts(random.Random(seed)))

    curbs: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for segment in segments:
        curbs.setdefault((segment.segment_id, segment.side), []).append(
            (segment.start_ft, segment.end_ft)
        )
    for key, extents in curbs.items():
        extents.sort()
        assert all(end > start for start, end in extents), (key, extents)
        assert all(earlier[1] <= later[0] for earlier, later in pairwise(extents)), (key, extents)


@pytest.mark.parametrize("seed", range(40))
def test_a_segment_side_with_a_real_span_never_also_draws_grey(seed):
    # D23's invariant, re-checked against the segment-local key: the placeholder
    # is per side, and a side that has any real span must not get one.
    segments, report = resolve(*random_posts(random.Random(seed)), with_graph=True)

    real = {(s.segment_id, s.side) for s in segments if s.gap_kind is None}
    grey = [(s.segment_id, s.side) for s in segments if s.gap_kind is not None]
    assert not real & set(grey)
    assert len(grey) == len(set(grey))
    assert report.sides_with_rules == len(real)
