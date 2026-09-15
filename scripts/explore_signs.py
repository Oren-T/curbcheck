"""Profile the Manhattan sign rows in data/raw/signs_manhattan.json.

Writes the long-form tables the parser and snapping work depend on into
data/explore/; docs/DATA.md carries the summary numbers. Read-only with respect
to data/raw. Stdlib only (shapely and pyproj are not installed in the explore
environment).
"""

from __future__ import annotations

import collections
import json
import re
from pathlib import Path
from typing import Any

from explore_names import NON_STREET_ENDPOINTS, normalize_street

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "explore"

# The export ships every row as record_type='Current' (441,237/441,237 citywide,
# measured 2026-09-15), so the voided date is the only usable retirement signal.
ACTIVE = "sign_design_voided_on_date"

# Arrows are drawn with a variable number of dashes ("<----->" appears 772
# times), so they are matched as a pattern rather than as three literals.
ARROW = re.compile(r"<-+>|<-+|-+>")

# The same relationship written in words, on signs whose artwork carries the
# arrow instead of the description text.
SINGLE_ARROW_TEXT = re.compile(r"\bSINGLE ARROW\b|\(ARROW\)")

# Trailing cross-reference parentheticals; the arrow sits before them.
SUPERSEDES_TAIL = re.compile(r"\s*\((?:SUPERSEDES|SUPERCEDES|SYPERSEDES)\b[^)]*\)\s*$", re.I)


def load(name: str) -> list[dict[str, Any]]:
    return json.loads((RAW / name).read_text())


def active_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if not r.get(ACTIVE)]


def glyph_of(description: str) -> str:
    """Arrow arity as one of `<->`, `-->`, `<--`, `SINGLE ARROW (text)`, `none`."""
    kinds = set()
    for match in ARROW.findall(description):
        if match.startswith("<") and match.endswith(">"):
            kinds.add("<->")
        elif match.endswith(">"):
            kinds.add("-->")
        else:
            kinds.add("<--")
    if kinds:
        return "+".join(sorted(kinds))
    if SINGLE_ARROW_TEXT.search(description):
        return "SINGLE ARROW (text)"
    return "none"


def classify(description: str) -> str:
    """Coarse bucket used to separate parkable regulations from the panels and
    locator plates that share the dataset."""
    text = description.upper()
    if text.startswith("NO PARKING"):
        return "NO PARKING..."
    if text.startswith("NO STANDING"):
        return "NO STANDING..."
    if text.startswith("NO STOPPING"):
        return "NO STOPPING..."
    if re.match(r"^\d+\s*(HOUR|HR|HMP|H\s)", text):
        return "N HOUR / HMP..."
    if text.startswith("METERS ARE NOT IN EFFECT"):
        return "meta: meters not in effect"
    if re.search(r"\b(ROUTE|DESTINATION|LOCATION) PANEL\b", text):
        return "MTA panel (not a regulation)"
    if "PAY-BY-CELL" in text or "PAY-BY-APP" in text:
        return "pay-by-cell info (not a regulation)"
    if text.startswith("BUS STOP SIGN"):
        return "BUS STOP SIGN"
    return "other"


def counter_table(counter: collections.Counter[Any], total: int) -> str:
    lines = []
    for key, count in counter.most_common():
        lines.append(f"{count:8d}  {100 * count / total:6.2f}%  {key}")
    return "\n".join(lines)


def write(name: str, text: str) -> None:
    (OUT / name).write_text(text + "\n")
    print(f"wrote data/explore/{name}")


def profile_descriptions(rows: list[dict[str, Any]], filename: str) -> None:
    counts: collections.Counter[str] = collections.Counter()
    codes: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    for r in rows:
        description = r["sign_description"]
        counts[description] += 1
        codes[description][r.get("sign_code", "")] += 1
    lines = ["count\tsign_codes\tsign_description"]
    for description, count in counts.most_common():
        code_field = "|".join(f"{c}:{n}" for c, n in codes[description].most_common())
        lines.append(f"{count}\t{code_field}\t{description}")
    write(filename, "\n".join(lines))


def profile_arrows(rows: list[dict[str, Any]]) -> None:
    total = len(rows)
    glyphs: collections.Counter[str] = collections.Counter()
    position: collections.Counter[str] = collections.Counter()
    tail_after_glyph: collections.Counter[str] = collections.Counter()
    for r in rows:
        description = r["sign_description"]
        glyphs[glyph_of(description)] += 1
        matches = list(ARROW.finditer(description))
        if not matches:
            continue
        stripped = SUPERSEDES_TAIL.sub("", description)
        last = matches[-1]
        tail_raw = description[last.end() :].strip()
        tail_stripped = stripped[last.end() :].strip() if last.end() <= len(stripped) else ""
        position["rows with an arrow"] += 1
        if not tail_raw:
            position["arrow ends the raw description"] += 1
        if not tail_stripped:
            position["arrow ends the description once the SUPERSEDES tail is removed"] += 1
        if tail_stripped:
            tail_after_glyph[tail_stripped[:60]] += 1

    # Non-arrow symbols that survive into the description text.
    symbols: collections.Counter[str] = collections.Counter()
    for r in rows:
        for ch in r["sign_description"]:
            if not (ch.isalnum() or ch.isspace() or ch in "-<>"):
                symbols[ch] += 1

    crosstab: collections.Counter[tuple[str, str, str]] = collections.Counter()
    for r in rows:
        crosstab[
            (
                glyph_of(r["sign_description"]),
                r.get("arrow_direction", ""),
                r.get("side_of_street", ""),
            )
        ] += 1

    arrow_by_side: collections.Counter[tuple[str, str]] = collections.Counter()
    for r in rows:
        arrow_by_side[(r.get("arrow_direction", ""), r.get("side_of_street", ""))] += 1

    arrow_tokens: collections.Counter[str] = collections.Counter()
    for r in rows:
        arrow_tokens.update(ARROW.findall(r["sign_description"]))

    parts = [
        "# Arrow glyph distribution (active Manhattan rows)",
        counter_table(glyphs, total),
        "",
        "# Glyph position within sign_description",
        counter_table(position, position["rows with an arrow"] or 1),
        "",
        "# Text that follows the last glyph after the SUPERSEDES tail is removed (top 40)",
        "\n".join(f"{n:8d}  {t!r}" for t, n in tail_after_glyph.most_common(40)),
        "",
        "# Literal arrow tokens",
        "\n".join(f"{n:8d}  {t!r}" for t, n in arrow_tokens.most_common()),
        "",
        "# Non-alphanumeric characters in sign_description (excluding - < >)",
        "\n".join(f"{n:8d}  {c!r} U+{ord(c):04X}" for c, n in symbols.most_common(30)),
        "",
        "# Crosstab: arrow_direction x side_of_street",
        "\n".join(
            f"{n:8d}  arrow_direction={a or '(blank)':9s} side_of_street={s or '(blank)'}"
            for (a, s), n in arrow_by_side.most_common()
        ),
        "",
        "# Crosstab: glyph x arrow_direction x side_of_street",
        "\n".join(
            f"{n:8d}  glyph={g:9s} arrow_direction={a or '(blank)':9s} side={s or '(blank)'}"
            for (g, a, s), n in crosstab.most_common()
        ),
    ]
    write("arrows.txt", "\n".join(parts))


def profile_fields(rows: list[dict[str, Any]]) -> None:
    total = len(rows)
    distances: list[float] = []
    non_numeric: collections.Counter[str] = collections.Counter()
    missing_distance = 0
    for r in rows:
        raw = r.get("distance_from_intersection")
        if raw is None or raw == "":
            missing_distance += 1
            continue
        try:
            distances.append(float(raw))
        except ValueError:
            non_numeric[str(raw)] += 1
    distances.sort()

    def pct(p: float) -> float:
        return distances[min(len(distances) - 1, int(p * len(distances)))]

    xs: list[float] = []
    ys: list[float] = []
    missing_coord = 0
    out_of_bounds: collections.Counter[str] = collections.Counter()
    for r in rows:
        x, y = r.get("sign_x_coord"), r.get("sign_y_coord")
        if x in (None, "") or y in (None, ""):
            missing_coord += 1
            continue
        xf, yf = float(x), float(y)
        xs.append(xf)
        ys.append(yf)
        if xf == 0 or yf == 0:
            out_of_bounds["zero"] += 1
        elif xf < 0 or yf < 0:
            out_of_bounds["negative"] += 1
        elif not (970_000 <= xf <= 1_010_000 and 190_000 <= yf <= 260_000):
            out_of_bounds["outside Manhattan EPSG:2263 box"] += 1

    parts = [
        "# distance_from_intersection (active Manhattan rows)",
        f"rows                 {total}",
        f"missing/blank        {missing_distance} ({100 * missing_distance / total:.2f}%)",
        f"non-numeric          {sum(non_numeric.values())}",
        "\n".join(f"    {v!r} x{n}" for v, n in non_numeric.most_common(20)),
        f"numeric              {len(distances)}",
        f"min / p25 / median   {distances[0]:.0f} / {pct(0.25):.0f} / {pct(0.5):.0f}",
        f"p75 / p95 / p99      {pct(0.75):.0f} / {pct(0.95):.0f} / {pct(0.99):.0f}",
        f"max                  {distances[-1]:.0f}",
        f"zero                 {sum(1 for d in distances if d == 0)}",
        f"negative             {sum(1 for d in distances if d < 0)}",
        f"over 2000 ft         {sum(1 for d in distances if d > 2000)}",
        "",
        "# side_of_street",
        counter_table(
            collections.Counter(r.get("side_of_street", "") or "(blank)" for r in rows), total
        ),
        "",
        "# sign_location",
        counter_table(
            collections.Counter(r.get("sign_location", "") or "(blank)" for r in rows), total
        ),
        "",
        "# facing_direction",
        counter_table(
            collections.Counter(r.get("facing_direction", "") or "(blank)" for r in rows), total
        ),
        "",
        "# support",
        counter_table(collections.Counter(r.get("support", "") or "(blank)" for r in rows), total),
        "",
        "# sign_x_coord / sign_y_coord",
        f"missing pair         {missing_coord} ({100 * missing_coord / total:.2f}%)",
        f"present              {len(xs)}",
        f"x min/max            {min(xs):.0f} / {max(xs):.0f}",
        f"y min/max            {min(ys):.0f} / {max(ys):.0f}",
        "\n".join(f"{n:8d}  {k}" for k, n in out_of_bounds.most_common())
        or "no junk coordinates found",
        "",
        "# street-name suffix columns (present count)",
        "\n".join(
            f"{sum(1 for r in rows if r.get(k)):8d}  {k}"
            for k in ("on_street_suffix", "from_street_suffix", "to_street_suffix")
        ),
        "",
        "# on_street_suffix values",
        counter_table(
            collections.Counter(r["on_street_suffix"] for r in rows if r.get("on_street_suffix")),
            total,
        ),
    ]
    write("fields.txt", "\n".join(parts))


def profile_grouping(rows: list[dict[str, Any]]) -> None:
    blockface_key = lambda r: (  # noqa: E731 - table-building helper
        normalize_street(r["on_street"]),
        normalize_street(r["from_street"]),
        normalize_street(r["to_street"]),
        r.get("side_of_street", ""),
    )
    faces: collections.Counter[tuple[str, ...]] = collections.Counter()
    posts: collections.Counter[tuple[Any, ...]] = collections.Counter()
    posts_per_face: dict[tuple[str, ...], set[Any]] = collections.defaultdict(set)
    orders_per_face: dict[tuple[str, ...], set[str]] = collections.defaultdict(set)
    for r in rows:
        face = blockface_key(r)
        distance = r.get("distance_from_intersection")
        faces[face] += 1
        posts[(*face, distance)] += 1
        posts_per_face[face].add(distance)
        orders_per_face[face].add(r["order_number"])

    def histogram(values: list[int]) -> str:
        counter = collections.Counter(values)
        lines = []
        for size in sorted(counter):
            if size > 12:
                break
            lines.append(f"    {size:3d} -> {counter[size]:6d}")
        tail = sum(n for s, n in counter.items() if s > 12)
        lines.append(f"    >12 -> {tail:6d}")
        return "\n".join(lines)

    signs_per_face = list(faces.values())
    signs_per_post = list(posts.values())
    post_counts = [len(v) for v in posts_per_face.values()]
    unordered_faces = [f for f, o in orders_per_face.items() if len(o) > 1]

    parts = [
        "# Grouping (active Manhattan rows)",
        f"rows                            {len(rows)}",
        f"distinct blockface-sides        {len(faces)}",
        "  key = (on_street, from_street, to_street, side_of_street), whitespace-collapsed",
        f"distinct posts                  {len(posts)}",
        "  key = blockface-side + distance_from_intersection",
        f"mean signs per blockface-side   {len(rows) / len(faces):.2f}",
        f"mean posts per blockface-side   {sum(post_counts) / len(faces):.2f}",
        f"mean signs per post             {len(rows) / len(posts):.2f}",
        f"blockface-sides with >1 order   {len(unordered_faces)}",
        "",
        "# signs per blockface-side",
        histogram(signs_per_face),
        "",
        "# posts per blockface-side",
        histogram(post_counts),
        "",
        "# signs per post",
        histogram(signs_per_post),
        "",
        "# busiest blockface-sides (top 20)",
        "\n".join(f"{n:5d}  {k}" for k, n in faces.most_common(20)),
        "",
        "# posts carrying the most signs (top 20)",
        "\n".join(f"{n:5d}  {k}" for k, n in posts.most_common(20)),
    ]
    write("grouping.txt", "\n".join(parts))


def profile_codes(rows: list[dict[str, Any]]) -> None:
    codes: collections.Counter[str] = collections.Counter()
    descriptions_per_code: dict[str, collections.Counter[str]] = collections.defaultdict(
        collections.Counter
    )
    for r in rows:
        code = r.get("sign_code", "") or "(blank)"
        codes[code] += 1
        descriptions_per_code[code][r["sign_description"]] += 1
    lines = [
        "# sign_code (active Manhattan rows)",
        f"distinct codes  {len(codes)}",
        f"prefixes        {dict(collections.Counter(c.split('-')[0] for c in codes).most_common())}",
        "",
        "# top 40 codes: count, distinct descriptions, most common description",
        "count\tdistinct_desc\tsign_code\texample_description",
    ]
    for code, count in codes.most_common(40):
        example = descriptions_per_code[code].most_common(1)[0][0]
        lines.append(f"{count}\t{len(descriptions_per_code[code])}\t{code}\t{example}")
    lines += [
        "",
        "# distinct descriptions per code, all codes (sorted by variety)",
        "distinct_desc\tcount\tsign_code",
    ]
    for code, variants in sorted(descriptions_per_code.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"{len(variants)}\t{codes[code]}\t{code}")
    write("sign_codes.txt", "\n".join(lines))


def profile_street_names(rows: list[dict[str, Any]], centerline: list[dict[str, Any]]) -> None:
    centerline_names: set[str] = set()
    for c in centerline:
        for key in ("full_street_name", "stname_label"):
            value = c.get(key)
            if value:
                centerline_names.add(normalize_street(str(value)))

    usage: collections.Counter[str] = collections.Counter()
    for r in rows:
        for key in ("on_street", "from_street", "to_street"):
            if r.get(key):
                usage[normalize_street(r[key])] += 1

    def resolvable(name: str) -> bool:
        return name in centerline_names or name in NON_STREET_ENDPOINTS

    matched = {n for n in usage if resolvable(n)}
    unmatched = {n: c for n, c in usage.items() if not resolvable(n)}
    matched_refs = sum(usage[n] for n in matched)
    all_refs = sum(usage.values())

    on_usage: collections.Counter[str] = collections.Counter(
        normalize_street(r["on_street"]) for r in rows if r.get("on_street")
    )
    on_unmatched = {n: c for n, c in on_usage.items() if not resolvable(n)}

    # The snap needs all three names at once, so the per-row figure is the one
    # that bounds how many signs can be placed by name join alone.
    fully_resolvable = sum(
        1
        for r in rows
        if all(
            resolvable(normalize_street(r[k])) for k in ("on_street", "from_street", "to_street")
        )
    )
    on_only = sum(1 for r in rows if resolvable(normalize_street(r["on_street"])))

    raw_quirks: collections.Counter[str] = collections.Counter()
    for r in rows:
        raw = r["on_street"]
        if "  " in raw:
            raw_quirks["two or more consecutive spaces"] += 1
        if raw != raw.strip():
            raw_quirks["leading/trailing space"] += 1
        if raw != raw.upper():
            raw_quirks["not uppercase"] += 1

    parts = [
        "# Sign street names vs centerline names (after scripts/explore_names.normalize_street)",
        f"distinct centerline names (full_street_name|stname_label)  {len(centerline_names)}",
        f"distinct sign street names (on/from/to)                    {len(usage)}",
        f"  resolvable                                               {len(matched)} ({100 * len(matched) / len(usage):.1f}%)",
        f"  unresolvable                                             {len(unmatched)}",
        f"name references resolvable                                 {matched_refs}/{all_refs} ({100 * matched_refs / all_refs:.2f}%)",
        f"distinct on_street values                                  {len(on_usage)}",
        f"  unresolvable on_street values                            {len(on_unmatched)}",
        f"rows whose on_street resolves                              {on_only}/{len(rows)} ({100 * on_only / len(rows):.2f}%)",
        f"rows whose on+from+to all resolve                          {fully_resolvable}/{len(rows)} ({100 * fully_resolvable / len(rows):.2f}%)",
        "",
        "# raw on_street formatting quirks (row counts)",
        "\n".join(f"{n:8d}  {k}" for k, n in raw_quirks.most_common()),
        "",
        "# 30 most common distinct on_street values, raw",
        "\n".join(
            f"{n:6d}  {v!r}"
            for v, n in collections.Counter(r["on_street"] for r in rows).most_common(30)
        ),
        "",
        "# top 60 unresolvable street names (on/from/to), by reference count",
        "\n".join(
            f"{n:6d}  {name}" for name, n in sorted(unmatched.items(), key=lambda kv: -kv[1])[:60]
        ),
        "",
        "# top 40 unresolvable on_street values, by reference count",
        "\n".join(
            f"{n:6d}  {name}"
            for name, n in sorted(on_unmatched.items(), key=lambda kv: -kv[1])[:40]
        ),
    ]
    write("street_names.txt", "\n".join(parts))


def profile_classes(rows: list[dict[str, Any]]) -> None:
    total = len(rows)
    classes: collections.Counter[str] = collections.Counter(
        classify(r["sign_description"]) for r in rows
    )
    other = collections.Counter(
        r["sign_description"] for r in rows if classify(r["sign_description"]) == "other"
    )
    distinct_per_class: dict[str, set[str]] = collections.defaultdict(set)
    for r in rows:
        distinct_per_class[classify(r["sign_description"])].add(r["sign_description"])
    parts = [
        "# Description classes (active Manhattan rows)",
        "\n".join(
            f"{n:8d}  {100 * n / total:6.2f}%  {len(distinct_per_class[k]):5d} distinct  {k}"
            for k, n in classes.most_common()
        ),
        "",
        "# 'other' class, top 60 descriptions",
        "\n".join(f"{n:6d}  {d}" for d, n in other.most_common(60)),
    ]
    write("description_classes.txt", "\n".join(parts))


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    all_rows = load("signs_manhattan.json")
    rows = active_rows(all_rows)
    print(f"{len(all_rows)} Manhattan rows, {len(rows)} active")
    profile_descriptions(rows, "descriptions.tsv")
    profile_arrows(rows)
    profile_fields(rows)
    profile_grouping(rows)
    profile_codes(rows)
    profile_classes(rows)
    profile_street_names(rows, load("centerline_manhattan.json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
