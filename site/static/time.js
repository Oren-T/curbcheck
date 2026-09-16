/**
 * America/New_York wall clock <-> epoch milliseconds, with zoneinfo's gap and fold rules.
 *
 * `curbcheck/engine/window.py` leans on three `zoneinfo` behaviours that plain
 * epoch arithmetic gets wrong: midnight plus N minutes is wall-clock addition
 * and so skips the hour that does not exist; two aware datetimes in one zone
 * compare by wall clock with their offsets ignored; and elapsed minutes between
 * them do not. A **wall clock** here is
 * `{year, month, day, hour, minute, second, fold}` in America/New_York; an
 * **instant** is an epoch millisecond. This module is the only place in
 * `site/static/` that does wall-clock arithmetic with `Date`.
 */

const ZONE = "America/New_York";

const HOUR_MS = 3_600_000;
const DAY_MS = 86_400_000;

// English three-letter weekday names, Monday first to match model.Weekday.
// Hard-coded rather than taken from Intl so the text cannot move with a locale.
const WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

const ZONE_PARTS = new Intl.DateTimeFormat("en-US", {
  timeZone: ZONE,
  hourCycle: "h23",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
});

// Date, optional time, optional offset. Pydantic reads a bare "2026-09-15" as
// midnight and a "T"-less space separator as a separator, so both are accepted.
const ISO_PATTERN =
  /^(\d{4})-(\d{2})-(\d{2})(?:[Tt ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,9}))?)?)?(Z|z|[+-]\d{2}:?\d{2})?$/;

// The same window is expanded once per candidate span, so the same few hundred
// boundaries are resolved thousands of times in one search; each miss costs
// four Intl formats. Keyed by the full wall clock including fold.
const EPOCH_CACHE = new Map();
const EPOCH_CACHE_MAX = 4096;

export function wallFromEpoch(ms) {
  const civil = civilFromEpoch(ms);
  // The wall clock an hour earlier being the same one means this is the second
  // occurrence of it, which is what fold=1 means (PEP 495).
  const fold = sameCivil(civil, civilFromEpoch(ms - HOUR_MS)) ? 1 : 0;
  return { ...civil, fold };
}

export function epochFromWall(wall) {
  const key = cacheKey(wall);
  const cached = EPOCH_CACHE.get(key);
  if (cached !== undefined) {
    return cached;
  }
  const instant = resolveWall(wall);
  if (EPOCH_CACHE.size >= EPOCH_CACHE_MAX) {
    EPOCH_CACHE.clear();
  }
  EPOCH_CACHE.set(key, instant);
  return instant;
}

/** Minute-resolution identity of a wall clock. `fold` is deliberately not part of it. */
export function wallKey(wall) {
  return `${dateKey(wall)}T${pad2(wall.hour)}:${pad2(wall.minute)}`;
}

/** "YYYY-MM-DD", the key `CalendarContext` uses. */
export function dateKey(wall) {
  return `${pad4(wall.year)}-${pad2(wall.month)}-${pad2(wall.day)}`;
}

/**
 * Order two wall clocks, `fold` ignored.
 *
 * Python compares two aware datetimes that share a tzinfo by their naive
 * values, so a fall-back 01:30 EDT and 01:30 EST are equal to it. Sorting,
 * deduplicating and range-filtering boundaries in `expandWindow` all rest on
 * that, so the comparison has to be the same one.
 */
export function compareWall(left, right) {
  const a = [left.year, left.month, left.day, left.hour, left.minute, left.second];
  const b = [right.year, right.month, right.day, right.hour, right.minute, right.second];
  for (let i = 0; i < a.length; i += 1) {
    if (a[i] !== b[i]) {
      return a[i] < b[i] ? -1 : 1;
    }
  }
  return 0;
}

/** Monday is 0, matching `datetime.weekday()` and model.Weekday. */
export function weekdayOf(wall) {
  return weekdayOfDate(wall.year, wall.month, wall.day);
}

export function weekdayOfDate(year, month, day) {
  return (new Date(Date.UTC(year, month - 1, day)).getUTCDay() + 6) % 7;
}

/** The weekday of a "YYYY-MM-DD" key. */
export function weekdayOfDateKey(key) {
  return weekdayOfDate(Number(key.slice(0, 4)), Number(key.slice(5, 7)), Number(key.slice(8, 10)));
}

/**
 * Local wall-clock time on `day`, `fold: 0`. Minute 1440 is the next day's 00:00.
 *
 * `datetime(..., tzinfo=NYC_TZ) + timedelta(minutes=n)` is arithmetic on the
 * naive value, so the hour that does not exist on the spring-forward Sunday is
 * still counted here and disappears only when the wall clock is resolved to an
 * instant.
 */
export function atMinute(year, month, day, minuteOfDay) {
  return civilFromNaiveMs(Date.UTC(year, month - 1, day) + minuteOfDay * 60_000, 0);
}

/** The calendar date after `{year, month, day}`. */
export function nextDate({ year, month, day }) {
  const next = new Date(Date.UTC(year, month - 1, day) + DAY_MS);
  return {
    year: next.getUTCFullYear(),
    month: next.getUTCMonth() + 1,
    day: next.getUTCDate(),
  };
}

/**
 * Parse an ISO 8601 request time to a New York wall clock.
 *
 * No offset means that wall clock with `fold: 0` -- the server reads a naive
 * time as local, because every parking sign states local time
 * (`api/schemas.py::_in_new_york`). An offset or `Z` names an instant, which is
 * then converted. Anything else is a `validation_error` for the caller to
 * raise; seconds are kept because the window boundaries are compared on them.
 */
export function parseRequestTime(text) {
  const match = ISO_PATTERN.exec(String(text).trim());
  if (match === null) {
    throw new Error("must be an ISO 8601 date-time");
  }
  const [, year, month, day, hour, minute, second, , offset] = match;
  const wall = {
    year: Number(year),
    month: Number(month),
    day: Number(day),
    hour: Number(hour ?? 0),
    minute: Number(minute ?? 0),
    second: Number(second ?? 0),
    fold: 0,
  };
  if (!isRealCivilDate(wall)) {
    throw new Error("must be an ISO 8601 date-time");
  }
  if (offset === undefined) {
    return wall;
  }
  return wallFromEpoch(asUtcMs(wall) - offsetTextToMs(offset));
}

/** `"%a %H:%M-%H:%M"` of two wall clocks, the format `resolve._when` writes. */
export function formatWhen(start, end) {
  const startText = `${pad2(start.hour)}:${pad2(start.minute)}`;
  const endText = `${pad2(end.hour)}:${pad2(end.minute)}`;
  return `${WEEKDAY_NAMES[weekdayOf(start)]} ${startText}-${endText}`;
}

/** Whether `value` has the shape this module produces. */
export function isWallClock(value) {
  if (value === null || typeof value !== "object") {
    return false;
  }
  const fields = ["year", "month", "day", "hour", "minute", "second", "fold"];
  return fields.every((field) => Number.isInteger(value[field]));
}

function resolveWall(wall) {
  const naive = asUtcMs(wall);
  // The offsets a day either side of the wall clock: one transition can fall
  // between them, and no zone has two in 48 hours.
  const before = zoneOffsetMs(naive - DAY_MS);
  const after = zoneOffsetMs(naive + DAY_MS);
  const candidates = [];
  for (const offset of before === after ? [before] : [before, after]) {
    const instant = naive - offset;
    if (sameCivil(civilFromEpoch(instant), wall)) {
      candidates.push(instant);
    }
  }
  if (candidates.length === 1) {
    return candidates[0];
  }
  if (candidates.length > 1) {
    // Fall back: the same wall clock happens twice, and fold picks which.
    return wall.fold ? Math.max(...candidates) : Math.min(...candidates);
  }
  // Spring forward: the wall clock never happens. PEP 495 resolves it with the
  // offset in force before the transition for fold=0 and after it for fold=1,
  // which is why 02:30 on 2026-03-08 is 07:30 UTC and not 06:30.
  return naive - (wall.fold ? after : before);
}

function civilFromEpoch(ms) {
  const fields = {};
  for (const part of ZONE_PARTS.formatToParts(ms)) {
    if (part.type !== "literal") {
      fields[part.type] = part.value;
    }
  }
  return {
    year: Number(fields.year),
    month: Number(fields.month),
    day: Number(fields.day),
    // Some ICU builds render midnight as hour 24 of the same date even under
    // hourCycle "h23"; the date part is already the one we want either way.
    hour: Number(fields.hour) % 24,
    minute: Number(fields.minute),
    second: Number(fields.second),
  };
}

function civilFromNaiveMs(ms, fold) {
  const moment = new Date(ms);
  return {
    year: moment.getUTCFullYear(),
    month: moment.getUTCMonth() + 1,
    day: moment.getUTCDate(),
    hour: moment.getUTCHours(),
    minute: moment.getUTCMinutes(),
    second: moment.getUTCSeconds(),
    fold,
  };
}

function zoneOffsetMs(ms) {
  return asUtcMs(civilFromEpoch(ms)) - ms;
}

function asUtcMs(civil) {
  return Date.UTC(civil.year, civil.month - 1, civil.day, civil.hour, civil.minute, civil.second);
}

function sameCivil(left, right) {
  return (
    left.year === right.year &&
    left.month === right.month &&
    left.day === right.day &&
    left.hour === right.hour &&
    left.minute === right.minute &&
    left.second === right.second
  );
}

function isRealCivilDate(wall) {
  const roundTrip = new Date(asUtcMs(wall));
  return (
    roundTrip.getUTCFullYear() === wall.year &&
    roundTrip.getUTCMonth() + 1 === wall.month &&
    roundTrip.getUTCDate() === wall.day &&
    wall.hour < 24 &&
    wall.minute < 60 &&
    wall.second < 60
  );
}

function offsetTextToMs(text) {
  if (text === "Z" || text === "z") {
    return 0;
  }
  const sign = text[0] === "-" ? -1 : 1;
  const digits = text.slice(1).replace(":", "");
  const hours = Number(digits.slice(0, 2));
  const minutes = Number(digits.slice(2, 4));
  return sign * (hours * 60 + minutes) * 60_000;
}

function cacheKey(wall) {
  return `${wallKey(wall)}:${pad2(wall.second)}:${wall.fold}`;
}

function pad2(value) {
  return String(value).padStart(2, "0");
}

function pad4(value) {
  return String(value).padStart(4, "0");
}
