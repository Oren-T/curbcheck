"""The pack compiler over a hand-built database: the encoding, row order, and what it refuses.

Everything the browser reads comes out of `curbcheck.pack`, and the JS has no
schema to check itself against — so this file checks the encoding the loader
assumes: rowid order, dictionary codes that decode back to the stored strings,
coordinates and offsets that reproduce each geometry, foreign keys as row
indexes, and the flag bitmask in `model.Flags` declaration order.

The same fixture is compiled into `site/tests/fixtures/pack/`, which
`site/tests/loader.test.js` reads. Run this file as a script to rewrite it:

    python tests/test_pack.py
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from curbcheck import pack
from curbcheck.config import REPO_ROOT
from curbcheck.db import create_schema, geojson_bbox
from curbcheck.model import Flags

JS_FIXTURE_DIR = REPO_ROOT / "site" / "tests" / "fixtures" / "pack"

ORIGIN_LON = -73.9600

# Ids are inserted out of alphabetical order on purpose: a scan of the primary
# key index would put them back in sorted order, and the pack promises the
# order SQLite's rowid gives.
SEGMENT_IDS = ("seg-3", "seg-1", "seg-2")
SPAN_IDS = ("span-b", "span-a", "span-d", "span-c")
SIGN_IDS = ("sign-2", "sign-1", "sign-3")


def line(lat: float, *, points: int = 2) -> str:
    """A short east-west LineString at `lat`, with coordinates that are not round numbers."""
    return json.dumps(
        {
            "type": "LineString",
            "coordinates": [
                [ORIGIN_LON + 0.0004 * step, lat + 0.0000125 * step] for step in range(points)
            ],
        }
    )


def add_node(conn: sqlite3.Connection, node_id: str, lon: float, lat: float, names: list[str]):
    conn.execute(
        "INSERT INTO street_node (node_id, lon, lat, street_names) VALUES (?, ?, ?, ?)",
        (node_id, lon, lat, json.dumps(names)),
    )


def add_segment(
    conn: sqlite3.Connection,
    segment_id: str,
    *,
    lat: float,
    from_node: str | None = None,
    to_node: str | None = None,
    width_ft: float | None = 34.0,
    addresses: tuple[str | None, str | None, str | None, str | None] = ("1500", "1598", None, None),
):
    geom = line(lat, points=3)
    conn.execute(
        "INSERT INTO street_segment (segment_id, street_name, street_norm, from_node, to_node,"
        " width_ft, length_ft, geom, min_lon, min_lat, max_lon, max_lat, left_low_address,"
        " left_high_address, right_low_address, right_high_address)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            segment_id,
            "3 AVENUE",
            "3 AV",
            from_node,
            to_node,
            width_ft,
            264.5,
            geom,
            *geojson_bbox(geom),
            *addresses,
        ),
    )


def add_sign(
    conn: sqlite3.Connection,
    sign_id: str,
    *,
    segment_id: str | None,
    description: str,
    order_number: str | None = "OR-100",
    sign_code: str | None = "NP",
    snap_notes: str | None = "single segment",
):
    conn.execute(
        "INSERT INTO sign (sign_id, order_number, on_street, from_street, to_street,"
        " side_of_street, distance_from_intersection, arrow_direction, facing_direction,"
        " sign_code, sign_description, sign_x_coord, sign_y_coord, derived_lon, derived_lat,"
        " segment_id, snap_confidence, snap_notes, is_regulation, panel_class)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            sign_id,
            order_number,
            "3 AVENUE",
            "EAST 85 STREET",
            "EAST 86 STREET",
            "E",
            42.5,
            "F",
            "N",
            sign_code,
            description,
            990_123.5,
            220_456.25,
            ORIGIN_LON,
            40.78,
            segment_id,
            0.95,
            snap_notes,
            1,
            "regulation",
        ),
    )


def add_span(
    conn: sqlite3.Connection,
    reg_seg_id: str,
    *,
    segment_id: str | None,
    lat: float,
    derived_from: list[str],
    gap_kind: str | None = None,
    side: str = "E",
):
    geom = line(lat)
    conn.execute(
        "INSERT INTO regulation_segment (reg_seg_id, segment_id, side, start_ft, end_ft, geom,"
        " min_lon, min_lat, max_lon, max_lat, length_ft, capacity_cars, capacity_approximate,"
        " confidence, derived_from, gap_kind)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            reg_seg_id,
            segment_id,
            side,
            0.0 if gap_kind is None else None,
            120.5 if gap_kind is None else None,
            geom,
            *geojson_bbox(geom),
            120.5,
            5,
            1,
            0.9,
            json.dumps(derived_from),
            gap_kind,
        ),
    )


def add_regulation(
    conn: sqlite3.Connection,
    reg_id: str,
    *,
    reg_seg_id: str,
    flags: Flags,
    time_from: str | None = "08:00",
    time_to: str | None = "09:30",
    description: str = "NO PARKING 8AM-9:30AM MON",
):
    conn.execute(
        "INSERT INTO regulation (reg_id, reg_seg_id, action, permitted, vehicle_class, exclusive,"
        " days_mask, time_from, time_to, metered, max_duration_min, flags, effective_from,"
        " effective_to, arrow, raw_sign_description, parse_method, parse_confidence, parse_notes)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            reg_id,
            reg_seg_id,
            "park",
            0,
            "all",
            0,
            0b0000001,
            time_from,
            time_to,
            0,
            None,
            flags.model_dump_json(),
            None,
            None,
            "forward",
            description,
            "grammar",
            0.97,
            "",
        ),
    )


def add_meter_rate(
    conn: sqlite3.Connection,
    blockface_id: str,
    *,
    segment_id: str | None,
    hour_rates: list[str],
    commercial_hour_rates: list[str] | None = None,
):
    conn.execute(
        "INSERT INTO meter_rate (blockface_id, segment_id, side, rate_label, hour_rates,"
        " commercial_hour_rates, max_session_min, source, confidence)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            blockface_id,
            segment_id,
            "E",
            "Zone 1",
            json.dumps(hour_rates),
            json.dumps(commercial_hour_rates or []),
            120,
            "parknyc",
            1.0,
        ),
    )


def build_fixture_database(conn: sqlite3.Connection) -> None:
    """A database with one row of every shape the encoding has to carry."""
    create_schema(conn)
    add_node(conn, "node-b", ORIGIN_LON, 40.7800, ["3 AVENUE", "EAST 85 STREET"])
    add_node(conn, "node-a", ORIGIN_LON, 40.7810, ["3 AVENUE", "EAST 86 STREET"])

    add_segment(conn, "seg-3", lat=40.7800, from_node="node-b", to_node="node-a")
    add_segment(conn, "seg-1", lat=40.7850, from_node="node-a", to_node=None)
    # No nodes, no width, no address ranges: every nullable column empty at once.
    add_segment(conn, "seg-2", lat=40.7900, width_ft=None, addresses=(None, None, None, None))

    add_sign(conn, "sign-2", segment_id="seg-1", description="NO PARKING 8AM-9:30AM MON")
    add_sign(conn, "sign-1", segment_id="seg-3", description="2 HOUR METERED PARKING 9AM-7PM")
    # Unsnapped, and every optional column null: `engine.signs` finds it by name.
    add_sign(
        conn,
        "sign-3",
        segment_id=None,
        description="NO STANDING ANYTIME",
        order_number=None,
        sign_code=None,
        snap_notes=None,
    )

    add_span(conn, "span-b", segment_id="seg-3", lat=40.7800, derived_from=["sign-1"])
    # "sign-404" names no sign row, so it is dropped and counted in meta.json.
    add_span(
        conn,
        "span-a",
        segment_id="seg-1",
        lat=40.7850,
        derived_from=["sign-2", "sign-404", "sign-1"],
    )
    add_span(conn, "span-d", segment_id="seg-1", lat=40.7850, derived_from=[], side="W")
    add_span(
        conn,
        "span-c",
        segment_id=None,
        lat=40.7900,
        derived_from=[],
        gap_kind="no_signs",
    )

    add_regulation(conn, "reg-2", reg_seg_id="span-a", flags=Flags(street_cleaning=True, meta=True))
    add_regulation(
        conn,
        "reg-1",
        reg_seg_id="span-b",
        flags=Flags(),
        time_from=None,
        time_to=None,
        description="2 HOUR METERED PARKING 9AM-7PM",
    )

    add_meter_rate(conn, "bf-2", segment_id="seg-3", hour_rates=["5.00", "8.25"])
    add_meter_rate(
        conn, "bf-1", segment_id=None, hour_rates=[], commercial_hour_rates=["7.00", "10.00"]
    )

    conn.executemany(
        "INSERT INTO asp_suspension (date, is_major_legal_holiday, meters_suspended, label)"
        " VALUES (?, ?, ?, ?)",
        [("2026-01-01", 1, 1, "New Year's Day"), ("2026-02-16", 0, 0, "Presidents' Day")],
    )
    _build_geocode_index(conn)
    conn.executemany(
        "INSERT INTO sync_meta (key, value, updated_at) VALUES (?, ?, ?)",
        [
            ("last_sync_at", "2026-09-15T17:37:46+00:00", None),
            ("calendar_missing", "0", None),
            ("calendar_source_path", "/workspace/data/raw/calendar/2026.ics", None),
            ("signs_loaded", "3", None),
        ],
    )
    conn.commit()


def _build_geocode_index(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT INTO street (street_norm, display, lon, lat) VALUES (?, ?, ?, ?)",
        [
            ("3 AV", "3 Avenue", ORIGIN_LON, 40.7805),
            ("E 86 ST", "East 86 Street", -73.9575, 40.779),
        ],
    )
    conn.executemany(
        "INSERT INTO street_variant (variant, street_norm) VALUES (?, ?)",
        [("3 av", "3 AV"), ("3 ave", "3 AV"), ("e 86 st", "E 86 ST")],
    )
    conn.executemany(
        "INSERT INTO address_point (street_norm, house_number, display, zipcode, lon, lat)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("3 AV", 1509, "1509 3 Avenue", "10028", -73.95448, 40.77814),
            ("3 AV", 1517, "1517 3 Avenue", None, -73.95445, 40.77850),
        ],
    )
    conn.executemany(
        "INSERT INTO intersection (a_norm, b_norm, display, lon, lat) VALUES (?, ?, ?, ?, ?)",
        [
            ("3 AV", "E 86 ST", "3 Avenue & East 86 Street", ORIGIN_LON, 40.7810),
            ("E 86 ST", "3 AV", "East 86 Street & 3 Avenue", ORIGIN_LON, 40.7810),
        ],
    )
    conn.execute(
        "INSERT INTO zip_centroid (zipcode, lon, lat, address_points) VALUES (?, ?, ?, ?)",
        ("10028", -73.9535, 40.7766, 2),
    )
    conn.executemany(
        "INSERT INTO place (place_id, display, lon, lat) VALUES (?, ?, ?, ?)",
        [(7, "Guggenheim Museum", -73.95893, 40.78285), (3, "Carl Schurz Park", -73.9435, 40.7745)],
    )
    conn.executemany(
        "INSERT INTO place_token (token, position, place_id, search_name) VALUES (?, ?, ?, ?)",
        [
            ("guggenheim", 0, 7, "guggenheim museum"),
            ("museum", 1, 7, "guggenheim museum"),
            ("carl", 0, 3, "carl schurz park"),
        ],
    )
    conn.executemany(
        "INSERT INTO street_token (token, position, street_norm, search_name) VALUES (?, ?, ?, ?)",
        [("3", 0, "3 AV", "3 av"), ("av", 1, "3 AV", "3 av"), ("86", 1, "E 86 ST", "e 86 st")],
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "fixture.sqlite"
    conn = sqlite3.connect(path)
    try:
        build_fixture_database(conn)
    finally:
        conn.close()
    return path


@pytest.fixture
def compiled(db_path: Path, tmp_path: Path) -> CompiledPack:
    return CompiledPack.build(db_path, tmp_path / "pack")


class CompiledPack:
    """A compiled pack read back the way `loader.js` reads it."""

    def __init__(self, report: pack.PackReport) -> None:
        self.report = report
        self.meta = report.meta
        self.payloads = {
            label: json.loads(gzip.decompress((report.directory / name).read_bytes()))
            for label, name in report.meta["files"].items()
        }

    @classmethod
    def build(cls, db_path: Path, out_dir: Path) -> CompiledPack:
        return cls(pack.compile_pack(db_path, out_dir))

    def table(self, label: str, name: str) -> dict[str, Any]:
        return self.payloads[label]["tables"][name]

    def column(self, label: str, name: str, column: str) -> Any:
        return self.table(label, name)["columns"][column]

    def strings(self, label: str, name: str, column: str) -> list[str | None]:
        """A dictionary column decoded, exactly as the loader decodes it."""
        encoded = self.column(label, name, column)
        dictionary = self.payloads[label]["dicts"][encoded["dict"]]
        return [None if code < 0 else dictionary[code] for code in encoded["codes"]]

    def line_string(self, label: str, name: str, row: int) -> list[list[float]]:
        column = self.column(label, name, "geom")
        start, end = column["offsets"][row], column["offsets"][row + 1]
        return [list(column["coords"][2 * i : 2 * i + 2]) for i in range(start, end)]


def test_the_three_files_are_named_by_the_hash_of_their_bytes(compiled: CompiledPack) -> None:
    for label, name in compiled.meta["files"].items():
        body = (compiled.report.directory / name).read_bytes()

        assert name.startswith(label + ".")
        assert name.endswith(".json.gz")
        assert name.split(".")[1] == hashlib.sha256(body).hexdigest()[:12]


def test_the_same_database_compiles_to_the_same_bytes(db_path: Path, tmp_path: Path) -> None:
    """mtime 0 and one gzip level, so an unchanged database is an unchanged deploy."""
    first = CompiledPack.build(db_path, tmp_path / "one")
    second = CompiledPack.build(db_path, tmp_path / "two")

    assert first.meta["files"] == second.meta["files"]


def test_a_pack_left_from_an_earlier_build_is_removed(db_path: Path, tmp_path: Path) -> None:
    out_dir = tmp_path / "pack"
    out_dir.mkdir()
    (out_dir / "rules.deadbeef0000.json.gz").write_bytes(b"stale")

    report = pack.compile_pack(db_path, out_dir)

    assert sorted(path.name for path in out_dir.glob("*.json.gz")) == sorted(
        report.meta["files"].values()
    )


def test_every_table_is_written_in_rowid_order(compiled: CompiledPack) -> None:
    """Insertion order, not primary-key order: the ids sort the other way."""
    assert compiled.column("rules", "spans", "regSegId") == list(SPAN_IDS)
    assert compiled.column("rules", "signs", "signId") == list(SIGN_IDS)
    assert compiled.column("streets", "segments", "segmentId") == list(SEGMENT_IDS)
    assert compiled.column("rules", "regulations", "regId") == ["reg-2", "reg-1"]
    assert compiled.column("rules", "meter_rates", "blockfaceId") == ["bf-2", "bf-1"]
    assert compiled.column("streets", "nodes", "nodeId") == ["node-b", "node-a"]


def test_a_without_rowid_table_is_written_in_primary_key_order(compiled: CompiledPack) -> None:
    """`street_variant` and the two token tables have no rowid; their key is the scan order."""
    assert compiled.column("geocode", "street_variants", "variant") == ["3 av", "3 ave", "e 86 st"]
    assert compiled.column("geocode", "street_tokens", "token") == ["3", "86", "av"]


def test_every_table_reports_its_own_row_count(compiled: CompiledPack) -> None:
    for label, payload in compiled.payloads.items():
        for name, table in payload["tables"].items():
            widths = {len(_column_length(column)) for column in table["columns"].values()}

            assert widths == {table["rows"]}, f"{label}.{name}"


def test_dictionary_columns_decode_back_to_the_stored_strings(compiled: CompiledPack) -> None:
    assert compiled.strings("rules", "signs", "onStreet") == ["3 AVENUE"] * 3
    assert compiled.strings("rules", "signs", "signCode") == ["NP", "NP", None]
    assert compiled.strings("rules", "signs", "snapNotes") == [
        "single segment",
        "single segment",
        None,
    ]
    assert compiled.strings("rules", "spans", "gapKind") == [None, None, None, "no_signs"]
    assert compiled.strings("rules", "regulations", "action") == ["park", "park"]


def test_the_three_blockface_name_columns_share_one_dictionary(compiled: CompiledPack) -> None:
    """A street spelling is stored once however many columns name it."""
    names = compiled.payloads["rules"]["dicts"]["street_names"]

    assert names == ["3 AVENUE", "EAST 85 STREET", "EAST 86 STREET"]
    assert compiled.column("rules", "signs", "toStreet")["dict"] == "street_names"


def test_a_sign_description_and_a_rule_description_share_one_dictionary(
    compiled: CompiledPack,
) -> None:
    descriptions = compiled.payloads["rules"]["dicts"]["descriptions"]

    assert descriptions.count("NO PARKING 8AM-9:30AM MON") == 1
    assert compiled.strings("rules", "regulations", "rawSignDescription") == [
        "NO PARKING 8AM-9:30AM MON",
        "2 HOUR METERED PARKING 9AM-7PM",
    ]


def test_geometry_becomes_flat_coordinates_with_row_offsets(compiled: CompiledPack) -> None:
    expected = json.loads(line(40.7850))["coordinates"]

    assert compiled.line_string("rules", "spans", SPAN_IDS.index("span-a")) == expected
    assert compiled.column("rules", "spans", "geom")["offsets"] == [0, 2, 4, 6, 8]
    assert compiled.column("streets", "segments", "geom")["offsets"] == [0, 3, 6, 9]


def test_coordinates_survive_as_the_doubles_they_were(
    compiled: CompiledPack, db_path: Path
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        stored = [
            json.loads(row[0])["coordinates"]
            for row in conn.execute("SELECT geom FROM regulation_segment ORDER BY rowid")
        ]
    finally:
        conn.close()

    for row, coordinates in enumerate(stored):
        assert compiled.line_string("rules", "spans", row) == coordinates


def test_a_geometry_the_pack_cannot_carry_fails_the_build(db_path: Path, tmp_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE regulation_segment SET geom = ? WHERE reg_seg_id = ?",
            (json.dumps({"type": "Point", "coordinates": [ORIGIN_LON, 40.78]}), "span-a"),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(pack.PackError, match="span-a"):
        pack.compile_pack(db_path, tmp_path / "pack")


def test_foreign_keys_become_row_indexes_and_minus_one_for_null(compiled: CompiledPack) -> None:
    assert compiled.column("rules", "spans", "segment") == [0, 1, 1, -1]
    assert compiled.column("rules", "spans", "segmentId") == ["seg-3", "seg-1", "seg-1", None]
    assert compiled.column("rules", "signs", "segment") == [1, 0, -1]
    assert compiled.column("rules", "regulations", "span") == [1, 0]
    assert compiled.column("rules", "meter_rates", "segment") == [0, -1]
    assert compiled.column("streets", "segments", "fromNode") == [0, 1, -1]
    assert compiled.column("streets", "segments", "toNode") == [1, -1, -1]


def test_derived_from_becomes_sign_rows_and_drops_the_ids_that_name_none(
    compiled: CompiledPack,
) -> None:
    column = compiled.column("rules", "spans", "derivedFrom")

    # span-a named sign-2 (row 0), the missing sign-404, then sign-1 (row 1).
    assert column["offsets"] == [0, 1, 3, 3, 3]
    assert column["lists"] == [1, 0, 1]
    assert compiled.meta["dropped_sign_refs"] == 1


def test_flags_are_a_bitmask_over_model_flags_in_declaration_order(compiled: CompiledPack) -> None:
    fields = list(Flags.model_fields)
    expected = (1 << fields.index("street_cleaning")) | (1 << fields.index("meta"))

    assert compiled.column("rules", "regulations", "flags") == [expected, 0]


def test_booleans_and_nulls_are_carried_as_they_are(compiled: CompiledPack) -> None:
    assert compiled.column("rules", "regulations", "permitted") == [0, 0]
    assert compiled.column("rules", "signs", "isRegulation") == [1, 1, 1]
    assert compiled.column("rules", "regulations", "timeFrom") == ["08:00", None]
    assert compiled.column("streets", "segments", "widthFt") == [34.0, 34.0, None]
    assert compiled.column("rules", "calendar", "isMajorLegalHoliday") == [1, 0]


def test_hour_rates_stay_the_string_they_were_stored_as(compiled: CompiledPack) -> None:
    """Money never round-trips through a float, here or in the browser."""
    assert compiled.column("rules", "meter_rates", "hourRates") == ['["5.00", "8.25"]', "[]"]
    assert compiled.column("rules", "meter_rates", "commercialHourRates") == [
        "[]",
        '["7.00", "10.00"]',
    ]


def test_a_rate_with_three_decimals_is_refused(db_path: Path, tmp_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE meter_rate SET hour_rates = ? WHERE blockface_id = ?",
            (json.dumps(["5.005"]), "bf-2"),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(pack.PackError, match="bf-2"):
        pack.compile_pack(db_path, tmp_path / "pack")


def test_the_geocode_tables_carry_every_column_the_suggester_reads(compiled: CompiledPack) -> None:
    assert compiled.strings("geocode", "address_points", "streetNorm") == ["3 AV", "3 AV"]
    assert compiled.column("geocode", "address_points", "houseNumber") == [1509, 1517]
    assert compiled.column("geocode", "address_points", "zipcode") == ["10028", None]
    assert compiled.column("geocode", "places", "placeId") == [3, 7]
    assert compiled.column("geocode", "place_tokens", "searchName") == [
        "carl schurz park",
        "guggenheim museum",
        "guggenheim museum",
    ]
    assert compiled.column("geocode", "zip_centroids", "addressPoints") == [2]
    assert compiled.column("geocode", "intersections", "aNorm") == ["3 AV", "E 86 ST"]


def test_meta_json_says_what_the_pack_holds(compiled: CompiledPack) -> None:
    meta = compiled.meta

    assert meta["format"] == pack.FORMAT_VERSION
    assert datetime.fromisoformat(meta["built_at"]).tzinfo is not None
    assert meta["counts"] == {"spans": 4, "regulations": 2, "signs": 3, "segments": 3}
    assert meta["calendar_missing"] is False
    assert meta["coverage"]["area"] == "Manhattan"
    assert meta["coverage"]["bbox"] == pytest.approx([-73.9600, 40.7800, -73.9592, 40.790025])
    assert sorted(meta["files"]) == ["geocode", "rules", "streets"]


def test_sync_meta_drops_the_keys_whose_value_is_a_path(compiled: CompiledPack) -> None:
    """docs/SECURITY.md: nothing the browser receives names a directory on the build machine."""
    sync = compiled.meta["sync"]

    assert "calendar_source_path" not in sync
    assert sync == {
        "last_sync_at": "2026-09-15T17:37:46+00:00",
        "calendar_missing": "0",
        "signs_loaded": "3",
    }


def test_an_empty_calendar_is_reported_the_way_the_server_reports_it(
    db_path: Path, tmp_path: Path
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DELETE FROM asp_suspension")
        conn.commit()
    finally:
        conn.close()

    report = pack.compile_pack(db_path, tmp_path / "pack")

    assert report.meta["calendar_missing"] is True
    assert report.meta["counts"]["spans"] == 4


def test_the_committed_javascript_fixture_is_what_this_compiler_writes(
    db_path: Path, tmp_path: Path
) -> None:
    """`site/tests/loader.test.js` reads a pack built from this fixture; keep them in step."""
    fresh = CompiledPack.build(db_path, tmp_path / "pack")
    committed = _read_committed_fixture()

    assert committed is not None, f"no fixture pack in {JS_FIXTURE_DIR}; run this file as a script"
    assert committed[0]["files"] == fresh.meta["files"]
    assert committed[1] == fresh.payloads


def _read_committed_fixture() -> tuple[dict[str, Any], dict[str, Any]] | None:
    meta_path = JS_FIXTURE_DIR / pack.META_NAME
    if not meta_path.is_file():
        return None
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    payloads = {
        label: json.loads(gzip.decompress((JS_FIXTURE_DIR / name).read_bytes()))
        for label, name in meta["files"].items()
    }
    return meta, payloads


def _column_length(column: Any) -> list[Any]:
    """The per-row part of any of the four column encodings."""
    if isinstance(column, list):
        return column
    if "codes" in column:
        return column["codes"]
    return column["offsets"][1:]


def _write_javascript_fixture() -> None:
    """Rebuild `site/tests/fixtures/pack/` from the fixture database."""
    import tempfile

    JS_FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for stale in JS_FIXTURE_DIR.glob("*.json*"):
        stale.unlink()
    with tempfile.TemporaryDirectory() as workspace:
        db = Path(workspace) / "fixture.sqlite"
        conn = sqlite3.connect(db)
        try:
            build_fixture_database(conn)
        finally:
            conn.close()
        report = pack.compile_pack(db, JS_FIXTURE_DIR)
    for line_out in pack.report_lines(report):
        print(line_out)


if __name__ == "__main__":
    _write_javascript_fixture()
    sys.exit(0)
