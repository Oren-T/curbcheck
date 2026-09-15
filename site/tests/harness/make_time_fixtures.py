"""Write `site/tests/fixtures/time_cases.json` from the reference engine.

The browser has to reproduce `zoneinfo`'s gap and fold rules, not approximate
them (docs/STATIC_SITE.md, "Time"), so the cases here are the ones where a
naive epoch implementation and `datetime` disagree: both DST Sundays of 2026 and
2027, windows across each, a `t1` that arrives as `fold=1` through `astimezone`,
minute 1440, a 24-hour window at each transition, a rule at 02:00-03:00 on the
spring-forward day, and the midnight-wrapping and seasonal rules that decide
which side of a boundary a rule is in force on.

Run once and commit the JSON; `site/tests/time.test.js` replays it.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from curbcheck.config import NYC_TZ
from curbcheck.engine.resolve import _when
from curbcheck.engine.window import (
    CalendarContext,
    Interval,
    expand_window,
    meter_is_charged,
    rule_is_active,
)
from curbcheck.model import Action, Flags, Regulation, VehicleClass

OUT_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "time_cases.json"

# The four transitions the fixture is built around. 2026-03-08 and 2027-03-14
# lose 02:00-03:00; 2026-11-01 and 2027-11-07 repeat 01:00-02:00.
SPRING_2026 = date(2026, 3, 8)
FALL_2026 = date(2026, 11, 1)
SPRING_2027 = date(2027, 3, 14)
FALL_2027 = date(2027, 11, 7)

EMPTY_CALENDAR = CalendarContext()
HOLIDAY_CALENDAR = CalendarContext(
    asp_suspension_dates=frozenset({FALL_2026, date(2026, 11, 26)}),
    major_holiday_dates=frozenset({date(2026, 11, 26)}),
    meter_suspended_dates=frozenset({date(2026, 11, 26)}),
)


def local(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=NYC_TZ)


def utc(text: str) -> datetime:
    """An instant, which `expand_window` converts -- this is how a `fold=1` t1 arrives."""
    return datetime.fromisoformat(text).replace(tzinfo=UTC).astimezone(NYC_TZ)


def reg(**kwargs: Any) -> Regulation:
    defaults: dict[str, Any] = {"action": Action.PARK, "permitted": False}
    return Regulation(**{**defaults, **kwargs})


NIGHT = reg(action=Action.STAND, time_from="20:00", time_to="06:00")
DST_HOUR = reg(time_from="02:00", time_to="03:00")
MARKET = reg(
    permitted=True,
    vehicle_class=VehicleClass.TRUCK,
    exclusive=True,
    days=[2],
    time_from="08:00",
    time_to="16:00",
    effective_from="06-01",
    effective_to="11-30",
)
WINTER = reg(effective_from="11-15", effective_to="03-15")
SUNDAY_METER = reg(
    permitted=True, metered=True, time_from="09:00", time_to="19:00", max_duration_min=120
)
INCLUDING_SUNDAY = reg(
    permitted=True,
    metered=True,
    time_from="09:00",
    time_to="19:00",
    max_duration_min=60,
    flags=Flags(including_sunday=True),
)
CLEANING = reg(days=[0, 3], time_from="09:00", time_to="10:30", flags=Flags(street_cleaning=True))

CASES: list[dict[str, Any]] = [
    {
        "name": "spring forward 2026, the hour that does not exist",
        "t1": local("2026-03-08T01:00"),
        "t2": local("2026-03-08T04:00"),
        "regulations": [DST_HOUR],
    },
    {
        "name": "spring forward 2026, twenty-four wall-clock hours",
        "t1": local("2026-03-08T00:00"),
        "t2": local("2026-03-09T00:00"),
        "regulations": [DST_HOUR, NIGHT],
    },
    {
        "name": "fall back 2026, the hour that happens twice",
        "t1": local("2026-11-01T00:00"),
        "t2": local("2026-11-01T04:00"),
        "regulations": [NIGHT],
    },
    {
        "name": "fall back 2026, twenty-four wall-clock hours",
        "t1": local("2026-11-01T00:00"),
        "t2": local("2026-11-02T00:00"),
        "regulations": [NIGHT, CLEANING],
        "calendar": HOLIDAY_CALENDAR,
    },
    {
        "name": "fall back 2026, t1 is the second 01:30",
        "t1": utc("2026-11-01T06:30"),
        "t2": local("2026-11-01T03:00"),
        "regulations": [NIGHT],
    },
    {
        "name": "fall back 2026, t1 is the first 01:30",
        "t1": utc("2026-11-01T05:30"),
        "t2": local("2026-11-01T03:00"),
        "regulations": [NIGHT],
    },
    {
        "name": "spring forward 2027, the hour that does not exist",
        "t1": local("2027-03-14T01:00"),
        "t2": local("2027-03-14T04:00"),
        "regulations": [DST_HOUR],
    },
    {
        "name": "spring forward 2027, twenty-four wall-clock hours",
        "t1": local("2027-03-14T00:00"),
        "t2": local("2027-03-15T00:00"),
        "regulations": [DST_HOUR],
    },
    {
        "name": "fall back 2027, the hour that happens twice",
        "t1": local("2027-11-07T00:00"),
        "t2": local("2027-11-07T04:00"),
        "regulations": [NIGHT],
    },
    {
        "name": "fall back 2027, twenty-four wall-clock hours",
        "t1": local("2027-11-07T00:00"),
        "t2": local("2027-11-08T00:00"),
        "regulations": [NIGHT],
    },
    {
        "name": "minute 1440 across the fall-back midnight",
        "t1": local("2026-10-31T23:00"),
        "t2": local("2026-11-01T01:00"),
        "regulations": [NIGHT],
    },
    {
        "name": "minute 1440 across the spring-forward midnight",
        "t1": local("2026-03-07T23:00"),
        "t2": local("2026-03-08T03:30"),
        "regulations": [NIGHT, DST_HOUR],
    },
    {
        "name": "midnight-wrapping night regulation on an ordinary Monday",
        "t1": local("2026-09-14T19:00"),
        "t2": local("2026-09-15T07:00"),
        "regulations": [NIGHT],
    },
    {
        "name": "seasonal rules on either side of their bounds",
        "t1": local("2026-11-30T07:00"),
        "t2": local("2026-12-01T09:00"),
        "regulations": [MARKET, WINTER],
    },
    {
        "name": "seasonal rule wrapping the year end",
        "t1": local("2026-12-31T22:00"),
        "t2": local("2027-01-01T02:00"),
        "regulations": [WINTER],
    },
    {
        "name": "Sunday meters and the INCLUDING SUNDAY override",
        "t1": local("2026-09-20T08:00"),
        "t2": local("2026-09-20T20:00"),
        "regulations": [SUNDAY_METER, INCLUDING_SUNDAY],
    },
    {
        "name": "major legal holiday frees the meter",
        "t1": local("2026-11-26T08:00"),
        "t2": local("2026-11-26T20:00"),
        "regulations": [SUNDAY_METER, INCLUDING_SUNDAY, CLEANING],
        "calendar": HOLIDAY_CALENDAR,
    },
    {
        "name": "a window that needs a half-minute boundary",
        "t1": local("2026-09-14T09:59:30"),
        "t2": local("2026-09-14T10:30:30"),
        "regulations": [CLEANING],
    },
]


def calendar_to_json(calendar: CalendarContext) -> dict[str, Any]:
    return {
        "asp_suspension_dates": sorted(day.isoformat() for day in calendar.asp_suspension_dates),
        "major_holiday_dates": sorted(day.isoformat() for day in calendar.major_holiday_dates),
        "meter_suspended_dates": sorted(day.isoformat() for day in calendar.meter_suspended_dates),
        "non_school_dates": (
            None
            if calendar.non_school_dates is None
            else sorted(day.isoformat() for day in calendar.non_school_dates)
        ),
        "snow_emergency": calendar.snow_emergency,
        "calendar_missing": calendar.calendar_missing,
    }


def interval_to_json(
    interval: Interval, regulations: list[Regulation], calendar: CalendarContext
) -> dict[str, Any]:
    return {
        "start": interval.start.isoformat(),
        "start_fold": interval.start.fold,
        "end": interval.end.isoformat(),
        "end_fold": interval.end.fold,
        "minutes": interval.minutes,
        "weekday": interval.weekday,
        "date": interval.date.isoformat(),
        "start_minute_of_day": interval.start_minute_of_day,
        "when": _when(interval),
        "rule_active": [rule_is_active(reg, interval, calendar) for reg in regulations],
        "meter_charged": [meter_is_charged(reg, interval, calendar) for reg in regulations],
    }


def build_case(case: dict[str, Any]) -> dict[str, Any]:
    calendar = case.get("calendar", EMPTY_CALENDAR)
    regulations = case["regulations"]
    intervals = expand_window(case["t1"], case["t2"], regulations)
    return {
        "name": case["name"],
        "t1": case["t1"].isoformat(),
        "t2": case["t2"].isoformat(),
        "regulations": [reg.model_dump(mode="json") for reg in regulations],
        "calendar": calendar_to_json(calendar),
        "intervals": [interval_to_json(item, regulations, calendar) for item in intervals],
    }


def wall_clock_cases() -> list[dict[str, Any]]:
    """Instant <-> wall clock on its own, which `expand_window` only exercises indirectly."""
    moments: list[datetime] = []
    for transition in (SPRING_2026, FALL_2026, SPRING_2027, FALL_2027):
        midnight = datetime(transition.year, transition.month, transition.day, tzinfo=UTC)
        for hours in range(0, 30):
            moments.append((midnight + timedelta(hours=hours)).astimezone(NYC_TZ))
    return [
        {
            "epoch_ms": round(moment.timestamp() * 1000),
            "iso": moment.isoformat(),
            "fold": moment.fold,
            "weekday": moment.weekday(),
        }
        for moment in moments
    ]


def main() -> None:
    payload = {
        "wall_clocks": wall_clock_cases(),
        "windows": [build_case(case) for case in CASES],
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=1, sort_keys=False) + "\n", encoding="utf-8")
    print(f"{OUT_PATH}: {len(payload['windows'])} windows, {len(payload['wall_clocks'])} instants")


if __name__ == "__main__":
    main()
