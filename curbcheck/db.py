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
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
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
        snap_notes                 TEXT,
        -- docs/DECISIONS.md D10: panels that carry no regulation stay in this
        -- table for audit but never reach the parser or a verdict.
        is_regulation              INTEGER NOT NULL DEFAULT 1,
        panel_class                TEXT NOT NULL DEFAULT 'regulation'
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
        -- Always 1 in v1: hydrant, driveway and crosswalk setbacks are not in
        -- the data, so a car count is an upper bound (SPEC §8.5).
        capacity_approximate INTEGER NOT NULL DEFAULT 1,
        confidence    REAL NOT NULL DEFAULT 0.0,
        derived_from  TEXT NOT NULL DEFAULT '[]',
        -- NULL on a real span. A placeholder covering a side with no rules at
        -- all says why it is empty, so SPEC §11's grey "no sign data" state can
        -- tell a data gap ('no_signs') from a matching gap ('unmatched_signs').
        gap_kind      TEXT
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
        parse_confidence     REAL NOT NULL,
        -- Why this rule reads the way it does: the parser's skipped-token note,
        -- or the sibling link a meta sign added (SPEC §8.5, docs/DECISIONS.md D17).
        parse_notes          TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS meter_rate (
        blockface_id    TEXT PRIMARY KEY,
        segment_id      TEXT REFERENCES street_segment(segment_id),
        side            TEXT,
        rate_label      TEXT,
        hour_rates      TEXT NOT NULL DEFAULT '[]',
        -- ParkNYC prices commercial plates separately; a passenger query never
        -- reads this column, but the detail panel shows what the meter charges.
        commercial_hour_rates TEXT NOT NULL DEFAULT '[]',
        max_session_min INTEGER,
        -- 'parknyc' is the blockface join, 'rate_zone' the point-in-polygon
        -- fallback for a metered segment ParkNYC does not list (SPEC §13.1c).
        source          TEXT NOT NULL DEFAULT 'parknyc',
        confidence      REAL NOT NULL DEFAULT 0.0,
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
    # The seven tables below are the address suggester's vocabulary index
    # (docs/ux/AUTOCOMPLETE_RESEARCH.md §2). They are written by
    # `etl.addresses` and read only by `curbcheck.geocode`; nothing in the
    # regulation pipeline joins to them.
    """
    CREATE TABLE IF NOT EXISTS street (
        street_norm TEXT PRIMARY KEY,
        display     TEXT NOT NULL,
        lon         REAL NOT NULL,
        lat         REAL NOT NULL
    )
    """,
    # One row per spelling a person might type. `variant` leads the primary key
    # so resolving a half-typed street is a range scan over it, and WITHOUT
    # ROWID keeps that scan inside the key itself.
    """
    CREATE TABLE IF NOT EXISTS street_variant (
        variant     TEXT NOT NULL,
        street_norm TEXT NOT NULL,
        PRIMARY KEY (variant, street_norm)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS address_point (
        street_norm  TEXT NOT NULL,
        house_number INTEGER NOT NULL,
        display      TEXT NOT NULL,
        zipcode      TEXT,
        lon          REAL NOT NULL,
        lat          REAL NOT NULL
    )
    """,
    # Both orders of a corner are stored, so a lookup never has to try the pair
    # twice and the (a_norm, b_norm) index can answer from the key alone.
    """
    CREATE TABLE IF NOT EXISTS intersection (
        a_norm  TEXT NOT NULL,
        b_norm  TEXT NOT NULL,
        display TEXT NOT NULL,
        lon     REAL NOT NULL,
        lat     REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS zip_centroid (
        zipcode        TEXT PRIMARY KEY,
        lon            REAL NOT NULL,
        lat            REAL NOT NULL,
        address_points INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS place (
        place_id    INTEGER PRIMARY KEY,
        display     TEXT NOT NULL,
        lon         REAL NOT NULL,
        lat         REAL NOT NULL
    )
    """,
    # One row per (word, where it sits, which spelling it came from) for every
    # place and every street. `token` leads the key so a half-typed word is a
    # range scan and `LIMIT` can stop it early, `position` comes next so the
    # rows that come back first are the ones where the word starts the name,
    # and `search_name` rides along so scoring a candidate never leaves the
    # index: reading the 200 matching `place` rows instead cost 55 ms warm and
    # 2.5 s cold on the 9p data mount, against 1.5 ms for the scan
    # (docs/ux/AUTOCOMPLETE_RESEARCH.md §6.3).
    """
    CREATE TABLE IF NOT EXISTS place_token (
        token       TEXT NOT NULL,
        position    INTEGER NOT NULL,
        place_id    INTEGER NOT NULL,
        search_name TEXT NOT NULL,
        PRIMARY KEY (token, position, place_id, search_name)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS street_token (
        token       TEXT NOT NULL,
        position    INTEGER NOT NULL,
        street_norm TEXT NOT NULL,
        search_name TEXT NOT NULL,
        PRIMARY KEY (token, position, street_norm, search_name)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS sync_meta (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,
        updated_at TEXT
    )
    """,
    # Looking a street up by name is what every geocode does, and without this
    # it was a scan of 11,102 rows carrying their geometry: 516 ms -> 0.4 ms
    # for "1519 3rd ave" on the 9p mount.
    "CREATE INDEX IF NOT EXISTS ix_street_segment_norm ON street_segment (street_norm)",
    # `engine.coverage` asks "is this point near any centerline?" once per
    # geocode candidate, and `geocode.reverse` asks what a pin is nearest to.
    # All four bbox bounds sit in one index, so a candidate is rejected on
    # latitude without the row ever being read: with a bound on one axis only,
    # SQLite fetched every row in the longitude band to test the other, which on
    # the 9p mount was 48 ms a check and made a cold `q=1519 3` 249 ms against
    # 15 ms for a street name. 14 ms a check with this, 0.5 MB. It also answers
    # `coverage_extent` on its own. `geom` is deliberately not in it: adding it
    # saves the ~48 row reads that survive the box but turns every extent scan
    # and every band scan into a walk over 2.4 MB, which measured worse on both
    # (cold 229 ms, warm p50 19 ms against 106 ms and 7 ms).
    "CREATE INDEX IF NOT EXISTS ix_street_segment_bbox ON street_segment"
    " (min_lon, max_lon, min_lat, max_lat)",
    # The same shape for the radius query, which filters on longitude first
    # (Manhattan is tall and narrow, so a longitude band cuts more candidates
    # than a latitude band). Before it, a 30-minute radius scanned one axis and
    # fetched every row in that band to test the other three.
    "CREATE INDEX IF NOT EXISTS ix_reg_seg_bbox ON regulation_segment"
    " (min_lon, max_lon, min_lat, max_lat)",
    "CREATE INDEX IF NOT EXISTS ix_reg_seg_segment ON regulation_segment (segment_id, side)",
    "CREATE INDEX IF NOT EXISTS ix_regulation_seg ON regulation (reg_seg_id)",
    "CREATE INDEX IF NOT EXISTS ix_sign_segment ON sign (segment_id)",
    # A sign that never snapped has no geometry, so `engine.signs` finds it by
    # blockface name instead. This partial index holds only those 2,700 rows
    # and every column that query reads, so the lookup is a range scan inside
    # the index with no table fetch at all: 615 ms -> 2.5 ms per detail panel
    # on the 9p mount, for 0.71 MB.
    "CREATE INDEX IF NOT EXISTS ix_sign_unmatched ON sign"
    " (side_of_street, on_street, from_street, to_street, sign_id, order_number, sign_code,"
    " sign_description, distance_from_intersection, arrow_direction, snap_confidence,"
    " snap_notes, is_regulation, panel_class) WHERE segment_id IS NULL",
    "CREATE INDEX IF NOT EXISTS ix_meter_rate_segment ON meter_rate (segment_id, side)",
    # Covering indexes: every suggestion query is answered out of the index
    # without touching the table, which is what holds a keystroke under a
    # millisecond (docs/ux/AUTOCOMPLETE_RESEARCH.md §2.4).
    "CREATE INDEX IF NOT EXISTS ix_address_point_street ON address_point"
    " (street_norm, house_number, lon, lat, display, zipcode)",
    # The bounding-box prefilter a dropped pin's reverse lookup runs.
    "CREATE INDEX IF NOT EXISTS ix_address_point_lon ON address_point (lon, lat, display)",
    "CREATE INDEX IF NOT EXISTS ix_intersection_pair ON intersection"
    " (a_norm, b_norm, lon, lat, display)",
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
    "parse_notes",
)

INSERT_REGULATION_SQL = (
    "INSERT INTO regulation (reg_id, reg_seg_id, action, permitted, vehicle_class, exclusive,"
    " days_mask, time_from, time_to, metered, max_duration_min, flags, effective_from,"
    " effective_to, arrow, raw_sign_description, parse_method, parse_confidence, parse_notes)"
    " VALUES (:reg_id, :reg_seg_id, :action, :permitted, :vehicle_class, :exclusive,"
    " :days_mask, :time_from, :time_to, :metered, :max_duration_min, :flags, :effective_from,"
    " :effective_to, :arrow, :raw_sign_description, :parse_method, :parse_confidence,"
    " :parse_notes)"
)


# SQLite's default page cache is 2 MB, and one `/api/geocode` reads more than
# that: the suggester walks the vocabulary index and then the coverage check
# reads centerline geometry at up to eight scattered points. At 2 MB those
# evict each other inside a single request, so the same query cost 117 ms every
# time on this container's 9p data mount; at 4 MB it costs 6.4 ms.
#
# A search is a much larger working set, and it is random: a 30-minute walk
# radius reads ~4,400 spans, their 7,600 rules and ~10,000 sign rows scattered
# over the three biggest tables, ~30 MB of pages. Below that the cache thrashes
# and every repeat search re-reads the file: on the 9p mount the same 30-minute
# search takes 3.1 s at 8 MB, 3.0 s at 24 MB and 0.88 s at 32 MB. 48 MB is that
# knee plus half again, and it is per connection — one worker with a handful of
# request threads (`api.routes.open_database`), so the ceiling is a few hundred
# MB and only for threads that have actually run a wide search.
#
# Written out rather than formatted from a number, because a PRAGMA is a
# statement and no statement in this package is built by formatting
# (`tests/test_engine_sql_safety.py`).
READONLY_CACHE_PRAGMA = "PRAGMA cache_size = -48000"


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
    if readonly:
        # The negative value means KiB rather than pages, so the size does not
        # change meaning with `page_size`.
        conn.execute(READONLY_CACHE_PRAGMA)
    if not readonly and str(path) != ":memory:":
        # Deliberately not WAL: a WAL database needs write access to its -shm
        # file, which a mode=ro connection on a read-only file cannot get
        # (sqlite.org/wal.html "Read-Only Databases"). The ETL writes once and
        # the server only reads, so the rollback journal costs us nothing.
        conn.execute("PRAGMA journal_mode = DELETE")
    return conn


@contextmanager
def read_snapshot(conn: sqlite3.Connection) -> Iterator[None]:
    """Run a burst of SELECTs inside one read transaction.

    Outside a transaction SQLite opens and closes one per statement, and each
    of those re-reads page 1 to check the file's change counter. On the 9p
    mount `data/` sits on that costs ~2.5 ms a statement, which took a ten-query
    suggestion from 4.5 ms to 35 ms. It is also the correctness-shaped thing to
    do: `curbcheck sync` swaps a new database file in under a running server,
    and one transaction means the whole answer is read from one snapshot.

    Nested calls are a no-op, because SQLite has no nested transactions.
    """
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN")
    try:
        yield
    finally:
        conn.execute("END")


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
    parse_notes: str = "",
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
        "parse_notes": parse_notes,
    }


def json_string_list(value: Any) -> list[str]:
    """Read a JSON list column as strings, treating anything unreadable as empty.

    `derived_from`, `street_names` and `hour_rates` are all JSON lists, and
    everything in `data/` is untrusted at read time (CLAUDE.md). A cell that is
    not a JSON list means "no ids", "no names" or "no known rate", which every
    caller already handles; raising would turn a half-built snapshot into a 500
    on every search.
    """
    if value is None:
        return []
    try:
        parsed = json.loads(str(value))
    except ValueError:
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


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
