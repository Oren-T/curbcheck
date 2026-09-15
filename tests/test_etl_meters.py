"""The ParkNYC blockface -> centerline rate join."""

from __future__ import annotations

import json

import pytest
from test_etl_fixtures import AVENUE_LON, CROSS_LATS, grid_graph

from curbcheck.etl.meters import (
    SOURCE_PARKNYC,
    SOURCE_RATE_ZONE,
    parse_hour_rates,
    parse_max_session_min,
    resolve_meter_rates,
    side_code,
    text_or_none,
)

# The fixture grid's BROAD AVE runs north along lon -73.9880 from E 1 ST to E 4 ST.
BLOCK_SOUTH = [[AVENUE_LON, CROSS_LATS[0]], [AVENUE_LON, CROSS_LATS[1]]]
FAR_AWAY = [[AVENUE_LON + 0.01, CROSS_LATS[0]], [AVENUE_LON + 0.01, CROSS_LATS[1]]]

ZONE_M2 = {
    "boro_name": "Manhattan",
    "zone": "Zone M2",
    "zone_name": "Manhattan Neighborhood",
    "rate_zone": (
        "Zone M2 - Commercial Vehicles: $6.00 1st Hour / $9.00 2nd Hour / $12.00 3rd Hour, "
        "All Vehicles: $5.00 1st Hour / $8.25 2nd Hour"
    ),
    "the_geom": {
        "type": "Polygon",
        "coordinates": [
            [
                [AVENUE_LON - 0.01, CROSS_LATS[0] - 0.01],
                [AVENUE_LON + 0.01, CROSS_LATS[0] - 0.01],
                [AVENUE_LON + 0.01, CROSS_LATS[-1] + 0.01],
                [AVENUE_LON - 0.01, CROSS_LATS[-1] + 0.01],
                [AVENUE_LON - 0.01, CROSS_LATS[0] - 0.01],
            ]
        ],
    },
}


def blockface(**overrides):
    """One ParkNYC row on the fixture grid's southernmost BROAD AVE block."""
    row = {
        "borough": "Manhattan",
        "on_street": "Broad Avenue",
        "from_stree": "E 1 Street",
        "to_street": "E 2 Street",
        "side_of_st": "W",
        "meter_rate": "Zone M2",
        "pay_by_cel": "100001",
        "vehicle_ty": "All Vehicles",
        "all_vehicl": "2 Hours",
        "all_vehi_1": "Monday-Saturday 9 AM-7 PM",
        "all_vehi_2": "$5.00 1st Hour / $8.25 2nd Hour",
        "all_vehi_3": "$13.25",
        "commercial": "N/A",
        "commerci_1": "N/A",
        "commerci_2": "N/A",
        "commerci_3": "N/A",
        "the_geom": {"type": "MultiLineString", "coordinates": [BLOCK_SOUTH]},
    }
    row.update(overrides)
    return row


@pytest.fixture
def raw_dir(tmp_path):
    def write(blockfaces, zones=(ZONE_M2,)):
        (tmp_path / "parknyc_blockfaces.json").write_text(json.dumps(blockfaces))
        (tmp_path / "meter_rate_zones.json").write_text(json.dumps(list(zones)))
        return tmp_path

    return write


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$5.00 1st Hour / $8.25 2nd Hour", ("5.00", "8.25")),
        ("$5.50 1st Hour / $9.00 2nd Hour / $5.50 Add'l Hours", ("5.50", "9.00", "5.50")),
        ("$7.00 1st Hour / $10.00 2nd Hour / $13.00 3rd Hour", ("7.00", "10.00", "13.00")),
        ("$1.50 per Hour", ("1.50",)),
        ("$6.00 1st Hour", ("6.00",)),
        # Never invent: a shape we do not recognize yields no rate at all, and
        # the engine then reports price_known=false (SPEC §13.1d).
        ("$7.00 per 30 Minutes", ()),
        ("N/A", ()),
        ("", ()),
        (None, ()),
        # Out-of-order ordinals mean we have misread the string, not that the
        # second hour is free.
        ("$5.00 2nd Hour / $8.25 1st Hour", ()),
    ],
)
def test_progressive_rates_are_read_in_hour_order_or_not_at_all(text, expected):
    assert parse_hour_rates(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2 Hours", 120),
        ("30 Minutes", 30),
        ("6 Hours", 360),
        # Two posted limits: keep the shorter one, which cannot cost a ticket.
        ("1 Hour, 2 Hours (Mon-Fri after 4PM, Sat)", 60),
        ("N/A", None),
    ],
)
def test_the_shortest_posted_session_limit_wins(text, expected):
    assert parse_max_session_min(text) == expected


def test_the_literal_na_reads_as_a_null_everywhere():
    assert text_or_none("N/A") is None
    assert text_or_none("  ") is None
    assert text_or_none("Zone M2") == "Zone M2"


def test_side_letters_fold_to_the_snap_convention():
    assert side_code("s") == "S"
    assert side_code("W") == "W"
    assert side_code("") is None
    assert side_code("Curb") is None


def test_the_name_join_finds_the_block_and_keeps_full_confidence(raw_dir):
    rates, report = resolve_meter_rates(raw_dir([blockface()]), grid_graph())

    assert report.manhattan_rows == 1
    assert report.matched == 1
    assert report.unmatched == 0
    [rate] = rates
    assert rate.segment_id == "avenue-0"
    assert rate.side == "W"
    assert rate.rate_label == "Zone M2"
    assert rate.hour_rates == ("5.00", "8.25")
    assert rate.max_session_min == 120
    assert rate.source == SOURCE_PARKNYC
    assert rate.confidence == 1.0


def test_the_borough_filter_folds_case(raw_dir):
    rows = [blockface(borough="MANHATTAN"), blockface(borough="Brooklyn", pay_by_cel="2")]

    _rates, report = resolve_meter_rates(raw_dir(rows), grid_graph())

    assert report.source_rows == 2
    assert report.manhattan_rows == 1


def test_a_blockface_spanning_several_segments_is_written_once_per_segment(raw_dir):
    # The engine looks a rate up by the segment a span's midpoint falls on, so a
    # three-segment blockface needs a row on each of them to be found at all.
    row = blockface(to_street="E 4 Street", the_geom=None)

    rates, report = resolve_meter_rates(raw_dir([row]), grid_graph())

    assert report.matched == 1
    assert sorted(rate.segment_id for rate in rates) == ["avenue-0", "avenue-1", "avenue-2"]
    assert len({rate.blockface_id for rate in rates}) == 3


def test_a_blockface_whose_geometry_lands_elsewhere_keeps_its_rate_but_loses_confidence(raw_dir):
    row = blockface(the_geom={"type": "MultiLineString", "coordinates": [FAR_AWAY]})

    [rate], report = resolve_meter_rates(raw_dir([row]), grid_graph())

    assert report.geometry_mismatches == 1
    assert rate.hour_rates == ("5.00", "8.25")
    assert rate.confidence < 1.0


def test_an_unjoinable_blockface_is_counted_with_its_reason(raw_dir):
    row = blockface(on_street="Nowhere Avenue")

    rates, report = resolve_meter_rates(raw_dir([row]), grid_graph())

    assert rates == []
    assert report.unmatched == 1
    assert report.unmatched_reasons == {"on_street_not_in_centerline": 1}


def test_two_zones_on_one_key_are_both_kept_and_counted(raw_dir):
    # SPEC §13.1(b): a blockface may carry more than one zone. Both rows survive
    # so the search can quote the dearest and say "confirm at the meter".
    rows = [
        blockface(),
        blockface(
            pay_by_cel="100002",
            meter_rate="Zone M1",
            all_vehi_2="$5.50 1st Hour / $9.00 2nd Hour",
        ),
    ]

    rates, report = resolve_meter_rates(raw_dir(rows), grid_graph())

    assert len(rates) == 2
    assert report.many_zones_per_blockface == 1
    assert report.matched_one_to_one == 0
    assert {rate.hour_rates for rate in rates} == {("5.00", "8.25"), ("5.50", "9.00")}


def test_a_metered_segment_with_no_parknyc_row_falls_back_to_the_zone_polygon(raw_dir):
    midpoint_geom = json.dumps({"type": "LineString", "coordinates": BLOCK_SOUTH})
    metered = [("seg-1", "avenue-0", "E", midpoint_geom)]

    rates, report = resolve_meter_rates(raw_dir([]), grid_graph(), metered_segments=metered)

    assert report.rate_zone_fallbacks == 1
    assert report.metered_segments_without_rate == 0
    [rate] = rates
    assert rate.source == SOURCE_RATE_ZONE
    assert rate.rate_label == "Zone M2"
    assert rate.hour_rates == ("5.00", "8.25")
    assert rate.commercial_hour_rates == ("6.00", "9.00", "12.00")
    assert rate.confidence < 1.0


def test_a_commercial_only_blockface_still_gets_a_passenger_zone_rate(raw_dir):
    # 620 Manhattan blockfaces publish commercial rates only. A row of theirs
    # answers no question a passenger query asks, so the polygon still applies.
    row = blockface(
        all_vehi_2="N/A",
        all_vehicl="N/A",
        commerci_2="$6.00 1st Hour / $9.00 2nd Hour / $12.00 3rd Hour",
    )
    metered = [
        ("seg-1", "avenue-0", "W", json.dumps({"type": "LineString", "coordinates": BLOCK_SOUTH}))
    ]

    rates, report = resolve_meter_rates(raw_dir([row]), grid_graph(), metered_segments=metered)

    assert report.rate_zone_fallbacks == 1
    sources = {rate.source: rate for rate in rates}
    assert sources[SOURCE_PARKNYC].hour_rates == ()
    assert sources[SOURCE_PARKNYC].commercial_hour_rates == ("6.00", "9.00", "12.00")
    assert sources[SOURCE_RATE_ZONE].hour_rates == ("5.00", "8.25")


def test_a_metered_segment_outside_every_zone_is_left_unpriced(raw_dir):
    metered = [
        ("seg-1", "avenue-0", "E", json.dumps({"type": "LineString", "coordinates": BLOCK_SOUTH}))
    ]

    rates, report = resolve_meter_rates(
        raw_dir([], zones=()), grid_graph(), metered_segments=metered
    )

    assert rates == []
    assert report.rate_zone_fallbacks == 0
    assert report.metered_segments_without_rate == 1
