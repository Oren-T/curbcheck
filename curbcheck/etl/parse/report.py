"""Coverage of the sign grammar over the live Manhattan description vocabulary.

Reads `data/explore/descriptions.tsv` (count, sign_codes, sign_description),
parses every string and splits the outcome two ways: by distinct string, and
weighted by how many sign rows carry it. The weighted number over *regulation*
rows is the one SPEC §8.6 sets a gate on; panels are excluded from it by
decision D10. The residue — everything unparsed or partial — is written out so
the grammar can be extended against it.

`curbcheck parse-report` and `scripts/parse_report.py` are both thin callers.
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from curbcheck.config import DATA_DIR
from curbcheck.etl.parse import parse_description
from curbcheck.model import ParsedSign, ParseMethod

DEFAULT_CORPUS = DATA_DIR / "explore" / "descriptions.tsv"
DEFAULT_RESIDUE = DATA_DIR / "explore" / "parse_residue.tsv"

PANEL = "panel"
FULL = "grammar full"
PARTIAL = "grammar partial"
UNPARSED = "unparsed"

OUTCOMES: tuple[str, ...] = (FULL, PARTIAL, UNPARSED, PANEL)


class CorpusMissingError(FileNotFoundError):
    """The description corpus has not been produced yet."""


@dataclass(frozen=True)
class ResidueRow:
    """One description the grammar could not fully read."""

    count: int
    outcome: str
    notes: str
    description: str


@dataclass(frozen=True)
class ParseCoverage:
    """Outcome counts by distinct string and weighted by sign rows."""

    by_string: dict[str, int]
    by_row: dict[str, int]
    residue: tuple[ResidueRow, ...]

    @property
    def total_strings(self) -> int:
        return sum(self.by_string.values())

    @property
    def total_rows(self) -> int:
        return sum(self.by_row.values())

    @property
    def regulation_rows(self) -> int:
        return self.total_rows - self.by_row[PANEL]

    @property
    def regulation_strings(self) -> int:
        return self.total_strings - self.by_string[PANEL]

    @property
    def full_share(self) -> float:
        """Share of regulation sign rows the grammar read with every token consumed."""
        return self.by_row[FULL] / self.regulation_rows if self.regulation_rows else 0.0


def outcome(parsed: ParsedSign) -> str:
    if parsed.parse_method is ParseMethod.UNPARSED:
        return UNPARSED
    if not parsed.regulations:
        return PANEL
    return FULL if parsed.confidence >= 1.0 else PARTIAL


def read_corpus(path: Path) -> list[tuple[int, str]]:
    """`(row count, description)` pairs from the TSV `scripts/explore_signs.py` writes."""
    if not path.exists():
        raise CorpusMissingError(f"missing {path}; run scripts/explore_signs.py first")
    with path.open(newline="", encoding="utf-8") as handle:
        return [
            (int(record["count"]), record["sign_description"])
            for record in csv.DictReader(handle, delimiter="\t")
        ]


def measure(corpus: Sequence[tuple[int, str]]) -> ParseCoverage:
    by_string = dict.fromkeys(OUTCOMES, 0)
    by_row = dict.fromkeys(OUTCOMES, 0)
    residue: list[ResidueRow] = []
    for count, description in corpus:
        parsed = parse_description(description)
        label = outcome(parsed)
        by_string[label] += 1
        by_row[label] += count
        if label in (UNPARSED, PARTIAL):
            residue.append(ResidueRow(count, label, parsed.notes, description))
    residue.sort(key=lambda row: (-row.count, row.description))
    return ParseCoverage(by_string=by_string, by_row=by_row, residue=tuple(residue))


def write_residue(coverage: ParseCoverage, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, delimiter="\t")
        writer.writerow(("count", "outcome", "notes", "sign_description"))
        writer.writerows(
            (row.count, row.outcome, row.notes, row.description) for row in coverage.residue
        )


def format_report(coverage: ParseCoverage, residue_path: Path) -> str:
    lines = [
        f"{coverage.total_strings} distinct descriptions, {coverage.total_rows} sign rows",
        "",
        f"{'outcome':<16}{'strings':>9}{'':>4}{'rows':>9}{'':>4}{'% of reg rows':>14}",
    ]
    for label in OUTCOMES:
        share = (
            ""
            if label == PANEL or not coverage.regulation_rows
            else f"{100 * coverage.by_row[label] / coverage.regulation_rows:13.2f}%"
        )
        lines.append(
            f"{label:<16}{coverage.by_string[label]:>9}{'':>4}"
            f"{coverage.by_row[label]:>9}{'':>4}{share:>14}"
        )
    residue_rows = sum(row.count for row in coverage.residue)
    lines += [
        "",
        f"regulation rows: {coverage.regulation_rows} ({coverage.regulation_strings} strings);"
        f" panels: {coverage.by_row[PANEL]} ({coverage.by_string[PANEL]} strings)",
        f"fully parsed, weighted by row count: {100 * coverage.full_share:.2f}%",
        f"residue: {len(coverage.residue)} strings, {residue_rows} rows -> {residue_path}",
    ]
    return "\n".join(lines)


def run(
    corpus_path: Path = DEFAULT_CORPUS, residue_path: Path = DEFAULT_RESIDUE
) -> tuple[ParseCoverage, str]:
    """Measure coverage, write the residue file, and return the report text."""
    coverage = measure(read_corpus(corpus_path))
    write_residue(coverage, residue_path)
    return (coverage, format_report(coverage, residue_path))
