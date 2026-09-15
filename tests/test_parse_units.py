"""The parser's sub-parsers on their own: lexer, days, times, seasons, panels."""

from __future__ import annotations

import pytest

from curbcheck.etl.parse import days, panels, times
from curbcheck.etl.parse.tokens import MAX_INPUT_CHARS, lex, tokenize
from curbcheck.model import Arrow

# Lexer


@pytest.mark.parametrize(
    ("raw", "arrow"),
    [
        ("NO PARKING ANYTIME", Arrow.NONE),
        ("NO PARKING ANYTIME <->", Arrow.BOTH),
        ("NO PARKING ANYTIME <----->", Arrow.BOTH),
        ("NO PARKING ANYTIME -->", Arrow.FORWARD),
        ("NO PARKING ANYTIME <--", Arrow.FORWARD),
        ("NO STANDING W/ SINGLE ARROW", Arrow.FORWARD),
        ("NO STANDING W/SINGLE ARROW", Arrow.FORWARD),
        ("NO STANDING (SINGLE ARROW)", Arrow.FORWARD),
        ("NO STANDING IN TUNNEL W/ 7 O'CLOCK ARROW", Arrow.FORWARD),
    ],
)
def test_every_arrow_spelling_is_recognized(raw, arrow):
    assert lex(raw).arrow is arrow


def test_supersedes_tail_is_recorded_not_parsed():
    lexed = lex("NO PARKING ANYTIME <-> (SUPERSEDES SP-854C)")
    assert lexed.supersedes == ("SUPERSEDES SP-854C",)
    assert "SUPERSEDES" not in lexed.text


def test_symbol_parenthetical_takes_its_noun_with_it():
    assert lex("TRUCK (SYMBOL) TRUCK LOADING ONLY").text == "TRUCK LOADING ONLY"
    assert lex("MOON & STARS (SYMBOLS) NO STANDING").text == "NO STANDING"
    assert lex("NO PARKING (SANITATION BROOM SYMBOL) W/ (MOON/STARS SYMBOLS) MON").text == (
        "NO PARKING MON"
    )


def test_broom_and_snow_parentheticals_set_their_flags():
    assert lex("NO PARKING (SANITATION BROOM SYMBOL) MON").street_cleaning is True
    assert lex("NO PARKING (SANITATION BROOM) MON").street_cleaning is True
    assert lex("NO STOPPING (SNOW EMERGENCY) ANYTIME").snow_emergency is True


def test_input_is_truncated_rather_than_scanned_in_full():
    assert len(lex("A" * 5000).text) <= MAX_INPUT_CHARS


def test_tokenizer_drops_separator_punctuation_but_keeps_a_bare_hyphen():
    assert tokenize("NO STANDING /BUS & HANDICAP ; MON - FRI") == [
        "NO",
        "STANDING",
        "BUS",
        "HANDICAP",
        "MON",
        "-",
        "FRI",
    ]


# Days


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("MONDAY", (0,)),
        ("SUN", (6,)),
        ("MONDAY THURSDAY", (0, 3)),
        ("TUESDAY THURSDAY SATURDAY", (1, 3, 5)),
        ("MONDAY-FRIDAY", (0, 1, 2, 3, 4)),
        ("MON-FRI", (0, 1, 2, 3, 4)),
        ("MON THRU FRI", (0, 1, 2, 3, 4)),
        ("MONDAY - FRIDAY", (0, 1, 2, 3, 4)),
        ("FRI-SUN", (4, 5, 6)),
        ("SUN-THURS", (6, 0, 1, 2, 3)),
        ("ALL DAYS", (0, 1, 2, 3, 4, 5, 6)),
        ("7 DAYS", (0, 1, 2, 3, 4, 5, 6)),
        ("EXCEPT SUNDAY", (0, 1, 2, 3, 4, 5)),
        ("INCLUDING SUNDAY", (0, 1, 2, 3, 4, 5, 6)),
        ("SCHOOL DAYS", (0, 1, 2, 3, 4)),
    ],
)
def test_day_expressions(text, expected):
    spec = days.scan_days(text.split(), 0)
    assert spec is not None
    assert spec.days == expected


def test_day_range_wrapping_past_sunday_keeps_calendar_order():
    spec = days.scan_days(["FRI-MON"], 0)
    assert spec is not None
    assert spec.days == (4, 5, 6, 0)


def test_day_expression_flags():
    assert days.scan_days(["EXCEPT", "SUNDAY"], 0).except_sunday is True
    assert days.scan_days(["INCLUDING", "SUNDAY"], 0).including_sunday is True
    assert days.scan_days(["SCHOOL", "DAYS"], 0).school_days is True
    assert days.scan_days(["SATURDAY", "SUNDAY", "HOLIDAYS"], 0).holidays is True


def test_a_word_that_is_not_a_day_is_not_read_as_one():
    assert days.scan_days(["HOTEL"], 0) is None
    assert days.scan_days(["MAY"], 0) is None


# Times


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("8AM-7PM", "@08:00-19:00"),
        ("11:30AM-1PM", "@11:30-13:00"),
        ("MIDNIGHT-3AM", "@00:00-03:00"),
        ("6PM-MIDNIGHT", "@18:00-00:00"),
        ("8AM TO 6PM", "@08:00-18:00"),
        ("NOON-7PM", "@12:00-19:00"),
        ("8AM-NOON", "@08:00-12:00"),
        ("7-10AM", "@07:00-10:00"),
        ("4-7PM", "@16:00-19:00"),
        ("6AM - 5PM", "@06:00-17:00"),
        ("10A M-11:30AM", "@10:00-11:30"),
        ("8AM- MIDNIGHT", "@08:00-00:00"),
        ("12AM-12PM", "@00:00-12:00"),
    ],
)
def test_clock_ranges(text, expected):
    assert times.mark_atoms(text).split() == [expected]


def test_two_clock_ranges_stay_separate():
    assert times.mark_atoms("7AM-10AM 4PM-7PM").split() == ["@07:00-10:00", "@16:00-19:00"]


@pytest.mark.parametrize(
    "text",
    ["SUPERSEDES SP-14A", "REVISED 6-30-99", "REVISION #2 6-4-02", "CALL 212-NYC-TAXI"],
)
def test_drafting_marks_are_not_read_as_times(text):
    assert times.TIME_PREFIX not in times.mark_atoms(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("JUNE 1 - NOV 30", "#06-01:11-30"),
        ("MAY 15-SEP 15", "#05-15:09-15"),
        ("MAY-OCTOBER", "#05-01:10-31"),
        ("MARCH-NOVEMBER", "#03-01:11-30"),
        ("FEB 1 - DEC 31", "#02-01:12-31"),
        ("APR 1 - DEC 31", "#04-01:12-31"),
    ],
)
def test_seasonal_ranges(text, expected):
    assert times.mark_atoms(text).split() == [expected]


def test_may_as_a_verb_is_not_read_as_a_season():
    assert times.SEASON_PREFIX not in times.mark_atoms("DIAMOND DELINEATOR MAY BE USED AS W14-2A")


def test_marked_tokens_are_recognized_and_read_back():
    assert times.is_time_token("@08:00-19:00")
    assert times.read_time_token("@08:00-19:00") == ("08:00", "19:00")
    assert times.is_season_token("#06-01:11-30")
    assert times.read_season_token("#06-01:11-30") == ("06-01", "11-30")
    assert not times.is_season_token("#2")
    assert not times.is_time_token("@")


# Panels


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("LOCAL MTA BUS ROUTE PANEL (TEXT TO BE MODIFIED AS REQUESTED)", "panel:mta_route"),
        ("EXPRESS MTA BUS DESTINATION PANEL", "panel:mta_route"),
        ("LOCAL 2 ROUTE BUS RIDER", "panel:mta_route"),
        ("PAY-BY-CELL LOCATOR NUMBER", "panel:pay_by_cell"),
        ("6TH AVE PAY-BY-APP INFORMATION SIGN", "panel:pay_by_cell"),
        ("14 STREET & UNION SQ (BOTTOM LOCATION PANEL)", "panel:location"),
        ("DAY - DAY XYY-XYY (FOR BUS STOP ONLY)", "panel:template"),
        ("WEIGHT LIMIT XXXX LBS INCLUDING PASSENGERS AND CARGO", "panel:template"),
        ("BACK IN ANGLE PARKING ONLY <->", "panel:parking_geometry"),
        ("(SUPERSEDES SP-854C)", "panel:supersedes_only"),
        ("", "panel:blank"),
        ("   ", "panel:blank"),
    ],
)
def test_panel_class_labels(raw, expected):
    assert panels.panel_class(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "NO PARKING ANYTIME <->",
        "2 HMP 9AM-7PM EXCEPT SUNDAY <->",
        "BUS STOP SIGN (BUS & HANDICAP SYMBOLS) NO STANDING <----->",
        "TRUCK (SYMBOL) TRUCK LOADING ONLY MONDAY-FRIDAY 7AM-7PM <->",
    ],
)
def test_real_regulations_are_not_classified_as_panels(raw):
    assert panels.panel_class(raw) is None


def test_advisory_class_only_fires_on_advisory_vocabulary():
    assert panels.advisory_class("THRU TRAFFIC KEEP LEFT") == "panel:advisory"
    assert panels.advisory_class("NO TRUCKS") == "panel:advisory"
    assert panels.advisory_class("NO PARKING ANYTIME") is None
