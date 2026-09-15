"""Profile the Manhattan street centerline (inkn-q76z) for the snap step.

The two questions that decide the snap design are (a) whether intersections can
be rebuilt purely from shared endpoint coordinates, since the Socrata export
drops LION's from/to node ids, and (b) how well `segmentlength` agrees with the
geometry, since `distance_from_intersection` is measured in feet along it.

Stdlib only; distances use a local equirectangular approximation, which is
accurate to well under a foot over a Manhattan block.
"""

from __future__ import annotations

import collections
import itertools
import json
import math
from pathlib import Path
from typing import Any

from explore_names import normalize_street

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "explore"

# rw_type 1 is the ordinary street network; everything else is a ramp, path,
# bridge, tunnel, ferry route or driveway. Values are from the DCP metadata.
RW_TYPE_LABELS = {
    "1": "Street",
    "2": "Highway",
    "3": "Bridge",
    "4": "Tunnel",
    "6": "Sidewalk/path",
    "7": "Pedestrian path",
    "8": "Driveway",
    "9": "Ramp",
    "10": "Alley",
    "11": "Unknown",
    "12": "Non-physical street segment",
    "13": "U-turn / connector",
    "14": "Ferry route",
}

FEET_PER_METER = 3.280839895
EARTH_RADIUS_M = 6_371_008.8

# Endpoints are compared at 1e-7 degrees (~1.1 cm), which is finer than the
# coordinates DCP publishes and coarser than float formatting noise.
COORD_PLACES = 7


def load(name: str) -> list[dict[str, Any]]:
    return json.loads((RAW / name).read_text())


def node_key(coord: list[float]) -> tuple[float, float]:
    return (round(coord[0], COORD_PLACES), round(coord[1], COORD_PLACES))


def parts_of(segment: dict[str, Any]) -> list[list[list[float]]]:
    geometry = segment.get("the_geom") or {}
    return geometry.get("coordinates", [])


def length_ft(part: list[list[float]]) -> float:
    total_m = 0.0
    for (lon1, lat1), (lon2, lat2) in itertools.pairwise(part):
        mean_lat = math.radians((lat1 + lat2) / 2)
        dx = math.radians(lon2 - lon1) * math.cos(mean_lat)
        dy = math.radians(lat2 - lat1)
        total_m += math.hypot(dx, dy) * EARTH_RADIUS_M
    return total_m * FEET_PER_METER


def counter_table(counter: collections.Counter[Any], total: int) -> str:
    return "\n".join(f"{n:8d}  {100 * n / total:6.2f}%  {k}" for k, n in counter.most_common())


def write(name: str, text: str) -> None:
    (OUT / name).write_text(text + "\n")
    print(f"wrote data/explore/{name}")


def endpoint_index(segments: list[dict[str, Any]]) -> dict[tuple[float, float], list[int]]:
    """Map endpoint coordinate -> indices of the segments that terminate there."""
    index: dict[tuple[float, float], list[int]] = collections.defaultdict(list)
    for i, segment in enumerate(segments):
        for part in parts_of(segment):
            if not part:
                continue
            index[node_key(part[0])].append(i)
            index[node_key(part[-1])].append(i)
    return index


def profile(segments: list[dict[str, Any]]) -> None:
    total = len(segments)
    streets = collections.Counter(normalize_street(s["full_street_name"]) for s in segments)
    street_segments = collections.Counter(
        normalize_street(s["full_street_name"]) for s in segments if s.get("rw_type") == "1"
    )

    widths = [float(s["streetwidth"]) for s in segments if s.get("streetwidth")]
    widths.sort()
    street_widths = [
        float(s["streetwidth"])
        for s in segments
        if s.get("rw_type") == "1" and s.get("streetwidth")
    ]
    street_widths.sort()

    geometry_types = collections.Counter(
        (s.get("the_geom") or {}).get("type", "(missing)") for s in segments
    )
    part_counts = collections.Counter(len(parts_of(s)) for s in segments)
    vertex_counts = collections.Counter(sum(len(p) for p in parts_of(s)) == 2 for s in segments)

    length_error = []
    for s in segments:
        declared = s.get("segmentlength")
        if not declared:
            continue
        measured = sum(length_ft(p) for p in parts_of(s))
        if measured:
            length_error.append(abs(measured - float(declared)) / float(declared))
    length_error.sort()

    only_streets = [s for s in segments if s.get("rw_type") == "1"]
    index = endpoint_index(only_streets)
    degrees = collections.Counter(len(v) for v in index.values())
    dangling = sum(1 for v in index.values() if len(v) == 1)

    # An intersection is usable for from/to lookup only when the node carries a
    # segment from a second, differently named street.
    cross_named = 0
    for members in index.values():
        names = {normalize_street(only_streets[i]["full_street_name"]) for i in members}
        if len(names) > 1:
            cross_named += 1

    reachable = 0
    for s in only_streets:
        name = normalize_street(s["full_street_name"])
        ends = []
        for part in parts_of(s):
            if part:
                ends += [node_key(part[0]), node_key(part[-1])]
        neighbors = {
            normalize_street(only_streets[j]["full_street_name"]) for e in ends for j in index[e]
        }
        if neighbors - {name}:
            reachable += 1

    def percentile(values: list[float], p: float) -> float:
        return values[min(len(values) - 1, int(p * len(values)))]

    parts = [
        "# Centerline inkn-q76z, boroughcode='1'",
        f"segments                     {total}",
        "",
        "# rw_type",
        "\n".join(
            f"{n:8d}  {100 * n / total:6.2f}%  {k} ({RW_TYPE_LABELS.get(str(k), '?')})"
            for k, n in collections.Counter(s.get("rw_type") for s in segments).most_common()
        ),
        "",
        "# geometry",
        counter_table(geometry_types, total),
        f"coordinate order             lon,lat (WGS84 / EPSG:4326), e.g. {parts_of(segments[0])[0][0]}",
        "parts per MultiLineString:",
        "\n".join(f"    {k:3d} -> {n:6d}" for k, n in sorted(part_counts.items())[:8]),
        f"    multi-part segments       {sum(n for k, n in part_counts.items() if k > 1)}",
        f"    exactly two vertices      {vertex_counts[True]} of {total}",
        "",
        "# segmentlength (ft) vs geometry length",
        f"segments with segmentlength  {len(length_error)}",
        f"relative error p50/p90/p99   {percentile(length_error, 0.5):.4f} / "
        f"{percentile(length_error, 0.9):.4f} / {percentile(length_error, 0.99):.4f}",
        f"segments off by >5%          {sum(1 for e in length_error if e > 0.05)}",
        "",
        "# streetwidth (ft)",
        f"null                         {total - len(widths)} of {total} "
        f"({100 * (total - len(widths)) / total:.2f}%)",
        f"all types min/p25/p50/p75/max  {widths[0]:.0f} / {percentile(widths, 0.25):.0f} / "
        f"{percentile(widths, 0.5):.0f} / {percentile(widths, 0.75):.0f} / {widths[-1]:.0f}",
        f"rw_type=1 null               {len(only_streets) - len(street_widths)} of {len(only_streets)}",
        f"rw_type=1 min/p25/p50/p75/max  {street_widths[0]:.0f} / "
        f"{percentile(street_widths, 0.25):.0f} / {percentile(street_widths, 0.5):.0f} / "
        f"{percentile(street_widths, 0.75):.0f} / {street_widths[-1]:.0f}",
        "streetwidth_irr: "
        + str(dict(collections.Counter(s.get("streetwidth_irr") for s in segments).most_common(5))),
        "",
        "# populated columns that the snap and geocoder need",
        "\n".join(
            f"{sum(1 for s in segments if s.get(k)):8d}  "
            f"{100 * sum(1 for s in segments if s.get(k)) / total:6.2f}%  {k}"
            for k in (
                "l_blockfaceid",
                "r_blockfaceid",
                "l_low_hn",
                "l_high_hn",
                "r_low_hn",
                "r_high_hn",
                "l_zip",
                "r_zip",
                "physicalid",
                "segmentlength",
                "streetwidth",
                "number_park_lanes",
                "posted_speed",
                "trafdir",
                "bike_lane",
            )
        ),
        "",
        "# trafdir",
        counter_table(collections.Counter(s.get("trafdir") for s in segments), total),
        "",
        "# status",
        counter_table(collections.Counter(s.get("status") for s in segments), total),
        "",
        "# distinct street names",
        f"all rw_types                 {len(streets)}",
        f"rw_type=1                    {len(street_segments)}",
        "segments per name, rw_type=1, top 20:",
        "\n".join(f"    {n:5d}  {k}" for k, n in street_segments.most_common(20)),
        "segments-per-name histogram (rw_type=1):",
        "\n".join(
            f"    {k:3d} -> {n:5d}"
            for k, n in sorted(collections.Counter(street_segments.values()).items())[:12]
        ),
        "",
        "# endpoint (node) reconstruction, rw_type=1 only",
        f"distinct endpoint coordinates {len(index)}",
        f"nodes joining >=2 segments    {sum(n for k, n in degrees.items() if k >= 2)}",
        f"dangling endpoints (degree 1) {dangling}",
        f"nodes with >1 street name     {cross_named}",
        f"segments with >=1 differently named neighbor  {reachable} of {len(only_streets)} "
        f"({100 * reachable / len(only_streets):.2f}%)",
        "node degree histogram:",
        "\n".join(f"    {k:3d} -> {n:6d}" for k, n in sorted(degrees.items())),
    ]
    write("centerline.txt", "\n".join(parts))


def prove_cross_street_lookup(segments: list[dict[str, Any]]) -> None:
    """Walk one known block (3 AVE between E 85 ST and E 86 ST) end to end."""
    only_streets = [s for s in segments if s.get("rw_type") == "1"]
    index = endpoint_index(only_streets)
    lines = ["# Cross-street lookup by shared endpoints: 3 AVENUE, E 85 ST to E 86 ST"]
    target = normalize_street("3 AVENUE")
    for s in only_streets:
        if normalize_street(s["full_street_name"]) != target:
            continue
        ends = []
        for part in parts_of(s):
            if part:
                ends += [node_key(part[0]), node_key(part[-1])]
        neighbor_names = [
            sorted(
                {
                    normalize_street(only_streets[j]["full_street_name"])
                    for j in index[e]
                    if normalize_street(only_streets[j]["full_street_name"]) != target
                }
            )
            for e in (ends[0], ends[-1])
        ]
        flat = {n for group in neighbor_names for n in group}
        if not {"E 85 ST", "E 86 ST"} <= flat:
            continue
        lines += [
            "",
            f"physicalid            {s.get('physicalid')}",
            f"full_street_name      {s.get('full_street_name')!r}",
            f"segmentlength / geom  {s.get('segmentlength')} ft / "
            f"{sum(length_ft(p) for p in parts_of(s)):.1f} ft",
            f"streetwidth           {s.get('streetwidth')}",
            f"trafdir               {s.get('trafdir')}",
            f"l_blockfaceid         {s.get('l_blockfaceid')}  hn {s.get('l_low_hn')}-{s.get('l_high_hn')}",
            f"r_blockfaceid         {s.get('r_blockfaceid')}  hn {s.get('r_low_hn')}-{s.get('r_high_hn')}",
            f"start node            {ends[0]}  neighbors {neighbor_names[0]}",
            f"end node              {ends[-1]}  neighbors {neighbor_names[1]}",
            f"vertices              {[len(p) for p in parts_of(s)]}",
        ]
    write("block_3ave_e85_e86.txt", "\n".join(lines))


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    segments = load("centerline_manhattan.json")
    profile(segments)
    prove_cross_street_lookup(segments)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
