"""Street-name normalization and the centerline graph the snap step walks.

The sign dataset spells names out ("EAST   85 STREET") while the centerline
abbreviates them ("E  85 ST"), so both sides go through `normalize_street_name`
before they are compared; docs/DATA.md §1.9 measures that at 99.74% of name
references. The graph itself is rebuilt from shared endpoint coordinates,
because the Socrata export of CSCL drops LION's node ids (docs/DECISIONS.md D1,
docs/DATA.md §2.3).
"""

from __future__ import annotations

import difflib
import logging
import re
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pyproj import Transformer
from shapely.geometry import LineString, MultiLineString, shape
from shapely.ops import linemerge, transform

from curbcheck.etl.stage import RawCenterlineRow

LOGGER = logging.getLogger(__name__)

# Suffix and directional words, sign spelling -> centerline spelling.
WORD_FORMS: dict[str, str] = {
    "STREET": "ST",
    "AVENUE": "AVE",
    "BOULEVARD": "BLVD",
    "PLACE": "PL",
    "DRIVE": "DR",
    "DRIVEWAY": "DR",
    "PARKWAY": "PKWY",
    "TERRACE": "TER",
    "SQUARE": "SQ",
    "ROAD": "RD",
    "LANE": "LN",
    "COURT": "CT",
    "ALLEY": "ALY",
    "PLAZA": "PLZ",
    "BRIDGE": "BRG",
    "EXPRESSWAY": "EXPY",
    "HIGHWAY": "HWY",
    "CIRCLE": "CIR",
    "TUNNEL": "TUNL",
    "EAST": "E",
    "WEST": "W",
    "NORTH": "N",
    "SOUTH": "S",
    "FT": "FORT",
    "SAINT": "ST",
    "MT": "MOUNT",
    "FIRST": "1",
    "SECOND": "2",
    "THIRD": "3",
    "FOURTH": "4",
    "FIFTH": "5",
    "SIXTH": "6",
    "SEVENTH": "7",
    "EIGHTH": "8",
    "NINTH": "9",
    "TENTH": "10",
    "ELEVENTH": "11",
    "TWELFTH": "12",
}

# Whole-name aliases the word map cannot reach, measured from the residue in
# data/explore/street_names.txt. Keys and values are already in word-normalized
# form. Three kinds live here: honorific renamings the two datasets disagree
# about, DOT shorthands, and outright typos in the sign data.
NAME_ALIASES: dict[str, str] = {
    # Honorific renamings: the sign data uses one official name, CSCL the other.
    "6 AVE": "AVE OF THE AMERICAS",
    "AVE OF AMERICAS": "AVE OF THE AMERICAS",
    "MALCOLM X BLVD": "LENOX AVE",
    "W 110 ST": "CATHEDRAL PKWY",
    "WILLETT ST": "BIALYSTOKER PL",
    "ADAM C POWELL BLVD": "ADAM CLAYTON POWELL JR BLVD",
    "ADAM CLAYTON POWELL BLVD": "ADAM CLAYTON POWELL JR BLVD",
    "A C POWELL BLVD": "ADAM CLAYTON POWELL JR BLVD",
    "FRED DOUGLASS BLVD": "FREDERICK DOUGLASS BLVD",
    "FRED DOUGLAS BLVD": "FREDERICK DOUGLASS BLVD",
    "FREDERICK DOUGLAS BLVD": "FREDERICK DOUGLASS BLVD",
    "FRED DOUGLASS CIR": "FREDERICK DOUGLASS CIR",
    # DOT shorthands and initialisms.
    "FDR DR": "FRANKLIN D ROOSEVELT DR",
    "FDR DRIVE": "FRANKLIN D ROOSEVELT DR",
    "F D R DR": "FRANKLIN D ROOSEVELT DR",
    "G WASHINGTON BRG": "GEORGE WASHINGTON BRG",
    "QUEENSBORO BRG": "ED KOCH QUEENSBORO BRG",
    "N D PERLMAN PL": "NATHAN D PERLMAN PL",
    "ROBERT F WAGNER PL": "R F WAGNER SR PL",
    "CORBIN DR": "MARGARET CORBIN DR",
    # Word-spacing disagreements between the two datasets.
    "MACDOUGAL ST": "MAC DOUGAL ST",
    "MACDOUGAL ALY": "MAC DOUGAL ALY",
    "LAGUARDIA PL": "LA GUARDIA PL",
    "LASALLE ST": "LA SALLE ST",
    "VAN DAM ST": "VANDAM ST",
    # Typos in the sign data.
    "BLEEKER ST": "BLEECKER ST",
    "CUMMINGS ST": "CUMMING ST",
    "THEATRE ALY": "THEATER ALY",
    "MARGRET CORBIN DR": "MARGARET CORBIN DR",
}

# Values DOT writes into from_street/to_street where no cross street exists.
NON_STREET_ENDPOINTS = frozenset({"DEAD END", "END", "CUL DE SAC"})

# Signs sit on highways and bridges too (FDR Drive is rw_type 2 and 3), so the
# candidate pool is wider than the plain street network (docs/DATA.md §2.1).
SNAPPABLE_RW_TYPES = frozenset({"1", "2", "3", "10"})

# docs/DATA.md §2.2: streetwidth is null on 181 of 9,297 rw_type=1 rows. The
# median Manhattan street is 34 ft wide, which is what those fall back to.
DEFAULT_STREET_WIDTH_FT = 34.0

# Endpoints are compared at 1e-7 degrees (~1.1 cm): finer than the coordinates
# DCP publishes, coarser than float formatting noise (docs/DATA.md §2.3).
COORD_PLACES = 7

# A DOT blockface can span several centerline segments (16.8% of sign rows,
# docs/DATA.md §2.4). The longest real case is Riverside Dr across ~16 blocks;
# this bound keeps a bad name match from walking the length of Broadway.
MAX_CHAIN_SEGMENTS = 30

# difflib ratio above which a street name with no exact or aliased match is
# accepted as a misspelling. 0.92 admits FORSYTHE ST -> FORSYTH ST and rejects
# E 85 ST -> E 86 ST (0.86), which is the pair that must never be confused.
FUZZY_NAME_CUTOFF = 0.92

_PUNCTUATION = re.compile(r"[.,]")
_WHITESPACE = re.compile(r"\s+")

_TO_FEET = Transformer.from_crs("EPSG:4326", "EPSG:2263", always_xy=True)
_TO_DEGREES = Transformer.from_crs("EPSG:2263", "EPSG:4326", always_xy=True)


class NameMatch(StrEnum):
    """How a sign's street name reached a centerline name. Feeds `snap_confidence`."""

    EXACT = "exact"
    ALIAS = "alias"
    FUZZY = "fuzzy"
    NOT_A_STREET = "not_a_street"
    MISSING = "missing"


def normalize_street_name(raw: str) -> str:
    """Canonical uppercase form: collapsed whitespace, abbreviated words, aliases applied.

    Applied to both sides of the sign/centerline join. Idempotent, so it is safe
    to call on an already-normalized name.
    """
    words = _normalize_words(raw)
    return NAME_ALIASES.get(words, words)


def _normalize_words(raw: str) -> str:
    """`normalize_street_name` without the alias table, so the two can be told apart."""
    # A few rows qualify the roadway after an asterisk ("PARK AVENUE*WEST RDWY");
    # the qualifier duplicates the on_street_suffix column, so drop it.
    text = _PUNCTUATION.sub("", str(raw).split("*")[0]).upper()
    text = _WHITESPACE.sub(" ", text).strip()
    return " ".join(WORD_FORMS.get(token, token) for token in text.split(" ") if token)


def to_feet(geometry: Any) -> Any:
    """Reproject a WGS-84 geometry into EPSG:2263, whose unit is the US survey foot."""
    return transform(_TO_FEET.transform, geometry)


def to_degrees(geometry: Any) -> Any:
    """Reproject an EPSG:2263 geometry back to WGS-84 lon/lat."""
    return transform(_TO_DEGREES.transform, geometry)


def feet_to_lonlat(x_ft: float, y_ft: float) -> tuple[float, float]:
    lon, lat = _TO_DEGREES.transform(x_ft, y_ft)
    return (float(lon), float(lat))


def lonlat_to_feet(lon: float, lat: float) -> tuple[float, float]:
    x_ft, y_ft = _TO_FEET.transform(lon, lat)
    return (float(x_ft), float(y_ft))


def node_id_for(lon: float, lat: float) -> str:
    """Identity of an intersection: its endpoint coordinate rounded to 1e-7 degrees."""
    return f"{round(lon, COORD_PLACES):.7f},{round(lat, COORD_PLACES):.7f}"


@dataclass(frozen=True)
class StreetNode:
    """An endpoint coordinate shared by one or more centerline segments."""

    node_id: str
    lon: float
    lat: float
    street_norms: frozenset[str]
    segment_ids: tuple[str, ...]


@dataclass(frozen=True)
class StreetSegment:
    """One centerline segment, keyed by `physicalid`, with geometry in both CRSs."""

    segment_id: str
    street_name: str
    street_norm: str
    rw_type: str
    from_node: str
    to_node: str
    line_deg: LineString
    line_ft: LineString
    length_ft: float
    width_ft: float
    width_defaulted: bool
    left_low_address: str | None = None
    left_high_address: str | None = None
    right_low_address: str | None = None
    right_high_address: str | None = None

    def other_end(self, node_id: str) -> str:
        return self.to_node if node_id == self.from_node else self.from_node


@dataclass(frozen=True)
class BlockMatch:
    """The ordered run of same-street segments from the `from_street` node to the `to_street` node.

    `line_ft` is oriented so distance 0 is the `from_street` node, which is where
    DOT measures `distance_from_intersection` from.
    """

    street_norm: str
    segments: tuple[StreetSegment, ...]
    from_node: str
    to_node: str
    line_ft: LineString
    length_ft: float
    width_ft: float
    width_defaulted: bool
    chain_count: int
    """Distinct shortest chains between the two nodes. >1 means the block is ambiguous."""

    @property
    def is_unique(self) -> bool:
        return self.chain_count == 1

    @property
    def is_single_segment(self) -> bool:
        return len(self.segments) == 1

    @property
    def segment_id(self) -> str:
        """The segment a chain is filed under: the one at the `from_street` end."""
        return self.segments[0].segment_id

    def segment_at(self, distance_ft: float) -> StreetSegment:
        """The segment covering a distance measured from the `from_street` node."""
        travelled = 0.0
        for segment in self.segments:
            travelled += segment.length_ft
            if distance_ft <= travelled:
                return segment
        return self.segments[-1]


@dataclass(frozen=True)
class StreetNameLookup:
    """Result of resolving one sign street name against the graph."""

    norm: str
    match: NameMatch

    @property
    def found(self) -> bool:
        return self.match in (NameMatch.EXACT, NameMatch.ALIAS, NameMatch.FUZZY)


@dataclass(frozen=True)
class BlockLookup:
    """A `find_block` result together with why it failed, for the coverage report."""

    match: BlockMatch | None
    reason: str
    on: StreetNameLookup
    from_: StreetNameLookup
    to: StreetNameLookup


@dataclass(frozen=True)
class GraphReport:
    """What the centerline snapshot yielded, for `sync_meta`."""

    source_rows: int
    segments: int
    nodes: int
    skipped_rw_type: int
    skipped_no_geometry: int
    non_contiguous_multiparts: int
    width_defaulted: int


class StreetGraph:
    """Centerline segments plus the intersection graph rebuilt from shared endpoints."""

    def __init__(self, segments: Sequence[StreetSegment]) -> None:
        self.segments: dict[str, StreetSegment] = {s.segment_id: s for s in segments}
        self.report = GraphReport(
            source_rows=len(segments),
            segments=len(segments),
            nodes=0,
            skipped_rw_type=0,
            skipped_no_geometry=0,
            non_contiguous_multiparts=0,
            width_defaulted=sum(1 for s in segments if s.width_defaulted),
        )
        self._by_street: dict[str, list[str]] = {}
        node_members: dict[str, list[str]] = {}
        node_coords: dict[str, tuple[float, float]] = {}
        self._incident: dict[tuple[str, str], list[str]] = {}
        for segment in segments:
            self._by_street.setdefault(segment.street_norm, []).append(segment.segment_id)
            for node_id, coord in (
                (segment.from_node, segment.line_deg.coords[0]),
                (segment.to_node, segment.line_deg.coords[-1]),
            ):
                node_members.setdefault(node_id, []).append(segment.segment_id)
                node_coords.setdefault(node_id, (float(coord[0]), float(coord[1])))
                self._incident.setdefault((segment.street_norm, node_id), []).append(
                    segment.segment_id
                )
        self.nodes: dict[str, StreetNode] = {
            node_id: StreetNode(
                node_id=node_id,
                lon=node_coords[node_id][0],
                lat=node_coords[node_id][1],
                street_norms=frozenset(self.segments[s].street_norm for s in members),
                segment_ids=tuple(sorted(set(members))),
            )
            for node_id, members in node_members.items()
        }
        self._street_nodes: dict[str, list[str]] = {}
        for street_norm, node_id in self._incident:
            self._street_nodes.setdefault(street_norm, []).append(node_id)
        for node_ids in self._street_nodes.values():
            node_ids.sort()
        self._street_names: tuple[str, ...] = tuple(sorted(self._by_street))
        self._fuzzy_cache: dict[str, str | None] = {}
        self._block_cache: dict[tuple[str, str, str], BlockLookup] = {}

    @property
    def street_names(self) -> tuple[str, ...]:
        return self._street_names

    def segments_for_street(self, norm_name: str) -> tuple[StreetSegment, ...]:
        return tuple(self.segments[s] for s in self._by_street.get(norm_name, ()))

    def node_streets(self, node_id: str) -> set[str]:
        node = self.nodes.get(node_id)
        return set(node.street_norms) if node else set()

    def resolve_street(self, raw_name: str) -> StreetNameLookup:
        """Map a sign's street name onto a centerline name, recording how it got there."""
        words = _normalize_words(raw_name)
        if words in NON_STREET_ENDPOINTS:
            return StreetNameLookup(norm=words, match=NameMatch.NOT_A_STREET)
        if words in self._by_street:
            return StreetNameLookup(norm=words, match=NameMatch.EXACT)
        aliased = NAME_ALIASES.get(words, words)
        if aliased in self._by_street:
            return StreetNameLookup(norm=aliased, match=NameMatch.ALIAS)
        close = self._closest_name(aliased)
        if close is not None:
            return StreetNameLookup(norm=close, match=NameMatch.FUZZY)
        return StreetNameLookup(norm=aliased, match=NameMatch.MISSING)

    def find_block(self, on: str, from_: str, to: str) -> BlockMatch | None:
        """The chain of `on` segments running from the `from_` node to the `to` node.

        A single segment spanning the block is the common case (73% of sign rows),
        but 16.8% need the chain walk, so both go through the same search.
        """
        return self.find_block_detail(on, from_, to).match

    def find_block_detail(self, on: str, from_: str, to: str) -> BlockLookup:
        """`find_block` plus the reason for a miss, which the coverage report needs."""
        key = (on, from_, to)
        cached = self._block_cache.get(key)
        if cached is not None:
            return cached
        lookup = self._find_block_uncached(on, from_, to)
        self._block_cache[key] = lookup
        return lookup

    def _find_block_uncached(self, on: str, from_: str, to: str) -> BlockLookup:
        on_name = self.resolve_street(on)
        from_name = self.resolve_street(from_)
        to_name = self.resolve_street(to)
        if not on_name.found:
            return BlockLookup(None, "on_street_not_in_centerline", on_name, from_name, to_name)
        if from_name.match is NameMatch.NOT_A_STREET or to_name.match is NameMatch.NOT_A_STREET:
            return BlockLookup(None, "cross_street_is_dead_end", on_name, from_name, to_name)
        if not from_name.found or not to_name.found:
            return BlockLookup(None, "cross_street_not_in_centerline", on_name, from_name, to_name)

        from_nodes = self._nodes_where(on_name.norm, from_name.norm)
        to_nodes = self._nodes_where(on_name.norm, to_name.norm)
        if not from_nodes or not to_nodes:
            return BlockLookup(
                None, "cross_street_does_not_meet_on_street", on_name, from_name, to_name
            )
        chain = self._shortest_chain(on_name.norm, from_nodes, to_nodes)
        if chain is None:
            return BlockLookup(None, "no_chain_between_nodes", on_name, from_name, to_name)
        segment_ids, start_node, end_node, chain_count = chain
        match = self._build_block(on_name.norm, segment_ids, start_node, end_node, chain_count)
        return BlockLookup(match, "matched", on_name, from_name, to_name)

    def _nodes_where(self, street_norm: str, cross_norm: str) -> list[str]:
        """Nodes on `street_norm` that a segment of `cross_norm` also terminates at."""
        return [
            node_id
            for node_id in self._street_nodes.get(street_norm, ())
            if cross_norm in self.nodes[node_id].street_norms
        ]

    def _shortest_chain(
        self, street_norm: str, from_nodes: Sequence[str], to_nodes: Sequence[str]
    ) -> tuple[list[str], str, str, int] | None:
        """Breadth-first walk over one street's segments, counting distinct shortest chains.

        The count is what tells a clean block from an ambiguous one: two parallel
        segments between the same pair of nodes (a divided roadway) both spell a
        shortest chain, and the caller must lose confidence rather than pick
        silently.
        """
        targets = set(to_nodes)
        distance: dict[str, int] = dict.fromkeys(from_nodes, 0)
        path_count: dict[str, int] = dict.fromkeys(from_nodes, 1)
        parent: dict[str, tuple[str, str]] = {}
        frontier = deque(sorted(from_nodes))
        depth = 0
        while frontier and depth < MAX_CHAIN_SEGMENTS:
            reached: dict[str, int] = {}
            for node_id in frontier:
                for segment_id in self._incident.get((street_norm, node_id), ()):
                    neighbour = self.segments[segment_id].other_end(node_id)
                    if neighbour == node_id or distance.get(neighbour, depth + 1) <= depth:
                        continue
                    reached[neighbour] = reached.get(neighbour, 0) + path_count[node_id]
                    parent.setdefault(neighbour, (node_id, segment_id))
            depth += 1
            for neighbour, count in reached.items():
                distance[neighbour] = depth
                path_count[neighbour] = count
            hits = sorted(node for node in reached if node in targets)
            if hits:
                end_node = hits[0]
                total = sum(path_count[node] for node in hits)
                return (*self._walk_back(end_node, parent), total)
            frontier = deque(sorted(reached))
        return None

    def _walk_back(
        self, end_node: str, parent: dict[str, tuple[str, str]]
    ) -> tuple[list[str], str, str]:
        segment_ids: list[str] = []
        node_id = end_node
        while node_id in parent:
            previous, segment_id = parent[node_id]
            segment_ids.append(segment_id)
            node_id = previous
        segment_ids.reverse()
        return (segment_ids, node_id, end_node)

    def _build_block(
        self,
        street_norm: str,
        segment_ids: Sequence[str],
        start_node: str,
        end_node: str,
        chain_count: int,
    ) -> BlockMatch:
        segments = tuple(self.segments[s] for s in segment_ids)
        coords: list[tuple[float, ...]] = []
        node_id = start_node
        for segment in segments:
            forward = segment.from_node == node_id
            part = list(segment.line_ft.coords) if forward else list(segment.line_ft.coords)[::-1]
            coords.extend(part[1:] if coords else part)
            node_id = segment.other_end(node_id)
        line_ft = LineString(coords)
        length_ft = sum(segment.length_ft for segment in segments)
        # Width is length-weighted: a chain can cross a widening, and the curb
        # offset should follow the part the sign actually sits on more closely.
        width_ft = (
            sum(segment.width_ft * segment.length_ft for segment in segments) / length_ft
            if length_ft
            else DEFAULT_STREET_WIDTH_FT
        )
        return BlockMatch(
            street_norm=street_norm,
            segments=segments,
            from_node=start_node,
            to_node=end_node,
            line_ft=line_ft,
            length_ft=length_ft,
            width_ft=width_ft,
            width_defaulted=any(segment.width_defaulted for segment in segments),
            chain_count=chain_count,
        )

    def _closest_name(self, name: str) -> str | None:
        if name in self._fuzzy_cache:
            return self._fuzzy_cache[name]
        matches = difflib.get_close_matches(name, self._street_names, n=1, cutoff=FUZZY_NAME_CUTOFF)
        best = matches[0] if matches else None
        self._fuzzy_cache[name] = best
        return best


@dataclass
class _GraphBuild:
    segments: list[StreetSegment] = field(default_factory=list)
    skipped_rw_type: int = 0
    skipped_no_geometry: int = 0
    non_contiguous: int = 0
    width_defaulted: int = 0


def build_graph(
    rows: Iterable[RawCenterlineRow], *, rw_types: frozenset[str] = SNAPPABLE_RW_TYPES
) -> StreetGraph:
    """Build the graph from validated centerline rows, keeping only snappable roadways."""
    build = _GraphBuild()
    source_rows = 0
    for row in rows:
        source_rows += 1
        if (row.rw_type or "") not in rw_types:
            build.skipped_rw_type += 1
            continue
        segment = _segment_from_row(row, build)
        if segment is not None:
            build.segments.append(segment)
    graph = StreetGraph(build.segments)
    graph.report = GraphReport(
        source_rows=source_rows,
        segments=len(graph.segments),
        nodes=len(graph.nodes),
        skipped_rw_type=build.skipped_rw_type,
        skipped_no_geometry=build.skipped_no_geometry,
        non_contiguous_multiparts=build.non_contiguous,
        width_defaulted=build.width_defaulted,
    )
    LOGGER.info(
        "streets.graph rows=%d segments=%d nodes=%d non_contiguous=%d width_defaulted=%d",
        source_rows,
        graph.report.segments,
        graph.report.nodes,
        graph.report.non_contiguous_multiparts,
        graph.report.width_defaulted,
    )
    return graph


def _segment_from_row(row: RawCenterlineRow, build: _GraphBuild) -> StreetSegment | None:
    segment_id = (row.physicalid or "").strip()
    name = (row.full_street_name or row.stname_label or "").strip()
    if not segment_id or not name or not row.the_geom:
        build.skipped_no_geometry += 1
        return None
    line_deg = _single_line(row.the_geom, build)
    if line_deg is None:
        return None
    line_ft = to_feet(line_deg)
    width_ft, defaulted = _width_ft(row.streetwidth)
    if defaulted:
        build.width_defaulted += 1
    coords = list(line_deg.coords)
    return StreetSegment(
        segment_id=segment_id,
        street_name=name,
        street_norm=normalize_street_name(name),
        rw_type=(row.rw_type or "").strip(),
        from_node=node_id_for(coords[0][0], coords[0][1]),
        to_node=node_id_for(coords[-1][0], coords[-1][1]),
        line_deg=line_deg,
        line_ft=line_ft,
        # EPSG:2263's unit is the US survey foot, so planar length is feet and
        # `segmentlength` is never used: it is off by >5% on 37% of rows (§2.2).
        length_ft=float(line_ft.length),
        width_ft=width_ft,
        width_defaulted=defaulted,
        left_low_address=_optional(row.l_low_hn),
        left_high_address=_optional(row.l_high_hn),
        right_low_address=_optional(row.r_low_hn),
        right_high_address=_optional(row.r_high_hn),
    )


def _single_line(geometry: dict[str, Any], build: _GraphBuild) -> LineString | None:
    """Flatten a MultiLineString into one LineString, keeping the longest run if it breaks.

    Every Manhattan centerline row merges cleanly today; the fallback exists so a
    future snapshot with a genuinely split segment degrades to its main part
    instead of aborting the build.
    """
    parsed = shape(geometry)
    if not isinstance(parsed, LineString | MultiLineString):
        build.skipped_no_geometry += 1
        return None
    merged = parsed if isinstance(parsed, LineString) else linemerge(parsed)
    if isinstance(merged, LineString):
        if len(merged.coords) >= 2:
            return merged
        build.skipped_no_geometry += 1
        return None
    build.non_contiguous += 1
    parts = list(merged.geoms)
    if not parts:
        build.skipped_no_geometry += 1
        return None
    return max(parts, key=lambda part: part.length)


def _width_ft(value: str | None) -> tuple[float, bool]:
    text = (value or "").strip()
    if text:
        try:
            width = float(text)
        except ValueError:
            width = 0.0
        if width > 0:
            return (width, False)
    return (DEFAULT_STREET_WIDTH_FT, True)


def _optional(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None
