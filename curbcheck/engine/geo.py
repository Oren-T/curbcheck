"""The local flat-earth projection every radius query in the engine shares.

Over a 1 km radius at Manhattan's latitude the error against a proper
projection is under a metre, and it keeps pyproj out of the request path.
Values are the WGS-84 metres per degree at 40.75 N.
"""

from __future__ import annotations

import json
import math
from typing import Any

from shapely.errors import ShapelyError
from shapely.geometry import Point, shape

M_PER_DEG_LAT = 111_132.0
M_PER_DEG_LON_AT_EQUATOR = 111_320.0


def meters_per_degree_lon(lat: float) -> float:
    return M_PER_DEG_LON_AT_EQUATOR * math.cos(math.radians(lat))


def degree_padding(lat: float, meters: float) -> tuple[float, float]:
    """The (lon, lat) half-widths of a bbox `meters` wide around a point at `lat`."""
    return (meters / meters_per_degree_lon(lat), meters / M_PER_DEG_LAT)


def to_local_meters(geometry: dict[str, Any], *, lon0: float, lat0: float) -> dict[str, Any]:
    """The same GeoJSON geometry with (lon, lat) replaced by metres from (lon0, lat0)."""
    scale_lon = meters_per_degree_lon(lat0)

    def convert(node: Any) -> Any:
        if isinstance(node, (list, tuple)) and node and isinstance(node[0], (int, float)):
            return [(float(node[0]) - lon0) * scale_lon, (float(node[1]) - lat0) * M_PER_DEG_LAT]
        return [convert(child) for child in node]

    return {"type": geometry["type"], "coordinates": convert(geometry["coordinates"])}


def measure(
    geom: Any, *, lon: float, lat: float, origin: Point
) -> tuple[dict[str, Any], float] | None:
    """A geometry column's value and its distance in metres, or None when it cannot be read.

    The database is untrusted at read time (CLAUDE.md): a truncated or
    hand-edited snapshot answered every search with a 500 before the guard
    (docs/SECURITY.md residual 9). `origin` is Point(0, 0), the query point in
    the local frame; callers hoist it out of their loop.
    """
    try:
        geometry = json.loads(str(geom))
        local = shape(to_local_meters(geometry, lon0=lon, lat0=lat))
        return geometry, float(local.distance(origin))
    except (ValueError, TypeError, KeyError, IndexError, ShapelyError):
        return None
