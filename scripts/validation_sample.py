"""Draw the stratified blockface-side sample for the ground-truth protocol (SPEC §13.3).

Reads the live `data/curbcheck.sqlite`, classifies every candidate blockface-side
by neighbourhood and regulation type, and prints a seeded random sample. Not a
unit test: it needs a synced database and makes no claim about code paths.

    python scripts/validation_sample.py [--seed 20260915] [--per-stratum 5]
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from curbcheck.config import DB_PATH

# Numbered cross street -> neighbourhood stratum. Manhattan's grid is tilted, so
# a latitude band alone mislabels the east and west ends of the same street; the
# street number is exact where the grid exists and latitude is the fallback for
# the colonial street plan south of Houston.
NEIGHBOURHOODS = (
    ("lower_manhattan", None, None, (None, 40.7205)),
    ("village_soho", 1, 14, (40.7205, 40.7390)),
    ("midtown", 30, 59, (None, None)),
    ("ues_uws", 60, 109, (None, None)),
    ("harlem", 110, 155, (None, None)),
    ("heights_inwood", 156, 230, (None, None)),
)

_NUMBERED = re.compile(r"\b([EW])?\s*(\d{1,3})\s*(?:ST|STREET)\b")


def street_number(name: str) -> int | None:
    match = _NUMBERED.search(name.upper())
    return int(match.group(2)) if match else None


def neighbourhood(names: list[str], lat: float) -> str | None:
    numbers = [n for n in (street_number(name) for name in names) if n is not None]
    if numbers:
        number = min(numbers)
        for label, low, high, _ in NEIGHBOURHOODS:
            if low is not None and low <= number <= high:
                return label
        return None
    for label, _, _, (lat_low, lat_high) in NEIGHBOURHOODS:
        if lat_low is None and lat_high is not None and lat < lat_high:
            return label
        if lat_low is not None and lat_high is not None and lat_low <= lat < lat_high:
            return label
    return None


def reg_type(descriptions: list[str], has_rules: bool) -> str:
    """Coarse regulation stratum, read off the raw sign text like the spec's strata."""
    if not descriptions or not has_rules:
        return "no_data"
    joined = " | ".join(descriptions)
    anytime = any(
        ("NO STANDING" in d or "NO PARKING" in d or "NO STOPPING" in d) and "ANYTIME" in d
        for d in descriptions
    )
    metered = "METERED" in joined or "HMP" in joined or " MP " in joined
    broom = "BROOM" in joined or "SANITATION" in joined or "STREET CLEANING" in joined
    kinds = sum([anytime, metered, broom])
    if kinds > 1 or len(set(descriptions)) > 3:
        return "stacked_mixed"
    if anytime:
        return "no_standing_anytime"
    if metered:
        return "metered"
    if broom:
        return "street_cleaning"
    return "other"


def load(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    nodes = {
        row["node_id"]: json.loads(row["street_names"])
        for row in conn.execute("SELECT node_id, street_names FROM street_node")
    }
    segments = {
        row["segment_id"]: dict(row)
        for row in conn.execute(
            "SELECT segment_id, street_name, from_node, to_node, length_ft,"
            " (min_lat+max_lat)/2 AS lat, (min_lon+max_lon)/2 AS lon,"
            " max_lat-min_lat AS dlat, max_lon-min_lon AS dlon FROM street_segment"
        )
    }
    signs: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in conn.execute(
        "SELECT segment_id, side_of_street, on_street, from_street, to_street,"
        " sign_description, distance_from_intersection, is_regulation"
        " FROM sign WHERE segment_id IS NOT NULL"
    ):
        signs[(row["segment_id"], row["side_of_street"])].append(row)
    observed: dict[str, tuple[str, ...]] = {}
    for row in conn.execute(
        "SELECT segment_id, side_of_street, COUNT(*) n FROM sign WHERE segment_id IS NOT NULL"
        " GROUP BY segment_id, side_of_street HAVING n >= 2"
    ):
        observed.setdefault(row["segment_id"], ())
        observed[row["segment_id"]] += (row["side_of_street"],)

    ruled = {
        (row["segment_id"], row["side"])
        for row in conn.execute(
            "SELECT DISTINCT rs.segment_id, rs.side FROM regulation_segment rs"
            " JOIN regulation r ON r.reg_seg_id = rs.reg_seg_id"
        )
    }

    out = []
    for segment_id, segment in segments.items():
        cross = nodes.get(segment["from_node"], []) + nodes.get(segment["to_node"], [])
        sides = observed.get(segment_id) or _sides_from_bearing(segment)
        for side in sides:
            rows = signs.get((segment_id, side), [])
            descriptions = [r["sign_description"] for r in rows if r["is_regulation"]]
            hood = neighbourhood([segment["street_name"], *cross], segment["lat"])
            if hood is None:
                continue
            out.append(
                {
                    "segment_id": segment_id,
                    "side": side,
                    "on_street": segment["street_name"],
                    "cross": sorted(set(cross) - {segment["street_name"]}),
                    "from_street": rows[0]["from_street"] if rows else None,
                    "to_street": rows[0]["to_street"] if rows else None,
                    "lat": segment["lat"],
                    "lon": segment["lon"],
                    "length_ft": segment["length_ft"],
                    "neighbourhood": hood,
                    "reg_type": reg_type(descriptions, (segment_id, side) in ruled),
                    "n_signs": len(descriptions),
                    "descriptions": descriptions,
                }
            )
    return out


def _sides_from_bearing(segment: dict[str, Any]) -> tuple[str, ...]:
    """A run that is mostly east-west has N and S curbs; mostly north-south has E and W.

    Read off the geometry, not the name: St Nicholas Ave and Broadway are
    diagonals, and DOT sides them E/W because they run more north than east.
    """
    # One degree of longitude is about 0.76 of a degree of latitude here.
    return ("N", "S") if segment["dlon"] * 0.76 > segment["dlat"] else ("E", "W")


# Bridges, ramps, transverses and park paths have no house numbers and no
# posted parking signs to look up, so a "no sign data" finding on one says
# nothing about the matching pipeline. A no-data sample must be a real street.
_NOT_A_STREET = re.compile(
    r"\b(BRG|BRIDGE|RAMP|TRANSVERSE|LOOP|PATH|CONNECTOR|EXIT|ENTRANCE|APPROACH"
    r"|TUNNEL|VIADUCT|OPAS|TERM|FERRY|DRWY|EXPY|HWY)\b"
)


def plausible_address(row: dict[str, Any]) -> bool:
    names = [row["on_street"], *row["cross"]]
    if any(_NOT_A_STREET.search(name.upper()) for name in names):
        return False
    return row["length_ft"] > 150 and len(row["cross"]) >= 2


def draw(rows: list[dict[str, Any]], seed: int, per_stratum: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    by_hood: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_hood[row["neighbourhood"]].append(row)
    wanted = ("metered", "street_cleaning", "no_standing_anytime", "stacked_mixed", "no_data")
    sample = []
    for label, *_ in NEIGHBOURHOODS:
        pool = by_hood.get(label, [])
        by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in pool:
            by_type[row["reg_type"]].append(row)
        for kind in wanted[:per_stratum]:
            choices = by_type.get(kind) or by_type.get("other") or pool
            if kind == "no_data":
                choices = [c for c in choices if plausible_address(c)] or choices
            choices = sorted(choices, key=lambda r: (r["segment_id"], r["side"]))
            if not choices:
                continue
            sample.append({**rng.choice(choices), "stratum": f"{label}/{kind}"})
    return sample


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--seed", type=int, default=20260915)
    parser.add_argument("--per-stratum", type=int, default=5)
    parser.add_argument("--counts", action="store_true", help="print stratum population sizes")
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = load(conn)
    finally:
        conn.close()

    if args.counts:
        counts = Counter((row["neighbourhood"], row["reg_type"]) for row in rows)
        for key in sorted(counts):
            print(f"{key[0]:<18} {key[1]:<20} {counts[key]}")
        return 0

    print(f"seed={args.seed} population={len(rows)} blockface-sides")
    for index, row in enumerate(draw(rows, args.seed, args.per_stratum), start=1):
        print(
            f"{index:2d}. [{row['stratum']}] {row['on_street']} side {row['side']}"
            f" between {' / '.join(row['cross'])}"
            f"  seg={row['segment_id']} lat={row['lat']:.6f} lon={row['lon']:.6f}"
            f" signs={row['n_signs']} len={row['length_ft']:.0f}ft"
        )
        for description in row["descriptions"][:12]:
            print(f"      {description}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
