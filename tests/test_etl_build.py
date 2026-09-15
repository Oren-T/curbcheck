"""The database-writing step, over a synthetic raw directory."""

from __future__ import annotations

import json

import pytest
from test_etl_fixtures import grid_rows

from curbcheck import db
from curbcheck.etl.build import build_all, run_calendar, run_meters, run_parse

SIGN_ROWS = [
    {
        "borough": "Manhattan",
        "record_type": "Current",
        "order_number": "P-1",
        "on_street": "BROAD AVENUE",
        "from_street": "E 1 STREET",
        "to_street": "E 2 STREET",
        "side_of_street": "W",
        "distance_from_intersection": "100",
        "sign_code": "PS-1G",
        "sign_description": "NO PARKING ANYTIME <->",
    },
    {
        "borough": "Manhattan",
        "record_type": "Current",
        "order_number": "P-2",
        "on_street": "NOWHERE AVENUE",
        "from_street": "E 1 STREET",
        "to_street": "E 2 STREET",
        "side_of_street": "E",
        "distance_from_intersection": "10",
        "sign_code": "PS-9",
        "sign_description": "PAY-BY-CELL LOCATOR NUMBER",
    },
]


@pytest.fixture
def raw_dir(tmp_path):
    directory = tmp_path / "raw"
    directory.mkdir()
    (directory / "signs_manhattan.json").write_text(json.dumps(SIGN_ROWS))
    (directory / "centerline_manhattan.json").write_text(json.dumps(grid_rows()))
    return directory


def test_build_all_writes_every_geometry_table_and_swaps_the_file_in(raw_dir, tmp_path):
    out_path = tmp_path / "curbcheck.sqlite"

    stats = build_all(raw_dir, out_path)

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
    assert panel["panel_class"] == "pay_by_cell"
    assert panel["segment_id"] is None
    assert json.loads(segment["derived_from"]) == [sign["sign_id"]]
    assert segment["capacity_approximate"] == 1
    assert json.loads(segment["geom"])["type"] == "LineString"


def test_coverage_numbers_land_in_sync_meta(raw_dir, tmp_path):
    out_path = tmp_path / "curbcheck.sqlite"

    build_all(raw_dir, out_path)

    conn = db.connect(out_path, readonly=True)
    try:
        meta = {
            row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM sync_meta")
        }
    finally:
        conn.close()

    # SPEC §11: the miss has to be attributable, not just counted.
    assert json.loads(meta["snap_unmatched_reason_classes"]) == {"no_name_match": 1}
    assert meta["signs_snapped"] == "1"
    assert json.loads(meta["signs_by_panel_class"]) == {"regulation": 1, "pay_by_cell": 1}
    assert "last_sync_at" in meta


def test_the_previous_database_is_kept_for_rollback(raw_dir, tmp_path):
    out_path = tmp_path / "curbcheck.sqlite"

    build_all(raw_dir, out_path)
    build_all(raw_dir, out_path)

    assert out_path.with_name(out_path.name + ".prev").exists()


@pytest.mark.parametrize("step", [run_parse, run_meters, run_calendar])
def test_the_steps_that_are_not_wired_in_yet_say_so(step):
    with pytest.raises(NotImplementedError):
        step(None) if step is run_parse else step(None, None)
