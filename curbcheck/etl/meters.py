"""ParkNYC blockface rates joined onto the centerline (SPEC §13.1, docs/DATA.md §3).

The join is by normalized street names — ParkNYC names the same `(on, from, to,
side)` blockface the sign data does, in Title Case — with the published geometry
used only as a check: a blockface whose midpoint lands far from the chain we
matched it to keeps its rate but loses confidence. Where a segment the signs say
is metered has no ParkNYC row at all, the citywide rate-zone polygon supplies a
zone-level rate at lower confidence (SPEC §13.1c). Nothing here ever invents a
price: an unreadable rate string produces no rate, and the engine then reports
`price_known=false` (SPEC §13.1d).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shapely.geometry import LineString, MultiLineString, Point, shape
from shapely.ops import linemerge

from curbcheck.etl import fetch
from curbcheck.etl.snap import name_quality
from curbcheck.etl.streets import BlockLookup, StreetGraph, to_feet

LOGGER = logging.getLogger(__name__)

# docs/DATA.md §3.1: the borough is spelled 'Manhattan', 'MANHATTAN' and
# 'manhattan' in the same export, so the filter has to fold case.
MANHATTAN = "manhattan"

# ParkNYC writes absent values as the literal string, not as a null or an
# omitted key (docs/DATA.md §3.1).
NOT_AVAILABLE = "N/A"

# Half a long Manhattan block. A blockface whose midpoint is further than this
# from the midpoint of the chain we matched it to is a name collision, not a
# rounding difference, so the row keeps its rate but is no longer trusted.
GEOMETRY_CHECK_FT = 150.0
GEOMETRY_MISMATCH_PENALTY = 0.5

# A zone polygon covers whole neighbourhoods, so it says what a meter here
# charges but not that this blockface has one. Below
# `engine.resolve.AMBIGUITY_THRESHOLD`, which is what we want for a guess.
RATE_ZONE_CONFIDENCE = 0.6

SOURCE_PARKNYC = "parknyc"
SOURCE_RATE_ZONE = "rate_zone"


# "$5.00 1st Hour / $8.25 2nd Hour / $5.00 Add'l Hours", "$1.50 per Hour".
# Anything else (ParkNYC also writes "$7.00 per 30 Minutes") is left unread.
_RATE_TERM = re.compile(
    r"\$(\d+(?:\.\d{1,2})?)\s*(?:(per)\s+Hour|(\d)(?:st|nd|rd|th)\s+Hour|(Add'?l)\s+Hours?)",
    re.IGNORECASE,
)

# "2 Hours", "30 Minutes", "1 Hour, 2 Hours (Mon-Fri after 4PM, Sat)".
_SESSION_TERM = re.compile(r"(\d+)\s*(Hours?|Minutes?)", re.IGNORECASE)

# The zone polygons put both vehicle classes in one string, with or without the
# colon: "Zone M2 - Commercial Vehicles: $6.00 ..., All Vehicles: $5.00 ..." and
# "Zone 2 - All Vehicles $2.00 1st Hour/ $3.00 2nd Hour".
_ZONE_ALL = re.compile(r"All Vehicles:?\s*(.*?)(?:,\s*Commercial Vehicles|$)", re.IGNORECASE | re.S)
_ZONE_COMMERCIAL = re.compile(
    r"Commercial Vehicles:?\s*(.*?)(?:,\s*All Vehicles|$)", re.IGNORECASE | re.S
)


@dataclass(frozen=True)
class MeterRate:
    """One `meter_rate` row: what a meter on this centerline segment and side charges."""

    blockface_id: str
    segment_id: str | None
    side: str | None
    rate_label: str | None
    hour_rates: tuple[str, ...]
    """Progressive hourly rates as decimal strings, first hour first (SPEC §13.2)."""
    commercial_hour_rates: tuple[str, ...]
    max_session_min: int | None
    source: str
    confidence: float
    geometry: dict[str, Any] | None = None
    bbox: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class MeterReport:
    """Join outcome, for `sync_meta`. SPEC §13.1 names the three cases to count.

    `matched_one_to_one` and `many_zones_per_blockface` are counted over
    `(segment_id, side)` keys rather than over ParkNYC rows, because that key is
    what the engine looks a rate up by and therefore where "several zones cover
    one blockface" actually shows up as a price range.
    """

    source_rows: int
    manhattan_rows: int
    matched: int
    matched_one_to_one: int
    many_zones_per_blockface: int
    unmatched: int
    unmatched_reasons: dict[str, int]
    geometry_mismatches: int
    unreadable_rate_strings: int
    rows_written: int
    rate_zone_fallbacks: int
    metered_segments: int
    metered_segments_without_rate: int


def side_code(value: str | None) -> str | None:
    """ParkNYC's side letter, folded to the N/S/E/W the sign data and `snap` use.

    One Manhattan row publishes a lowercase 's' (docs/DATA.md §3.1).
    """
    text = (value or "").strip().upper()
    return text[:1] if text[:1] in {"N", "S", "E", "W"} else None


def text_or_none(value: str | None) -> str | None:
    """A ParkNYC cell as text, with the literal "N/A" read as a null."""
    text = (value or "").strip()
    return None if not text or text == NOT_AVAILABLE else text


def parse_hour_rates(value: str | None) -> tuple[str, ...]:
    """Progressive hourly rates from a ParkNYC or rate-zone price string.

    Returns decimal strings in hour order, so `engine.cost.meter_price` bills
    the first hour at `[0]` and anything past the last listed hour at `[-1]`.
    Returns empty for a string whose shape we do not recognize: a wrong price is
    worse than no price (SPEC §13.1d).
    """
    text = text_or_none(value)
    if text is None:
        return ()
    rates: list[str] = []
    for match in _RATE_TERM.finditer(text):
        amount, per_hour, ordinal, additional = match.groups()
        if per_hour:
            # "$1.50 per Hour" states one flat rate; anything beside it would
            # contradict it, so a second term means we have misread the string.
            return (amount,) if not rates else ()
        if ordinal is not None and int(ordinal) != len(rates) + 1:
            return ()
        if additional and not rates:
            return ()
        rates.append(amount)
    return tuple(rates)


def parse_max_session_min(value: str | None) -> int | None:
    """Longest session the meter sells, in minutes.

    A few rows state two limits for different times of day ("1 Hour, 2 Hours
    (Mon-Fri after 4PM, Sat)"). We keep the shortest: it is the limit that can
    only cost the user a spot rather than a ticket.
    """
    text = text_or_none(value)
    if text is None:
        return None
    minutes = [
        int(amount) * (60 if unit.lower().startswith("hour") else 1)
        for amount, unit in _SESSION_TERM.findall(text)
    ]
    return min(minutes) if minutes else None


def load_manhattan_blockfaces(raw_dir: Path) -> tuple[list[dict[str, Any]], int]:
    """Every ParkNYC blockface in Manhattan, with the citywide row count beside it."""
    rows = fetch.load_rows("parknyc_blockfaces", raw_dir)
    manhattan = [row for row in rows if str(row.get("borough") or "").strip().lower() == MANHATTAN]
    return (manhattan, len(rows))


def resolve_meter_rates(
    raw_dir: Path,
    graph: StreetGraph,
    *,
    metered_segments: Sequence[tuple[str, str | None, str | None, str]] = (),
) -> tuple[list[MeterRate], MeterReport]:
    """Every `meter_rate` row for this snapshot, plus the join statistics.

    `metered_segments` is `(reg_seg_id, segment_id, side, geojson)` for each
    regulation segment whose signs say it is metered; those are what the
    rate-zone fallback and the "metered but unpriced" count are computed over.

    "Priced" means a *passenger* rate: 620 of the Manhattan blockfaces are
    commercial-only and publish no `all_vehi_2`, so a row of theirs does not
    answer the question this tool asks and the zone polygon still applies.
    """
    blockfaces, source_rows = load_manhattan_blockfaces(raw_dir)
    rates: list[MeterRate] = []
    counts = _Counts()
    for row in blockfaces:
        rates.extend(_rates_for_blockface(row, graph, counts))

    priced_keys = {(rate.segment_id, rate.side) for rate in rates if rate.hour_rates}
    fallbacks = _rate_zone_fallbacks(raw_dir, metered_segments, priced_keys)
    rates.extend(fallbacks)

    covered = priced_keys | {(rate.segment_id, rate.side) for rate in fallbacks if rate.hour_rates}
    unpriced = sum(
        1 for _, segment_id, side, _ in metered_segments if (segment_id, side) not in covered
    )
    per_key: dict[tuple[str | None, str | None], set[tuple[str, ...]]] = {}
    for rate in rates:
        if rate.source == SOURCE_PARKNYC and rate.hour_rates:
            per_key.setdefault((rate.segment_id, rate.side), set()).add(rate.hour_rates)

    report = MeterReport(
        source_rows=source_rows,
        manhattan_rows=len(blockfaces),
        matched=counts.matched,
        matched_one_to_one=sum(1 for prices in per_key.values() if len(prices) == 1),
        many_zones_per_blockface=sum(1 for prices in per_key.values() if len(prices) > 1),
        unmatched=counts.unmatched,
        unmatched_reasons=counts.unmatched_reasons,
        geometry_mismatches=counts.geometry_mismatches,
        unreadable_rate_strings=counts.unreadable_rates,
        rows_written=len(rates),
        rate_zone_fallbacks=len(fallbacks),
        metered_segments=len(metered_segments),
        metered_segments_without_rate=unpriced,
    )
    LOGGER.info(
        "meters.join blockfaces=%d matched=%d unmatched=%d rows=%d zone_fallback=%d unpriced=%d",
        report.manhattan_rows,
        report.matched,
        report.unmatched,
        report.rows_written,
        report.rate_zone_fallbacks,
        report.metered_segments_without_rate,
    )
    return (rates, report)


@dataclass
class _Counts:
    matched: int = 0
    unmatched: int = 0
    geometry_mismatches: int = 0
    unreadable_rates: int = 0
    unmatched_reasons: dict[str, int] = field(default_factory=dict)

    def miss(self, reason: str) -> None:
        self.unmatched += 1
        self.unmatched_reasons[reason] = self.unmatched_reasons.get(reason, 0) + 1


def _rates_for_blockface(
    row: Mapping[str, Any], graph: StreetGraph, counts: _Counts
) -> list[MeterRate]:
    """The rows one ParkNYC blockface contributes, one per centerline segment it covers.

    A DOT blockface can span several centerline segments, and
    `regulation_segment.segment_id` is whichever of them covers that span's
    midpoint, so a single row filed under the chain's first segment would be
    invisible to the engine for spans further along the chain.
    """
    side = side_code(row.get("side_of_st"))
    if side is None:
        counts.miss("side_not_nsew")
        return []
    # The graph normalizes and aliases the names itself, and the confidence
    # below reads which of the three it needed, so pass ParkNYC's Title Case
    # through untouched.
    lookup = graph.find_block_detail(
        str(row.get("on_street") or ""),
        str(row.get("from_stree") or ""),
        str(row.get("to_street") or ""),
    )
    if lookup.match is None:
        counts.miss(lookup.reason)
        return []
    counts.matched += 1

    hour_rates = parse_hour_rates(row.get("all_vehi_2"))
    commercial = parse_hour_rates(row.get("commerci_2"))
    if text_or_none(row.get("all_vehi_2")) is not None and not hour_rates:
        counts.unreadable_rates += 1

    geometry = _blockface_line(row.get("the_geom"))
    confidence = _join_confidence(lookup, geometry, counts)
    blockface_id = str(row.get("pay_by_cel") or "").strip() or _fallback_id(row)
    geojson = None if geometry is None else _geojson(geometry)
    return [
        MeterRate(
            blockface_id=f"{blockface_id}:{segment.segment_id}",
            segment_id=segment.segment_id,
            side=side,
            rate_label=text_or_none(row.get("meter_rate")),
            hour_rates=hour_rates,
            commercial_hour_rates=commercial,
            max_session_min=parse_max_session_min(row.get("all_vehicl")),
            source=SOURCE_PARKNYC,
            confidence=confidence,
            geometry=geojson,
            bbox=None if geometry is None else geometry.bounds,
        )
        for segment in lookup.match.segments
    ]


def _join_confidence(lookup: BlockLookup, geometry: LineString | None, counts: _Counts) -> float:
    """How much to trust a name join, checked against the published geometry."""
    quality = min(
        name_quality(lookup.on.match),
        name_quality(lookup.from_.match),
        name_quality(lookup.to.match),
    )
    if geometry is None or lookup.match is None:
        return round(quality, 4)
    chain = lookup.match.line_ft
    published = to_feet(geometry)
    offset_ft = published.interpolate(0.5, normalized=True).distance(
        chain.interpolate(0.5, normalized=True)
    )
    if offset_ft > GEOMETRY_CHECK_FT:
        counts.geometry_mismatches += 1
        return round(quality * GEOMETRY_MISMATCH_PENALTY, 4)
    return round(quality, 4)


def _rate_zone_fallbacks(
    raw_dir: Path,
    metered_segments: Sequence[tuple[str, str | None, str | None, str]],
    priced_keys: set[tuple[str | None, str | None]],
) -> list[MeterRate]:
    """Zone-polygon rates for metered segments ParkNYC does not list (SPEC §13.1c).

    One row per `(segment_id, side, zone)` rather than per regulation segment:
    several spans share a centerline segment and the engine reads several rows
    on one key as "more than one zone covers this blockface".
    """
    wanted = [
        (segment_id, side, geom)
        for _, segment_id, side, geom in metered_segments
        if (segment_id, side) not in priced_keys
    ]
    if not wanted:
        return []
    zones = _load_rate_zones(raw_dir)
    if not zones:
        return []

    rates: dict[str, MeterRate] = {}
    for segment_id, side, geom in wanted:
        midpoint = _midpoint(geom)
        if midpoint is None:
            continue
        zone = next((zone for zone in zones if zone.polygon.contains(midpoint)), None)
        if zone is None or not zone.hour_rates:
            continue
        blockface_id = f"{SOURCE_RATE_ZONE}:{segment_id}:{side}:{zone.label}"
        rates.setdefault(
            blockface_id,
            MeterRate(
                blockface_id=blockface_id,
                segment_id=segment_id,
                side=side,
                rate_label=zone.label,
                hour_rates=zone.hour_rates,
                commercial_hour_rates=zone.commercial_hour_rates,
                max_session_min=None,
                source=SOURCE_RATE_ZONE,
                confidence=RATE_ZONE_CONFIDENCE,
            ),
        )
    return list(rates.values())


@dataclass(frozen=True)
class _RateZone:
    label: str
    hour_rates: tuple[str, ...]
    commercial_hour_rates: tuple[str, ...]
    polygon: Any


def _load_rate_zones(raw_dir: Path) -> list[_RateZone]:
    """Manhattan rate-zone polygons with their prices read out of `rate_zone`."""
    zones = []
    for row in fetch.load_rows("meter_rate_zones", raw_dir):
        if str(row.get("boro_name") or "").strip().lower() != MANHATTAN:
            continue
        geometry = row.get("the_geom")
        if not geometry:
            continue
        rate_text = str(row.get("rate_zone") or "")
        all_vehicles = _ZONE_ALL.search(rate_text)
        commercial = _ZONE_COMMERCIAL.search(rate_text)
        zones.append(
            _RateZone(
                label=str(row.get("zone") or "").strip() or "unknown zone",
                hour_rates=parse_hour_rates(all_vehicles.group(1) if all_vehicles else None),
                commercial_hour_rates=parse_hour_rates(commercial.group(1) if commercial else None),
                polygon=shape(geometry),
            )
        )
    # Smallest first: the Financial District polygon sits inside the borough-wide
    # M2 polygon, and the narrower zone is the one that governs.
    zones.sort(key=lambda zone: zone.polygon.area)
    return zones


def _midpoint(geom: str) -> Point | None:
    coordinates = json.loads(geom).get("coordinates") or []
    if len(coordinates) < 2:
        return None
    line = LineString([(float(lon), float(lat)) for lon, lat in coordinates])
    return line.interpolate(0.5, normalized=True)


def _blockface_line(geometry: Any) -> LineString | None:
    """The published blockface geometry as one line, or None if it is unusable."""
    if not isinstance(geometry, dict):
        return None
    parsed = shape(geometry)
    if isinstance(parsed, LineString):
        return parsed if len(parsed.coords) >= 2 else None
    if not isinstance(parsed, MultiLineString):
        return None
    merged = linemerge(parsed)
    if isinstance(merged, LineString):
        return merged if len(merged.coords) >= 2 else None
    parts = [part for part in merged.geoms if len(part.coords) >= 2]
    return max(parts, key=lambda part: part.length) if parts else None


def _geojson(line: LineString) -> dict[str, Any]:
    return {"type": "LineString", "coordinates": [[x, y] for x, y in line.coords]}


def _fallback_id(row: Mapping[str, Any]) -> str:
    return "|".join(
        str(row.get(key) or "") for key in ("on_street", "from_stree", "to_street", "side_of_st")
    )
