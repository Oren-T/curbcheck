"""Profile the meter datasets and test the blockface join against the signs.

The price side of the ranking depends on matching a ParkNYC blockface
(e7yp-wx55) to the same (on_street, from_street, to_street, side) triple the
sign data uses, so the profile measures that join directly rather than only
listing columns.
"""

from __future__ import annotations

import collections
from typing import Any

from explore_names import normalize_street
from explore_streets import length_ft, load, parts_of, write

# ParkNYC spells the borough inconsistently ('MANHATTAN', 'manhattan'), so the
# filter has to be case-insensitive.
MANHATTAN = "manhattan"

# Sign side_of_street is a single letter; ParkNYC spells it out.
SIDE_FORMS = {"NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W"}


def side_code(value: str | None) -> str:
    if not value:
        return ""
    text = value.strip().upper()
    return SIDE_FORMS.get(text, text[:1] if text else "")


def counter_table(counter: collections.Counter[Any], total: int) -> str:
    return "\n".join(f"{n:8d}  {100 * n / total:6.2f}%  {k!r}" for k, n in counter.most_common())


def profile_blockfaces() -> list[dict[str, Any]]:
    rows = load("parknyc_blockfaces.json")
    total = len(rows)
    manhattan = [r for r in rows if (r.get("borough") or "").strip().lower() == MANHATTAN]
    rates = collections.Counter(r.get("meter_rate") for r in manhattan)
    present = collections.Counter()
    for r in rows:
        present.update(r.keys())
    geometry_types = collections.Counter((r.get("the_geom") or {}).get("type") for r in rows)
    lengths = sorted(sum(length_ft(p) for p in parts_of(r)) for r in manhattan)

    parts = [
        "# e7yp-wx55 Parking Meters - ParkNYC Block Faces",
        f"rows                 {total}",
        f"Manhattan rows       {len(manhattan)} (case-insensitive borough match)",
        "",
        "# borough values as published",
        counter_table(collections.Counter(r.get("borough") for r in rows), total),
        "",
        "# column presence (all boroughs)",
        "\n".join(f"{n:8d}  {100 * n / total:6.2f}%  {k}" for k, n in present.most_common()),
        "",
        "# geometry",
        counter_table(geometry_types, total),
        f"Manhattan blockface length ft  min {lengths[0]:.0f} / "
        f"p50 {lengths[len(lengths) // 2]:.0f} / max {lengths[-1]:.0f}",
        "",
        "# meter_rate, Manhattan",
        counter_table(rates, len(manhattan)),
        "",
        "# side_of_st, Manhattan",
        counter_table(collections.Counter(r.get("side_of_st") for r in manhattan), len(manhattan)),
        "",
        "# vehicle_ty / pay_by_cel, Manhattan",
        counter_table(collections.Counter(r.get("vehicle_ty") for r in manhattan), len(manhattan)),
        counter_table(
            collections.Counter(bool(r.get("pay_by_cel")) for r in manhattan), len(manhattan)
        ),
        "",
        "# hours columns, sample of distinct values",
        "\n".join(
            f"  {k}: "
            + str([v for v, _ in collections.Counter(r.get(k) for r in manhattan).most_common(4)])
            for k in (
                "all_vehicl",
                "all_vehi_1",
                "all_vehi_2",
                "all_vehi_3",
                "commercial",
                "commerci_1",
                "commerci_2",
                "commerci_3",
            )
        ),
        "",
        "# on_street style compared with the sign dataset",
        "\n".join(
            f"  {r.get('on_street')!r} | {r.get('from_stree')!r} | {r.get('to_street')!r} | "
            f"{r.get('side_of_st')!r}"
            for r in manhattan[:12]
        ),
    ]
    write("meters_parknyc.txt", "\n".join(parts))
    return manhattan


def profile_meter_points() -> None:
    rows = load("meters_manhattan.json")
    total = len(rows)
    present: collections.Counter[str] = collections.Counter()
    for r in rows:
        present.update(r.keys())
    parts = [
        "# 693u-uax6 Parking Meters Locations and Status, borough='Manhattan'",
        f"rows  {total}",
        "",
        "# column presence",
        "\n".join(f"{n:8d}  {100 * n / total:6.2f}%  {k}" for k, n in present.most_common()),
        "",
        "# status",
        counter_table(collections.Counter(r.get("status") for r in rows), total),
        "",
        "# side_of_street",
        counter_table(collections.Counter(r.get("side_of_street") for r in rows), total),
        "",
        "# meter_hours, top 15",
        "\n".join(
            f"{n:8d}  {v!r}"
            for v, n in collections.Counter(r.get("meter_hours") for r in rows).most_common(15)
        ),
        "",
        "# sample rows",
        "\n".join(
            f"  {r.get('meter_number')} {r.get('on_street')!r} | {r.get('from_street')!r} | "
            f"{r.get('to_street')!r} | {r.get('side_of_street')!r} | {r.get('lat')},{r.get('long')}"
            for r in rows[:10]
        ),
    ]
    write("meters_points.txt", "\n".join(parts))


def profile_rate_zones() -> None:
    rows = load("meter_rate_zones.json")
    total = len(rows)
    parts = [
        "# f72k-2u3b Parking Meters - Citywide Rate Zones (data twin of the da76-p95d map)",
        f"polygons  {total}",
        "",
        "# geometry",
        counter_table(
            collections.Counter((r.get("the_geom") or {}).get("type") for r in rows), total
        ),
        "",
        "# boro_name",
        counter_table(collections.Counter(r.get("boro_name") for r in rows), total),
        "",
        "# zone / rate_zone / zone_name, all rows",
        "\n".join(
            f"  boro={r.get('boro_name')!r} zone={r.get('zone')!r} "
            f"rate_zone={r.get('rate_zone')!r} zone_name={r.get('zone_name')!r}"
            for r in sorted(rows, key=lambda r: (str(r.get("boro_name")), str(r.get("zone"))))
        ),
    ]
    write("meter_rate_zones.txt", "\n".join(parts))


def profile_join(blockfaces: list[dict[str, Any]]) -> None:
    signs = [r for r in load("signs_manhattan.json") if not r.get("sign_design_voided_on_date")]
    sign_faces = {
        (
            normalize_street(r["on_street"]),
            frozenset({normalize_street(r["from_street"]), normalize_street(r["to_street"])}),
            r.get("side_of_street", ""),
        )
        for r in signs
    }
    sign_blocks = {(on, cross) for on, cross, _ in sign_faces}

    exact = cross_only = street_only = miss = 0
    misses: collections.Counter[str] = collections.Counter()
    for b in blockfaces:
        key = (
            normalize_street(b.get("on_street") or ""),
            frozenset(
                {
                    normalize_street(b.get("from_stree") or ""),
                    normalize_street(b.get("to_street") or ""),
                }
            ),
            side_code(b.get("side_of_st")),
        )
        if key in sign_faces:
            exact += 1
        elif (key[0], key[1]) in sign_blocks:
            cross_only += 1
        elif any(on == key[0] for on, _ in sign_blocks):
            street_only += 1
        else:
            miss += 1
            misses[key[0]] += 1

    total = len(blockfaces)
    parts = [
        "# ParkNYC blockface -> sign blockface-side join (Manhattan)",
        f"ParkNYC Manhattan blockfaces      {total}",
        f"distinct sign blockface-sides     {len(sign_faces)}",
        "",
        f"matched on street + cross pair + side   {exact} ({100 * exact / total:.2f}%)",
        f"matched on street + cross pair only     {cross_only} ({100 * cross_only / total:.2f}%)",
        f"street name known, cross pair unknown   {street_only} ({100 * street_only / total:.2f}%)",
        f"street name unknown to the sign data    {miss} ({100 * miss / total:.2f}%)",
        "",
        "# unmatched on_street values, top 20",
        "\n".join(f"{n:6d}  {k}" for k, n in misses.most_common(20)),
    ]
    write("meters_join.txt", "\n".join(parts))


def main() -> int:
    blockfaces = profile_blockfaces()
    profile_meter_points()
    profile_rate_zones()
    profile_join(blockfaces)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
