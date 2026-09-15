"""Re-run the 30 ground-truth sample sides and compare with what docs/VALIDATION.md recorded.

`scripts/validation_dossier.py` needs the server; this does not. It reads the
sample table and the results table out of `docs/VALIDATION.md` (§1 and §2), and
for each blockface-side evaluates every span the database holds for it over the
three SPEC §13.3 windows, straight through `engine.resolve`. The point is to
answer one question after a change to the ETL: does each of the 30 sides still
say what DOT said, and did the sides that used to return nothing start
returning something?

    python scripts/validation_regress.py [--db data/curbcheck.sqlite]
                                         [--baseline data/curbcheck.sqlite.prev]

With `--baseline`, a side DOT agreed with must still read the same way; the
baseline database is where "the same way" comes from, because §2 records
agreement rather than the verdict itself. Exit status is the number of sides
that need a human to look at them.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from curbcheck import db
from curbcheck.config import DB_PATH, NYC_TZ, REPO_ROOT
from curbcheck.engine.resolve import RegulationWithMeta, Verdict, evaluate_segment
from curbcheck.engine.search import load_calendar
from curbcheck.model import ParseMethod

VALIDATION_MD = REPO_ROOT / "docs" / "VALIDATION.md"

# SPEC §13.3's three windows, the ones §2's W/S/Su columns were scored over.
WINDOWS = (
    ("W", datetime(2026, 9, 16, 10, 0, tzinfo=NYC_TZ), datetime(2026, 9, 16, 12, 0, tzinfo=NYC_TZ)),
    ("S", datetime(2026, 9, 19, 9, 0, tzinfo=NYC_TZ), datetime(2026, 9, 19, 11, 0, tzinfo=NYC_TZ)),
    ("Su", datetime(2026, 9, 20, 14, 0, tzinfo=NYC_TZ), datetime(2026, 9, 20, 16, 0, tzinfo=NYC_TZ)),
)

_SEGMENT_SQL = (
    "SELECT reg_seg_id, derived_from FROM regulation_segment WHERE segment_id = ? AND side = ?"
    " ORDER BY start_ft, end_ft"
)
_RULES_SQL = "SELECT * FROM regulation WHERE reg_seg_id = ?"

# A row of §1's sample table: "| 9 | village/stacked | 8 AVE | E | ... | 1100 |".
_SAMPLE_ROW = re.compile(
    r"^\|\s*(\d+)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([NSEW])\s*\|\s*([^|]*?)\s*\|"
    r"\s*(\d+)\s*\|$"
)
# A row of §2's results table: "| 9 | ✓ 6/6 | ✓ | ✗ | ✗ | ✗ | note |".
_RESULT_ROW = re.compile(r"^\|\s*(\d+)\s*\|((?:[^|]*\|){5})\s*([^|]*)\|$")


@dataclass(frozen=True)
class Sample:
    """One sampled blockface-side, as docs/VALIDATION.md §1 and §2 record it."""

    number: int
    stratum: str
    on_street: str
    side: str
    between: str
    segment_id: str
    recorded: tuple[str, ...]
    """The W / S / Su marks from §2: agreement with DOT, not a verdict."""
    note: str


def parse_validation(path: Path) -> list[Sample]:
    """Read §1's sample table and §2's result table out of the markdown."""
    samples: dict[int, dict[str, str]] = {}
    results: dict[int, tuple[tuple[str, ...], str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        sample = _SAMPLE_ROW.match(line.strip())
        if sample is not None:
            samples[int(sample.group(1))] = {
                "stratum": sample.group(2),
                "on_street": sample.group(3),
                "side": sample.group(4),
                "between": sample.group(5),
                "segment_id": sample.group(6),
            }
            continue
        result = _RESULT_ROW.match(line.strip())
        if result is not None:
            cells = [cell.strip() for cell in result.group(2).split("|")[:5]]
            results[int(result.group(1))] = (tuple(cells[2:5]), result.group(3).strip())
    return [
        Sample(
            number=number,
            recorded=results.get(number, ((), ""))[0],
            note=results.get(number, ((), ""))[1],
            **fields,  # type: ignore[arg-type]
        )
        for number, fields in sorted(samples.items())
    ]


def side_verdicts(conn: sqlite3.Connection, sample: Sample) -> dict[str, str]:
    """The verdict of each window for one side: the best any span on it offers.

    A driver parks in one span, so a side reads as legal when any of its spans
    is legal for the whole window; `no_data` only when every span is a
    placeholder or carries no rule at all.
    """
    calendar = load_calendar(conn)
    spans = list(conn.execute(_SEGMENT_SQL, (sample.segment_id, sample.side)))
    verdicts = {}
    for label, t1, t2 in WINDOWS:
        seen = [
            evaluate_segment(_stack(conn, str(row["reg_seg_id"])), t1, t2, calendar).verdict
            for row in spans
        ]
        verdicts[label] = _best(seen).value if seen else "none"
    return verdicts


def _stack(conn: sqlite3.Connection, reg_seg_id: str) -> list[RegulationWithMeta]:
    return [
        RegulationWithMeta(
            regulation=db.regulation_from_row(row),
            parse_method=ParseMethod(str(row["parse_method"])),
            parse_confidence=float(row["parse_confidence"]),
            reg_seg_id=reg_seg_id,
            raw_sign_description=str(row["raw_sign_description"]),
        )
        for row in conn.execute(_RULES_SQL, (reg_seg_id,))
    ]


_BEST_FIRST = (Verdict.LEGAL, Verdict.AMBIGUOUS, Verdict.ILLEGAL, Verdict.NO_DATA)


def _best(verdicts: list[Verdict]) -> Verdict:
    for verdict in _BEST_FIRST:
        if verdict in verdicts:
            return verdict
    return Verdict.NO_DATA


def span_counts(conn: sqlite3.Connection, sample: Sample) -> tuple[int, int]:
    """(real spans, placeholders) on one side."""
    # A placeholder is recognised by its empty `derived_from` rather than by
    # `gap_kind`, so a pre-fix database without the column still reads.
    rows = list(conn.execute(_SEGMENT_SQL, (sample.segment_id, sample.side)))
    placeholders = sum(1 for row in rows if not json.loads(str(row["derived_from"])))
    return (len(rows) - placeholders, placeholders)


def status(
    sample: Sample, now: dict[str, str], before: dict[str, str] | None, real_spans: int
) -> str:
    """Whether this side still says what docs/VALIDATION.md §2 says DOT said.

    §2 records agreement, not a verdict, so an "agreed" row is checked against
    the baseline database and a "disagreed" or "gap" row against the fix it was
    written to demand: a side DOT posts signs on must now return a real span.
    """
    marks = set(sample.recorded)
    if not marks or marks <= {"—", "-", ""}:
        return "REVIEW: no verdict recorded"
    if marks <= {"✓"}:
        if before is None:
            return "ok (agreed with DOT; no baseline to compare against)"
        return "ok (unchanged)" if before == now else f"REVIEW: was {before}, now {now}"
    if "gap" in marks:
        if "data gap" in sample.note:
            return "ok (data gap: DOT publishes nothing here either)"
        return "ok (gap closed)" if real_spans else "REVIEW: still no span"
    if "✗" in marks:
        return "ok (was wrong; now legal)" if "legal" in now.values() else "REVIEW: still no legal"
    return f"REVIEW: recorded {'/'.join(sample.recorded)}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--baseline", type=Path, default=None)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()

    samples = parse_validation(VALIDATION_MD)
    conn = _open(args.db)
    baseline = _open(args.baseline) if args.baseline else None
    report = []
    try:
        print(f"{len(samples)} sampled sides from {VALIDATION_MD}, three SPEC §13.3 windows\n")
        print(f"{'#':>3} {'side':<34} {'recorded':<10} {'W/S/Su now':<30} spans  status")
        for sample in samples:
            now = side_verdicts(conn, sample)
            before = side_verdicts(baseline, sample) if baseline else None
            real, placeholders = span_counts(conn, sample)
            line = status(sample, now, before, real)
            label = f"{sample.on_street} {sample.side} seg {sample.segment_id}"
            shown = "/".join(now[key] for key, _, _ in WINDOWS)
            print(
                f"{sample.number:>3} {label:<34} {'/'.join(sample.recorded):<10}"
                f" {shown:<30} {real}+{placeholders}  {line}"
            )
            report.append({"n": sample.number, "now": now, "before": before, "status": line})
    finally:
        conn.close()
        if baseline is not None:
            baseline.close()

    needs_review = sum(1 for row in report if row["status"].startswith("REVIEW"))
    print(f"\n{len(report) - needs_review} of {len(report)} sides agree; {needs_review} to review")
    if args.json:
        args.json.write_text(json.dumps(report, indent=1))
    return needs_review


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


if __name__ == "__main__":
    raise SystemExit(main())
