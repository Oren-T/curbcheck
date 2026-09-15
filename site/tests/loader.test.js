/**
 * The loader over the fixture pack in `fixtures/pack/`, which
 * `tests/test_pack.py` compiles from a hand-built database and keeps in step.
 *
 * The fixture's shape, because every expectation below counts on it:
 * segments seg-3, seg-1, seg-2; spans span-b (on seg-3), span-a and span-d (on
 * seg-1) and span-c (a placeholder on no segment); signs sign-2 (on seg-1),
 * sign-1 (on seg-3) and sign-3 (never snapped); rules reg-2 (on span-a) and
 * reg-1 (on span-b). Ids sort the other way round from their rowids on
 * purpose.
 */

import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";

import { loadPack, lineString } from "../static/pack/loader.js";
import { CELL_LAT_DEG, CELL_LON_DEG } from "../static/pack/grid.js";
import { readPackText } from "./helpers/read_pack.js";

const FIXTURE_DIR = fileURLToPath(new URL("./fixtures/pack/", import.meta.url));

const pack = await loadPack(readPackText(FIXTURE_DIR));

test("meta.json arrives as it was written", () => {
  assert.equal(pack.meta.format, 1);
  assert.deepEqual(pack.meta.counts, { spans: 4, regulations: 2, signs: 3, segments: 3 });
  assert.equal(pack.meta.coverage.area, "Manhattan");
  assert.equal(pack.meta.sync.calendar_source_path, undefined);
});

test("every table keeps the row order the pack was written in", () => {
  assert.equal(pack.spans.n, 4);
  assert.deepEqual(pack.spans.regSegId, ["span-b", "span-a", "span-d", "span-c"]);
  assert.deepEqual(pack.segments.segmentId, ["seg-3", "seg-1", "seg-2"]);
  assert.deepEqual(pack.signs.signId, ["sign-2", "sign-1", "sign-3"]);
  assert.deepEqual(pack.regulations.regId, ["reg-2", "reg-1"]);
  assert.deepEqual(pack.geocode.streetVariants.variant, ["3 av", "3 ave", "e 86 st"]);
});

test("a numeric column with no nulls becomes a typed array", () => {
  assert.ok(pack.spans.segment instanceof Int32Array);
  assert.deepEqual(Array.from(pack.spans.segment), [0, 1, 1, -1]);
  assert.ok(pack.spans.confidence instanceof Float64Array);
  assert.ok(pack.regulations.permitted instanceof Uint8Array);
  assert.ok(pack.geocode.addressPoints.lat instanceof Float64Array);
  assert.equal(pack.geocode.addressPoints.lat[0], 40.77814);
});

test("a column that can be null keeps its nulls", () => {
  assert.deepEqual(pack.segments.widthFt, [34.0, 34.0, null]);
  assert.deepEqual(pack.regulations.timeFrom, ["08:00", null]);
  assert.deepEqual(pack.spans.segmentId, ["seg-3", "seg-1", "seg-1", null]);
});

test("dictionary columns arrive as the strings they encode", () => {
  assert.deepEqual(pack.signs.onStreet, ["3 AVENUE", "3 AVENUE", "3 AVENUE"]);
  assert.deepEqual(pack.signs.signCode, ["NP", "NP", null]);
  assert.deepEqual(pack.spans.gapKind, [null, null, null, "no_signs"]);
  assert.equal(pack.regulations.rawSignDescription[0], "NO PARKING 8AM-9:30AM MON");
  // Shared, not copied: the sign and the rule quote the same string object.
  assert.ok(pack.regulations.rawSignDescription[0] === pack.signs.signDescription[0]);
});

test("a lists column holds the rows it points at", () => {
  const { lists, offsets } = pack.spans.derivedFrom;

  assert.deepEqual(Array.from(offsets), [0, 1, 3, 3, 3]);
  assert.deepEqual(Array.from(lists.subarray(offsets[1], offsets[2])), [0, 1]);
  assert.equal(pack.meta.dropped_sign_refs, 1);
});

test("flags are a bitmask in model.Flags declaration order", () => {
  const streetCleaning = 1 << 0;
  const meta = 1 << 7;

  assert.deepEqual(Array.from(pack.regulations.flags), [streetCleaning | meta, 0]);
});

test("the id maps answer with row numbers", () => {
  assert.equal(pack.index.spanByRegSegId.get("span-a"), 1);
  assert.equal(pack.index.spanByRegSegId.get("span-404"), undefined);
  assert.equal(pack.index.segmentById.get("seg-2"), 2);
  assert.equal(pack.index.nodeById.get("node-a"), 1);
  assert.equal(pack.index.signById.get("sign-3"), 2);
});

test("the grouped indexes are in row order and empty where nothing points", () => {
  assert.deepEqual(Array.from(pack.index.regulationsBySpan[0]), [1]);
  assert.deepEqual(Array.from(pack.index.regulationsBySpan[1]), [0]);
  assert.equal(pack.index.regulationsBySpan[3].length, 0);
  assert.ok(pack.index.regulationsBySpan[3] instanceof Int32Array);

  assert.deepEqual(Array.from(pack.index.signsBySegment[0]), [1]);
  assert.deepEqual(Array.from(pack.index.signsBySegment[1]), [0]);
  assert.equal(pack.index.signsBySegment[2].length, 0);

  assert.deepEqual(Array.from(pack.index.meterRatesBySegment[0]), [0]);
  assert.equal(pack.index.meterRatesBySegment[1].length, 0);
});

test("a geometry column reads back as the LineString it was", () => {
  assert.deepEqual(lineString(pack.spans, 1), {
    type: "LineString",
    coordinates: [
      [-73.96, 40.785],
      [-73.9596, 40.78501249999999],
    ],
  });
  assert.equal(pack.lineString, lineString);
  assert.equal(lineString(pack.segments, 0).coordinates.length, 3);
});

test("bounding boxes are derived from the coordinates", () => {
  assert.equal(pack.spans.bbox.minLon[1], -73.96);
  assert.equal(pack.spans.bbox.maxLon[1], -73.9596);
  assert.equal(pack.spans.bbox.minLat[1], 40.785);
  assert.equal(pack.spans.bbox.maxLat[1], 40.78501249999999);
  assert.equal(pack.segments.bbox.maxLat[2], 40.790025);
});

test("the grid cell is about 100 m at Manhattan's latitude", () => {
  assert.ok(Math.abs(CELL_LAT_DEG * 111132 - 100) < 1e-9);
  assert.ok(Math.abs(CELL_LON_DEG - 0.0011863) < 1e-6);
});

test("the grid answers with a superset of the rows in the box, in row order", () => {
  const near = pack.index.spanGrid.query(-73.9601, 40.7849, -73.9595, 40.7851);

  assert.ok(near instanceof Int32Array);
  assert.ok(near.includes(1) && near.includes(2), "the two spans at 40.7850");
  assert.deepEqual(
    Array.from(near),
    Array.from(near)
      .slice()
      .sort((a, b) => a - b),
  );
  assert.ok(!near.includes(3), "the span 550 m north is in another cell");
});

test("the grid covers every row when the box covers the data", () => {
  const all = pack.index.spanGrid.query(-74.1, 40.6, -73.8, 40.9);
  const segments = pack.index.segmentGrid.query(-74.1, 40.6, -73.8, 40.9);

  assert.deepEqual(Array.from(all), [0, 1, 2, 3]);
  assert.deepEqual(Array.from(segments), [0, 1, 2]);
});

test("the grid answers nothing for a box outside the data", () => {
  const away = pack.index.spanGrid.query(-74.05, 40.7, -74.04, 40.71);

  assert.equal(away.length, 0);
});

test("the geocode tables are all present", () => {
  assert.deepEqual(Object.keys(pack.geocode).sort(), [
    "addressPoints",
    "intersections",
    "placeTokens",
    "places",
    "streetTokens",
    "streetVariants",
    "streets",
    "zipCentroids",
  ]);
  assert.deepEqual(pack.geocode.addressPoints.streetNorm, ["3 AV", "3 AV"]);
  assert.deepEqual(Array.from(pack.geocode.places.placeId), [3, 7]);
  assert.equal(pack.geocode.zipCentroids.zipcode[0], "10028");
  assert.deepEqual(pack.geocode.placeTokens.searchName, [
    "carl schurz park",
    "guggenheim museum",
    "guggenheim museum",
  ]);
});

test("a pack of another format is refused", async () => {
  const readText = readPackText(FIXTURE_DIR);
  const wrongFormat = async (name) => {
    const text = await readText(name);
    return name === "meta.json" ? JSON.stringify({ ...JSON.parse(text), format: 99 }) : text;
  };

  await assert.rejects(() => loadPack(wrongFormat), /format 99/);
});
