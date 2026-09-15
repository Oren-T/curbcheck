/**
 * Request validation, mirroring `tests/test_api.py`'s 422 table.
 *
 * The bodies are the ones the server rejects; what is pinned here is the code,
 * because that is what the frontend branches on and what the differential
 * harness compares (docs/API.md).
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  MAX_LAT,
  MIN_WINDOW_MS,
  REG_SEG_ID_PATTERN,
  validateGeocodeArgs,
  validateRegSegId,
  validateReverseArgs,
  validateSearchRequest,
} from "../static/schemas.js";

const DESTINATION = { lat: 40.7784, lon: -73.9542 };
const WINDOW = { t1: "2026-09-15T09:00:00", t2: "2026-09-15T11:00:00" };

function refused(body, reason) {
  let thrown = null;
  try {
    validateSearchRequest(body);
  } catch (error) {
    thrown = error;
  }
  assert.ok(thrown !== null, `${reason} should have been refused`);
  assert.equal(thrown.code, "validation_error", reason);
  assert.equal(typeof thrown.message, "string");
  assert.ok(!thrown.message.includes("/workspace"), "messages carry no path");
}

test("a good body comes back with the server's defaults filled in", () => {
  const body = validateSearchRequest({ ...DESTINATION, ...WINDOW });

  assert.equal(body.lat, 40.7784);
  assert.equal(body.address, null);
  assert.equal(body.walkMinutes, 10.0);
  assert.equal(body.limit, 100);
  assert.equal(body.mapLimit, 2000);
  assert.deepEqual(body.weights, { walk: 1.0, money: 1.0, risk: 0.5 });
  assert.deepEqual({ year: body.t1.year, hour: body.t1.hour }, { year: 2026, hour: 9 });
});

test("bad search bodies are refused with the error envelope", () => {
  const cases = [
    [{ ...DESTINATION, ...WINDOW, surprise: 1 }, "unknown field"],
    [{ ...DESTINATION, t1: "2026-09-15T11:00:00", t2: "2026-09-15T09:00:00" }, "t2 before t1"],
    [{ ...DESTINATION, t1: "2026-09-15T09:00:00", t2: "2026-09-15T09:00:00" }, "zero length"],
    [{ ...DESTINATION, t1: "2026-09-15T09:00:00", t2: "2026-09-17T09:00:00" }, "over 24 h"],
    [{ lat: 40.7784, lon: -80.0, ...WINDOW }, "lon outside the bbox"],
    [{ lat: 51.5, lon: -73.95, ...WINDOW }, "lat outside the bbox"],
    [{ lat: 40.7784, ...WINDOW }, "lat without lon"],
    [{ ...DESTINATION, address: "1519 3 Ave", ...WINDOW }, "both point and address"],
    [{ ...WINDOW }, "no destination"],
    [{ ...DESTINATION, ...WINDOW, walk_minutes: 0 }, "walk_minutes under 1"],
    [{ ...DESTINATION, ...WINDOW, walk_minutes: 31 }, "walk_minutes over 30"],
    [{ ...DESTINATION, ...WINDOW, weights: { walk: 11 } }, "weight over 10"],
    [{ ...DESTINATION, ...WINDOW, weights: { walk: -1 } }, "negative weight"],
    [{ ...DESTINATION, ...WINDOW, weights: { speed: 1 } }, "unknown weight"],
    [{ ...DESTINATION, ...WINDOW, limit: 0 }, "limit under 1"],
    [{ ...DESTINATION, ...WINDOW, limit: 501 }, "limit over 500"],
    [{ ...DESTINATION, ...WINDOW, map_limit: 5001 }, "map_limit over its maximum"],
    [{ ...DESTINATION, ...WINDOW, limit: 1.5 }, "a fractional limit"],
    [{ ...DESTINATION, t1: "not a time", t2: "2026-09-15T11:00:00" }, "unparseable time"],
    [{ ...DESTINATION }, "no window at all"],
    [{ lat: Number.NaN, lon: -73.95, ...WINDOW }, "lat is NaN"],
    [{ lat: Number.POSITIVE_INFINITY, lon: -73.95, ...WINDOW }, "lat is infinite"],
    [{ address: "", ...WINDOW }, "an empty address"],
    [{ address: "x".repeat(121), ...WINDOW }, "an over-long address"],
    [null, "no body at all"],
  ];
  for (const [body, reason] of cases) {
    refused(body, reason);
  }
});

test("the window is measured on the wall clock, as CPython measures it", () => {
  // 2026-11-01 falls back, so midnight to midnight on it is 24 hours of wall
  // clock and 25 hours of real time. The server accepts it: `t2 - t1` is a
  // subtraction of two aware datetimes carrying the same `ZoneInfo` object, and
  // `datetime.__sub__` then ignores the offsets. Measuring on instants would
  // refuse a window the reference implementation answers.
  const body = validateSearchRequest({
    ...DESTINATION,
    t1: "2026-11-01T00:00:00",
    t2: "2026-11-02T00:00:00",
  });

  assert.equal(body.t2.day, 2);
  assert.throws(
    () =>
      validateSearchRequest({
        ...DESTINATION,
        t1: "2026-11-01T00:00:00",
        t2: "2026-11-02T00:01:00",
      }),
    /24 hours or less/,
  );
});

test("an offset-carrying time is read as the instant it names", () => {
  const body = validateSearchRequest({
    ...DESTINATION,
    t1: "2026-09-15T13:00:00+00:00",
    t2: "2026-09-15T15:00:00Z",
  });

  // 13:00 UTC is 09:00 in New York in September.
  assert.equal(body.t1.hour, 9);
  assert.equal(body.t2.hour, 11);
});

test("the shortest window the server accepts is accepted here", () => {
  assert.equal(MIN_WINDOW_MS, 5 * 60 * 1000);
  const body = validateSearchRequest({
    ...DESTINATION,
    t1: "2026-09-15T09:00:00",
    t2: "2026-09-15T09:05:00",
  });

  assert.equal(body.t2.minute, 5);
});

test("the latitude bound is the server's own", () => {
  assert.equal(MAX_LAT, 40.9);
  assert.equal(validateSearchRequest({ lat: MAX_LAT, lon: -73.95, ...WINDOW }).lat, MAX_LAT);
});

test("a geocode query is one to a hundred and twenty characters", () => {
  assert.equal(validateGeocodeArgs({ q: "350 5th" }), "350 5th");
  for (const args of [{}, { q: "" }, { q: "x".repeat(121) }, { q: 5 }]) {
    assert.throws(
      () => validateGeocodeArgs(args),
      (error) => error.code === "validation_error",
    );
  }
});

test("reverse takes a lat and a lon inside the input box", () => {
  assert.deepEqual(validateReverseArgs({ lat: 40.7784, lon: -73.9542 }), {
    lat: 40.7784,
    lon: -73.9542,
  });
  const bad = [
    { lat: 40.7784 },
    { lon: -73.9542 },
    { lat: 41.5, lon: -73.9542 },
    { lat: "nope", lon: -73.9542 },
    { lat: Number.NaN, lon: -73.9542 },
    { lat: Number.POSITIVE_INFINITY, lon: -73.9542 },
  ];
  for (const args of bad) {
    assert.throws(
      () => validateReverseArgs(args),
      (error) => error.code === "validation_error",
    );
  }
});

test("a segment id outside the charset never reaches a lookup", () => {
  assert.equal(validateRegSegId("3681:W:0"), "3681:W:0");
  assert.ok(REG_SEG_ID_PATTERN.test("3681:W:0"));

  const garbage = [
    "' OR 1=1",
    "a'b",
    "3681:W:0;DROP TABLE sign",
    "<script>",
    "x".repeat(200),
    "../../etc/passwd",
    "",
  ];
  for (const value of garbage) {
    assert.throws(
      () => validateRegSegId(value),
      (error) => error.code === "invalid_request",
    );
  }
});
