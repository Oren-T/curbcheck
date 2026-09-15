"""Most-restrictive-wins resolution over the SPEC §8.4 worked examples.

Each test builds the rule stack a correct parse of the named sign would produce
and asserts the verdict a driver would get. 2026-09-14 is a Monday, 09-16 a
Wednesday, 09-17 a Thursday, 09-19 a Saturday, 09-20 a Sunday.
"""

from __future__ import annotations

from datetime import date, datetime

from curbcheck.config import NYC_TZ
from curbcheck.engine.resolve import (
    AMBIGUITY_THRESHOLD,
    RegulationWithMeta,
    Verdict,
    evaluate_segment,
    resolve_interval,
)
from curbcheck.engine.window import CalendarContext, Interval
from curbcheck.model import Action, Flags, ParseMethod, Regulation, VehicleClass

EMPTY_CALENDAR = CalendarContext()


def moment(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=NYC_TZ)


def stacked(
    *regulations: Regulation,
    method: ParseMethod = ParseMethod.GRAMMAR,
    confidence: float = 0.98,
    raw: str = "TEST SIGN",
) -> list[RegulationWithMeta]:
    return [
        RegulationWithMeta(
            regulation=reg,
            parse_method=method,
            parse_confidence=confidence,
            reg_seg_id="seg-1",
            raw_sign_description=raw,
        )
        for reg in regulations
    ]


def no_parking_anytime() -> Regulation:
    """SPEC §8.4 ex. 9."""
    return Regulation(action=Action.PARK, permitted=False)


def no_standing_anytime() -> Regulation:
    """SPEC §8.4 ex. 8."""
    return Regulation(action=Action.STAND, permitted=False)


def street_cleaning_mon_thu() -> Regulation:
    """SPEC §8.4 ex. 2: NO PARKING (BROOM) MONDAY THURSDAY 9AM-10:30AM."""
    return Regulation(
        action=Action.PARK,
        permitted=False,
        days=[0, 3],
        time_from="09:00",
        time_to="10:30",
        flags=Flags(street_cleaning=True),
    )


def two_hour_meter_saturday() -> Regulation:
    """SPEC §8.4 ex. 1: 2 HMP SATURDAY 8AM-7PM."""
    return Regulation(
        action=Action.PARK,
        permitted=True,
        days=[5],
        time_from="08:00",
        time_to="19:00",
        metered=True,
        max_duration_min=120,
    )


def one_hour_meter_including_sunday() -> Regulation:
    """SPEC §8.4 ex. 10: 1 HOUR METERED PARKING 9AM-7PM INCLUDING SUNDAY."""
    return Regulation(
        action=Action.PARK,
        permitted=True,
        time_from="09:00",
        time_to="19:00",
        metered=True,
        max_duration_min=60,
        flags=Flags(including_sunday=True),
    )


def test_no_parking_anytime_is_illegal() -> None:
    verdict = evaluate_segment(
        stacked(no_parking_anytime()),
        moment("2026-09-14T10:00"),
        moment("2026-09-14T12:00"),
        EMPTY_CALENDAR,
    )

    assert verdict.verdict is Verdict.ILLEGAL
    assert "no parking" in verdict.reason


def test_no_standing_anytime_also_forbids_parking() -> None:
    verdict = evaluate_segment(
        stacked(no_standing_anytime()),
        moment("2026-09-14T10:00"),
        moment("2026-09-14T12:00"),
        EMPTY_CALENDAR,
    )

    assert verdict.verdict is Verdict.ILLEGAL
    assert "no standing" in verdict.reason


def test_the_most_restrictive_prohibition_is_the_one_reported() -> None:
    """Restrictiveness order, SPEC §9.2: no stopping > no standing > no parking."""
    stack = stacked(
        no_parking_anytime(),
        no_standing_anytime(),
        Regulation(action=Action.STOP, permitted=False),
    )

    outcome = resolve_interval(
        stack, Interval(moment("2026-09-14T10:00"), moment("2026-09-14T11:00")), EMPTY_CALENDAR
    )

    assert outcome.prohibiting_action is Action.STOP
    assert "no stopping" in outcome.reason


def test_stand_outranks_park_when_both_apply() -> None:
    stack = stacked(no_parking_anytime(), no_standing_anytime())

    outcome = resolve_interval(
        stack, Interval(moment("2026-09-14T10:00"), moment("2026-09-14T11:00")), EMPTY_CALENDAR
    )

    assert outcome.prohibiting_action is Action.STAND


def test_a_saturday_meter_window_inside_the_posted_hours_is_legal() -> None:
    verdict = evaluate_segment(
        stacked(two_hour_meter_saturday()),
        moment("2026-09-19T10:00"),
        moment("2026-09-19T11:30"),
        EMPTY_CALENDAR,
    )

    assert verdict.verdict is Verdict.LEGAL
    assert verdict.metered
    assert verdict.charged_minutes == 90
    assert verdict.max_duration_min == 120


def test_a_one_hour_meter_fails_a_three_hour_window() -> None:
    """SPEC §9.2: max_duration_min must cover the whole window."""
    verdict = evaluate_segment(
        stacked(one_hour_meter_including_sunday()),
        moment("2026-09-14T10:00"),
        moment("2026-09-14T13:00"),
        EMPTY_CALENDAR,
    )

    assert verdict.verdict is Verdict.ILLEGAL
    assert "60 min" in verdict.reason
    assert verdict.first_offending is not None


def test_a_posted_limit_is_measured_over_the_hours_it_is_in_force_for() -> None:
    """2 HMP 8AM-7PM, parked Sat 18:00-21:00: 60 of the 180 minutes are limited.

    The sign says nothing about 7PM onwards, so the curb is unrestricted then
    (34 RCNY 4-08) and the two-hour limit is not spent by sitting there. Testing
    the limit against the whole window read this as ILLEGAL and cost the user a
    legal spot. docs/DECISIONS.md D12(b) is unaffected: the limit still counts
    on the hours the rule is in force, meter running or not.
    """
    verdict = evaluate_segment(
        stacked(two_hour_meter_saturday()),
        moment("2026-09-19T18:00"),
        moment("2026-09-19T21:00"),
        EMPTY_CALENDAR,
    )

    assert verdict.verdict is Verdict.LEGAL
    assert verdict.window_minutes == 180
    assert verdict.charged_minutes == 60


def test_a_posted_limit_shorter_than_its_own_hours_is_still_illegal() -> None:
    verdict = evaluate_segment(
        stacked(two_hour_meter_saturday()),
        moment("2026-09-19T15:00"),
        moment("2026-09-19T21:00"),
        EMPTY_CALENDAR,
    )

    assert verdict.verdict is Verdict.ILLEGAL
    assert "120 min" in verdict.reason
    assert "240 min it is in force for" in verdict.reason


def test_a_limit_that_lapses_and_resumes_counts_both_stretches_against_itself() -> None:
    """Two separated limited stretches do not each get a fresh allowance.

    Summing the limited minutes rather than taking the longest run is the
    conservative reading: SPEC §8.6 makes a false "legal" the P0 defect.
    """
    morning = Regulation(
        action=Action.PARK,
        permitted=True,
        time_from="08:00",
        time_to="10:00",
        max_duration_min=120,
    )
    afternoon = morning.model_copy(update={"time_from": "14:00", "time_to": "16:00"})

    verdict = evaluate_segment(
        stacked(morning, afternoon),
        moment("2026-09-19T08:00"),
        moment("2026-09-19T16:00"),
        EMPTY_CALENDAR,
    )

    assert verdict.verdict is Verdict.ILLEGAL
    assert "240 min it is in force for" in verdict.reason


def test_a_window_that_runs_past_the_posted_hours_is_legal_by_absence_after_them() -> None:
    verdict = evaluate_segment(
        stacked(two_hour_meter_saturday()),
        moment("2026-09-19T18:30"),
        moment("2026-09-19T20:00"),
        EMPTY_CALENDAR,
    )

    assert verdict.verdict is Verdict.LEGAL
    assert verdict.charged_minutes == 30
    assert any(outcome.by_absence for outcome in verdict.intervals)
    assert "no posted rule" in " ".join(verdict.caveats)


def test_a_prohibition_that_covers_part_of_the_window_disqualifies_the_segment() -> None:
    verdict = evaluate_segment(
        stacked(street_cleaning_mon_thu()),
        moment("2026-09-14T08:00"),
        moment("2026-09-14T12:00"),
        EMPTY_CALENDAR,
    )

    assert verdict.verdict is Verdict.ILLEGAL
    assert verdict.first_offending is not None
    assert verdict.first_offending.interval.start == moment("2026-09-14T09:00")


def test_street_cleaning_on_a_suspension_date_leaves_the_block_parkable() -> None:
    """SPEC §9.3(c): ASP regs are suspended on calendar suspension dates."""
    calendar = CalendarContext(asp_suspension_dates=frozenset({date(2026, 9, 14)}))

    verdict = evaluate_segment(
        stacked(street_cleaning_mon_thu()),
        moment("2026-09-14T09:00"),
        moment("2026-09-14T10:00"),
        calendar,
    )

    assert verdict.verdict is Verdict.LEGAL
    assert "street cleaning is suspended on this date" in verdict.caveats


def test_a_midnight_wrapping_prohibition_catches_the_early_morning_side() -> None:
    """SPEC §8.4 ex. 3: NO PARKING (BROOM) MONDAY THURSDAY MIDNIGHT-3AM."""
    overnight = Regulation(
        action=Action.PARK,
        permitted=False,
        days=[0, 3],
        time_from="00:00",
        time_to="03:00",
        flags=Flags(street_cleaning=True),
    )

    verdict = evaluate_segment(
        stacked(overnight),
        moment("2026-09-13T23:00"),
        moment("2026-09-14T01:00"),
        EMPTY_CALENDAR,
    )

    assert verdict.verdict is Verdict.ILLEGAL
    assert verdict.first_offending is not None
    assert verdict.first_offending.interval.start == moment("2026-09-14T00:00")


def test_a_night_regulation_is_legal_after_it_lifts() -> None:
    """SPEC §8.4 ex. 15: NIGHT REGULATION NO STANDING 8PM-6AM ALL DAYS."""
    night = Regulation(action=Action.STAND, permitted=False, time_from="20:00", time_to="06:00")

    illegal = evaluate_segment(
        stacked(night), moment("2026-09-14T21:00"), moment("2026-09-15T01:00"), EMPTY_CALENDAR
    )
    legal = evaluate_segment(
        stacked(night), moment("2026-09-15T07:00"), moment("2026-09-15T09:00"), EMPTY_CALENDAR
    )

    assert illegal.verdict is Verdict.ILLEGAL
    assert legal.verdict is Verdict.LEGAL


def test_a_seasonal_reservation_only_bites_in_season() -> None:
    """SPEC §8.4 ex. 5: TRUCK FARMERS MARKET ONLY JUNE 1 - NOV 30 WEDNESDAY 8AM-4PM."""
    market = Regulation(
        action=Action.PARK,
        permitted=True,
        vehicle_class=VehicleClass.TRUCK,
        exclusive=True,
        days=[2],
        time_from="08:00",
        time_to="16:00",
        effective_from="06-01",
        effective_to="11-30",
    )

    in_season = evaluate_segment(
        stacked(market), moment("2026-09-16T09:00"), moment("2026-09-16T11:00"), EMPTY_CALENDAR
    )
    out_of_season = evaluate_segment(
        stacked(market), moment("2026-12-16T09:00"), moment("2026-12-16T11:00"), EMPTY_CALENDAR
    )

    assert in_season.verdict is Verdict.ILLEGAL
    assert "reserved for truck" in in_season.reason
    assert out_of_season.verdict is Verdict.LEGAL


def test_commercial_vehicles_only_is_a_prohibition_for_a_passenger_car() -> None:
    """SPEC §8.4 ex. 14: 3 HOUR PARKING 9AM-6PM MON-FRI COMMERCIAL VEHICLES ONLY."""
    commercial = Regulation(
        action=Action.PARK,
        permitted=True,
        vehicle_class=VehicleClass.COMMERCIAL,
        exclusive=True,
        days=[0, 1, 2, 3, 4],
        time_from="09:00",
        time_to="18:00",
        max_duration_min=180,
    )

    verdict = evaluate_segment(
        stacked(commercial), moment("2026-09-14T10:00"), moment("2026-09-14T11:00"), EMPTY_CALENDAR
    )

    assert verdict.verdict is Verdict.ILLEGAL


def test_an_authorized_vehicles_only_school_sign_prohibits_us_when_school_may_be_in() -> None:
    """SPEC §8.4 ex. 6: AVO DEPT OF EDUCATION SCHOOL DAYS 7AM-4PM."""
    avo = Regulation(
        action=Action.PARK,
        permitted=True,
        vehicle_class=VehicleClass.AUTHORIZED,
        exclusive=True,
        days=[0, 1, 2, 3, 4],
        time_from="07:00",
        time_to="16:00",
        flags=Flags(school_days=True),
    )

    verdict = evaluate_segment(
        stacked(avo), moment("2026-09-14T08:00"), moment("2026-09-14T09:00"), EMPTY_CALENDAR
    )

    assert verdict.verdict is Verdict.ILLEGAL
    assert "school-day rule assumed active" in verdict.caveats


def test_a_school_day_rule_lifts_on_a_known_non_school_day() -> None:
    avo = Regulation(
        action=Action.PARK,
        permitted=False,
        days=[0, 1, 2, 3, 4],
        time_from="07:00",
        time_to="16:00",
        flags=Flags(school_days=True),
    )
    calendar = CalendarContext(non_school_dates=frozenset({date(2026, 9, 14)}))

    verdict = evaluate_segment(
        stacked(avo), moment("2026-09-14T08:00"), moment("2026-09-14T09:00"), calendar
    )

    assert verdict.verdict is Verdict.LEGAL
    assert "school-day rule assumed active" not in verdict.caveats


def test_a_snow_emergency_rule_only_bites_when_one_is_declared() -> None:
    """SPEC §8.4 ex. 13: NO STOPPING (SNOW EMERGENCY) ANYTIME."""
    snow = Regulation(action=Action.STOP, permitted=False, flags=Flags(snow_emergency=True))

    quiet = evaluate_segment(
        stacked(snow), moment("2026-01-05T08:00"), moment("2026-01-05T09:00"), EMPTY_CALENDAR
    )
    declared = evaluate_segment(
        stacked(snow),
        moment("2026-01-05T08:00"),
        moment("2026-01-05T09:00"),
        CalendarContext(snow_emergency=True),
    )

    assert quiet.verdict is Verdict.LEGAL
    assert any("snow emergency" in caveat for caveat in quiet.caveats)
    assert declared.verdict is Verdict.ILLEGAL


def test_a_sunday_meter_permits_parking_without_charging() -> None:
    """SPEC §9.3(b): meters are not in effect on Sundays."""
    sunday_meter = Regulation(
        action=Action.PARK,
        permitted=True,
        time_from="09:00",
        time_to="19:00",
        metered=True,
        max_duration_min=120,
    )

    verdict = evaluate_segment(
        stacked(sunday_meter),
        moment("2026-09-20T10:00"),
        moment("2026-09-20T11:00"),
        EMPTY_CALENDAR,
    )

    assert verdict.verdict is Verdict.LEGAL
    assert verdict.metered
    assert verdict.charged_minutes == 0
    assert "meters are not in effect for part of this window" in verdict.caveats


def test_an_including_sunday_meter_charges_on_sunday() -> None:
    verdict = evaluate_segment(
        stacked(one_hour_meter_including_sunday()),
        moment("2026-09-20T10:00"),
        moment("2026-09-20T11:00"),
        EMPTY_CALENDAR,
    )

    assert verdict.verdict is Verdict.LEGAL
    assert verdict.charged_minutes == 60
    assert verdict.charged_intervals[0].interval.start == moment("2026-09-20T10:00")


def test_a_major_legal_holiday_frees_the_meter_but_keeps_the_seven_day_rules() -> None:
    calendar = CalendarContext(
        asp_suspension_dates=frozenset({date(2026, 12, 25)}),
        major_holiday_dates=frozenset({date(2026, 12, 25)}),
        meter_suspended_dates=frozenset({date(2026, 12, 25)}),
    )
    meter = Regulation(action=Action.PARK, permitted=True, metered=True, max_duration_min=120)

    metered_verdict = evaluate_segment(
        stacked(meter), moment("2026-12-25T10:00"), moment("2026-12-25T11:00"), calendar
    )
    standing_verdict = evaluate_segment(
        stacked(no_standing_anytime()),
        moment("2026-12-25T10:00"),
        moment("2026-12-25T11:00"),
        calendar,
    )

    assert metered_verdict.verdict is Verdict.LEGAL
    assert metered_verdict.charged_minutes == 0
    assert standing_verdict.verdict is Verdict.ILLEGAL


def test_a_free_time_limited_sign_outside_its_hours_is_permitted_by_absence() -> None:
    """SPEC §8.4 ex. 11: 2 HOUR PARKING 8AM-6PM EXCEPT SUNDAY, asked about a Sunday."""
    two_hour = Regulation(
        action=Action.PARK,
        permitted=True,
        days=[0, 1, 2, 3, 4, 5],
        time_from="08:00",
        time_to="18:00",
        max_duration_min=120,
        flags=Flags(except_sunday=True),
    )

    verdict = evaluate_segment(
        stacked(two_hour), moment("2026-09-20T09:00"), moment("2026-09-20T15:00"), EMPTY_CALENDAR
    )

    assert verdict.verdict is Verdict.LEGAL
    assert all(outcome.by_absence for outcome in verdict.intervals)
    assert verdict.max_duration_min is None


def test_a_prohibition_beats_a_permission_in_the_same_interval() -> None:
    stack = stacked(two_hour_meter_saturday(), no_standing_anytime())

    verdict = evaluate_segment(
        stack, moment("2026-09-19T10:00"), moment("2026-09-19T11:00"), EMPTY_CALENDAR
    )

    assert verdict.verdict is Verdict.ILLEGAL


def test_the_shortest_posted_limit_governs_a_stack_of_permissions() -> None:
    stack = stacked(two_hour_meter_saturday(), one_hour_meter_including_sunday())

    verdict = evaluate_segment(
        stack, moment("2026-09-19T10:00"), moment("2026-09-19T10:45"), EMPTY_CALENDAR
    )

    assert verdict.verdict is Verdict.LEGAL
    assert verdict.max_duration_min == 60


def test_an_unparsed_neighbour_makes_the_whole_segment_ambiguous() -> None:
    """SPEC §11: one unreadable sign poisons the block; never show a confident legal."""
    readable = stacked(two_hour_meter_saturday())
    unreadable = stacked(
        no_parking_anytime(),
        method=ParseMethod.UNPARSED,
        confidence=0.0,
        raw="NO PARKING (SOMETHING WE COULD NOT READ)",
    )

    verdict = evaluate_segment(
        readable + unreadable,
        moment("2026-09-19T10:00"),
        moment("2026-09-19T11:00"),
        EMPTY_CALENDAR,
    )

    assert verdict.verdict is Verdict.AMBIGUOUS
    assert "could not be read" in verdict.reason


def test_a_meta_sign_makes_the_segment_ambiguous() -> None:
    """SPEC §8.4 ex. 7: METERS ARE NOT IN EFFECT ABOVE TIMES modifies a sibling rule."""
    meta = Regulation(action=Action.PARK, permitted=True, flags=Flags(meta=True))

    verdict = evaluate_segment(
        stacked(two_hour_meter_saturday()) + stacked(meta),
        moment("2026-09-19T10:00"),
        moment("2026-09-19T11:00"),
        EMPTY_CALENDAR,
    )

    assert verdict.verdict is Verdict.AMBIGUOUS
    assert "modifies another sign" in verdict.reason


def test_a_low_confidence_parse_makes_the_segment_ambiguous() -> None:
    verdict = evaluate_segment(
        stacked(two_hour_meter_saturday(), confidence=AMBIGUITY_THRESHOLD - 0.01),
        moment("2026-09-19T10:00"),
        moment("2026-09-19T11:00"),
        EMPTY_CALENDAR,
    )

    assert verdict.verdict is Verdict.AMBIGUOUS
    assert verdict.confidence < AMBIGUITY_THRESHOLD


def test_a_parse_exactly_on_the_threshold_is_trusted() -> None:
    verdict = evaluate_segment(
        stacked(two_hour_meter_saturday(), confidence=AMBIGUITY_THRESHOLD),
        moment("2026-09-19T10:00"),
        moment("2026-09-19T11:00"),
        EMPTY_CALENDAR,
    )

    assert verdict.verdict is Verdict.LEGAL


def test_ambiguity_still_reports_the_intervals_and_charged_minutes() -> None:
    """The cost module prices ambiguous segments too; the UI just colours them amber."""
    verdict = evaluate_segment(
        stacked(two_hour_meter_saturday(), confidence=0.5),
        moment("2026-09-19T10:00"),
        moment("2026-09-19T11:00"),
        EMPTY_CALENDAR,
    )

    assert verdict.charged_minutes == 60
    assert len(verdict.intervals) == 1


def test_an_empty_stack_is_no_data() -> None:
    verdict = evaluate_segment(
        [], moment("2026-09-14T10:00"), moment("2026-09-14T12:00"), EMPTY_CALENDAR
    )

    assert verdict.verdict is Verdict.NO_DATA
    assert verdict.window_minutes == 120
    assert verdict.intervals == []


def test_a_temporary_sign_adds_a_caveat() -> None:
    temporary = Regulation(action=Action.PARK, permitted=True, flags=Flags(temporary=True))

    verdict = evaluate_segment(
        stacked(temporary), moment("2026-09-14T10:00"), moment("2026-09-14T11:00"), EMPTY_CALENDAR
    )

    assert "a sign here marks itself temporary" in verdict.caveats


def test_a_rule_for_another_class_that_is_not_exclusive_is_ignored() -> None:
    """A truck loading rule says nothing about where a passenger car may park."""
    truck_only_prohibition = Regulation(
        action=Action.STAND, permitted=False, vehicle_class=VehicleClass.TRUCK
    )

    verdict = evaluate_segment(
        stacked(truck_only_prohibition),
        moment("2026-09-14T10:00"),
        moment("2026-09-14T11:00"),
        EMPTY_CALENDAR,
    )

    assert verdict.verdict is Verdict.LEGAL
    assert all(outcome.by_absence for outcome in verdict.intervals)


def test_a_stand_permission_does_not_authorise_parking() -> None:
    """A passenger loading zone permits standing only; parking there is still parking."""
    loading = Regulation(action=Action.STAND, permitted=True, time_from="08:00", time_to="18:00")

    verdict = evaluate_segment(
        stacked(loading), moment("2026-09-14T10:00"), moment("2026-09-14T11:00"), EMPTY_CALENDAR
    )

    assert verdict.verdict is Verdict.LEGAL
    assert all(outcome.by_absence for outcome in verdict.intervals)


def test_no_standing_handicap_bus_stop_is_illegal_for_us() -> None:
    """SPEC §8.4 ex. 4: NO STANDING (SINGLE ARROW) HANDICAP BUS W/4 ROUTES."""
    bus_stop = Regulation(action=Action.STAND, permitted=False)

    verdict = evaluate_segment(
        stacked(bus_stop), moment("2026-09-14T10:00"), moment("2026-09-14T11:00"), EMPTY_CALENDAR
    )

    assert verdict.verdict is Verdict.ILLEGAL
