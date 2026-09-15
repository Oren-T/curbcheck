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

# SPEC §11: a missing calendar is a failure state the user has to see, because
# every holiday and street-cleaning verdict silently becomes a guess without it.
CALENDAR_MISSING_CAVEAT = (
    "Holiday and street-cleaning suspension calendar is missing; holiday and ASP verdicts "
    "may be wrong."
)

# The reason on a span that is legal only because nothing is posted. Written as
# a sentence, not as a phrase like the other reasons, because the UI leads the
# card with it rather than appending it to a verdict word (UX audit P0-1).
ABSENCE_REASON = "No posted rule is in effect during this window"

# `regulation_segment.gap_kind`: why a placeholder span carries no rules at all.
# Spelled out here rather than imported from `etl.segments`, which would pull
# the snapping and staging modules into the server process for two strings.
NO_SIGNS = "no_signs"
UNMATCHED_SIGNS = "unmatched_signs"

# What grey means on this stretch. The two gaps are different facts about the
# curb and must not read the same: `unmatched_signs` is a blockface DOT does
# publish signs for and we could not place, and 332 of those 499 sides carry a
# NO STANDING/PARKING/STOPPING ANYTIME panel (docs/VALIDATION.md §5), so
# "no sign data on this block" is false there rather than merely vague.
NO_DATA_REASON: dict[str, str] = {
    NO_SIGNS: "NYC DOT lists no signs on this stretch",
    UNMATCHED_SIGNS: "Signs exist here that CurbCheck could not place",
}
# A stack that is empty without a `gap_kind` to explain it: a snapshot built
# before the column, or a span whose rules did not load. Neither sentence above
# can be claimed about it, so it says only what we know.
UNKNOWN_GAP_REASON = "No sign data for this stretch"

# The same two facts in the caveat list, which is what a client that reads only
# `caveats` sees. Each carries the consequence the reason has no room for.
NO_DATA_CAVEAT: dict[str, str] = {
    NO_SIGNS: "NYC DOT lists no signs on this stretch; unknown is not the same as unrestricted.",
    UNMATCHED_SIGNS: (
        "DOT publishes signs for this blockface that CurbCheck could not place;"
        " read the posted signs."
    ),
}

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


class VerdictBasis(StrEnum):
    """Why a LEGAL verdict is legal: a rule that permits, or no rule at all.

    Absence really is a permission under 34 RCNY 4-08, but it is evidence of
    nothing having been read, and the two must not reach the user as one thing:
    every ranked result on the Upper East Side read "Legal - 100% confidence -
    no posted rule covers this window", which is the app announcing that it
    found nothing in its most confident voice (UX audit P0-1).
    """

    POSTED = "posted"
    ABSENCE = "absence"


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
    def basis(self) -> VerdictBasis | None:
        """Why this span is legal, or None when the verdict is not LEGAL.

        ABSENCE only when *no* posted rule was in force for any part of the
        window; one permitting rule anywhere in it makes the verdict POSTED,
        because there is then a sign the user can go and read.
        """
        if self.verdict is not Verdict.LEGAL:
            return None
        if self.intervals and all(outcome.by_absence for outcome in self.intervals):
            return VerdictBasis.ABSENCE
        return VerdictBasis.POSTED

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
    *,
    gap_kind: str | None = None,
) -> SegmentVerdict:
    """Verdict for one segment over [t1, t2): legal only if every sub-interval permits parking.

    `gap_kind` is the span's `regulation_segment.gap_kind`, which is the only
    thing that can say what an empty stack means. Without it the NO_DATA reason
    claims nothing about the curb beyond our own ignorance.
    """
    if not stack:
        return SegmentVerdict(
            verdict=Verdict.NO_DATA,
            reason=NO_DATA_REASON.get(gap_kind or "", UNKNOWN_GAP_REASON),
            caveats=_calendar_caveats(calendar) + _gap_caveats(gap_kind),
            window_minutes=_window_minutes(t1, t2),
            confidence=0.0,
        )

    intervals = expand_window(t1, t2, [item.regulation for item in stack])
    outcomes = [resolve_interval(stack, interval, calendar) for interval in intervals]
    window_minutes = sum(outcome.interval.minutes for outcome in outcomes)
    charged_minutes = sum(o.interval.minutes for o in outcomes if o.meter_charged)
    limits = [o.max_duration_min for o in outcomes if o.max_duration_min is not None]
    max_duration_min = min(limits) if limits else None
    # Only the minutes a posted limit is actually in force count against it. A
    # "2 HMP 8AM-7PM" sign says nothing about 7PM onwards, so a 18:00-21:00 stay
    # spends 60 of its 180 minutes under the limit. Summing them all rather than
    # taking the longest run keeps two separated limited stretches from each
    # getting a full allowance. D12(b) is unchanged: a rule in force on a day
    # its meter is not running still counts, because the rule is still in force.
    limited_minutes = sum(o.interval.minutes for o in outcomes if o.max_duration_min is not None)
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

    if max_duration_min is not None and max_duration_min < limited_minutes:
        too_short = next(o for o in outcomes if o.max_duration_min == max_duration_min)
        return decided(
            Verdict.ILLEGAL,
            f"posted limit of {max_duration_min} min is shorter than"
            f" the {limited_minutes} min it is in force for",
            too_short,
        )

    if all(outcome.by_absence for outcome in outcomes):
        return decided(Verdict.LEGAL, ABSENCE_REASON)
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
    caveats = _calendar_caveats(calendar)
    regulations = [item.regulation for item in stack]
    dates = sorted({outcome.interval.date for outcome in outcomes})

    if any(reg.flags.snow_emergency for reg in regulations):
        caveats.append(
            "Snow emergency rule present; in force only when the city declares one."
            if not calendar.snow_emergency
            else "Snow emergency declared; snow rules are in force."
        )
    # Only when a school-day rule actually overlaps the window: a 7AM-4PM school
    # rule says nothing about an evening search, so the caveat would mislead.
    school_rules = [reg for reg in regulations if reg.flags.school_days]
    if any(
        rule_is_active(reg, outcome.interval, calendar)
        for reg in school_rules
        for outcome in outcomes
    ) and any(not calendar.is_known_non_school_day(day) for day in dates):
        caveats.append("School-day rule assumed active.")
    if any(reg.flags.temporary for reg in regulations):
        caveats.append("A sign here marks itself temporary.")
    if any(reg.flags.street_cleaning for reg in regulations) and any(
        calendar.is_asp_suspended(day) for day in dates
    ):
        caveats.append("Street cleaning is suspended on this date.")
    if any(outcome.metered and not outcome.meter_charged for outcome in outcomes):
        caveats.append("Meters are not in effect for part of this window.")
    if all(outcome.by_absence for outcome in outcomes):
        caveats.append("No posted rule is in effect during this window; read the curb.")
    elif any(outcome.by_absence for outcome in outcomes):
        caveats.append("Part of this window has no posted rule in effect; read the curb.")
    return caveats


def _calendar_caveats(calendar: CalendarContext) -> list[str]:
    """The caveats that are true of every span in the database, not of this one."""
    return [CALENDAR_MISSING_CAVEAT] if calendar.calendar_missing else []


def _gap_caveats(gap_kind: str | None) -> list[str]:
    """What the caveat list says about a stretch with no rules on it."""
    caveat = NO_DATA_CAVEAT.get(gap_kind or "")
    return [caveat] if caveat is not None else []


def _prohibition_reason(reg: Regulation) -> str:
    if reg.permitted and reg.exclusive:
        return f"reserved for {reg.vehicle_class.value} vehicles"
    return _PROHIBITION_PHRASE[reg.action]


def _when(interval: Interval) -> str:
    return f"{interval.start:%a %H:%M}-{interval.end:%H:%M}"


def _window_minutes(t1: datetime, t2: datetime) -> int:
    return max(0, round((t2.timestamp() - t1.timestamp()) / 60))
