"""Radius query over regulation segments, evaluated and ranked.

The only module in the engine that touches SQLite. Shape of the work: bbox
prefilter in SQL, exact distance in Python with shapely, one batched query per
kind of related row (never one per candidate), then verdict, price, and rank.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from shapely.errors import ShapelyError
from shapely.geometry import Point, shape

from curbcheck.db import placeholders, regulation_from_row
from curbcheck.engine.cost import (
    Weights,
    meter_price,
    risk_dollars,
    total_cost,
    walk_minutes_for_meters,
    walk_radius_meters,
)
from curbcheck.engine.resolve import (
    RegulationWithMeta,
    SegmentVerdict,
    Verdict,
    evaluate_segment,
)
from curbcheck.engine.window import CalendarContext
from curbcheck.model import ParseMethod

LOGGER = logging.getLogger(__name__)

# The ranked list the user reads is short; the map layer is not. Ranking and
# drawing were one capped list until docs/VALIDATION.md U1 measured what that
# costs: on the Upper East Side a 10-minute walk holds more than 500 legal
# spans, so `limit` cut every illegal and ambiguous one and the map drew nothing
# but green. The two caps are now separate.
DEFAULT_LIMIT = 100
DEFAULT_MAP_LIMIT = 2000
# 5,000 line features is about where MapLibre's first paint starts to lag on a
# laptop, and the whole of Manhattan holds 36,518 spans, so the map cap needs a
# ceiling a request cannot raise.
MAX_MAP_LIMIT = 5000

# SQLite's default SQLITE_MAX_VARIABLE_NUMBER is 32766 in modern builds, but
# older builds use 999. 400 ids per query is comfortably under both.
_ID_CHUNK = 400

# Local flat-earth scale. Over a 1 km radius at Manhattan's latitude the error
# against a proper projection is under a metre, and it keeps pyproj out of the
# request path. Values are the WGS-84 metres per degree at 40.75 N.
_M_PER_DEG_LAT = 111_132.0
_M_PER_DEG_LON_AT_EQUATOR = 111_320.0

_CANDIDATE_COLUMNS = (
    "reg_seg_id, segment_id, side, geom, length_ft, capacity_cars, confidence, derived_from"
)
_CANDIDATE_SQL = (
    "SELECT {columns} FROM regulation_segment"
    " WHERE max_lon >= ? AND min_lon <= ? AND max_lat >= ? AND min_lat <= ?"
)
_ASP_SQL = "SELECT date, is_major_legal_holiday, meters_suspended, label FROM asp_suspension"
# The ETL writes this key when `curbcheck sync` found no calendar file at all.
_CALENDAR_META_SQL = "SELECT value FROM sync_meta WHERE key = 'calendar_missing'"
_REGULATION_SQL = (
    "SELECT reg_id, reg_seg_id, action, permitted, vehicle_class, exclusive, days_mask,"
    " time_from, time_to, metered, max_duration_min, flags, effective_from, effective_to,"
    " arrow, raw_sign_description, parse_method, parse_confidence FROM regulation"
    " WHERE reg_seg_id IN ("
)
# PRAGMA takes no bound parameters, so the table name is a literal here.
_GAP_KIND_PRAGMA = "PRAGMA table_info(regulation_segment)"
_STREET_SQL = (
    "SELECT ss.segment_id, ss.street_name, fn.street_names AS from_names,"
    " tn.street_names AS to_names FROM street_segment ss"
    " LEFT JOIN street_node fn ON fn.node_id = ss.from_node"
    " LEFT JOIN street_node tn ON tn.node_id = ss.to_node"
    " WHERE ss.segment_id IN ("
)
_SIGN_SQL = "SELECT sign_id, order_number, sign_code, sign_description FROM sign WHERE sign_id IN ("
_METER_SQL = "SELECT segment_id, side, rate_label, hour_rates FROM meter_rate WHERE segment_id IN ("


@dataclass(frozen=True)
class SignRef:
    """The raw sign text behind a verdict, always shown next to it (CLAUDE.md)."""

    sign_id: str
    order_number: str | None
    sign_code: str | None
    sign_description: str
    parse_method: str | None
    parse_confidence: float | None


@dataclass(frozen=True)
class SearchResult:
    """One ranked curb span. `money` is a decimal string so the API never floats money."""

    reg_seg_id: str
    geometry: dict[str, Any]
    verdict: Verdict
    reason: str
    caveats: list[str]
    walk_min: float
    money: str | None
    price_known: bool
    capacity_cars: int | None
    confidence: float
    signs: list[SignRef]
    charged_minutes: int = 0
    metered: bool = False
    score: float = 0.0
    rate_label: str | None = None
    # A human label for the span, e.g. "3 AVENUE, west side, E 85 ST -> E 86 ST".
    # None when the span has no centerline segment to name it from.
    street_name: str | None = None
    # NULL on a real span. A placeholder covering a centerline side with no
    # rules says why it is empty, so the grey state can tell a data gap
    # ('no_signs') from a matching gap ('unmatched_signs') -- a blank curb and
    # an unplaced sign need different words (SPEC §11, docs/VALIDATION.md §5).
    gap_kind: str | None = None


@dataclass(frozen=True)
class SearchCounts:
    """How much curb of each kind was in radius, counted before any cap."""

    legal: int = 0
    illegal: int = 0
    ambiguous: int = 0
    no_data: int = 0
    total: int = 0


@dataclass(frozen=True)
class SearchResults:
    """The ranked legal list, everything else for the map, and the true counts.

    `legal` is what the user reads, capped at `limit`. `others` is every other
    verdict in radius, capped separately at `map_limit`, so a legal-rich
    neighbourhood cannot push the red curb off the map (docs/VALIDATION.md U1).
    `counts` is measured before either cap, which is what makes the status line
    an honest statement about the query rather than about the response.
    """

    legal: list[SearchResult]
    others: list[SearchResult]
    counts: SearchCounts

    @property
    def all(self) -> list[SearchResult]:
        """Ranked legal first, then the rest — the order the API returns."""
        return [*self.legal, *self.others]


@dataclass
class _Candidate:
    reg_seg_id: str
    segment_id: str | None
    side: str | None
    geometry: dict[str, Any]
    capacity_cars: int | None
    snap_confidence: float
    sign_ids: list[str]
    walk_min: float
    gap_kind: str | None = None
    street_name: str | None = None


def search(
    conn: sqlite3.Connection,
    *,
    lon: float,
    lat: float,
    t1: datetime,
    t2: datetime,
    walk_minutes_max: float,
    weights: Weights | None = None,
    limit: int = DEFAULT_LIMIT,
    map_limit: int = DEFAULT_MAP_LIMIT,
    calendar: CalendarContext | None = None,
) -> SearchResults:
    """Rank the curb spans within `walk_minutes_max` of (lon, lat) for the window [t1, t2).

    Returns the ranked legal spans capped at `limit`, every other verdict in
    radius capped separately at `map_limit` (nearest first, so the cap drops
    the farthest curb rather than a whole verdict), and the counts of all four
    verdicts taken before either cap. The map colours illegal, ambiguous and
    no-data curb too (SPEC §11), and ranking them alongside the legal ones is
    what made them disappear (docs/VALIDATION.md U1).
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if map_limit < 1:
        raise ValueError("map_limit must be at least 1")
    map_limit = min(map_limit, MAX_MAP_LIMIT)
    resolved_weights = weights or Weights()
    resolved_calendar = calendar or load_calendar(conn)

    candidates = _candidates_in_radius(
        conn, lon=lon, lat=lat, radius_m=walk_radius_meters(walk_minutes_max)
    )
    if not candidates:
        return SearchResults(legal=[], others=[], counts=SearchCounts())

    _label_candidates(conn, candidates)
    stacks = _load_stacks(conn, [candidate.reg_seg_id for candidate in candidates])
    signs = _load_signs(conn, candidates, stacks)
    meters = _load_meter_rates(conn, candidates)

    results = []
    for candidate in candidates:
        verdict = evaluate_segment(stacks.get(candidate.reg_seg_id, []), t1, t2, resolved_calendar)
        results.append(
            _build_result(
                candidate,
                verdict,
                signs.get(candidate.reg_seg_id, []),
                meters.get((candidate.segment_id, candidate.side), []),
                resolved_weights,
            )
        )

    return _split_and_cap(results, limit=limit, map_limit=map_limit)


def _split_and_cap(results: list[SearchResult], *, limit: int, map_limit: int) -> SearchResults:
    legal = sorted(
        (result for result in results if result.verdict is Verdict.LEGAL),
        key=lambda result: (result.score, result.reg_seg_id),
    )
    others = [result for result in results if result.verdict is not Verdict.LEGAL]
    # Choose *which* others survive the cap by distance, so a dense band of one
    # verdict cannot crowd out another, then order the survivors for display.
    kept = sorted(others, key=lambda result: (result.walk_min, result.reg_seg_id))[:map_limit]
    kept.sort(key=lambda result: (_VERDICT_ORDER[result.verdict], result.score, result.reg_seg_id))
    return SearchResults(legal=legal[:limit], others=kept, counts=_count_verdicts(results))


def _count_verdicts(results: Sequence[SearchResult]) -> SearchCounts:
    tally = dict.fromkeys(Verdict, 0)
    for result in results:
        tally[result.verdict] += 1
    return SearchCounts(
        legal=tally[Verdict.LEGAL],
        illegal=tally[Verdict.ILLEGAL],
        ambiguous=tally[Verdict.AMBIGUOUS],
        no_data=tally[Verdict.NO_DATA],
        total=len(results),
    )


def load_calendar(
    conn: sqlite3.Connection,
    *,
    non_school_dates: Iterable[Any] | None = None,
    snow_emergency: bool = False,
) -> CalendarContext:
    """Build the calendar from `asp_suspension`. The table is one year of dates, so read it whole.

    A database with no calendar produces a context that says so, and every
    verdict resolved against it carries the caveat: without the calendar a
    holiday reads as an ordinary day and a street-cleaning ban that the city
    suspended still reads as in force (SPEC §11).
    """
    rows = [dict(row) for row in conn.execute(_ASP_SQL)]
    return CalendarContext.from_asp_rows(
        rows,
        non_school_dates=non_school_dates,
        snow_emergency=snow_emergency,
        calendar_missing=not rows or _meta_says_calendar_missing(conn),
    )


def calendar_is_missing(conn: sqlite3.Connection) -> bool:
    """Whether this database has no usable ASP and holiday calendar.

    Two signals meaning one thing: the ETL recorded that it found no calendar
    file, or `asp_suspension` is empty. `/api/health` reports `degraded` on
    either, because a sync that quietly dropped the calendar otherwise looks
    exactly like a good one.
    """
    if _meta_says_calendar_missing(conn):
        return True
    return conn.execute(_ASP_SQL).fetchone() is None


def _meta_says_calendar_missing(conn: sqlite3.Connection) -> bool:
    row = conn.execute(_CALENDAR_META_SQL).fetchone()
    if row is None or row[0] is None:
        return False
    return str(row[0]).strip().lower() not in ("", "0", "false", "no")


def _candidates_in_radius(
    conn: sqlite3.Connection, *, lon: float, lat: float, radius_m: float
) -> list[_Candidate]:
    lat_pad = radius_m / _M_PER_DEG_LAT
    lon_pad = radius_m / _meters_per_degree_lon(lat)
    has_gap_kind = _has_gap_kind(conn)
    columns = _CANDIDATE_COLUMNS + (", gap_kind" if has_gap_kind else "")
    rows = conn.execute(
        _CANDIDATE_SQL.format(columns=columns),
        (lon - lon_pad, lon + lon_pad, lat - lat_pad, lat + lat_pad),
    ).fetchall()

    origin = Point(0.0, 0.0)
    candidates: list[_Candidate] = []
    unreadable = 0
    for row in rows:
        measured = _measure(row["geom"], lon=lon, lat=lat, origin=origin)
        if measured is None:
            unreadable += 1
            continue
        geometry, distance_m = measured
        if distance_m > radius_m:
            continue
        candidates.append(
            _Candidate(
                reg_seg_id=str(row["reg_seg_id"]),
                segment_id=None if row["segment_id"] is None else str(row["segment_id"]),
                side=None if row["side"] is None else str(row["side"]),
                geometry=geometry,
                capacity_cars=None if row["capacity_cars"] is None else int(row["capacity_cars"]),
                snap_confidence=float(row["confidence"] or 0.0),
                sign_ids=_json_string_list(row["derived_from"]),
                walk_min=walk_minutes_for_meters(distance_m),
                gap_kind=_optional_str(row["gap_kind"]) if has_gap_kind else None,
            )
        )
    if unreadable:
        # One line for the whole query, not one per row: a truncated snapshot
        # can hold thousands of these and the point is the count, not each id.
        LOGGER.warning("skipped %d regulation_segment row(s) with unreadable geometry", unreadable)
    return candidates


def _measure(
    geom: Any, *, lon: float, lat: float, origin: Point
) -> tuple[dict[str, Any], float] | None:
    """The row's geometry and its distance in metres, or None when it cannot be read.

    The database is untrusted at read time (CLAUDE.md), and `geom` is the last
    column in this query that was still decoded unguarded: a truncated or
    hand-edited snapshot answered every search with a 500
    (docs/SECURITY.md residual 9). Dropping the row hides a stretch of curb the
    user asked about, which is why the count is logged rather than swallowed —
    but a search that answers about the rest of the neighbourhood is worth more
    than one that answers about none of it.
    """
    try:
        geometry = json.loads(str(geom))
        local = shape(_to_local_meters(geometry, lon0=lon, lat0=lat))
        return geometry, float(local.distance(origin))
    except (ValueError, TypeError, KeyError, IndexError, ShapelyError):
        return None


# CSCL writes street names in capitals ("3 AVENUE"); they are shown as stored,
# because a title-cased "3 Avenue" is our text, not DOT's.
_SIDE_WORDS = {"N": "north", "S": "south", "E": "east", "W": "west"}


def _label_candidates(conn: sqlite3.Connection, candidates: Sequence[_Candidate]) -> None:
    """Give every candidate a human label, in one query for the whole radius.

    The label is the centerline's own street name, the side, and the cross
    streets at the two ends of the chain, which are the names carried by the
    nodes the segment runs between: "3 AVENUE, west side, E 85 ST → E 86 ST".
    Without it a result card can only show the opaque `reg_seg_id`, and the
    frontend used to buy the name back with one `/api/segment` request per card.
    """
    between: dict[str, str] = {}
    segment_ids = sorted({c.segment_id for c in candidates if c.segment_id is not None})
    for chunk in _chunked(segment_ids):
        for row in conn.execute(_STREET_SQL + placeholders(len(chunk)) + ")", chunk):
            street_name = _optional_str(row["street_name"])
            if street_name:
                between[str(row["segment_id"])] = street_name + _cross_street_phrase(
                    street_name, row["from_names"], row["to_names"]
                )
    for candidate in candidates:
        label = between.get(candidate.segment_id or "")
        if label is None:
            continue
        side = _SIDE_WORDS.get(candidate.side or "")
        candidate.street_name = _with_side(label, side) if side else label


def _with_side(label: str, side: str) -> str:
    """Insert the side after the street name, before the cross streets."""
    street_name, separator, rest = label.partition(", ")
    return f"{street_name}, {side} side{separator}{rest}"


def _cross_street_phrase(street_name: str, from_names: Any, to_names: Any) -> str:
    """ ", E 85 ST → E 86 ST", ", at E 85 ST", or "" when neither node names one."""
    start = _cross_street(from_names, street_name)
    end = _cross_street(to_names, street_name)
    if start and end:
        return f", {start} → {end}"
    if start or end:
        return f", at {start or end}"
    return ""


def _cross_street(names: Any, street_name: str) -> str | None:
    """The first name on the node that is not the street the span runs along.

    Compared on collapsed whitespace: CSCL writes the same street as `E 85 ST`
    on the node and `E  85 ST` on the segment often enough that an exact match
    would label a corner as its own cross street.
    """
    own = _collapse(street_name)
    for name in _json_string_list(names):
        if name and _collapse(name) != own:
            return name
    return None


def _collapse(name: str) -> str:
    return " ".join(name.split()).casefold()


def _load_stacks(
    conn: sqlite3.Connection, reg_seg_ids: Sequence[str]
) -> dict[str, list[RegulationWithMeta]]:
    stacks: dict[str, list[RegulationWithMeta]] = {}
    for chunk in _chunked(reg_seg_ids):
        rows = conn.execute(_REGULATION_SQL + placeholders(len(chunk)) + ")", chunk).fetchall()
        for row in rows:
            reg_seg_id = str(row["reg_seg_id"])
            stacks.setdefault(reg_seg_id, []).append(
                RegulationWithMeta(
                    regulation=regulation_from_row(row),
                    parse_method=ParseMethod(str(row["parse_method"])),
                    parse_confidence=float(row["parse_confidence"]),
                    reg_seg_id=reg_seg_id,
                    raw_sign_description=str(row["raw_sign_description"]),
                )
            )
    return stacks


def _load_signs(
    conn: sqlite3.Connection,
    candidates: Sequence[_Candidate],
    stacks: dict[str, list[RegulationWithMeta]],
) -> dict[str, list[SignRef]]:
    """Fetch the sign rows behind each segment, with the parse metadata of their rules.

    A sign is tied to its rules by `raw_sign_description`: the parser reads each
    distinct description once (docs/ARCHITECTURE.md step 6), so the description
    is what a `regulation` row and a `sign` row have in common.
    """
    wanted = {sign_id for candidate in candidates for sign_id in candidate.sign_ids}
    rows_by_id: dict[str, sqlite3.Row] = {}
    for chunk in _chunked(sorted(wanted)):
        for row in conn.execute(_SIGN_SQL + placeholders(len(chunk)) + ")", chunk):
            rows_by_id[str(row["sign_id"])] = row

    signs: dict[str, list[SignRef]] = {}
    for candidate in candidates:
        parses = _parse_metadata(stacks.get(candidate.reg_seg_id, []))
        refs = []
        for sign_id in candidate.sign_ids:
            row = rows_by_id.get(sign_id)
            if row is None:
                continue
            description = str(row["sign_description"])
            method, confidence = parses.get(description, (None, None))
            refs.append(
                SignRef(
                    sign_id=sign_id,
                    order_number=_optional_str(row["order_number"]),
                    sign_code=_optional_str(row["sign_code"]),
                    sign_description=description,
                    parse_method=method,
                    parse_confidence=confidence,
                )
            )
        signs[candidate.reg_seg_id] = refs
    return signs


def _parse_metadata(
    stack: Sequence[RegulationWithMeta],
) -> dict[str, tuple[str | None, float | None]]:
    parses: dict[str, tuple[str | None, float | None]] = {}
    for item in stack:
        existing = parses.get(item.raw_sign_description)
        if existing is None or (existing[1] is not None and item.parse_confidence < existing[1]):
            parses[item.raw_sign_description] = (item.parse_method.value, item.parse_confidence)
    return parses


def _load_meter_rates(
    conn: sqlite3.Connection, candidates: Sequence[_Candidate]
) -> dict[tuple[str | None, str | None], list[tuple[str | None, list[Decimal]]]]:
    """Rates keyed by (segment_id, side). SPEC §13.1: a blockface may carry more than one zone."""
    segment_ids = sorted({c.segment_id for c in candidates if c.segment_id is not None})
    rates: dict[tuple[str | None, str | None], list[tuple[str | None, list[Decimal]]]] = {}
    for chunk in _chunked(segment_ids):
        for row in conn.execute(_METER_SQL + placeholders(len(chunk)) + ")", chunk):
            key = (
                str(row["segment_id"]),
                None if row["side"] is None else str(row["side"]),
            )
            hour_rates = _decimal_list(row["hour_rates"])
            if hour_rates:
                rates.setdefault(key, []).append((_optional_str(row["rate_label"]), hour_rates))
    return rates


def _build_result(
    candidate: _Candidate,
    verdict: SegmentVerdict,
    signs: list[SignRef],
    rates: list[tuple[str | None, list[Decimal]]],
    weights: Weights,
) -> SearchResult:
    caveats = list(verdict.caveats)
    money, price_known, rate_label = _price(verdict, rates, caveats)
    risk = risk_dollars(verdict.confidence, candidate.snap_confidence)
    score = total_cost(candidate.walk_min, money or Decimal("0.00"), risk, weights)
    return SearchResult(
        reg_seg_id=candidate.reg_seg_id,
        geometry=candidate.geometry,
        verdict=verdict.verdict,
        reason=verdict.reason,
        caveats=caveats,
        walk_min=candidate.walk_min,
        money=None if money is None else str(money),
        price_known=price_known,
        capacity_cars=candidate.capacity_cars,
        confidence=min(verdict.confidence, candidate.snap_confidence),
        signs=signs,
        charged_minutes=verdict.charged_minutes,
        metered=verdict.metered,
        score=score,
        rate_label=rate_label,
        street_name=candidate.street_name,
        gap_kind=candidate.gap_kind,
    )


def _price(
    verdict: SegmentVerdict,
    rates: list[tuple[str | None, list[Decimal]]],
    caveats: list[str],
) -> tuple[Decimal | None, bool, str | None]:
    """Meter cost for the window, or None when we have no rate. Never fabricate one (SPEC §13.1d)."""
    if not verdict.metered or verdict.charged_minutes == 0:
        return (Decimal("0.00"), True, None)
    if not rates:
        caveats.append("metered, but no published rate for this blockface")
        return (None, False, None)

    priced = [
        (meter_price(hour_rates, verdict.charged_minutes), label) for label, hour_rates in rates
    ]
    highest = max(priced, key=lambda item: item[0])
    if len({price for price, _ in priced}) > 1:
        # SPEC §13.1(b): a few blockfaces carry several zones. Quote the dearest
        # and tell the user to confirm rather than guessing which one applies.
        caveats.append("more than one meter zone covers this blockface; confirm at the meter")
        return (highest[0], False, highest[1])
    return (highest[0], True, highest[1])


_VERDICT_ORDER: dict[Verdict, int] = {
    Verdict.LEGAL: 0,
    Verdict.AMBIGUOUS: 1,
    Verdict.ILLEGAL: 2,
    Verdict.NO_DATA: 3,
}


def _has_gap_kind(conn: sqlite3.Connection) -> bool:
    """Whether this database has `regulation_segment.gap_kind`.

    One `PRAGMA table_info` per search, cheaper than the bbox query beside it.
    The column arrived after the first databases were built, and a snapshot from
    before it is still usable — it just cannot say *why* a stretch is empty.
    """
    return any(str(row[1]) == "gap_kind" for row in conn.execute(_GAP_KIND_PRAGMA))


def _meters_per_degree_lon(lat: float) -> float:
    return _M_PER_DEG_LON_AT_EQUATOR * math.cos(math.radians(lat))


def _to_local_meters(geometry: dict[str, Any], *, lon0: float, lat0: float) -> dict[str, Any]:
    scale_lon = _meters_per_degree_lon(lat0)

    def convert(node: Any) -> Any:
        if isinstance(node, (list, tuple)) and node and isinstance(node[0], (int, float)):
            return [(float(node[0]) - lon0) * scale_lon, (float(node[1]) - lat0) * _M_PER_DEG_LAT]
        return [convert(child) for child in node]

    return {"type": geometry["type"], "coordinates": convert(geometry["coordinates"])}


def _chunked(values: Sequence[str], size: int = _ID_CHUNK) -> Iterator[list[str]]:
    for start in range(0, len(values), size):
        yield list(values[start : start + size])


def _json_string_list(value: Any) -> list[str]:
    """Read a JSON list column, treating anything unreadable as empty.

    The database is untrusted at read time (CLAUDE.md), including columns the
    ETL is supposed to have written as JSON. A `derived_from` or `hour_rates`
    cell that is not a JSON list means "no sign ids" and "no known rate", which
    the caller already handles; letting the decode error out would turn a
    half-built snapshot into a 500 on every search. Matches
    `api.routes._json_list`.
    """
    if value is None:
        return []
    try:
        parsed = json.loads(str(value))
    except ValueError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]


def _decimal_list(value: Any) -> list[Decimal]:
    """Hourly rates are stored as JSON strings so money never round-trips through a float."""
    rates = []
    for item in _json_string_list(value):
        try:
            rates.append(Decimal(item))
        except InvalidOperation:
            return []
    return rates


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)
