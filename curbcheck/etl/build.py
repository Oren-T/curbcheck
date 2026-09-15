"""Run the ETL steps in order and write a fresh database, then swap it in.

Only this module and `db` touch SQLite; every step it calls is a pure function
over dataclasses. The order is stage -> streets -> snap -> parse -> segments ->
load: the parse result has to exist before `segments.resolve_segments` runs,
because the arrow-extension rule needs to know which posts carry "the same kind
of sign" and the parsed action is a better answer to that than the description's
first two words. `parse.parse_description` is cached per distinct string, so
reading the same descriptions again in `run_parse` costs nothing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from shapely.geometry import mapping

from curbcheck import db
from curbcheck.config import DB_PATH, RAW_DIR
from curbcheck.etl import fetch
from curbcheck.etl.calendar import CalendarReport, load_asp_suspensions
from curbcheck.etl.meters import MeterReport, resolve_meter_rates
from curbcheck.etl.parse import parse_description
from curbcheck.etl.segments import (
    RegulationSegment,
    SegmentReport,
    SignReading,
    arrow_arity,
    reading_of,
    resolve_segments,
)
from curbcheck.etl.snap import SnapReport, SnapResult, snap_signs
from curbcheck.etl.stage import StageReport, stage_centerline, stage_signs
from curbcheck.etl.streets import StreetGraph, StreetSegment, build_graph
from curbcheck.model import (
    ALL_DAYS,
    Action,
    Arrow,
    Flags,
    ParsedSign,
    ParseMethod,
    Regulation,
    VehicleClass,
)

LOGGER = logging.getLogger(__name__)

# docs/DECISIONS.md D13: a sign the grammar cannot read still gets a row, so the
# engine sees it in the stack. The rule itself is a prohibition covering every
# day, which is the reading that can only cost the user a spot, never a tow.
UNPARSED_PLACEHOLDER = Regulation(
    action=Action.PARK, permitted=False, days=list(ALL_DAYS), arrow=Arrow.NONE
)

# SPEC §8.5 ex. 7: a meta sign states no rule of its own, it modifies a sibling.
# It is stored as a prohibition so that a reader which somehow missed
# `flags.meta` still errs towards keeping the spot off the legal list, and at a
# confidence below `engine.resolve.AMBIGUITY_THRESHOLD` so the stack it sits in
# reads as AMBIGUOUS (docs/DECISIONS.md D17).
META_PLACEHOLDER = Regulation(
    action=Action.PARK,
    permitted=False,
    vehicle_class=VehicleClass.ALL,
    days=list(ALL_DAYS),
    flags=Flags(meta=True),
    arrow=Arrow.NONE,
)
META_CONFIDENCE = 0.7

_META_NOTE = "a sibling sign says METERS ARE NOT IN EFFECT ABOVE TIMES"

# Compass unit vectors in lon/lat, used to read a single arrow's bearing against
# the direction a regulation segment's curb line is drawn in.
_COMPASS_LONLAT: dict[str, tuple[float, float]] = {
    "N": (0.0, 1.0),
    "S": (0.0, -1.0),
    "E": (1.0, 0.0),
    "W": (-1.0, 0.0),
}

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
    " confidence, derived_from, gap_kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_INSERT_METER_RATE = (
    "INSERT OR REPLACE INTO meter_rate (blockface_id, segment_id, side, rate_label, hour_rates,"
    " commercial_hour_rates, max_session_min, source, confidence, geom, min_lon, min_lat,"
    " max_lon, max_lat) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_INSERT_ASP = (
    "INSERT OR REPLACE INTO asp_suspension (date, is_major_legal_holiday, meters_suspended,"
    " label) VALUES (?, ?, ?, ?)"
)
_INSERT_SYNC_META = "INSERT OR REPLACE INTO sync_meta (key, value, updated_at) VALUES (?, ?, ?)"

_SIGNS_FOR_PARSE_SQL = (
    "SELECT sign_id, sign_description, arrow_direction, on_street, from_street, to_street,"
    " side_of_street, distance_from_intersection FROM sign WHERE is_regulation = 1"
)
_REG_SEGMENTS_FOR_PARSE_SQL = "SELECT reg_seg_id, geom, derived_from FROM regulation_segment"
_METERED_SEGMENTS_SQL = (
    "SELECT DISTINCT rs.reg_seg_id, rs.segment_id, rs.side, rs.geom FROM regulation_segment rs"
    " JOIN regulation r ON r.reg_seg_id = rs.reg_seg_id WHERE r.metered = 1"
)


@dataclass(frozen=True)
class GeometryStats:
    """Everything the geometry half of the ETL measured about this snapshot."""

    stage: StageReport
    snap: SnapReport
    segments: SegmentReport
    street_segments: int
    street_nodes: int
    elapsed_s: float


@dataclass(frozen=True)
class ParseStats:
    """What `run_parse` wrote, split the ways SPEC §11 needs it split."""

    distinct_descriptions: int
    regulation_rows: int
    rows_by_parse_method: dict[str, int]
    segments_with_rules: int
    segments_without_rules: int
    unparsed_signs: int
    advisory_signs: int
    meta_signs: int
    meta_segments: int
    arrow_disagreements: int
    elapsed_s: float


@dataclass(frozen=True)
class BuildStats:
    """One snapshot's worth of numbers, from every step."""

    geometry: GeometryStats
    parse: ParseStats
    meters: MeterReport
    calendar: CalendarReport
    elapsed_s: float

    @property
    def snap(self) -> SnapReport:
        return self.geometry.snap

    @property
    def segments(self) -> SegmentReport:
        return self.geometry.segments


def run_geometry(
    raw_dir: Path, conn: sqlite3.Connection, *, graph: StreetGraph | None = None
) -> GeometryStats:
    """stage -> streets -> snap -> parse -> segments, written to an open database.

    The caller owns the connection and the schema; this commits once at the end
    so a failure anywhere leaves the database untouched. `graph` is accepted so
    `build_all` can build the centerline once and hand the same graph to the
    meter join.
    """
    started = time.monotonic()
    resolved_graph = graph or build_centerline_graph(raw_dir)
    signs, stage_report = stage_signs(fetch.load_rows("signs_manhattan", raw_dir))
    snaps, snap_report = snap_signs(signs, resolved_graph)
    readings = sign_readings(snap.sign.sign_description for snap in snaps)
    reg_segments, segment_report = resolve_segments(
        snaps, readings=readings, graph=resolved_graph
    )

    _write_nodes(conn, resolved_graph)
    _write_segments(conn, resolved_graph.segments.values())
    _write_signs(conn, snaps)
    _write_regulation_segments(conn, reg_segments)
    stats = GeometryStats(
        stage=stage_report,
        snap=snap_report,
        segments=segment_report,
        street_segments=len(resolved_graph.segments),
        street_nodes=len(resolved_graph.nodes),
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


def build_centerline_graph(raw_dir: Path) -> StreetGraph:
    return build_graph(stage_centerline(fetch.load_rows("centerline_manhattan", raw_dir)))


def sign_readings(descriptions: Iterable[str]) -> dict[str, SignReading]:
    """What the grammar read, per distinct description, for `segments.resolve_segments`.

    The span rules need two things off each sign — which family it belongs to and
    how many arrows it carries — and both come from the same parse, so the
    grammar is the only reader of either (docs/ARCHITECTURE.md, the parse ->
    segments ordering). `parse_description` is cached, so `run_parse` reading the
    same strings again afterwards costs nothing.
    """
    readings: dict[str, SignReading] = {}
    for description in descriptions:
        if description not in readings:
            readings[description] = reading_of(parse_description(description))
    return readings


def run_parse(conn: sqlite3.Connection) -> ParseStats:
    """Write one `regulation` row per rule per sign on every `regulation_segment`.

    Reads the descriptions already in the `sign` table, so it must run after
    `run_geometry`. Three rules beyond "parse the string":

    - An unparsed sign still gets a row (docs/DECISIONS.md D13), carrying
      `UNPARSED_PLACEHOLDER` so a stack containing it can never read as a
      confident LEGAL.
    - A single arrow reaches here as the `Arrow.FORWARD` placeholder; it is
      resolved against the sign's `arrow_direction` bearing and the direction
      the segment's curb line is drawn in, and stored for audit.
    - A meta sign ("METERS ARE NOT IN EFFECT ABOVE TIMES") is written at
      `META_CONFIDENCE`, which forces its segment to AMBIGUOUS
      (docs/DECISIONS.md D17).
    """
    started = time.monotonic()
    signs = {str(row["sign_id"]): row for row in conn.execute(_SIGNS_FOR_PARSE_SQL)}
    meta_by_post = _meta_signs_by_post(signs.values())
    counts = _ParseCounts()
    rows: list[dict[str, Any]] = []
    for segment in conn.execute(_REG_SEGMENTS_FOR_PARSE_SQL).fetchall():
        forward = _segment_points(str(segment["geom"]))
        sign_ids = _json_string_list(segment["derived_from"])
        members = [signs[sign_id] for sign_id in sign_ids if sign_id in signs]
        segment_rows = _segment_regulations(
            str(segment["reg_seg_id"]),
            members + _meta_siblings(members, meta_by_post),
            forward,
            counts,
        )
        if segment_rows:
            counts.segments_with_rules += 1
        else:
            counts.segments_without_rules += 1
        rows.extend(segment_rows)

    conn.executemany(db.INSERT_REGULATION_SQL, rows)
    by_method: dict[str, int] = {}
    for row in rows:
        method = str(row["parse_method"])
        by_method[method] = by_method.get(method, 0) + 1
    stats = ParseStats(
        distinct_descriptions=len({str(row["sign_description"]) for row in signs.values()}),
        regulation_rows=len(rows),
        rows_by_parse_method=by_method,
        segments_with_rules=counts.segments_with_rules,
        segments_without_rules=counts.segments_without_rules,
        unparsed_signs=counts.unparsed_signs,
        advisory_signs=counts.advisory_signs,
        meta_signs=counts.meta_signs,
        meta_segments=counts.meta_segments,
        arrow_disagreements=counts.arrow_disagreements,
        elapsed_s=round(time.monotonic() - started, 2),
    )
    write_sync_meta(conn, _parse_meta(stats))
    conn.commit()
    LOGGER.info(
        "build.parse rows=%d by_method=%s unparsed_signs=%d meta_segments=%d arrow_conflicts=%d",
        stats.regulation_rows,
        stats.rows_by_parse_method,
        stats.unparsed_signs,
        stats.meta_segments,
        stats.arrow_disagreements,
    )
    return stats


def run_meters(
    raw_dir: Path, conn: sqlite3.Connection, *, graph: StreetGraph | None = None
) -> MeterReport:
    """Join ParkNYC blockfaces to `street_segment` and fill `meter_rate` (SPEC §13.1)."""
    resolved_graph = graph or build_centerline_graph(raw_dir)
    metered = [
        (
            str(row["reg_seg_id"]),
            _optional_text(row["segment_id"]),
            _optional_text(row["side"]),
            str(row["geom"]),
        )
        for row in conn.execute(_METERED_SEGMENTS_SQL)
    ]
    rates, report = resolve_meter_rates(raw_dir, resolved_graph, metered_segments=metered)
    conn.executemany(
        _INSERT_METER_RATE,
        [
            (
                rate.blockface_id,
                rate.segment_id,
                rate.side,
                rate.rate_label,
                json.dumps(list(rate.hour_rates)),
                json.dumps(list(rate.commercial_hour_rates)),
                rate.max_session_min,
                rate.source,
                rate.confidence,
                None if rate.geometry is None else json.dumps(rate.geometry, separators=(",", ":")),
                *(rate.bbox or (None, None, None, None)),
            )
            for rate in rates
        ],
    )
    write_sync_meta(conn, {f"meter_{key}": value for key, value in asdict(report).items()})
    conn.commit()
    LOGGER.info(
        "build.meters rows=%d matched=%d unmatched=%d zone_fallbacks=%d metered_without_rate=%d",
        len(rates),
        report.matched,
        report.unmatched,
        report.rate_zone_fallbacks,
        report.metered_segments_without_rate,
    )
    return report


def run_calendar(raw_dir: Path, conn: sqlite3.Connection) -> CalendarReport:
    """Load the DOT ASP suspension ICS into `asp_suspension` (docs/DECISIONS.md D11).

    Independent of the geometry steps. A missing calendar is not fatal: the
    table stays empty and `sync_meta.calendar_missing` says so, because a build
    that refuses to finish would leave the user with no database at all.
    """
    suspensions, report = load_asp_suspensions(raw_dir)
    conn.executemany(
        _INSERT_ASP,
        [
            (
                day.date.isoformat(),
                int(day.is_major_legal_holiday),
                int(day.meters_suspended),
                day.label,
            )
            for day in suspensions
        ],
    )
    meta: dict[str, object] = {f"calendar_{key}": value for key, value in asdict(report).items()}
    meta["calendar_missing"] = int(report.source_path is None)
    write_sync_meta(conn, meta)
    conn.commit()
    if report.source_path is None:
        LOGGER.warning("build.calendar no ICS in %s; asp_suspension is empty", raw_dir / "calendar")
    else:
        LOGGER.info(
            "build.calendar days=%d dates=%d major_holidays=%d source=%s",
            report.suspended_days,
            report.distinct_dates,
            report.major_holidays,
            report.source_path,
        )
    return report


def build_all(raw_dir: Path = RAW_DIR, out_path: Path = DB_PATH) -> BuildStats:
    """Build a complete database beside `out_path` and rename it into place.

    The working file is a sibling so `db.swap_in` can rename atomically, which
    it can only do within one filesystem.
    """
    started = time.monotonic()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    working = out_path.with_name(out_path.name + ".new")
    working.unlink(missing_ok=True)
    conn = db.connect(working)
    try:
        db.create_schema(conn)
        graph = build_centerline_graph(raw_dir)
        geometry = run_geometry(raw_dir, conn, graph=graph)
        parse = run_parse(conn)
        meters = run_meters(raw_dir, conn, graph=graph)
        calendar = run_calendar(raw_dir, conn)
        write_sync_meta(conn, {"last_sync_at": datetime.now(UTC).isoformat(timespec="seconds")})
        conn.commit()
    finally:
        conn.close()
    db.swap_in(working, out_path)
    LOGGER.info("build.swapped path=%s", out_path)
    return BuildStats(
        geometry=geometry,
        parse=parse,
        meters=meters,
        calendar=calendar,
        elapsed_s=round(time.monotonic() - started, 2),
    )


def write_sync_meta(conn: sqlite3.Connection, values: Mapping[str, object]) -> None:
    """Upsert key/value rows; non-string values are stored as JSON."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    conn.executemany(
        _INSERT_SYNC_META,
        [
            (key, value if isinstance(value, str) else json.dumps(value, sort_keys=True), now)
            for key, value in sorted(values.items())
        ],
    )


def _meta_signs_by_post(signs: Iterable[sqlite3.Row]) -> dict[tuple[str, ...], list[sqlite3.Row]]:
    """Meta signs keyed by the post they stand on (docs/DATA.md §1.8's post grouping).

    SPEC §8.5 says a meta sign links to the sibling rule it modifies. The
    sibling is the panel above it on the same post, and a post is a
    `(blockface-side, distance_from_intersection)` pair, so that is the key.
    """
    by_post: dict[tuple[str, ...], list[sqlite3.Row]] = {}
    for sign in signs:
        parsed = parse_description(str(sign["sign_description"]))
        if any(rule.flags.meta for rule in parsed.regulations):
            by_post.setdefault(_post_key(sign), []).append(sign)
    return by_post


def _meta_siblings(
    members: Sequence[sqlite3.Row], meta_by_post: Mapping[tuple[str, ...], list[sqlite3.Row]]
) -> list[sqlite3.Row]:
    """Meta signs sharing a post with this span's signs but not extended onto it.

    A meta sign carries no arrow, so `segments` gives it the whole blockface-side
    while the metered sign it modifies keeps a shorter arrow span. Without this
    the two never meet in one stack and the modification would be lost.
    """
    present = {str(sign["sign_id"]) for sign in members}
    siblings: dict[str, sqlite3.Row] = {}
    for sign in members:
        for meta in meta_by_post.get(_post_key(sign), ()):
            sign_id = str(meta["sign_id"])
            if sign_id not in present:
                siblings[sign_id] = meta
    return list(siblings.values())


def _post_key(sign: sqlite3.Row) -> tuple[str, ...]:
    return tuple(
        str(sign[column])
        for column in (
            "on_street",
            "from_street",
            "to_street",
            "side_of_street",
            "distance_from_intersection",
        )
    )


@dataclass
class _ParseCounts:
    segments_with_rules: int = 0
    segments_without_rules: int = 0
    unparsed_signs: int = 0
    advisory_signs: int = 0
    meta_signs: int = 0
    meta_segments: int = 0
    arrow_disagreements: int = 0
    seen_arrow_conflicts: set[str] = field(default_factory=set)


def _segment_regulations(
    reg_seg_id: str,
    signs: Sequence[sqlite3.Row],
    forward: tuple[float, float] | None,
    counts: _ParseCounts,
) -> list[dict[str, Any]]:
    """Every `regulation` row one segment's signs produce, meta links applied.

    Two posts on one span often carry the same panel text; identical rules from
    identical text pointing the same way are one rule, so they collapse rather
    than stacking a duplicate the engine would have to resolve against itself.
    """
    rows: dict[str, dict[str, Any]] = {}
    for sign in signs:
        description = str(sign["sign_description"])
        parsed = parse_description(description)
        arrow_direction = _optional_text(sign["arrow_direction"])
        _count_arrow_disagreement(description, parsed, counts)
        for index, params in enumerate(
            _sign_regulations(reg_seg_id, description, parsed, arrow_direction, forward, counts)
        ):
            key = f"{params['action']}|{params['arrow']}|{index}|{description}"
            params["reg_id"] = hashlib.sha256(f"{reg_seg_id}|{key}".encode()).hexdigest()[:16]
            rows.setdefault(key, params)
    return _apply_meta_links(list(rows.values()), counts)


def _sign_regulations(
    reg_seg_id: str,
    description: str,
    parsed: ParsedSign,
    arrow_direction: str | None,
    forward: tuple[float, float] | None,
    counts: _ParseCounts,
) -> list[dict[str, Any]]:
    if parsed.parse_method is ParseMethod.UNPARSED:
        counts.unparsed_signs += 1
        return [
            db.regulation_to_params(
                UNPARSED_PLACEHOLDER,
                reg_id="",
                reg_seg_id=reg_seg_id,
                raw_sign_description=description,
                parse_method=ParseMethod.UNPARSED,
                parse_confidence=0.0,
                parse_notes=parsed.notes,
            )
        ]
    if not parsed.regulations:
        # A string the grammar found no rule head in and `advisory_class` named:
        # "NO ENGINE IDLING", "KEEP RIGHT". It regulates traffic, not the curb.
        counts.advisory_signs += 1
        return []

    return [
        db.regulation_to_params(
            _stored_rule(rule, arrow_direction, forward),
            reg_id="",
            reg_seg_id=reg_seg_id,
            raw_sign_description=description,
            parse_method=parsed.parse_method,
            parse_confidence=META_CONFIDENCE if rule.flags.meta else parsed.confidence,
            parse_notes=_rule_notes(rule, parsed),
        )
        for rule in parsed.regulations
    ]


def _stored_rule(
    rule: Regulation, arrow_direction: str | None, forward: tuple[float, float] | None
) -> Regulation:
    """The rule as stored: meta signs become the placeholder, arrows get resolved."""
    if rule.flags.meta:
        return META_PLACEHOLDER
    return rule.model_copy(update={"arrow": _resolve_arrow(rule.arrow, arrow_direction, forward)})


def _rule_notes(rule: Regulation, parsed: ParsedSign) -> str:
    if not rule.flags.meta:
        return parsed.notes
    # SPEC §8.5 wants a meta sign linked to the sibling it modifies. We cannot
    # yet tell which times "ABOVE TIMES" points at, so the link is recorded and
    # the stack is made ambiguous rather than re-priced (docs/DECISIONS.md D17).
    return "; ".join(
        part
        for part in (parsed.notes, "meta sign: modifies the metered rule(s) on this span")
        if part
    )


def _apply_meta_links(rows: list[dict[str, Any]], counts: _ParseCounts) -> list[dict[str, Any]]:
    """Mark the metered rules a meta sign on the same span modifies (SPEC §8.5)."""
    meta_rows = [row for row in rows if json.loads(str(row["flags"])).get("meta")]
    if not meta_rows:
        return rows
    counts.meta_signs += len(meta_rows)
    counts.meta_segments += 1
    for row in rows:
        if row["metered"]:
            row["parse_notes"] = "; ".join(
                part for part in (str(row["parse_notes"]), _META_NOTE) if part
            )
    return rows


def _count_arrow_disagreement(description: str, parsed: ParsedSign, counts: _ParseCounts) -> None:
    """Count strings the glyph fallback would read a different arrow on.

    `segments` takes arity from the grammar now, so this no longer means a span
    is at risk; it measures how far `segments.arrow_arity` — the fallback for
    strings no rule parsed from — has drifted from the grammar it backs up.
    """
    if not parsed.regulations or description in counts.seen_arrow_conflicts:
        return
    arity = arrow_arity(description).value
    expected = {
        Arrow.FORWARD: "single",
        Arrow.BACKWARD: "single",
        Arrow.BOTH: "double",
        Arrow.NONE: "none",
    }
    if any(expected[rule.arrow] != arity for rule in parsed.regulations):
        counts.seen_arrow_conflicts.add(description)
        counts.arrow_disagreements += 1


def _resolve_arrow(
    arrow: Arrow, arrow_direction: str | None, forward: tuple[float, float] | None
) -> Arrow:
    """Turn the grammar's single-arrow placeholder into a real direction.

    `Arrow.FORWARD` out of the parser means only "one arrow" (parse package
    docstring); the bearing is the compass word in `arrow_direction`, read
    against the direction the span's curb line runs in. An unreadable bearing
    falls back to `Arrow.NONE`, which is the whole-blockface reading
    `segments._span_for` already used for it.
    """
    if arrow is not Arrow.FORWARD:
        return arrow
    if forward is None or not arrow_direction:
        return Arrow.NONE
    compass = _COMPASS_LONLAT.get(arrow_direction.strip().upper()[:1])
    if compass is None:
        return Arrow.NONE
    alignment = forward[0] * compass[0] + forward[1] * compass[1]
    return Arrow.FORWARD if alignment >= 0 else Arrow.BACKWARD


def _segment_points(geom: str) -> tuple[float, float] | None:
    """Unit vector of a segment's curb line, from its start to its end.

    The curb line is built by offsetting the chain in its canonical direction
    and neither `offset_curve` nor the manual fallback reverses it, so the first
    coordinate is `start_ft` and the last is `end_ft` (see `segments`).
    Longitude is scaled to metres so the dot product against a compass vector is
    not skewed by Manhattan's 0.76 cos(lat) factor.
    """
    coordinates = json.loads(geom).get("coordinates") or []
    if len(coordinates) < 2:
        return None
    (lon0, lat0), (lon1, lat1) = coordinates[0], coordinates[-1]
    dx = (float(lon1) - float(lon0)) * math.cos(math.radians(float(lat0)))
    dy = float(lat1) - float(lat0)
    span = math.hypot(dx, dy)
    if span == 0:
        return None
    return (dx / span, dy / span)


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
        # SPEC §11's grey state, per centerline side: how many sides a rule
        # covers, and how many draw a placeholder because the source has no
        # signs there or because none of its signs could be snapped.
        "coverage.sides_with_rules": stats.segments.sides_with_rules,
        "coverage.no_signs_sides": stats.segments.no_signs_sides,
        "coverage.unmatched_sides": stats.segments.unmatched_sides,
        "geometry_elapsed_s": stats.elapsed_s,
    }


def _parse_meta(stats: ParseStats) -> dict[str, object]:
    return {
        "parse_distinct_descriptions": stats.distinct_descriptions,
        "regulation_rows": stats.regulation_rows,
        "regulation_rows_by_parse_method": stats.rows_by_parse_method,
        "parse_segments_with_rules": stats.segments_with_rules,
        "parse_segments_without_rules": stats.segments_without_rules,
        "parse_unparsed_signs": stats.unparsed_signs,
        "parse_advisory_signs": stats.advisory_signs,
        "parse_meta_signs": stats.meta_signs,
        "parse_meta_segments": stats.meta_segments,
        # Strings where the glyph fallback in segments.arrow_arity would read a
        # different arrow from the grammar that now decides every span.
        "parse_arrow_disagreements": stats.arrow_disagreements,
        "parse_elapsed_s": stats.elapsed_s,
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
                snap.sign.panel_class,
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
                segment.gap_kind,
            )
            for segment in segments
        ],
    )


def _json_string_list(value: Any) -> list[str]:
    parsed = json.loads(str(value))
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _optional_text(value: Any) -> str | None:
    return None if value is None else str(value)
