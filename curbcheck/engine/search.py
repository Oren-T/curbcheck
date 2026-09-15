"""Radius query over regulation segments, evaluated and ranked.

Shape of the work: bbox prefilter in SQL, exact distance in Python with
shapely, one batched query per kind of related row (never one per candidate),
then verdict, price, and rank.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from shapely.errors import ShapelyError
from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry

from curbcheck.db import json_string_list, placeholders, regulation_from_row
from curbcheck.engine.cost import (
    Weights,
    meter_price,
    risk_dollars,
    total_cost,
    walk_minutes_for_meters,
    walk_radius_meters,
)
from curbcheck.engine.geo import degree_padding, measure
from curbcheck.engine.labels import between_phrase, single_spaced, span_label
from curbcheck.engine.resolve import (
    RegulationWithMeta,
    SegmentVerdict,
    Verdict,
    VerdictBasis,
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
# laptop, and the whole of Manhattan holds 34,016 spans, so the map cap needs a
# ceiling a request cannot raise.
MAX_MAP_LIMIT = 5000

# SQLite's default SQLITE_MAX_VARIABLE_NUMBER is 32766 in modern builds, but
# older builds use 999. 400 ids per query is comfortably under both.
_ID_CHUNK = 400

# Two legal spans whose cost differs by less than this are the same price as
# far as the ranking is concerned: half a minute of walking under the default
# weights, which is under the noise in a straight-line walk estimate. Inside a
# band a posted permission outranks mere absence of a rule (decision D27).
COST_TIE_BAND = 0.5

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
    # The three terms `score` is made of, so a client can re-rank under its own
    # weights without a round trip: walk_min above, `money_value` here as the
    # numeric twin of the `money` string, and `risk` in the same dollars.
    money_value: float | None = None
    risk: float = 0.0
    # Why a LEGAL span is legal, and whether the confidence number means
    # anything on it. Both are None/False on the states where a percentage
    # would be the app sounding sure about having found nothing (UX audit P0-1).
    basis: VerdictBasis | None = None
    confidence_shown: bool = True
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

    verdicts = {
        candidate.reg_seg_id: evaluate_segment(
            stacks.get(candidate.reg_seg_id, []),
            t1,
            t2,
            resolved_calendar,
            gap_kind=candidate.gap_kind,
        )
        for candidate in candidates
    }
    _demote_contested_spans(candidates, verdicts)

    results = [
        _build_result(
            candidate,
            verdicts[candidate.reg_seg_id],
            signs.get(candidate.reg_seg_id, []),
            meters.get((candidate.segment_id, candidate.side), []),
            resolved_weights,
        )
        for candidate in candidates
    ]
    return _split_and_cap(results, limit=limit, map_limit=map_limit)


# Curb has to overlap by more than this before two spans are treated as covering
# the same stretch. ~1 ft in degrees, so a shared endpoint does not count.
_OVERLAP_EPS_DEG = 3e-6

CONTESTED_REASON = (
    "another sign on this block prohibits parking over part of this stretch; the signs conflict"
)


def _demote_contested_spans(
    candidates: Sequence[_Candidate], verdicts: dict[str, SegmentVerdict]
) -> None:
    """Turn a legal span that a prohibition also covers into AMBIGUOUS, in place.

    Since docs/DECISIONS.md D25 and D26 this should never fire: `etl.segments`
    stacks every span that reaches one centerline segment-side, from whatever
    chain, and cuts them at every boundary, so a prohibition and a permission
    that overlap arrive as one row with both rules in its stack and `resolve`
    settles them most-restrictive-wins per foot of curb. There are zero such
    pairs on the 2026-09-15 database (docs/VALIDATION.md §11). It is kept as
    defence in depth against a database built before them — `curbcheck.sqlite.prev`
    is one, and the engine will open whatever file it is pointed at — where a
    permissive span reaching back over a `NO STANDING ANYTIME` span would read
    LEGAL over curb that is not, which is SPEC §8.6's P0 defect.

    Its key is `(segment_id, side)`, so it does not reach the one residue left:
    the RIVERSIDE DR viaduct, which CSCL draws twice under two names and two
    `physicalid`s. `scripts/validation_regress.py --overlaps` compares geometry
    and does catch it.

    An overlap here means the ETL's invariant has broken, so it is logged: the
    demotion is honest (SPEC §11's "the signs conflict") but it loses the legal
    remainder that D25 keeps.
    """
    by_face: dict[tuple[str | None, str | None], list[_Candidate]] = {}
    for candidate in candidates:
        by_face.setdefault((candidate.segment_id, candidate.side), []).append(candidate)

    contested = 0
    for face in by_face.values():
        if len(face) < 2:
            continue
        banned = [c for c in face if verdicts[c.reg_seg_id].verdict is Verdict.ILLEGAL]
        allowed = [c for c in face if verdicts[c.reg_seg_id].verdict is Verdict.LEGAL]
        if not banned or not allowed:
            continue
        shapes = {c.reg_seg_id: _line_or_none(c.geometry) for c in face}
        for candidate in allowed:
            line = shapes[candidate.reg_seg_id]
            if line is None:
                continue
            if any(_overlaps(line, shapes[other.reg_seg_id]) for other in banned):
                contested += 1
                verdicts[candidate.reg_seg_id] = replace(
                    verdicts[candidate.reg_seg_id],
                    verdict=Verdict.AMBIGUOUS,
                    reason=CONTESTED_REASON,
                )
    if contested:
        LOGGER.warning(
            "demoted %d overlapping legal span(s) to ambiguous; this database predates"
            " docs/DECISIONS.md D25 and should be rebuilt with `curbcheck sync`",
            contested,
        )


def _line_or_none(geometry: dict[str, Any]) -> BaseGeometry | None:
    try:
        return shape(geometry)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, ShapelyError):
        return None


def _overlaps(line: BaseGeometry, other: BaseGeometry | None) -> bool:
    if other is None:
        return False
    try:
        return bool(line.intersection(other).length > _OVERLAP_EPS_DEG)
    except ShapelyError:
        return False


def _split_and_cap(results: list[SearchResult], *, limit: int, map_limit: int) -> SearchResults:
    legal = _rank_legal([result for result in results if result.verdict is Verdict.LEGAL])
    others = [result for result in results if result.verdict is not Verdict.LEGAL]
    # Choose *which* others survive the cap by distance, so a dense band of one
    # verdict cannot crowd out another, then order the survivors for display.
    kept = sorted(others, key=lambda result: (result.walk_min, result.reg_seg_id))[:map_limit]
    kept.sort(key=lambda result: (_VERDICT_ORDER[result.verdict], result.score, result.reg_seg_id))
    return SearchResults(legal=legal[:limit], others=kept, counts=_count_verdicts(results))


def _rank_legal(legal: list[SearchResult]) -> list[SearchResult]:
    """Cheapest first, but a posted permission ahead of mere absence at the same cost.

    The ranking stays a cost ranking: only spans whose cost the user could not
    tell apart are reordered, and only to put a span with a sign to read above
    one where nothing is posted (decision D27, UX audit P0-1).
    """
    by_cost = sorted(legal, key=lambda result: (result.score, result.reg_seg_id))
    ranked: list[SearchResult] = []
    for band in _cost_bands(by_cost):
        ranked.extend(
            sorted(band, key=lambda result: (_BASIS_ORDER.get(result.basis, 1), result.score))
        )
    return ranked


def _cost_bands(by_cost: Sequence[SearchResult]) -> Iterator[list[SearchResult]]:
    """Runs of results within COST_TIE_BAND of the *first* result in the run.

    Anchoring each band on its own first element rather than on the previous
    one is what keeps a dense list from chaining into one band: 600 spans a
    tenth of a point apart would otherwise all tie, and the cost ranking would
    stop meaning anything.
    """
    band: list[SearchResult] = []
    for result in by_cost:
        if band and result.score - band[0].score > COST_TIE_BAND:
            yield band
            band = []
        band.append(result)
    if band:
        yield band


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
    lon_pad, lat_pad = degree_padding(lat, radius_m)
    gap_kind_column = has_gap_kind(conn)
    columns = _CANDIDATE_COLUMNS + (", gap_kind" if gap_kind_column else "")
    rows = conn.execute(
        _CANDIDATE_SQL.format(columns=columns),
        (lon - lon_pad, lon + lon_pad, lat - lat_pad, lat + lat_pad),
    ).fetchall()

    origin = Point(0.0, 0.0)
    candidates: list[_Candidate] = []
    unreadable = 0
    for row in rows:
        measured = measure(row["geom"], lon=lon, lat=lat, origin=origin)
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
                sign_ids=json_string_list(row["derived_from"]),
                walk_min=walk_minutes_for_meters(distance_m),
                gap_kind=_optional_str(row["gap_kind"]) if gap_kind_column else None,
            )
        )
    if unreadable:
        # One line for the whole query, not one per row: a truncated snapshot
        # can hold thousands of these and the point is the count, not each id.
        LOGGER.warning("skipped %d regulation_segment row(s) with unreadable geometry", unreadable)
    return candidates


def _label_candidates(conn: sqlite3.Connection, candidates: Sequence[_Candidate]) -> None:
    """Give every candidate a human label, in one query for the whole radius.

    The label is the centerline's own street name, the side, and the cross
    streets at the two ends of the chain, which are the names carried by the
    nodes the segment runs between: "3 AVENUE, west side, E 85 ST → E 86 ST".
    Without it a result card can only show the opaque `reg_seg_id`, and the
    frontend used to buy the name back with one `/api/segment` request per card.
    """
    labels: dict[str, tuple[str, str | None]] = {}
    segment_ids = sorted({c.segment_id for c in candidates if c.segment_id is not None})
    for chunk in _chunked(segment_ids):
        for row in conn.execute(_STREET_SQL + placeholders(len(chunk)) + ")", chunk):
            street_name = single_spaced(_optional_str(row["street_name"]) or "")
            if street_name:
                labels[str(row["segment_id"])] = (
                    street_name,
                    between_phrase(street_name, row["from_names"], row["to_names"]),
                )
    for candidate in candidates:
        named = labels.get(candidate.segment_id or "")
        if named is None:
            continue
        candidate.street_name = span_label(named[0], candidate.side, named[1])


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
    money, price_known, rate_label = _price(
        verdict, rates, caveats, placeholder=candidate.gap_kind is not None
    )
    risk = risk_dollars(verdict.confidence, candidate.snap_confidence)
    score = total_cost(candidate.walk_min, money or Decimal("0.00"), risk, weights)
    basis = verdict.basis
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
        money_value=None if money is None else float(money),
        risk=float(risk),
        basis=basis,
        confidence_shown=_confidence_is_meaningful(verdict.verdict, basis),
        rate_label=rate_label,
        street_name=candidate.street_name,
        gap_kind=candidate.gap_kind,
    )


def _confidence_is_meaningful(verdict: Verdict, basis: VerdictBasis | None) -> bool:
    """Whether the confidence number says anything the user should be shown.

    On NO_DATA it is 0 next to a reason that says nothing was read, and on an
    absence-based LEGAL verdict it is the confidence of an empty stack, i.e. 1.
    Both printed as a percentage read as certainty about parking rather than as
    certainty about a reading (UX audit P0-1, P2-1).
    """
    return verdict is not Verdict.NO_DATA and basis is not VerdictBasis.ABSENCE


def _price(
    verdict: SegmentVerdict,
    rates: list[tuple[str | None, list[Decimal]]],
    caveats: list[str],
    *,
    placeholder: bool,
) -> tuple[Decimal | None, bool, str | None]:
    """Meter cost for the window, or None when we have no rate. Never fabricate one (SPEC §13.1d).

    Curb with no rules is unpriced, not free: "$0.00" and "no meter" were being
    printed on grey spans the app knows nothing about, which is a claim the data
    never made (UX audit P0-5, SPEC §11).
    """
    if placeholder or verdict.verdict is Verdict.NO_DATA:
        return (None, False, None)
    if not verdict.metered or verdict.charged_minutes == 0:
        return (Decimal("0.00"), True, None)
    if not rates:
        caveats.append("Metered, but no published rate for this blockface.")
        return (None, False, None)

    priced = [
        (meter_price(hour_rates, verdict.charged_minutes), label) for label, hour_rates in rates
    ]
    highest = max(priced, key=lambda item: item[0])
    if len({price for price, _ in priced}) > 1:
        # SPEC §13.1(b): a few blockfaces carry several zones. Quote the dearest
        # and tell the user to confirm rather than guessing which one applies.
        caveats.append("More than one meter zone covers this blockface; confirm at the meter.")
        return (highest[0], False, highest[1])
    return (highest[0], True, highest[1])


_BASIS_ORDER: dict[VerdictBasis | None, int] = {VerdictBasis.POSTED: 0, VerdictBasis.ABSENCE: 1}

_VERDICT_ORDER: dict[Verdict, int] = {
    Verdict.LEGAL: 0,
    Verdict.AMBIGUOUS: 1,
    Verdict.ILLEGAL: 2,
    Verdict.NO_DATA: 3,
}


def has_gap_kind(conn: sqlite3.Connection) -> bool:
    """Whether this database has `regulation_segment.gap_kind`.

    One `PRAGMA table_info` per search, cheaper than the bbox query beside it.
    The column arrived after the first databases were built, and a snapshot from
    before it is still usable — it just cannot say *why* a stretch is empty.
    """
    return any(str(row[1]) == "gap_kind" for row in conn.execute(_GAP_KIND_PRAGMA))


def _chunked(values: Sequence[str], size: int = _ID_CHUNK) -> Iterator[list[str]]:
    for start in range(0, len(values), size):
        yield list(values[start : start + size])


def _decimal_list(value: Any) -> list[Decimal]:
    """Hourly rates are stored as JSON strings so money never round-trips through a float."""
    rates = []
    for item in json_string_list(value):
        try:
            rates.append(Decimal(item))
        except InvalidOperation:
            return []
    return rates


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)
