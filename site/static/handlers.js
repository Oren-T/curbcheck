/**
 * The six operations of `curbcheck/api/routes.py`, as pure functions over a
 * loaded pack.
 *
 * `worker.js` moves messages onto these and nothing else; the differential
 * harness calls them directly. Text coming out of them is returned exactly as
 * the ETL stored it, including sign descriptions that contain HTML: the
 * frontend is the single escaping boundary (STYLE_GUIDE §4, docs/API.md).
 */

import { calendarIsMissing } from "./engine/calendar.js";
import { COVERAGE_AREA, coverageBbox, withinCoverage } from "./engine/coverage.js";
import {
  ParseMethod,
  jsonStringList,
  regulationToJson,
  regulationWithMeta,
} from "./engine/model.js";
import { evaluateSegment } from "./engine/resolve.js";
import { search } from "./engine/search.js";
import { blockfaceSigns } from "./engine/signs.js";
import { expandWindow, ruleIsActive } from "./engine/window.js";
import { geocode } from "./geocode/suggest.js";
import { reverseGeocode } from "./geocode/reverse.js";
import {
  MAX_WINDOW_MS,
  MIN_WINDOW_MS,
  parseTime,
  validateGeocodeArgs,
  validateRegSegId,
  validateReverseArgs,
  validateSearchRequest,
  wallSpanMs,
} from "./schemas.js";
import { epochFromWall } from "./time.js";

// SPEC §17, the persistent banner. Kept verbatim except for the markdown bold,
// which is the frontend's job.
export const DISCLAIMER =
  "CurbCheck is advisory only. This tool derives parking legality and price from NYC " +
  "Open Data (NYC DOT Sign Information Management System) and may be incomplete, out of " +
  "date, or misread by the software. Parking regulations change and temporary or " +
  "construction signage may override what is shown here. The posted sign at the curb is " +
  "the only authoritative regulation. Always read the posted sign before parking. " +
  "Sign-free prohibitions — within 15 feet of a fire hydrant (34 RCNY §4-08(e)(2)), " +
  "crosswalks, bus stops, and driveways — apply even where no sign is shown. CurbCheck " +
  "does not predict whether a space is physically available. A blank or grey curb means " +
  "no data, not no restriction. " +
  "Data © NYC Open Data; basemap © OpenStreetMap contributors.";

// SPEC §11 requires both of these on every answer, not just on bad ones.
// Temporary and construction signage is largely absent from the source dataset
// (Research Q A.5), so the warning is universal rather than conditional.
export const TEMPORARY_SIGNAGE_CAVEAT =
  "Temporary or construction signage may override what is shown here. The posted sign " +
  "at the curb is the only authoritative regulation.";
export const ASP_SUSPENSION_CAVEAT =
  "Emergency ASP suspensions are not reflected. Same-day weather and parade suspensions " +
  "are only visible if the optional 311 live check is enabled, and it is off by default.";
export const UNIVERSAL_CAVEATS = [TEMPORARY_SIGNAGE_CAVEAT, ASP_SUSPENSION_CAVEAT];

export const OUTSIDE_COVERAGE_MESSAGE = `That location is outside ${COVERAGE_AREA}, the only area CurbCheck covers.`;

// How much of the window one rule is in force for. `null` is reserved for the
// two cases where the question has no answer: no window was asked about, and a
// sign the parser could not read (D13).
const IN_EFFECT_ALL = "all";
const IN_EFFECT_PART = "part";
const IN_EFFECT_NONE = "none";

// A pack older than this gets a notice on `/api/health`. The weekly job is what
// keeps the site current, and GitHub disables a `schedule` trigger after 60 days
// without a commit, so a page that has silently stopped updating is the case
// this exists for.
export const STALE_AFTER_DAYS = 14;

const HOSTING_TEXT =
  "This copy of CurbCheck is a static site. The parking engine, the address index and the " +
  "whole dataset are downloaded once and run inside this browser, so no address, " +
  "coordinate or search you type ever leaves your machine. The files are served by GitHub " +
  "Pages, which sees your IP address, which pages you request and which map tiles you " +
  "look at. There are no cookies, no stored data, no analytics and no third-party request.";

const STALE_NOTICE_TITLE = "This data may be out of date";

/** A failure the worker turns into `{code, message}`. Messages carry no path or trace. */
export class HandlerError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "HandlerError";
    this.code = code;
    this.message = message;
  }
}

/**
 * Run one operation. `state` is built once after the pack loads and holds the
 * geocode index and the ASP calendar, both of which cost more to build than any
 * single request is allowed to.
 *
 * @param {string} op
 * @param {object} args
 * @param {object} pack
 * @param {{geocodeIndex: object, calendar: object}} state
 */
export function handle(op, args, pack, state) {
  switch (op) {
    case "search":
      return postSearch(args, pack, state);
    case "segment":
      return getSegment(args, pack, state);
    case "geocode":
      return getGeocode(args, pack, state);
    case "reverse":
      return getReverse(args, pack, state);
    case "health":
      return getHealth(pack);
    case "syncStatus":
      return getSyncStatus(pack);
    default:
      throw new HandlerError("invalid_request", "unknown operation");
  }
}

/** Rank the curb spans near a destination for a window. See docs/API.md. */
function postSearch(args, pack, state) {
  const body = validateSearchRequest(args);
  const destination = _destination(pack, state, body);
  if (!withinCoverage(pack, destination.lon, destination.lat)) {
    // Answering "nothing within that walk radius, try a longer walk" for a pin
    // in New Jersey advises widening a radius that was never the problem
    // (UX audit P0-3).
    throw new HandlerError("outside_coverage", OUTSIDE_COVERAGE_MESSAGE);
  }
  const found = search(pack, {
    lon: destination.lon,
    lat: destination.lat,
    t1: body.t1,
    t2: body.t2,
    walkMinutesMax: body.walkMinutes,
    weights: body.weights,
    limit: body.limit,
    mapLimit: body.mapLimit,
    calendar: state.calendar,
  });
  return {
    destination,
    // Ranked legal first, then every other verdict in radius. One array rather
    // than two: the map draws all of it and the list renders all of it.
    results: [...found.legal, ...found.others].map(_resultPayload),
    counts: found.counts,
    disclaimer: DISCLAIMER,
    caveats: [...UNIVERSAL_CAVEATS],
    sync: _syncSummary(pack),
  };
}

/**
 * The rule stack, the raw sign text, the meter rates and the geometry behind
 * one verdict.
 *
 * With `t1`/`t2` each rule also reports how much of the window it is in force
 * for and which single rule the verdict rests on. Omitting both is the
 * pre-window behaviour: every `in_effect` is null.
 */
function getSegment(args, pack, state) {
  const fields = args === null || args === undefined ? {} : args;
  const regSegId = validateRegSegId(fields.reg_seg_id);
  const window = _segmentWindow(fields.t1, fields.t2);

  const spanRow = pack.index.spanByRegSegId.get(regSegId);
  if (spanRow === undefined) {
    throw new HandlerError("not_found", "no such segment");
  }
  const spans = pack.spans;
  const segmentRow = spans.segment[spanRow];
  const segmentId = optionalStr(spans.segmentId[spanRow]);
  const side = optionalStr(spans.side[spanRow]);
  const gapKind = optionalStr(spans.gapKind[spanRow]);
  const derivedFromRows = spans.derivedFrom.lists.subarray(
    spans.derivedFrom.offsets[spanRow],
    spans.derivedFrom.offsets[spanRow + 1],
  );
  const derivedFrom = Array.from(derivedFromRows, (row) => String(pack.signs.signId[row]));
  const signs = blockfaceSigns(pack, regSegId, derivedFromRows, segmentRow, side);

  return {
    segment: {
      reg_seg_id: String(spans.regSegId[spanRow]),
      segment_id: segmentId,
      street_name: segmentRow < 0 ? null : optionalStr(pack.segments.streetName[segmentRow]),
      side,
      start_ft: optionalFloat(spans.startFt[spanRow]),
      end_ft: optionalFloat(spans.endFt[spanRow]),
      length_ft: optionalFloat(spans.lengthFt[spanRow]),
      capacity_cars: optionalInt(spans.capacityCars[spanRow]),
      capacity_approximate: Boolean(spans.capacityApproximate[spanRow]),
      confidence: spans.confidence[spanRow] || 0.0,
      derived_from: derivedFrom,
      gap_kind: gapKind,
    },
    geometry: pack.lineString(spans, spanRow),
    window: window === null ? null : { t1: isoWithOffset(window[0]), t2: isoWithOffset(window[1]) },
    regulations: _segmentRegulations(pack, spanRow, signs.governing, window, gapKind, state),
    // Two groups, not one list: 9 of the 11 signs the old list showed under the
    // audited green verdict do not govern the stretch, and the first of them
    // read NO STANDING ANYTIME (UX audit P0-2).
    governing: signs.governing,
    other_on_block: signs.otherOnBlock,
    meter_rates: _segmentMeterRates(pack, segmentRow, side),
  };
}

/** Local address and intersection lookup. An empty candidate list is a result, not an error. */
function getGeocode(args, pack, state) {
  const q = validateGeocodeArgs(args);
  return {
    query: q,
    candidates: geocode(pack, state.geocodeIndex, q).map(_candidatePayload),
  };
}

/** What a dropped pin is nearest to, so the UI can echo a place rather than a coordinate. */
function getReverse(args, pack, state) {
  const { lat, lon } = validateReverseArgs(args);
  if (!withinCoverage(pack, lon, lat)) {
    throw new HandlerError("outside_coverage", OUTSIDE_COVERAGE_MESSAGE);
  }
  const match = reverseGeocode(pack, state.geocodeIndex, lon, lat);
  if (match === null || match === undefined) {
    throw new HandlerError("not_found", "nothing on the map near that point");
  }
  return _reversePayload(match);
}

/**
 * The server's health shape plus the two fields only a static build has: the
 * staleness notice and the paragraph About shows about how this copy is served.
 */
function getHealth(pack) {
  const noCalendar = calendarIsMissing(pack);
  const bbox = coverageBbox(pack);
  const health = {
    // A pack with no calendar answers every query, and gets every holiday and
    // street-cleaning suspension wrong while doing it (SPEC §11).
    status: noCalendar ? "degraded" : "ok",
    db_present: true,
    db_readonly: true,
    sign_count: pack.signs.n,
    calendar_missing: noCalendar,
    // The edge of what CurbCheck knows, for the map to draw and for the client
    // to keep a pin inside without a round trip.
    coverage: bbox === null ? null : { area: COVERAGE_AREA, bbox: [...bbox] },
    hosting: { kind: "static", built_at: pack.meta.built_at, text: HOSTING_TEXT },
  };
  const notice = _staleNotice(pack.meta.built_at);
  if (notice !== null) {
    health.notice = notice;
  }
  return health;
}

/** The pack's `sync_meta` as a flat object. Values are returned as stored, never parsed. */
function getSyncStatus(pack) {
  const sync = pack.meta.sync || {};
  const status = {};
  for (const key of Object.keys(sync)) {
    status[key] = String(sync[key]);
  }
  return status;
}

/**
 * A warning when the pack is older than `STALE_AFTER_DAYS`.
 *
 * A weekly job that silently stops must not leave a page that looks current.
 * An unparseable `built_at` gets no notice: it is a build-time fact, not
 * something a visitor can act on, and guessing "stale" from it would put a
 * warning on every good deploy whose clock format changed.
 */
function _staleNotice(builtAt) {
  const built = Date.parse(String(builtAt));
  if (Number.isNaN(built)) {
    return null;
  }
  const days = (Date.now() - built) / 86_400_000;
  if (days <= STALE_AFTER_DAYS) {
    return null;
  }
  return {
    kind: "warn",
    title: STALE_NOTICE_TITLE,
    text:
      `This site's parking data was built on ${String(builtAt).slice(0, 10)} and is meant to be ` +
      "rebuilt every week. Signs and meter rates may have changed since. Read the posted " +
      "sign at the curb.",
  };
}

function _destination(pack, state, body) {
  if (body.lat !== null && body.lon !== null) {
    return {
      lat: body.lat,
      lon: body.lon,
      label: `${body.lat.toFixed(6)}, ${body.lon.toFixed(6)}`,
    };
  }
  const candidates = geocode(pack, state.geocodeIndex, body.address === null ? "" : body.address);
  if (candidates.length === 0) {
    throw new HandlerError("address_not_found", "could not find that address in Manhattan");
  }
  const best = candidates[0];
  return { lat: best.lat, lon: best.lon, label: best.label };
}

/** A `SearchResult` as JSON, field names unchanged, plus the universal caveat. */
function _resultPayload(result) {
  return { ...result, caveats: [...result.caveats, TEMPORARY_SIGNAGE_CAVEAT] };
}

function _candidatePayload(candidate) {
  return {
    label: candidate.label,
    lat: candidate.lat,
    lon: candidate.lon,
    kind: candidate.kind,
    confidence: candidate.confidence,
    secondary: candidate.secondary === undefined ? null : candidate.secondary,
  };
}

function _reversePayload(match) {
  return {
    label: match.label,
    secondary: match.secondary === undefined ? null : match.secondary,
    kind: match.kind,
    lat: match.lat,
    lon: match.lon,
    distance_m: match.distance_m,
  };
}

/**
 * The three `sync_meta` values the UI shows, tolerant of a half-built pack.
 *
 * The ETL writes its own key names; these are the aliases it has used, newest
 * first. A key the ETL stops writing degrades to null or zero rather than
 * breaking search, which matters because search is what surfaces the rest of the
 * failure states (SPEC §11).
 */
function _syncSummary(pack) {
  const meta = pack.meta.sync || {};
  let coverage = _first(meta, "coverage_pct");
  if (coverage === null) {
    // The ETL publishes blockface-side coverage as a 0-1 share. The Python
    // round-trips it through `str()`, which is the shortest repr of the same
    // double, so multiplying here is the same number.
    const share = _asFloat(_first(meta, "blockface_sides_matched_share"));
    coverage = share === null ? null : 100 * share;
  }
  return {
    last_sync: _first(meta, "last_sync_at", "last_sync"),
    sign_count: _asInt(_first(meta, "signs_loaded", "sign_count")),
    coverage_pct: _asFloat(coverage),
  };
}

function _first(meta, ...keys) {
  for (const key of keys) {
    if (Object.prototype.hasOwnProperty.call(meta, key)) {
      return meta[key];
    }
  }
  return null;
}

/**
 * The optional `t1`/`t2` window, or null when not asked for.
 *
 * Same rules as the search body: a naive value is local (every sign states local
 * time), and the window has to be between five minutes and a day so the per-day
 * expansion in `engine/window.js` stays bounded. A malformed time is the
 * framework's `validation_error`; a half or impossible window is the route's own
 * `invalid_request`.
 */
function _segmentWindow(t1, t2) {
  const hasT1 = t1 !== null && t1 !== undefined;
  const hasT2 = t2 !== null && t2 !== undefined;
  if (!hasT1 && !hasT2) {
    return null;
  }
  if (!hasT1 || !hasT2) {
    throw new HandlerError("invalid_request", "give both t1 and t2, or neither");
  }
  const start = parseTime(t1, "t1");
  const end = parseTime(t2, "t2");
  const span = wallSpanMs(start, end);
  if (span < MIN_WINDOW_MS) {
    throw new HandlerError(
      "invalid_request",
      "the parking window must be at least 5 minutes and end after it starts",
    );
  }
  if (span > MAX_WINDOW_MS) {
    throw new HandlerError("invalid_request", "the parking window must be 24 hours or less");
  }
  return [start, end];
}

/**
 * The span's rules, each naming the sign it was read from and what it does to
 * the window.
 *
 * A rule is tied to its sign by the raw description: the parser reads each
 * distinct description once, so that string is what a `regulation` row and a
 * `sign` row have in common. Two posts on one span carrying the same panel text
 * collapse to one rule, so the first sign in curb order is named.
 *
 * `_SEGMENT_REGULATION_SQL` ends in `ORDER BY reg_id`, so the rows are sorted
 * here rather than left in the pack's rowid order.
 */
function _segmentRegulations(pack, spanRow, governing, window, gapKind, state) {
  const byDescription = new Map();
  for (const sign of governing) {
    if (!byDescription.has(sign.sign_description)) {
      byDescription.set(sign.sign_description, sign.sign_id);
    }
  }

  const rows = Array.from(pack.index.regulationsBySpan[spanRow] || []);
  rows.sort((a, b) => {
    const left = String(pack.regulations.regId[a]);
    const right = String(pack.regulations.regId[b]);
    if (left === right) {
      return 0;
    }
    return left < right ? -1 : 1;
  });
  const stack = rows.map((row) => regulationWithMeta(pack, row));
  const coverage = _windowCoverage(stack, window, gapKind, state);

  return rows.map((row, index) => {
    const item = stack[index];
    const description = String(pack.regulations.rawSignDescription[row]);
    const seen = coverage.get(item);
    const signId = byDescription.get(description);
    return {
      reg_id: String(pack.regulations.regId[row]),
      sign_id: signId === undefined ? null : signId,
      raw_sign_description: description,
      parse_method: String(pack.regulations.parseMethod[row]),
      parse_confidence: Number(pack.regulations.parseConfidence[row]),
      regulation: regulationToJson(item.regulation),
      in_effect: seen === undefined ? null : seen[0],
      deciding: seen === undefined ? false : seen[1],
    };
  });
}

/**
 * `(in_effect, deciding)` per stack entry, keyed by object identity.
 *
 * Identity rather than `reg_id`, because the engine hands back the very objects
 * it was given and two identical rules would otherwise compare equal.
 */
function _windowCoverage(stack, window, gapKind, state) {
  const coverage = new Map();
  if (window === null || stack.length === 0) {
    return coverage;
  }
  const calendar = state.calendar;
  const [t1, t2] = window;
  const intervals = expandWindow(
    t1,
    t2,
    stack.map((item) => item.regulation),
  );
  let total = 0;
  for (const interval of intervals) {
    total += interval.minutes;
  }
  const verdict = evaluateSegment(stack.slice(), t1, t2, calendar, { gapKind });
  const deciding = verdict.deciding;

  for (const item of stack) {
    const isDeciding = deciding !== null && deciding !== undefined && item === deciding;
    if (item.parseMethod === ParseMethod.UNPARSED) {
      coverage.set(item, [null, isDeciding]);
      continue;
    }
    let active = 0;
    for (const interval of intervals) {
      if (ruleIsActive(item.regulation, interval, calendar)) {
        active += interval.minutes;
      }
    }
    let share = IN_EFFECT_PART;
    if (active === 0) {
      share = IN_EFFECT_NONE;
    } else if (active >= total) {
      share = IN_EFFECT_ALL;
    }
    coverage.set(item, [share, isDeciding]);
  }
  return coverage;
}

/**
 * Rates for this blockface-side. SPEC §13.1(b): several zones may cover one.
 *
 * `_METER_RATE_SQL` ends in `ORDER BY blockface_id`.
 */
function _segmentMeterRates(pack, segmentRow, side) {
  if (segmentRow === undefined || segmentRow === null || segmentRow < 0) {
    return [];
  }
  const rows = Array.from(pack.index.meterRatesBySegment[segmentRow] || []);
  rows.sort((a, b) => {
    const left = String(pack.meterRates.blockfaceId[a]);
    const right = String(pack.meterRates.blockfaceId[b]);
    if (left === right) {
      return 0;
    }
    return left < right ? -1 : 1;
  });
  const rates = [];
  for (const row of rows) {
    const rowSide = optionalStr(pack.meterRates.side[row]);
    if (rowSide !== null && side !== null && rowSide !== side) {
      continue;
    }
    rates.push({
      blockface_id: String(pack.meterRates.blockfaceId[row]),
      side: rowSide,
      rate_label: optionalStr(pack.meterRates.rateLabel[row]),
      hour_rates: jsonStringList(pack.meterRates.hourRates[row]),
      max_session_min: optionalInt(pack.meterRates.maxSessionMin[row]),
    });
  }
  return rates;
}

/**
 * `datetime.isoformat()` of an aware New York wall clock: the offset comes from
 * the instant the wall clock names, so a fall-back 01:30 prints -04:00 or -05:00
 * according to its `fold`.
 */
function isoWithOffset(wall) {
  const asUtc = Date.UTC(wall.year, wall.month - 1, wall.day, wall.hour, wall.minute, wall.second);
  const offsetMinutes = Math.round((asUtc - epochFromWall(wall)) / 60_000);
  const sign = offsetMinutes < 0 ? "-" : "+";
  const size = Math.abs(offsetMinutes);
  return (
    `${pad(wall.year, 4)}-${pad(wall.month, 2)}-${pad(wall.day, 2)}` +
    `T${pad(wall.hour, 2)}:${pad(wall.minute, 2)}:${pad(wall.second, 2)}` +
    `${sign}${pad(Math.floor(size / 60), 2)}:${pad(size % 60, 2)}`
  );
}

function pad(value, width) {
  return String(value).padStart(width, "0");
}

function _asInt(value) {
  if (value === null || value === undefined) {
    return 0;
  }
  const number = Number(value);
  return Number.isFinite(number) ? Math.trunc(number) : 0;
}

function _asFloat(value) {
  if (value === null || value === undefined) {
    return null;
  }
  const number = Number(value);
  return Number.isNaN(number) ? null : number;
}

function optionalStr(value) {
  return value === null || value === undefined ? null : String(value);
}

function optionalInt(value) {
  return value === null || value === undefined ? null : Math.trunc(Number(value));
}

function optionalFloat(value) {
  return value === null || value === undefined ? null : Number(value);
}
