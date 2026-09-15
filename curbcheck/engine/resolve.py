"""Collapse a stack of rules into one verdict, most-restrictive-wins, for a passenger car.

DOT's stacking rule: where several signs cover the same curb, the most
restrictive governs; where a sign is missing, the remaining posted regulations
apply (SPEC §9.2). A segment is LEGAL only if parking is permitted for every
sub-interval of the window and any posted time limit covers the whole window.

The asymmetry that drives the design: a false "illegal" costs the user a parking
spot, a false "legal" costs them a tow. Anything we cannot read confidently is
AMBIGUOUS (SPEC §8.6, §11).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from curbcheck.engine.window import (
    CalendarContext,
    Interval,
    expand_window,
    meter_is_charged,
    rule_is_active,
)
from curbcheck.model import Action, ParseMethod, Regulation

# A parse we trust less than this makes the whole segment ambiguous. SPEC §8.6
# sets the gold-set gate at 98% exact match for the grammar path; 0.8 is the
# per-rule floor below which we refuse to show a verdict.
AMBIGUITY_THRESHOLD = 0.8

# Least to most restrictive when prohibited, for picking what to report.
_PROHIBITION_RANK: dict[Action, int] = {Action.PARK: 0, Action.STAND: 1, Action.STOP: 2}

_PROHIBITION_PHRASE: dict[Action, str] = {
    Action.PARK: "no parking",
    Action.STAND: "no standing",
    Action.STOP: "no stopping",
}


class Verdict(StrEnum):
    """The four states the UI must keep visually distinct (SPEC §11)."""

    LEGAL = "legal"
    ILLEGAL = "illegal"
    AMBIGUOUS = "ambiguous"
    NO_DATA = "no_data"


@dataclass(frozen=True)
class RegulationWithMeta:
    """One parsed rule plus the provenance the verdict has to account for.

    An `unparsed` entry carries no usable rule: its `regulation` is a
    placeholder the ETL writes so the sign is visible in the stack, and the
    engine only ever reads its metadata.
    """

    regulation: Regulation
    parse_method: ParseMethod
    parse_confidence: float
    reg_seg_id: str
    raw_sign_description: str


@dataclass(frozen=True)
class IntervalOutcome:
    """What the stack says about one sub-interval of the window."""

    interval: Interval
    permitted: bool
    reason: str
    prohibiting_action: Action | None = None
    by_absence: bool = False
    metered: bool = False
    meter_charged: bool = False
    max_duration_min: int | None = None


@dataclass(frozen=True)
class SegmentVerdict:
    """The verdict for one regulation segment over the whole requested window."""

    verdict: Verdict
    reason: str
    intervals: list[IntervalOutcome] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    window_minutes: int = 0
    charged_minutes: int = 0
    metered: bool = False
    max_duration_min: int | None = None
    confidence: float = 1.0
    first_offending: IntervalOutcome | None = None

    @property
    def charged_intervals(self) -> list[IntervalOutcome]:
        return [outcome for outcome in self.intervals if outcome.meter_charged]


def resolve_interval(
    stack: list[RegulationWithMeta], interval: Interval, calendar: CalendarContext
) -> IntervalOutcome:
    """Most-restrictive-wins over the rules active in `interval`, read for a passenger car.

    Ambiguity is deliberately not considered here: it is a property of the whole
    segment, handled in `evaluate_segment`.
    """
    active = [item for item in stack if rule_is_active(item.regulation, interval, calendar)]
    prohibitions = [item for item in active if item.regulation.applies_to_passenger() is False]
    if prohibitions:
        worst = max(prohibitions, key=lambda item: _PROHIBITION_RANK[item.regulation.action])
        action = worst.regulation.action
        return IntervalOutcome(
            interval=interval,
            permitted=False,
            reason=f"{_prohibition_reason(worst.regulation)} {_when(interval)}",
            prohibiting_action=action,
        )

    permits = [
        item
        for item in active
        if item.regulation.action is Action.PARK and item.regulation.applies_to_passenger() is True
    ]
    if permits:
        limits = [
            item.regulation.max_duration_min
            for item in permits
            if item.regulation.max_duration_min is not None
        ]
        metered = any(item.regulation.metered for item in permits)
        charged = any(meter_is_charged(item.regulation, interval, calendar) for item in permits)
        return IntervalOutcome(
            interval=interval,
            permitted=True,
            reason="metered parking permitted" if metered else "parking permitted",
            metered=metered,
            meter_charged=charged,
            max_duration_min=min(limits) if limits else None,
        )

    # 34 RCNY 4-08: where no posted sign applies, parking is allowed. We still
    # mark it, because "no rule is in force right now" reads differently to a
    # user than "a sign says you may park here".
    return IntervalOutcome(
        interval=interval,
        permitted=True,
        reason="no posted rule is in force",
        by_absence=True,
    )


def evaluate_segment(
    stack: list[RegulationWithMeta],
    t1: datetime,
    t2: datetime,
    calendar: CalendarContext,
) -> SegmentVerdict:
    """Verdict for one segment over [t1, t2): legal only if every sub-interval permits parking."""
    if not stack:
        return SegmentVerdict(
            verdict=Verdict.NO_DATA,
            reason="no sign data on this block",
            window_minutes=_window_minutes(t1, t2),
            confidence=0.0,
        )

    intervals = expand_window(t1, t2, [item.regulation for item in stack])
    outcomes = [resolve_interval(stack, interval, calendar) for interval in intervals]
    window_minutes = sum(outcome.interval.minutes for outcome in outcomes)
    charged_minutes = sum(o.interval.minutes for o in outcomes if o.meter_charged)
    limits = [o.max_duration_min for o in outcomes if o.max_duration_min is not None]
    max_duration_min = min(limits) if limits else None
    confidence = min(item.parse_confidence for item in stack)
    caveats = _caveats(stack, outcomes, calendar)

    def decided(
        verdict: Verdict, reason: str, offending: IntervalOutcome | None = None
    ) -> SegmentVerdict:
        return SegmentVerdict(
            verdict=verdict,
            reason=reason,
            intervals=outcomes,
            caveats=caveats,
            window_minutes=window_minutes,
            charged_minutes=charged_minutes,
            metered=any(outcome.metered for outcome in outcomes),
            max_duration_min=max_duration_min,
            confidence=confidence,
            first_offending=offending,
        )

    ambiguity = ambiguity_reason(stack)
    if ambiguity is not None:
        return decided(Verdict.AMBIGUOUS, ambiguity)

    prohibited = next((outcome for outcome in outcomes if not outcome.permitted), None)
    if prohibited is not None:
        return decided(Verdict.ILLEGAL, prohibited.reason, prohibited)

    too_short = next(
        (
            outcome
            for outcome in outcomes
            if outcome.max_duration_min is not None and outcome.max_duration_min < window_minutes
        ),
        None,
    )
    if too_short is not None:
        return decided(
            Verdict.ILLEGAL,
            f"posted limit of {too_short.max_duration_min} min is shorter than"
            f" the {window_minutes} min window",
            too_short,
        )

    if all(outcome.by_absence for outcome in outcomes):
        return decided(Verdict.LEGAL, "no posted rule covers this window")
    return decided(Verdict.LEGAL, "parking permitted for the whole window")


def ambiguity_reason(stack: list[RegulationWithMeta]) -> str | None:
    """Why this stack cannot be read confidently, or None if it can.

    One unreadable sign poisons the whole segment: we cannot know whether the
    sign we failed to read is the one that prohibits parking (SPEC §11).
    """
    if any(item.parse_method is ParseMethod.UNPARSED for item in stack):
        return "a sign on this block could not be read"
    if any(item.regulation.flags.meta for item in stack):
        return "a sign here modifies another sign; the combination is not machine-readable"
    if any(item.parse_confidence < AMBIGUITY_THRESHOLD for item in stack):
        return "a sign on this block was read with low confidence"
    return None


def _caveats(
    stack: list[RegulationWithMeta],
    outcomes: list[IntervalOutcome],
    calendar: CalendarContext,
) -> list[str]:
    caveats: list[str] = []
    regulations = [item.regulation for item in stack]
    dates = sorted({outcome.interval.date for outcome in outcomes})

    if any(reg.flags.snow_emergency for reg in regulations):
        caveats.append(
            "snow emergency rule present; in force only when the city declares one"
            if not calendar.snow_emergency
            else "snow emergency declared; snow rules are in force"
        )
    if any(reg.flags.school_days for reg in regulations) and any(
        not calendar.is_known_non_school_day(day) for day in dates
    ):
        caveats.append("school-day rule assumed active")
    if any(reg.flags.temporary for reg in regulations):
        caveats.append("a sign here marks itself temporary")
    if any(reg.flags.street_cleaning for reg in regulations) and any(
        calendar.is_asp_suspended(day) for day in dates
    ):
        caveats.append("street cleaning is suspended on this date")
    if any(outcome.metered and not outcome.meter_charged for outcome in outcomes):
        caveats.append("meters are not in effect for part of this window")
    if any(outcome.by_absence for outcome in outcomes):
        caveats.append("part of this window has no posted rule; read the curb")
    return caveats


def _prohibition_reason(reg: Regulation) -> str:
    if reg.permitted and reg.exclusive:
        return f"reserved for {reg.vehicle_class.value} vehicles"
    return _PROHIBITION_PHRASE[reg.action]


def _when(interval: Interval) -> str:
    return f"{interval.start:%a %H:%M}-{interval.end:%H:%M}"


def _window_minutes(t1: datetime, t2: datetime) -> int:
    return max(0, round((t2.timestamp() - t1.timestamp()) / 60))
