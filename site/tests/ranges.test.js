/**
 * House numbers placed along a segment's published range.
 *
 * `curbcheck/engine/ranges.py` has no test file of its own — it is covered
 * through the address ladder in `tests/test_geocode.py` — so the expected
 * points below were read out of the Python with shapely and are pasted here to
 * every digit: `point_along` is the one place the JS has to reproduce a GEOS
 * traversal rather than arithmetic the two languages share.
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  Blockface,
  _crossStreets,
  blockfaces,
  houseInt,
  lineOf,
  pointAlong,
} from "../static/engine/ranges.js";

/** The one segment the point tests walk, and shapely's answers for it. */
const BENT_LINE = [
  [-73.98, 40.75],
  [-73.975, 40.752],
  [-73.97, 40.758],
];
const SHAPELY_POINTS = [
  [-0.5, [-73.98, 40.75]],
  [0.0, [-73.98, 40.75]],
  [0.25, [-73.97693709130648, 40.75122516347741]],
  [0.5, [-73.97422374924957, 40.75293150090051]],
  [0.75, [-73.97211187462479, 40.755465750450256]],
  [1.0, [-73.97, 40.758]],
  [1.5, [-73.97, 40.758]],
];

function coordsColumn(lines) {
  const offsets = new Uint32Array(lines.length + 1);
  let total = 0;
  for (const [row, line] of lines.entries()) {
    total += line.length;
    offsets[row + 1] = total;
  }
  const coords = new Float64Array(total * 2);
  let at = 0;
  for (const line of lines) {
    for (const [lon, lat] of line) {
      coords[at] = lon;
      coords[at + 1] = lat;
      at += 2;
    }
  }
  return { coords, offsets };
}

/**
 * Two blocks of one street, the first with a range on both sides and a corner
 * at each end, the second with a range on one side and no node at its far end.
 */
function streetPack() {
  const lines = [
    [
      [-73.9544835, 40.7781469],
      [-73.953985, 40.7788304],
    ],
    [
      [-73.953985, 40.7788304],
      [-73.953, 40.7795],
    ],
    BENT_LINE,
  ];
  const pack = {
    segments: {
      n: 3,
      segmentId: ["3681", "3682", "9200"],
      streetName: ["3  AVE", "3 AVE", "AVE OF THE AMERICAS"],
      streetNorm: ["3 AVE", "3 AVE", "AVE OF THE AMERICAS"],
      fromNode: [0, 1, -1],
      toNode: [1, -1, -1],
      leftLowAddress: ["1510", "1530", null],
      leftHighAddress: ["1528", "1548", null],
      rightLowAddress: ["1509", null, null],
      rightHighAddress: ["1525", null, null],
      geom: coordsColumn(lines),
    },
    nodes: {
      n: 2,
      nodeId: ["a", "b"],
      lon: [-73.9544835, -73.953985],
      lat: [40.7781469, 40.7788304],
      streetNames: ['["3 AVE", "E 85 ST"]', '["3  AVE", "E 86 ST"]'],
    },
  };
  const index = {
    segmentsByNorm: new Map([
      ["3 AVE", Int32Array.from([0, 1])],
      ["AVE OF THE AMERICAS", Int32Array.from([2])],
    ]),
  };
  return { pack, index };
}

test("a blockface claims only its own parity", () => {
  // NYC puts odd numbers on one side and even on the other, so a range whose
  // ends agree on parity only claims numbers of that parity.
  const even = new Blockface({ low: 1510, high: 1528 });

  assert.equal(even.contains(1518), true);
  assert.equal(even.contains(1517), false);
  assert.equal(even.contains(1530), false);
});

test("a range whose ends disagree on parity is a data error and claims both", () => {
  const broken = new Blockface({ low: 1510, high: 1529 });

  assert.equal(broken.contains(1517), true);
  assert.equal(broken.contains(1518), true);
});

test("a one-number range places its number in the middle", () => {
  assert.equal(new Blockface({ low: 12, high: 12 }).position(12), 0.5);
  assert.equal(new Blockface({ low: 2, high: 20 }).position(10), (10 - 2) / (20 - 2));
});

test("both sides of every segment that publishes a range come back, in rowid order", () => {
  const { pack, index } = streetPack();

  const faces = blockfaces(pack, index, "3 AVE");

  assert.deepEqual(
    faces.map((face) => [face.segmentId, face.low, face.high]),
    [
      ["3681", 1510, 1528],
      ["3681", 1509, 1525],
      ["3682", 1530, 1548],
    ],
  );
});

test("a segment with no published range on either side is skipped", () => {
  const { pack, index } = streetPack();

  assert.deepEqual(blockfaces(pack, index, "AVE OF THE AMERICAS"), []);
});

test("a blockface carries the cross streets of its own two corners", () => {
  const { pack, index } = streetPack();

  const [first, , second] = blockfaces(pack, index, "3 AVE");

  // The node writes `3 AVE` and the segment `3  AVE`; compared on collapsed
  // whitespace, so neither corner is labelled as its own street.
  assert.equal(first.crossStreets, "E 85 ST → E 86 ST");
  assert.equal(second.crossStreets, "at E 86 ST");
});

test("point_along walks the line the way GEOS does", () => {
  for (const [position, expected] of SHAPELY_POINTS) {
    assert.deepEqual(pointAlong(BENT_LINE, position), expected, String(position));
  }
});

test("a geometry with no vertices has no point along it", () => {
  assert.equal(pointAlong(null, 0.5), null);
  assert.equal(lineOf({ geom: coordsColumn([[]]) }, 0), null);
});

test("house_int reads the leading digits of a house-number cell", () => {
  assert.equal(houseInt("1510"), 1510);
  assert.equal(houseInt("1510 REAR"), 1510);
  assert.equal(houseInt("  42x"), 42);
  assert.equal(houseInt(""), null);
  assert.equal(houseInt(null), null);
});

test("a segment with no node at an end names the one cross street it has", () => {
  const { pack } = streetPack();

  assert.equal(_crossStreets(pack, 2), null);
});
