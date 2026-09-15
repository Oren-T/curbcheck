"""Dump what CurbCheck says about each sampled blockface-side, for ground-truth comparison.

Reads the sample `scripts/validation_sample.py` drew, then for each blockface-side
prints the snapped signs, the regulation segments with their extents, and the
verdict `POST /api/search` returns for the three SPEC §13.3 windows. The server
must be running (`curbcheck serve`).

    python scripts/validation_dossier.py [--seed 20260915] [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import urllib.request
from pathlib import Path
from typing import Any

from curbcheck.config import DB_PATH
from scripts.validation_sample import draw, load

API = "http://127.0.0.1:8765/api/search"

# SPEC §13.3's three test windows, on the first dates after the 2026-09-15 sync.
WINDOWS = (
    ("wed", "2026-09-16T10:00:00", "2026-09-16T12:00:00"),
    ("sat", "2026-09-19T09:00:00", "2026-09-19T11:00:00"),
    ("sun", "2026-09-20T14:00:00", "2026-09-20T16:00:00"),
)
WALK_MINUTES = 2.0


def post_search(lat: float, lon: float, t1: str, t2: str) -> dict[str, Any]:
    body = json.dumps(
        {"lat": lat, "lon": lon, "t1": t1, "t2": t2, "walk_minutes": WALK_MINUTES, "limit": 500}
    ).encode()
    request = urllib.request.Request(
        API, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - 127.0.0.1 only
        return json.loads(response.read())


def db_facts(conn: sqlite3.Connection, segment_id: str, side: str) -> dict[str, Any]:
    signs = [
        dict(row)
        for row in conn.execute(
            "SELECT sign_id, sign_code, sign_description, distance_from_intersection AS dist,"
            " arrow_direction, from_street, to_street, is_regulation, panel_class, snap_confidence"
            " FROM sign WHERE segment_id = ? AND side_of_street = ?"
            " ORDER BY distance_from_intersection",
            (segment_id, side),
        )
    ]
    segments = []
    for row in conn.execute(
        "SELECT reg_seg_id, start_ft, end_ft, length_ft, capacity_cars, confidence"
        " FROM regulation_segment WHERE segment_id = ? AND side = ? ORDER BY start_ft, end_ft",
        (segment_id, side),
    ):
        rules = [
            dict(rule)
            for rule in conn.execute(
                "SELECT raw_sign_description, parse_method, parse_confidence, arrow"
                " FROM regulation WHERE reg_seg_id = ?",
                (row["reg_seg_id"],),
            )
        ]
        segments.append({**dict(row), "rules": rules})
    return {"signs": signs, "segments": segments}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--seed", type=int, default=20260915)
    parser.add_argument("--per-stratum", type=int, default=5)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        sample = draw(load(conn), args.seed, args.per_stratum)
        dossier = []
        for index, row in enumerate(sample, start=1):
            facts = db_facts(conn, row["segment_id"], row["side"])
            verdicts = {}
            for label, t1, t2 in WINDOWS:
                response = post_search(row["lat"], row["lon"], t1, t2)
                ids = {segment["reg_seg_id"] for segment in facts["segments"]}
                mine = [r for r in response["results"] if r["reg_seg_id"] in ids]
                verdicts[label] = [
                    {
                        "reg_seg_id": r["reg_seg_id"],
                        "verdict": r["verdict"],
                        "reason": r["reason"],
                        "money": r["money"],
                        "price_known": r["price_known"],
                        "confidence": r["confidence"],
                        "caveats": r["caveats"],
                    }
                    for r in mine
                ]
                verdicts[f"{label}_in_radius"] = len(response["results"])
            dossier.append({"n": index, **row, **facts, "verdicts": verdicts})
            emit(index, row, facts, verdicts)
    finally:
        conn.close()

    if args.json:
        args.json.write_text(json.dumps(dossier, indent=1, default=str))
    return 0


def emit(index: int, row: dict[str, Any], facts: dict[str, Any], verdicts: dict[str, Any]) -> None:
    print(f"\n### {index}. [{row['stratum']}] {row['on_street']} side {row['side']}")
    print(f"    between {' / '.join(row['cross'])}  seg={row['segment_id']}"
          f"  midpoint {row['lat']:.6f},{row['lon']:.6f}  centerline {row['length_ft']:.0f} ft")
    print("    DB signs (distance ft, arrow bearing, code, text):")
    for sign in facts["signs"]:
        flag = "" if sign["is_regulation"] else f"  [{sign['panel_class']}]"
        print(f"      {sign['dist']:>7.0f}  {sign['arrow_direction'] or '-':<6}"
              f" {sign['sign_code'] or '-':<8} {sign['sign_description']}{flag}")
    print("    DB regulation segments:")
    for segment in facts["segments"]:
        print(f"      {segment['reg_seg_id']}  {segment['start_ft']:.0f}-{segment['end_ft']:.0f} ft"
              f"  cap {segment['capacity_cars']}  conf {segment['confidence']}")
        for rule in segment["rules"]:
            print(f"          [{rule['parse_method']}/{rule['parse_confidence']}"
                  f" arrow={rule['arrow']}] {rule['raw_sign_description']}")
    for label, _, _ in WINDOWS:
        for entry in verdicts[label]:
            money = "-" if entry["money"] is None else f"${entry['money']}"
            known = "" if entry["price_known"] else " (unconfirmed)"
            print(f"    {label}: {entry['verdict'].upper():<9} {money}{known}"
                  f"  {entry['reason']}  [{entry['reg_seg_id']}]")
        if not verdicts[label]:
            print(f"    {label}: NO RESULT - this blockface-side returns no span at all")


if __name__ == "__main__":
    raise SystemExit(main())
