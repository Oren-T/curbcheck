"""Score the sign grammar against the hand-labeled gold set (SPEC §8.6).

    python scripts/eval_gold.py [--gold PATH] [--max-detail N]

Two numbers, both weighted by how many active sign rows carry each description:

- **exact match** — the whole `ParsedSign` minus `notes`, with the regulation
  list compared as a set of canonical JSON objects. This is the strict §8.6
  metric and it punishes every representation difference.
- **semantic match** — the passenger-car *effect* of the rules over a canonical
  week at 5-minute resolution: for every slot, may a passenger car park, is it
  metered, what limit applies, and which calendar-conditional flags are on. The
  parser and the labeler were written independently and legitimately disagree
  about how to write some signs down (`TRUCK LOADING ONLY` as a permitted-truck
  exclusive on `park` or on `stand`; a hotel loading zone as an exclusive or as
  a plain prohibition). Those differences cannot change what a driver of a
  passenger car may do, and `tests/gold/ADJUDICATIONS.md` records each class we
  accepted.

`confidence` is compared in the exact match and in the per-field table but not
in the effect: the gold README and the parser docstring define two different
confidence scales, so the number says more about who wrote the sign down than
about the curb. Where the two land on opposite sides of
`resolve.AMBIGUITY_THRESHOLD` the report says so under "readability".

The gate that matters most is the last one: a **false permitted**, a slot where
the label prohibits a passenger car and the parser does not, is a P0 defect
(SPEC §8.6). Those are printed first and force a non-zero exit on their own.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from curbcheck.engine.resolve import AMBIGUITY_THRESHOLD
from curbcheck.etl.parse import parse_description
from curbcheck.model import Action, ParsedSign, ParseMethod, Regulation

GOLD_PATH = Path("tests/gold/gold_set.jsonl")

# SPEC §8.6 acceptance gates.
OVERALL_GATE = 0.95
TEMPLATED_GATE = 0.98
TEMPLATED_STRATUM = "top100"

# A finer grid would only split intervals no sign boundary falls in: every
# posted NYC time is on a 30-minute boundary at worst (7:30AM, 10:30AM).
SLOT_MINUTES = 5
SLOTS_PER_DAY = 24 * 60 // SLOT_MINUTES
WEEK_SLOTS = 7 * SLOTS_PER_DAY

# Codes for the per-slot passenger-car reading, in the same three states
# `engine.resolve.resolve_interval` produces.
NO_RULE = 0
PERMITTED = 1
PROHIBITED = 2

# A leap year, so a 02-29 season boundary is a real date.
_SAMPLE_YEAR = 2024
_DEFAULT_SAMPLE = (3, 1)

FIELDS: tuple[str, ...] = (
    "action",
    "permitted",
    "vehicle_class",
    "exclusive",
    "days",
    "time_from",
    "time_to",
    "metered",
    "max_duration_min",
    "flags.street_cleaning",
    "flags.school_days",
    "flags.except_sunday",
    "flags.including_sunday",
    "flags.snow_emergency",
    "flags.holiday_exempt",
    "flags.temporary",
    "flags.meta",
    "effective_from",
    "effective_to",
    "arrow",
    "parse_method",
    "confidence",
)


@dataclass(frozen=True)
class GoldRow:
    """One line of `gold_set.jsonl`."""

    description: str
    count: int
    stratum: str
    label: ParsedSign
    labeler_notes: str


@dataclass(frozen=True)
class Effect:
    """What one sign's rules let a passenger car do, slot by slot over a canonical week."""

    allowed: bytes
    metered: bytes
    limits: tuple[int | None, ...]
    street_cleaning: bytes
    snow_emergency: bytes
    school_days: bytes


@dataclass(frozen=True)
class Comparison:
    """The verdict on one gold row."""

    row: GoldRow
    parsed: ParsedSign
    exact: bool
    semantic: bool
    differing_fields: tuple[str, ...]
    false_permitted_slots: tuple[int, ...]
    stricter_slots: tuple[int, ...]
    label_readable: bool
    parser_readable: bool

    @property
    def readability_differs(self) -> bool:
        return self.label_readable != self.parser_readable


def load_gold(path: Path = GOLD_PATH) -> list[GoldRow]:
    """Read the gold set. Raises if a label does not validate against the schema."""
    rows: list[GoldRow] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            rows.append(
                GoldRow(
                    description=record["description"],
                    count=int(record["count"]),
                    stratum=record["stratum"],
                    label=ParsedSign.model_validate(record["label"]),
                    labeler_notes=record.get("labeler_notes", ""),
                )
            )
    return rows


def evaluate(rows: Sequence[GoldRow]) -> list[Comparison]:
    return [compare(row) for row in rows]


def compare(row: GoldRow) -> Comparison:
    parsed = parse_description(row.description)
    samples = _season_samples((*row.label.regulations, *parsed.regulations))
    label_effect = _rules_effect(tuple(row.label.regulations), samples)
    parser_effect = _rules_effect(tuple(parsed.regulations), samples)
    parser_readable = is_readable(parsed)
    false_permitted, stricter = _slot_disagreements(
        label_effect, parser_effect, parser_readable=parser_readable
    )
    return Comparison(
        row=row,
        parsed=parsed,
        exact=canonical(row.label) == canonical(parsed),
        semantic=label_effect == parser_effect,
        differing_fields=tuple(
            field
            for field in FIELDS
            if field_values(row.label, field) != field_values(parsed, field)
        ),
        false_permitted_slots=false_permitted,
        stricter_slots=stricter,
        label_readable=is_readable(row.label),
        parser_readable=parser_readable,
    )


def canonical(sign: ParsedSign) -> tuple[Any, ...]:
    """The strict §8.6 comparison key: everything but `notes`, regulations as a set."""
    dumped = sign.model_dump(mode="json")
    regulations = sorted(json.dumps(rule, sort_keys=True) for rule in dumped["regulations"])
    return (dumped["parse_method"], dumped["confidence"], tuple(regulations))


def field_values(sign: ParsedSign, field: str) -> Any:
    """The value of one comparison field, as a sorted multiset over the regulations."""
    if field == "parse_method":
        return sign.parse_method.value
    if field == "confidence":
        return sign.confidence
    dumped = sign.model_dump(mode="json")
    return sorted(json.dumps(_dig(rule, field)) for rule in dumped["regulations"])


def _dig(rule: dict[str, Any], field: str) -> Any:
    head, _, tail = field.partition(".")
    return rule[head][tail] if tail else rule[head]


def is_readable(sign: ParsedSign) -> bool:
    """Whether `engine.resolve` would render a verdict from this sign standing alone.

    Mirrors `resolve.ambiguity_reason` for a one-sign stack: an unreadable sign
    makes the whole segment AMBIGUOUS whatever its regulations say.
    """
    if sign.parse_method is ParseMethod.UNPARSED:
        return False
    if any(rule.flags.meta for rule in sign.regulations):
        return False
    return sign.confidence >= AMBIGUITY_THRESHOLD


def _rules_effect(rules: tuple[Regulation, ...], samples: tuple[tuple[int, int], ...]) -> Effect:
    """The passenger-car reading of a rule list, slot by slot over every season sample.

    Most-restrictive-wins, exactly as `resolve.resolve_interval` does it: a rule
    that `applies_to_passenger()` denies prohibits the slot whatever else covers
    it, and only a permitted `park` rule contributes a meter or a time limit.
    The calendar-conditional flags are not applied (we have no calendar here);
    they are reported per slot instead, so a lost broom or snow symbol shows up
    as a mismatch rather than silently changing nothing.
    """
    span = len(samples) * WEEK_SLOTS
    allowed = bytearray(span)
    metered = bytearray(span)
    limits: list[int | None] = [None] * span
    street_cleaning = bytearray(span)
    snow = bytearray(span)
    school = bytearray(span)

    for rule in rules:
        passenger = rule.applies_to_passenger()
        if passenger is None:
            continue
        slots = _active_slots(rule, samples)
        if passenger is False:
            for slot in slots:
                allowed[slot] = PROHIBITED
        elif rule.action is Action.PARK:
            for slot in slots:
                if allowed[slot] != PROHIBITED:
                    allowed[slot] = PERMITTED
                if rule.metered:
                    metered[slot] = 1
                limits[slot] = _tighter(limits[slot], rule.max_duration_min)
        for flag, target in (
            (rule.flags.street_cleaning, street_cleaning),
            (rule.flags.snow_emergency, snow),
            (rule.flags.school_days, school),
        ):
            if flag:
                for slot in slots:
                    target[slot] = 1

    return Effect(
        allowed=bytes(allowed),
        metered=bytes(metered),
        limits=tuple(limits),
        street_cleaning=bytes(street_cleaning),
        snow_emergency=bytes(snow),
        school_days=bytes(school),
    )


def _tighter(current: int | None, candidate: int | None) -> int | None:
    if candidate is None:
        return current
    return candidate if current is None else min(current, candidate)


def _active_slots(rule: Regulation, samples: tuple[tuple[int, int], ...]) -> list[int]:
    """Week-slot indices the rule is in force in, for every season sample."""
    day_mask = _day_mask(rule.time_from, rule.time_to)
    days = set(rule.days)
    if rule.flags.except_sunday:
        days.discard(6)
    slots: list[int] = []
    for sample_index, sample in enumerate(samples):
        if not _in_season(rule, sample):
            continue
        base = sample_index * WEEK_SLOTS
        for weekday in sorted(days):
            offset = base + weekday * SLOTS_PER_DAY
            slots.extend(offset + minute for minute in day_mask)
    return slots


@lru_cache(maxsize=1024)
def _day_mask(time_from: str | None, time_to: str | None) -> tuple[int, ...]:
    """Slot indices within a day the posted window covers; a wrap past midnight splits."""
    if time_from is None or time_to is None:
        return tuple(range(SLOTS_PER_DAY))
    start = _slot_of(time_from)
    end = _slot_of(time_to)
    if end > start:
        return tuple(range(start, end))
    # `time_to <= time_from` wraps (SPEC §8.4 ex. 15); equal ends mean all day.
    return tuple(range(start, SLOTS_PER_DAY)) + tuple(range(end))


def _slot_of(hhmm: str) -> int:
    return (int(hhmm[:2]) * 60 + int(hhmm[3:])) // SLOT_MINUTES


def _in_season(rule: Regulation, sample: tuple[int, int]) -> bool:
    if rule.effective_from is None or rule.effective_to is None:
        return True
    start = _mmdd(rule.effective_from)
    end = _mmdd(rule.effective_to)
    if start <= end:
        return start <= sample <= end
    return sample >= start or sample <= end


def _mmdd(value: str) -> tuple[int, int]:
    return (int(value[:2]), int(value[3:]))


def _season_samples(rules: Iterable[Regulation]) -> tuple[tuple[int, int], ...]:
    """One representative date per seasonal segment the two rule lists cut the year into.

    Seasons are the only date-dependent thing in the schema, so sampling the
    first day of every segment the boundaries create covers the whole year.
    """
    boundaries: set[tuple[int, int]] = set()
    for rule in rules:
        if rule.effective_from is None or rule.effective_to is None:
            continue
        boundaries.add(_mmdd(rule.effective_from))
        boundaries.add(_day_after(_mmdd(rule.effective_to)))
    return tuple(sorted(boundaries)) if boundaries else (_DEFAULT_SAMPLE,)


def _day_after(sample: tuple[int, int]) -> tuple[int, int]:
    moment = date(_SAMPLE_YEAR, sample[0], sample[1]) + timedelta(days=1)
    return (moment.month, moment.day)


def _slot_disagreements(
    label: Effect, parser: Effect, *, parser_readable: bool
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Slots where the parser lets a passenger car park and the label does not, and vice versa.

    A parse the engine refuses to read is not a false permitted: it renders
    AMBIGUOUS, never LEGAL. Everything else is — including an empty regulation
    list from the grammar path, which the engine reads as "no rule here" and
    turns into a LEGAL verdict by absence (SPEC §9.2, 34 RCNY 4-08).
    """
    if not parser_readable:
        return ((), ())
    false_permitted = tuple(
        slot
        for slot in range(len(label.allowed))
        if label.allowed[slot] == PROHIBITED and parser.allowed[slot] != PROHIBITED
    )
    stricter = tuple(
        slot
        for slot in range(len(label.allowed))
        if parser.allowed[slot] == PROHIBITED and label.allowed[slot] != PROHIBITED
    )
    return false_permitted, stricter


def false_permitted(results: Sequence[Comparison]) -> list[Comparison]:
    return [result for result in results if result.false_permitted_slots]


def rate(results: Iterable[Comparison], *, semantic: bool, weighted: bool) -> float:
    """Share of gold rows (or of the sign rows they stand for) that match."""
    total = 0
    hit = 0
    for result in results:
        weight = result.row.count if weighted else 1
        total += weight
        if result.semantic if semantic else result.exact:
            hit += weight
    return hit / total if total else 1.0


def gate_failures(results: Sequence[Comparison]) -> list[str]:
    """Every SPEC §8.6 acceptance gate this run does not clear."""
    failures: list[str] = []
    overall = rate(results, semantic=True, weighted=True)
    if overall < OVERALL_GATE:
        failures.append(f"overall semantic match {overall:.4%} < {OVERALL_GATE:.0%}")
    templated = [r for r in results if r.row.stratum == TEMPLATED_STRATUM]
    templated_rate = rate(templated, semantic=True, weighted=True)
    if templated_rate < TEMPLATED_GATE:
        failures.append(
            f"{TEMPLATED_STRATUM} semantic match {templated_rate:.4%} < {TEMPLATED_GATE:.0%}"
        )
    offenders = false_permitted(results)
    if offenders:
        failures.append(
            f"{len(offenders)} descriptions read as permitted where the label prohibits"
        )
    return failures


def brief(sign: ParsedSign) -> str:
    """One-line rendering of a ParsedSign: method, confidence and the rules that differ from default."""
    rules = " ;; ".join(_brief_rule(rule) for rule in sign.regulations) or "(no regulations)"
    return f"{sign.parse_method.value}@{sign.confidence:g} {rules}"


def _brief_rule(rule: Regulation) -> str:
    default = Regulation(action=rule.action, permitted=rule.permitted)
    parts = [rule.action.value, "permitted" if rule.permitted else "prohibited"]
    dumped = rule.model_dump(mode="json")
    base = default.model_dump(mode="json")
    for name in ("vehicle_class", "exclusive", "days", "metered", "max_duration_min", "arrow"):
        if dumped[name] != base[name]:
            parts.append(f"{name}={dumped[name]}")
    if rule.time_from is not None:
        parts.append(f"{rule.time_from}-{rule.time_to}")
    if rule.effective_from is not None:
        parts.append(f"season {rule.effective_from}..{rule.effective_to}")
    flags = [name for name, value in dumped["flags"].items() if value]
    if flags:
        parts.append("flags=" + ",".join(flags))
    return " ".join(parts)


def _slot_when(slot: int) -> str:
    weekday = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[(slot // SLOTS_PER_DAY) % 7]
    minute = (slot % SLOTS_PER_DAY) * SLOT_MINUTES
    return f"{weekday} {minute // 60:02d}:{minute % 60:02d}"


def slot_ranges(slots: Sequence[int], limit: int = 3) -> str:
    """`Mon 08:00-Mon 18:00, ...` for a run of slot indices."""
    if not slots:
        return ""
    runs: list[tuple[int, int]] = []
    start = previous = slots[0]
    for slot in slots[1:]:
        if slot != previous + 1:
            runs.append((start, previous))
            start = slot
        previous = slot
    runs.append((start, previous))
    shown = ", ".join(f"{_slot_when(a)}-{_slot_when(b + 1)}" for a, b in runs[:limit])
    return shown + (f" (+{len(runs) - limit} more)" if len(runs) > limit else "")


def _print_false_permitted(results: Sequence[Comparison]) -> None:
    offenders = false_permitted(results)
    print("=" * 100)
    if not offenders:
        print(
            "FALSE PERMITTED: none. No gold description is read as parkable where the label prohibits."
        )
        print("=" * 100)
        return
    rows = sum(result.row.count for result in offenders)
    print(
        f"!!! FALSE PERMITTED: {len(offenders)} descriptions, {rows} active sign rows. P0 (SPEC §8.6)."
    )
    print("=" * 100)
    for result in offenders:
        print(f"\n  {result.row.description}")
        print(f"    label  {brief(result.row.label)}")
        print(f"    parser {brief(result.parsed)}")
        print(f"    parser permits a passenger car at: {slot_ranges(result.false_permitted_slots)}")
    print("=" * 100)


def _print_rates(results: Sequence[Comparison]) -> None:
    strata = sorted({result.row.stratum for result in results})
    print("\nmatch rates (weighted by active sign rows, unweighted by distinct description)\n")
    header = f"{'stratum':<10}{'signs':>7}{'rows':>9}{'exact wt':>11}{'semantic wt':>13}{'exact':>9}{'semantic':>10}"
    print(header)
    print("-" * len(header))
    for stratum in [*strata, "ALL"]:
        subset = [r for r in results if stratum in (r.row.stratum, "ALL")]
        print(
            f"{stratum:<10}{len(subset):>7}{sum(r.row.count for r in subset):>9}"
            f"{rate(subset, semantic=False, weighted=True):>11.2%}"
            f"{rate(subset, semantic=True, weighted=True):>13.2%}"
            f"{rate(subset, semantic=False, weighted=False):>9.2%}"
            f"{rate(subset, semantic=True, weighted=False):>10.2%}"
        )


def _print_fields(results: Sequence[Comparison]) -> None:
    print(
        "\nper-field agreement (share of the 520 descriptions whose whole multiset of values agrees)\n"
    )
    header = f"{'field':<26}{'signs':>8}{'rows':>9}{'differing':>11}"
    print(header)
    print("-" * len(header))
    total_signs = len(results)
    total_rows = sum(result.row.count for result in results)
    for field in FIELDS:
        differing = [result for result in results if field in result.differing_fields]
        agree_rows = total_rows - sum(result.row.count for result in differing)
        print(
            f"{field:<26}{(total_signs - len(differing)) / total_signs:>8.2%}"
            f"{agree_rows / total_rows:>9.2%}{len(differing):>11}"
        )


def _print_mismatches(results: Sequence[Comparison], max_detail: int) -> None:
    mismatches = [result for result in results if not result.exact]
    semantic_only = [result for result in mismatches if result.semantic]
    real = [result for result in mismatches if not result.semantic]
    print(
        f"\n{len(mismatches)} exact mismatches: {len(semantic_only)} with identical passenger-car"
        f" effect, {len(real)} with a different effect.\n"
    )
    for title, subset in (("EFFECT DIFFERS", real), ("REPRESENTATION ONLY", semantic_only)):
        print(f"--- {title} ({len(subset)}) " + "-" * 40)
        for result in sorted(subset, key=lambda r: -r.row.count)[:max_detail]:
            print(f"\n[{result.row.stratum} n={result.row.count}] {result.row.description}")
            print(f"  label  {brief(result.row.label)}")
            print(f"  parser {brief(result.parsed)}")
            print(f"  diff   {', '.join(result.differing_fields) or '(notes only)'}")
            if result.stricter_slots and not result.semantic:
                print(
                    f"  parser prohibits where the label does not: {slot_ranges(result.stricter_slots)}"
                )
        if len(subset) > max_detail:
            print(f"\n  ... {len(subset) - max_detail} more suppressed by --max-detail")
        print()


def _print_readability(results: Sequence[Comparison]) -> None:
    """Signs the engine would read confidently from one side only.

    `resolve.ambiguity_reason` turns an unparsed sign, a `flags.meta` sign or a
    confidence below `AMBIGUITY_THRESHOLD` into an AMBIGUOUS verdict, so these
    are the rows where the two sides send a different word to the user even
    when they agree about the curb.
    """
    split = [result for result in results if result.readability_differs]
    print(
        f"\nreadability (engine renders a verdict at all): {len(split)} descriptions differ,"
        f" {sum(r.row.count for r in split)} rows"
    )
    for result in sorted(split, key=lambda r: -r.row.count):
        side = (
            "parser ambiguous, label readable"
            if result.label_readable
            else "parser readable, label ambiguous"
        )
        print(
            f"  {side}: label@{result.row.label.confidence:g} parser@{result.parsed.confidence:g}"
            f" n={result.row.count}  {result.row.description[:80]}"
        )


def _print_method_counts(results: Sequence[Comparison]) -> None:
    counts: Counter[str] = Counter()
    for result in results:
        counts[f"{result.row.label.parse_method.value} -> {result.parsed.parse_method.value}"] += 1
    print(
        "\nparse_method label -> parser: "
        + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
    )


def _report(results: Sequence[Comparison], max_detail: int) -> int:
    _print_false_permitted(results)
    _print_rates(results)
    _print_method_counts(results)
    _print_readability(results)
    _print_fields(results)
    _print_mismatches(results, max_detail)

    failures = gate_failures(results)
    print("=" * 100)
    if failures:
        print("GATES FAILED (SPEC §8.6):")
        for failure in failures:
            print(f"  - {failure}")
    else:
        print(
            f"GATES PASSED: overall semantic {rate(results, semantic=True, weighted=True):.2%}"
            f" >= {OVERALL_GATE:.0%}, {TEMPLATED_STRATUM} semantic"
            f" {rate([r for r in results if r.row.stratum == TEMPLATED_STRATUM], semantic=True, weighted=True):.2%}"
            f" >= {TEMPLATED_GATE:.0%}, zero false permitted."
        )
    print("=" * 100)
    return 1 if failures else 0


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gold", type=Path, default=GOLD_PATH, help="gold set JSONL")
    parser.add_argument("--max-detail", type=int, default=200, help="mismatches to print per class")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _arguments(argv)
    if not arguments.gold.exists():
        print(f"missing gold set: {arguments.gold}")
        return 2
    results = evaluate(load_gold(arguments.gold))
    return _report(results, arguments.max_detail)


if __name__ == "__main__":
    raise SystemExit(main())
