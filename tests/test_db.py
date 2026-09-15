"""Schema, connection posture, and the Regulation round trip the ETL and engine share."""

from __future__ import annotations

import sqlite3

import pytest

from curbcheck import db
from curbcheck.model import Action, Arrow, Flags, ParseMethod, Regulation, VehicleClass

EXPECTED_TABLES = {
    "sign",
    "street_segment",
    "street_node",
    "regulation_segment",
    "regulation",
    "meter_rate",
    "asp_suspension",
    "sync_meta",
}


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = db.connect(":memory:")
    db.create_schema(connection)
    return connection


def table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {str(row["name"]) for row in rows}


def test_create_schema_creates_every_documented_table(conn: sqlite3.Connection) -> None:
    assert EXPECTED_TABLES <= table_names(conn)


def test_create_schema_is_idempotent(conn: sqlite3.Connection) -> None:
    db.create_schema(conn)

    assert EXPECTED_TABLES <= table_names(conn)


def test_regulation_segment_bbox_columns_are_indexed(conn: sqlite3.Connection) -> None:
    indexes = conn.execute("PRAGMA index_list('regulation_segment')").fetchall()
    indexed_columns = set()
    for index in indexes:
        # The table-valued form of the pragma is the only one that takes a bound parameter.
        for column in conn.execute("SELECT name FROM pragma_index_info(?)", (index["name"],)):
            indexed_columns.add(str(column["name"]))

    assert {"min_lon", "max_lon", "min_lat", "max_lat"} <= indexed_columns


def test_rows_are_accessible_by_column_name(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO sync_meta (key, value) VALUES (?, ?)", ("last_sync", "2026-09-14"))

    row = conn.execute("SELECT key, value FROM sync_meta").fetchone()

    assert row["value"] == "2026-09-14"


def test_foreign_keys_are_enforced(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO regulation (reg_id, reg_seg_id, action, permitted, vehicle_class,"
            " days_mask, parse_method, parse_confidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("r1", "missing-segment", "park", 0, "all", 127, "grammar", 1.0),
        )


def test_days_bitmask_puts_monday_in_bit_zero_and_sunday_in_bit_six() -> None:
    assert db.days_to_mask([0]) == 1
    assert db.days_to_mask([6]) == 64
    assert db.days_to_mask([0, 3]) == 0b1001
    assert db.days_from_mask(0b1001) == [0, 3]
    assert db.days_from_mask(127) == [0, 1, 2, 3, 4, 5, 6]


def test_regulation_survives_a_write_and_read_round_trip(conn: sqlite3.Connection) -> None:
    reg = Regulation(
        action=Action.PARK,
        permitted=True,
        vehicle_class=VehicleClass.PASSENGER,
        exclusive=True,
        days=[0, 3, 6],
        time_from="09:00",
        time_to="19:00",
        metered=True,
        max_duration_min=120,
        flags=Flags(street_cleaning=True, including_sunday=True),
        effective_from="06-01",
        effective_to="11-30",
        arrow=Arrow.BOTH,
    )
    _insert_segment(conn)
    conn.execute(
        db.INSERT_REGULATION_SQL,
        db.regulation_to_params(
            reg,
            reg_id="r1",
            reg_seg_id="seg-1",
            raw_sign_description="2 HMP SATURDAY 8AM-7PM",
            parse_method=ParseMethod.GRAMMAR,
            parse_confidence=0.95,
        ),
    )

    row = conn.execute("SELECT * FROM regulation WHERE reg_id = ?", ("r1",)).fetchone()

    assert db.regulation_from_row(row) == reg
    assert row["parse_method"] == "grammar"
    assert row["raw_sign_description"] == "2 HMP SATURDAY 8AM-7PM"


def test_a_rule_with_no_times_round_trips_as_all_day(conn: sqlite3.Connection) -> None:
    reg = Regulation(action=Action.STAND, permitted=False)
    _insert_segment(conn)
    conn.execute(
        db.INSERT_REGULATION_SQL,
        db.regulation_to_params(
            reg,
            reg_id="r2",
            reg_seg_id="seg-1",
            raw_sign_description="NO STANDING ANYTIME",
            parse_method=ParseMethod.GRAMMAR,
            parse_confidence=1.0,
        ),
    )

    row = conn.execute("SELECT * FROM regulation WHERE reg_id = ?", ("r2",)).fetchone()
    restored = db.regulation_from_row(row)

    assert restored.time_from is None
    assert restored.days == [0, 1, 2, 3, 4, 5, 6]


def test_readonly_connection_rejects_writes(tmp_path) -> None:
    path = tmp_path / "curbcheck.sqlite"
    writable = db.connect(path)
    db.create_schema(writable)
    writable.close()

    reader = db.connect(path, readonly=True)

    with pytest.raises(sqlite3.OperationalError):
        reader.execute("INSERT INTO sync_meta (key, value) VALUES (?, ?)", ("k", "v"))


def test_the_built_database_is_not_left_in_wal_mode(tmp_path) -> None:
    """A mode=ro connection cannot open a WAL database it has no write access to."""
    path = tmp_path / "curbcheck.sqlite"
    writable = db.connect(path)
    db.create_schema(writable)

    mode = writable.execute("PRAGMA journal_mode").fetchone()[0]

    assert mode == "delete"


def test_placeholders_builds_an_in_clause_body() -> None:
    assert db.placeholders(1) == "?"
    assert db.placeholders(3) == "?,?,?"
    with pytest.raises(ValueError, match="at least one"):
        db.placeholders(0)


def test_geojson_bbox_covers_every_coordinate() -> None:
    geometry = {
        "type": "LineString",
        "coordinates": [[-73.96, 40.78], [-73.95, 40.7805], [-73.97, 40.779]],
    }

    assert db.geojson_bbox(geometry) == (-73.97, 40.779, -73.95, 40.7805)


def test_swap_in_moves_the_new_file_into_place_and_keeps_the_previous(tmp_path) -> None:
    live = tmp_path / "curbcheck.sqlite"
    new = tmp_path / "curbcheck.sqlite.new"
    live.write_text("old")
    new.write_text("new")

    previous = db.swap_in(new, live)

    assert live.read_text() == "new"
    assert previous.read_text() == "old"
    assert not new.exists()


def test_swap_in_works_when_there_is_no_live_database_yet(tmp_path) -> None:
    live = tmp_path / "curbcheck.sqlite"
    new = tmp_path / "curbcheck.sqlite.new"
    new.write_text("new")

    previous = db.swap_in(new, live)

    assert live.read_text() == "new"
    assert not previous.exists()


def test_swap_in_refuses_to_run_without_a_new_database(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        db.swap_in(tmp_path / "missing.sqlite", tmp_path / "curbcheck.sqlite")


def _insert_segment(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO regulation_segment (reg_seg_id, side, geom, min_lon, min_lat, max_lon,"
        " max_lat) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "seg-1",
            "E",
            '{"type":"LineString","coordinates":[[-73.96,40.78],[-73.96,40.781]]}',
            -73.96,
            40.78,
            -73.96,
            40.781,
        ),
    )
