/**
 * Window expansion and per-rule activity, including the calendar overrides in SPEC §9.3.
 *
 * A port of `tests/test_engine_window.py`, test for test. Dates are chosen for
 * their weekday: 2026-09-14 is a Monday, 09-19 a Saturday, 09-20 a Sunday, and
 * 2026-03-08 is the spring-forward Sunday.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { parseRequestTime, wallKey } from "../static/time.js";
import { CalendarContext } from "../static/engine/calendar.js";
import { Interval, expandWindow, meterIsCharged, ruleIsActive } from "../static/engine/window.js";
import {
  ALL_DAYS,
  Action,
  Arrow,
  VehicleClass,
  appliesToPassenger,
} from "../static/engine/model.js";

const EMPTY_CALENDAR = new CalendarContext();

function moment(text) {
  return parseRequestTime(text);
}

function interval(start, end) {
  return new Interval(moment(start), moment(end));
}

function flags(overrides = {}) {
  return {
    streetCleaning: false,
    schoolDays: false,
    exceptSunday: false,
    includingSunday: false,
    snowEmergency: false,
    holidayExempt: false,
    temporary: false,
    meta: false,
    ...overrides,
  };
}

function reg(overrides = {}) {
  return {
    action: Action.PARK,
    permitted: false,
    vehicleClass: VehicleClass.ALL,
    exclusive: false,
    days: [...ALL_DAYS],
    timeFrom: null,
    timeTo: null,
    metered: false,
    maxDurationMin: null,
    flags: flags(),
    effectiveFrom: null,
    effectiveTo: null,
    arrow: Arrow.NONE,
    ...overrides,
  };
}

function bounds(intervals) {
  return intervals.map((item) => [wallKey(item.start), wallKey(item.end)]);
}

test("a window inside one day with no rules is a single interval", () => {
  const intervals = expandWindow(moment("2026-09-14T10:00"), moment("2026-09-14T12:00"));

  assert.deepStrictEqual(bounds(intervals), [["2026-09-14T10:00", "2026-09-14T12:00"]]);
});

test("an overnight window splits at midnight", () => {
  const intervals = expandWindow(moment("2026-09-14T22:00"), moment("2026-09-15T02:00"));

  assert.deepStrictEqual(bounds(intervals), [
    ["2026-09-14T22:00", "2026-09-15T00:00"],
    ["2026-09-15T00:00", "2026-09-15T02:00"],
  ]);
});

test("the window splits at every rule boundary", () => {
  const cleaning = reg({ days: [0], timeFrom: "09:00", timeTo: "10:30" });

  const intervals = expandWindow(moment("2026-09-14T08:00"), moment("2026-09-14T12:00"), [
    cleaning,
  ]);

  assert.deepStrictEqual(bounds(intervals), [
    ["2026-09-14T08:00", "2026-09-14T09:00"],
    ["2026-09-14T09:00", "2026-09-14T10:30"],
    ["2026-09-14T10:30", "2026-09-14T12:00"],
  ]);
});

test("rule boundaries outside the window do not add intervals", () => {
  const evening = reg({ timeFrom: "20:00", timeTo: "06:00" });

  const intervals = expandWindow(moment("2026-09-14T09:00"), moment("2026-09-14T11:00"), [evening]);

  assert.strictEqual(intervals.length, 1);
});

test("boundaries apply on every day the window covers", () => {
  const cleaning = reg({ days: [0, 3], timeFrom: "09:00", timeTo: "10:30" });

  const intervals = expandWindow(moment("2026-09-14T08:00"), moment("2026-09-16T08:00"), [
    cleaning,
  ]);
  const starts = intervals.map((item) => wallKey(item.start).slice(5).replace("T", " "));

  assert.deepStrictEqual(starts, [
    "09-14 08:00",
    "09-14 09:00",
    "09-14 10:30",
    "09-15 00:00",
    "09-15 09:00",
    "09-15 10:30",
    "09-16 00:00",
  ]);
});

test("expand_window rejects naive datetimes", () => {
  // The JS boundary is a wall clock with a `fold`; anything else never passed
  // through `parseRequestTime` and so never named an instant.
  const naive = { year: 2026, month: 9, day: 14, hour: 10, minute: 0, second: 0 };

  assert.throws(() => expandWindow(naive, moment("2026-09-14T12:00")), /timezone-aware/);
});

test("expand_window rejects a window that does not move forward", () => {
  assert.throws(
    () => expandWindow(moment("2026-09-14T12:00"), moment("2026-09-14T12:00")),
    /end after it starts/,
  );
});

test("a spring forward window loses the hour that does not exist", () => {
  const intervals = expandWindow(moment("2026-03-08T01:00"), moment("2026-03-08T04:00"));

  assert.strictEqual(
    intervals.reduce((total, item) => total + item.minutes, 0),
    120,
  );
});

test("interval reports its weekday and date", () => {
  const saturday = interval("2026-09-19T08:00", "2026-09-19T19:00");

  assert.strictEqual(saturday.weekday, 5);
  assert.strictEqual(saturday.date, "2026-09-19");
  assert.strictEqual(saturday.minutes, 660);
});

test("a rule is inactive on a day it does not list", () => {
  // SPEC §8.4 ex. 2: NO PARKING (BROOM) MONDAY THURSDAY 9AM-10:30AM.
  const cleaning = reg({ days: [0, 3], timeFrom: "09:00", timeTo: "10:30" });

  assert.ok(
    ruleIsActive(cleaning, interval("2026-09-14T09:00", "2026-09-14T10:30"), EMPTY_CALENDAR),
  );
  assert.ok(
    !ruleIsActive(cleaning, interval("2026-09-15T09:00", "2026-09-15T10:30"), EMPTY_CALENDAR),
  );
});

test("a rule with no times is active all day", () => {
  // SPEC §8.4 ex. 9: NO PARKING ANYTIME.
  const anytime = reg();

  assert.ok(
    ruleIsActive(anytime, interval("2026-09-20T03:00", "2026-09-20T04:00"), EMPTY_CALENDAR),
  );
});

test("a midnight wrapping range is active on both sides of midnight", () => {
  // SPEC §8.4 ex. 15: NIGHT REGULATION NO STANDING 8PM-6AM ALL DAYS.
  const night = reg({ action: Action.STAND, timeFrom: "20:00", timeTo: "06:00" });

  assert.ok(ruleIsActive(night, interval("2026-09-14T21:00", "2026-09-15T00:00"), EMPTY_CALENDAR));
  assert.ok(ruleIsActive(night, interval("2026-09-15T00:00", "2026-09-15T05:00"), EMPTY_CALENDAR));
  assert.ok(!ruleIsActive(night, interval("2026-09-15T07:00", "2026-09-15T08:00"), EMPTY_CALENDAR));
});

test("a range ending at midnight runs to the end of the day", () => {
  const evening = reg({ timeFrom: "19:00", timeTo: "00:00" });

  assert.ok(
    ruleIsActive(evening, interval("2026-09-14T23:00", "2026-09-15T00:00"), EMPTY_CALENDAR),
  );
  assert.ok(
    !ruleIsActive(evening, interval("2026-09-15T00:00", "2026-09-15T01:00"), EMPTY_CALENDAR),
  );
});

test("a seasonal rule is inactive outside its dates", () => {
  // SPEC §8.4 ex. 5: FARMERS MARKET JUNE 1 - NOV 30 WEDNESDAY 8AM-4PM.
  const market = reg({
    days: [2],
    timeFrom: "08:00",
    timeTo: "16:00",
    effectiveFrom: "06-01",
    effectiveTo: "11-30",
  });

  assert.ok(ruleIsActive(market, interval("2026-09-16T09:00", "2026-09-16T10:00"), EMPTY_CALENDAR));
  assert.ok(
    !ruleIsActive(market, interval("2026-12-16T09:00", "2026-12-16T10:00"), EMPTY_CALENDAR),
  );
});

test("seasonal bounds are inclusive on both ends", () => {
  const market = reg({ effectiveFrom: "06-01", effectiveTo: "11-30" });

  assert.ok(ruleIsActive(market, interval("2026-06-01T09:00", "2026-06-01T10:00"), EMPTY_CALENDAR));
  assert.ok(ruleIsActive(market, interval("2026-11-30T09:00", "2026-11-30T10:00"), EMPTY_CALENDAR));
  assert.ok(
    !ruleIsActive(market, interval("2026-05-31T09:00", "2026-05-31T10:00"), EMPTY_CALENDAR),
  );
});

test("a season may wrap the year end", () => {
  const winter = reg({ effectiveFrom: "11-15", effectiveTo: "03-15" });

  assert.ok(ruleIsActive(winter, interval("2026-12-25T09:00", "2026-12-25T10:00"), EMPTY_CALENDAR));
  assert.ok(ruleIsActive(winter, interval("2026-01-05T09:00", "2026-01-05T10:00"), EMPTY_CALENDAR));
  assert.ok(
    !ruleIsActive(winter, interval("2026-07-04T09:00", "2026-07-04T10:00"), EMPTY_CALENDAR),
  );
});

test("street cleaning is suspended on an asp suspension date", () => {
  const cleaning = reg({
    days: [0],
    timeFrom: "09:00",
    timeTo: "10:30",
    flags: flags({ streetCleaning: true }),
  });
  const calendar = new CalendarContext({ aspSuspensionDates: ["2026-09-14"] });

  assert.ok(!ruleIsActive(cleaning, interval("2026-09-14T09:00", "2026-09-14T10:00"), calendar));
  assert.ok(
    ruleIsActive(cleaning, interval("2026-09-14T09:00", "2026-09-14T10:00"), EMPTY_CALENDAR),
  );
});

test("a holiday exempt rule is inactive on a major legal holiday", () => {
  const calendar = new CalendarContext({ majorHolidayDates: ["2026-12-25"] });
  const rule = reg({ days: [4], flags: flags({ holidayExempt: true }) });

  assert.ok(!ruleIsActive(rule, interval("2026-12-25T09:00", "2026-12-25T10:00"), calendar));
});

test("a seven day prohibition survives a holiday", () => {
  // SPEC §9.3(a): NO STANDING ANYTIME and friends remain in force on holidays.
  const calendar = new CalendarContext({ majorHolidayDates: ["2026-12-25"] });
  const standing = reg({ action: Action.STAND });

  assert.ok(ruleIsActive(standing, interval("2026-12-25T09:00", "2026-12-25T10:00"), calendar));
});

test("a snow emergency rule is inactive unless an emergency is declared", () => {
  // SPEC §8.4 ex. 13: NO STOPPING (SNOW EMERGENCY) ANYTIME.
  const snow = reg({ action: Action.STOP, flags: flags({ snowEmergency: true }) });
  const declared = new CalendarContext({ snowEmergency: true });

  assert.ok(!ruleIsActive(snow, interval("2026-01-05T09:00", "2026-01-05T10:00"), EMPTY_CALENDAR));
  assert.ok(ruleIsActive(snow, interval("2026-01-05T09:00", "2026-01-05T10:00"), declared));
});

test("a school day rule is assumed active when the calendar is unknown", () => {
  // SPEC §8.4 ex. 6: assuming school is out would risk a false legal verdict.
  const school = reg({
    days: [0],
    timeFrom: "07:00",
    timeTo: "16:00",
    flags: flags({ schoolDays: true }),
  });

  assert.ok(ruleIsActive(school, interval("2026-09-14T08:00", "2026-09-14T09:00"), EMPTY_CALENDAR));
});

test("a school day rule is inactive on a known non school day", () => {
  const school = reg({
    days: [0],
    timeFrom: "07:00",
    timeTo: "16:00",
    flags: flags({ schoolDays: true }),
  });
  const calendar = new CalendarContext({ nonSchoolDates: ["2026-09-14"] });

  assert.ok(!ruleIsActive(school, interval("2026-09-14T08:00", "2026-09-14T09:00"), calendar));
});

test("an except sunday rule is inactive on sunday", () => {
  // SPEC §8.4 ex. 11: 2 HOUR PARKING 8AM-6PM EXCEPT SUNDAY.
  const rule = reg({
    permitted: true,
    days: [0, 1, 2, 3, 4, 5, 6],
    timeFrom: "08:00",
    timeTo: "18:00",
    flags: flags({ exceptSunday: true }),
  });

  assert.ok(!ruleIsActive(rule, interval("2026-09-20T09:00", "2026-09-20T10:00"), EMPTY_CALENDAR));
});

test("meters are not charged on sunday", () => {
  const meter = reg({ permitted: true, metered: true, timeFrom: "09:00", timeTo: "19:00" });

  assert.ok(
    !meterIsCharged(meter, interval("2026-09-20T10:00", "2026-09-20T11:00"), EMPTY_CALENDAR),
  );
  assert.ok(
    meterIsCharged(meter, interval("2026-09-19T10:00", "2026-09-19T11:00"), EMPTY_CALENDAR),
  );
});

test("including sunday meters are charged on sunday", () => {
  // SPEC §8.4 ex. 10: 1 HOUR METERED PARKING 9AM-7PM INCLUDING SUNDAY.
  const meter = reg({
    permitted: true,
    metered: true,
    maxDurationMin: 60,
    timeFrom: "09:00",
    timeTo: "19:00",
    flags: flags({ includingSunday: true }),
  });

  assert.ok(
    meterIsCharged(meter, interval("2026-09-20T10:00", "2026-09-20T11:00"), EMPTY_CALENDAR),
  );
});

test("meters are not charged on a major legal holiday", () => {
  const meter = reg({ permitted: true, metered: true });
  const calendar = new CalendarContext({
    majorHolidayDates: ["2026-12-25"],
    aspSuspensionDates: ["2026-12-25"],
  });

  assert.ok(!meterIsCharged(meter, interval("2026-12-25T10:00", "2026-12-25T11:00"), calendar));
});

test("an unmetered rule is never charged", () => {
  const free = reg({ permitted: true, maxDurationMin: 120 });

  assert.ok(
    !meterIsCharged(free, interval("2026-09-19T10:00", "2026-09-19T11:00"), EMPTY_CALENDAR),
  );
});

test("calendar context reads asp suspension rows", () => {
  const calendar = CalendarContext.fromAspRows([
    {
      date: "2026-12-25",
      is_major_legal_holiday: 1,
      meters_suspended: 1,
      label: "Christmas Day",
    },
    {
      date: "2026-11-26",
      is_major_legal_holiday: 0,
      meters_suspended: 0,
      label: "Diwali",
    },
  ]);

  assert.ok(calendar.isAspSuspended("2026-11-26"));
  assert.ok(!calendar.isMajorHoliday("2026-11-26"));
  assert.ok(calendar.isMajorHoliday("2026-12-25"));
  assert.ok(!calendar.metersInEffect("2026-12-25"));
  assert.ok(calendar.metersInEffect("2026-11-26"));
  assert.strictEqual(calendar.labels.get("2026-12-25"), "Christmas Day");
});

test("an exclusive class rule reads as prohibited for a passenger car", () => {
  // SPEC §8.4 ex. 14: COMMERCIAL VEHICLES ONLY is a prohibition for us.
  const commercial = reg({
    permitted: true,
    vehicleClass: VehicleClass.COMMERCIAL,
    exclusive: true,
    days: [0, 1, 2, 3, 4],
    timeFrom: "09:00",
    timeTo: "18:00",
  });

  assert.strictEqual(appliesToPassenger(commercial), false);
});
