"""Run the ETL steps in order and write a fresh database, then swap it in.

Only this module and `db` touch SQLite; every step it calls is a pure function
over dataclasses. The geometry half (stage, streets, snap, segments) is
implemented; the parse, meter and calendar steps are stubs with the shape their
callers will need.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from shapely.geometry import mapping

from curbcheck import db
from curbcheck.config import DB_PATH, RAW_DIR
from curbcheck.etl import fetch
from curbcheck.etl.segments import RegulationSegment, SegmentReport, resolve_segments
from curbcheck.etl.snap import SnapReport, SnapResult, snap_signs
from curbcheck.etl.stage import StageReport, stage_centerline, stage_signs
from curbcheck.etl.streets import StreetGraph, StreetSegment, build_graph

LOGGER = logging.getLogger(__name__)

_INSERT_NODE = (
    "INSERT OR REPLACE INTO street_node (node_id, lon, lat, street_names) VALUES (?, ?, ?, ?)"
)
_INSERT_SEGMENT = (
    "INSERT OR REPLACE INTO street_segment (segment_id, street_name, street_norm, from_node,"
    " to_node, width_ft, length_ft, geom, min_lon, min_lat, max_lon, max_lat,"
    " left_low_address, left_high_address, right_low_address, right_high_address)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_INSERT_SIGN = (
    "INSERT OR REPLACE INTO sign (sign_id, order_number, on_street, from_street, to_street,"
    " side_of_street, distance_from_intersection, arrow_direction, facing_direction, sign_code,"
    " sign_description, sign_x_coord, sign_y_coord, derived_lon, derived_lat, segment_id,"
    " snap_confidence, snap_notes, is_regulation, panel_class)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_INSERT_REG_SEGMENT = (
    "INSERT OR REPLACE INTO regulation_segment (reg_seg_id, segment_id, side, start_ft, end_ft,"
    " geom, min_lon, min_lat, max_lon, max_lat, length_ft, capacity_cars, capacity_approximate,"
    " confidence, derived_from) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_INSERT_SYNC_META = "INSERT OR REPLACE INTO sync_meta (key, value, updated_at) VALUES (?, ?, ?)"


@dataclass(frozen=True)
class GeometryStats:
    """Everything the geometry half of the ETL measured about this snapshot."""

    stage: StageReport
    snap: SnapReport
    segments: SegmentReport
    street_segments: int
    street_nodes: int
    elapsed_s: float


def run_geometry(raw_dir: Path, conn: sqlite3.Connection) -> GeometryStats:
    """stage -> streets -> snap -> segments, written to an open database.

    The caller owns the connection and the schema; this commits once at the end
    so a failure anywhere leaves the database untouched.
    """
    started = time.monotonic()
    graph = build_graph(stage_centerline(fetch.load_rows("centerline_manhattan", raw_dir)))
    signs, stage_report = stage_signs(fetch.load_rows("signs_manhattan", raw_dir))
    snaps, snap_report = snap_signs(signs, graph)
    reg_segments, segment_report = resolve_segments(snaps)

    _write_nodes(conn, graph)
    _write_segments(conn, graph.segments.values())
    _write_signs(conn, snaps)
    _write_regulation_segments(conn, reg_segments)
    stats = GeometryStats(
        stage=stage_report,
        snap=snap_report,
        segments=segment_report,
        street_segments=len(graph.segments),
        street_nodes=len(graph.nodes),
        elapsed_s=round(time.monotonic() - started, 2),
    )
    write_sync_meta(conn, _geometry_meta(stats))
    conn.commit()
    LOGGER.info(
        "build.geometry signs=%d snapped=%d (%.2f%%) reg_segments=%d elapsed=%.1fs",
        snap_report.signs,
        snap_report.matched,
        100 * snap_report.matched_share,
        len(reg_segments),
        stats.elapsed_s,
    )
    return stats


def run_parse(conn: sqlite3.Connection) -> None:
    """Hook: parse each distinct `sign_description` once and write `regulation` rows.

    Owned by `curbcheck.etl.parse`. It reads the descriptions already in the
    `sign` table, so it must run after `run_geometry`, and it fills `regulation`
    with one row per rule per `regulation_segment` via `db.INSERT_REGULATION_SQL`.
    """
    raise NotImplementedError("curbcheck.etl.parse is not wired in yet")


def run_meters(raw_dir: Path, conn: sqlite3.Connection) -> None:
    """Hook: join ParkNYC blockfaces to `street_segment` and fill `meter_rate`.

    Owned by `curbcheck.etl.meters`. Needs `run_geometry` first for the
    normalized street names and segment ids it joins on (docs/DATA.md §3.2).
    """
    raise NotImplementedError("curbcheck.etl.meters is not wired in yet")


def run_calendar(raw_dir: Path, conn: sqlite3.Connection) -> None:
    """Hook: load the DOT ASP suspension ICS into `asp_suspension`.

    Owned by `curbcheck.etl.calendar`. Independent of the geometry steps
    (docs/DECISIONS.md D11).
    """
    raise NotImplementedError("curbcheck.etl.calendar is not wired in yet")


def build_all(raw_dir: Path = RAW_DIR, out_path: Path = DB_PATH) -> GeometryStats:
    """Build a complete database beside `out_path` and rename it into place.

    The working file is a sibling so `db.swap_in` can rename atomically, which
    it can only do within one filesystem.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    working = out_path.with_name(out_path.name + ".new")
    working.unlink(missing_ok=True)
    conn = db.connect(working)
    try:
        db.create_schema(conn)
        stats = run_geometry(raw_dir, conn)
        write_sync_meta(conn, {"last_sync_at": datetime.now(UTC).isoformat(timespec="seconds")})
        conn.commit()
    finally:
        conn.close()
    db.swap_in(working, out_path)
    LOGGER.info("build.swapped path=%s", out_path)
    return stats


def write_sync_meta(conn: sqlite3.Connection, values: dict[str, object]) -> None:
    """Upsert key/value rows; non-string values are stored as JSON."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    conn.executemany(
        _INSERT_SYNC_META,
        [
            (key, value if isinstance(value, str) else json.dumps(value, sort_keys=True), now)
            for key, value in sorted(values.items())
        ],
    )


def _geometry_meta(stats: GeometryStats) -> dict[str, object]:
    """The coverage numbers SPEC §11 needs to tell a data gap from a matching gap."""
    snap = stats.snap
    return {
        "signs_source_rows": stats.stage.source_rows,
        "signs_active": stats.stage.active_rows,
        "signs_loaded": snap.signs,
        "signs_duplicate_dropped": stats.stage.duplicate_rows_dropped,
        "signs_rejected": stats.stage.rejected_rows,
        "signs_by_panel_class": stats.stage.panel_counts,
        "signs_regulation": stats.stage.regulation_rows,
        "signs_snapped": snap.matched,
        "signs_snapped_share": round(snap.matched_share, 4),
        "snap_single_segment": snap.single_segment,
        "snap_chain_walk": snap.chain_walk,
        "snap_ambiguous_chain": snap.ambiguous_chain,
        "snap_distance_clamped": snap.distance_clamped,
        "snap_mean_confidence": snap.mean_confidence,
        "snap_unmatched_reasons": snap.unmatched_reasons,
        "snap_unmatched_reason_classes": snap.unmatched_reason_classes,
        "blockface_sides_with_signs": snap.blockface_sides,
        "blockface_sides_matched": snap.blockface_sides_matched,
        "blockface_sides_matched_share": round(snap.blockface_side_share, 4),
        "blockface_side_reason_classes": snap.blockface_side_reason_classes,
        "street_segments": stats.street_segments,
        "street_nodes": stats.street_nodes,
        "regulation_segments": asdict(stats.segments),
        "geometry_elapsed_s": stats.elapsed_s,
    }


def _write_nodes(conn: sqlite3.Connection, graph: StreetGraph) -> None:
    conn.executemany(
        _INSERT_NODE,
        [
            (node.node_id, node.lon, node.lat, json.dumps(sorted(node.street_norms)))
            for node in graph.nodes.values()
        ],
    )


def _write_segments(conn: sqlite3.Connection, segments: Iterable[StreetSegment]) -> None:
    rows = []
    for segment in segments:
        geom = json.dumps(mapping(segment.line_deg), separators=(",", ":"))
        min_lon, min_lat, max_lon, max_lat = segment.line_deg.bounds
        rows.append(
            (
                segment.segment_id,
                segment.street_name,
                segment.street_norm,
                segment.from_node,
                segment.to_node,
                segment.width_ft,
                segment.length_ft,
                geom,
                min_lon,
                min_lat,
                max_lon,
                max_lat,
                segment.left_low_address,
                segment.left_high_address,
                segment.right_low_address,
                segment.right_high_address,
            )
        )
    conn.executemany(_INSERT_SEGMENT, rows)


def _write_signs(conn: sqlite3.Connection, snaps: Sequence[SnapResult]) -> None:
    conn.executemany(
        _INSERT_SIGN,
        [
            (
                snap.sign.sign_id,
                snap.sign.order_number,
                snap.sign.on_street,
                snap.sign.from_street,
                snap.sign.to_street,
                snap.sign.side_of_street,
                snap.sign.distance_from_intersection_ft,
                snap.sign.arrow_direction,
                snap.sign.facing_direction,
                snap.sign.sign_code,
                snap.sign.sign_description,
                snap.sign.sign_x_coord,
                snap.sign.sign_y_coord,
                snap.derived_lon,
                snap.derived_lat,
                snap.segment_id,
                snap.snap_confidence,
                snap.notes_text,
                int(snap.sign.is_regulation),
                snap.sign.panel_class.value,
            )
            for snap in snaps
        ],
    )


def _write_regulation_segments(
    conn: sqlite3.Connection, segments: Sequence[RegulationSegment]
) -> None:
    conn.executemany(
        _INSERT_REG_SEGMENT,
        [
            (
                segment.reg_seg_id,
                segment.segment_id,
                segment.side,
                segment.start_ft,
                segment.end_ft,
                json.dumps(segment.geometry, separators=(",", ":")),
                segment.bbox[0],
                segment.bbox[1],
                segment.bbox[2],
                segment.bbox[3],
                segment.length_ft,
                segment.capacity_cars,
                int(segment.capacity_approximate),
                segment.confidence,
                json.dumps(list(segment.derived_from)),
            )
            for segment in segments
        ],
    )
