"""The signs behind one curb span, split into the ones that govern it and the rest.

SPEC §10 requires the raw sign text next to every verdict, and the detail panel
used to satisfy it by listing every sign on the parent centerline segment in
`sign_id` order. On the audited block that meant 11 sign cards under a green
badge, 9 of which do not govern the stretch, with `NO STANDING ANYTIME` first
(UX audit P0-2). The signs are the same; what changes here is that the span
says which of them produced its verdict, and in what order they stand along the
curb.

Grouping is allowed, deleting is not: every sign the old list showed for the
blockface-side is still returned, in `other_on_block`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from curbcheck.db import json_string_list
from curbcheck.etl.streets import normalize_street_name

# Written out twice rather than shared through a variable: every SQL string in
# this package is a literal, which is what lets `tests/test_engine_sql_safety.py`
# enforce that none of them is ever built by formatting.
_SEGMENT_SIGNS_SQL = (
    "SELECT sign_id, order_number, sign_code, sign_description, on_street, from_street,"
    " to_street, side_of_street, distance_from_intersection, arrow_direction, snap_confidence,"
    " snap_notes, is_regulation, panel_class FROM sign WHERE segment_id = ?"
)
# An unmatched sign has no geometry by definition, so it is found by name.
_UNMATCHED_SIGNS_SQL = (
    "SELECT sign_id, order_number, sign_code, sign_description, on_street, from_street,"
    " to_street, side_of_street, distance_from_intersection, arrow_direction, snap_confidence,"
    " snap_notes, is_regulation, panel_class FROM sign"
    " WHERE segment_id IS NULL AND side_of_street = ?"
)
_SEGMENT_NAMES_SQL = (
    "SELECT ss.street_norm, fn.street_names AS from_names, tn.street_names AS to_names"
    " FROM street_segment ss"
    " LEFT JOIN street_node fn ON fn.node_id = ss.from_node"
    " LEFT JOIN street_node tn ON tn.node_id = ss.to_node"
    " WHERE ss.segment_id = ?"
)
_STREET_NAMES_SQL = "SELECT DISTINCT street_norm FROM street_segment"

# The `regulation_segment.gap_kind` value `etl.segments` writes when DOT
# publishes signs for a blockface-side and none of them could be placed. Spelled
# here rather than imported, because importing `etl.segments` would pull the
# snapping and staging modules into the server process for one string.
UNMATCHED_SIGNS = "unmatched_signs"


@dataclass(frozen=True)
class SignDetail:
    """One `sign` row as the detail panel needs it. `sign_description` is untrusted."""

    sign_id: str
    order_number: str | None
    sign_code: str | None
    sign_description: str
    on_street: str | None
    from_street: str | None
    to_street: str | None
    side_of_street: str | None
    distance_from_intersection: float | None
    # DOT's `distance_from_intersection` is in feet; the unit is in the name so
    # the UI can print it without going back to the dataset to find out.
    distance_ft: float | None
    # DOT's own bearing word for the arrow on the post: "North", "South",
    # "East", "West", or null on the 73.5% of signs with no arrow
    # (docs/DATA.md §1.6). Which way that points *along this curb* is the
    # resolved `arrow` on each rule, not this.
    arrow: str | None
    snap_confidence: float | None
    snap_notes: str | None
    is_regulation: bool
    panel_class: str

    @property
    def blockface_key(self) -> tuple[str | None, str | None, str | None, str | None]:
        """DOT's `(on, from, to, side)` group, which is what it calls a blockface-side."""
        return (self.on_street, self.from_street, self.to_street, self.side_of_street)


@dataclass(frozen=True)
class BlockfaceSigns:
    """The span's own signs, then every other sign posted on the same blockface-side."""

    governing: list[SignDetail]
    other_on_block: list[SignDetail]


def blockface_signs(
    conn: sqlite3.Connection,
    *,
    segment_id: str | None,
    side: str | None,
    derived_from: Sequence[str],
    gap_kind: str | None,
) -> BlockfaceSigns:
    """The signs behind one span, split into governing and merely nearby, in curb order.

    `governing` is the span's `derived_from` set: the posts whose text became
    its rules. `other_on_block` is every other sign DOT posts on the same
    blockface-side — the same `(on, from, to, side)` name tuple as a governing
    sign, or, when the span has no governing sign to take a tuple from, the
    signs snapped to this centerline segment on the same side letter.

    A placeholder span (`gap_kind` set) has no governing signs at all. When its
    gap is `unmatched_signs`, DOT does publish signs for the blockface and none
    of them could be placed on the centerline; those are found by name and
    returned in `other_on_block`, because 332 of the 499 unmatched
    blockface-sides carry a NO STANDING/PARKING/STOPPING ANYTIME sign
    (docs/VALIDATION.md §5) and hiding them would leave the grey state looking
    emptier than the source is.

    Every row in the `sign` table is an active sign: `etl.stage` keeps only
    `sign_design_voided_on_date IS NULL` (docs/DATA.md §1.1).
    """
    if segment_id is None:
        return BlockfaceSigns(governing=[], other_on_block=[])

    on_segment = [_detail(row) for row in conn.execute(_SEGMENT_SIGNS_SQL, (segment_id,))]
    wanted = set(derived_from)
    governing = [] if gap_kind else [sign for sign in on_segment if sign.sign_id in wanted]

    keys = {sign.blockface_key for sign in governing}
    if keys:
        rest = [
            sign for sign in on_segment if sign.sign_id not in wanted and sign.blockface_key in keys
        ]
    else:
        rest = [sign for sign in on_segment if sign.side_of_street == side]
    if gap_kind == UNMATCHED_SIGNS:
        rest = rest + _unmatched_on_blockface(conn, segment_id=segment_id, side=side)

    return BlockfaceSigns(
        governing=_along_the_curb(governing), other_on_block=_along_the_curb(rest)
    )


def _along_the_curb(signs: Iterable[SignDetail]) -> list[SignDetail]:
    """In order of distance from the intersection DOT measured them from, unmeasured last."""
    return sorted(
        signs,
        key=lambda sign: (
            sign.distance_from_intersection is None,
            sign.distance_from_intersection or 0.0,
            sign.sign_id,
        ),
    )


def _unmatched_on_blockface(
    conn: sqlite3.Connection, *, segment_id: str, side: str | None
) -> list[SignDetail]:
    """The signs DOT posts on this blockface-side that never snapped to a centerline.

    The same name test `etl.segments._gap_kind` uses to decide that this side's
    gap is `unmatched_signs`: the on-street has to be this street, and both
    cross streets have to be streets that meet this segment — unless one of the
    names is in no centerline row at all, which is the case that produced the
    miss in the first place (docs/VALIDATION.md §4 D4).

    Costs one indexed pass over the signs that never snapped -- 2,700 rows on
    the 2026-09-15 database, of which a side letter keeps a few hundred -- and,
    only for a partial cross-street match, one scan of the 1,017 street names.
    Just 499 of the 5,648 placeholder spans reach this path at all.
    """
    if side is None:
        return []
    row = conn.execute(_SEGMENT_NAMES_SQL, (segment_id,)).fetchone()
    if row is None:
        return []
    street_norm = str(row["street_norm"])
    crossing = {
        normalize_street_name(name)
        for name in [*json_string_list(row["from_names"]), *json_string_list(row["to_names"])]
    }

    known_streets: set[str] | None = None
    unmatched = []
    for candidate in conn.execute(_UNMATCHED_SIGNS_SQL, (side,)):
        sign = _detail(candidate)
        if normalize_street_name(sign.on_street or "") != street_norm:
            continue
        crosses = {
            normalize_street_name(sign.from_street or ""),
            normalize_street_name(sign.to_street or ""),
        }
        if crosses <= crossing:
            unmatched.append(sign)
            continue
        if crosses & crossing:
            if known_streets is None:
                known_streets = {str(name[0]) for name in conn.execute(_STREET_NAMES_SQL)}
            if any(name not in known_streets for name in crosses):
                unmatched.append(sign)
    return unmatched


def _detail(row: sqlite3.Row) -> SignDetail:
    distance_ft = _optional_float(row["distance_from_intersection"])
    return SignDetail(
        sign_id=str(row["sign_id"]),
        order_number=_optional_str(row["order_number"]),
        sign_code=_optional_str(row["sign_code"]),
        sign_description=str(row["sign_description"]),
        on_street=_optional_str(row["on_street"]),
        from_street=_optional_str(row["from_street"]),
        to_street=_optional_str(row["to_street"]),
        side_of_street=_optional_str(row["side_of_street"]),
        distance_from_intersection=distance_ft,
        distance_ft=distance_ft,
        arrow=_optional_str(row["arrow_direction"]),
        snap_confidence=_optional_float(row["snap_confidence"]),
        snap_notes=_optional_str(row["snap_notes"]),
        is_regulation=bool(row["is_regulation"]),
        panel_class=str(row["panel_class"]),
    )


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)
