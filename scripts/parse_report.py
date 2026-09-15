"""Coverage of the sign grammar over the live Manhattan description vocabulary.

Reads `data/explore/descriptions.tsv` (count, sign_codes, sign_description),
parses every string, and prints the outcome split two ways: by distinct string
and weighted by how many sign rows carry it. Weighted coverage of the
*regulation* rows is the number that matters; panels are excluded from it by
decision D10.

Writes the residue — everything unparsed or partial — to
`data/explore/parse_residue.tsv` so the grammar can be extended against it.

    python scripts/parse_report.py
"""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

from curbcheck.etl.parse import parse_description
from curbcheck.model import ParsedSign, ParseMethod

CORPUS = Path("data/explore/descriptions.tsv")
RESIDUE = Path("data/explore/parse_residue.tsv")

PANEL = "panel"
FULL = "grammar full"
PARTIAL = "grammar partial"
UNPARSED = "unparsed"


def outcome(parsed: ParsedSign) -> str:
    if parsed.parse_method is ParseMethod.UNPARSED:
        return UNPARSED
    if not parsed.regulations:
        return PANEL
    return FULL if parsed.confidence >= 1.0 else PARTIAL


def main() -> int:
    if not CORPUS.exists():
        print(f"missing {CORPUS}; run scripts/explore_signs.py first")
        return 1

    rows = []
    with CORPUS.open(newline="") as handle:
        for record in csv.DictReader(handle, delimiter="\t"):
            rows.append((int(record["count"]), record["sign_description"]))

    by_string: Counter[str] = Counter()
    by_row: Counter[str] = Counter()
    residue: list[tuple[int, str, str, str]] = []
    for count, description in rows:
        parsed = parse_description(description)
        label = outcome(parsed)
        by_string[label] += 1
        by_row[label] += count
        if label in (UNPARSED, PARTIAL):
            residue.append((count, label, parsed.notes, description))

    total_strings = sum(by_string.values())
    total_rows = sum(by_row.values())
    regulation_rows = total_rows - by_row[PANEL]
    regulation_strings = total_strings - by_string[PANEL]

    print(f"{len(rows)} distinct descriptions, {total_rows} sign rows\n")
    print(f"{'outcome':<16}{'strings':>9}{'':>4}{'rows':>9}{'':>4}{'% of reg rows':>14}")
    for label in (FULL, PARTIAL, UNPARSED, PANEL):
        share = "" if label == PANEL else f"{100 * by_row[label] / regulation_rows:13.2f}%"
        print(f"{label:<16}{by_string[label]:>9}{'':>4}{by_row[label]:>9}{'':>4}{share:>14}")

    print(
        f"\nregulation rows: {regulation_rows} ({regulation_strings} strings);"
        f" panels: {by_row[PANEL]} ({by_string[PANEL]} strings)"
    )
    print(f"fully parsed, weighted by row count: {100 * by_row[FULL] / regulation_rows:.2f}%")

    residue.sort(reverse=True)
    RESIDUE.parent.mkdir(parents=True, exist_ok=True)
    with RESIDUE.open("w", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t")
        writer.writerow(("count", "outcome", "notes", "sign_description"))
        writer.writerows(residue)
    print(f"residue: {len(residue)} strings, {sum(r[0] for r in residue)} rows -> {RESIDUE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
