/**
 * End-to-end radius search over a hand-built pack.
 *
 * `tests/test_engine_search.py`, ported: the fixture places four curb spans due
 * north of one point so distances are easy to reason about — 0.001 degrees of
 * latitude is about 111 m. The pack stand-in holds the columns the engine reads
 * and a grid that answers with every row, which the contract allows (the caller
 * re-checks the box).
 */

import assert from "node:assert/strict";
import test from "node:test";

import { COST_TIE_BAND, MAX_MAP_LIMIT, search } from "../static/engine/search.js";
import { CALENDAR_MISSING_CAVEAT, UNKNOWN_GAP_REASON } from "../static/engine/resolve.js";
import { parseRequestTime } from "../static/time.js";

const ORIGIN_LON = -73.96;
const ORIGIN_LAT = 40.78;
const SATURDAY = {
  t1: parseRequestTime("2026-09-19T10:00"),
  t2: parseRequestTime("2026-09-19T11:30"),
};

const NO_PARKING = { action: "park", permitted: false };
const PERMITTED = { action: "park", permitted: true };
const WEEKDAY_BAN = { action: "park", permitted: false, days: [0, 1, 2, 3, 4] };
const METERED_SATURDAY = {
  action: "park",
  permitted: true,
  days: [5],
  timeFrom: "08:00",
  timeTo: "19:00",
  metered: true,
  maxDurationMin: 180,
};

function lineAt(lat, fromLon = ORIGIN_LON, toLon = ORIGIN_LON + 0.0005) {
  return [
    [fromLon, lat],
    [toLon, lat],
  ];
}

function column(rows, name, fallback = null) {
  return rows.map((row) => (row[name] === undefined ? fallback : row[name]));
}

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

function bboxColumn(lines) {
  const of = (line, index, pick) => pick(...line.map((point) => point[index]));
  return {
    minLon: Float64Array.from(lines, (line) => of(line, 0, Math.min)),
    minLat: Float64Array.from(lines, (line) => of(line, 1, Math.min)),
    maxLon: Float64Array.from(lines, (line) => of(line, 0, Math.max)),
    maxLat: Float64Array.from(lines, (line) => of(line, 1, Math.max)),
  };
}

function listsColumn(lists) {
  const flat = [];
  const offsets = [0];
  for (const list of lists) {
    flat.push(...list);
    offsets.push(flat.length);
  }
  return { lists: Int32Array.from(flat), offsets: Uint32Array.from(offsets) };
}

const DAY_BITS = [1, 2, 4, 8, 16, 32, 64];

function daysMask(days) {
  return days.reduce((mask, day) => mask | DAY_BITS[day], 0);
}

/**
 * A pack holding just enough of every table for `engine/search.js`.
 *
 * Spans name their street segment; one is created per distinct `segmentId` with
 * the span's own geometry, which is what `add_segment` does in the Python
 * fixture.
 */
function buildPack(spec) {
  const spans = spec.spans;
  const signs = spec.signs || [];
  const nodes = spec.nodes || [];
  const signRow = new Map(signs.map((sign, row) => [sign.signId, row]));

  const segments = spec.segments || [];
  const segmentRow = new Map();
  for (const span of spans) {
    if (span.segmentId === null || segmentRow.has(span.segmentId)) {
      continue;
    }
    if (!segments.some((segment) => segment.segmentId === span.segmentId)) {
      segments.push({
        segmentId: span.segmentId,
        streetName: "3 AVENUE",
        streetNorm: "3 AV",
        line: span.line,
      });
    }
  }
  segments.forEach((segment, row) => segmentRow.set(segment.segmentId, row));

  const regulations = spec.regulations || [];
  const spanRow = new Map(spans.map((span, row) => [span.regSegId, row]));

  const spanLines = spans.map((span) => span.line);
  const segmentLines = segments.map((segment) => segment.line);

  const regulationsBySpan = spans.map(() => []);
  regulations.forEach((rule, row) => regulationsBySpan[spanRow.get(rule.regSegId)].push(row));

  const meterRates = spec.meterRates || [];
  const meterRatesBySegment = segments.map(() => []);
  meterRates.forEach((rate, row) => meterRatesBySegment[segmentRow.get(rate.segmentId)].push(row));

  const allSpanRows = Int32Array.from(spans.map((_, row) => row));
  const allSegmentRows = Int32Array.from(segments.map((_, row) => row));

  return {
    meta: { built_at: "2026-09-15T18:02:11+00:00", sync: {}, calendar_missing: false },
    spans: {
      n: spans.length,
      regSegId: column(spans, "regSegId"),
      segment: Int32Array.from(spans, (span) =>
        span.segmentId === null ? -1 : segmentRow.get(span.segmentId),
      ),
      segmentId: column(spans, "segmentId"),
      side: column(spans, "side", "E"),
      startFt: column(spans, "startFt"),
      endFt: column(spans, "endFt"),
      geom: coordsColumn(spanLines),
      lengthFt: column(spans, "lengthFt", 220.0),
      capacityCars: column(spans, "capacityCars", 10),
      capacityApproximate: Uint8Array.from(spans, () => 1),
      confidence: Float64Array.from(spans, (span) =>
        span.confidence === undefined ? 1.0 : span.confidence,
      ),
      derivedFrom: listsColumn(
        spans.map((span) => (span.derivedFrom || []).map((id) => signRow.get(id))),
      ),
      gapKind: column(spans, "gapKind"),
      bbox: bboxColumn(spanLines),
    },
    regulations: {
      n: regulations.length,
      regId: column(regulations, "regId"),
      span: Int32Array.from(regulations, (rule) => spanRow.get(rule.regSegId)),
      action: regulations.map((rule) => rule.reg.action),
      permitted: Uint8Array.from(regulations, (rule) => (rule.reg.permitted ? 1 : 0)),
      vehicleClass: regulations.map((rule) => rule.reg.vehicleClass || "all"),
      exclusive: Uint8Array.from(regulations, (rule) => (rule.reg.exclusive ? 1 : 0)),
      daysMask: Int32Array.from(regulations, (rule) =>
        daysMask(rule.reg.days || [0, 1, 2, 3, 4, 5, 6]),
      ),
      timeFrom: regulations.map((rule) => rule.reg.timeFrom || null),
      timeTo: regulations.map((rule) => rule.reg.timeTo || null),
      metered: Uint8Array.from(regulations, (rule) => (rule.reg.metered ? 1 : 0)),
      maxDurationMin: regulations.map((rule) =>
        rule.reg.maxDurationMin === undefined ? null : rule.reg.maxDurationMin,
      ),
      flags: Int32Array.from(regulations, (rule) => rule.reg.flags || 0),
      effectiveFrom: regulations.map(() => null),
      effectiveTo: regulations.map(() => null),
      arrow: regulations.map((rule) => rule.reg.arrow || "none"),
      rawSignDescription: column(regulations, "raw"),
      parseMethod: column(regulations, "parseMethod", "grammar"),
      parseConfidence: Float64Array.from(regulations, (rule) =>
        rule.parseConfidence === undefined ? 0.98 : rule.parseConfidence,
      ),
    },
    signs: {
      n: signs.length,
      signId: column(signs, "signId"),
      orderNumber: signs.map((sign) => `order-${sign.signId}`),
      signCode: column(signs, "signCode", "PS-127C"),
      signDescription: column(signs, "signDescription"),
      segmentId: signs.map(() => null),
    },
    meterRates: {
      n: meterRates.length,
      blockfaceId: column(meterRates, "blockfaceId"),
      segmentId: column(meterRates, "segmentId"),
      side: column(meterRates, "side", "E"),
      rateLabel: column(meterRates, "rateLabel", "M2"),
      hourRates: meterRates.map((rate) => JSON.stringify(rate.hourRates)),
      maxSessionMin: meterRates.map(() => null),
    },
    segments: {
      n: segments.length,
      segmentId: column(segments, "segmentId"),
      streetName: column(segments, "streetName"),
      streetNorm: column(segments, "streetNorm"),
      fromNode: Int32Array.from(segments, (segment) =>
        segment.fromNode === undefined ? -1 : segment.fromNode,
      ),
      toNode: Int32Array.from(segments, (segment) =>
        segment.toNode === undefined ? -1 : segment.toNode,
      ),
      geom: coordsColumn(segmentLines),
      bbox: bboxColumn(segmentLines),
    },
    nodes: {
      n: nodes.length,
      nodeId: column(nodes, "nodeId"),
      streetNames: nodes.map((node) => JSON.stringify(node.streetNames)),
    },
    calendar: {
      n: (spec.calendar || []).length,
      date: column(spec.calendar || [], "date"),
      isMajorLegalHoliday: Uint8Array.from(spec.calendar || [], (day) =>
        day.isMajorLegalHoliday ? 1 : 0,
      ),
      metersSuspended: Uint8Array.from(spec.calendar || [], (day) => (day.metersSuspended ? 1 : 0)),
      label: column(spec.calendar || [], "label", ""),
    },
    index: {
      spanByRegSegId: spanRow,
      segmentById: segmentRow,
      signById: signRow,
      regulationsBySpan: regulationsBySpan.map((rows) => Int32Array.from(rows)),
      meterRatesBySegment: meterRatesBySegment.map((rows) => Int32Array.from(rows)),
      signsBySegment: segments.map(() => new Int32Array(0)),
      spanGrid: { query: () => allSpanRows },
      segmentGrid: { query: () => allSegmentRows },
    },
    lineString(table, row) {
      const start = table.geom.offsets[row];
      const end = table.geom.offsets[row + 1];
      const coordinates = [];
      for (let vertex = start; vertex < end; vertex += 1) {
        coordinates.push([table.geom.coords[2 * vertex], table.geom.coords[2 * vertex + 1]]);
      }
      return { type: "LineString", coordinates };
    },
  };
}

/** The four spans of the Python fixture, plus one far outside the radius. */
function fixture(extra = {}) {
  const spec = {
    signs: [
      { signId: "sign-meter", signDescription: "3 HOUR METERED PARKING SATURDAY 8AM-7PM" },
      { signId: "sign-free", signDescription: "NO PARKING ANYTIME" },
      { signId: "sign-odd", signDescription: "NO PARKING EXCEPT ODD SIDE HOLIDAYS PER SW-473" },
      { signId: "sign-far", signDescription: "NO PARKING ANYTIME" },
    ],
    spans: [
      {
        regSegId: "seg-meter",
        segmentId: "street-seg-meter",
        side: "E",
        line: lineAt(ORIGIN_LAT + 0.001),
        derivedFrom: ["sign-meter"],
      },
      {
        regSegId: "seg-free",
        segmentId: "street-seg-free",
        side: "E",
        line: lineAt(ORIGIN_LAT + 0.003),
        derivedFrom: ["sign-free"],
      },
      {
        regSegId: "seg-odd",
        segmentId: "street-seg-odd",
        side: "E",
        line: lineAt(ORIGIN_LAT + 0.002),
        derivedFrom: ["sign-odd"],
      },
      {
        regSegId: "seg-blank",
        segmentId: "street-seg-blank",
        side: "E",
        line: lineAt(ORIGIN_LAT + 0.0015),
        derivedFrom: [],
      },
      {
        regSegId: "seg-far",
        segmentId: "street-seg-far",
        side: "E",
        line: lineAt(ORIGIN_LAT + 0.02),
        derivedFrom: ["sign-far"],
      },
    ],
    regulations: [
      {
        regId: "reg-meter",
        regSegId: "seg-meter",
        reg: METERED_SATURDAY,
        raw: "3 HOUR METERED PARKING SATURDAY 8AM-7PM",
      },
      {
        regId: "reg-free",
        regSegId: "seg-free",
        reg: NO_PARKING,
        raw: "NO PARKING ANYTIME",
      },
      {
        regId: "reg-odd",
        regSegId: "seg-odd",
        reg: NO_PARKING,
        raw: "NO PARKING EXCEPT ODD SIDE HOLIDAYS PER SW-473",
        parseMethod: "unparsed",
        parseConfidence: 0.0,
      },
      {
        regId: "reg-far",
        regSegId: "seg-far",
        reg: NO_PARKING,
        raw: "NO PARKING ANYTIME",
      },
    ],
    meterRates: [
      {
        blockfaceId: "bf-1",
        segmentId: "street-seg-meter",
        side: "E",
        rateLabel: "M2",
        hourRates: ["5.00", "8.25"],
      },
    ],
    calendar: [
      {
        date: "2026-12-25",
        isMajorLegalHoliday: true,
        metersSuspended: true,
        label: "Christmas Day",
      },
    ],
  };
  for (const [key, value] of Object.entries(extra)) {
    spec[key] = [...(spec[key] || []), ...value];
  }
  return buildPack(spec);
}

function run(pack, overrides = {}) {
  return search(pack, {
    lon: ORIGIN_LON,
    lat: ORIGIN_LAT,
    t1: SATURDAY.t1,
    t2: SATURDAY.t2,
    walkMinutesMax: 10.0,
    ...overrides,
  });
}

/** Every result the search returned, ranked legal first — the API's own order. */
function runSearch(pack, overrides = {}) {
  const found = run(pack, overrides);
  return [...found.legal, ...found.others];
}

const byId = (results) => new Map(results.map((result) => [result.reg_seg_id, result]));
const one = (results, id) => byId(results).get(id);

test("search returns every span within the walk radius", () => {
  const ids = runSearch(fixture()).map((result) => result.reg_seg_id);

  assert.deepEqual(new Set(ids), new Set(["seg-meter", "seg-free", "seg-odd", "seg-blank"]));
});

test("spans beyond the radius are excluded", () => {
  // 10 walk minutes is a 618 m straight-line radius; seg-far is 2.2 km away.
  const ids = runSearch(fixture()).map((result) => result.reg_seg_id);

  assert.equal(ids.includes("seg-far"), false);
});

test("a tighter radius drops the farther spans", () => {
  // 2 walk minutes is a 124 m radius: it reaches seg-meter at 111 m but not
  // seg-blank at 166 m.
  const ids = runSearch(fixture(), { walkMinutesMax: 2.0 }).map((result) => result.reg_seg_id);

  assert.deepEqual(ids, ["seg-meter"]);
});

test("results are ordered legal then ambiguous then illegal then no_data", () => {
  const verdicts = runSearch(fixture()).map((result) => result.verdict);

  assert.deepEqual(verdicts, ["legal", "ambiguous", "illegal", "no_data"]);
});

test("the metered span is priced progressively", () => {
  // 90 charged minutes on M2 rates: $5.00 for the first hour, half of $8.25.
  const result = one(runSearch(fixture()), "seg-meter");

  assert.equal(result.money, "9.13");
  assert.equal(result.money_value, 9.13);
  assert.equal(result.price_known, true);
  assert.equal(result.rate_label, "M2");
  assert.equal(result.charged_minutes, 90);
});

test("a metered span without a published rate reports an unknown price", () => {
  const pack = fixture();
  pack.meterRates.n = 0;
  pack.index.meterRatesBySegment = pack.index.meterRatesBySegment.map(() => new Int32Array(0));

  const result = one(runSearch(pack), "seg-meter");

  assert.equal(result.money, null);
  assert.equal(result.money_value, null);
  assert.equal(result.price_known, false);
  assert.ok(result.caveats.includes("Metered, but no published rate for this blockface."));
});

test("two meter zones on one blockface quote the dearer and flag it", () => {
  // SPEC §13.1(b): show the dearer and say confirm at the meter.
  const pack = fixture({
    meterRates: [
      {
        blockfaceId: "bf-2",
        segmentId: "street-seg-meter",
        side: "E",
        rateLabel: "M1",
        hourRates: ["5.50", "9.00"],
      },
    ],
  });

  const result = one(runSearch(pack), "seg-meter");

  assert.equal(result.money, "10.00");
  assert.equal(result.price_known, false);
  assert.ok(
    result.caveats.includes(
      "More than one meter zone covers this blockface; confirm at the meter.",
    ),
  );
});

test("an unmetered span is free, which is not the same as unpriced", () => {
  const result = one(runSearch(fixture()), "seg-free");

  assert.equal(result.money, "0.00");
  assert.equal(result.price_known, true);
});

test("each result carries its raw sign text and parse metadata", () => {
  const result = one(runSearch(fixture()), "seg-odd");

  assert.deepEqual(result.signs, [
    {
      sign_id: "sign-odd",
      order_number: "order-sign-odd",
      sign_code: "PS-127C",
      sign_description: "NO PARKING EXCEPT ODD SIDE HOLIDAYS PER SW-473",
      parse_method: "unparsed",
      parse_confidence: 0.0,
    },
  ]);
});

test("a span with no signs is no_data with no sign list", () => {
  const result = one(runSearch(fixture()), "seg-blank");

  assert.equal(result.verdict, "no_data");
  assert.deepEqual(result.signs, []);
  assert.equal(result.capacity_cars, 10);
  assert.equal(result.reason, UNKNOWN_GAP_REASON);
  assert.deepEqual(result.caveats, []);
});

test("a placeholder span reports why it has no data and is never priced", () => {
  const pack = fixture({
    spans: [
      {
        regSegId: "seg-gap",
        segmentId: "street-seg-meter",
        side: "W",
        line: lineAt(ORIGIN_LAT + 0.0013),
        derivedFrom: [],
        gapKind: "no_signs",
      },
      {
        regSegId: "seg-unmatched",
        segmentId: "street-seg-unmatched",
        side: "E",
        line: lineAt(ORIGIN_LAT + 0.0014),
        derivedFrom: [],
        gapKind: "unmatched_signs",
      },
    ],
  });

  const found = byId(runSearch(pack));

  assert.equal(found.get("seg-gap").gap_kind, "no_signs");
  assert.equal(found.get("seg-gap").reason, "NYC DOT lists no signs on this stretch");
  assert.equal(
    found.get("seg-unmatched").reason,
    "Signs exist here that CurbCheck could not place",
  );
  assert.equal(found.get("seg-blank").gap_kind, null);
  // "$0.00" and "no meter" on grey curb assert what the data never said (P0-5).
  assert.equal(found.get("seg-gap").money, null);
  assert.equal(found.get("seg-gap").price_known, false);
  assert.equal(found.get("seg-gap").rate_label, null);
  assert.equal(found.get("seg-gap").confidence_shown, false);
});

test("each result carries a human street label", () => {
  const pack = fixture();
  pack.nodes = {
    n: 2,
    nodeId: ["n85", "n86"],
    streetNames: [JSON.stringify(["3 AVENUE", "E 85 ST"]), JSON.stringify(["E 86 ST", "3 AVENUE"])],
  };
  const row = pack.index.segmentById.get("street-seg-meter");
  pack.segments.fromNode[row] = 0;
  pack.segments.toNode[row] = 1;

  assert.equal(
    one(runSearch(pack), "seg-meter").street_name,
    "3 AVENUE, east side, E 85 ST → E 86 ST",
  );
});

test("a corner is never labelled as its own cross street", () => {
  // CSCL writes `E 85 ST` on the node and `E  85 ST` on the segment.
  const pack = fixture();
  pack.nodes = {
    n: 1,
    nodeId: ["n-dup"],
    streetNames: [JSON.stringify(["E  85 ST", "e 85 st", "2 AVENUE"])],
  };
  const row = pack.index.segmentById.get("street-seg-meter");
  pack.segments.streetName[row] = "E  85 ST";
  pack.segments.fromNode[row] = 0;

  // And the run of spaces CSCL carries does not reach the label.
  assert.equal(one(runSearch(pack), "seg-meter").street_name, "E 85 ST, east side, at 2 AVENUE");
});

test("a span whose chain ends name no cross street is still labelled", () => {
  assert.equal(one(runSearch(fixture()), "seg-free").street_name, "3 AVENUE, east side");
});

test("geometry comes back as GeoJSON", () => {
  const result = one(runSearch(fixture()), "seg-meter");

  assert.equal(result.geometry.type, "LineString");
  assert.deepEqual(result.geometry.coordinates[0], [ORIGIN_LON, ORIGIN_LAT + 0.001]);
});

test("walk minutes grow with distance", () => {
  const walk = new Map(runSearch(fixture()).map((r) => [r.reg_seg_id, r.walk_min]));

  assert.ok(walk.get("seg-meter") < walk.get("seg-blank"));
  assert.ok(walk.get("seg-blank") < walk.get("seg-free"));
  assert.ok(Math.abs(walk.get("seg-meter") - 1.8) < 0.3);
});

test("the limit caps the ranked legal list only", () => {
  // docs/VALIDATION.md U1: `limit` must never cost the map its illegal curb.
  const found = run(fixture(), { limit: 1 });

  assert.deepEqual(
    found.legal.map((result) => result.verdict),
    ["legal"],
  );
  assert.deepEqual(
    new Set(found.others.map((result) => result.verdict)),
    new Set(["ambiguous", "illegal", "no_data"]),
  );
});

test("counts are measured before either cap", () => {
  const found = run(fixture(), { limit: 1, mapLimit: 1 });

  assert.equal(found.legal.length, 1);
  assert.equal(found.others.length, 1);
  assert.deepEqual(found.counts, { legal: 1, illegal: 1, ambiguous: 1, no_data: 1, total: 4 });
});

test("the map cap keeps the nearest of the other verdicts", () => {
  // seg-blank at 166 m is nearer than seg-odd at 222 m and seg-free at 333 m.
  const found = run(fixture(), { mapLimit: 1 });

  assert.deepEqual(
    found.others.map((result) => result.reg_seg_id),
    ["seg-blank"],
  );
});

test("the map limit is clamped to its hard maximum", () => {
  const clamped = run(fixture(), { mapLimit: MAX_MAP_LIMIT * 10 }).others;

  assert.deepEqual(
    clamped.map((result) => result.reg_seg_id),
    run(fixture()).others.map((result) => result.reg_seg_id),
  );
});

test("a limit below one is rejected", () => {
  assert.throws(() => run(fixture(), { limit: 0 }), /at least 1/);
  assert.throws(() => run(fixture(), { mapLimit: 0 }), /at least 1/);
});

test("a dense band of legal curb cannot push the illegal off the map", () => {
  // The U1 measurement, in miniature: 150 legal and 50 illegal spans, limit 100.
  const signs = [];
  const spans = [];
  const regulations = [];
  for (let index = 0; index < 150; index += 1) {
    signs.push({ signId: `sign-ok-${index}`, signDescription: "PARKING PERMITTED" });
    spans.push({
      regSegId: `seg-ok-${index}`,
      segmentId: `street-seg-ok-${index}`,
      side: "E",
      line: lineAt(ORIGIN_LAT + 0.0001 + index * 0.000001),
      derivedFrom: [`sign-ok-${index}`],
    });
    regulations.push({
      regId: `reg-ok-${index}`,
      regSegId: `seg-ok-${index}`,
      reg: PERMITTED,
      raw: "PARKING PERMITTED",
    });
  }
  for (let index = 0; index < 50; index += 1) {
    signs.push({ signId: `sign-no-${index}`, signDescription: "NO PARKING ANYTIME" });
    spans.push({
      regSegId: `seg-no-${index}`,
      segmentId: `street-seg-no-${index}`,
      side: "E",
      line: lineAt(ORIGIN_LAT + 0.0002 + index * 0.000001),
      derivedFrom: [`sign-no-${index}`],
    });
    regulations.push({
      regId: `reg-no-${index}`,
      regSegId: `seg-no-${index}`,
      reg: NO_PARKING,
      raw: "NO PARKING ANYTIME",
    });
  }

  const found = run(fixture({ signs, spans, regulations }), { limit: 100 });

  assert.equal(found.legal.length, 100);
  assert.ok(found.legal.every((result) => result.verdict === "legal"));
  assert.equal(found.others.filter((result) => result.verdict === "illegal").length, 51);
  assert.equal(found.counts.legal, 151);
  assert.equal(found.counts.illegal, 51);
  assert.equal(found.counts.total, 204);
});

test("weights change the ranking", () => {
  // With money weighted heavily the priced metered span falls behind a free one.
  const pack = fixture({
    signs: [{ signId: "sign-free-near", signDescription: "PARKING PERMITTED" }],
    spans: [
      {
        regSegId: "seg-free-near",
        segmentId: "street-seg-free-near",
        side: "E",
        line: lineAt(ORIGIN_LAT + 0.0012),
        derivedFrom: ["sign-free-near"],
      },
    ],
    regulations: [
      {
        regId: "reg-free-near",
        regSegId: "seg-free-near",
        reg: PERMITTED,
        raw: "PARKING PERMITTED",
      },
    ],
  });

  const moneyMatters = runSearch(pack, { weights: { walk: 1.0, money: 5.0, risk: 0.0 } });
  const walkMatters = runSearch(pack, { weights: { walk: 5.0, money: 0.0, risk: 0.0 } });

  assert.equal(moneyMatters[0].reg_seg_id, "seg-free-near");
  assert.equal(walkMatters[0].reg_seg_id, "seg-meter");
});

test("each result carries the terms its score is made of", () => {
  const weights = { walk: 1.0, money: 1.0, risk: 0.5 };
  const result = one(runSearch(fixture(), { weights }), "seg-meter");

  assert.equal(result.money_value, 9.13);
  assert.equal(result.risk, 0.65);
  assert.ok(Math.abs(result.score - (result.walk_min + 9.13 + 0.5 * 0.65)) < 1e-12);
});

test("risk grows as the snap confidence falls", () => {
  const pack = fixture();
  pack.spans.confidence[pack.index.spanByRegSegId.get("seg-meter")] = 0.5;

  assert.equal(one(runSearch(pack), "seg-meter").risk, 16.25);
});

test("a no_data span has no price at all", () => {
  const result = one(runSearch(fixture()), "seg-blank");

  assert.equal(result.verdict, "no_data");
  assert.equal(result.money, null);
  assert.equal(result.money_value, null);
  assert.equal(result.price_known, false);
  assert.equal(result.rate_label, null);
  assert.equal(result.confidence_shown, false);
});

test("a posted permission outranks absence at the same cost", () => {
  // Both spans cost the same; the one with a sign to read goes first (D27).
  const pack = fixture({
    signs: [
      { signId: "sign-posted", signDescription: "PARKING PERMITTED" },
      { signId: "sign-weekday", signDescription: "NO PARKING MON-FRI" },
    ],
    spans: [
      {
        regSegId: "seg-posted",
        segmentId: "street-seg-posted",
        side: "E",
        line: lineAt(ORIGIN_LAT + 0.0009),
        derivedFrom: ["sign-posted"],
      },
      {
        regSegId: "seg-absent",
        segmentId: "street-seg-absent",
        side: "E",
        line: lineAt(ORIGIN_LAT + 0.0009),
        derivedFrom: ["sign-weekday"],
      },
    ],
    regulations: [
      { regId: "reg-posted", regSegId: "seg-posted", reg: PERMITTED, raw: "PARKING PERMITTED" },
      { regId: "reg-weekday", regSegId: "seg-absent", reg: WEEKDAY_BAN, raw: "NO PARKING MON-FRI" },
    ],
  });

  const found = run(pack);
  const ranked = found.legal.map((result) => result.reg_seg_id);
  const absent = one(found.legal, "seg-absent");
  const posted = one(found.legal, "seg-posted");

  assert.equal(absent.basis, "absence");
  assert.equal(posted.basis, "posted");
  assert.ok(Math.abs(absent.score - posted.score) < COST_TIE_BAND);
  assert.ok(ranked.indexOf("seg-posted") < ranked.indexOf("seg-absent"));
});

test("a cheaper absence still outranks a dearer posted span", () => {
  // The tie-break is a tie-break: it never reorders spans whose cost differs.
  const pack = fixture({
    signs: [{ signId: "sign-weekday", signDescription: "NO PARKING MON-FRI" }],
    spans: [
      {
        regSegId: "seg-absent",
        segmentId: "street-seg-absent",
        side: "E",
        line: lineAt(ORIGIN_LAT + 0.0002),
        derivedFrom: ["sign-weekday"],
      },
    ],
    regulations: [
      { regId: "reg-weekday", regSegId: "seg-absent", reg: WEEKDAY_BAN, raw: "NO PARKING MON-FRI" },
    ],
  });

  const found = run(pack);

  assert.equal(found.legal[0].reg_seg_id, "seg-absent");
  assert.ok(found.legal[0].score + COST_TIE_BAND < found.legal[1].score);
});

test("a dense run of costs does not chain into one tie", () => {
  // Twenty spans a fifth of a point apart, plus one posted span dearer than all
  // of them: if the bands chained, the posted span would be promoted to the
  // front of a list it is last in.
  const signs = [];
  const spans = [];
  const regulations = [];
  for (let index = 0; index < 20; index += 1) {
    signs.push({ signId: `sign-step-${index}`, signDescription: "NO PARKING MON-FRI" });
    spans.push({
      regSegId: `seg-step-${index}`,
      segmentId: `street-seg-step-${index}`,
      side: "E",
      line: lineAt(ORIGIN_LAT + 0.0002 + index * 0.00005),
      derivedFrom: [`sign-step-${index}`],
    });
    regulations.push({
      regId: `reg-step-${index}`,
      regSegId: `seg-step-${index}`,
      reg: WEEKDAY_BAN,
      raw: "NO PARKING MON-FRI",
    });
  }
  signs.push({ signId: "sign-dearest", signDescription: "PARKING PERMITTED" });
  spans.push({
    regSegId: "seg-dearest",
    segmentId: "street-seg-dearest",
    side: "E",
    line: lineAt(ORIGIN_LAT + 0.0016),
    derivedFrom: ["sign-dearest"],
  });
  regulations.push({
    regId: "reg-dearest",
    regSegId: "seg-dearest",
    reg: PERMITTED,
    raw: "PARKING PERMITTED",
  });

  const found = run(fixture({ signs, spans, regulations }));
  const ranked = found.legal.map((result) => result.reg_seg_id);

  assert.ok(found.legal.slice(0, 20).every((result) => result.basis === "absence"));
  assert.equal(ranked[0], "seg-step-0");
  assert.equal(ranked.indexOf("seg-dearest"), 20);
});

test("an empty pack returns nothing", () => {
  const pack = buildPack({ spans: [] });

  const found = run(pack);

  assert.deepEqual(found.legal, []);
  assert.deepEqual(found.others, []);
  assert.equal(found.counts.total, 0);
});

test("a pre-D25 pack still refuses to call an overlapped span legal", () => {
  // SPEC §8.6: a permissive span reaching over a ban must never read green.
  const lat = ORIGIN_LAT + 0.0012;
  const pack = fixture({
    signs: [
      { signId: "sign-seg-ban", signDescription: "NO STANDING ANYTIME <->", signCode: "PS-2G" },
      { signId: "sign-seg-hmp", signDescription: "2 HMP 8AM-7PM <->" },
    ],
    spans: [
      {
        regSegId: "seg-ban",
        segmentId: "street-seg-meter",
        side: "E",
        line: lineAt(lat, ORIGIN_LON, ORIGIN_LON + 0.0006),
        derivedFrom: ["sign-seg-ban"],
      },
      {
        regSegId: "seg-hmp",
        segmentId: "street-seg-meter",
        side: "E",
        line: lineAt(lat, ORIGIN_LON + 0.0002, ORIGIN_LON + 0.0012),
        derivedFrom: ["sign-seg-hmp"],
      },
    ],
    regulations: [
      {
        regId: "reg-ban",
        regSegId: "seg-ban",
        reg: { action: "stand", permitted: false },
        raw: "NO STANDING ANYTIME <->",
      },
      {
        regId: "reg-hmp",
        regSegId: "seg-hmp",
        reg: METERED_SATURDAY,
        raw: "2 HMP 8AM-7PM <->",
      },
    ],
  });

  const found = byId(runSearch(pack));

  assert.equal(found.get("seg-ban").verdict, "illegal");
  assert.equal(found.get("seg-hmp").verdict, "ambiguous");
  assert.ok(found.get("seg-hmp").reason.includes("conflict"));
  assert.equal(found.get("seg-hmp").basis, null);
  // The span that is not contested keeps its verdict.
  assert.equal(found.get("seg-meter").verdict, "legal");
});

test("spans that only touch at a shared end do not contest each other", () => {
  // Abutting spans tile the curb; treating a shared endpoint as a conflict would
  // make every blockface with two regimes ambiguous.
  const lat = ORIGIN_LAT + 0.0012;
  const pack = fixture({
    signs: [
      { signId: "sign-seg-ban", signDescription: "NO STANDING ANYTIME <->", signCode: "PS-2G" },
      { signId: "sign-seg-hmp", signDescription: "2 HMP 8AM-7PM <->" },
    ],
    spans: [
      {
        regSegId: "seg-ban",
        segmentId: "street-seg-meter",
        side: "E",
        line: lineAt(lat, ORIGIN_LON, ORIGIN_LON + 0.0006),
        derivedFrom: ["sign-seg-ban"],
      },
      {
        regSegId: "seg-hmp",
        segmentId: "street-seg-meter",
        side: "E",
        line: lineAt(lat, ORIGIN_LON + 0.0006, ORIGIN_LON + 0.0012),
        derivedFrom: ["sign-seg-hmp"],
      },
    ],
    regulations: [
      {
        regId: "reg-ban",
        regSegId: "seg-ban",
        reg: { action: "stand", permitted: false },
        raw: "NO STANDING ANYTIME <->",
      },
      { regId: "reg-hmp", regSegId: "seg-hmp", reg: METERED_SATURDAY, raw: "2 HMP 8AM-7PM <->" },
    ],
  });

  assert.equal(one(runSearch(pack), "seg-hmp").verdict, "legal");
});

test("a prohibition on the other side of the street does not contest", () => {
  const lat = ORIGIN_LAT + 0.0012;
  const pack = fixture({
    signs: [
      { signId: "sign-seg-ban", signDescription: "NO STANDING ANYTIME <->", signCode: "PS-2G" },
      { signId: "sign-seg-hmp", signDescription: "2 HMP 8AM-7PM <->" },
    ],
    spans: [
      {
        regSegId: "seg-ban",
        segmentId: "street-seg-meter",
        side: "W",
        line: lineAt(lat, ORIGIN_LON, ORIGIN_LON + 0.0006),
        derivedFrom: ["sign-seg-ban"],
      },
      {
        regSegId: "seg-hmp",
        segmentId: "street-seg-meter",
        side: "E",
        line: lineAt(lat, ORIGIN_LON + 0.0002, ORIGIN_LON + 0.0012),
        derivedFrom: ["sign-seg-hmp"],
      },
    ],
    regulations: [
      {
        regId: "reg-ban",
        regSegId: "seg-ban",
        reg: { action: "stand", permitted: false },
        raw: "NO STANDING ANYTIME <->",
      },
      { regId: "reg-hmp", regSegId: "seg-hmp", reg: METERED_SATURDAY, raw: "2 HMP 8AM-7PM <->" },
    ],
  });

  assert.equal(one(runSearch(pack), "seg-hmp").verdict, "legal");
});

test("a missing calendar puts the caveat on every result", () => {
  // Without the calendar a holiday reads as an ordinary day.
  const pack = fixture();
  pack.calendar.n = 0;

  const results = runSearch(pack);

  assert.ok(results.length > 0);
  for (const result of results) {
    assert.ok(result.caveats.includes(CALENDAR_MISSING_CAVEAT));
  }
});

test("a pack with a calendar carries no calendar caveat", () => {
  for (const result of runSearch(fixture())) {
    assert.equal(result.caveats.includes(CALENDAR_MISSING_CAVEAT), false);
  }
});
