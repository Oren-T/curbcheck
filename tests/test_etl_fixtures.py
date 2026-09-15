"""Small synthetic centerline and sign rows shared by the ETL tests.

Everything here is inline: the ETL tests must run without the network and
without the snapshots in data/raw. The geometry is a toy grid near Manhattan's
latitude so reprojected lengths come out in plausible feet.

The grid, in WGS-84:

    E 4 ST  ────┬──── lat 40.7530
                │  (BROAD AVE, 728 ft)
    E 3 ST  ────┼──── lat 40.7510
                │  (BROAD AVE, 364 ft)
    E 2 ST  ────┼──── lat 40.7500   <- MAIN ST also runs along this latitude
                │  (BROAD AVE, 364 ft)
    E 1 ST  ────┴──── lat 40.7490
             lon -73.9880
"""

from __future__ import annotations

from typing import Any

from curbcheck.etl.stage import REGULATION_PANEL_CLASS, StagedSign, stage_centerline
from curbcheck.etl.streets import StreetGraph, build_graph

AVENUE_LON = -73.9880
CROSS_LATS = (40.7490, 40.7500, 40.7510, 40.7530)
CROSS_NAMES = ("E 1 ST", "E 2 ST", "E 3 ST", "E 4 ST")

# Node ids sort by lon then lat, so a south-to-north avenue is already in the
# canonical orientation `segments` groups by. See segments._canonical_chain.
AVENUE_LENGTHS_FT = (364.0, 364.0, 728.0)
AVENUE_LENGTH_FT = sum(AVENUE_LENGTHS_FT)
AVENUE_SEGMENT_IDS = ("avenue-0", "avenue-1", "avenue-2")

MAIN_STREET_LAT = 40.7500
MAIN_STREET_LONS = (-73.9900, -73.9860)


def centerline_row(
    physicalid: str,
    name: str,
    coordinates: list[list[float]],
    *,
    rw_type: str = "1",
    streetwidth: str | None = "34",
) -> dict[str, Any]:
    """A centerline row shaped the way Socrata sends it: strings and MultiLineString."""
    row: dict[str, Any] = {
        "physicalid": physicalid,
        "full_street_name": name,
        "rw_type": rw_type,
        "the_geom": {"type": "MultiLineString", "coordinates": [coordinates]},
    }
    if streetwidth is not None:
        row["streetwidth"] = streetwidth
    return row


def grid_rows() -> list[dict[str, Any]]:
    """Three BROAD AVE segments, four cross streets, and one east-west MAIN ST."""
    rows = [
        centerline_row(
            f"avenue-{index}",
            "BROAD AVE",
            [[AVENUE_LON, CROSS_LATS[index]], [AVENUE_LON, CROSS_LATS[index + 1]]],
            streetwidth="60",
        )
        for index in range(3)
    ]
    # CSCL splits a street at every intersection, so the cross streets have to
    # end at the avenue rather than pass through it: the graph rebuilds nodes
    # from shared *endpoints* (docs/DATA.md §2.3).
    for index, (name, lat) in enumerate(zip(CROSS_NAMES, CROSS_LATS, strict=True)):
        rows.append(
            centerline_row(f"cross-{index}w", name, [[AVENUE_LON - 0.002, lat], [AVENUE_LON, lat]])
        )
        rows.append(
            centerline_row(f"cross-{index}e", name, [[AVENUE_LON, lat], [AVENUE_LON + 0.002, lat]])
        )
    rows.append(
        centerline_row(
            "main",
            "MAIN ST",
            [[MAIN_STREET_LONS[0], MAIN_STREET_LAT], [MAIN_STREET_LONS[1], MAIN_STREET_LAT]],
            streetwidth=None,
        )
    )
    return rows


# A three-segment stub running east from the BROAD AVE / E 3 ST corner to a
# dangling endpoint: DOT writes `DEAD END` for the far end of a street like
# this one (docs/VALIDATION.md §4 D2).
STUB_LONS = (AVENUE_LON, AVENUE_LON + 0.0005, AVENUE_LON + 0.0010, AVENUE_LON + 0.0015)
STUB_LAT = CROSS_LATS[2]


def dead_end_rows() -> list[dict[str, Any]]:
    """The grid plus STUB ST, which leaves E 3 ST and simply stops."""
    rows = grid_rows()
    rows.extend(
        centerline_row(
            f"stub-{index}",
            "STUB ST",
            [[STUB_LONS[index], STUB_LAT], [STUB_LONS[index + 1], STUB_LAT]],
        )
        for index in range(3)
    )
    return rows


def dead_end_graph() -> StreetGraph:
    return build_graph(stage_centerline(dead_end_rows()))


# A second BROAD AVE carriageway between E 1 ST and E 2 ST, 74 ft east of the
# first: CSCL models a divided roadway as two parallel centerlines sharing the
# cross-street nodes, so both spell an equally short chain (docs/DATA.md §2.3).
DIVIDED_LON_OFFSET = 0.0009


def divided_rows() -> list[dict[str, Any]]:
    rows = grid_rows()
    rows.append(
        centerline_row(
            "avenue-0-east",
            "BROAD AVE",
            [
                [AVENUE_LON, CROSS_LATS[0]],
                [AVENUE_LON + DIVIDED_LON_OFFSET, CROSS_LATS[0] + 0.0002],
                [AVENUE_LON + DIVIDED_LON_OFFSET, CROSS_LATS[1] - 0.0002],
                [AVENUE_LON, CROSS_LATS[1]],
            ],
            streetwidth="60",
        )
    )
    return rows


def divided_graph() -> StreetGraph:
    return build_graph(stage_centerline(divided_rows()))


def grid_graph() -> StreetGraph:
    return build_graph(stage_centerline(grid_rows()))


def avenue_chain_offsets() -> dict[str, float]:
    """Where each BROAD AVE segment starts along the south-to-north chain, in feet.

    Read off the graph rather than off AVENUE_LENGTHS_FT so span assertions do
    not have to carry the reprojection's own rounding.
    """
    segments = grid_graph().segments
    offsets: dict[str, float] = {}
    travelled = 0.0
    for segment_id in AVENUE_SEGMENT_IDS:
        offsets[segment_id] = travelled
        travelled += segments[segment_id].length_ft
    return offsets


# Two segments of one avenue, digitized in opposite directions, with a cross
# street at each end and in the middle. DOT writes some signs against
# "E 16 ST -> E 18 ST" and others against "E 16 ST -> E 17 ST", which are two
# chains over one piece of curb (docs/DECISIONS.md D26). Both centerline rows
# start at the middle corner, so the chain from E 16 ST runs with one of them
# and against the other.
TWO_CHAIN_LON = -73.9700
TWO_CHAIN_LATS = (40.7360, 40.7370, 40.7380)
TWO_CHAIN_NAMES = ("E 16 ST", "E 17 ST", "E 18 ST")
SOUTH_SEGMENT = "ave-south"
NORTH_SEGMENT = "ave-north"


def two_chain_rows() -> list[dict[str, Any]]:
    """1 AVE in two oppositely-digitized segments, plus its three cross streets."""
    rows = [
        centerline_row(
            SOUTH_SEGMENT,
            "1 AVE",
            [[TWO_CHAIN_LON, TWO_CHAIN_LATS[1]], [TWO_CHAIN_LON, TWO_CHAIN_LATS[0]]],
            streetwidth="60",
        ),
        centerline_row(
            NORTH_SEGMENT,
            "1 AVE",
            [[TWO_CHAIN_LON, TWO_CHAIN_LATS[1]], [TWO_CHAIN_LON, TWO_CHAIN_LATS[2]]],
            streetwidth="60",
        ),
    ]
    for name, lat in zip(TWO_CHAIN_NAMES, TWO_CHAIN_LATS, strict=True):
        rows.append(
            centerline_row(f"{name}-w", name, [[TWO_CHAIN_LON - 0.002, lat], [TWO_CHAIN_LON, lat]])
        )
        rows.append(
            centerline_row(f"{name}-e", name, [[TWO_CHAIN_LON, lat], [TWO_CHAIN_LON + 0.002, lat]])
        )
    return rows


def two_chain_graph() -> StreetGraph:
    return build_graph(stage_centerline(two_chain_rows()))


def avenue_chain_length_ft() -> float:
    """Measured length of the three-segment BROAD AVE chain, in feet.

    Taken from the graph rather than from AVENUE_LENGTHS_FT so span assertions
    do not have to carry the reprojection's own rounding.
    """
    block = grid_graph().find_block("BROAD AVENUE", "E 1 STREET", "E 4 STREET")
    if block is None:
        raise AssertionError("the fixture grid no longer spans E 1 ST to E 4 ST")
    return block.length_ft


def first_block_length_ft() -> float:
    """Measured length of the BROAD AVE block between E 1 ST and E 2 ST, in feet."""
    block = grid_graph().find_block("BROAD AVENUE", "E 1 STREET", "E 2 STREET")
    if block is None:
        raise AssertionError("the fixture grid no longer spans E 1 ST to E 2 ST")
    return block.length_ft


def staged_sign(
    sign_id: str,
    *,
    on_street: str = "BROAD AVENUE",
    from_street: str = "E 1 STREET",
    to_street: str = "E 4 STREET",
    side: str = "W",
    distance_ft: float = 100.0,
    description: str = "NO PARKING ANYTIME <->",
    sign_code: str = "PS-1G",
    arrow_direction: str | None = None,
    x_coord: float | None = None,
    y_coord: float | None = None,
    panel_class: str = REGULATION_PANEL_CLASS,
) -> StagedSign:
    return StagedSign(
        sign_id=sign_id,
        order_number="P-1",
        on_street=on_street,
        from_street=from_street,
        to_street=to_street,
        side_of_street=side,
        distance_from_intersection_ft=distance_ft,
        arrow_direction=arrow_direction,
        facing_direction=None,
        sign_code=sign_code,
        sign_description=description,
        sign_notes=None,
        sign_x_coord=x_coord,
        sign_y_coord=y_coord,
        panel_class=panel_class,
        is_regulation=panel_class == REGULATION_PANEL_CLASS,
    )
