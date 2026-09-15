"""Measure how far a name-only sign-to-centerline join gets us.

Answers the question the snap design turns on: for each (on_street,
from_street, to_street) triple in the sign data, can we find the centerline
segment or chain of segments that spans it by matching endpoint nodes, and does
`distance_from_intersection` fit inside the block we found?

Reports coverage only; it does not write the match itself. Stdlib only.
"""

from __future__ import annotations

import collections
from pathlib import Path
from typing import Any

from explore_names import NON_STREET_ENDPOINTS, normalize_street
from explore_streets import endpoint_index, length_ft, load, node_key, parts_of, write

OUT = Path(__file__).resolve().parent.parent / "data" / "explore"

# Sign rows sit on highways and bridges too (FDR Drive, the approaches), so the
# candidate pool is wider than the plain street network.
SNAPPABLE_RW_TYPES = frozenset({"1", "2", "3", "10"})


def segment_ends(segment: dict[str, Any]) -> tuple[tuple[float, float], tuple[float, float]] | None:
    parts = [p for p in parts_of(segment) if p]
    if not parts:
        return None
    return node_key(parts[0][0]), node_key(parts[-1][-1])


def main() -> int:
    segments = [
        s for s in load("centerline_manhattan.json") if s.get("rw_type") in SNAPPABLE_RW_TYPES
    ]
    index = endpoint_index(segments)
    names = [normalize_street(s["full_street_name"]) for s in segments]

    by_name: dict[str, list[int]] = collections.defaultdict(list)
    for i, name in enumerate(names):
        by_name[name].append(i)

    node_names: dict[tuple[float, float], set[str]] = collections.defaultdict(set)
    for node, members in index.items():
        node_names[node] = {names[i] for i in members}

    signs = [r for r in load("signs_manhattan.json") if not r.get("sign_design_voided_on_date")]
    faces: dict[tuple[str, str, str], list[dict[str, Any]]] = collections.defaultdict(list)
    for r in signs:
        faces[
            (
                normalize_street(r["on_street"]),
                normalize_street(r["from_street"]),
                normalize_street(r["to_street"]),
            )
        ].append(r)

    outcome: collections.Counter[str] = collections.Counter()
    sign_outcome: collections.Counter[str] = collections.Counter()
    overflow: list[tuple[float, float, str]] = []
    unmatched_examples: collections.Counter[tuple[str, str, str]] = collections.Counter()

    for (on, frm, to), rows in faces.items():
        if on not in by_name:
            outcome["on_street not in centerline"] += 1
            sign_outcome["on_street not in centerline"] += len(rows)
            unmatched_examples[(on, frm, to)] += len(rows)
            continue
        candidates = by_name[on]
        # A block is a chain of same-named segments; the simple case is a single
        # segment whose two ends touch both cross streets.
        direct = []
        for i in candidates:
            ends = segment_ends(segments[i])
            if not ends:
                continue
            start_names, end_names = node_names[ends[0]], node_names[ends[1]]
            if (frm in start_names and to in end_names) or (to in start_names and frm in end_names):
                direct.append(i)
        if direct:
            key = "single segment" if len(direct) == 1 else "ambiguous (>1 segment)"
            outcome[key] += 1
            sign_outcome[key] += len(rows)
            block_ft = max(sum(length_ft(p) for p in parts_of(segments[i])) for i in direct)
            for r in rows:
                distance = float(r["distance_from_intersection"])
                if distance > block_ft + 1:
                    overflow.append((distance, block_ft, f"{on} / {frm} / {to}"))
            continue
        reachable_names: set[str] = set()
        for i in candidates:
            ends = segment_ends(segments[i])
            if ends:
                reachable_names |= node_names[ends[0]] | node_names[ends[1]]
        touches_from = frm in reachable_names
        touches_to = to in reachable_names
        if frm in NON_STREET_ENDPOINTS or to in NON_STREET_ENDPOINTS:
            key = "cross street is DEAD END"
        elif touches_from and touches_to:
            key = "both cross streets present, needs multi-segment chain"
        elif touches_from or touches_to:
            key = "only one cross street found on this street"
        else:
            key = "neither cross street found on this street"
        outcome[key] += 1
        sign_outcome[key] += len(rows)
        unmatched_examples[(on, frm, to)] += len(rows)

    total_faces = sum(outcome.values())
    total_signs = sum(sign_outcome.values())
    matched_signs = sign_outcome["single segment"] + sign_outcome["ambiguous (>1 segment)"]
    overflow.sort(reverse=True)

    parts = [
        "# Name-join feasibility: sign blockface -> centerline segment",
        f"centerline segments considered (rw_type in {sorted(SNAPPABLE_RW_TYPES)})  {len(segments)}",
        f"distinct sign blockfaces (on, from, to)                     {total_faces}",
        f"active sign rows                                            {total_signs}",
        "",
        "# outcome by blockface",
        "\n".join(f"{n:8d}  {100 * n / total_faces:6.2f}%  {k}" for k, n in outcome.most_common()),
        "",
        "# outcome by sign row",
        "\n".join(
            f"{n:8d}  {100 * n / total_signs:6.2f}%  {k}" for k, n in sign_outcome.most_common()
        ),
        "",
        f"rows landing on exactly one segment          {sign_outcome['single segment']} "
        f"({100 * sign_outcome['single segment'] / total_signs:.2f}%)",
        f"rows landing on one or more segments         {matched_signs} "
        f"({100 * matched_signs / total_signs:.2f}%)",
        "",
        "# distance_from_intersection overflowing the matched block",
        f"count  {len(overflow)} of {matched_signs} matched rows "
        f"({100 * len(overflow) / max(matched_signs, 1):.2f}%)",
        "worst 20 (distance_ft, block_ft, blockface):",
        "\n".join(f"    {d:8.0f}  {b:8.1f}  {k}" for d, b, k in overflow[:20]),
        "",
        "# blockfaces that did not resolve, top 40 by sign count",
        "\n".join(
            f"{n:6d}  {on} | {frm} | {to}"
            for (on, frm, to), n in unmatched_examples.most_common(40)
        ),
    ]
    write("snap_feasibility.txt", "\n".join(parts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
