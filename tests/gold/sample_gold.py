#!/usr/bin/env python3
"""Select the stratified gold-set sample of sign descriptions.

Deterministic: the same corpus file always yields the same rows, so a label in
`gold_set.jsonl` can be traced back to the draw that produced it. Stratum
`top100` is the 100 most common descriptions (78% of active Manhattan sign
rows, docs/DATA.md §1.4); stratum `tail` is a uniform draw from the other
1,690, which is where the odd phrasings and the garbled strings live.

Usage: python tests/gold/sample_gold.py [corpus.tsv] > sample.tsv
"""

from __future__ import annotations

import csv
import random
import sys
from pathlib import Path

CORPUS = Path("data/explore/descriptions.tsv")
SEED = 20260914
TOP_N = 100
TAIL_N = 420


class SignRow:
    """One line of the corpus: a distinct description and how often it is posted."""

    def __init__(self, count: int, codes: list[str], description: str) -> None:
        self.count = count
        self.codes = codes
        self.description = description


def read_corpus(path: Path) -> list[SignRow]:
    """Read the `count / sign_codes / sign_description` TSV.

    The `sign_codes` cell is `CODE:count` pairs joined by `,` (one pair on
    every Manhattan row, per docs/DATA.md §1.3); only the code is kept.
    """
    rows: list[SignRow] = []
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        for record in reader:
            codes = [pair.split(":")[0] for pair in record["sign_codes"].split(",")]
            rows.append(SignRow(int(record["count"]), codes, record["sign_description"]))
    return rows


def stratify(rows: list[SignRow]) -> list[tuple[SignRow, str]]:
    """Return every row of the top-100 stratum plus a fixed random tail draw."""
    # Ties on count are broken by description so the split does not depend on
    # the order the corpus file happened to be written in.
    ordered = sorted(rows, key=lambda row: (-row.count, row.description))
    top = ordered[:TOP_N]
    tail_pool = ordered[TOP_N:]
    # A fixed-seed draw is the point here, not unpredictability.
    tail = random.Random(SEED).sample(tail_pool, TAIL_N)  # noqa: S311
    tail.sort(key=lambda row: (-row.count, row.description))
    return [(row, "top100") for row in top] + [(row, "tail") for row in tail]


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else CORPUS
    writer = csv.writer(sys.stdout, delimiter="\t", lineterminator="\n")
    writer.writerow(["count", "codes", "stratum", "description"])
    for row, stratum in stratify(read_corpus(path)):
        writer.writerow([row.count, ",".join(row.codes), stratum, row.description])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
