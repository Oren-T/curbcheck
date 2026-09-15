"""Place each sign on the curb by linear referencing, and say how much to trust it.

DOT describes a sign's position in words: the street it is on, the two cross
streets that bound the block, the side, and how many feet it sits from the
`from_street` end (SPEC §B.1). That description is the primary source of
position here; the published `sign_x_coord`/`sign_y_coord` pair is only a
confidence input, because it is absent on 6.6% of rows (docs/DATA.md §1.7).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from shapely.geometry import LineString, Point

from curbcheck.etl.stage import StagedSign
from curbcheck.etl.streets import BlockMatch, NameMatch, StreetGraph, feet_to_lonlat

LOGGER = logging.getLogger(__name__)

# Unit vectors for the compass letters in side_of_street, in EPSG:2263 where x
# grows east and y grows north.
COMPASS_UNITS: dict[str, tuple[float, float]] = {
    "N": (0.0, 1.0),
    "S": (0.0, -1.0),
    "E": (1.0, 0.0),
    "W": (-1.0, 0.0),
}

# A side letter within this many degrees of the chain's own bearing tells us
# nothing about which curb is meant; 20 degrees off perpendicular still does.
SIDE_AMBIGUOUS_COS = math.cos(math.radians(70.0))

# Feet along the chain used to read its local direction when offsetting to the curb.
_TANGENT_STEP_FT = 1.0

# Published-coordinate agreement: inside half a wide street plus GPS noise is
# full credit, and credit decays to nothing by the length of a short block.
COORD_AGREEMENT_FT = 75.0
COORD_DISAGREEMENT_FT = 400.0

# Weights of the four `snap_confidence` inputs (SPEC §B.4). Name quality leads
# because a wrong name puts the sign on the wrong street entirely, while a
# published coordinate is missing often enough that it cannot carry much.
_WEIGHT_NAME = 0.45
_WEIGHT_CHAIN = 0.20
_WEIGHT_DISTANCE = 0.20
_WEIGHT_COORD = 0.15

# Credit a row with no published coordinate gets: neither corroborated nor
# contradicted, so slightly below a coordinate that agrees.
_COORD_UNKNOWN_QUALITY = 0.7

_NAME_QUALITY: dict[NameMatch, float] = {
    NameMatch.EXACT: 1.0,
    NameMatch.ALIAS: 0.95,
    # A `DEAD END` resolved by walking the street's own chain is a geometric
    # inference, not a name match: the block is right whenever the street really
    # does stop there, so the penalty is modest (docs/VALIDATION.md §4 D2).
    NameMatch.DEAD_END: 0.85,
    NameMatch.FUZZY: 0.75,
    # A cross street CSCL does not carry at all: the block was inferred from the
    # other corner plus the published coordinate, which lands the whole row in
    # the 0.6-ish tier the UI already shows as low (docs/VALIDATION.md §4 D4).
    NameMatch.INFERRED: 0.35,
    NameMatch.NOT_A_STREET: 0.0,
    NameMatch.MISSING: 0.0,
}

# SPEC §11 wants a data gap told apart from a matching gap, which needs the
# fine-grained miss reasons collapsed into these three buckets.
_REASON_CLASSES: dict[str, str] = {
    "on_street_not_in_centerline": "no_name_match",
    "cross_street_not_in_centerline": "no_name_match",
    "cross_street_does_not_meet_on_street": "no_chain",
    "no_chain_between_nodes": "no_chain",
    "cross_street_is_dead_end": "other",
}


@dataclass(frozen=True)
class SnapResult:
    """Where one sign landed, and every reason the answer might be wrong."""

    sign: StagedSign
    block: BlockMatch | None
    segment_id: str | None
    side: str
    distance_ft: float
    """Feet along the chain from the `from_street` node, clamped to the chain."""
    derived_lon: float | None
    derived_lat: float | None
    snap_confidence: float
    snap_notes: tuple[str, ...]
    reason: str
    published_offset_ft: float | None
    distance_clamped: bool = False
    side_ambiguous: bool = False

    @property
    def matched(self) -> bool:
        return self.block is not None

    @property
    def reason_class(self) -> str:
        return _REASON_CLASSES.get(self.reason, "other")

    @property
    def notes_text(self) -> str:
        return "; ".join(self.snap_notes)


@dataclass(frozen=True)
class BlockfaceCoverage:
    """Matched/unmatched tally for one `(on, from, to, side)` group (docs/DATA.md §1.8)."""

    on_street: str
    from_street: str
    to_street: str
    side: str
    signs: int
    matched: int
    reason: str
    reason_class: str


@dataclass(frozen=True)
class SnapReport:
    """Coverage numbers for `sync_meta`, which is where SPEC §11's question is answered."""

    signs: int
    matched: int
    single_segment: int
    chain_walk: int
    ambiguous_chain: int
    unmatched: int
    unmatched_reasons: dict[str, int]
    unmatched_reason_classes: dict[str, int]
    blockface_sides: int
    blockface_sides_matched: int
    blockface_side_reason_classes: dict[str, int]
    distance_clamped: int
    side_ambiguous: int
    mean_confidence: float

    @property
    def matched_share(self) -> float:
        return self.matched / self.signs if self.signs else 0.0

    @property
    def blockface_side_share(self) -> float:
        return self.blockface_sides_matched / self.blockface_sides if self.blockface_sides else 0.0


def name_quality(match: NameMatch) -> float:
    """How much a street-name match is worth to a confidence score, in [0, 1].

    The one table, shared with the meter join, so a new `NameMatch` value cannot
    be scored two ways or forgotten by one of them.
    """
    return _NAME_QUALITY[match]


def side_offset_sign(line_ft: LineString, side: str) -> tuple[int, bool]:
    """Which side of the directed chain the compass letter names.

    Returns (+1 for the left-hand side of travel, -1 for the right, and whether
    the letter was too close to the chain's own bearing to mean anything). The
    rule: take the chain's overall bearing from the `from_street` node to the
    `to_street` node, rotate it 90 degrees left to get the left-hand normal, and
    keep the side whose normal points the compass way. docs/DATA.md §1.6 shows
    `side_of_street` is always perpendicular to the street's axis, so the
    overall bearing is the right thing to compare against rather than the local
    tangent, which wobbles on curves.
    """
    start = line_ft.coords[0]
    end = line_ft.coords[-1]
    dx, dy = end[0] - start[0], end[1] - start[1]
    span = math.hypot(dx, dy)
    if span == 0:
        return (1, True)
    left_normal = (-dy / span, dx / span)
    compass = COMPASS_UNITS[side]
    alignment = left_normal[0] * compass[0] + left_normal[1] * compass[1]
    ambiguous = abs(alignment) < SIDE_AMBIGUOUS_COS
    return (1 if alignment >= 0 else -1, ambiguous)


def curb_point_ft(
    line_ft: LineString, distance_ft: float, offset_ft: float, offset_sign: int
) -> tuple[float, float]:
    """Point `offset_ft` perpendicular to the chain at `distance_ft` along it.

    SPEC §B.3: DOT publishes no curb-edge line, so the curb is modelled as the
    centerline offset by half the roadbed width.
    """
    clamped = min(max(distance_ft, 0.0), line_ft.length)
    here = line_ft.interpolate(clamped)
    ahead_at = min(clamped + _TANGENT_STEP_FT, line_ft.length)
    behind_at = max(ahead_at - _TANGENT_STEP_FT, 0.0)
    ahead = line_ft.interpolate(ahead_at)
    behind = line_ft.interpolate(behind_at)
    dx, dy = ahead.x - behind.x, ahead.y - behind.y
    span = math.hypot(dx, dy)
    if span == 0:
        return (float(here.x), float(here.y))
    normal = (-dy / span * offset_sign, dx / span * offset_sign)
    return (float(here.x + normal[0] * offset_ft), float(here.y + normal[1] * offset_ft))


def snap_sign(sign: StagedSign, graph: StreetGraph) -> SnapResult:
    """Locate one sign on the curb, with a confidence in [0, 1] and notes on what hurt it."""
    lookup = graph.find_block_detail(
        sign.on_street, sign.from_street, sign.to_street, near_ft=_published_point_ft(sign)
    )
    block = lookup.match
    if block is None:
        return SnapResult(
            sign=sign,
            block=None,
            segment_id=None,
            side=sign.side_of_street,
            distance_ft=sign.distance_from_intersection_ft,
            derived_lon=None,
            derived_lat=None,
            snap_confidence=0.0,
            snap_notes=(f"unmatched: {lookup.reason}",),
            reason=lookup.reason,
            published_offset_ft=None,
        )

    notes: list[str] = []
    best_name_quality = min(
        name_quality(lookup.on.match),
        name_quality(lookup.from_.match),
        name_quality(lookup.to.match),
    )
    for label, name in (("on", lookup.on), ("from", lookup.from_), ("to", lookup.to)):
        if name.match is NameMatch.FUZZY:
            notes.append(f"{label}_street matched by spelling similarity to {name.norm}")
        elif name.match is NameMatch.ALIAS:
            notes.append(f"{label}_street matched through the alias table as {name.norm}")
        elif name.match is NameMatch.DEAD_END:
            notes.append(
                f"{label}_street is a dead end; used the terminal node of {name.norm}'s own chain"
            )
        elif name.match is NameMatch.INFERRED:
            notes.append(
                f"{label}_street {name.norm} is in no centerline row; took the block one segment"
                " from the other corner"
            )

    chain_quality = 1.0
    if not block.is_unique:
        chain_quality = 0.6
        block = _outer_carriageway(lookup.alternatives, sign, notes)
    elif not block.is_single_segment:
        chain_quality = 0.9
        notes.append(f"block spans {len(block.segments)} centerline segments")

    distance_ft, distance_quality = _clamped_distance(
        sign.distance_from_intersection_ft, block.length_ft, notes
    )
    offset_sign, side_ambiguous = side_offset_sign(block.line_ft, sign.side_of_street)
    if side_ambiguous:
        notes.append(f"side {sign.side_of_street} is nearly parallel to the block's bearing")
    x_ft, y_ft = curb_point_ft(block.line_ft, distance_ft, block.width_ft / 2.0, offset_sign)
    if block.width_defaulted:
        notes.append("street width defaulted to the Manhattan median 34 ft")

    published_offset, coord_quality = _coordinate_agreement(sign, x_ft, y_ft, notes)
    confidence = (
        _WEIGHT_NAME * best_name_quality
        + _WEIGHT_CHAIN * chain_quality
        + _WEIGHT_DISTANCE * distance_quality
        + _WEIGHT_COORD * coord_quality
    )
    if side_ambiguous:
        confidence *= 0.8
    lon, lat = feet_to_lonlat(x_ft, y_ft)
    return SnapResult(
        sign=sign,
        block=block,
        segment_id=block.segment_at(distance_ft).segment_id,
        side=sign.side_of_street,
        distance_ft=distance_ft,
        derived_lon=lon,
        derived_lat=lat,
        snap_confidence=round(min(max(confidence, 0.0), 1.0), 4),
        snap_notes=tuple(notes),
        reason="matched",
        published_offset_ft=published_offset,
        distance_clamped=sign.distance_from_intersection_ft > block.length_ft,
        side_ambiguous=side_ambiguous,
    )


def _published_point_ft(sign: StagedSign) -> tuple[float, float] | None:
    """The sign's published EPSG:2263 point, when DOT gave one (docs/DATA.md §1.7)."""
    if sign.sign_x_coord is None or sign.sign_y_coord is None:
        return None
    return (sign.sign_x_coord, sign.sign_y_coord)


def side_normal(line_ft: LineString, side: str) -> tuple[float, float]:
    """Unit vector from the chain towards the curb the side letter names."""
    offset_sign, _ = side_offset_sign(line_ft, side)
    start, end = line_ft.coords[0], line_ft.coords[-1]
    dx, dy = end[0] - start[0], end[1] - start[1]
    span = math.hypot(dx, dy)
    if span == 0:
        return (0.0, 0.0)
    return (-dy / span * offset_sign, dx / span * offset_sign)


def _outer_carriageway(
    candidates: Sequence[BlockMatch], sign: StagedSign, notes: list[str]
) -> BlockMatch:
    """Pick between equally short chains: the one whose named curb faces outwards.

    CSCL models a divided roadway as two parallel centerlines, and both spell an
    equally short chain between the same pair of nodes. A sign whose
    `side_of_street` is W belongs to the west curb of the *west* carriageway, so
    the right chain is the one for which offsetting towards the named side moves
    *away* from its sibling. That is geometric, not compass-based, so it holds on
    Manhattan's tilted grid. The published coordinate only breaks a remaining tie
    (SPEC §B.1). Before this, the walk took whichever chain it saw first and put
    7.2% of snapped rows at risk of half a roadway width
    (docs/VALIDATION.md §4 D3).
    """
    notes.append(f"{len(candidates)} equally short chains span this block")
    scored = []
    for index, candidate in enumerate(candidates):
        centroid = candidate.line_ft.interpolate(0.5, normalized=True)
        sibling = _nearest_sibling(candidates, index)
        normal = side_normal(candidate.line_ft, sign.side_of_street)
        dx, dy = sibling.x - centroid.x, sibling.y - centroid.y
        span = math.hypot(dx, dy) or 1.0
        facing = normal[0] * dx / span + normal[1] * dy / span
        scored.append((round(facing, 6), _published_distance_ft(sign, centroid), index, candidate))
    scored.sort()
    chosen = scored[0]
    if chosen[0] < scored[-1][0]:
        notes.append(
            f"tie broken by side: the {sign.side_of_street} curb of this chain faces away"
            " from the parallel carriageway"
        )
    else:
        notes.append("tie broken by the published coordinate; the side letter did not separate")
    return chosen[3]


def _nearest_sibling(candidates: Sequence[BlockMatch], index: int) -> Point:
    """Centroid of the competing chain closest to this one."""
    here = candidates[index].line_ft.interpolate(0.5, normalized=True)
    others = [
        other.line_ft.interpolate(0.5, normalized=True)
        for position, other in enumerate(candidates)
        if position != index
    ]
    return min(others, key=here.distance)


def _published_distance_ft(sign: StagedSign, centroid: Point) -> float:
    """Feet from the sign's published point to a chain's midpoint, or 0 with no point."""
    if sign.sign_x_coord is None or sign.sign_y_coord is None:
        return 0.0
    return math.hypot(centroid.x - sign.sign_x_coord, centroid.y - sign.sign_y_coord)


def snap_signs(
    signs: Iterable[StagedSign], graph: StreetGraph
) -> tuple[list[SnapResult], SnapReport]:
    """Snap every sign and summarize coverage per sign and per blockface-side."""
    results = [snap_sign(sign, graph) for sign in signs]
    return results, summarize(results)


def blockface_coverage(results: Sequence[SnapResult]) -> list[BlockfaceCoverage]:
    """Per `(on, from, to, side)` group: how many signs matched, and why the rest did not.

    This is the number SPEC §11 asks for. A group with zero signs in the source
    never appears here at all, which is exactly what makes a data gap
    distinguishable from a matching gap.
    """
    groups: dict[tuple[str, str, str, str], list[SnapResult]] = {}
    for result in results:
        groups.setdefault(result.sign.blockface_key, []).append(result)
    coverage = []
    for key, members in groups.items():
        matched = sum(1 for member in members if member.matched)
        first_miss = next((m for m in members if not m.matched), None)
        reason = "matched" if first_miss is None else first_miss.reason
        reason_class = "matched" if first_miss is None else first_miss.reason_class
        coverage.append(
            BlockfaceCoverage(
                on_street=key[0],
                from_street=key[1],
                to_street=key[2],
                side=key[3],
                signs=len(members),
                matched=matched,
                reason=reason,
                reason_class=reason_class,
            )
        )
    return coverage


def summarize(results: Sequence[SnapResult]) -> SnapReport:
    """Roll snap results up into the counts `build` writes to `sync_meta`."""
    counts = _Counts()
    for result in results:
        counts.add(result)
    coverage = blockface_coverage(results)
    face_classes: dict[str, int] = {}
    faces_matched = 0
    for face in coverage:
        if face.matched:
            faces_matched += 1
        face_classes[face.reason_class] = face_classes.get(face.reason_class, 0) + 1
    return SnapReport(
        signs=len(results),
        matched=counts.matched,
        single_segment=counts.single_segment,
        chain_walk=counts.chain_walk,
        ambiguous_chain=counts.ambiguous,
        unmatched=len(results) - counts.matched,
        unmatched_reasons=dict(sorted(counts.reasons.items())),
        unmatched_reason_classes=dict(sorted(counts.reason_classes.items())),
        blockface_sides=len(coverage),
        blockface_sides_matched=faces_matched,
        blockface_side_reason_classes=dict(sorted(face_classes.items())),
        distance_clamped=counts.clamped,
        side_ambiguous=counts.side_ambiguous,
        mean_confidence=round(counts.confidence / counts.matched, 4) if counts.matched else 0.0,
    )


@dataclass
class _Counts:
    matched: int = 0
    single_segment: int = 0
    chain_walk: int = 0
    ambiguous: int = 0
    clamped: int = 0
    side_ambiguous: int = 0
    confidence: float = 0.0
    reasons: dict[str, int] = field(default_factory=dict)
    reason_classes: dict[str, int] = field(default_factory=dict)

    def add(self, result: SnapResult) -> None:
        if result.block is None:
            self.reasons[result.reason] = self.reasons.get(result.reason, 0) + 1
            key = result.reason_class
            self.reason_classes[key] = self.reason_classes.get(key, 0) + 1
            return
        self.matched += 1
        self.confidence += result.snap_confidence
        if not result.block.is_unique:
            self.ambiguous += 1
        elif result.block.is_single_segment:
            self.single_segment += 1
        else:
            self.chain_walk += 1
        if result.distance_clamped:
            self.clamped += 1
        if result.side_ambiguous:
            self.side_ambiguous += 1


def _clamped_distance(
    distance_ft: float, chain_length_ft: float, notes: list[str]
) -> tuple[float, float]:
    """Keep the post on the chain, losing confidence in proportion to the overrun.

    0.25% of matched rows claim a distance longer than the block they matched
    (docs/DATA.md §2.4); those are DOT descriptions that span more than the two
    cross streets name, so the post belongs at the far corner, not past it.
    """
    if chain_length_ft <= 0:
        notes.append("distance not checkable: matched chain has zero length")
        return (0.0, 0.0)
    if distance_ft <= chain_length_ft:
        return (distance_ft, 1.0)
    overrun = distance_ft - chain_length_ft
    notes.append(
        f"distance {distance_ft:.0f} ft overruns the {chain_length_ft:.0f} ft block; "
        "clamped to the far corner"
    )
    return (chain_length_ft, max(0.0, 1.0 - overrun / chain_length_ft))


def _coordinate_agreement(
    sign: StagedSign, x_ft: float, y_ft: float, notes: list[str]
) -> tuple[float | None, float]:
    """Compare the derived curb point with the published one, in feet.

    Both are EPSG:2263, so the comparison needs no reprojection; the derived
    point is converted to lon/lat only for storage. The published pair never
    moves the sign, it only raises or lowers confidence (SPEC §B.1).
    """
    if sign.sign_x_coord is None or sign.sign_y_coord is None:
        return (None, _COORD_UNKNOWN_QUALITY)
    offset = math.hypot(x_ft - sign.sign_x_coord, y_ft - sign.sign_y_coord)
    if offset <= COORD_AGREEMENT_FT:
        return (offset, 1.0)
    if offset >= COORD_DISAGREEMENT_FT:
        notes.append(f"published coordinate is {offset:.0f} ft from the derived point")
        return (offset, 0.0)
    span = COORD_DISAGREEMENT_FT - COORD_AGREEMENT_FT
    notes.append(f"published coordinate is {offset:.0f} ft from the derived point")
    return (offset, (COORD_DISAGREEMENT_FT - offset) / span)
