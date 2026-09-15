"""The SPEC §8.6 acceptance gates, run against the hand-labeled gold set.

`scripts/eval_gold.py` is the report; this is the gate. It fails the build when
the grammar regresses against the 520 independently labeled descriptions, and
in particular when any of them starts reading as parkable where the label
prohibits a passenger car.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GOLD = ROOT / "tests" / "gold" / "gold_set.jsonl"

# eval_gold lives in scripts/, which is not a package: it is a report first and
# a library second, and CI runs it both ways.
sys.path.insert(0, str(ROOT / "scripts"))

import eval_gold  # noqa: E402

pytestmark = pytest.mark.skipif(not GOLD.exists(), reason="gold set is missing")


@lru_cache(maxsize=1)
def results() -> tuple[eval_gold.Comparison, ...]:
    """Parse and score all 520 descriptions once for the whole module."""
    return tuple(eval_gold.evaluate(eval_gold.load_gold(GOLD)))


def test_the_gold_set_is_the_520_rows_the_readme_describes():
    rows = [result.row for result in results()]
    assert len(rows) == 520
    assert len({row.description for row in rows}) == 520
    assert sum(1 for row in rows if row.stratum == "top100") == 100


def test_no_description_reads_as_permitted_where_the_label_prohibits():
    """A false 'legal' is a P0 defect (SPEC §8.6): it costs the user a tow."""
    offenders = eval_gold.false_permitted(results())
    assert offenders == [], "\n".join(
        f"{result.row.description}\n  label  {eval_gold.brief(result.row.label)}"
        f"\n  parser {eval_gold.brief(result.parsed)}"
        f"\n  permitted at {eval_gold.slot_ranges(result.false_permitted_slots)}"
        for result in offenders
    )


def test_overall_semantic_match_clears_the_hybrid_gate():
    matched = eval_gold.rate(results(), semantic=True, weighted=True)
    assert matched >= eval_gold.OVERALL_GATE, _misses(results())


def test_templated_majority_semantic_match_clears_the_grammar_gate():
    templated = [
        result for result in results() if result.row.stratum == eval_gold.TEMPLATED_STRATUM
    ]
    matched = eval_gold.rate(templated, semantic=True, weighted=True)
    assert matched >= eval_gold.TEMPLATED_GATE, _misses(templated)


def test_every_gate_the_report_checks_passes():
    assert eval_gold.gate_failures(results()) == []


def _misses(subset) -> str:
    return "\n".join(
        f"[{result.row.stratum} n={result.row.count}] {result.row.description}"
        f"\n  label  {eval_gold.brief(result.row.label)}"
        f"\n  parser {eval_gold.brief(result.parsed)}"
        for result in subset
        if not result.semantic
    )
