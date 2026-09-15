"""SQLite storage: schema, connections, and the one serialization of a `Regulation`.

The ETL writes a fresh database file and swaps it over the live one; the server
opens that file read-only. Both sides share `regulation_from_row` /
`regulation_to_params` so a rule means the same thing on the way in and out.

Geometry is GeoJSON text in EPSG:4326 with a bounding box alongside, per
docs/DECISIONS.md D4: radius queries filter on the bbox in SQL and refine with
shapely in Python.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from curbcheck.model import (
    ALL_DAYS,
    Action,
    Arrow,
    Flags,
    ParseMethod,
    Regulation,
    VehicleClass,
    Weekday,
)

# Monday is bit 0 through Sunday is bit 6, matching model.Weekday.
_DAY_BITS = tuple(1 << day for day in ALL_DAYS)

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS street_node (
        node_id      TEXT PRIMARY KEY,
        lon          REAL NOT NULL,
        lat          REAL NOT NULL,
        street_names TEXT NOT NULL DEFAULT '[]'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS street_segment (
        segment_id   TEXT PRIMARY KEY,
        street_name  TEXT NOT NULL,
        street_norm  TEXT NOT NULL,
        from_node    TEXT REFERENCES street_node(node_id),
        to_node      TEXT REFERENCES street_node(node_id),
        width_ft     REAL,
        length_ft    REAL,
        geom         TEXT NOT NULL,
        min_lon      REAL NOT NULL,
        min_lat      REAL NOT NULL,
        max_lon      REAL NOT NULL,
        max_lat      REAL NOT NULL,
        left_low_address   TEXT,
        left_high_address  TEXT,
        right_low_address  TEXT,
        right_high_address TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sign (
        sign_id                    TEXT PRIMARY KEY,
        order_number               TEXT,
        on_street                  TEXT,
        from_street                TEXT,
        to_street                  TEXT,
        side_of_street             TEXT,
        distance_from_intersection REAL,
        arrow_direction            TEXT,
        facing_direction           TEXT,
        sign_code                  TEXT,
        sign_description           TEXT NOT NULL,
        sign_x_coord               REAL,
        sign_y_coord               REAL,
        derived_lon                REAL,
        derived_lat                REAL,
        segment_id                 TEXT REFERENCES street_segment(segment_id),
        snap_confidence            REAL,
        snap_notes                 TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS regulation_segment (
        reg_seg_id    TEXT PRIMARY KEY,
        segment_id    TEXT REFERENCES street_segment(segment_id),
        side          TEXT NOT NULL,
        start_ft      REAL,
        end_ft        REAL,
        geom          TEXT NOT NULL,
        min_lon       REAL NOT NULL,
        min_lat       REAL NOT NULL,
        max_lon       REAL NOT NULL,
        max_lat       REAL NOT NULL,
        length_ft     REAL,
        capacity_cars INTEGER,
        confidence    REAL NOT NULL DEFAULT 0.0,
        derived_from  TEXT NOT NULL DEFAULT '[]'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS regulation (
        reg_id               TEXT PRIMARY KEY,
        reg_seg_id           TEXT NOT NULL REFERENCES regulation_segment(reg_seg_id),
        action               TEXT NOT NULL,
        permitted            INTEGER NOT NULL,
        vehicle_class        TEXT NOT NULL,
        exclusive            INTEGER NOT NULL DEFAULT 0,
        days_mask            INTEGER NOT NULL,
        time_from            TEXT,
        time_to              TEXT,
        metered              INTEGER NOT NULL DEFAULT 0,
        max_duration_min     INTEGER,
        flags                TEXT NOT NULL DEFAULT '{}',
        effective_from       TEXT,
        effective_to         TEXT,
        arrow                TEXT NOT NULL DEFAULT 'none',
        raw_sign_description TEXT NOT NULL DEFAULT '',
        parse_method         TEXT NOT NULL,
        parse_confidence     REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS meter_rate (
        blockface_id    TEXT PRIMARY KEY,
        segment_id      TEXT REFERENCES street_segment(segment_id),
        side            TEXT,
        rate_label      TEXT,
        hour_rates      TEXT NOT NULL DEFAULT '[]',
        max_session_min INTEGER,
        geom            TEXT,
        min_lon         REAL,
        min_lat         REAL,
        max_lon         REAL,
        max_lat         REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS asp_suspension (
        date                   TEXT PRIMARY KEY,
        is_major_legal_holiday INTEGER NOT NULL DEFAULT 0,
        meters_suspended       INTEGER NOT NULL DEFAULT 0,
        label                  TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sync_meta (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,
        updated_at TEXT
    )
    """,
    # The radius query filters on longitude first (Manhattan is tall and narrow,
    # so a longitude band cuts more candidates than a latitude band).
    "CREATE INDEX IF NOT EXISTS ix_reg_seg_lon ON regulation_segment (min_lon, max_lon)",
    "CREATE INDEX IF NOT EXISTS ix_reg_seg_lat ON regulation_segment (min_lat, max_lat)",
    "CREATE INDEX IF NOT EXISTS ix_reg_seg_segment ON regulation_segment (segment_id, side)",
    "CREATE INDEX IF NOT EXISTS ix_regulation_seg ON regulation (reg_seg_id)",
    "CREATE INDEX IF NOT EXISTS ix_sign_segment ON sign (segment_id)",
    "CREATE INDEX IF NOT EXISTS ix_meter_rate_segment ON meter_rate (segment_id, side)",
)

REGULATION_COLUMNS: tuple[str, ...] = (
    "reg_id",
    "reg_seg_id",
    "action",
    "permitted",
    "vehicle_class",
    "exclusive",
    "days_mask",
    "time_from",
    "time_to",
    "metered",
    "max_duration_min",
    "flags",
    "effective_from",
    "effective_to",
    "arrow",
    "raw_sign_description",
    "parse_method",
    "parse_confidence",
)

INSERT_REGULATION_SQL = (
    "INSERT INTO regulation (reg_id, reg_seg_id, action, permitted, vehicle_class, exclusive,"
    " days_mask, time_from, time_to, metered, max_duration_min, flags, effective_from,"
    " effective_to, arrow, raw_sign_description, parse_method, parse_confidence)"
    " VALUES (:reg_id, :reg_seg_id, :action, :permitted, :vehicle_class, :exclusive,"
    " :days_mask, :time_from, :time_to, :metered, :max_duration_min, :flags, :effective_from,"
    " :effective_to, :arrow, :raw_sign_description, :parse_method, :parse_confidence)"
)


def connect(path: Path | str, *, readonly: bool = False) -> sqlite3.Connection:
    """Open the database with row access by name and foreign keys enforced.

    `readonly=True` opens a `mode=ro` URI, which the API server uses so a bug in
    a request handler cannot write. The special path ":memory:" is accepted for
    tests and ignores `readonly`.
    """
    if str(path) == ":memory:":
        conn = sqlite3.connect(":memory:")
    elif readonly:
        conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(Path(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if not readonly and str(path) != ":memory:":
        # Deliberately not WAL: a WAL database needs write access to its -shm
        # file, which a mode=ro connection on a read-only file cannot get
        # (sqlite.org/wal.html "Read-Only Databases"). The ETL writes once and
        # the server only reads, so the rollback journal costs us nothing.
        conn.execute("PRAGMA journal_mode = DELETE")
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    """Create every table and index. Idempotent."""
    for statement in SCHEMA_STATEMENTS:
        conn.execute(statement)
    conn.commit()


def placeholders(count: int) -> str:
    """Build the `?,?,?` body of an IN clause.

    `count` always comes from `len()` of a list the code built, never from
    downloaded data, so this is the one sanctioned way to size a query.
    """
    if count < 1:
        raise ValueError("an IN clause needs at least one placeholder")
    return ",".join("?" * count)


def days_to_mask(days: Iterable[Weekday]) -> int:
    mask = 0
    for day in days:
        mask |= _DAY_BITS[day]
    return mask


def days_from_mask(mask: int) -> list[Weekday]:
    return [day for day in ALL_DAYS if mask & _DAY_BITS[day]]


def regulation_from_row(row: Mapping[str, Any] | sqlite3.Row) -> Regulation:
    """Rebuild a `Regulation` from a `regulation` row.

    Rows are untrusted at read time (CLAUDE.md), so every field goes back
    through the Pydantic model rather than being trusted as stored.
    """
    return Regulation(
        action=Action(str(row["action"])),
        permitted=bool(row["permitted"]),
        vehicle_class=VehicleClass(str(row["vehicle_class"])),
        exclusive=bool(row["exclusive"]),
        days=days_from_mask(int(row["days_mask"])),
        time_from=_optional_text(row["time_from"]),
        time_to=_optional_text(row["time_to"]),
        metered=bool(row["metered"]),
        max_duration_min=_optional_int(row["max_duration_min"]),
        flags=Flags.model_validate_json(str(row["flags"])),
        effective_from=_optional_text(row["effective_from"]),
        effective_to=_optional_text(row["effective_to"]),
        arrow=Arrow(str(row["arrow"])),
    )


def regulation_to_params(
    reg: Regulation,
    *,
    reg_id: str,
    reg_seg_id: str,
    raw_sign_description: str,
    parse_method: ParseMethod,
    parse_confidence: float,
) -> dict[str, Any]:
    """Named parameters for `INSERT_REGULATION_SQL`."""
    return {
        "reg_id": reg_id,
        "reg_seg_id": reg_seg_id,
        "action": reg.action.value,
        "permitted": int(reg.permitted),
        "vehicle_class": reg.vehicle_class.value,
        "exclusive": int(reg.exclusive),
        "days_mask": days_to_mask(reg.days),
        "time_from": reg.time_from,
        "time_to": reg.time_to,
        "metered": int(reg.metered),
        "max_duration_min": reg.max_duration_min,
        "flags": reg.flags.model_dump_json(),
        "effective_from": reg.effective_from,
        "effective_to": reg.effective_to,
        "arrow": reg.arrow.value,
        "raw_sign_description": raw_sign_description,
        "parse_method": parse_method.value,
        "parse_confidence": parse_confidence,
    }


def geojson_bbox(geometry: Mapping[str, Any] | str) -> tuple[float, float, float, float]:
    """Bounding box (min_lon, min_lat, max_lon, max_lat) of a GeoJSON geometry."""
    parsed = json.loads(geometry) if isinstance(geometry, str) else geometry
    lons: list[float] = []
    lats: list[float] = []
    _collect_positions(parsed.get("coordinates"), lons, lats)
    if not lons:
        raise ValueError("geometry has no coordinates")
    return (min(lons), min(lats), max(lons), max(lats))


def swap_in(new_path: Path, live_path: Path) -> Path:
    """Move `new_path` into place as `live_path`, keeping the old file as `.prev`.

    Each rename is atomic, but the pair is not: a crash between them leaves the
    live path missing while `.prev` holds the previous database. Both paths must
    be on one filesystem, which is what makes each rename atomic at all.
    """
    previous = live_path.with_name(live_path.name + ".prev")
    if not new_path.exists():
        raise FileNotFoundError(f"no new database at {new_path}")
    if new_path.stat().st_dev != live_path.parent.stat().st_dev:
        raise ValueError("new and live database paths must be on the same filesystem")
    if live_path.exists():
        live_path.replace(previous)
    new_path.replace(live_path)
    return previous


def _collect_positions(node: Any, lons: list[float], lats: list[float]) -> None:
    if isinstance(node, (list, tuple)):
        if node and isinstance(node[0], (int, float)):
            lons.append(float(node[0]))
            lats.append(float(node[1]))
            return
        for child in node:
            _collect_positions(child, lons, lats)


def _optional_text(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)
