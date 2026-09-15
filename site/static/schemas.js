/**
 * Validation for everything that arrives on the worker's message port.
 *
 * Mirrors `curbcheck/api/schemas.py` and the `Query(...)` bounds on the GET
 * routes: same bounds, same window limits, unknown fields rejected, so a typo in
 * a field name is an error rather than a silently ignored setting. Messages
 * match the server's where the server's are one sentence; the differential
 * harness compares codes, not messages.
 *
 * Every failure is a `HandlerError`, which is the one error shape the worker
 * hands back (docs/API.md).
 */

import { DEFAULT_LIMIT, DEFAULT_MAP_LIMIT, MAX_MAP_LIMIT } from "./engine/search.js";
import { MAX_QUERY_CHARS } from "./geocode/query.js";
import { HandlerError } from "./handlers.js";
import { parseRequestTime } from "./time.js";

// A Manhattan-ish bounding box, generous at every edge: Battery Park to Inwood,
// the Hudson to the East River. An input sanity check, not a service area --
// the pack only holds Manhattan, so a point in the corner of the box simply
// returns nothing, and `engine/coverage.js` is the narrower test.
export const MIN_LAT = 40.68;
export const MAX_LAT = 40.9;
export const MIN_LON = -74.05;
export const MAX_LON = -73.88;

// A window shorter than this is not a parking decision, and one longer than a
// day would make the per-day expansion in engine/window.js unbounded work.
export const MIN_WINDOW_MS = 5 * 60 * 1000;
export const MAX_WINDOW_MS = 24 * 60 * 60 * 1000;

// Ids the ETL mints are 16 hex characters (`segments._reg_seg_id`). The charset
// is wider than that so an older snapshot's ids still resolve; anything outside
// it cannot be a real id, so it is rejected before a lookup is made.
export const REG_SEG_ID_PATTERN = /^[A-Za-z0-9:_.-]{1,80}$/;

const SEARCH_FIELDS = new Set([
  "lat",
  "lon",
  "address",
  "t1",
  "t2",
  "walk_minutes",
  "weights",
  "limit",
  "map_limit",
]);
const WEIGHT_FIELDS = new Set(["walk", "money", "risk"]);

// The three ranking sliders (SPEC §9.4). The upper bound keeps one from dwarfing
// the score; the defaults read as "one minute of walking is worth one dollar".
const WEIGHT_DEFAULTS = { walk: 1.0, money: 1.0, risk: 0.5 };
const MAX_WEIGHT = 10.0;

const DEFAULT_WALK_MINUTES = 10.0;
const MIN_WALK_MINUTES = 1.0;
const MAX_WALK_MINUTES = 30.0;
const MAX_LIMIT = 500;

/**
 * The `POST /api/search` body: a destination, a window, and how far the user
 * will walk.
 *
 * @returns {{lat: number|null, lon: number|null, address: string|null, t1: object,
 *   t2: object, walkMinutes: number, weights: object, limit: number, mapLimit: number}}
 */
export function validateSearchRequest(body) {
  const fields = requireObject(body, "body");
  rejectUnknown(fields, SEARCH_FIELDS);

  const lat = optionalNumber(fields.lat, "lat", MIN_LAT, MAX_LAT);
  const lon = optionalNumber(fields.lon, "lon", MIN_LON, MAX_LON);
  const address = optionalText(fields.address, "address", MAX_QUERY_CHARS);
  const t1 = parseTime(requirePresent(fields.t1, "t1"), "t1");
  const t2 = parseTime(requirePresent(fields.t2, "t2"), "t2");
  const walkMinutes = numberWithDefault(
    fields.walk_minutes,
    "walk_minutes",
    MIN_WALK_MINUTES,
    MAX_WALK_MINUTES,
    DEFAULT_WALK_MINUTES,
  );
  const weights = validateWeights(fields.weights);
  // `limit` caps the ranked legal list the user reads; `map_limit` caps
  // everything else the map draws (docs/VALIDATION.md U1).
  const limit = integerWithDefault(fields.limit, "limit", 1, MAX_LIMIT, DEFAULT_LIMIT);
  const mapLimit = integerWithDefault(
    fields.map_limit,
    "map_limit",
    1,
    MAX_MAP_LIMIT,
    DEFAULT_MAP_LIMIT,
  );

  if ((lat === null) !== (lon === null)) {
    throw invalid("lat and lon must be given together");
  }
  const hasPoint = lat !== null;
  if (hasPoint && address !== null) {
    throw invalid("give either lat and lon or address, not both");
  }
  if (!hasPoint && address === null) {
    throw invalid("give a destination: either lat and lon, or address");
  }

  const span = wallSpanMs(t1, t2);
  if (span < MIN_WINDOW_MS) {
    throw invalid("the parking window must be at least 5 minutes and end after it starts");
  }
  if (span > MAX_WINDOW_MS) {
    throw invalid("the parking window must be 24 hours or less");
  }

  return { lat, lon, address, t1, t2, walkMinutes, weights, limit, mapLimit };
}

/** `GET /api/geocode?q=` — 1 to `MAX_QUERY_CHARS` characters. */
export function validateGeocodeArgs(args) {
  const fields = requireObject(args, "query");
  const q = fields.q;
  if (typeof q !== "string" || q.length < 1) {
    throw invalid("q: give something to look up");
  }
  if (q.length > MAX_QUERY_CHARS) {
    throw invalid(`q: at most ${MAX_QUERY_CHARS} characters`);
  }
  return q;
}

/** `GET /api/reverse?lat=&lon=` — both required, both inside the input box. */
export function validateReverseArgs(args) {
  const fields = requireObject(args, "query");
  return {
    lat: requireNumber(fields.lat, "lat", MIN_LAT, MAX_LAT),
    lon: requireNumber(fields.lon, "lon", MIN_LON, MAX_LON),
  };
}

/**
 * The `reg_seg_id` path parameter. A bad one is `invalid_request` and never
 * reaches a lookup, which is the server's 400 rather than its 422.
 */
export function validateRegSegId(value) {
  if (typeof value !== "string" || !REG_SEG_ID_PATTERN.test(value)) {
    throw new HandlerError("invalid_request", "not a segment id");
  }
  return value;
}

/**
 * How long the window is, the way CPython measures it.
 *
 * `t2 - t1` in the Python is a subtraction of two aware datetimes that carry the
 * *same* `ZoneInfo` object -- every request time goes through
 * `SearchRequest._in_new_york`, which either `replace`s or `astimezone`s to the
 * one `config.NYC_TZ` -- and `datetime.__sub__` skips the offset correction when
 * `self._tzinfo is other._tzinfo`. So the window is measured on the wall clock,
 * not on the instants: midnight to midnight across the fall-back Sunday is 24
 * hours here and 25 hours of real time, and the server accepts it.
 */
export function wallSpanMs(from, to) {
  return (
    Date.UTC(to.year, to.month - 1, to.day, to.hour, to.minute, to.second) -
    Date.UTC(from.year, from.month - 1, from.day, from.hour, from.minute, from.second)
  );
}

/**
 * An ISO 8601 time as a New York wall clock. A naive value is local, because
 * every parking sign states local time.
 */
export function parseTime(value, field) {
  if (typeof value !== "string") {
    throw invalid(`${field}: give an ISO 8601 date and time`);
  }
  try {
    return parseRequestTime(value);
  } catch {
    throw invalid(`${field}: give an ISO 8601 date and time`);
  }
}

function validateWeights(value) {
  if (value === undefined || value === null) {
    return { ...WEIGHT_DEFAULTS };
  }
  const fields = requireObject(value, "weights");
  rejectUnknown(fields, WEIGHT_FIELDS, "weights");
  const weights = {};
  for (const name of WEIGHT_FIELDS) {
    weights[name] = numberWithDefault(
      fields[name],
      `weights.${name}`,
      0.0,
      MAX_WEIGHT,
      WEIGHT_DEFAULTS[name],
    );
  }
  return weights;
}

function requireObject(value, what) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw invalid(`${what} must be an object`);
  }
  return value;
}

function rejectUnknown(fields, allowed, prefix = "") {
  for (const name of Object.keys(fields)) {
    if (!allowed.has(name)) {
      throw invalid(`${prefix ? `${prefix}.` : ""}${name}: unknown field`);
    }
  }
}

function requirePresent(value, field) {
  if (value === undefined || value === null) {
    throw invalid(`${field}: this field is required`);
  }
  return value;
}

function optionalNumber(value, field, low, high) {
  if (value === undefined || value === null) {
    return null;
  }
  return requireNumber(value, field, low, high);
}

function requireNumber(value, field, low, high) {
  // `allow_inf_nan=False`: NaN and infinity are not coordinates, and a string
  // that looks like one is the caller sending the wrong type.
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw invalid(`${field}: give a number`);
  }
  if (value < low || value > high) {
    throw invalid(`${field}: must be between ${low} and ${high}`);
  }
  return value;
}

function numberWithDefault(value, field, low, high, fallback) {
  if (value === undefined || value === null) {
    return fallback;
  }
  return requireNumber(value, field, low, high);
}

function integerWithDefault(value, field, low, high, fallback) {
  if (value === undefined || value === null) {
    return fallback;
  }
  if (typeof value !== "number" || !Number.isInteger(value)) {
    throw invalid(`${field}: give a whole number`);
  }
  if (value < low || value > high) {
    throw invalid(`${field}: must be between ${low} and ${high}`);
  }
  return value;
}

function optionalText(value, field, maxChars) {
  if (value === undefined || value === null) {
    return null;
  }
  if (typeof value !== "string") {
    throw invalid(`${field}: give text`);
  }
  if (value.length < 1 || value.length > maxChars) {
    throw invalid(`${field}: between 1 and ${maxChars} characters`);
  }
  return value;
}

function invalid(message) {
  return new HandlerError("validation_error", message);
}
