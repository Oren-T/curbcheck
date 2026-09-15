"""The `/api` routes. Contract and examples: `docs/API.md`.

Text coming out of these handlers is returned exactly as the ETL stored it,
including sign descriptions that contain HTML. The frontend is the single
escaping boundary (STYLE_GUIDE §4); escaping here as well would double-encode
the raw sign text that CLAUDE.md requires be shown next to every verdict.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Query, Request

from curbcheck.api.errors import ApiError, database_unavailable
from curbcheck.api.schemas import MAX_QUERY_CHARS, REG_SEG_ID_PATTERN, SearchRequest
from curbcheck.db import connect, regulation_from_row
from curbcheck.engine.search import SearchResult, search
from curbcheck.geocode import GeocodeCandidate, geocode

# SPEC §17, the persistent banner. Kept verbatim except for the markdown bold,
# which is the frontend's job.
DISCLAIMER = (
    "CurbCheck is advisory only. This tool derives parking legality and price from NYC "
    "Open Data (NYC DOT Sign Information Management System) and may be incomplete, out of "
    "date, or misread by the software. Parking regulations change and temporary or "
    "construction signage may override what is shown here. The posted sign at the curb is "
    "the only authoritative regulation. Always read the posted sign before parking. "
    "Sign-free prohibitions — within 15 feet of a fire hydrant (34 RCNY §4-08(e)(2)), "
    "crosswalks, bus stops, and driveways — apply even where no sign is shown. CurbCheck "
    "does not predict whether a space is physically available. "
    "Data © NYC Open Data; basemap © OpenStreetMap contributors."
)

# SPEC §11 requires both of these on every answer, not just on bad ones.
# Temporary and construction signage is largely absent from the source dataset
# (Research Q A.5), so the warning is universal rather than conditional.
TEMPORARY_SIGNAGE_CAVEAT = (
    "Temporary or construction signage may override what is shown here. The posted sign "
    "at the curb is the only authoritative regulation."
)
ASP_SUSPENSION_CAVEAT = (
    "Emergency ASP suspensions are not reflected. Same-day weather and parade suspensions "
    "are only visible if the optional 311 live check is enabled, and it is off by default."
)
UNIVERSAL_CAVEATS = (TEMPORARY_SIGNAGE_CAVEAT, ASP_SUSPENSION_CAVEAT)

_SEGMENT_SQL = (
    "SELECT rs.reg_seg_id, rs.segment_id, rs.side, rs.start_ft, rs.end_ft, rs.geom,"
    " rs.length_ft, rs.capacity_cars, rs.capacity_approximate, rs.confidence,"
    " rs.derived_from, ss.street_name"
    " FROM regulation_segment rs LEFT JOIN street_segment ss ON ss.segment_id = rs.segment_id"
    " WHERE rs.reg_seg_id = ?"
)
_SEGMENT_REGULATION_SQL = (
    "SELECT reg_id, reg_seg_id, action, permitted, vehicle_class, exclusive, days_mask,"
    " time_from, time_to, metered, max_duration_min, flags, effective_from, effective_to,"
    " arrow, raw_sign_description, parse_method, parse_confidence"
    " FROM regulation WHERE reg_seg_id = ? ORDER BY reg_id"
)
_SEGMENT_SIGN_SQL = (
    "SELECT sign_id, order_number, sign_code, sign_description, on_street, from_street,"
    " to_street, side_of_street, distance_from_intersection, snap_confidence, snap_notes,"
    " is_regulation, panel_class FROM sign WHERE segment_id = ? ORDER BY sign_id"
)
_METER_RATE_SQL = (
    "SELECT blockface_id, side, rate_label, hour_rates, max_session_min"
    " FROM meter_rate WHERE segment_id = ? ORDER BY blockface_id"
)
_SYNC_META_SQL = "SELECT key, value FROM sync_meta"
_SIGN_COUNT_SQL = "SELECT count(*) FROM sign"

router = APIRouter()


@contextmanager
def open_database(request: Request) -> Iterator[sqlite3.Connection]:
    """A read-only connection for the life of one request.

    Deliberately not a FastAPI dependency. `sqlite3` objects are bound to the
    thread that created them, and FastAPI runs sync dependencies and sync
    endpoints as separate threadpool tasks that need not land on the same
    worker thread. Opening inside the endpoint keeps creation and use on one
    thread, so `check_same_thread` stays at its safe default. Opening a SQLite
    file costs tens of microseconds, which is nothing next to the query.
    """
    db_path = request.app.state.db_path
    if not db_path.is_file():
        raise database_unavailable()
    connection = connect(db_path, readonly=True)
    try:
        yield connection
    finally:
        connection.close()


@router.post("/search")
def post_search(request: Request, body: SearchRequest) -> dict[str, Any]:
    """Rank the curb spans near a destination for a window. See docs/API.md."""
    with open_database(request) as conn:
        destination = _destination(conn, body)
        found = search(
            conn,
            lon=destination["lon"],
            lat=destination["lat"],
            t1=body.t1,
            t2=body.t2,
            walk_minutes_max=body.walk_minutes,
            weights=body.weights.to_engine(),
            limit=body.limit,
            map_limit=body.map_limit,
        )
        return {
            "destination": destination,
            # Ranked legal first, then every other verdict in radius. One array
            # rather than two: the map draws all of it and the list renders all
            # of it, so splitting it would only make the frontend rejoin it.
            "results": [_result_payload(result) for result in found.all],
            "counts": asdict(found.counts),
            "disclaimer": DISCLAIMER,
            "caveats": list(UNIVERSAL_CAVEATS),
            "sync": _sync_summary(conn),
        }


@router.get("/segment/{reg_seg_id}")
def get_segment(request: Request, reg_seg_id: str) -> dict[str, Any]:
    """The rule stack, the raw sign text, the meter rates, and the geometry behind one verdict."""
    if not REG_SEG_ID_PATTERN.match(reg_seg_id):
        raise ApiError(400, "invalid_request", "not a segment id")

    with open_database(request) as conn:
        row = conn.execute(_SEGMENT_SQL, (reg_seg_id,)).fetchone()
        if row is None:
            raise ApiError(404, "not_found", "no such segment")
        segment_id = None if row["segment_id"] is None else str(row["segment_id"])
        side = None if row["side"] is None else str(row["side"])
        return {
            "segment": {
                "reg_seg_id": str(row["reg_seg_id"]),
                "segment_id": segment_id,
                "street_name": _optional_str(row["street_name"]),
                "side": side,
                "start_ft": _optional_float(row["start_ft"]),
                "end_ft": _optional_float(row["end_ft"]),
                "length_ft": _optional_float(row["length_ft"]),
                "capacity_cars": _optional_int(row["capacity_cars"]),
                "capacity_approximate": bool(row["capacity_approximate"]),
                "confidence": float(row["confidence"] or 0.0),
                "derived_from": _json_list(row["derived_from"]),
            },
            "geometry": json.loads(str(row["geom"])),
            "regulations": _segment_regulations(conn, reg_seg_id),
            "signs": _segment_signs(conn, segment_id),
            "meter_rates": _segment_meter_rates(conn, segment_id, side),
        }


@router.get("/geocode")
def get_geocode(
    request: Request, q: str = Query(min_length=1, max_length=MAX_QUERY_CHARS)
) -> dict[str, Any]:
    """Local address and intersection lookup. An empty candidate list is a 200, not an error."""
    with open_database(request) as conn:
        return {"query": q, "candidates": [_candidate_payload(c) for c in geocode(conn, q)]}


@router.get("/health")
def get_health(request: Request) -> dict[str, Any]:
    """Answers even with no database: this is the endpoint you ask *about* the database."""
    db_path = request.app.state.db_path
    if not db_path.is_file():
        return {"status": "degraded", "db_present": False, "db_readonly": False, "sign_count": 0}

    connection = connect(db_path, readonly=True)
    try:
        sign_count = int(connection.execute(_SIGN_COUNT_SQL).fetchone()[0])
    except sqlite3.Error:
        # A file that exists but will not answer is a failed or partial sync,
        # which SPEC §11 says to surface rather than to hide behind a 500.
        return {"status": "degraded", "db_present": True, "db_readonly": True, "sign_count": 0}
    finally:
        connection.close()
    return {
        "status": "ok",
        "db_present": True,
        "db_readonly": True,
        "sign_count": sign_count,
    }


@router.get("/sync-status")
def get_sync_status(request: Request) -> dict[str, str]:
    """The `sync_meta` table as a flat object. Values are returned as stored, never parsed."""
    with open_database(request) as conn:
        return {str(row["key"]): str(row["value"]) for row in conn.execute(_SYNC_META_SQL)}


def _destination(conn: sqlite3.Connection, body: SearchRequest) -> dict[str, Any]:
    if body.lat is not None and body.lon is not None:
        return {"lat": body.lat, "lon": body.lon, "label": f"{body.lat:.6f}, {body.lon:.6f}"}

    candidates = geocode(conn, body.address or "")
    if not candidates:
        raise ApiError(404, "address_not_found", "could not find that address in Manhattan")
    best = candidates[0]
    return {"lat": best.lat, "lon": best.lon, "label": best.label}


def _result_payload(result: SearchResult) -> dict[str, Any]:
    """`SearchResult` as JSON, field names unchanged, plus the universal caveat."""
    payload = asdict(result)
    payload["verdict"] = result.verdict.value
    payload["caveats"] = [*result.caveats, TEMPORARY_SIGNAGE_CAVEAT]
    return payload


def _candidate_payload(candidate: GeocodeCandidate) -> dict[str, Any]:
    payload = asdict(candidate)
    payload["kind"] = candidate.kind.value
    return payload


def _sync_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """The three `sync_meta` values the UI shows, tolerant of a half-built database.

    The ETL writes its own key names; these are the aliases it has used, newest
    first. A key the ETL stops writing degrades to null or zero rather than
    breaking search, which matters because search is what surfaces the rest of
    the failure states (SPEC §11). `/api/sync-status` returns the raw table.
    """
    meta = {str(row["key"]): str(row["value"]) for row in conn.execute(_SYNC_META_SQL)}
    coverage = _first(meta, "coverage_pct")
    if coverage is None:
        # The ETL publishes blockface-side coverage as a 0-1 share.
        share = _as_float(_first(meta, "blockface_sides_matched_share"))
        coverage = None if share is None else str(100 * share)
    return {
        "last_sync": _first(meta, "last_sync_at", "last_sync"),
        "sign_count": _as_int(_first(meta, "signs_loaded", "sign_count")),
        "coverage_pct": _as_float(coverage),
    }


def _first(meta: dict[str, str], *keys: str) -> str | None:
    for key in keys:
        if key in meta:
            return meta[key]
    return None


def _segment_regulations(conn: sqlite3.Connection, reg_seg_id: str) -> list[dict[str, Any]]:
    regulations = []
    for row in conn.execute(_SEGMENT_REGULATION_SQL, (reg_seg_id,)):
        regulations.append(
            {
                "reg_id": str(row["reg_id"]),
                "raw_sign_description": str(row["raw_sign_description"]),
                "parse_method": str(row["parse_method"]),
                "parse_confidence": float(row["parse_confidence"]),
                "regulation": regulation_from_row(row).model_dump(mode="json"),
            }
        )
    return regulations


def _segment_signs(conn: sqlite3.Connection, segment_id: str | None) -> list[dict[str, Any]]:
    if segment_id is None:
        return []
    return [
        {
            "sign_id": str(row["sign_id"]),
            "order_number": _optional_str(row["order_number"]),
            "sign_code": _optional_str(row["sign_code"]),
            "sign_description": str(row["sign_description"]),
            "on_street": _optional_str(row["on_street"]),
            "from_street": _optional_str(row["from_street"]),
            "to_street": _optional_str(row["to_street"]),
            "side_of_street": _optional_str(row["side_of_street"]),
            "distance_from_intersection": _optional_float(row["distance_from_intersection"]),
            "snap_confidence": _optional_float(row["snap_confidence"]),
            "snap_notes": _optional_str(row["snap_notes"]),
            "is_regulation": bool(row["is_regulation"]),
            "panel_class": str(row["panel_class"]),
        }
        for row in conn.execute(_SEGMENT_SIGN_SQL, (segment_id,))
    ]


def _segment_meter_rates(
    conn: sqlite3.Connection, segment_id: str | None, side: str | None
) -> list[dict[str, Any]]:
    """Rates for this blockface-side. SPEC §13.1(b): several zones may cover one."""
    if segment_id is None:
        return []
    rows = conn.execute(_METER_RATE_SQL, (segment_id,)).fetchall()
    return [
        {
            "blockface_id": str(row["blockface_id"]),
            "side": _optional_str(row["side"]),
            "rate_label": _optional_str(row["rate_label"]),
            "hour_rates": _json_list(row["hour_rates"]),
            "max_session_min": _optional_int(row["max_session_min"]),
        }
        for row in rows
        if row["side"] is None or side is None or str(row["side"]) == side
    ]


def _json_list(value: Any) -> list[str]:
    if value is None:
        return []
    try:
        parsed = json.loads(str(value))
    except (ValueError, json.JSONDecodeError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _as_int(value: str | None) -> int:
    try:
        return int(float(value)) if value is not None else 0
    except ValueError:
        return 0


def _as_float(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)
