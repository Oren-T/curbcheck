"""One real block end to end. Skipped when the raw snapshots are not on disk.

The block is the one in the spec's own sample record (SPEC §5.1): sign
P-01667780 on the west side of 3 AVE, 44 ft north of E 85 ST. docs/DATA.md §2.3
works it out by hand; this checks the pipeline reaches the same answer.
"""

from __future__ import annotations

import pytest

from curbcheck.config import RAW_DIR
from curbcheck.etl.fetch import load_rows
from curbcheck.etl.segments import resolve_segments
from curbcheck.etl.snap import snap_sign
from curbcheck.etl.stage import stage_centerline, stage_signs
from curbcheck.etl.streets import build_graph

SAMPLE_ORDER = "P-01667780"
SAMPLE_CODE = "PS-127C"
SAMPLE_SEGMENT = "3681"

pytestmark = pytest.mark.skipif(
    not (RAW_DIR / "signs_manhattan.json").exists(),
    reason="needs the data/raw snapshots; run `curbcheck sync`",
)


@pytest.fixture(scope="module")
def graph():
    return build_graph(stage_centerline(load_rows("centerline_manhattan")))


@pytest.fixture(scope="module")
def block_signs():
    signs, _ = stage_signs(load_rows("signs_manhattan"))
    return [
        sign
        for sign in signs
        if sign.on_street == "3 AVENUE"
        and {sign.from_street, sign.to_street} == {"EAST   85 STREET", "EAST   86 STREET"}
    ]


def test_the_spec_sample_sign_lands_on_the_west_curb_of_physicalid_3681(graph, block_signs):
    sample = next(
        sign
        for sign in block_signs
        if sign.order_number == SAMPLE_ORDER and sign.sign_code == SAMPLE_CODE
    )

    result = snap_sign(sample, graph)

    assert result.matched
    assert result.segment_id == SAMPLE_SEGMENT
    assert result.distance_ft == pytest.approx(44.0)
    assert result.block is not None
    assert result.block.is_single_segment
    assert result.block.length_ft == pytest.approx(284.8, abs=1.0)
    assert result.block.width_ft == pytest.approx(70.0)
    # The block is digitized from E 85 ST to E 86 ST, so the post is 44 ft north
    # of E 85 ST and half the 70 ft roadbed west of the centerline.
    node = graph.nodes[result.block.from_node]
    assert "E 85 ST" in node.street_norms
    assert result.derived_lat is not None and result.derived_lat > node.lat
    assert result.derived_lon is not None and result.derived_lon < node.lon
    assert result.snap_confidence > 0.9
    assert result.snap_notes == ()


def test_the_whole_block_resolves_into_curb_spans_on_both_sides(graph, block_signs):
    snaps = [snap_sign(sign, graph) for sign in block_signs]
    segments, report = resolve_segments(snaps)

    assert all(snap.matched for snap in snaps)
    assert {snap.segment_id for snap in snaps} == {SAMPLE_SEGMENT}
    assert {segment.side for segment in segments} == {"E", "W"}
    assert report.blockface_sides == 2
    for segment in segments:
        assert 0.0 <= segment.start_ft < segment.end_ft <= 286.0
        assert segment.capacity_cars == int(segment.length_ft // 22)
