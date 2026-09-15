"""Radius query over regulation segments, evaluated and ranked.

The only module in the engine that touches SQLite. Shape of the work: bbox
prefilter in SQL, exact distance in Python with shapely, one batched query per
kind of related row (never one per candidate), then verdict, price, and rank.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

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

_CANDIDATE_SQL = (
    "SELECT reg_seg_id, segment_id, side, geom, length_ft, capacity_cars, confidence,"
    " derived_from FROM regulation_segment"
    " WHERE max_lon >= ? AND min_lon <= ? AND max_lat >= ? AND min_lat <= ?"
)
_ASP_SQL = "SELECT date, is_major_legal_holiday, meters_suspended, label FROM asp_suspension"
_REGULATION_SQL = (
    "SELECT reg_id, reg_seg_id, action, permitted, vehicle_class, exclusive, days_mask,"
    " time_from, time_to, metered, max_duration_min, flags, effective_from, effective_to,"
    " arrow, raw_sign_description, parse_method, parse_confidence FROM regulation"
    " WHERE reg_seg_id IN ("
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
    """Build the calendar from `asp_suspension`. The table is one year of dates, so read it whole."""
    rows = [dict(row) for row in conn.execute(_ASP_SQL)]
    return CalendarContext.from_asp_rows(
        rows, non_school_dates=non_school_dates, snow_emergency=snow_emergency
    )


def _candidates_in_radius(
    conn: sqlite3.Connection, *, lon: float, lat: float, radius_m: float
) -> list[_Candidate]:
    lat_pad = radius_m / _M_PER_DEG_LAT
    lon_pad = radius_m / _meters_per_degree_lon(lat)
    rows = conn.execute(
        _CANDIDATE_SQL, (lon - lon_pad, lon + lon_pad, lat - lat_pad, lat + lat_pad)
    ).fetchall()

    origin = Point(0.0, 0.0)
    candidates: list[_Candidate] = []
    for row in rows:
        geometry = json.loads(str(row["geom"]))
        local = shape(_to_local_meters(geometry, lon0=lon, lat0=lat))
        distance_m = float(local.distance(origin))
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
            )
        )
    return candidates


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
