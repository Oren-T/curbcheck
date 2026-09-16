/**
 * The six operations, over the `tests/test_api.py` fixture block.
 *
 * The pack stand-in is the 3 AVE / E 85 ST database that file builds, minus the
 * address index: the geocode tables are empty, which is what makes
 * `address_not_found` reachable without depending on the suggester's ladder.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { buildGeocodeIndex } from "../static/geocode/index.js";
import { loadCalendar } from "../static/engine/calendar.js";
import {
  DISCLAIMER,
  HandlerError,
  STALE_AFTER_DAYS,
  TEMPORARY_SIGNAGE_CAVEAT,
  UNIVERSAL_CAVEATS,
  handle,
} from "../static/handlers.js";

const NODE_85 = [-73.9544835, 40.7781469];
const NODE_86 = [-73.953985, 40.7788304];
const DESTINATION = { lat: 40.7784, lon: -73.9542 };
const WINDOW = { t1: "2026-09-15T09:00:00", t2: "2026-09-15T11:00:00" };
// A sign description carrying markup, to prove the worker neither sanitizes nor
// mangles it: the frontend is the escaping boundary (docs/API.md).
const XSS_SIGN_TEXT = "2 HOUR PARKING <script>alert('x')</script> 9AM-7PM";

const BLOCK = [NODE_85, NODE_86];
const BBOX = {
  minLon: Float64Array.from([Math.min(NODE_85[0], NODE_86[0])]),
  minLat: Float64Array.from([Math.min(NODE_85[1], NODE_86[1])]),
  maxLon: Float64Array.from([Math.max(NODE_85[0], NODE_86[0])]),
  maxLat: Float64Array.from([Math.max(NODE_85[1], NODE_86[1])]),
};

function coordsColumn(lines) {
  const coords = [];
  const offsets = [0];
  for (const line of lines) {
    for (const [lon, lat] of line) {
      coords.push(lon, lat);
    }
    offsets.push(coords.length / 2);
  }
  return { coords: Float64Array.from(coords), offsets: Uint32Array.from(offsets) };
}

const EMPTY_TABLES = {
  addressPoints: ["streetNorm", "houseNumber", "display", "zipcode", "lon", "lat"],
  intersections: ["aNorm", "bNorm", "display", "lon", "lat"],
  places: ["placeId", "display", "lon", "lat"],
  placeTokens: ["token", "position", "placeId", "searchName"],
  streets: ["streetNorm", "display", "lon", "lat"],
  streetVariants: ["variant", "streetNorm"],
  streetTokens: ["token", "position", "streetNorm", "searchName"],
  zipCentroids: ["zipcode", "lon", "lat", "addressPoints"],
};

function emptyGeocodeTables() {
  const geocode = {};
  for (const [name, columns] of Object.entries(EMPTY_TABLES)) {
    geocode[name] = { n: 0 };
    for (const column of columns) {
      geocode[name][column] = [];
    }
  }
  return geocode;
}

/** The fixture block, with `overrides` folded into the tables they name. */
function buildPack(overrides = {}) {
  const signs = overrides.signs || [
    { signId: "sign-1", signDescription: XSS_SIGN_TEXT },
    { signId: "sign-2", signDescription: "NO STANDING ANYTIME" },
  ];
  const meterRates = overrides.meterRates || [
    {
      blockfaceId: "bf-1",
      side: "W",
      rateLabel: "Area 1",
      hourRates: '["4.50", "5.50"]',
      maxSessionMin: 120,
    },
  ];
  const calendar = overrides.calendar === undefined ? ["2026-12-25"] : overrides.calendar;

  return {
    meta: {
      format: 1,
      built_at: overrides.builtAt || new Date().toISOString(),
      sync: overrides.sync || {
        last_sync_at: "2026-09-14T22:05:11-04:00",
        signs_loaded: "2",
        blockface_sides_matched_share: "0.9378",
      },
      coverage: { area: "Manhattan" },
      calendar_missing: false,
    },
    spans: {
      n: 1,
      regSegId: ["3681:W:0"],
      segment: Int32Array.from([0]),
      segmentId: ["3681"],
      side: ["W"],
      startFt: [0.0],
      endFt: [284.8],
      geom: coordsColumn([BLOCK]),
      lengthFt: [284.8],
      capacityCars: [12],
      capacityApproximate: Uint8Array.from([1]),
      confidence: Float64Array.from([0.94]),
      derivedFrom: { lists: Int32Array.from([0]), offsets: Uint32Array.from([0, 1]) },
      gapKind: [null],
      bbox: BBOX,
    },
    regulations: {
      n: 1,
      regId: ["3681:W:0:1"],
      span: Int32Array.from([0]),
      action: ["park"],
      permitted: Uint8Array.from([1]),
      vehicleClass: ["all"],
      exclusive: Uint8Array.from([0]),
      daysMask: Int32Array.from([127]),
      timeFrom: ["08:00"],
      timeTo: ["19:00"],
      metered: Uint8Array.from([0]),
      maxDurationMin: [240],
      flags: Int32Array.from([0]),
      effectiveFrom: [null],
      effectiveTo: [null],
      arrow: ["none"],
      rawSignDescription: [XSS_SIGN_TEXT],
      parseMethod: ["grammar"],
      parseConfidence: Float64Array.from([0.98]),
    },
    signs: {
      n: signs.length,
      signId: signs.map((sign) => sign.signId),
      orderNumber: signs.map(() => "1-11111"),
      signCode: signs.map(() => "PRK-9"),
      signDescription: signs.map((sign) => sign.signDescription),
      onStreet: signs.map(() => "3 AVENUE"),
      fromStreet: signs.map(() => "EAST 85 STREET"),
      toStreet: signs.map(() => "EAST 86 STREET"),
      sideOfStreet: signs.map(() => "W"),
      distanceFromIntersection: signs.map((sign) =>
        sign.distance === undefined ? 44.0 : sign.distance,
      ),
      arrowDirection: signs.map(() => null),
      snapConfidence: signs.map(() => 0.94),
      snapNotes: signs.map(() => ""),
      isRegulation: Uint8Array.from(signs, () => 1),
      panelClass: signs.map(() => "regulation"),
      segment: Int32Array.from(signs, () => 0),
      segmentId: signs.map(() => "3681"),
    },
    meterRates: {
      n: meterRates.length,
      blockfaceId: meterRates.map((rate) => rate.blockfaceId),
      segment: Int32Array.from(meterRates, () => 0),
      segmentId: meterRates.map(() => "3681"),
      side: meterRates.map((rate) => rate.side),
      rateLabel: meterRates.map((rate) => rate.rateLabel),
      hourRates: meterRates.map((rate) => rate.hourRates),
      maxSessionMin: meterRates.map((rate) => rate.maxSessionMin),
    },
    calendar: {
      n: calendar.length,
      date: calendar,
      isMajorLegalHoliday: Uint8Array.from(calendar, () => 1),
      metersSuspended: Uint8Array.from(calendar, () => 1),
      label: calendar.map(() => "Christmas Day"),
    },
    segments: {
      n: 1,
      segmentId: ["3681"],
      streetName: ["3 AVE"],
      streetNorm: ["3 AVE"],
      fromNode: Int32Array.from([0]),
      toNode: Int32Array.from([-1]),
      geom: coordsColumn([BLOCK]),
      bbox: BBOX,
    },
    nodes: {
      n: 1,
      nodeId: ["n85"],
      lon: Float64Array.from([NODE_85[0]]),
      lat: Float64Array.from([NODE_85[1]]),
      streetNames: [JSON.stringify(["3 AVE", "E 85 ST"])],
    },
    geocode: emptyGeocodeTables(),
    index: {
      spanByRegSegId: new Map([["3681:W:0", 0]]),
      segmentById: new Map([["3681", 0]]),
      signById: new Map(signs.map((sign, row) => [sign.signId, row])),
      regulationsBySpan: [Int32Array.from([0])],
      signsBySegment: [Int32Array.from(signs.map((_, row) => row))],
      meterRatesBySegment: [Int32Array.from(meterRates.map((_, row) => row))],
      spanGrid: { query: () => Int32Array.from([0]) },
      segmentGrid: { query: () => Int32Array.from([0]) },
    },
    lineString(table, row) {
      const coordinates = [];
      for (let at = table.geom.offsets[row]; at < table.geom.offsets[row + 1]; at += 1) {
        coordinates.push([table.geom.coords[2 * at], table.geom.coords[2 * at + 1]]);
      }
      return { type: "LineString", coordinates };
    },
  };
}

function call(op, args, overrides = {}) {
  const pack = buildPack(overrides);
  const state = { geocodeIndex: buildGeocodeIndex(pack), calendar: loadCalendar(pack) };
  return handle(op, args, pack, state);
}

function refuses(op, args, code, overrides = {}) {
  assert.throws(
    () => call(op, args, overrides),
    (error) => error instanceof HandlerError && error.code === code,
  );
}

// --- search --------------------------------------------------------------

test("search answers with the disclaimer, the caveats and the sync summary", () => {
  const body = call("search", { ...DESTINATION, ...WINDOW });

  assert.equal(body.disclaimer, DISCLAIMER);
  assert.deepEqual(body.caveats, UNIVERSAL_CAVEATS);
  assert.deepEqual(body.destination, {
    lat: 40.7784,
    lon: -73.9542,
    label: "40.778400, -73.954200",
  });
  assert.deepEqual(body.sync, {
    last_sync: "2026-09-14T22:05:11-04:00",
    sign_count: 2,
    coverage_pct: 93.78,
  });
  assert.deepEqual(body.counts, { legal: 1, illegal: 0, ambiguous: 0, no_data: 0, total: 1 });
});

test("every result carries the universal temporary-signage caveat", () => {
  const [result] = call("search", { ...DESTINATION, ...WINDOW }).results;

  assert.equal(result.reg_seg_id, "3681:W:0");
  assert.equal(result.verdict, "legal");
  assert.equal(result.basis, "posted");
  assert.equal(result.caveats.at(-1), TEMPORARY_SIGNAGE_CAVEAT);
  assert.equal(result.street_name, "3 AVE, west side, at E 85 ST");
  // Money is a string, never a JSON number (docs/API.md).
  assert.equal(result.money, "0.00");
  assert.equal(typeof result.money_value, "number");
  assert.deepEqual(
    result.signs.map((sign) => sign.sign_description),
    [XSS_SIGN_TEXT],
  );
});

test("a destination outside coverage is refused, not answered empty", () => {
  // The New Jersey case: never advise a longer walk (docs/DECISIONS.md D28).
  refuses("search", { lat: 40.7, lon: -74.04, ...WINDOW }, "outside_coverage");
});

test("an address the local index cannot resolve is address_not_found", () => {
  refuses("search", { address: "nowhere at all", ...WINDOW }, "address_not_found");
});

test("a bad search body is a validation error, not a crash", () => {
  refuses("search", { ...DESTINATION, ...WINDOW, surprise: 1 }, "validation_error");
});

// --- segment detail ------------------------------------------------------

test("segment returns the stack, the signs and the rates", () => {
  const body = call("segment", { reg_seg_id: "3681:W:0" });

  assert.equal(body.segment.street_name, "3 AVE");
  assert.equal(body.segment.capacity_cars, 12);
  assert.equal(body.segment.capacity_approximate, true);
  assert.deepEqual(body.segment.derived_from, ["sign-1"]);
  assert.equal(body.segment.gap_kind, null);
  assert.equal(body.geometry.type, "LineString");

  const [regulation] = body.regulations;
  assert.equal(regulation.parse_method, "grammar");
  assert.equal(regulation.parse_confidence, 0.98);
  assert.equal(regulation.raw_sign_description, XSS_SIGN_TEXT);
  assert.equal(regulation.regulation.max_duration_min, 240);
  // The rule names the post it was read from, so the UI can file it under it.
  assert.equal(regulation.sign_id, "sign-1");

  assert.deepEqual(
    body.governing.map((sign) => sign.sign_description),
    [XSS_SIGN_TEXT],
  );
  assert.deepEqual(
    body.other_on_block.map((sign) => sign.sign_description),
    ["NO STANDING ANYTIME"],
  );
  assert.deepEqual(body.meter_rates, [
    {
      blockface_id: "bf-1",
      side: "W",
      rate_label: "Area 1",
      hour_rates: ["4.50", "5.50"],
      max_session_min: 120,
    },
  ]);
});

test("segment without a window answers nothing about the window", () => {
  const body = call("segment", { reg_seg_id: "3681:W:0" });

  assert.equal(body.window, null);
  assert.deepEqual(
    body.regulations.map((rule) => rule.in_effect),
    [null],
  );
  assert.deepEqual(
    body.regulations.map((rule) => rule.deciding),
    [false],
  );
});

test("segment with a window says which rule decided and how much it covers", () => {
  const body = call("segment", { reg_seg_id: "3681:W:0", ...WINDOW });

  assert.equal(body.window.t1, "2026-09-15T09:00:00-04:00");
  assert.equal(body.window.t2, "2026-09-15T11:00:00-04:00");
  const [rule] = body.regulations;
  assert.equal(rule.in_effect, "all");
  assert.equal(rule.deciding, true);
});

test("segment reports a rule that is off during the window", () => {
  const body = call("segment", {
    reg_seg_id: "3681:W:0",
    t1: "2026-09-15T20:00:00",
    t2: "2026-09-15T22:00:00",
  });

  const [rule] = body.regulations;
  assert.equal(rule.in_effect, "none");
  // Nothing is in force, so the verdict is legality by absence and there is no
  // rule to name (UX audit P0-1).
  assert.equal(rule.deciding, false);
});

test("segment reports a rule that covers only part of the window", () => {
  const body = call("segment", {
    reg_seg_id: "3681:W:0",
    t1: "2026-09-15T07:00:00",
    t2: "2026-09-15T09:00:00",
  });

  assert.equal(body.regulations[0].in_effect, "part");
});

test("a half or impossible window is refused", () => {
  const cases = [
    { reg_seg_id: "3681:W:0", t1: "2026-09-15T09:00:00" },
    { reg_seg_id: "3681:W:0", t2: "2026-09-15T11:00:00" },
    { reg_seg_id: "3681:W:0", t1: "2026-09-15T09:00:00", t2: "2026-09-15T09:01:00" },
    { reg_seg_id: "3681:W:0", t1: "2026-09-15T09:00:00", t2: "2026-09-17T09:00:00" },
  ];
  for (const args of cases) {
    refuses("segment", args, "invalid_request");
  }
});

test("an unparseable window time is a validation error", () => {
  refuses(
    "segment",
    { reg_seg_id: "3681:W:0", t1: "not a time", t2: "2026-09-15T11:00:00" },
    "validation_error",
  );
});

test("an unknown segment id is not_found and garbage never reaches a lookup", () => {
  refuses("segment", { reg_seg_id: "3681:W:999" }, "not_found");
  refuses("segment", { reg_seg_id: "3681:W:0;DROP TABLE sign" }, "invalid_request");
});

test("a meter rate on the other side of the street is not quoted", () => {
  const body = call(
    "segment",
    { reg_seg_id: "3681:W:0" },
    {
      meterRates: [
        {
          blockfaceId: "bf-2",
          side: "E",
          rateLabel: "Area 2",
          hourRates: '["9.00"]',
          maxSessionMin: 60,
        },
        {
          blockfaceId: "bf-1",
          side: "W",
          rateLabel: "Area 1",
          hourRates: '["4.50"]',
          maxSessionMin: 120,
        },
        {
          blockfaceId: "bf-0",
          side: null,
          rateLabel: "Citywide",
          hourRates: '["1.00"]',
          maxSessionMin: null,
        },
      ],
    },
  );

  // `_METER_RATE_SQL` ends in `ORDER BY blockface_id`, and a row with no side
  // covers the whole blockface.
  assert.deepEqual(
    body.meter_rates.map((rate) => rate.blockface_id),
    ["bf-0", "bf-1"],
  );
});

// --- geocode and reverse -------------------------------------------------

test("an empty candidate list is a result, not an error", () => {
  assert.deepEqual(call("geocode", { q: "nothing here" }), {
    query: "nothing here",
    candidates: [],
  });
});

test("a hostile geocode query never raises", () => {
  for (const q of ["'; DROP TABLE sign--", "<script>", "%", "a".repeat(120)]) {
    assert.deepEqual(call("geocode", { q }).candidates, []);
  }
});

test("a pin outside coverage is refused before the index is read", () => {
  refuses("reverse", { lat: 40.7, lon: -74.04 }, "outside_coverage");
});

test("a pin is read back as the corner it landed on", () => {
  // `lat`/`lon` are the returned place, not the pin, and `distance_m` is how far
  // the pin is from it (docs/API.md).
  const match = call("reverse", { lat: NODE_85[1], lon: NODE_85[0] });

  assert.equal(match.kind, "intersection");
  assert.equal(match.secondary, "Manhattan");
  assert.deepEqual([match.lon, match.lat], NODE_85);
  assert.equal(match.distance_m, 0);
});

// --- health and sync status ----------------------------------------------

test("health reports the pack, its coverage and how this copy is served", () => {
  const body = call("health");

  assert.equal(body.status, "ok");
  assert.equal(body.db_present, true);
  assert.equal(body.db_readonly, true);
  assert.equal(body.sign_count, 2);
  assert.equal(body.calendar_missing, false);
  assert.deepEqual(body.coverage, {
    area: "Manhattan",
    bbox: [
      Math.min(NODE_85[0], NODE_86[0]),
      Math.min(NODE_85[1], NODE_86[1]),
      Math.max(NODE_85[0], NODE_86[0]),
      Math.max(NODE_85[1], NODE_86[1]),
    ],
  });
  assert.equal(body.hosting.kind, "static");
  assert.ok(body.hosting.text.includes("ever leaves your machine"));
  // A fresh build says nothing; the notice is for a job that has stopped.
  assert.equal("notice" in body, false);
});

test("a pack older than the staleness window carries a notice", () => {
  const stale = new Date(Date.now() - (STALE_AFTER_DAYS + 1) * 86_400_000).toISOString();

  const body = call("health", {}, { builtAt: stale });

  assert.equal(body.notice.kind, "warn");
  assert.ok(body.notice.text.includes(stale.slice(0, 10)));
});

test("a pack with no calendar is degraded", () => {
  // A sync that dropped the calendar answers every query and gets holidays wrong.
  const body = call("health", {}, { calendar: [] });

  assert.equal(body.status, "degraded");
  assert.equal(body.calendar_missing, true);
  assert.equal(body.sign_count, 2);
});

test("sync status returns the meta table as strings", () => {
  assert.deepEqual(call("syncStatus"), {
    last_sync_at: "2026-09-14T22:05:11-04:00",
    signs_loaded: "2",
    blockface_sides_matched_share: "0.9378",
  });
});

test("a sync table missing a key degrades to null and zero rather than failing", () => {
  const body = call("search", { ...DESTINATION, ...WINDOW }, { sync: {} });

  assert.deepEqual(body.sync, { last_sync: null, sign_count: 0, coverage_pct: null });
});

test("an unknown operation is refused", () => {
  refuses("drop-everything", {}, "invalid_request");
});
