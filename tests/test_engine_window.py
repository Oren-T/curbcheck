"""Window expansion and per-rule activity, including the calendar overrides in SPEC §9.3.

Dates are chosen for their weekday: 2026-09-14 is a Monday, 09-19 a Saturday,
09-20 a Sunday, and 2026-03-08 is the spring-forward Sunday.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from curbcheck.config import NYC_TZ
from curbcheck.engine.window import (
    CalendarContext,
    Interval,
    expand_window,
    meter_is_charged,
    rule_is_active,
)
from curbcheck.model import Action, Flags, Regulation, VehicleClass

EMPTY_CALENDAR = CalendarContext()


def moment(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=NYC_TZ)


def interval(start: str, end: str) -> Interval:
    return Interval(moment(start), moment(end))


def reg(**kwargs: object) -> Regulation:
    defaults: dict[str, object] = {"action": Action.PARK, "permitted": False}
    return Regulation(**{**defaults, **kwargs})  # type: ignore[arg-type]


def test_a_window_inside_one_day_with_no_rules_is_a_single_interval() -> None:
    intervals = expand_window(moment("2026-09-14T10:00"), moment("2026-09-14T12:00"))

    assert intervals == [interval("2026-09-14T10:00", "2026-09-14T12:00")]


def test_an_overnight_window_splits_at_midnight() -> None:
    intervals = expand_window(moment("2026-09-14T22:00"), moment("2026-09-15T02:00"))

    assert intervals == [
        interval("2026-09-14T22:00", "2026-09-15T00:00"),
        interval("2026-09-15T00:00", "2026-09-15T02:00"),
    ]


def test_the_window_splits_at_every_rule_boundary() -> None:
    cleaning = reg(days=[0], time_from="09:00", time_to="10:30")

    intervals = expand_window(moment("2026-09-14T08:00"), moment("2026-09-14T12:00"), [cleaning])

    assert intervals == [
        interval("2026-09-14T08:00", "2026-09-14T09:00"),
        interval("2026-09-14T09:00", "2026-09-14T10:30"),
        interval("2026-09-14T10:30", "2026-09-14T12:00"),
    ]


def test_rule_boundaries_outside_the_window_do_not_add_intervals() -> None:
    evening = reg(time_from="20:00", time_to="06:00")

    intervals = expand_window(moment("2026-09-14T09:00"), moment("2026-09-14T11:00"), [evening])

    assert len(intervals) == 1


def test_boundaries_apply_on_every_day_the_window_covers() -> None:
    cleaning = reg(days=[0, 3], time_from="09:00", time_to="10:30")

    intervals = expand_window(moment("2026-09-14T08:00"), moment("2026-09-16T08:00"), [cleaning])
    starts = [i.start.strftime("%m-%d %H:%M") for i in intervals]

    assert starts == [
        "09-14 08:00",
        "09-14 09:00",
        "09-14 10:30",
        "09-15 00:00",
        "09-15 09:00",
        "09-15 10:30",
        "09-16 00:00",
    ]


def test_expand_window_rejects_naive_datetimes() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        expand_window(datetime(2026, 9, 14, 10), moment("2026-09-14T12:00"))


def test_expand_window_rejects_a_window_that_does_not_move_forward() -> None:
    with pytest.raises(ValueError, match="end after it starts"):
        expand_window(moment("2026-09-14T12:00"), moment("2026-09-14T12:00"))


def test_a_spring_forward_window_loses_the_hour_that_does_not_exist() -> None:
    intervals = expand_window(moment("2026-03-08T01:00"), moment("2026-03-08T04:00"))

    assert sum(i.minutes for i in intervals) == 120


def test_interval_reports_its_weekday_and_date() -> None:
    saturday = interval("2026-09-19T08:00", "2026-09-19T19:00")

    assert saturday.weekday == 5
    assert saturday.date == date(2026, 9, 19)
    assert saturday.minutes == 660


def test_a_rule_is_inactive_on_a_day_it_does_not_list() -> None:
    """SPEC §8.4 ex. 2: NO PARKING (BROOM) MONDAY THURSDAY 9AM-10:30AM."""
    cleaning = reg(days=[0, 3], time_from="09:00", time_to="10:30")

    assert rule_is_active(
        cleaning, interval("2026-09-14T09:00", "2026-09-14T10:30"), EMPTY_CALENDAR
    )
    assert not rule_is_active(
        cleaning, interval("2026-09-15T09:00", "2026-09-15T10:30"), EMPTY_CALENDAR
    )


def test_a_rule_with_no_times_is_active_all_day() -> None:
    """SPEC §8.4 ex. 9: NO PARKING ANYTIME."""
    anytime = reg()

    assert rule_is_active(anytime, interval("2026-09-20T03:00", "2026-09-20T04:00"), EMPTY_CALENDAR)


def test_a_midnight_wrapping_range_is_active_on_both_sides_of_midnight() -> None:
    """SPEC §8.4 ex. 15: NIGHT REGULATION NO STANDING 8PM-6AM ALL DAYS."""
    night = reg(action=Action.STAND, time_from="20:00", time_to="06:00")

    assert rule_is_active(night, interval("2026-09-14T21:00", "2026-09-15T00:00"), EMPTY_CALENDAR)
    assert rule_is_active(night, interval("2026-09-15T00:00", "2026-09-15T05:00"), EMPTY_CALENDAR)
    assert not rule_is_active(
        night, interval("2026-09-15T07:00", "2026-09-15T08:00"), EMPTY_CALENDAR
    )


def test_a_range_ending_at_midnight_runs_to_the_end_of_the_day() -> None:
    evening = reg(time_from="19:00", time_to="00:00")

    assert rule_is_active(evening, interval("2026-09-14T23:00", "2026-09-15T00:00"), EMPTY_CALENDAR)
    assert not rule_is_active(
        evening, interval("2026-09-15T00:00", "2026-09-15T01:00"), EMPTY_CALENDAR
    )


def test_a_seasonal_rule_is_inactive_outside_its_dates() -> None:
    """SPEC §8.4 ex. 5: FARMERS MARKET JUNE 1 - NOV 30 WEDNESDAY 8AM-4PM."""
    market = reg(
        days=[2], time_from="08:00", time_to="16:00", effective_from="06-01", effective_to="11-30"
    )

    assert rule_is_active(market, interval("2026-09-16T09:00", "2026-09-16T10:00"), EMPTY_CALENDAR)
    assert not rule_is_active(
        market, interval("2026-12-16T09:00", "2026-12-16T10:00"), EMPTY_CALENDAR
    )


def test_seasonal_bounds_are_inclusive_on_both_ends() -> None:
    market = reg(effective_from="06-01", effective_to="11-30")

    assert rule_is_active(market, interval("2026-06-01T09:00", "2026-06-01T10:00"), EMPTY_CALENDAR)
    assert rule_is_active(market, interval("2026-11-30T09:00", "2026-11-30T10:00"), EMPTY_CALENDAR)
    assert not rule_is_active(
        market, interval("2026-05-31T09:00", "2026-05-31T10:00"), EMPTY_CALENDAR
    )


def test_a_season_may_wrap_the_year_end() -> None:
    winter = reg(effective_from="11-15", effective_to="03-15")

    assert rule_is_active(winter, interval("2026-12-25T09:00", "2026-12-25T10:00"), EMPTY_CALENDAR)
    assert rule_is_active(winter, interval("2026-01-05T09:00", "2026-01-05T10:00"), EMPTY_CALENDAR)
    assert not rule_is_active(
        winter, interval("2026-07-04T09:00", "2026-07-04T10:00"), EMPTY_CALENDAR
    )


def test_street_cleaning_is_suspended_on_an_asp_suspension_date() -> None:
    cleaning = reg(days=[0], time_from="09:00", time_to="10:30", flags=Flags(street_cleaning=True))
    calendar = CalendarContext(asp_suspension_dates=frozenset({date(2026, 9, 14)}))

    assert not rule_is_active(cleaning, interval("2026-09-14T09:00", "2026-09-14T10:00"), calendar)
    assert rule_is_active(
        cleaning, interval("2026-09-14T09:00", "2026-09-14T10:00"), EMPTY_CALENDAR
    )


def test_a_holiday_exempt_rule_is_inactive_on_a_major_legal_holiday() -> None:
    calendar = CalendarContext(major_holiday_dates=frozenset({date(2026, 12, 25)}))
    rule = reg(days=[4], flags=Flags(holiday_exempt=True))

    assert not rule_is_active(rule, interval("2026-12-25T09:00", "2026-12-25T10:00"), calendar)


def test_a_seven_day_prohibition_survives_a_holiday() -> None:
    """SPEC §9.3(a): NO STANDING ANYTIME and friends remain in force on holidays."""
    calendar = CalendarContext(major_holiday_dates=frozenset({date(2026, 12, 25)}))
    standing = reg(action=Action.STAND)

    assert rule_is_active(standing, interval("2026-12-25T09:00", "2026-12-25T10:00"), calendar)


def test_a_snow_emergency_rule_is_inactive_unless_an_emergency_is_declared() -> None:
    """SPEC §8.4 ex. 13: NO STOPPING (SNOW EMERGENCY) ANYTIME."""
    snow = reg(action=Action.STOP, flags=Flags(snow_emergency=True))
    declared = CalendarContext(snow_emergency=True)

    assert not rule_is_active(
        snow, interval("2026-01-05T09:00", "2026-01-05T10:00"), EMPTY_CALENDAR
    )
    assert rule_is_active(snow, interval("2026-01-05T09:00", "2026-01-05T10:00"), declared)


def test_a_school_day_rule_is_assumed_active_when_the_calendar_is_unknown() -> None:
    """SPEC §8.4 ex. 6: assuming school is out would risk a false legal verdict."""
    school = reg(days=[0], time_from="07:00", time_to="16:00", flags=Flags(school_days=True))

    assert rule_is_active(school, interval("2026-09-14T08:00", "2026-09-14T09:00"), EMPTY_CALENDAR)


def test_a_school_day_rule_is_inactive_on_a_known_non_school_day() -> None:
    school = reg(days=[0], time_from="07:00", time_to="16:00", flags=Flags(school_days=True))
    calendar = CalendarContext(non_school_dates=frozenset({date(2026, 9, 14)}))

    assert not rule_is_active(school, interval("2026-09-14T08:00", "2026-09-14T09:00"), calendar)


def test_an_except_sunday_rule_is_inactive_on_sunday() -> None:
    """SPEC §8.4 ex. 11: 2 HOUR PARKING 8AM-6PM EXCEPT SUNDAY."""
    rule = reg(
        permitted=True,
        days=[0, 1, 2, 3, 4, 5, 6],
        time_from="08:00",
        time_to="18:00",
        flags=Flags(except_sunday=True),
    )

    assert not rule_is_active(
        rule, interval("2026-09-20T09:00", "2026-09-20T10:00"), EMPTY_CALENDAR
    )


def test_meters_are_not_charged_on_sunday() -> None:
    meter = reg(permitted=True, metered=True, time_from="09:00", time_to="19:00")

    assert not meter_is_charged(
        meter, interval("2026-09-20T10:00", "2026-09-20T11:00"), EMPTY_CALENDAR
    )
    assert meter_is_charged(meter, interval("2026-09-19T10:00", "2026-09-19T11:00"), EMPTY_CALENDAR)


def test_including_sunday_meters_are_charged_on_sunday() -> None:
    """SPEC §8.4 ex. 10: 1 HOUR METERED PARKING 9AM-7PM INCLUDING SUNDAY."""
    meter = reg(
        permitted=True,
        metered=True,
        max_duration_min=60,
        time_from="09:00",
        time_to="19:00",
        flags=Flags(including_sunday=True),
    )

    assert meter_is_charged(meter, interval("2026-09-20T10:00", "2026-09-20T11:00"), EMPTY_CALENDAR)


def test_meters_are_not_charged_on_a_major_legal_holiday() -> None:
    meter = reg(permitted=True, metered=True)
    calendar = CalendarContext(
        major_holiday_dates=frozenset({date(2026, 12, 25)}),
        asp_suspension_dates=frozenset({date(2026, 12, 25)}),
    )

    assert not meter_is_charged(meter, interval("2026-12-25T10:00", "2026-12-25T11:00"), calendar)


def test_an_unmetered_rule_is_never_charged() -> None:
    free = reg(permitted=True, max_duration_min=120)

    assert not meter_is_charged(
        free, interval("2026-09-19T10:00", "2026-09-19T11:00"), EMPTY_CALENDAR
    )


def test_calendar_context_reads_asp_suspension_rows() -> None:
    calendar = CalendarContext.from_asp_rows(
        [
            {
                "date": "2026-12-25",
                "is_major_legal_holiday": 1,
                "meters_suspended": 1,
                "label": "Christmas Day",
            },
            {
                "date": "2026-11-26",
                "is_major_legal_holiday": 0,
                "meters_suspended": 0,
                "label": "Diwali",
            },
        ]
    )

    assert calendar.is_asp_suspended(date(2026, 11, 26))
    assert not calendar.is_major_holiday(date(2026, 11, 26))
    assert calendar.is_major_holiday(date(2026, 12, 25))
    assert not calendar.meters_in_effect(date(2026, 12, 25))
    assert calendar.meters_in_effect(date(2026, 11, 26))
    assert calendar.labels[date(2026, 12, 25)] == "Christmas Day"


def test_an_exclusive_class_rule_reads_as_prohibited_for_a_passenger_car() -> None:
    """SPEC §8.4 ex. 14: COMMERCIAL VEHICLES ONLY is a prohibition for us."""
    commercial = reg(
        permitted=True,
        vehicle_class=VehicleClass.COMMERCIAL,
        exclusive=True,
        days=[0, 1, 2, 3, 4],
        time_from="09:00",
        time_to="18:00",
    )

    assert commercial.applies_to_passenger() is False
