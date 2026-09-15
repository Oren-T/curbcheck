"""The DOT ASP suspension ICS reader."""

from __future__ import annotations

from datetime import date

from curbcheck.etl.calendar import (
    MAX_EVENT_DAYS,
    expanded_day_count,
    find_ics,
    is_major_legal_holiday,
    load_asp_suspensions,
    parse_ics,
    unescape,
    unfold,
)

CHRISTMAS = """BEGIN:VCALENDAR
BEGIN:VEVENT
DESCRIPTION:Alternate Side Parking suspended for Christmas Day. Parking met
\ters will not be in effect. Stopping\\, standing and parking are permitted.
DTEND;VALUE=DATE:20261226
DTSTART;VALUE=DATE:20261225
SUMMARY;LANGUAGE=en-us:Alternate Side Parking Suspended
END:VEVENT
END:VCALENDAR
"""

TWO_DAY = """BEGIN:VCALENDAR
BEGIN:VEVENT
DESCRIPTION:Alternate Side Parking suspended for Rosh Hashanah. Parking meters will be in effect.
DTEND;VALUE=DATE:20260914
DTSTART;VALUE=DATE:20260912
END:VEVENT
END:VCALENDAR
"""

NO_DTEND = """BEGIN:VCALENDAR
BEGIN:VEVENT
DESCRIPTION:Alternate Side Parking suspended for Holy Thursday. Parking meters will be in effect.
DTSTART;VALUE=DATE:20260402
END:VEVENT
END:VCALENDAR
"""


def test_folded_description_lines_rejoin_before_the_meter_sentence_is_read():
    # The sentence that decides the meter status is split across the fold, so a
    # reader that does not unfold would see "Parking met" and miss it entirely.
    assert "meters will not be in effect" in unfold(CHRISTMAS)

    [day] = parse_ics(CHRISTMAS)

    assert day.date == date(2026, 12, 25)
    assert day.label == "Christmas Day"
    assert day.meters_suspended is True
    assert day.is_major_legal_holiday is True


def test_dtend_is_exclusive_so_a_two_day_holiday_yields_two_dates():
    days = parse_ics(TWO_DAY)

    assert [day.date for day in days] == [date(2026, 9, 12), date(2026, 9, 13)]
    assert all(day.meters_suspended is False for day in days)
    assert all(day.is_major_legal_holiday is False for day in days)


def test_an_event_without_dtend_covers_its_start_date_only():
    [day] = parse_ics(NO_DTEND)

    assert day.date == date(2026, 4, 2)
    assert day.label == "Holy Thursday"


def test_a_runaway_dtend_cannot_expand_without_bound():
    runaway = NO_DTEND.replace(
        "DTSTART;VALUE=DATE:20260402", "DTSTART;VALUE=DATE:20260402\nDTEND;VALUE=DATE:20991231"
    )

    assert len(parse_ics(runaway)) == MAX_EVENT_DAYS


def test_sundays_are_not_inserted_as_suspensions():
    # 2026-12-27 is the Sunday after Christmas; the engine handles Sundays by
    # weekday and a row here would claim the city suspended ASP that day.
    assert [day.date for day in parse_ics(CHRISTMAS)] == [date(2026, 12, 25)]


def test_a_major_holiday_is_read_from_the_meter_sentence_and_cross_checked_by_name():
    assert is_major_legal_holiday("Immaculate Conception", meters_suspended=False) is False
    assert is_major_legal_holiday("Immaculate Conception", meters_suspended=True) is True
    # The apostrophe DOT uses is the typographic one; the name list uses ASCII.
    assert is_major_legal_holiday("New Year\u2019s Day", meters_suspended=False) is True


def test_ics_text_escapes_are_undone():
    assert unescape("Stopping\\, standing\\; parking\\nare permitted") == (
        "Stopping, standing; parking are permitted"
    )


def test_overlapping_events_collapse_onto_one_row_but_are_still_counted():
    doubled = CHRISTMAS.replace("END:VCALENDAR", "") + TWO_DAY.split("BEGIN:VCALENDAR")[1]

    assert len(parse_ics(doubled)) == 3
    assert expanded_day_count(doubled) == 3

    twice = CHRISTMAS.replace("END:VCALENDAR", "") + CHRISTMAS.split("BEGIN:VCALENDAR")[1]

    assert len(parse_ics(twice)) == 1
    assert expanded_day_count(twice) == 2


def test_a_missing_calendar_is_an_empty_table_and_a_reported_gap(tmp_path):
    suspensions, report = load_asp_suspensions(tmp_path)

    assert suspensions == []
    assert report.source_path is None
    assert report.suspended_days == 0


def test_the_year_named_in_the_filename_picks_the_snapshot(tmp_path):
    directory = tmp_path / "calendar"
    directory.mkdir()
    (directory / "2025-alternate-side.ics").write_text(NO_DTEND, encoding="utf-8")
    (directory / "2026-alternate-side.ics").write_text(CHRISTMAS, encoding="utf-8")

    assert find_ics(tmp_path, 2025).name == "2025-alternate-side.ics"
    # With no year asked for, the newest file name wins.
    assert find_ics(tmp_path).name == "2026-alternate-side.ics"

    suspensions, report = load_asp_suspensions(tmp_path, 2026)

    assert [day.date for day in suspensions] == [date(2026, 12, 25)]
    assert report.major_holidays == 1
    assert report.unnamed_major_holidays == ()
