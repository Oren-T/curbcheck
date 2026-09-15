"""Turn snapped sign posts into the curb spans each regulation governs.

SPEC §B.2 picks arrow-direction extrapolation: a one-way arrow runs from its
post to the next post carrying the same kind of sign, or to the corner; a sign
with no arrow governs the whole blockface, which is also what 34 RCNY 4-08 says
about a single authorized sign. The arrow's *arity* is read from the glyph and
its *bearing* from `arrow_direction` (docs/DECISIONS.md D3 refined).
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from shapely.geometry import LineString, mapping
from shapely.ops import substring

from curbcheck.config import CAR_LENGTH_FT
from curbcheck.etl.snap import COMPASS_UNITS, SnapResult, side_offset_sign
from curbcheck.etl.stage import StagedSign
from curbcheck.etl.streets import BlockMatch, to_degrees

LOGGER = logging.getLogger(__name__)

# The trailing "(SUPERSEDES SP-1B & SP-2B)" hides the arrow on 46,030 of the
# 54,164 rows that otherwise end with one, so it comes off first (DATA §1.6).
_SUPERSEDES_TAIL = re.compile(r"\s*\((?:SUPERSEDES|REPLACES)[^)]*\)\s*$")

# Arrows are drawn with a variable number of dashes: <-----> occurs 772 times.
_DOUBLE_ARROW = re.compile(r"<-+>")
_SINGLE_ARROW = re.compile(r"(?:<-+(?!>))|(?:-+>)")
_WORDED_ARROW = re.compile(r"\bSINGLE ARROW\b|\(ARROW\)")

_N_HOUR_PREFIX = re.compile(r"^\d+\s*(?:HOUR|HR|HMP|MMP|MP)\b")
_LEADING_SYMBOL = re.compile(r"^\([^)]*\)\s*|^[A-Z /&]+\(SYMBOLS?\)\s*")

# A span shorter than this is a sign pointing off the end of its own block,
# which means the distance or the bearing is wrong; fall back to the whole side.
MIN_SPAN_FT = 1.0

# Vertex spacing when shapely's offset_curve degenerates and the curb line has
# to be rebuilt by hand.
_FALLBACK_STEP_FT = 25.0


class Arity(StrEnum):
    """How many ways a sign's arrow points. See docs/DECISIONS.md D3 (refined)."""

    NONE = "none"
    SINGLE = "single"
    DOUBLE = "double"


@dataclass(frozen=True)
class RegulationSegment:
    """One curb span, ready for the `regulation_segment` table."""

    reg_seg_id: str
    segment_id: str
    side: str
    start_ft: float
    end_ft: float
    length_ft: float
    geometry: dict[str, Any]
    bbox: tuple[float, float, float, float]
    capacity_cars: int
    capacity_approximate: bool
    confidence: float
    derived_from: tuple[str, ...]


@dataclass(frozen=True)
class SegmentReport:
    """Counts for `sync_meta`, so a change in the arrow logic is visible in the diff."""

    blockface_sides: int
    segments: int
    whole_side_spans: int
    arrow_extended_spans: int
    degenerate_spans: int
    offset_curve_fallbacks: int
    signs_used: int


def strip_supersedes(description: str) -> str:
    return _SUPERSEDES_TAIL.sub("", description).strip()


def arrow_arity(description: str) -> Arity:
    """Read arrow arity off a sign description.

    Hook: once `curbcheck.etl.parse` lands it will return arity as part of its
    structured output and this becomes a fallback for descriptions it cannot
    read. The geometry step cannot wait for it, because the span of a rule is
    decided before anything is parsed.
    """
    text = strip_supersedes(description.upper())
    if _DOUBLE_ARROW.search(text):
        return Arity.DOUBLE
    if _SINGLE_ARROW.search(text) or _WORDED_ARROW.search(text):
        return Arity.SINGLE
    return Arity.NONE


def regulation_family(sign: StagedSign, parsed_action: str | None = None) -> str:
    """Key deciding whether two posts carry "the same kind of sign" for arrow extension.

    Until the parser is available this is the `sign_code` prefix before the dash
    plus the head of the description ("NO PARKING", "NO STANDING", "HMP"), which
    separates the families that actually subdivide a blockface. Hook: pass
    `parsed_action` from `curbcheck.etl.parse` to key on the real action instead.
    """
    code_prefix = sign.sign_code.split("-", 1)[0].strip() or "UNKNOWN"
    head = parsed_action or _description_head(sign.sign_description)
    return f"{code_prefix}|{head}"


def _description_head(description: str) -> str:
    text = _LEADING_SYMBOL.sub("", strip_supersedes(description.upper())).strip()
    for prefix in ("NO PARKING", "NO STANDING", "NO STOPPING", "BUS STOP"):
        if text.startswith(prefix):
            return prefix
    if _N_HOUR_PREFIX.match(text):
        # "1 HMP", "2 HOUR PARKING" and "3 HR METERED" are one family: they are
        # the same regulation with a different number on it.
        return "HMP"
    return " ".join(text.split()[:2])


def resolve_segments(
    snaps: Iterable[SnapResult], *, parsed_actions: Mapping[str, str] | None = None
) -> tuple[list[RegulationSegment], SegmentReport]:
    """Group snapped regulation signs into curb spans, one per distinct span.

    Only matched regulation panels take part (docs/DECISIONS.md D10). Signs
    whose spans come out identical — the usual case being several panels on one
    post — collapse into a single segment carrying all their sign ids.
    """
    faces: dict[tuple[str, str], list[SnapResult]] = {}
    for snap in snaps:
        if snap.block is None or not snap.sign.is_regulation:
            continue
        faces.setdefault((_chain_key(snap), snap.side), []).append(snap)

    segments: list[RegulationSegment] = []
    counts = {"whole": 0, "arrow": 0, "degenerate": 0, "fallback": 0, "signs": 0}
    for (chain_key, side), members in sorted(faces.items()):
        segments.extend(_resolve_face(chain_key, side, members, parsed_actions or {}, counts))
    report = SegmentReport(
        blockface_sides=len(faces),
        segments=len(segments),
        whole_side_spans=counts["whole"],
        arrow_extended_spans=counts["arrow"],
        degenerate_spans=counts["degenerate"],
        offset_curve_fallbacks=counts["fallback"],
        signs_used=counts["signs"],
    )
    LOGGER.info(
        "segments.resolve faces=%d segments=%d whole_side=%d arrow=%d degenerate=%d",
        report.blockface_sides,
        report.segments,
        report.whole_side_spans,
        report.arrow_extended_spans,
        report.degenerate_spans,
    )
    return segments, report


@dataclass(frozen=True)
class _Post:
    """One sign placed on the canonical blockface-side line."""

    snap: SnapResult
    distance_ft: float
    family: str
    arity: Arity
    forward: bool | None
    """Whether a single arrow points towards increasing distance. None if unknown."""


def _resolve_face(
    chain_key: str,
    side: str,
    members: Sequence[SnapResult],
    parsed_actions: Mapping[str, str],
    counts: dict[str, int],
) -> list[RegulationSegment]:
    block = _block_of(members[0])
    line_ft = _canonical_line(block)
    length_ft = float(line_ft.length)
    posts = sorted(
        (_post_for(snap, line_ft, length_ft, parsed_actions) for snap in members),
        key=lambda post: (post.distance_ft, post.snap.sign.sign_id),
    )
    by_family: dict[str, list[float]] = {}
    for post in posts:
        by_family.setdefault(post.family, []).append(post.distance_ft)

    spans: dict[tuple[float, float], list[_Post]] = {}
    for post in posts:
        span = _span_for(post, by_family[post.family], length_ft, counts)
        spans.setdefault(span, []).append(post)
        counts["signs"] += 1

    offset_sign, _ = side_offset_sign(line_ft, side)
    chain = _canonical_chain(block)
    resolved = []
    for (start_ft, end_ft), group in sorted(spans.items()):
        geometry, fell_back = _curb_geometry(
            line_ft, start_ft, end_ft, block.width_ft / 2.0, offset_sign
        )
        if fell_back:
            counts["fallback"] += 1
        sign_ids = tuple(sorted(post.snap.sign.sign_id for post in group))
        span_length = end_ft - start_ft
        resolved.append(
            RegulationSegment(
                reg_seg_id=_reg_seg_id(chain_key, side, start_ft, end_ft),
                segment_id=_segment_at(chain, (start_ft + end_ft) / 2.0),
                side=side,
                start_ft=round(start_ft, 2),
                end_ft=round(end_ft, 2),
                length_ft=round(span_length, 2),
                geometry=geometry,
                bbox=_bbox(geometry),
                capacity_cars=int(span_length // CAR_LENGTH_FT),
                # v1 cannot subtract hydrant, driveway or crosswalk setbacks:
                # none of them are in the data yet (SPEC §8.5, config.HYDRANT_SETBACK_FT).
                capacity_approximate=True,
                confidence=round(min(post.snap.snap_confidence for post in group), 4),
                derived_from=sign_ids,
            )
        )
    return resolved


def _post_for(
    snap: SnapResult, line_ft: LineString, length_ft: float, parsed_actions: Mapping[str, str]
) -> _Post:
    distance = _canonical_distance(snap, _block_of(snap), length_ft)
    sign = snap.sign
    arity = arrow_arity(sign.sign_description)
    forward = None
    if arity is Arity.SINGLE:
        forward = _points_forward(sign.arrow_direction, line_ft)
    return _Post(
        snap=snap,
        distance_ft=distance,
        family=regulation_family(sign, parsed_actions.get(sign.sign_description)),
        arity=arity,
        forward=forward,
    )


def _span_for(
    post: _Post, family_posts: Sequence[float], length_ft: float, counts: dict[str, int]
) -> tuple[float, float]:
    """The curb span one post governs, per SPEC §B.2 and 34 RCNY 4-08."""
    whole_side = (0.0, length_ft)
    span = whole_side
    if post.arity is Arity.SINGLE and post.forward is not None:
        span = _single_arrow_span(post, family_posts, length_ft)
    elif post.arity is Arity.DOUBLE:
        span = _double_arrow_span(post, family_posts, length_ft)
    if span[1] - span[0] < MIN_SPAN_FT:
        counts["degenerate"] += 1
        span = whole_side
    if span == whole_side:
        counts["whole"] += 1
    else:
        counts["arrow"] += 1
    return span


def _single_arrow_span(
    post: _Post, family_posts: Sequence[float], length_ft: float
) -> tuple[float, float]:
    if post.forward:
        ahead = _next_after(family_posts, post.distance_ft)
        return (post.distance_ft, length_ft if ahead is None else ahead)
    behind = _last_before(family_posts, post.distance_ft)
    return (0.0 if behind is None else behind, post.distance_ft)


def _double_arrow_span(
    post: _Post, family_posts: Sequence[float], length_ft: float
) -> tuple[float, float]:
    """A two-way arrow governs the whole side unless same-family posts bound it both ways.

    34 RCNY 4-08 makes one authorized sign govern the block, so the default is
    the whole blockface-side; a same-family post on each side means DOT has
    subdivided the block and the rule stops where the next one starts.
    """
    before = _last_before(family_posts, post.distance_ft)
    after = _next_after(family_posts, post.distance_ft)
    if before is None or after is None:
        return (0.0, length_ft)
    return (before, after)


def _next_after(posts: Sequence[float], distance_ft: float) -> float | None:
    later = [post for post in posts if post > distance_ft]
    return min(later) if later else None


def _last_before(posts: Sequence[float], distance_ft: float) -> float | None:
    earlier = [post for post in posts if post < distance_ft]
    return max(earlier) if earlier else None


def _points_forward(arrow_direction: str | None, line_ft: LineString) -> bool | None:
    """Map the compass word in `arrow_direction` onto the chain's direction of travel.

    Returns None when the column is blank or unreadable, which makes the sign
    fall back to governing the whole blockface-side rather than guessing a way.
    """
    if not arrow_direction:
        return None
    compass = COMPASS_UNITS.get(arrow_direction.strip().upper()[:1])
    if compass is None:
        return None
    start, end = line_ft.coords[0], line_ft.coords[-1]
    dx, dy = end[0] - start[0], end[1] - start[1]
    span = math.hypot(dx, dy)
    if span == 0:
        return None
    alignment = (dx / span) * compass[0] + (dy / span) * compass[1]
    return alignment >= 0


def _curb_geometry(
    line_ft: LineString, start_ft: float, end_ft: float, offset_ft: float, offset_sign: int
) -> tuple[dict[str, Any], bool]:
    """GeoJSON for the curb line of one span, in EPSG:4326.

    shapely's offset_curve can return an empty or multi-part result on a chain
    that doubles back on itself, so the fallback rebuilds the line by walking
    the span and offsetting each sample along its own normal.
    """
    span = _span_line(line_ft, start_ft, end_ft)
    offset = span.offset_curve(offset_ft * offset_sign)
    fell_back = False
    if not isinstance(offset, LineString) or offset.is_empty or len(offset.coords) < 2:
        offset = _manual_offset(span, offset_ft, offset_sign)
        fell_back = True
    geometry: dict[str, Any] = mapping(to_degrees(offset))
    return (geometry, fell_back)


def _span_line(line_ft: LineString, start_ft: float, end_ft: float) -> LineString:
    """The chain between two distances; shapely collapses a zero-length cut to a Point."""
    cut = substring(line_ft, start_ft, end_ft)
    if isinstance(cut, LineString) and len(cut.coords) >= 2:
        return cut
    start = min(max(start_ft, 0.0), max(line_ft.length - MIN_SPAN_FT, 0.0))
    here = line_ft.interpolate(start)
    ahead = line_ft.interpolate(min(start + MIN_SPAN_FT, line_ft.length))
    return LineString([(here.x, here.y), (ahead.x, ahead.y)])


def _manual_offset(span: LineString, offset_ft: float, offset_sign: int) -> LineString:
    steps = max(int(span.length // _FALLBACK_STEP_FT), 1)
    points = []
    for index in range(steps + 1):
        at = span.length * index / steps
        here = span.interpolate(at)
        ahead = span.interpolate(min(at + 1.0, span.length))
        behind = span.interpolate(max(min(at + 1.0, span.length) - 1.0, 0.0))
        dx, dy = ahead.x - behind.x, ahead.y - behind.y
        length = math.hypot(dx, dy) or 1.0
        points.append(
            (
                here.x - dy / length * offset_ft * offset_sign,
                here.y + dx / length * offset_ft * offset_sign,
            )
        )
    return LineString(points)


def _bbox(geometry: Mapping[str, Any]) -> tuple[float, float, float, float]:
    coords = [tuple(position) for position in geometry["coordinates"]]
    lons = [float(position[0]) for position in coords]
    lats = [float(position[1]) for position in coords]
    return (min(lons), min(lats), max(lons), max(lats))


def _chain_key(snap: SnapResult) -> str:
    return "+".join(_canonical_chain(_block_of(snap))[0])


def _block_of(snap: SnapResult) -> BlockMatch:
    if snap.block is None:
        raise ValueError(f"sign {snap.sign.sign_id} was not snapped to a block")
    return snap.block


def _canonical_chain(block: BlockMatch) -> tuple[tuple[str, ...], tuple[float, ...]]:
    """Segment ids and lengths in the chain's canonical direction.

    A blockface described as "E 85 ST to E 86 ST" and one described the other
    way round are the same curb, so both are reduced to the orientation whose
    start node sorts first before anything is grouped or measured.
    """
    ids = tuple(segment.segment_id for segment in block.segments)
    lengths = tuple(segment.length_ft for segment in block.segments)
    if _is_canonical(block):
        return (ids, lengths)
    return (ids[::-1], lengths[::-1])


def _is_canonical(block: BlockMatch) -> bool:
    return block.from_node <= block.to_node


def _canonical_line(block: BlockMatch) -> LineString:
    if _is_canonical(block):
        return block.line_ft
    return LineString(list(block.line_ft.coords)[::-1])


def _canonical_distance(snap: SnapResult, block: BlockMatch, length_ft: float) -> float:
    if _is_canonical(block):
        return snap.distance_ft
    return max(0.0, length_ft - snap.distance_ft)


def _segment_at(chain: tuple[tuple[str, ...], tuple[float, ...]], distance_ft: float) -> str:
    """The centerline segment a span is filed under: the one covering its midpoint."""
    ids, lengths = chain
    travelled = 0.0
    for segment_id, length in zip(ids, lengths, strict=True):
        travelled += length
        if distance_ft <= travelled:
            return segment_id
    return ids[-1]


def _reg_seg_id(chain_key: str, side: str, start_ft: float, end_ft: float) -> str:
    key = f"{chain_key}|{side}|{start_ft:.2f}|{end_ft:.2f}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
