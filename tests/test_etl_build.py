"""The database-writing step, over a synthetic raw directory."""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from test_etl_fixtures import AVENUE_LON, CROSS_LATS, grid_rows

from curbcheck import db
from curbcheck.config import NYC_TZ
from curbcheck.engine.resolve import Verdict, evaluate_segment
from curbcheck.engine.search import load_calendar
from curbcheck.engine.window import CalendarContext
from curbcheck.etl.build import build_all
from curbcheck.model import ParseMethod

# 2026-12-25 is a Friday and the ICS calls it a Major Legal Holiday.
CHRISTMAS_ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
DESCRIPTION:Alternate Side Parking suspended for Christmas Day. Parking met
\ters will not be in effect.
DTEND;VALUE=DATE:20261226
DTSTART;VALUE=DATE:20261225
END:VEVENT
END:VCALENDAR
"""

ZONE_M2 = {
    "boro_name": "Manhattan",
    "zone": "Zone M2",
    "rate_zone": "Zone M2 - All Vehicles: $5.00 1st Hour / $8.25 2nd Hour",
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


def sign_row(order_number, description, **overrides):
    """A sign row shaped the way Socrata sends it, on the fixture grid's south block."""
    row = {
        "borough": "Manhattan",
        "record_type": "Current",
        "order_number": order_number,
        "on_street": "BROAD AVENUE",
        "from_street": "E 1 STREET",
        "to_street": "E 2 STREET",
        "side_of_street": "W",
        "distance_from_intersection": "100",
        "sign_code": "PS-1G",
        "sign_description": description,
    }
    row.update(overrides)
    return row


SIGN_ROWS = [
    sign_row("P-1", "NO PARKING ANYTIME <->"),
    sign_row(
        "P-2",
        "PAY-BY-CELL LOCATOR NUMBER",
        on_street="NOWHERE AVENUE",
        side_of_street="E",
        distance_from_intersection="10",
        sign_code="PS-9",
    ),
]


def write_raw(
    directory, sign_rows=SIGN_ROWS, *, blockfaces=(), zones=(ZONE_M2,), ics=CHRISTMAS_ICS
):
    """A minimal `data/raw` for one block: signs, centerline, meters, calendar.

    Every step of `build_all` reads from here, so a test that wants to exercise
    one step still gets a complete, if tiny, snapshot.
    """
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "signs_manhattan.json").write_text(json.dumps(list(sign_rows)))
    (directory / "centerline_manhattan.json").write_text(json.dumps(grid_rows()))
    (directory / "parknyc_blockfaces.json").write_text(json.dumps(list(blockfaces)))
    (directory / "meter_rate_zones.json").write_text(json.dumps(list(zones)))
    if ics is not None:
        calendar = directory / "calendar"
        calendar.mkdir(exist_ok=True)
        (calendar / "2026-alternate-side.ics").write_text(ics, encoding="utf-8")
    return directory


@pytest.fixture
def raw_dir(tmp_path):
    return write_raw(tmp_path / "raw")


def build(raw_dir, tmp_path):
    out_path = tmp_path / "curbcheck.sqlite"
    stats = build_all(raw_dir, out_path)
    return (stats, out_path)


def rows(out_path, sql, parameters=()):
    conn = db.connect(out_path, readonly=True)
    try:
        return [dict(row) for row in conn.execute(sql, parameters)]
    finally:
        conn.close()


def test_build_all_writes_every_geometry_table_and_swaps_the_file_in(raw_dir, tmp_path):
    stats, out_path = build(raw_dir, tmp_path)

    assert out_path.exists()
    assert stats.snap.signs == 2
    assert stats.snap.matched == 1
    conn = db.connect(out_path, readonly=True)
    try:
        counts = {
            table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608
            for table in ("street_node", "street_segment", "sign", "regulation_segment")
        }
        sign = conn.execute("SELECT * FROM sign WHERE order_number = 'P-1'").fetchone()
        panel = conn.execute("SELECT * FROM sign WHERE order_number = 'P-2'").fetchone()
        segment = conn.execute("SELECT * FROM regulation_segment").fetchone()
    finally:
        conn.close()

    assert counts["street_segment"] == len(grid_rows())
    assert counts["sign"] == 2
    # The pay-by-cell plate is kept for audit but never becomes a curb span (D10).
    assert counts["regulation_segment"] == 1
    assert sign["segment_id"] == "avenue-0"
    assert sign["is_regulation"] == 1
    assert sign["derived_lon"] < -73.9880
    assert panel["panel_class"] == "panel:pay_by_cell"
    assert panel["segment_id"] is None
    assert json.loads(segment["derived_from"]) == [sign["sign_id"]]
    assert segment["capacity_approximate"] == 1
    assert json.loads(segment["geom"])["type"] == "LineString"


def test_the_steps_run_in_an_order_that_lets_each_read_the_last(raw_dir, tmp_path):
    # parse before segments (the family key uses the parsed action), segments
    # before the regulation rows that reference them, and both before meters,
    # which is driven by which segments came out metered.
    blockfaces = [
        {
            "borough": "Manhattan",
            "on_street": "Broad Avenue",
            "from_stree": "E 1 Street",
            "to_street": "E 2 Street",
            "side_of_st": "W",
            "meter_rate": "Zone M2",
            "pay_by_cel": "100001",
            "all_vehicl": "2 Hours",
            "all_vehi_2": "$5.00 1st Hour / $8.25 2nd Hour",
        }
    ]
    write_raw(
        raw_dir,
        [sign_row("P-3", "2 HOUR METERED PARKING 8AM-7PM EXCEPT SUNDAY <->")],
        blockfaces=blockfaces,
    )

    stats, out_path = build(raw_dir, tmp_path)

    [regulation] = rows(out_path, "SELECT * FROM regulation")
    [segment] = rows(out_path, "SELECT * FROM regulation_segment")
    [rate] = rows(out_path, "SELECT * FROM meter_rate")
    assert regulation["reg_seg_id"] == segment["reg_seg_id"]
    assert regulation["metered"] == 1
    assert rate["segment_id"] == segment["segment_id"]
    assert json.loads(rate["hour_rates"]) == ["5.00", "8.25"]
    assert stats.meters.metered_segments == 1
    assert stats.meters.metered_segments_without_rate == 0
    assert stats.calendar.distinct_dates == 1


def test_an_unparsed_sign_still_gets_a_prohibitive_placeholder_row(raw_dir, tmp_path):
    # docs/DECISIONS.md D13: no row would make the dangerous sign invisible to
    # the stacking engine, and the block would read as a confident LEGAL.
    write_raw(raw_dir, [sign_row("P-4", "QQQQ WIBBLE ZORK")])

    stats, out_path = build(raw_dir, tmp_path)

    [regulation] = rows(out_path, "SELECT * FROM regulation")
    assert regulation["parse_method"] == ParseMethod.UNPARSED.value
    assert regulation["parse_confidence"] == 0.0
    assert regulation["permitted"] == 0
    assert regulation["days_mask"] == 0b1111111
    assert regulation["time_from"] is None
    assert regulation["raw_sign_description"] == "QQQQ WIBBLE ZORK"
    assert stats.parse.unparsed_signs == 1
    assert _verdict(out_path, regulation["reg_seg_id"]) is Verdict.AMBIGUOUS


def test_a_meta_sign_makes_the_metered_rule_on_its_post_ambiguous(raw_dir, tmp_path):
    # SPEC §8.5: the meta sign is not a standalone rule. It shares a post with
    # the metered panel above it, carries no arrow of its own, and we cannot yet
    # read which times "ABOVE TIMES" points at (docs/DECISIONS.md D16).
    write_raw(
        raw_dir,
        [
            sign_row(
                "P-5", "2 HOUR METERED PARKING 8AM-7PM EXCEPT SUNDAY -->", arrow_direction="North"
            ),
            sign_row("P-6", "METERS ARE NOT IN EFFECT ABOVE TIMES", sign_code="PS-9"),
        ],
    )

    stats, out_path = build(raw_dir, tmp_path)

    metered = rows(out_path, "SELECT * FROM regulation WHERE metered = 1")
    assert len(metered) == 1
    assert "METERS ARE NOT IN EFFECT" in metered[0]["parse_notes"]
    stack = rows(
        out_path, "SELECT * FROM regulation WHERE reg_seg_id = ?", (metered[0]["reg_seg_id"],)
    )
    meta = next(row for row in stack if json.loads(row["flags"])["meta"])
    assert meta["parse_confidence"] == 0.7
    assert stats.parse.meta_segments >= 1
    assert _verdict(out_path, metered[0]["reg_seg_id"]) is Verdict.AMBIGUOUS


def test_a_single_arrow_is_resolved_against_the_bearing_and_the_chain(raw_dir, tmp_path):
    # The fixture avenue is digitized south to north, so a north-pointing arrow
    # runs with increasing distance and a south-pointing one runs against it.
    write_raw(
        raw_dir,
        [
            sign_row("P-7", "NO PARKING ANYTIME -->", arrow_direction="North"),
            sign_row(
                "P-8",
                "NO STANDING ANYTIME -->",
                arrow_direction="South",
                sign_code="PS-2",
                distance_from_intersection="200",
            ),
        ],
    )

    _stats, out_path = build(raw_dir, tmp_path)

    arrows = {
        row["raw_sign_description"]: row["arrow"]
        for row in rows(out_path, "SELECT raw_sign_description, arrow FROM regulation")
    }
    assert arrows["NO PARKING ANYTIME -->"] == "forward"
    assert arrows["NO STANDING ANYTIME -->"] == "backward"


def test_a_sign_with_no_readable_bearing_governs_the_whole_blockface(raw_dir, tmp_path):
    write_raw(raw_dir, [sign_row("P-9", "NO PARKING ANYTIME -->")])

    _stats, out_path = build(raw_dir, tmp_path)

    [regulation] = rows(out_path, "SELECT * FROM regulation")
    assert regulation["arrow"] == "none"


def test_the_calendar_lands_in_asp_suspension_and_the_engine_reads_it(raw_dir, tmp_path):
    _stats, out_path = build(raw_dir, tmp_path)

    [suspension] = rows(out_path, "SELECT * FROM asp_suspension")
    assert suspension["date"] == "2026-12-25"
    assert suspension["is_major_legal_holiday"] == 1
    assert suspension["meters_suspended"] == 1

    conn = db.connect(out_path, readonly=True)
    try:
        calendar = load_calendar(conn)
    finally:
        conn.close()
    assert calendar.meters_in_effect(datetime(2026, 12, 25, tzinfo=NYC_TZ).date()) is False


def test_a_missing_calendar_is_reported_rather_than_fatal(raw_dir, tmp_path):
    write_raw(raw_dir, ics=None)
    (raw_dir / "calendar" / "2026-alternate-side.ics").unlink(missing_ok=True)

    stats, out_path = build(raw_dir, tmp_path)

    assert stats.calendar.source_path is None
    assert rows(out_path, "SELECT * FROM asp_suspension") == []
    meta = {row["key"]: row["value"] for row in rows(out_path, "SELECT key, value FROM sync_meta")}
    assert meta["calendar_missing"] == "1"


def test_coverage_numbers_land_in_sync_meta(raw_dir, tmp_path):
    _stats, out_path = build(raw_dir, tmp_path)

    meta = {row["key"]: row["value"] for row in rows(out_path, "SELECT key, value FROM sync_meta")}

    # SPEC §11: the miss has to be attributable, not just counted.
    assert json.loads(meta["snap_unmatched_reason_classes"]) == {"no_name_match": 1}
    assert meta["signs_snapped"] == "1"
    assert json.loads(meta["signs_by_panel_class"]) == {"regulation": 1, "panel:pay_by_cell": 1}
    assert json.loads(meta["regulation_rows_by_parse_method"]) == {"grammar": 1}
    assert meta["parse_arrow_disagreements"] == "0"
    assert "last_sync_at" in meta


def test_the_previous_database_is_kept_for_rollback(raw_dir, tmp_path):
    build(raw_dir, tmp_path)
    _stats, out_path = build(raw_dir, tmp_path)

    assert out_path.with_name(out_path.name + ".prev").exists()


def _verdict(out_path, reg_seg_id):
    """Run the engine over one segment's stack, the way `search` would."""
    from curbcheck.engine.resolve import RegulationWithMeta

    stack = [
        RegulationWithMeta(
            regulation=db.regulation_from_row(row),
            parse_method=ParseMethod(row["parse_method"]),
            parse_confidence=row["parse_confidence"],
            reg_seg_id=reg_seg_id,
            raw_sign_description=row["raw_sign_description"],
        )
        for row in rows(out_path, "SELECT * FROM regulation WHERE reg_seg_id = ?", (reg_seg_id,))
    ]
    t1 = datetime(2026, 9, 16, 10, 0, tzinfo=NYC_TZ)
    t2 = datetime(2026, 9, 16, 12, 0, tzinfo=NYC_TZ)
    return evaluate_segment(stack, t1, t2, CalendarContext()).verdict
