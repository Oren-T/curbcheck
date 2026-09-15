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
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pyproj import Transformer
from shapely.geometry import LineString, MultiLineString, Point, shape
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
    "ROBERT F WAGNER SR PL": "R F WAGNER SR PL",
    "QUEENSBOROUGH BRG": "ED KOCH QUEENSBORO BRG",
    "LUIS MUNOZ MARIN BLVD": "E 116 ST",
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
    "AUDOBON AVE": "AUDUBON AVE",
    "BENSON ST": "BENSON PL",
    # DOT adds a directional to a roadway CSCL carries under one name. Each
    # target was checked against `street_segment.street_norm` before it was
    # added, and the names with no CSCL entry at all — ROCKEFELLER PLZ,
    # COENTIES SLIP, THELONIOUS SPHERE MONK CIR — are deliberately absent
    # (docs/DATA.md §1.9, docs/VALIDATION.md §4 D4).
    "MAIN ST N": "MAIN ST",
    "CENTRAL RD N": "CENTRAL RD",
    "DELANCEY ST N": "DELANCEY ST",
    "DELANCEY ST S": "DELANCEY ST",
}

# Values DOT writes into from_street/to_street where no cross street exists.
# `DEAD END STREET` normalizes to `DEAD END ST` and is written on 8 rows
# (docs/DATA.md §1.9); `DEADEND` is defensive, it is not in today's snapshot.
NON_STREET_ENDPOINTS = frozenset({"DEAD END", "DEAD END ST", "DEADEND", "END", "CUL DE SAC"})

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

# How many equally short chains are kept for the caller to choose between. Two
# is the real case (a divided roadway); the bound stops a pathological node
# layout from unfolding exponentially.
MAX_TIED_CHAINS = 8

# difflib ratio above which a street name with no exact or aliased match is
# accepted as a misspelling. 0.92 admits FORSYTHE ST -> FORSYTH ST and rejects
# E 85 ST -> E 86 ST (0.86), which is the pair that must never be confused.
FUZZY_NAME_CUTOFF = 0.92

# Looser ratio used only to ask "does the corner one block along carry a name
# like the one DOT wrote?", where the alternative is dropping the sign entirely
# and the published coordinate has already had its say.
NEAR_NAME_CUTOFF = 0.8

# Feet per bucket when a published coordinate joins the block-lookup cache key.
# Half a short block: fine enough that two signs in one bucket want the same
# block, coarse enough that the cache still pays.
_COORD_BUCKET_FT = 50.0

_PUNCTUATION = re.compile(r"[.,]")
_WHITESPACE = re.compile(r"\s+")

_TO_FEET = Transformer.from_crs("EPSG:4326", "EPSG:2263", always_xy=True)
_TO_DEGREES = Transformer.from_crs("EPSG:2263", "EPSG:4326", always_xy=True)


class NameMatch(StrEnum):
    """How a sign's street name reached a centerline name. Feeds `snap_confidence`."""

    EXACT = "exact"
    ALIAS = "alias"
    FUZZY = "fuzzy"
    DEAD_END = "dead_end"
    INFERRED = "inferred"
    """The name is in no centerline row; the block was inferred from the other corner."""
    """`DEAD END` resolved to the street's own terminal node by walking the chain."""
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
        return self.match in (
            NameMatch.EXACT,
            NameMatch.ALIAS,
            NameMatch.FUZZY,
            NameMatch.DEAD_END,
            NameMatch.INFERRED,
        )


@dataclass(frozen=True)
class BlockLookup:
    """A `find_block` result together with why it failed, for the coverage report."""

    match: BlockMatch | None
    reason: str
    on: StreetNameLookup
    from_: StreetNameLookup
    to: StreetNameLookup
    alternatives: tuple[BlockMatch, ...] = ()
    """Every equally short chain, `match` first. Longer than one on a divided roadway."""


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
        self._near_block_cache: dict[tuple[str, str, str, int, int], BlockLookup] = {}

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

    def find_block_detail(
        self, on: str, from_: str, to: str, *, near_ft: tuple[float, float] | None = None
    ) -> BlockLookup:
        """`find_block` plus the reason for a miss, which the coverage report needs.

        `near_ft` is the sign's published EPSG:2263 point. It never moves a block
        that the names resolve on their own (SPEC §B.1); it is only consulted
        when one cross street is not in the centerline at all and the block has
        to be inferred from the other corner.
        """
        key = (on, from_, to)
        lookup = self._block_cache.get(key)
        if lookup is None:
            lookup = self._find_block_uncached(on, from_, to)
            self._block_cache[key] = lookup
        if lookup.match is not None or not _half_named(lookup):
            return lookup
        near_key = (*key, *_coordinate_bucket(near_ft))
        inferred = self._near_block_cache.get(near_key)
        if inferred is None:
            inferred = self._one_block_from_known_corner(lookup, near_ft)
            self._near_block_cache[near_key] = inferred
        return inferred

    def _one_block_from_known_corner(
        self, lookup: BlockLookup, near_ft: tuple[float, float] | None
    ) -> BlockLookup:
        """Infer the block when only one of the two cross streets is in the centerline.

        739 sign rows name a cross street CSCL does not carry — a renamed circle,
        a plaza, a ferry terminal (docs/VALIDATION.md §4 D4). The block is then
        one segment away from the corner that *did* resolve, and the only
        question is which way. The published coordinate answers it where DOT
        supplied one; failing that, a far corner whose own names look like the
        missing one answers it; failing both, the sign stays unmatched, because
        a coin flip would put it on the wrong block half the time.
        """
        from_known = lookup.from_.found
        known = lookup.from_ if from_known else lookup.to
        missing = lookup.to if from_known else lookup.from_
        nodes = self._nodes_where(lookup.on.norm, known.norm)
        if not nodes:
            return lookup
        chosen = self._block_towards(lookup.on.norm, nodes, missing.norm, near_ft)
        if chosen is None:
            return lookup
        node_id, segment_id = chosen
        far_node = self.segments[segment_id].other_end(node_id)
        start_node, end_node = (node_id, far_node) if from_known else (far_node, node_id)
        match = self._build_block(lookup.on.norm, [segment_id], start_node, end_node, 1)
        inferred = StreetNameLookup(norm=missing.norm, match=NameMatch.INFERRED)
        return BlockLookup(
            match,
            "matched",
            lookup.on,
            lookup.from_ if from_known else inferred,
            inferred if from_known else lookup.to,
            (match,),
        )

    def _block_towards(
        self,
        street_norm: str,
        nodes: Sequence[str],
        missing_norm: str,
        near_ft: tuple[float, float] | None,
    ) -> tuple[str, str] | None:
        """The one segment off a known corner that the sign's own evidence points at."""
        candidates = [
            (node_id, segment_id)
            for node_id in sorted(nodes)
            for segment_id in sorted(self._incident.get((street_norm, node_id), ()))
        ]
        if not candidates:
            return None
        if near_ft is not None:
            point = Point(*near_ft)
            return min(candidates, key=lambda pair: self.segments[pair[1]].line_ft.distance(point))
        named = [
            pair
            for pair in candidates
            if self._names_like(
                missing_norm, self.nodes[self.segments[pair[1]].other_end(pair[0])].street_norms
            )
        ]
        return named[0] if len(named) == 1 else None

    def _names_like(self, missing_norm: str, far_names: Iterable[str]) -> bool:
        """Whether a corner's own street names look like the name DOT wrote."""
        return any(
            name != missing_norm
            and difflib.SequenceMatcher(None, missing_norm, name).ratio() >= NEAR_NAME_CUTOFF
            for name in far_names
        )

    def _find_block_uncached(self, on: str, from_: str, to: str) -> BlockLookup:
        on_name = self.resolve_street(on)
        from_name = self.resolve_street(from_)
        to_name = self.resolve_street(to)
        if not on_name.found:
            return BlockLookup(None, "on_street_not_in_centerline", on_name, from_name, to_name)
        from_dead = from_name.match is NameMatch.NOT_A_STREET
        to_dead = to_name.match is NameMatch.NOT_A_STREET
        if from_dead and to_dead:
            return BlockLookup(None, "cross_street_is_dead_end", on_name, from_name, to_name)
        if from_dead or to_dead:
            return self._dead_end_block(on_name, from_name, to_name, from_is_dead=from_dead)
        if not from_name.found or not to_name.found:
            return BlockLookup(None, "cross_street_not_in_centerline", on_name, from_name, to_name)

        from_nodes = self._nodes_where(on_name.norm, from_name.norm)
        to_nodes = self._nodes_where(on_name.norm, to_name.norm)
        if not from_nodes or not to_nodes:
            return BlockLookup(
                None, "cross_street_does_not_meet_on_street", on_name, from_name, to_name
            )
        chains = self._shortest_chains(on_name.norm, from_nodes, to_nodes)
        if not chains:
            return BlockLookup(None, "no_chain_between_nodes", on_name, from_name, to_name)
        matches = tuple(
            self._build_block(on_name.norm, segment_ids, start_node, end_node, len(chains))
            for segment_ids, start_node, end_node in chains
        )
        return BlockLookup(matches[0], "matched", on_name, from_name, to_name, matches)

    def _dead_end_block(
        self,
        on_name: StreetNameLookup,
        from_name: StreetNameLookup,
        to_name: StreetNameLookup,
        *,
        from_is_dead: bool,
    ) -> BlockLookup:
        """Resolve a blockface whose one named cross street ends at the street's own end.

        DOT writes `DEAD END` where no cross street exists (604 sign rows over
        147 blockface-sides, docs/VALIDATION.md §4 D2). The end it means is the
        end of the named street's own run, so the chain is walked away from the
        named corner until the street stops: a dangling endpoint, a fork, or the
        point where the name changes. A fork is refused, because which branch
        dead-ends is not knowable from the names alone.
        """
        named = to_name if from_is_dead else from_name
        if not named.found:
            return BlockLookup(None, "cross_street_not_in_centerline", on_name, from_name, to_name)
        named_nodes = self._nodes_where(on_name.norm, named.norm)
        if not named_nodes:
            return BlockLookup(
                None, "cross_street_does_not_meet_on_street", on_name, from_name, to_name
            )
        run = self._best_terminal_run(on_name.norm, named_nodes)
        if run is None:
            return BlockLookup(None, "cross_street_is_dead_end", on_name, from_name, to_name)
        segment_ids, named_node, terminal_node = run
        # `distance_from_intersection` is measured from the from_street end, so
        # the chain has to start there whichever end the dead end is.
        if from_is_dead:
            segment_ids = list(reversed(segment_ids))
            start_node, end_node = terminal_node, named_node
        else:
            start_node, end_node = named_node, terminal_node
        match = self._build_block(on_name.norm, segment_ids, start_node, end_node, 1)
        resolved = StreetNameLookup(norm=named.norm, match=NameMatch.DEAD_END)
        return BlockLookup(
            match,
            "matched",
            on_name,
            resolved if from_is_dead else from_name,
            to_name if from_is_dead else resolved,
            (match,),
        )

    def _best_terminal_run(
        self, street_norm: str, named_nodes: Sequence[str]
    ) -> tuple[list[str], str, str] | None:
        """The shortest walk from a named corner to where the street ends.

        A run ending at a dangling endpoint (degree 1) beats one that merely ran
        into a name change, and a shorter run beats a longer one: a dead-end
        stub is the block next to the corner, not the far end of the street.
        """
        runs = []
        for node_id in sorted(named_nodes):
            for segment_id in sorted(self._incident.get((street_norm, node_id), ())):
                walked = self._walk_to_end(street_norm, node_id, segment_id)
                if walked is None:
                    continue
                segment_ids, terminal_node = walked
                dangling = len(self.nodes[terminal_node].segment_ids) == 1
                length_ft = sum(self.segments[s].length_ft for s in segment_ids)
                runs.append((0 if dangling else 1, length_ft, segment_ids, node_id, terminal_node))
        if not runs:
            return None
        best = min(runs, key=lambda run: (run[0], run[1], run[2]))
        return (best[2], best[3], best[4])

    def _walk_to_end(
        self, street_norm: str, start_node: str, first_segment: str
    ) -> tuple[list[str], str] | None:
        """Follow one street away from a node until it runs out, or refuse at a fork."""
        segment_ids = [first_segment]
        node_id = self.segments[first_segment].other_end(start_node)
        seen = {start_node, node_id}
        while len(segment_ids) < MAX_CHAIN_SEGMENTS:
            onward = [
                segment_id
                for segment_id in self._incident.get((street_norm, node_id), ())
                if segment_id not in segment_ids
            ]
            if not onward:
                return (segment_ids, node_id)
            if len(onward) > 1:
                return None
            neighbour = self.segments[onward[0]].other_end(node_id)
            if neighbour in seen:
                return None
            segment_ids.append(onward[0])
            node_id = neighbour
            seen.add(node_id)
        return None

    def _nodes_where(self, street_norm: str, cross_norm: str) -> list[str]:
        """Nodes on `street_norm` that a segment of `cross_norm` also terminates at."""
        return [
            node_id
            for node_id in self._street_nodes.get(street_norm, ())
            if cross_norm in self.nodes[node_id].street_norms
        ]

    def _shortest_chains(
        self, street_norm: str, from_nodes: Sequence[str], to_nodes: Sequence[str]
    ) -> list[tuple[list[str], str, str]]:
        """Every shortest chain of one street's segments between the two node sets.

        More than one means the block is ambiguous: two parallel segments between
        the same pair of nodes are a divided roadway, and the caller has to pick
        between the carriageways rather than take whichever the walk saw first
        (docs/VALIDATION.md §4 D3).
        """
        targets = set(to_nodes)
        distance: dict[str, int] = dict.fromkeys(from_nodes, 0)
        parents: dict[str, list[tuple[str, str]]] = {}
        frontier = deque(sorted(from_nodes))
        depth = 0
        while frontier and depth < MAX_CHAIN_SEGMENTS:
            reached: dict[str, list[tuple[str, str]]] = {}
            for node_id in frontier:
                for segment_id in self._incident.get((street_norm, node_id), ()):
                    neighbour = self.segments[segment_id].other_end(node_id)
                    if neighbour == node_id or distance.get(neighbour, depth + 1) <= depth:
                        continue
                    reached.setdefault(neighbour, []).append((node_id, segment_id))
            depth += 1
            for neighbour, links in reached.items():
                distance[neighbour] = depth
                parents[neighbour] = sorted(links)
            hits = sorted(node for node in reached if node in targets)
            if hits:
                return [chain for node in hits for chain in self._walk_back_all(node, parents)]
            frontier = deque(sorted(reached))
        return []

    def _walk_back_all(
        self, end_node: str, parents: Mapping[str, list[tuple[str, str]]]
    ) -> list[tuple[list[str], str, str]]:
        """Unfold the BFS parent links into whole chains, newest link first."""
        chains: list[tuple[list[str], str, str]] = []
        stack: list[tuple[list[str], str]] = [([], end_node)]
        while stack and len(chains) < MAX_TIED_CHAINS:
            segment_ids, node_id = stack.pop()
            links = parents.get(node_id)
            if not links:
                chains.append((list(reversed(segment_ids)), node_id, end_node))
                continue
            for previous, segment_id in reversed(links):
                stack.append(([*segment_ids, segment_id], previous))
        return chains

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


def _half_named(lookup: BlockLookup) -> bool:
    """Whether exactly one cross street reached a centerline name."""
    return lookup.reason == "cross_street_not_in_centerline" and (
        lookup.from_.found != lookup.to.found
    )


def _coordinate_bucket(near_ft: tuple[float, float] | None) -> tuple[int, int]:
    if near_ft is None:
        return (0, 0)
    return (int(near_ft[0] // _COORD_BUCKET_FT), int(near_ft[1] // _COORD_BUCKET_FT))


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
