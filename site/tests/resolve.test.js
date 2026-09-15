/**
 * Most-restrictive-wins resolution over the SPEC §8.4 worked examples.
 *
 * A port of `tests/test_engine_resolve.py`, test for test. Each test builds the
 * rule stack a correct parse of the named sign would produce and asserts the
 * verdict a driver would get. 2026-09-14 is a Monday, 09-16 a Wednesday, 09-17 a
 * Thursday, 09-19 a Saturday, 09-20 a Sunday. The last two blocks cover
 * `model.js` and `labels.js`, which have no Python test of their own to port.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { parseRequestTime, wallKey } from "../static/time.js";
import { CalendarContext } from "../static/engine/calendar.js";
import {
  ABSENCE_REASON,
  AMBIGUITY_THRESHOLD,
  NO_DATA_CAVEAT,
  UNKNOWN_GAP_REASON,
  Verdict,
  VerdictBasis,
  evaluateSegment,
  resolveInterval,
} from "../static/engine/resolve.js";
import { Interval } from "../static/engine/window.js";
import {
  ALL_DAYS,
  Action,
  Arrow,
  ParseMethod,
  VehicleClass,
  regulationFromPack,
  regulationToJson,
  regulationWithMeta,
} from "../static/engine/model.js";
import { betweenPhrase, singleSpaced, spanLabel } from "../static/engine/labels.js";

const EMPTY_CALENDAR = new CalendarContext();

function moment(text) {
  return parseRequestTime(text);
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

function regulation(overrides = {}) {
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

function stacked(
  regulations,
  { method = ParseMethod.GRAMMAR, confidence = 0.98, raw = "TEST SIGN" } = {},
) {
  return regulations.map((reg) => ({
    regulation: reg,
    parseMethod: method,
    parseConfidence: confidence,
    regSegId: "seg-1",
    rawSignDescription: raw,
  }));
}

/** SPEC §8.4 ex. 9. */
function noParkingAnytime() {
  return regulation({ action: Action.PARK, permitted: false });
}

/** SPEC §8.4 ex. 8. */
function noStandingAnytime() {
  return regulation({ action: Action.STAND, permitted: false });
}

/** SPEC §8.4 ex. 2: NO PARKING (BROOM) MONDAY THURSDAY 9AM-10:30AM. */
function streetCleaningMonThu() {
  return regulation({
    days: [0, 3],
    timeFrom: "09:00",
    timeTo: "10:30",
    flags: flags({ streetCleaning: true }),
  });
}

/** SPEC §8.4 ex. 1: 2 HMP SATURDAY 8AM-7PM. */
function twoHourMeterSaturday() {
  return regulation({
    permitted: true,
    days: [5],
    timeFrom: "08:00",
    timeTo: "19:00",
    metered: true,
    maxDurationMin: 120,
  });
}

/** SPEC §8.4 ex. 10: 1 HOUR METERED PARKING 9AM-7PM INCLUDING SUNDAY. */
function oneHourMeterIncludingSunday() {
  return regulation({
    permitted: true,
    timeFrom: "09:00",
    timeTo: "19:00",
    metered: true,
    maxDurationMin: 60,
    flags: flags({ includingSunday: true }),
  });
}

test("no parking anytime is illegal", () => {
  const verdict = evaluateSegment(
    stacked([noParkingAnytime()]),
    moment("2026-09-14T10:00"),
    moment("2026-09-14T12:00"),
    EMPTY_CALENDAR,
  );

  assert.strictEqual(verdict.verdict, Verdict.ILLEGAL);
  assert.ok(verdict.reason.includes("no parking"));
});

test("no standing anytime also forbids parking", () => {
  const verdict = evaluateSegment(
    stacked([noStandingAnytime()]),
    moment("2026-09-14T10:00"),
    moment("2026-09-14T12:00"),
    EMPTY_CALENDAR,
  );

  assert.strictEqual(verdict.verdict, Verdict.ILLEGAL);
  assert.ok(verdict.reason.includes("no standing"));
});

test("the most restrictive prohibition is the one reported", () => {
  // Restrictiveness order, SPEC §9.2: no stopping > no standing > no parking.
  const stack = stacked([
    noParkingAnytime(),
    noStandingAnytime(),
    regulation({ action: Action.STOP, permitted: false }),
  ]);

  const outcome = resolveInterval(
    stack,
    new Interval(moment("2026-09-14T10:00"), moment("2026-09-14T11:00")),
    EMPTY_CALENDAR,
  );

  assert.strictEqual(outcome.prohibitingAction, Action.STOP);
  assert.ok(outcome.reason.includes("no stopping"));
});

test("stand outranks park when both apply", () => {
  const stack = stacked([noParkingAnytime(), noStandingAnytime()]);

  const outcome = resolveInterval(
    stack,
    new Interval(moment("2026-09-14T10:00"), moment("2026-09-14T11:00")),
    EMPTY_CALENDAR,
  );

  assert.strictEqual(outcome.prohibitingAction, Action.STAND);
});

test("a saturday meter window inside the posted hours is legal", () => {
  const verdict = evaluateSegment(
    stacked([twoHourMeterSaturday()]),
    moment("2026-09-19T10:00"),
    moment("2026-09-19T11:30"),
    EMPTY_CALENDAR,
  );

  assert.strictEqual(verdict.verdict, Verdict.LEGAL);
  assert.ok(verdict.metered);
  assert.strictEqual(verdict.chargedMinutes, 90);
  assert.strictEqual(verdict.maxDurationMin, 120);
});

test("a one hour meter fails a three hour window", () => {
  // SPEC §9.2: max_duration_min must cover the whole window.
  const verdict = evaluateSegment(
    stacked([oneHourMeterIncludingSunday()]),
    moment("2026-09-14T10:00"),
    moment("2026-09-14T13:00"),
    EMPTY_CALENDAR,
  );

  assert.strictEqual(verdict.verdict, Verdict.ILLEGAL);
  assert.ok(verdict.reason.includes("60 min"));
  assert.notStrictEqual(verdict.firstOffending, null);
});

test("a posted limit is measured over the hours it is in force for", () => {
  // 2 HMP 8AM-7PM, parked Sat 18:00-21:00: 60 of the 180 minutes are limited.
  // The sign says nothing about 7PM onwards, so the curb is unrestricted then
  // (34 RCNY 4-08) and the two-hour limit is not spent by sitting there.
  const verdict = evaluateSegment(
    stacked([twoHourMeterSaturday()]),
    moment("2026-09-19T18:00"),
    moment("2026-09-19T21:00"),
    EMPTY_CALENDAR,
  );

  assert.strictEqual(verdict.verdict, Verdict.LEGAL);
  assert.strictEqual(verdict.windowMinutes, 180);
  assert.strictEqual(verdict.chargedMinutes, 60);
});

test("a posted limit shorter than its own hours is still illegal", () => {
  const verdict = evaluateSegment(
    stacked([twoHourMeterSaturday()]),
    moment("2026-09-19T15:00"),
    moment("2026-09-19T21:00"),
    EMPTY_CALENDAR,
  );

  assert.strictEqual(verdict.verdict, Verdict.ILLEGAL);
  assert.ok(verdict.reason.includes("120 min"));
  assert.ok(verdict.reason.includes("240 min it is in force for"));
});

test("a limit that lapses and resumes counts both stretches against itself", () => {
  // Two separated limited stretches do not each get a fresh allowance. Summing
  // the limited minutes rather than taking the longest run is the conservative
  // reading: SPEC §8.6 makes a false "legal" the P0 defect.
  const morning = regulation({
    permitted: true,
    timeFrom: "08:00",
    timeTo: "10:00",
    maxDurationMin: 120,
  });
  const afternoon = { ...morning, timeFrom: "14:00", timeTo: "16:00" };

  const verdict = evaluateSegment(
    stacked([morning, afternoon]),
    moment("2026-09-19T08:00"),
    moment("2026-09-19T16:00"),
    EMPTY_CALENDAR,
  );

  assert.strictEqual(verdict.verdict, Verdict.ILLEGAL);
  assert.ok(verdict.reason.includes("240 min it is in force for"));
});

test("a window that runs past the posted hours is legal by absence after them", () => {
  const verdict = evaluateSegment(
    stacked([twoHourMeterSaturday()]),
    moment("2026-09-19T18:30"),
    moment("2026-09-19T20:00"),
    EMPTY_CALENDAR,
  );

  assert.strictEqual(verdict.verdict, Verdict.LEGAL);
  assert.strictEqual(verdict.chargedMinutes, 30);
  assert.ok(verdict.intervals.some((outcome) => outcome.byAbsence));
  assert.ok(verdict.caveats.join(" ").includes("no posted rule"));
});

test("a prohibition that covers part of the window disqualifies the segment", () => {
  const verdict = evaluateSegment(
    stacked([streetCleaningMonThu()]),
    moment("2026-09-14T08:00"),
    moment("2026-09-14T12:00"),
    EMPTY_CALENDAR,
  );

  assert.strictEqual(verdict.verdict, Verdict.ILLEGAL);
  assert.notStrictEqual(verdict.firstOffending, null);
  assert.strictEqual(wallKey(verdict.firstOffending.interval.start), "2026-09-14T09:00");
});

test("street cleaning on a suspension date leaves the block parkable", () => {
  // SPEC §9.3(c): ASP regs are suspended on calendar suspension dates.
  const calendar = new CalendarContext({ aspSuspensionDates: ["2026-09-14"] });

  const verdict = evaluateSegment(
    stacked([streetCleaningMonThu()]),
    moment("2026-09-14T09:00"),
    moment("2026-09-14T10:00"),
    calendar,
  );

  assert.strictEqual(verdict.verdict, Verdict.LEGAL);
  assert.ok(verdict.caveats.includes("Street cleaning is suspended on this date."));
});

test("a midnight wrapping prohibition catches the early morning side", () => {
  // SPEC §8.4 ex. 3: NO PARKING (BROOM) MONDAY THURSDAY MIDNIGHT-3AM.
  const overnight = regulation({
    days: [0, 3],
    timeFrom: "00:00",
    timeTo: "03:00",
    flags: flags({ streetCleaning: true }),
  });

  const verdict = evaluateSegment(
    stacked([overnight]),
    moment("2026-09-13T23:00"),
    moment("2026-09-14T01:00"),
    EMPTY_CALENDAR,
  );

  assert.strictEqual(verdict.verdict, Verdict.ILLEGAL);
  assert.notStrictEqual(verdict.firstOffending, null);
  assert.strictEqual(wallKey(verdict.firstOffending.interval.start), "2026-09-14T00:00");
});

test("a night regulation is legal after it lifts", () => {
  // SPEC §8.4 ex. 15: NIGHT REGULATION NO STANDING 8PM-6AM ALL DAYS.
  const night = regulation({
    action: Action.STAND,
    permitted: false,
    timeFrom: "20:00",
    timeTo: "06:00",
  });

  const illegal = evaluateSegment(
    stacked([night]),
    moment("2026-09-14T21:00"),
    moment("2026-09-15T01:00"),
    EMPTY_CALENDAR,
  );
  const legal = evaluateSegment(
    stacked([night]),
    moment("2026-09-15T07:00"),
    moment("2026-09-15T09:00"),
    EMPTY_CALENDAR,
  );

  assert.strictEqual(illegal.verdict, Verdict.ILLEGAL);
  assert.strictEqual(legal.verdict, Verdict.LEGAL);
});

test("a seasonal reservation only bites in season", () => {
  // SPEC §8.4 ex. 5: TRUCK FARMERS MARKET ONLY JUNE 1 - NOV 30 WEDNESDAY 8AM-4PM.
  const market = regulation({
    permitted: true,
    vehicleClass: VehicleClass.TRUCK,
    exclusive: true,
    days: [2],
    timeFrom: "08:00",
    timeTo: "16:00",
    effectiveFrom: "06-01",
    effectiveTo: "11-30",
  });

  const inSeason = evaluateSegment(
    stacked([market]),
    moment("2026-09-16T09:00"),
    moment("2026-09-16T11:00"),
    EMPTY_CALENDAR,
  );
  const outOfSeason = evaluateSegment(
    stacked([market]),
    moment("2026-12-16T09:00"),
    moment("2026-12-16T11:00"),
    EMPTY_CALENDAR,
  );

  assert.strictEqual(inSeason.verdict, Verdict.ILLEGAL);
  assert.ok(inSeason.reason.includes("reserved for truck"));
  assert.strictEqual(outOfSeason.verdict, Verdict.LEGAL);
});

test("commercial vehicles only is a prohibition for a passenger car", () => {
  // SPEC §8.4 ex. 14: 3 HOUR PARKING 9AM-6PM MON-FRI COMMERCIAL VEHICLES ONLY.
  const commercial = regulation({
    permitted: true,
    vehicleClass: VehicleClass.COMMERCIAL,
    exclusive: true,
    days: [0, 1, 2, 3, 4],
    timeFrom: "09:00",
    timeTo: "18:00",
    maxDurationMin: 180,
  });

  const verdict = evaluateSegment(
    stacked([commercial]),
    moment("2026-09-14T10:00"),
    moment("2026-09-14T11:00"),
    EMPTY_CALENDAR,
  );

  assert.strictEqual(verdict.verdict, Verdict.ILLEGAL);
});

test("an authorized vehicles only school sign prohibits us when school may be in", () => {
  // SPEC §8.4 ex. 6: AVO DEPT OF EDUCATION SCHOOL DAYS 7AM-4PM.
  const avo = regulation({
    permitted: true,
    vehicleClass: VehicleClass.AUTHORIZED,
    exclusive: true,
    days: [0, 1, 2, 3, 4],
    timeFrom: "07:00",
    timeTo: "16:00",
    flags: flags({ schoolDays: true }),
  });

  const verdict = evaluateSegment(
    stacked([avo]),
    moment("2026-09-14T08:00"),
    moment("2026-09-14T09:00"),
    EMPTY_CALENDAR,
  );

  assert.strictEqual(verdict.verdict, Verdict.ILLEGAL);
  assert.ok(verdict.caveats.includes("School-day rule assumed active."));
});

test("a school day rule lifts on a known non school day", () => {
  const avo = regulation({
    permitted: false,
    days: [0, 1, 2, 3, 4],
    timeFrom: "07:00",
    timeTo: "16:00",
    flags: flags({ schoolDays: true }),
  });
  const calendar = new CalendarContext({ nonSchoolDates: ["2026-09-14"] });

  const verdict = evaluateSegment(
    stacked([avo]),
    moment("2026-09-14T08:00"),
    moment("2026-09-14T09:00"),
    calendar,
  );

  assert.strictEqual(verdict.verdict, Verdict.LEGAL);
  assert.ok(!verdict.caveats.includes("School-day rule assumed active."));
});

test("a school day rule outside the window adds no caveat", () => {
  // A 7AM-4PM school rule says nothing about an evening search (W 24 St, seen live).
  const school = regulation({
    action: Action.STAND,
    permitted: false,
    days: [0, 1, 2, 3, 4],
    timeFrom: "07:00",
    timeTo: "16:00",
    flags: flags({ schoolDays: true }),
  });

  const verdict = evaluateSegment(
    stacked([school]),
    moment("2026-09-14T20:00"),
    moment("2026-09-14T22:00"),
    EMPTY_CALENDAR,
  );

  assert.strictEqual(verdict.verdict, Verdict.LEGAL);
  assert.ok(!verdict.caveats.includes("School-day rule assumed active."));
  assert.ok(
    verdict.caveats.includes("No posted rule is in effect during this window; read the curb."),
  );
});

test("a snow emergency rule only bites when one is declared", () => {
  // SPEC §8.4 ex. 13: NO STOPPING (SNOW EMERGENCY) ANYTIME.
  const snow = regulation({
    action: Action.STOP,
    permitted: false,
    flags: flags({ snowEmergency: true }),
  });

  const quiet = evaluateSegment(
    stacked([snow]),
    moment("2026-01-05T08:00"),
    moment("2026-01-05T09:00"),
    EMPTY_CALENDAR,
  );
  const declared = evaluateSegment(
    stacked([snow]),
    moment("2026-01-05T08:00"),
    moment("2026-01-05T09:00"),
    new CalendarContext({ snowEmergency: true }),
  );

  assert.strictEqual(quiet.verdict, Verdict.LEGAL);
  assert.ok(quiet.caveats.some((caveat) => caveat.toLowerCase().includes("snow emergency")));
  assert.strictEqual(declared.verdict, Verdict.ILLEGAL);
});

test("a sunday meter permits parking without charging", () => {
  // SPEC §9.3(b): meters are not in effect on Sundays.
  const sundayMeter = regulation({
    permitted: true,
    timeFrom: "09:00",
    timeTo: "19:00",
    metered: true,
    maxDurationMin: 120,
  });

  const verdict = evaluateSegment(
    stacked([sundayMeter]),
    moment("2026-09-20T10:00"),
    moment("2026-09-20T11:00"),
    EMPTY_CALENDAR,
  );

  assert.strictEqual(verdict.verdict, Verdict.LEGAL);
  assert.ok(verdict.metered);
  assert.strictEqual(verdict.chargedMinutes, 0);
  assert.ok(verdict.caveats.includes("Meters are not in effect for part of this window."));
});

test("an including sunday meter charges on sunday", () => {
  const verdict = evaluateSegment(
    stacked([oneHourMeterIncludingSunday()]),
    moment("2026-09-20T10:00"),
    moment("2026-09-20T11:00"),
    EMPTY_CALENDAR,
  );

  assert.strictEqual(verdict.verdict, Verdict.LEGAL);
  assert.strictEqual(verdict.chargedMinutes, 60);
  assert.strictEqual(wallKey(verdict.chargedIntervals[0].interval.start), "2026-09-20T10:00");
});

test("a major legal holiday frees the meter but keeps the seven day rules", () => {
  const calendar = new CalendarContext({
    aspSuspensionDates: ["2026-12-25"],
    majorHolidayDates: ["2026-12-25"],
    meterSuspendedDates: ["2026-12-25"],
  });
  const meter = regulation({ permitted: true, metered: true, maxDurationMin: 120 });

  const meteredVerdict = evaluateSegment(
    stacked([meter]),
    moment("2026-12-25T10:00"),
    moment("2026-12-25T11:00"),
    calendar,
  );
  const standingVerdict = evaluateSegment(
    stacked([noStandingAnytime()]),
    moment("2026-12-25T10:00"),
    moment("2026-12-25T11:00"),
    calendar,
  );

  assert.strictEqual(meteredVerdict.verdict, Verdict.LEGAL);
  assert.strictEqual(meteredVerdict.chargedMinutes, 0);
  assert.strictEqual(standingVerdict.verdict, Verdict.ILLEGAL);
});

test("a free time limited sign outside its hours is permitted by absence", () => {
  // SPEC §8.4 ex. 11: 2 HOUR PARKING 8AM-6PM EXCEPT SUNDAY, asked about a Sunday.
  const twoHour = regulation({
    permitted: true,
    days: [0, 1, 2, 3, 4, 5],
    timeFrom: "08:00",
    timeTo: "18:00",
    maxDurationMin: 120,
    flags: flags({ exceptSunday: true }),
  });

  const verdict = evaluateSegment(
    stacked([twoHour]),
    moment("2026-09-20T09:00"),
    moment("2026-09-20T15:00"),
    EMPTY_CALENDAR,
  );

  assert.strictEqual(verdict.verdict, Verdict.LEGAL);
  assert.ok(verdict.intervals.every((outcome) => outcome.byAbsence));
  assert.strictEqual(verdict.maxDurationMin, null);
});

test("a prohibition beats a permission in the same interval", () => {
  const stack = stacked([twoHourMeterSaturday(), noStandingAnytime()]);

  const verdict = evaluateSegment(
    stack,
    moment("2026-09-19T10:00"),
    moment("2026-09-19T11:00"),
    EMPTY_CALENDAR,
  );

  assert.strictEqual(verdict.verdict, Verdict.ILLEGAL);
});

test("the shortest posted limit governs a stack of permissions", () => {
  const stack = stacked([twoHourMeterSaturday(), oneHourMeterIncludingSunday()]);

  const verdict = evaluateSegment(
    stack,
    moment("2026-09-19T10:00"),
    moment("2026-09-19T10:45"),
    EMPTY_CALENDAR,
  );

  assert.strictEqual(verdict.verdict, Verdict.LEGAL);
  assert.strictEqual(verdict.maxDurationMin, 60);
});

test("an unparsed neighbour makes the whole segment ambiguous", () => {
  // SPEC §11: one unreadable sign poisons the block; never show a confident legal.
  const readable = stacked([twoHourMeterSaturday()]);
  const unreadable = stacked([noParkingAnytime()], {
    method: ParseMethod.UNPARSED,
    confidence: 0.0,
    raw: "NO PARKING (SOMETHING WE COULD NOT READ)",
  });

  const verdict = evaluateSegment(
    [...readable, ...unreadable],
    moment("2026-09-19T10:00"),
    moment("2026-09-19T11:00"),
    EMPTY_CALENDAR,
  );

  assert.strictEqual(verdict.verdict, Verdict.AMBIGUOUS);
  assert.ok(verdict.reason.includes("could not be read"));
});

test("a meta sign makes the segment ambiguous", () => {
  // SPEC §8.4 ex. 7: METERS ARE NOT IN EFFECT ABOVE TIMES modifies a sibling rule.
  const meta = regulation({ permitted: true, flags: flags({ meta: true }) });

  const verdict = evaluateSegment(
    [...stacked([twoHourMeterSaturday()]), ...stacked([meta])],
    moment("2026-09-19T10:00"),
    moment("2026-09-19T11:00"),
    EMPTY_CALENDAR,
  );

  assert.strictEqual(verdict.verdict, Verdict.AMBIGUOUS);
  assert.ok(verdict.reason.includes("modifies another sign"));
});

test("a low confidence parse makes the segment ambiguous", () => {
  const verdict = evaluateSegment(
    stacked([twoHourMeterSaturday()], { confidence: AMBIGUITY_THRESHOLD - 0.01 }),
    moment("2026-09-19T10:00"),
    moment("2026-09-19T11:00"),
    EMPTY_CALENDAR,
  );

  assert.strictEqual(verdict.verdict, Verdict.AMBIGUOUS);
  assert.ok(verdict.confidence < AMBIGUITY_THRESHOLD);
});

test("a parse exactly on the threshold is trusted", () => {
  const verdict = evaluateSegment(
    stacked([twoHourMeterSaturday()], { confidence: AMBIGUITY_THRESHOLD }),
    moment("2026-09-19T10:00"),
    moment("2026-09-19T11:00"),
    EMPTY_CALENDAR,
  );

  assert.strictEqual(verdict.verdict, Verdict.LEGAL);
});

test("ambiguity still reports the intervals and charged minutes", () => {
  // The cost module prices ambiguous segments too; the UI just colours them amber.
  const verdict = evaluateSegment(
    stacked([twoHourMeterSaturday()], { confidence: 0.5 }),
    moment("2026-09-19T10:00"),
    moment("2026-09-19T11:00"),
    EMPTY_CALENDAR,
  );

  assert.strictEqual(verdict.chargedMinutes, 60);
  assert.strictEqual(verdict.intervals.length, 1);
});

test("an empty stack is no data", () => {
  const verdict = evaluateSegment(
    [],
    moment("2026-09-14T10:00"),
    moment("2026-09-14T12:00"),
    EMPTY_CALENDAR,
  );

  assert.strictEqual(verdict.verdict, Verdict.NO_DATA);
  assert.strictEqual(verdict.windowMinutes, 120);
  assert.deepStrictEqual(verdict.intervals, []);
  assert.strictEqual(verdict.reason, UNKNOWN_GAP_REASON);
});

test("a stretch dot lists no signs for says so", () => {
  const verdict = evaluateSegment(
    [],
    moment("2026-09-14T10:00"),
    moment("2026-09-14T12:00"),
    EMPTY_CALENDAR,
    { gapKind: "no_signs" },
  );

  assert.strictEqual(verdict.reason, "NYC DOT lists no signs on this stretch");
  assert.deepStrictEqual(verdict.caveats, [NO_DATA_CAVEAT.no_signs]);
});

test("a stretch with signs we could not place is not called empty", () => {
  // DOT publishes signs for these 499 blockface-sides; "no sign data" is false there.
  const verdict = evaluateSegment(
    [],
    moment("2026-09-14T10:00"),
    moment("2026-09-14T12:00"),
    EMPTY_CALENDAR,
    { gapKind: "unmatched_signs" },
  );

  assert.strictEqual(verdict.reason, "Signs exist here that CurbCheck could not place");
  assert.ok(verdict.caveats[0].includes("could not place"));
  assert.ok(!verdict.reason.toLowerCase().includes("no sign"));
});

test("a temporary sign adds a caveat", () => {
  const temporary = regulation({ permitted: true, flags: flags({ temporary: true }) });

  const verdict = evaluateSegment(
    stacked([temporary]),
    moment("2026-09-14T10:00"),
    moment("2026-09-14T11:00"),
    EMPTY_CALENDAR,
  );

  assert.ok(verdict.caveats.includes("A sign here marks itself temporary."));
});

test("a rule for another class that is not exclusive is ignored", () => {
  // A truck loading rule says nothing about where a passenger car may park.
  const truckOnlyProhibition = regulation({
    action: Action.STAND,
    permitted: false,
    vehicleClass: VehicleClass.TRUCK,
  });

  const verdict = evaluateSegment(
    stacked([truckOnlyProhibition]),
    moment("2026-09-14T10:00"),
    moment("2026-09-14T11:00"),
    EMPTY_CALENDAR,
  );

  assert.strictEqual(verdict.verdict, Verdict.LEGAL);
  assert.ok(verdict.intervals.every((outcome) => outcome.byAbsence));
});

test("a stand permission does not authorise parking", () => {
  // A passenger loading zone permits standing only; parking there is still parking.
  const loading = regulation({
    action: Action.STAND,
    permitted: true,
    timeFrom: "08:00",
    timeTo: "18:00",
  });

  const verdict = evaluateSegment(
    stacked([loading]),
    moment("2026-09-14T10:00"),
    moment("2026-09-14T11:00"),
    EMPTY_CALENDAR,
  );

  assert.strictEqual(verdict.verdict, Verdict.LEGAL);
  assert.ok(verdict.intervals.every((outcome) => outcome.byAbsence));
});

test("no standing handicap bus stop is illegal for us", () => {
  // SPEC §8.4 ex. 4: NO STANDING (SINGLE ARROW) HANDICAP BUS W/4 ROUTES.
  const busStop = regulation({ action: Action.STAND, permitted: false });

  const verdict = evaluateSegment(
    stacked([busStop]),
    moment("2026-09-14T10:00"),
    moment("2026-09-14T11:00"),
    EMPTY_CALENDAR,
  );

  assert.strictEqual(verdict.verdict, Verdict.ILLEGAL);
});

// --- why a legal verdict is legal ----------------------------------------

test("a rule that permits the whole window gives a posted basis", () => {
  const verdict = evaluateSegment(
    stacked([twoHourMeterSaturday()]),
    moment("2026-09-19T10:00"),
    moment("2026-09-19T11:00"),
    EMPTY_CALENDAR,
  );

  assert.strictEqual(verdict.verdict, Verdict.LEGAL);
  assert.strictEqual(verdict.basis, VerdictBasis.POSTED);
});

test("a window no rule reaches gives an absence basis", () => {
  // Legal under 34 RCNY 4-08, but the engine read no permission (UX audit P0-1).
  const verdict = evaluateSegment(
    stacked([twoHourMeterSaturday()]),
    moment("2026-09-20T10:00"),
    moment("2026-09-20T11:00"),
    EMPTY_CALENDAR,
  );

  assert.strictEqual(verdict.verdict, Verdict.LEGAL);
  assert.strictEqual(verdict.basis, VerdictBasis.ABSENCE);
  assert.strictEqual(verdict.reason, ABSENCE_REASON);
});

test("one permitting interval is enough to make the basis posted", () => {
  // A window half inside the posted hours has a sign the driver can go and read.
  const verdict = evaluateSegment(
    stacked([twoHourMeterSaturday()]),
    moment("2026-09-19T18:30"),
    moment("2026-09-19T20:00"),
    EMPTY_CALENDAR,
  );

  assert.strictEqual(verdict.verdict, Verdict.LEGAL);
  assert.strictEqual(verdict.basis, VerdictBasis.POSTED);
});

test("a verdict that is not legal has no basis", () => {
  const illegal = evaluateSegment(
    stacked([noParkingAnytime()]),
    moment("2026-09-19T10:00"),
    moment("2026-09-19T11:00"),
    EMPTY_CALENDAR,
  );
  const noData = evaluateSegment(
    [],
    moment("2026-09-19T10:00"),
    moment("2026-09-19T11:00"),
    EMPTY_CALENDAR,
  );

  assert.strictEqual(illegal.basis, null);
  assert.strictEqual(noData.basis, null);
});

test("the deciding rule on an illegal span is the prohibition that bit", () => {
  // The panel names the sign on the pole, so the verdict has to carry which one.
  // Two rules are in force at 09:30 on a Monday: the street-cleaning ban and a
  // permission. The one a driver needs read back to them is the ban.
  const cleaning = streetCleaningMonThu();
  const permission = regulation({ permitted: true, timeFrom: "08:00", timeTo: "19:00" });
  const verdict = evaluateSegment(
    stacked([permission, cleaning]),
    moment("2026-09-14T09:00"),
    moment("2026-09-14T10:00"),
    EMPTY_CALENDAR,
  );

  assert.strictEqual(verdict.verdict, Verdict.ILLEGAL);
  assert.notStrictEqual(verdict.deciding, null);
  assert.strictEqual(verdict.deciding.regulation, cleaning);
});

test("the deciding rule on a posted legal span is the tightest permission", () => {
  // Two permissions, one limited: the limit is what can still make the stay illegal.
  const limited = twoHourMeterSaturday();
  const unlimited = regulation({ permitted: true, days: [5] });
  const verdict = evaluateSegment(
    stacked([unlimited, limited]),
    moment("2026-09-19T10:00"),
    moment("2026-09-19T11:00"),
    EMPTY_CALENDAR,
  );

  assert.strictEqual(verdict.basis, VerdictBasis.POSTED);
  assert.notStrictEqual(verdict.deciding, null);
  assert.strictEqual(verdict.deciding.regulation, limited);
});

test("a verdict with no rule in force names no rule", () => {
  // Absence, ambiguity and no data have nothing to quote, and must not invent
  // one. Naming a rule on an absence verdict would turn "nothing is in effect"
  // into "this sign says you may park", which is the P0-1 failure in one field.
  const absence = evaluateSegment(
    stacked([streetCleaningMonThu()]),
    moment("2026-09-14T14:00"),
    moment("2026-09-14T15:00"),
    EMPTY_CALENDAR,
  );
  const ambiguous = evaluateSegment(
    stacked([noParkingAnytime()], { method: ParseMethod.UNPARSED }),
    moment("2026-09-14T14:00"),
    moment("2026-09-14T15:00"),
    EMPTY_CALENDAR,
  );
  const noData = evaluateSegment(
    [],
    moment("2026-09-14T14:00"),
    moment("2026-09-14T15:00"),
    EMPTY_CALENDAR,
  );

  assert.strictEqual(absence.basis, VerdictBasis.ABSENCE);
  assert.strictEqual(absence.deciding, null);
  assert.strictEqual(ambiguous.verdict, Verdict.AMBIGUOUS);
  assert.strictEqual(ambiguous.deciding, null);
  assert.strictEqual(noData.deciding, null);
});

// --- model.js and labels.js ----------------------------------------------

/** One span with one regulation on it, in the shape `pack/loader.js` produces. */
function onePack() {
  return {
    spans: { n: 1, regSegId: ["3681:W:0"] },
    regulations: {
      n: 1,
      regId: ["3681:W:0:1"],
      span: Int32Array.from([0]),
      action: ["park"],
      permitted: Uint8Array.from([1]),
      vehicleClass: ["all"],
      exclusive: Uint8Array.from([0]),
      daysMask: Int32Array.from([0b0111111]),
      timeFrom: ["08:30"],
      timeTo: ["19:00"],
      metered: Uint8Array.from([1]),
      maxDurationMin: [120],
      flags: Int32Array.from([0b00000100]),
      effectiveFrom: [null],
      effectiveTo: [null],
      arrow: ["none"],
      rawSignDescription: ["2 HOUR METERED PARKING 8:30AM-7PM EXCEPT SUNDAY"],
      parseMethod: ["grammar"],
      parseConfidence: Float64Array.from([0.98]),
    },
  };
}

test("a packed regulation round-trips to the API shape the server returns", () => {
  const pack = onePack();

  assert.deepStrictEqual(regulationToJson(regulationFromPack(pack, 0)), {
    action: "park",
    permitted: true,
    vehicle_class: "all",
    exclusive: false,
    days: [0, 1, 2, 3, 4, 5],
    time_from: "08:30",
    time_to: "19:00",
    metered: true,
    max_duration_min: 120,
    flags: {
      street_cleaning: false,
      school_days: false,
      except_sunday: true,
      including_sunday: false,
      snow_emergency: false,
      holiday_exempt: false,
      temporary: false,
      meta: false,
    },
    effective_from: null,
    effective_to: null,
    arrow: "none",
  });
});

test("a packed regulation carries the provenance the verdict accounts for", () => {
  const meta = regulationWithMeta(onePack(), 0);

  assert.strictEqual(meta.parseMethod, ParseMethod.GRAMMAR);
  assert.strictEqual(meta.parseConfidence, 0.98);
  assert.strictEqual(meta.regSegId, "3681:W:0");
  assert.strictEqual(meta.rawSignDescription, "2 HOUR METERED PARKING 8:30AM-7PM EXCEPT SUNDAY");
});

test("an unreadable packed row is refused rather than guessed at", () => {
  const pack = onePack();
  pack.regulations.action = ["flying"];

  assert.throws(() => regulationFromPack(pack, 0), /unknown action/);
});

test("a span label names the street, the side and the cross streets", () => {
  const phrase = betweenPhrase("3 AVENUE", '["E  85 ST", "3 AVENUE"]', '["3 AVENUE", "E 86 ST"]');

  assert.strictEqual(phrase, "E 85 ST → E 86 ST");
  assert.strictEqual(spanLabel("3  AVENUE", "W", phrase), "3 AVENUE, west side, E 85 ST → E 86 ST");
  assert.strictEqual(spanLabel("3 AVENUE", null, null), "3 AVENUE");
  assert.strictEqual(singleSpaced("E  85   ST "), "E 85 ST");
});

test("a node that names only its own street gives no cross street", () => {
  assert.strictEqual(betweenPhrase("3 AVENUE", '["3 AVENUE"]', '["3 AVENUE"]'), null);
  assert.strictEqual(betweenPhrase("3 AVENUE", "not json", '["E 86 ST"]'), "at E 86 ST");
});
