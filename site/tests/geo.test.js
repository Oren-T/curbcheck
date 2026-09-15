/**
 * The local projection and the coverage test built on it.
 *
 * `geo.js` is the function the differential harness is most sensitive to: every
 * radius query, every rank and every `outside_coverage` answer rests on it, so
 * the cases here pin the formula rather than a tolerance. The coverage cases are
 * `tests/test_engine_coverage.py`, over a hand-built pack instead of a database.
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  M_PER_DEG_LAT,
  degreePadding,
  measure,
  metersPerDegreeLon,
  pointToSegment,
  toLocalMeters,
} from "../static/engine/geo.js";
import { COVERAGE_RADIUS_M, coverageBbox, withinCoverage } from "../static/engine/coverage.js";

// 3 AVE between E 85 ST and E 86 ST, the worked example in docs/DATA.md §2.3.
const NODE_85 = [-73.9544835, 40.7781469];
const NODE_86 = [-73.953985, 40.7788304];
// One degree of latitude is 111,132 m here, so 0.0100 deg is 1,111 m north.
const FAR_NORTH = [NODE_85[0], NODE_85[1] + 0.01];

/** A pack holding nothing but `street_segment`, with a grid that answers everything. */
function packOfSegments(lines) {
  const coords = [];
  const offsets = [0];
  const minLon = [];
  const minLat = [];
  const maxLon = [];
  const maxLat = [];
  for (const line of lines) {
    for (const [lon, lat] of line) {
      coords.push(lon, lat);
    }
    offsets.push(coords.length / 2);
    const lons = line.map((point) => point[0]);
    const lats = line.map((point) => point[1]);
    minLon.push(Math.min(...lons, Infinity));
    minLat.push(Math.min(...lats, Infinity));
    maxLon.push(Math.max(...lons, -Infinity));
    maxLat.push(Math.max(...lats, -Infinity));
  }
  const rows = Int32Array.from(lines.map((_, row) => row));
  return {
    segments: {
      n: lines.length,
      geom: { coords: Float64Array.from(coords), offsets: Uint32Array.from(offsets) },
      bbox: {
        minLon: Float64Array.from(minLon),
        minLat: Float64Array.from(minLat),
        maxLon: Float64Array.from(maxLon),
        maxLat: Float64Array.from(maxLat),
      },
    },
    // The grid is allowed to answer with a superset; the caller re-checks the box.
    index: { segmentGrid: { query: () => rows } },
  };
}

const oneBlock = () => packOfSegments([[NODE_85, NODE_86]]);
const southOf = (point, meters) => [point[0], point[1] - meters / M_PER_DEG_LAT];

test("metres per degree of longitude shrink with the cosine of the latitude", () => {
  assert.equal(metersPerDegreeLon(0), 111_320.0);
  assert.equal(metersPerDegreeLon(40.75), 111_320.0 * Math.cos(40.75 * (Math.PI / 180)));
});

test("the padding is the half-width of a box that many metres across", () => {
  const pad = degreePadding(40.75, 250);
  assert.equal(pad.lat, 250 / M_PER_DEG_LAT);
  assert.equal(pad.lon, 250 / metersPerDegreeLon(40.75));
});

test("a point before the start of a segment measures to the start", () => {
  assert.equal(pointToSegment(-3, 4, 0, 0, 10, 0), 5);
});

test("a point past the end of a segment measures to the end", () => {
  assert.equal(pointToSegment(13, -4, 0, 0, 10, 0), 5);
});

test("a point beside a segment measures perpendicular to it", () => {
  assert.equal(pointToSegment(4, 3, 0, 0, 10, 0), 3);
  assert.equal(pointToSegment(4, -3, 0, 0, 10, 0), 3);
});

test("a zero-length segment measures as its own endpoint, as GEOS does", () => {
  assert.equal(pointToSegment(3, 4, 1, 1, 1, 1), Math.sqrt(2 * 2 + 3 * 3));
});

test("a geometry row is projected to metres from the query point", () => {
  const pack = oneBlock();
  const local = toLocalMeters(pack.segments.geom, 0, NODE_85[0], NODE_85[1]);

  assert.deepEqual([local[0], local[1]], [0, 0]);
  assert.ok(Math.abs(local[3] - (NODE_86[1] - NODE_85[1]) * M_PER_DEG_LAT) < 1e-9);
});

test("a row with fewer than two vertices cannot be read", () => {
  const pack = packOfSegments([[NODE_85]]);

  assert.equal(toLocalMeters(pack.segments.geom, 0, NODE_85[0], NODE_85[1]), null);
  assert.equal(measure(pack.segments.geom, 0, NODE_85[0], NODE_85[1]), null);
});

test("a row past the end of the column cannot be read", () => {
  const pack = oneBlock();

  assert.equal(measure(pack.segments.geom, 7, NODE_85[0], NODE_85[1]), null);
});

test("the distance to a block is measured in metres", () => {
  const pack = oneBlock();
  const measured = measure(pack.segments.geom, 0, ...southOf(NODE_85, 200));

  assert.ok(Math.abs(measured.distanceM - 200) < 0.5);
});

test("a point on the street is in coverage", () => {
  assert.equal(withinCoverage(oneBlock(), NODE_85[0], NODE_85[1]), true);
});

test("a point just inside the radius is in coverage", () => {
  // 200 m off the block: a park interior or a river pier, still answerable.
  assert.equal(withinCoverage(oneBlock(), ...southOf(NODE_85, 200)), true);
});

test("a point past the radius is out of coverage", () => {
  assert.equal(withinCoverage(oneBlock(), ...southOf(NODE_85, 300)), false);
});

test("a point a kilometre away is out of coverage", () => {
  // The New Jersey case: nothing in the prefilter box at all.
  assert.equal(withinCoverage(oneBlock(), FAR_NORTH[0], FAR_NORTH[1]), false);
});

test("the radius is the one the module publishes", () => {
  assert.equal(withinCoverage(oneBlock(), ...southOf(NODE_85, COVERAGE_RADIUS_M - 5)), true);
  assert.equal(withinCoverage(oneBlock(), ...southOf(NODE_85, COVERAGE_RADIUS_M + 5)), false);
});

test("an unreadable geometry does not put a point in coverage", () => {
  // data/ is untrusted at read time (CLAUDE.md): a broken row answers nothing.
  const pack = packOfSegments([[NODE_85]]);

  assert.equal(withinCoverage(pack, NODE_85[0], NODE_85[1]), false);
});

test("an empty pack covers nothing", () => {
  const pack = packOfSegments([]);

  assert.equal(withinCoverage(pack, NODE_85[0], NODE_85[1]), false);
  assert.equal(coverageBbox(pack), null);
});

test("the bbox spans every centerline", () => {
  const pack = packOfSegments([
    [NODE_85, NODE_86],
    [NODE_86, FAR_NORTH],
  ]);

  assert.deepEqual(coverageBbox(pack), [
    Math.min(NODE_85[0], NODE_86[0]),
    Math.min(NODE_85[1], NODE_86[1]),
    Math.max(NODE_85[0], NODE_86[0], FAR_NORTH[0]),
    Math.max(FAR_NORTH[1], NODE_86[1]),
  ]);
});

test("a point near the far end of a long segment is in coverage", () => {
  // A bridge centerline is a kilometre of curb in one row, so a point at its far
  // end is 1,111 m past where that row's bounding box starts.
  const pack = packOfSegments([
    [NODE_85, NODE_86],
    [NODE_85, FAR_NORTH],
  ]);

  assert.equal(withinCoverage(pack, ...southOf(FAR_NORTH, 50)), true);
});

test("a radius argument narrows the test", () => {
  assert.equal(withinCoverage(oneBlock(), ...southOf(NODE_85, 100), 50), false);
  assert.equal(withinCoverage(oneBlock(), ...southOf(NODE_85, 100), 150), true);
});
