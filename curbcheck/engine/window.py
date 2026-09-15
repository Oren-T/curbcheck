"""Split a requested parking window into sub-intervals and decide which rules are in force.

A rule's truth value only changes at midnight, at one of its own time
boundaries, or at a calendar boundary (which is a date, hence a midnight). So
we cut [T1, T2] at every distinct boundary the stack under evaluation can
produce and evaluate each piece once. SPEC §9.2 and §9.3.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from itertools import pairwise
from typing import Any

from curbcheck.config import NYC_TZ
from curbcheck.model import Regulation

MINUTES_PER_DAY = 24 * 60


@dataclass(frozen=True)
class Interval:
    """A half-open [start, end) slice of the requested window, never crossing midnight."""

    start: datetime
    end: datetime

    @property
    def weekday(self) -> int:
        """Monday is 0, matching model.Weekday."""
        return self.start.weekday()

    @property
    def date(self) -> date:
        return self.start.date()

    @property
    def minutes(self) -> int:
        """Elapsed real minutes. Subtracting two datetimes in one zone would count wall-clock
        minutes, so the hour that does not happen on the spring-forward Sunday would be billed."""
        return round((self.end.timestamp() - self.start.timestamp()) / 60)

    @property
    def start_minute_of_day(self) -> int:
        return self.start.hour * 60 + self.start.minute


@dataclass(frozen=True)
class CalendarContext:
    """The dates that change what a sign means, as known offline.

    Built from the `asp_suspension` table plus whatever the caller knows about
    school closures. Emergency suspensions are unknowable offline (SPEC §11), so
    an empty context means "nothing special known", never "nothing is happening".
    """

    asp_suspension_dates: frozenset[date] = frozenset()
    major_holiday_dates: frozenset[date] = frozenset()
    meter_suspended_dates: frozenset[date] = frozenset()
    # None means "we have no school calendar"; a school-days rule is then assumed
    # active, because assuming it off would risk a false "legal" (SPEC §11).
    non_school_dates: frozenset[date] | None = None
    snow_emergency: bool = False
    labels: Mapping[date, str] = field(default_factory=dict)

    @classmethod
    def from_asp_rows(
        cls,
        rows: Iterable[Mapping[str, Any]],
        *,
        non_school_dates: Iterable[date] | None = None,
        snow_emergency: bool = False,
    ) -> CalendarContext:
        """Build from `asp_suspension` rows (`date`, `is_major_legal_holiday`, `meters_suspended`, `label`)."""
        suspensions: set[date] = set()
        holidays: set[date] = set()
        meters_off: set[date] = set()
        labels: dict[date, str] = {}
        for row in rows:
            day = date.fromisoformat(str(row["date"]))
            suspensions.add(day)
            if bool(row["is_major_legal_holiday"]):
                holidays.add(day)
            if bool(row["meters_suspended"]):
                meters_off.add(day)
            label = str(row.get("label") or "")
            if label:
                labels[day] = label
        return cls(
            asp_suspension_dates=frozenset(suspensions),
            major_holiday_dates=frozenset(holidays),
            meter_suspended_dates=frozenset(meters_off),
            non_school_dates=None if non_school_dates is None else frozenset(non_school_dates),
            snow_emergency=snow_emergency,
            labels=labels,
        )

    def is_asp_suspended(self, day: date) -> bool:
        return day in self.asp_suspension_dates

    def is_major_holiday(self, day: date) -> bool:
        return day in self.major_holiday_dates

    def is_known_non_school_day(self, day: date) -> bool:
        return self.non_school_dates is not None and day in self.non_school_dates

    def meters_in_effect(self, day: date) -> bool:
        """Meters run every day except Sundays and the six Major Legal Holidays (SPEC §13.2)."""
        if day.weekday() == 6:
            return False
        if day in self.meter_suspended_dates:
            return False
        return not self.is_major_holiday(day)


def expand_window(
    t1: datetime, t2: datetime, regulations: Iterable[Regulation] = ()
) -> list[Interval]:
    """Cut [t1, t2) at midnights and at every rule boundary in `regulations`.

    Both ends must be timezone-aware; parking rules are stated in local time, so
    they are converted to America/New_York first. A window of zero or negative
    length raises.
    """
    start = _as_nyc(t1, "t1")
    end = _as_nyc(t2, "t2")
    if end <= start:
        raise ValueError("the parking window must end after it starts")

    rule_minutes = _rule_boundary_minutes(regulations)
    boundaries = {start, end}
    for day in _dates_covered(start, end):
        for minute in rule_minutes:
            boundaries.add(_at_minute(day, minute))
        boundaries.add(_at_minute(day, MINUTES_PER_DAY))

    ordered = sorted(moment for moment in boundaries if start <= moment <= end)
    return [Interval(left, right) for left, right in pairwise(ordered) if right > left]


def rule_is_active(reg: Regulation, interval: Interval, calendar: CalendarContext) -> bool:
    """Whether the rule is in force for the whole of `interval`.

    Assumes `interval` came from `expand_window` with this rule in the stack, so
    the rule cannot switch on or off part-way through it.
    """
    if interval.weekday not in reg.days:
        return False
    if reg.flags.except_sunday and interval.weekday == 6:
        return False
    if not _in_season(reg, interval.date):
        return False
    if not _in_time_range(reg, interval):
        return False
    return _calendar_allows(reg, interval.date, calendar)


def meter_is_charged(reg: Regulation, interval: Interval, calendar: CalendarContext) -> bool:
    """Whether a metered rule actually costs money in this interval.

    Sundays and Major Legal Holidays are free (SPEC §9.3), except where the sign
    says INCLUDING SUNDAY (§8.4 ex. 10). We read that flag as overriding the
    holiday exemption too: the sign is claiming its own calendar.
    """
    if not reg.metered:
        return False
    if reg.flags.including_sunday:
        return True
    return calendar.meters_in_effect(interval.date)


def _calendar_allows(reg: Regulation, day: date, calendar: CalendarContext) -> bool:
    if reg.flags.street_cleaning and calendar.is_asp_suspended(day):
        return False
    if reg.flags.holiday_exempt and calendar.is_major_holiday(day):
        return False
    if reg.flags.snow_emergency and not calendar.snow_emergency:
        return False
    return not (reg.flags.school_days and calendar.is_known_non_school_day(day))


def _rule_boundary_minutes(regulations: Iterable[Regulation]) -> set[int]:
    minutes = {0}
    for reg in regulations:
        for value in (reg.time_from, reg.time_to):
            if value is not None:
                minutes.add(hhmm_to_minutes(value))
    return minutes


def _dates_covered(start: datetime, end: datetime) -> list[date]:
    days = []
    day = start.date()
    while day <= end.date():
        days.append(day)
        day += timedelta(days=1)
    return days


def _at_minute(day: date, minute_of_day: int) -> datetime:
    """Local wall-clock time on `day`. Minute 1440 is midnight at the start of the next day."""
    midnight = datetime(day.year, day.month, day.day, tzinfo=NYC_TZ)
    return midnight + timedelta(minutes=minute_of_day)


def _as_nyc(moment: datetime, name: str) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware; parking rules are local time")
    return moment.astimezone(NYC_TZ)


def hhmm_to_minutes(value: str) -> int:
    return int(value[:2]) * 60 + int(value[3:])


def _in_time_range(reg: Regulation, interval: Interval) -> bool:
    if reg.time_from is None or reg.time_to is None:
        return True
    start = hhmm_to_minutes(reg.time_from)
    stop = hhmm_to_minutes(reg.time_to)
    minute = interval.start_minute_of_day
    if stop > start:
        return start <= minute < stop
    # time_to <= time_from wraps past midnight ("8PM-6AM", SPEC §8.4 ex. 15).
    # The equal case is how an all-day range written as 12AM-12AM arrives.
    return minute >= start or minute < stop


def _in_season(reg: Regulation, day: date) -> bool:
    """Seasonal MM-DD bounds are inclusive on both ends and may wrap the year end."""
    if reg.effective_from is None or reg.effective_to is None:
        return True
    today = (day.month, day.day)
    season_start = _mmdd(reg.effective_from)
    season_end = _mmdd(reg.effective_to)
    if season_start <= season_end:
        return season_start <= today <= season_end
    return today >= season_start or today <= season_end


def _mmdd(value: str) -> tuple[int, int]:
    return (int(value[:2]), int(value[3:]))
