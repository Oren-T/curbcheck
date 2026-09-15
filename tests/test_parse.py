"""End-to-end parser behaviour, anchored on the fifteen worked examples in SPEC §8.4."""

from __future__ import annotations

import csv
import time
from pathlib import Path

import pytest

from curbcheck.etl.parse import PARTIAL_CONFIDENCE, parse_description
from curbcheck.model import ALL_DAYS, Action, Arrow, Flags, ParseMethod, Regulation, VehicleClass

CORPUS = Path("data/explore/descriptions.tsv")

WEEKDAYS = [0, 1, 2, 3, 4]
MON_TO_SAT = [0, 1, 2, 3, 4, 5]


def only_rule(raw: str) -> Regulation:
    parsed = parse_description(raw)
    assert parsed.parse_method is ParseMethod.GRAMMAR
    assert parsed.confidence == 1.0, parsed.notes
    assert len(parsed.regulations) == 1, parsed.regulations
    return parsed.regulations[0]


# SPEC §8.4, example by example. Each asserts the whole Regulation, not a field.


def test_example_1_hmp_abbreviation_is_hour_metered_parking():
    # The middle dot is in the spec's sample record but in no live description
    # (docs/DATA.md §1.4); it must not stop the parse.
    assert only_rule("2 HMP SATURDAY 8AM-7PM ·") == Regulation(
        action=Action.PARK,
        permitted=True,
        days=[5],
        time_from="08:00",
        time_to="19:00",
        metered=True,
        max_duration_min=120,
    )


def test_example_2_broom_symbol_sets_street_cleaning():
    assert only_rule("NO PARKING (SANITATION BROOM SYMBOL) MONDAY THURSDAY 9AM-10:30AM") == (
        Regulation(
            action=Action.PARK,
            permitted=False,
            days=[0, 3],
            time_from="09:00",
            time_to="10:30",
            flags=Flags(street_cleaning=True),
        )
    )


def test_example_3_midnight_start_and_double_arrow():
    raw = (
        "NO PARKING (SANITATION BROOM SYMBOL) MOON & STARS (SYMBOLS)"
        " MONDAY THURSDAY MIDNIGHT-3AM <->"
    )
    assert only_rule(raw) == Regulation(
        action=Action.PARK,
        permitted=False,
        days=[0, 3],
        time_from="00:00",
        time_to="03:00",
        flags=Flags(street_cleaning=True),
        arrow=Arrow.BOTH,
    )


def test_example_4_single_arrow_and_bus_route_rider():
    # `(SINGLE ARROW)` carries arity only; the bearing comes from the sign row's
    # arrow_direction column (docs/DECISIONS.md D3 refined).
    assert only_rule("NO STANDING (SINGLE ARROW) HANDICAP BUS (SYMBOL) W/4 ROUTES") == (
        Regulation(action=Action.STAND, permitted=False, arrow=Arrow.FORWARD)
    )


def test_example_5_seasonal_farmers_market_is_exclusive():
    raw = "TRUCK (SYMBOL) FARMERS MARKET ONLY JUNE 1 - NOV 30 WEDNESDAY 8AM-4PM -->"
    rule = only_rule(raw)
    assert rule == Regulation(
        action=Action.STAND,
        permitted=True,
        vehicle_class=VehicleClass.OTHER,
        exclusive=True,
        days=[2],
        time_from="08:00",
        time_to="16:00",
        effective_from="06-01",
        effective_to="11-30",
        arrow=Arrow.FORWARD,
    )
    assert rule.applies_to_passenger() is False


def test_example_6_avo_school_sign():
    raw = "STAR (SYMBOL) AVO DEPT OF EDUCATION SCHOOL DAYS 7AM-4PM --> (PUBLIC SCHOOL SIGN)"
    rule = only_rule(raw)
    assert rule == Regulation(
        action=Action.PARK,
        permitted=True,
        vehicle_class=VehicleClass.AUTHORIZED,
        exclusive=True,
        days=WEEKDAYS,
        time_from="07:00",
        time_to="16:00",
        flags=Flags(school_days=True),
        arrow=Arrow.FORWARD,
    )
    assert rule.applies_to_passenger() is False


def test_example_7_meter_meta_rule_carries_only_the_meta_flag():
    raw = (
        "METERS ARE NOT IN EFFECT ABOVE TIMES (TO BE USED ONLY FOR CONFLICTING"
        " STREET CLEANING AND METERED PARKING REGULATIONS) (SUPERSEDES SW-473)"
    )
    rule = only_rule(raw)
    assert rule.flags.meta is True
    assert rule.days == list(ALL_DAYS)
    assert rule.time_from is None


def test_example_8_no_standing_anytime():
    assert only_rule("NO STANDING ANYTIME") == Regulation(action=Action.STAND, permitted=False)


def test_example_9_no_parking_anytime():
    assert only_rule("NO PARKING ANYTIME") == Regulation(action=Action.PARK, permitted=False)


def test_example_10_including_sunday_overrides_the_meter_exemption():
    assert only_rule("1 HOUR METERED PARKING 9AM-7PM INCLUDING SUNDAY") == Regulation(
        action=Action.PARK,
        permitted=True,
        days=list(ALL_DAYS),
        time_from="09:00",
        time_to="19:00",
        metered=True,
        max_duration_min=60,
        flags=Flags(including_sunday=True),
    )


def test_example_11_except_sunday_is_a_day_set_and_a_flag():
    assert only_rule("2 HOUR PARKING 8AM-6PM EXCEPT SUNDAY") == Regulation(
        action=Action.PARK,
        permitted=True,
        days=MON_TO_SAT,
        time_from="08:00",
        time_to="18:00",
        metered=False,
        max_duration_min=120,
        flags=Flags(except_sunday=True),
    )


def test_example_12_abbreviated_days_and_backward_glyph():
    # `<--` occurs on 2 of 74,590 active rows and never with a bearing, so it is
    # emitted as the same single-arrow placeholder as `-->`.
    assert only_rule("NO PARKING (SANITATION BROOM) TUES FRI 11:30AM-1PM <--") == Regulation(
        action=Action.PARK,
        permitted=False,
        days=[1, 4],
        time_from="11:30",
        time_to="13:00",
        flags=Flags(street_cleaning=True),
        arrow=Arrow.FORWARD,
    )


def test_example_13_snow_emergency_is_conditional_not_always_active():
    assert only_rule("NO STOPPING (SNOW EMERGENCY) ANYTIME") == Regulation(
        action=Action.STOP, permitted=False, flags=Flags(snow_emergency=True)
    )


def test_example_14_commercial_only_prohibits_a_passenger_car():
    rule = only_rule("3 HOUR PARKING 9AM-6PM MON-FRI (SYMBOL) COMMERCIAL VEHICLES ONLY")
    assert rule == Regulation(
        action=Action.PARK,
        permitted=True,
        vehicle_class=VehicleClass.COMMERCIAL,
        exclusive=True,
        days=WEEKDAYS,
        time_from="09:00",
        time_to="18:00",
        max_duration_min=180,
    )
    assert rule.applies_to_passenger() is False


def test_example_15_night_range_wraps_past_midnight():
    assert only_rule("NIGHT REGULATION NO STANDING 8PM-6AM ALL DAYS") == Regulation(
        action=Action.STAND,
        permitted=False,
        days=list(ALL_DAYS),
        time_from="20:00",
        time_to="06:00",
    )


# Representation rules the rest of the pipeline depends on.


@pytest.mark.parametrize(
    "raw",
    [
        "NO STANDING EXCEPT TRUCKS LOADING & UNLOADING",
        "NO PARKING EXCEPT AUTHORIZED VEHICLES",
        "NO STOPPING ANYTIME EXCEPT AUTHORIZED VEHICLES <------->",
    ],
)
def test_except_class_riders_stay_prohibitions_for_all_vehicles(raw):
    rule = only_rule(raw)
    assert rule.permitted is False
    assert rule.vehicle_class is VehicleClass.ALL
    assert rule.exclusive is False


@pytest.mark.parametrize(
    ("raw", "vehicle_class"),
    [
        ("BUS STOP SIGN (BUS & HANDICAP SYMBOLS) NO STANDING <----->", VehicleClass.BUS),
        ("TAXI HAILING (SYMBOL) TAXI STAND -->", VehicleClass.TAXI),
        ("NO STANDING FIRE ZONE", VehicleClass.AUTHORIZED),
        ("TRUCK (SYMBOL) TRUCK LOADING ONLY <->", VehicleClass.TRUCK),
        ("NO STANDING HOTEL LOADING ZONE -->", VehicleClass.OTHER),
    ],
)
def test_class_reservations_use_the_exclusive_representation(raw, vehicle_class):
    rule = only_rule(raw)
    assert (rule.permitted, rule.exclusive, rule.vehicle_class) == (True, True, vehicle_class)
    assert rule.applies_to_passenger() is False


# Regressions found by scoring the grammar against tests/gold/gold_set.jsonl.
# Each is a shape the gold set disagreed with; see tests/gold/ADJUDICATIONS.md.


def test_day_range_survives_a_space_on_one_side_of_the_joiner():
    # `MONDAY -FRIDAY` read as the single day Monday left four weekdays unposted.
    rule = only_rule(
        "FOR HIRE VEHICLE (SYMBOL) FOR HIRE VEHICLES ONLY MONDAY -FRIDAY 8AM-MIDNIGHT <->"
    )
    assert rule.days == WEEKDAYS


def test_a_class_named_after_a_prohibitions_hours_does_not_take_over_its_schedule():
    """The reservation covers the whole week; only the no-standing part is timed."""
    parsed = parse_description(
        "NO STANDING 8AM-6PM EXCEPT SUNDAY --> W/ ACCESS-A-RIDE (SYMBOL) ACCESS-A-RIDE BUS STOP"
    )
    prohibition, reservation = parsed.regulations
    assert (prohibition.permitted, prohibition.time_from, prohibition.days) == (
        False,
        "08:00",
        MON_TO_SAT,
    )
    assert (reservation.vehicle_class, reservation.exclusive) == (VehicleClass.BUS, True)
    assert (reservation.time_from, reservation.days) == (None, list(ALL_DAYS))


def test_an_except_rider_that_names_a_reservation_stays_one_prohibition():
    """The reservation is the exception to these hours, not a rule of its own."""
    rule = only_rule(
        "NO STANDING 5PM-MIDNIGHT MON-FRI EXCEPT TLC LICENSED VEHICLES"
        " PRE-ARRANGED SERVICE ONLY W/ SINGLE ARROW"
    )
    assert (rule.permitted, rule.vehicle_class, rule.exclusive) == (False, VehicleClass.ALL, False)
    assert (rule.days, rule.time_from, rule.time_to) == (WEEKDAYS, "17:00", "00:00")


@pytest.mark.parametrize(
    "raw",
    [
        "FHV (SYMBOL) FOR-HIRE VEHICLES ONLY PICK-UP / DROP-OFF ONLY -->",
        "MICROHUB (SYMBOL) MICROHUB ZONE VEHICLES WITH PERMIT ONLY <->",
    ],
)
def test_two_names_for_one_reservation_yield_one_regulation(raw):
    assert only_rule(raw).exclusive is True


def test_a_blank_template_that_still_names_a_rule_is_unparsed_not_a_panel():
    # `panel:template` tells the engine the curb is free. These two say a bus
    # stop and a no-standing zone are there, with the schedule left blank.
    parsed = parse_description("DAY - DAY XYY-XYY (FOR BUS STOP ONLY)")
    assert parsed.parse_method is ParseMethod.UNPARSED
    assert parsed.regulations == []

    blank_schedule = parse_description(
        "NO STANDING W/ SINGLE ARROW ZZZ THRU ZZZ XX:XXYY-XX:XXYY W/ BUS & HANDICAP (SYMBOLS)"
        " (TWO ROUTES) (DAYS AND TIMES TO BE SPECIFIED)"
    )
    assert blank_schedule.confidence == PARTIAL_CONFIDENCE
    assert blank_schedule.regulations[0].applies_to_passenger() is False


def test_a_greenway_guide_sign_regulates_no_curb():
    parsed = parse_description("BIKE (SYMBOL) LATOURETTE PARK GREENWAY W/ 9M O'CLOCK ARROW")
    assert parsed.parse_method is ParseMethod.GRAMMAR
    assert parsed.regulations == []


def test_two_metered_clauses_become_two_regulations():
    parsed = parse_description("2 HMP 7AM-6PM EXCEPT SUNDAY 6 HMP 6PM-MIDNIGHT EXCEPT SUNDAY <->")
    assert parsed.confidence == 1.0
    assert [rule.max_duration_min for rule in parsed.regulations] == [120, 360]
    assert [(rule.time_from, rule.time_to) for rule in parsed.regulations] == [
        ("07:00", "18:00"),
        ("18:00", "00:00"),
    ]


def test_two_day_groups_in_one_clause_become_two_regulations():
    parsed = parse_description("6 HMP MONDAY-FRIDAY 6PM-MIDNIGHT SATURDAY 8AM-MIDNIGHT <->")
    assert parsed.confidence == 1.0
    assert [rule.days for rule in parsed.regulations] == [WEEKDAYS, [5]]


def test_two_time_ranges_in_one_day_group_become_two_regulations():
    parsed = parse_description("NO STANDING MONDAY-FRIDAY 7AM-10AM 4PM-7PM")
    assert parsed.confidence == 1.0
    assert [(rule.days, rule.time_from, rule.time_to) for rule in parsed.regulations] == [
        (WEEKDAYS, "07:00", "10:00"),
        (WEEKDAYS, "16:00", "19:00"),
    ]


# Confidence and the never-invent-a-permission rule.


def test_partial_match_on_a_prohibition_keeps_the_prohibition_at_lower_confidence():
    parsed = parse_description("NO PARKING ANYTIME QWERTYUIOP")
    assert parsed.parse_method is ParseMethod.GRAMMAR
    assert parsed.confidence == PARTIAL_CONFIDENCE
    assert parsed.regulations[0].applies_to_passenger() is False
    assert "QWERTYUIOP" in parsed.notes


def test_partial_match_on_a_permissive_sign_is_unparsed():
    parsed = parse_description("2 HMP 9AM-7PM EXCEPT SUNDAY QWERTYUIOP")
    assert parsed.parse_method is ParseMethod.UNPARSED
    assert parsed.regulations == []
    assert parsed.confidence == 0.0


def test_unrecognized_string_is_unparsed_not_guessed():
    parsed = parse_description("GOVERNORS IS FERRY TICKET BOOTH")
    assert parsed.parse_method is ParseMethod.UNPARSED
    assert parsed.regulations == []


# Hostile and degenerate input. SPEC §3.3: downloaded text is untrusted.


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "\x00",
        "NO PARKING\x00ANYTIME",
        "<script>alert(1)</script>",
        "=cmd|' /C calc'!A0",
        "'; DROP TABLE sign; --",
        "你好世界",
        "NO PARKING \U0001f697 ANYTIME",
        "(" * 200,
        "-" * 200,
        "A" * 5000,
        "NO PARKING " * 500,
        "8AM-7PM " * 400,
    ],
)
def test_hostile_input_returns_a_result_without_raising(raw):
    started = time.monotonic()
    parsed = parse_description(raw)
    assert time.monotonic() - started < 1.0
    assert parsed.raw == raw
    if parsed.parse_method is ParseMethod.UNPARSED:
        assert parsed.regulations == []
        assert parsed.confidence == 0.0
    # Junk wrapped around real sign words (`NO PARKING\x00ANYTIME`) may still
    # read as the prohibition it looks like. What it may never do is permit.
    assert all(rule.applies_to_passenger() is not True for rule in parsed.regulations)


def test_null_bytes_and_control_characters_do_not_reach_the_output():
    parsed = parse_description("NO PARKING\x07 ANYTIME\x00")
    assert parsed.confidence == 1.0
    assert parsed.regulations[0].action is Action.PARK


def test_parse_is_pure_and_cached():
    raw = "NO STANDING ANYTIME <->"
    assert parse_description(raw) is parse_description(raw)


# Property: the whole live vocabulary parses without raising.


@pytest.mark.skipif(not CORPUS.exists(), reason="corpus snapshot is gitignored")
def test_every_live_description_parses_without_raising():
    with CORPUS.open(newline="") as handle:
        descriptions = [row["sign_description"] for row in csv.DictReader(handle, delimiter="\t")]
    assert len(descriptions) > 1000
    for description in descriptions:
        parsed = parse_description(description)
        assert parsed.raw == description
        assert 0.0 <= parsed.confidence <= 1.0
        if parsed.parse_method is ParseMethod.UNPARSED:
            assert parsed.regulations == []


@pytest.mark.skipif(not CORPUS.exists(), reason="corpus snapshot is gitignored")
def test_no_live_description_yields_an_unconditional_permission_for_a_passenger_car():
    """A rule permitting any vehicle at any hour would be a false 'legal' waiting to happen."""
    with CORPUS.open(newline="") as handle:
        rows = [
            (int(row["count"]), row["sign_description"])
            for row in csv.DictReader(handle, delimiter="\t")
        ]
    offenders = [
        raw
        for _, raw in rows
        for rule in parse_description(raw).regulations
        if rule.applies_to_passenger() is True
        and rule.time_from is None
        and not rule.flags.meta
        and rule.max_duration_min is None
    ]
    assert offenders == []
