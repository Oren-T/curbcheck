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


def grid_graph() -> StreetGraph:
    return build_graph(stage_centerline(grid_rows()))


def avenue_chain_length_ft() -> float:
    """Measured length of the three-segment BROAD AVE chain, in feet.

    Taken from the graph rather than from AVENUE_LENGTHS_FT so span assertions
    do not have to carry the reprojection's own rounding.
    """
    block = grid_graph().find_block("BROAD AVENUE", "E 1 STREET", "E 4 STREET")
    if block is None:
        raise AssertionError("the fixture grid no longer spans E 1 ST to E 4 ST")
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
