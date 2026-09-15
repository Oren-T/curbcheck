/**
 * Split a requested parking window into sub-intervals and decide which rules are in force.
 *
 * A rule's truth value only changes at midnight, at one of its own time
 * boundaries, or at a calendar boundary (which is a date, hence a midnight). So
 * we cut [T1, T2] at every distinct boundary the stack under evaluation can
 * produce and evaluate each piece once. SPEC §9.2 and §9.3.
 *
 * Port of `curbcheck/engine/window.py`; its `CalendarContext` lives in
 * `calendar.js`. Boundaries are New York wall clocks (`../time.js`), because
 * that is what Python compares when two aware datetimes share a tzinfo.
 */

import {
  atMinute,
  compareWall,
  dateKey,
  epochFromWall,
  isWallClock,
  nextDate,
  weekdayOf,
} from "../time.js";

export const MINUTES_PER_DAY = 24 * 60;

/** A half-open [start, end) slice of the requested window, never crossing midnight. */
export class Interval {
  constructor(start, end) {
    this.start = start;
    this.end = end;
    this.startMs = epochFromWall(start);
    this.endMs = epochFromWall(end);
    Object.freeze(this);
  }

  /** Monday is 0, matching model.Weekday. */
  get weekday() {
    return weekdayOf(this.start);
  }

  /** "YYYY-MM-DD", the key `CalendarContext` is looked up by. */
  get date() {
    return dateKey(this.start);
  }

  /**
   * Elapsed real minutes. Subtracting two wall clocks in one zone would count
   * wall-clock minutes, so the hour that does not happen on the spring-forward
   * Sunday would be billed.
   */
  get minutes() {
    return roundHalfEven((this.endMs - this.startMs) / 60_000);
  }

  get startMinuteOfDay() {
    return this.start.hour * 60 + this.start.minute;
  }
}

/**
 * Cut [t1, t2) at midnights and at every rule boundary in `regulations`.
 *
 * Both ends are New York wall clocks; parking rules are stated in local time,
 * and `time.parseRequestTime` has already resolved any offset the caller gave.
 * A window of zero or negative length throws.
 */
export function expandWindow(t1, t2, regulations = []) {
  const start = asNyc(t1, "t1");
  const end = asNyc(t2, "t2");
  if (compareWall(end, start) <= 0) {
    throw new Error("the parking window must end after it starts");
  }

  const ruleMinutes = ruleBoundaryMinutes(regulations);
  // Python collects these in a set of aware datetimes, which deduplicates and
  // orders them by wall clock with `fold` ignored, and keeps the first of any
  // two that compare equal. Insertion order therefore matters: a t1 that
  // arrived as fold=1 has to survive a midnight boundary written as fold=0.
  const boundaries = [start, end];
  for (const day of datesCovered(start, end)) {
    for (const minute of ruleMinutes) {
      boundaries.push(atMinute(day.year, day.month, day.day, minute));
    }
    boundaries.push(atMinute(day.year, day.month, day.day, MINUTES_PER_DAY));
  }

  const inWindow = boundaries.filter(
    (moment) => compareWall(start, moment) <= 0 && compareWall(moment, end) <= 0,
  );
  const ordered = firstOfEachRun(stableSortByWall(inWindow));
  const intervals = [];
  for (let i = 0; i + 1 < ordered.length; i += 1) {
    intervals.push(new Interval(ordered[i], ordered[i + 1]));
  }
  return intervals;
}

/**
 * Whether the rule is in force for the whole of `interval`.
 *
 * Assumes `interval` came from `expandWindow` with this rule in the stack, so
 * the rule cannot switch on or off part-way through it.
 */
export function ruleIsActive(reg, interval, calendar) {
  if (!reg.days.includes(interval.weekday)) {
    return false;
  }
  if (reg.flags.exceptSunday && interval.weekday === 6) {
    return false;
  }
  if (!inSeason(reg, interval.date)) {
    return false;
  }
  if (!inTimeRange(reg, interval)) {
    return false;
  }
  return calendarAllows(reg, interval.date, calendar);
}

/**
 * Whether a metered rule actually costs money in this interval.
 *
 * Sundays and Major Legal Holidays are free (SPEC §9.3), except where the sign
 * says INCLUDING SUNDAY (§8.4 ex. 10). We read that flag as overriding the
 * holiday exemption too: the sign is claiming its own calendar.
 */
export function meterIsCharged(reg, interval, calendar) {
  if (!reg.metered) {
    return false;
  }
  if (reg.flags.includingSunday) {
    return true;
  }
  return calendar.metersInEffect(interval.date);
}

function calendarAllows(reg, day, calendar) {
  if (reg.flags.streetCleaning && calendar.isAspSuspended(day)) {
    return false;
  }
  if (reg.flags.holidayExempt && calendar.isMajorHoliday(day)) {
    return false;
  }
  if (reg.flags.snowEmergency && !calendar.snowEmergency) {
    return false;
  }
  return !(reg.flags.schoolDays && calendar.isKnownNonSchoolDay(day));
}

function ruleBoundaryMinutes(regulations) {
  const minutes = new Set([0]);
  for (const reg of regulations) {
    for (const value of [reg.timeFrom, reg.timeTo]) {
      if (value !== null) {
        minutes.add(hhmmToMinutes(value));
      }
    }
  }
  // Python iterates an unordered set here; every distinct minute-of-day gives a
  // distinct wall clock on a given day, so no two of them can tie and the order
  // never reaches the answer. Sorted anyway so the boundary list is readable.
  return [...minutes].sort((a, b) => a - b);
}

function datesCovered(start, end) {
  const days = [];
  let day = { year: start.year, month: start.month, day: start.day };
  const last = { year: end.year, month: end.month, day: end.day };
  while (compareDate(day, last) <= 0) {
    days.push(day);
    day = nextDate(day);
  }
  return days;
}

function asNyc(moment, name) {
  // `_as_nyc` converts an aware datetime and rejects a naive one. Here every
  // instant has already been resolved to a New York wall clock, so this only
  // rejects the shape `expand_window` would have raised on.
  if (!isWallClock(moment)) {
    throw new Error(`${name} must be timezone-aware; parking rules are local time`);
  }
  return moment;
}

export function hhmmToMinutes(value) {
  return Number(value.slice(0, 2)) * 60 + Number(value.slice(3));
}

function inTimeRange(reg, interval) {
  if (reg.timeFrom === null || reg.timeTo === null) {
    return true;
  }
  const start = hhmmToMinutes(reg.timeFrom);
  const stop = hhmmToMinutes(reg.timeTo);
  const minute = interval.startMinuteOfDay;
  if (stop > start) {
    return start <= minute && minute < stop;
  }
  // time_to <= time_from wraps past midnight ("8PM-6AM", SPEC §8.4 ex. 15).
  // The equal case is how an all-day range written as 12AM-12AM arrives.
  return minute >= start || minute < stop;
}

/** Seasonal MM-DD bounds are inclusive on both ends and may wrap the year end. */
function inSeason(reg, day) {
  if (reg.effectiveFrom === null || reg.effectiveTo === null) {
    return true;
  }
  const today = [Number(day.slice(5, 7)), Number(day.slice(8, 10))];
  const seasonStart = mmdd(reg.effectiveFrom);
  const seasonEnd = mmdd(reg.effectiveTo);
  if (comparePair(seasonStart, seasonEnd) <= 0) {
    return comparePair(seasonStart, today) <= 0 && comparePair(today, seasonEnd) <= 0;
  }
  return comparePair(today, seasonStart) >= 0 || comparePair(today, seasonEnd) <= 0;
}

function mmdd(value) {
  return [Number(value.slice(0, 2)), Number(value.slice(3))];
}

function comparePair([leftMonth, leftDay], [rightMonth, rightDay]) {
  if (leftMonth !== rightMonth) {
    return leftMonth < rightMonth ? -1 : 1;
  }
  if (leftDay !== rightDay) {
    return leftDay < rightDay ? -1 : 1;
  }
  return 0;
}

function compareDate(left, right) {
  if (left.year !== right.year) {
    return left.year < right.year ? -1 : 1;
  }
  return comparePair([left.month, left.day], [right.month, right.day]);
}

function stableSortByWall(moments) {
  // Array.prototype.sort has been stable since ES2019, so equal wall clocks
  // keep the order they were pushed in and the tie goes to the first insert.
  return [...moments].sort(compareWall);
}

function firstOfEachRun(sorted) {
  return sorted.filter(
    (moment, index) => index === 0 || compareWall(sorted[index - 1], moment) < 0,
  );
}

/**
 * Python's `round()` is round-half-to-even, so a window boundary on a half
 * minute would otherwise come out a minute longer here than in the reference.
 */
export function roundHalfEven(value) {
  const floor = Math.floor(value);
  const fraction = value - floor;
  if (fraction > 0.5) {
    return floor + 1;
  }
  if (fraction < 0.5) {
    return floor;
  }
  return floor % 2 === 0 ? floor : floor + 1;
}
