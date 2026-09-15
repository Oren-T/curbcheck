"""Turn snapped sign posts into the curb spans each regulation governs.

SPEC §B.2 picks arrow-direction extrapolation: a one-way arrow runs from its
post to the next post carrying the same kind of sign, or to the corner; a
two-way arrow runs each way to the post that says where its own rule stops
(docs/DECISIONS.md D20); a sign with no arrow governs the whole blockface, which
is what 34 RCNY 4-08 says about a single authorized sign. The arrow's *arity*
comes from the grammar's reading of the sign and its *bearing* from
`arrow_direction` (docs/DECISIONS.md D3 refined).

Two passes follow the extrapolation: posts that repeat one rule over a stretch
have their spans unioned (D5 in docs/VALIDATION.md §4), and every street side
left with no span at all gets a placeholder so the map can say "no data" rather
than draw nothing (docs/DECISIONS.md D23).
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from shapely.geometry import LineString, mapping
from shapely.ops import substring

from curbcheck.config import CAR_LENGTH_FT
from curbcheck.db import geojson_bbox
from curbcheck.etl.snap import COMPASS_UNITS, SnapResult, side_offset_sign
from curbcheck.etl.stage import StagedSign
from curbcheck.etl.streets import (
    BlockMatch,
    StreetGraph,
    StreetSegment,
    normalize_street_name,
    to_degrees,
)
from curbcheck.model import Action, Arrow, ParsedSign

LOGGER = logging.getLogger(__name__)

# Which prohibition stands for a post carrying several: the widest one, because
# that is the rule whose span decides where the next sign takes over.
_PROHIBITION_ORDER: dict[Action, int] = {Action.PARK: 0, Action.STAND: 1, Action.STOP: 2}

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

# `gap_kind` values on a placeholder span: the source carries no active sign
# rows for that blockface-side at all, or it carries them and none could be
# snapped. SPEC §11 asks for exactly this split.
NO_SIGNS = "no_signs"
UNMATCHED_SIGNS = "unmatched_signs"

# Placeholders are drawn for the street network only. rw_type 2 (highway), 3
# (bridge) and 10 (connector) are snappable because signs are posted along them,
# but there is no parkable curb there to leave grey (docs/DATA.md §2.1).
PLACEHOLDER_RW_TYPE = "1"

# Vertex spacing when shapely's offset_curve degenerates and the curb line has
# to be rebuilt by hand.
_FALLBACK_STEP_FT = 25.0


class Arity(StrEnum):
    """How many ways a sign's arrow points. See docs/DECISIONS.md D3 (refined)."""

    NONE = "none"
    SINGLE = "single"
    DOUBLE = "double"


@dataclass(frozen=True)
class SignReading:
    """What the grammar read off one description that the span rules need.

    Both halves come from the same `ParsedSign`, so there is one reader of the
    arrow and one reader of the rule; `arrow_arity` is only the fallback for
    strings the grammar could not read at all.
    """

    family_action: str | None
    arity: Arity | None
    """None when the grammar read no rule at all; the caller falls back to the glyph."""


def reading_of(parsed: ParsedSign) -> SignReading:
    """Fold a parse result into the family key and arrow arity `segments` needs."""
    return SignReading(family_action=_family_action(parsed), arity=_parsed_arity(parsed))


def _family_action(parsed: ParsedSign) -> str | None:
    """The one action key that stands for a whole sign, or None if it states no rule.

    A sign carrying several rules is keyed by its most restrictive prohibition,
    because that is the one whose span decides where the next sign takes over.
    A meta sign states no rule of its own, so it has no family and must not bound
    the span of the sign it modifies (SPEC §8.5).
    """
    rules = [rule for rule in parsed.regulations if not rule.flags.meta]
    prohibitions = [rule.action for rule in rules if rule.applies_to_passenger() is False]
    if prohibitions:
        return "no-" + max(prohibitions, key=_PROHIBITION_ORDER.__getitem__).value
    if any(rule.applies_to_passenger() is True for rule in rules):
        return "park"
    return None


def _parsed_arity(parsed: ParsedSign) -> Arity | None:
    """Arrow arity as the grammar read it, or None when it read no rule at all.

    The grammar is the single reader of the arrow: it sees `W/ 7 O'CLOCK ARROW`
    and the worded forms that the glyph regex alone misses (3 strings, 21 rows
    on the 2026-09-15 snapshot).
    """
    arrows = {rule.arrow for rule in parsed.regulations}
    if not arrows:
        return None
    if Arrow.BOTH in arrows:
        return Arity.DOUBLE
    if arrows & {Arrow.FORWARD, Arrow.BACKWARD}:
        return Arity.SINGLE
    return Arity.NONE


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
    capacity_cars: int | None
    """None on a placeholder: an unknown stretch of curb holds an unknown number of cars."""
    capacity_approximate: bool
    confidence: float
    derived_from: tuple[str, ...]
    gap_kind: str | None = None
    """None on a real span; `no_signs` or `unmatched_signs` on a coverage placeholder."""


@dataclass(frozen=True)
class SegmentReport:
    """Counts for `sync_meta`, so a change in the arrow logic is visible in the diff."""

    blockface_sides: int
    segments: int
    whole_side_spans: int
    arrow_extended_spans: int
    merged_repeat_spans: int
    """Spans absorbed into a neighbour because they state the same rule (SPEC §B.2)."""
    degenerate_spans: int
    offset_curve_fallbacks: int
    signs_used: int
    sides_with_rules: int
    """Centerline `(segment_id, side)` pairs a resolved span covers."""
    no_signs_sides: int
    unmatched_sides: int


def strip_supersedes(description: str) -> str:
    return _SUPERSEDES_TAIL.sub("", description).strip()


def arrow_arity(description: str) -> Arity:
    """Read arrow arity off the description glyph, for signs the grammar could not read.

    The grammar is the primary reader (`reading_of`); this is the fallback, and
    it exists because a span has to be computed for every sign, including the
    ones no rule could be parsed from. `build.run_parse` counts the strings
    where the two would differ into `sync_meta.parse_arrow_disagreements`.
    """
    text = strip_supersedes(description.upper())
    if _DOUBLE_ARROW.search(text):
        return Arity.DOUBLE
    if _SINGLE_ARROW.search(text) or _WORDED_ARROW.search(text):
        return Arity.SINGLE
    return Arity.NONE


def regulation_family(sign: StagedSign, parsed_action: str | None = None) -> str:
    """Key deciding whether two posts carry "the same kind of sign" for arrow extension.

    The `sign_code` prefix before the dash plus the rule the sign states.
    `parsed_action` is `SignReading.family_action` and is the preferred half; the
    description's head word ("NO PARKING", "HMP") is the fallback for strings the
    grammar cannot read.
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
    snaps: Iterable[SnapResult],
    *,
    readings: Mapping[str, SignReading] | None = None,
    graph: StreetGraph | None = None,
) -> tuple[list[RegulationSegment], SegmentReport]:
    """Group snapped regulation signs into curb spans, one per distinct span.

    Only matched regulation panels take part (docs/DECISIONS.md D10). Signs
    whose spans come out identical — the usual case being several panels on one
    post — collapse into a single segment carrying all their sign ids.

    With `graph`, every street side that ends up with no span also gets a
    placeholder so the map can draw SPEC §11's grey "no sign data" state there
    instead of nothing at all. Without it — the unit tests — only real spans
    come back.
    """
    all_snaps = list(snaps)
    faces: dict[tuple[str, str], list[SnapResult]] = {}
    for snap in all_snaps:
        if snap.block is None or not snap.sign.is_regulation:
            continue
        faces.setdefault((_chain_key(snap), snap.side), []).append(snap)

    segments: list[RegulationSegment] = []
    counts = {"whole": 0, "arrow": 0, "merged": 0, "degenerate": 0, "fallback": 0, "signs": 0}
    covered: set[tuple[str, str]] = set()
    for (chain_key, side), members in sorted(faces.items()):
        segments.extend(_resolve_face(chain_key, side, members, readings or {}, counts, covered))
    placeholders = [] if graph is None else coverage_placeholders(graph, covered, all_snaps)
    segments.extend(placeholders)
    report = SegmentReport(
        blockface_sides=len(faces),
        segments=len(segments),
        whole_side_spans=counts["whole"],
        arrow_extended_spans=counts["arrow"],
        merged_repeat_spans=counts["merged"],
        degenerate_spans=counts["degenerate"],
        offset_curve_fallbacks=counts["fallback"],
        signs_used=counts["signs"],
        sides_with_rules=len(covered),
        no_signs_sides=sum(1 for span in placeholders if span.gap_kind == NO_SIGNS),
        unmatched_sides=sum(1 for span in placeholders if span.gap_kind == UNMATCHED_SIGNS),
    )
    LOGGER.info(
        "segments.resolve faces=%d segments=%d whole_side=%d arrow=%d merged=%d degenerate=%d",
        report.blockface_sides,
        report.segments,
        report.whole_side_spans,
        report.arrow_extended_spans,
        report.merged_repeat_spans,
        report.degenerate_spans,
    )
    return segments, report


def coverage_placeholders(
    graph: StreetGraph, covered: set[tuple[str, str]], snaps: Sequence[SnapResult]
) -> list[RegulationSegment]:
    """One empty span per street side that no resolved span covers (SPEC §11).

    A blank map is read by a driver as "nothing here", not as "unknown", which
    is the hazard SPEC §11 exists to prevent; 10,208 of Manhattan's 22,204
    centerline sides drew nothing at all before this (docs/VALIDATION.md §5).
    Each placeholder carries no rules, zero confidence, no car count, and a
    `gap_kind` saying whether the source has no signs for that blockface-side
    (`no_signs`) or has them and could not snap them (`unmatched_signs`).

    Only `rw_type` 1 — the street network — gets placeholders. Highways (2),
    bridges (3) and connectors (10) are in the snap pool because signs are
    posted along them, but they have no parkable curb to leave grey.
    """
    unmatched = _unmatched_faces(graph, snaps)
    sides_used: dict[str, set[str]] = {}
    for segment_id, side in covered:
        sides_used.setdefault(segment_id, set()).add(side)
    placeholders = []
    for segment in graph.segments.values():
        if segment.rw_type != PLACEHOLDER_RW_TYPE:
            continue
        crossing = graph.node_streets(segment.from_node) | graph.node_streets(segment.to_node)
        for side in _sides_for(segment, sides_used.get(segment.segment_id)):
            if (segment.segment_id, side) in covered:
                continue
            placeholders.append(
                _placeholder(segment, side, _gap_kind(unmatched, segment, crossing, side))
            )
    return placeholders


def _placeholder(segment: StreetSegment, side: str, gap_kind: str) -> RegulationSegment:
    offset_sign, _ = side_offset_sign(segment.line_ft, side)
    geometry, _ = _curb_geometry(
        segment.line_ft, 0.0, segment.length_ft, segment.width_ft / 2.0, offset_sign
    )
    return RegulationSegment(
        reg_seg_id=_reg_seg_id(f"gap|{segment.segment_id}", side, 0.0, segment.length_ft),
        segment_id=segment.segment_id,
        side=side,
        start_ft=0.0,
        end_ft=round(segment.length_ft, 2),
        length_ft=round(segment.length_ft, 2),
        geometry=geometry,
        bbox=geojson_bbox(geometry),
        capacity_cars=None,
        capacity_approximate=True,
        confidence=0.0,
        derived_from=(),
        gap_kind=gap_kind,
    )


_SIDE_AXIS: dict[str, tuple[str, str]] = {
    "N": ("N", "S"),
    "S": ("N", "S"),
    "E": ("E", "W"),
    "W": ("E", "W"),
}


def _sides_for(segment: StreetSegment, used: set[str] | None) -> tuple[str, str]:
    """The two curb letters a segment can carry.

    A letter one of its own spans already uses settles it. DOT sides a run by
    the axis it happens to be closest to and does not always agree with the
    bearing — PECK SLIP runs more north than east and DOT sides it N/S — and
    taking the bearing's answer there would draw a second, differently-lettered
    placeholder over a side that already has rules.
    """
    if used:
        return _SIDE_AXIS[min(used)]
    return _sides_of(segment.line_ft)


def _sides_of(line_ft: LineString) -> tuple[str, str]:
    """The two curb letters a run can carry, read off its bearing, not its name.

    Broadway and St Nicholas Ave are diagonals and DOT sides them E/W because
    they run more north than east, which is what comparing the components does.
    """
    start, end = line_ft.coords[0], line_ft.coords[-1]
    east_west = abs(end[0] - start[0]) > abs(end[1] - start[1])
    return ("N", "S") if east_west else ("E", "W")


@dataclass(frozen=True)
class _UnmatchedFace:
    """One `(on, from, to, side)` group DOT posts signs on that none of ours snapped."""

    crosses: frozenset[str]
    has_unknown_cross: bool


def _unmatched_faces(
    graph: StreetGraph, snaps: Sequence[SnapResult]
) -> dict[tuple[str, str], list[_UnmatchedFace]]:
    faces: dict[tuple[str, str], list[_UnmatchedFace]] = {}
    seen: set[tuple[str, str, str, str]] = set()
    for snap in snaps:
        sign = snap.sign
        if snap.matched or not sign.is_regulation or sign.blockface_key in seen:
            continue
        seen.add(sign.blockface_key)
        crosses = {normalize_street_name(sign.from_street), normalize_street_name(sign.to_street)}
        key = (normalize_street_name(sign.on_street), sign.side_of_street)
        faces.setdefault(key, []).append(
            _UnmatchedFace(
                crosses=frozenset(crosses),
                has_unknown_cross=any(name not in graph.street_names for name in crosses),
            )
        )
    return faces


def _gap_kind(
    unmatched: Mapping[tuple[str, str], Sequence[_UnmatchedFace]],
    segment: StreetSegment,
    crossing: set[str],
    side: str,
) -> str:
    """Which kind of gap a side is: no signs in the source, or signs that never snapped.

    A blockface-side is matched to a centerline side by name, because an
    unmatched sign has no geometry by definition. Both cross streets have to
    meet this segment, unless one of the names is in no centerline row at all —
    the case that produced the miss in the first place (docs/VALIDATION.md §4 D4).
    """
    for face in unmatched.get((segment.street_norm, side), ()):
        shared = face.crosses & crossing
        if face.crosses <= crossing or (shared and face.has_unknown_cross):
            return UNMATCHED_SIGNS
    return NO_SIGNS


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
    readings: Mapping[str, SignReading],
    counts: dict[str, int],
    covered: set[tuple[str, str]],
) -> list[RegulationSegment]:
    block = _block_of(members[0])
    line_ft = _canonical_line(block)
    length_ft = float(line_ft.length)
    posts = sorted(
        (_post_for(snap, line_ft, length_ft, readings) for snap in members),
        key=lambda post: (post.distance_ft, post.snap.sign.sign_id),
    )
    by_family: dict[str, list[float]] = {}
    for post in posts:
        by_family.setdefault(post.family, []).append(post.distance_ft)

    spans: dict[tuple[float, float], list[_Post]] = {}
    for post in posts:
        others = [other.distance_ft for other in posts if other.family != post.family]
        span = _span_for(post, by_family[post.family], others, length_ft, counts)
        # Quantized because the chain length reaches a span two ways — the line's
        # own length and a post clamped to it — which differ by an ULP, and two
        # spans a hundredth of a foot apart are one span, not two rows sharing a
        # `reg_seg_id`.
        spans.setdefault((round(span[0], 2), round(span[1], 2)), []).append(post)
        counts["signs"] += 1
    spans = _merge_repeated_spans(spans, counts)

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
        # A span that runs over a chain covers every segment it crosses, not
        # only the one it is filed under, or the rest would look uncovered.
        covered.update(
            (segment_id, side) for segment_id in _segments_between(chain, start_ft, end_ft)
        )
        resolved.append(
            RegulationSegment(
                reg_seg_id=_reg_seg_id(chain_key, side, start_ft, end_ft),
                segment_id=_segment_at(chain, (start_ft + end_ft) / 2.0),
                side=side,
                start_ft=round(start_ft, 2),
                end_ft=round(end_ft, 2),
                length_ft=round(span_length, 2),
                geometry=geometry,
                bbox=geojson_bbox(geometry),
                capacity_cars=int(span_length // CAR_LENGTH_FT),
                # v1 cannot subtract hydrant, driveway or crosswalk setbacks:
                # none of them are in the data yet (SPEC §8.5, config.HYDRANT_SETBACK_FT).
                capacity_approximate=True,
                confidence=round(min(post.snap.snap_confidence for post in group), 4),
                derived_from=sign_ids,
            )
        )
    return resolved


def _merge_repeated_spans(
    spans: Mapping[tuple[float, float], list[_Post]], counts: dict[str, int]
) -> dict[tuple[float, float], list[_Post]]:
    """Union the spans of posts that repeat one rule and overlap or touch.

    SPEC §B.2 reads a repeated sign as notice, not as a second rule: five
    identical `<->` posts along a stretch describe one span, and leaving them as
    five overlapping spans repeats the same curb in the ranked list and inflates
    the count line (docs/VALIDATION.md §4 D5). Only an identical rule set merges
    — a different sign text is a different rule and keeps its own span, however
    much the two overlap.
    """
    by_rule: dict[frozenset[str], list[tuple[tuple[float, float], list[_Post]]]] = {}
    for span, group in spans.items():
        by_rule.setdefault(_rule_key(group), []).append((span, group))
    merged: dict[tuple[float, float], list[_Post]] = {}
    for items in by_rule.values():
        for span, group in _union_touching(items, counts):
            merged.setdefault(span, []).extend(group)
    return merged


def _rule_key(group: Sequence[_Post]) -> frozenset[str]:
    """What the posts behind a span say, as the rule identity two spans share."""
    return frozenset(strip_supersedes(post.snap.sign.sign_description.upper()) for post in group)


def _union_touching(
    items: Sequence[tuple[tuple[float, float], list[_Post]]], counts: dict[str, int]
) -> list[tuple[tuple[float, float], list[_Post]]]:
    runs: list[tuple[tuple[float, float], list[_Post]]] = []
    for (start_ft, end_ft), group in sorted(items):
        if runs and start_ft <= runs[-1][0][1]:
            (run_start, run_end), members = runs[-1]
            runs[-1] = ((run_start, max(run_end, end_ft)), [*members, *group])
            counts["merged"] += 1
            continue
        runs.append(((start_ft, end_ft), list(group)))
    return runs


def _post_for(
    snap: SnapResult, line_ft: LineString, length_ft: float, readings: Mapping[str, SignReading]
) -> _Post:
    distance = _canonical_distance(snap, _block_of(snap), length_ft)
    sign = snap.sign
    reading = readings.get(sign.sign_description)
    arity = reading.arity if reading is not None and reading.arity is not None else None
    if arity is None:
        arity = arrow_arity(sign.sign_description)
    forward = None
    if arity is Arity.SINGLE:
        forward = _points_forward(sign.arrow_direction, line_ft)
    return _Post(
        snap=snap,
        distance_ft=distance,
        family=regulation_family(sign, reading.family_action if reading else None),
        arity=arity,
        forward=forward,
    )


def _span_for(
    post: _Post,
    family_posts: Sequence[float],
    other_posts: Sequence[float],
    length_ft: float,
    counts: dict[str, int],
) -> tuple[float, float]:
    """The curb span one post governs, per SPEC §B.2 and 34 RCNY 4-08."""
    whole_side = (0.0, length_ft)
    span = whole_side
    if post.arity is Arity.SINGLE and post.forward is not None:
        span = _single_arrow_span(post, family_posts, length_ft)
    elif post.arity is Arity.DOUBLE:
        span = _double_arrow_span(post, family_posts, other_posts, length_ft)
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
    post: _Post, family_posts: Sequence[float], other_posts: Sequence[float], length_ft: float
) -> tuple[float, float]:
    """A two-way arrow runs each way to the post that says where its own rule stops.

    Each direction is resolved on its own: the nearest same-family post first,
    because that is DOT subdividing the block; failing that the nearest post of
    any other family, because a new sign is where a new regime starts; and only
    a direction with no post at all reaches the corner, which is 34 RCNY 4-08's
    "one authorized sign governs the block".

    The old rule fell back to the whole side as soon as one direction had no
    same-family post, which buried a metered curb under a bus stop on 8 AVE
    side E (docs/VALIDATION.md §4 D1).
    """
    start = _bound(_last_before, family_posts, other_posts, post.distance_ft)
    end = _bound(_next_after, family_posts, other_posts, post.distance_ft)
    return (0.0 if start is None else start, length_ft if end is None else end)


def _bound(
    pick: Callable[[Sequence[float], float], float | None],
    family_posts: Sequence[float],
    other_posts: Sequence[float],
    distance_ft: float,
) -> float | None:
    same_family = pick(family_posts, distance_ft)
    return same_family if same_family is not None else pick(other_posts, distance_ft)


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


def _segments_between(
    chain: tuple[tuple[str, ...], tuple[float, ...]], start_ft: float, end_ft: float
) -> list[str]:
    """Every centerline segment a span touches, in chain order."""
    ids, lengths = chain
    touched = []
    travelled = 0.0
    for segment_id, length in zip(ids, lengths, strict=True):
        if travelled < end_ft and start_ft < travelled + length:
            touched.append(segment_id)
        travelled += length
    return touched or [ids[-1]]


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
