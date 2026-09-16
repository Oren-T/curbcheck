/**
 * The dates that change what a sign means, as known offline.
 *
 * Port of `CalendarContext` in `curbcheck/engine/window.py` plus
 * `engine.search.load_calendar` and `engine.search.calendar_is_missing`, which
 * are the two functions that fill it from the database. Dates are
 * "YYYY-MM-DD" strings here, which is what `Interval.date` produces.
 */

import { weekdayOfDateKey } from "../time.js";

const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

export class CalendarContext {
  /**
   * Built from the `asp_suspension` table plus whatever the caller knows about
   * school closures. Emergency suspensions are unknowable offline (SPEC §11),
   * so an empty context means "nothing special known", never "nothing is
   * happening".
   */
  constructor({
    aspSuspensionDates = [],
    majorHolidayDates = [],
    meterSuspendedDates = [],
    // null means "we have no school calendar"; a school-days rule is then
    // assumed active, because assuming it off would risk a false "legal"
    // (SPEC §11).
    nonSchoolDates = null,
    snowEmergency = false,
    labels = new Map(),
    // True when this database has no ASP/holiday calendar at all. Every verdict
    // that could turn on a holiday or a suspension is then a guess, and says so.
    calendarMissing = false,
  } = {}) {
    this.aspSuspensionDates = new Set(aspSuspensionDates);
    this.majorHolidayDates = new Set(majorHolidayDates);
    this.meterSuspendedDates = new Set(meterSuspendedDates);
    this.nonSchoolDates = nonSchoolDates === null ? null : new Set(nonSchoolDates);
    this.snowEmergency = snowEmergency;
    this.labels = labels instanceof Map ? labels : new Map(Object.entries(labels));
    this.calendarMissing = calendarMissing;
    Object.freeze(this);
  }

  /**
   * Build from `asp_suspension` rows (`date`, `is_major_legal_holiday`,
   * `meters_suspended`, `label`), whose keys are the SQLite column names.
   */
  static fromAspRows(
    rows,
    { nonSchoolDates = null, snowEmergency = false, calendarMissing = false } = {},
  ) {
    const suspensions = new Set();
    const holidays = new Set();
    const metersOff = new Set();
    const labels = new Map();
    for (const row of rows) {
      const day = asDateKey(row.date);
      suspensions.add(day);
      if (row.is_major_legal_holiday) {
        holidays.add(day);
      }
      if (row.meters_suspended) {
        metersOff.add(day);
      }
      const label = row.label === null || row.label === undefined ? "" : String(row.label);
      if (label) {
        labels.set(day, label);
      }
    }
    return new CalendarContext({
      aspSuspensionDates: suspensions,
      majorHolidayDates: holidays,
      meterSuspendedDates: metersOff,
      nonSchoolDates,
      snowEmergency,
      labels,
      calendarMissing,
    });
  }

  isAspSuspended(day) {
    return this.aspSuspensionDates.has(day);
  }

  isMajorHoliday(day) {
    return this.majorHolidayDates.has(day);
  }

  isKnownNonSchoolDay(day) {
    return this.nonSchoolDates !== null && this.nonSchoolDates.has(day);
  }

  /** Meters run every day except Sundays and the six Major Legal Holidays (SPEC §13.2). */
  metersInEffect(day) {
    if (weekdayOfDateKey(day) === 6) {
      return false;
    }
    if (this.meterSuspendedDates.has(day)) {
      return false;
    }
    return !this.isMajorHoliday(day);
  }
}

/**
 * Build the calendar from `pack.calendar`, the compiled `asp_suspension` table.
 *
 * A pack with no calendar produces a context that says so, and every verdict
 * resolved against it carries the caveat: without the calendar a holiday reads
 * as an ordinary day and a street-cleaning ban that the city suspended still
 * reads as in force (SPEC §11).
 */
export function loadCalendar(pack, { nonSchoolDates = null, snowEmergency = false } = {}) {
  const rows = aspRows(pack);
  return CalendarContext.fromAspRows(rows, {
    nonSchoolDates,
    snowEmergency,
    calendarMissing: rows.length === 0 || metaSaysCalendarMissing(pack),
  });
}

/**
 * Whether this pack has no usable ASP and holiday calendar.
 *
 * Two signals meaning one thing: the ETL recorded that it found no calendar
 * file, or the table is empty. `health()` reports `degraded` on either, because
 * a sync that quietly dropped the calendar otherwise looks exactly like a good
 * one.
 */
export function calendarIsMissing(pack) {
  if (metaSaysCalendarMissing(pack)) {
    return true;
  }
  return pack.calendar.n === 0;
}

function aspRows(pack) {
  // `_ASP_SQL`: every column of `asp_suspension`, no ORDER BY, and the order
  // does not reach the answer -- the rows only fill four sets and a map.
  const table = pack.calendar;
  const rows = [];
  for (let row = 0; row < table.n; row += 1) {
    rows.push({
      date: table.date[row],
      is_major_legal_holiday: table.isMajorLegalHoliday[row],
      meters_suspended: table.metersSuspended[row],
      label: table.label[row],
    });
  }
  return rows;
}

/**
 * `_meta_says_calendar_missing`: the `calendar_missing` sync_meta row, which the
 * pack carries in `meta.json`. Read as text so that both the compiler's boolean
 * and the raw `"0"`/`"false"` the ETL stores mean the same thing.
 */
function metaSaysCalendarMissing(pack) {
  const value = pack.meta?.calendar_missing;
  if (value === null || value === undefined) {
    return false;
  }
  return !["", "0", "false", "no"].includes(String(value).trim().toLowerCase());
}

function asDateKey(value) {
  const text = String(value);
  if (!DATE_PATTERN.test(text)) {
    throw new Error("calendar rows must carry an ISO date");
  }
  return text;
}
