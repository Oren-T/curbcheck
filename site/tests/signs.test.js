/**
 * The two sign groups behind one span.
 *
 * The cases in `tests/test_api.py` that exercise `engine/signs.py`: curb order,
 * a placeholder's empty `governing`, and the signs an `unmatched_signs`
 * placeholder finds by name because they never snapped to a centerline.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { blockfaceSigns } from "../static/engine/signs.js";

const XSS_SIGN_TEXT = "2 HOUR PARKING <script>alert('x')</script> 9AM-7PM";

function column(rows, name, fallback = null) {
  return rows.map((row) => (row[name] === undefined ? fallback : row[name]));
}

/** The 3 AVE / E 85 ST fixture block, as much of it as `signs.js` reads. */
function buildPack({ signs, spans, gapKind = null }) {
  const signRow = new Map(signs.map((sign, row) => [sign.signId, row]));
  const spanRow = new Map(spans.map((span, row) => [span.regSegId, row]));
  const onSegment = signs
    .map((sign, row) => (sign.segmentId === "3681" ? row : -1))
    .filter((row) => row >= 0);

  return {
    spans: {
      n: spans.length,
      regSegId: column(spans, "regSegId"),
      gapKind: spans.map((span) => (span.gapKind === undefined ? gapKind : span.gapKind)),
    },
    signs: {
      n: signs.length,
      signId: column(signs, "signId"),
      orderNumber: column(signs, "orderNumber", "1-11111"),
      signCode: column(signs, "signCode", "PRK-9"),
      signDescription: column(signs, "signDescription"),
      onStreet: column(signs, "onStreet", "3 AVENUE"),
      fromStreet: column(signs, "fromStreet", "EAST 85 STREET"),
      toStreet: column(signs, "toStreet", "EAST 86 STREET"),
      sideOfStreet: column(signs, "sideOfStreet", "W"),
      distanceFromIntersection: column(signs, "distance", 44.0),
      arrowDirection: column(signs, "arrow"),
      snapConfidence: column(signs, "snapConfidence", 0.94),
      snapNotes: column(signs, "snapNotes", ""),
      isRegulation: Uint8Array.from(signs, () => 1),
      panelClass: column(signs, "panelClass", "regulation"),
      segmentId: column(signs, "segmentId"),
    },
    segments: {
      n: 1,
      segmentId: ["3681"],
      streetNorm: ["3 AVE"],
      fromNode: Int32Array.from([0]),
      toNode: Int32Array.from([1]),
    },
    nodes: {
      n: 2,
      streetNames: [JSON.stringify(["3 AVE", "E 85 ST"]), JSON.stringify(["3 AVE", "E 86 ST"])],
    },
    index: {
      spanByRegSegId: spanRow,
      signById: signRow,
      signsBySegment: [Int32Array.from(onSegment)],
    },
  };
}

const FIXTURE_SIGNS = [
  { signId: "sign-1", signDescription: XSS_SIGN_TEXT, segmentId: "3681" },
  { signId: "sign-2", signDescription: "NO STANDING ANYTIME", segmentId: "3681", distance: 232.0 },
];

function fixture(extraSigns = [], span = { regSegId: "3681:W:0" }) {
  return buildPack({ signs: [...FIXTURE_SIGNS, ...extraSigns], spans: [span] });
}

const described = (group) => group.map((sign) => sign.sign_description);

test("governing is the span's own derived_from and the rest is the block", () => {
  const pack = fixture();

  const { governing, otherOnBlock } = blockfaceSigns(
    pack,
    "3681:W:0",
    Int32Array.from([0]),
    0,
    "W",
  );

  assert.deepEqual(described(governing), [XSS_SIGN_TEXT]);
  assert.deepEqual(described(otherOnBlock), ["NO STANDING ANYTIME"]);
});

test("a sign detail carries the distance under both names and DOT's arrow word", () => {
  const pack = fixture();

  const [sign] = blockfaceSigns(pack, "3681:W:0", Int32Array.from([0]), 0, "W").governing;

  assert.deepEqual(sign, {
    sign_id: "sign-1",
    order_number: "1-11111",
    sign_code: "PRK-9",
    sign_description: XSS_SIGN_TEXT,
    on_street: "3 AVENUE",
    from_street: "EAST 85 STREET",
    to_street: "EAST 86 STREET",
    side_of_street: "W",
    distance_from_intersection: 44.0,
    distance_ft: 44.0,
    arrow: null,
    snap_confidence: 0.94,
    snap_notes: "",
    is_regulation: true,
    panel_class: "regulation",
  });
});

test("governing signs come back in curb order, not by id", () => {
  // SPEC §10's sign list is read standing on the pavement.
  const pack = fixture([
    {
      signId: "aaa-first-by-id",
      signDescription: "2 HOUR PARKING 9AM-7PM",
      segmentId: "3681",
      distance: 120.0,
      arrow: "N",
    },
  ]);

  const { governing } = blockfaceSigns(pack, "3681:W:0", Int32Array.from([0, 2]), 0, "W");

  assert.deepEqual(
    governing.map((sign) => sign.distance_ft),
    [44.0, 120.0],
  );
  assert.equal(governing[1].arrow, "N");
});

test("a sign with no measured distance sorts last, then by id", () => {
  const pack = fixture([
    { signId: "zzz-unmeasured", signDescription: "PARKING", segmentId: "3681", distance: null },
    { signId: "aaa-unmeasured", signDescription: "PARKING", segmentId: "3681", distance: null },
  ]);

  const { governing } = blockfaceSigns(pack, "3681:W:0", Int32Array.from([0, 2, 3]), 0, "W");

  assert.deepEqual(
    governing.map((sign) => sign.sign_id),
    ["sign-1", "aaa-unmeasured", "zzz-unmeasured"],
  );
});

test("a span with no centerline segment has no signs to show", () => {
  const pack = fixture();

  assert.deepEqual(blockfaceSigns(pack, "3681:W:0", Int32Array.from([]), -1, "W"), {
    governing: [],
    otherOnBlock: [],
  });
});

test("with no governing sign the block is every sign on the same side letter", () => {
  const pack = fixture([
    {
      signId: "other-side",
      signDescription: "NO PARKING",
      segmentId: "3681",
      sideOfStreet: "E",
      distance: 10.0,
    },
  ]);

  const { governing, otherOnBlock } = blockfaceSigns(pack, "3681:W:0", Int32Array.from([]), 0, "W");

  assert.deepEqual(governing, []);
  assert.deepEqual(described(otherOnBlock), [XSS_SIGN_TEXT, "NO STANDING ANYTIME"]);
});

test("a placeholder span has no governing signs and shows the block beside it", () => {
  // A grey span asserts nothing: no sign of its own, and the block's signs next to it.
  const pack = fixture([], { regSegId: "3681:W:gap", gapKind: "no_signs" });

  const { governing, otherOnBlock } = blockfaceSigns(
    pack,
    "3681:W:gap",
    Int32Array.from([0]),
    0,
    "W",
  );

  assert.deepEqual(governing, []);
  assert.deepEqual(
    new Set(described(otherOnBlock)),
    new Set([XSS_SIGN_TEXT, "NO STANDING ANYTIME"]),
  );
});

test("an unmatched placeholder lists the signs that never snapped", () => {
  // 332 of 499 unmatched blockface-sides carry a NO STANDING sign (VALIDATION §5).
  const pack = buildPack({
    signs: [
      ...FIXTURE_SIGNS,
      {
        signId: "sign-unplaced",
        signDescription: "NO STANDING ANYTIME",
        segmentId: null,
        sideOfStreet: "E",
        distance: 10.0,
        snapNotes: "unmatched: no_geometry",
      },
    ],
    spans: [{ regSegId: "3681:E:gap", gapKind: "unmatched_signs" }],
  });

  const { governing, otherOnBlock } = blockfaceSigns(
    pack,
    "3681:E:gap",
    Int32Array.from([]),
    0,
    "E",
  );

  assert.deepEqual(governing, []);
  assert.deepEqual(
    otherOnBlock.map((sign) => sign.sign_id),
    ["sign-unplaced"],
  );
});

test("an unmatched sign on another street is not pulled onto this blockface", () => {
  const pack = buildPack({
    signs: [
      ...FIXTURE_SIGNS,
      {
        signId: "sign-elsewhere",
        signDescription: "NO STANDING ANYTIME",
        segmentId: null,
        sideOfStreet: "E",
        onStreet: "LEXINGTON AVENUE",
        distance: 10.0,
      },
    ],
    spans: [{ regSegId: "3681:E:gap", gapKind: "unmatched_signs" }],
  });

  const { otherOnBlock } = blockfaceSigns(pack, "3681:E:gap", Int32Array.from([]), 0, "E");

  assert.deepEqual(otherOnBlock, []);
});

test("a plain no_signs placeholder never reaches the unmatched pass", () => {
  const pack = buildPack({
    signs: [
      {
        signId: "sign-unplaced",
        signDescription: "NO STANDING ANYTIME",
        segmentId: null,
        sideOfStreet: "E",
        distance: 10.0,
      },
    ],
    spans: [{ regSegId: "3681:E:gap", gapKind: "no_signs" }],
  });

  const { otherOnBlock } = blockfaceSigns(pack, "3681:E:gap", Int32Array.from([]), 0, "E");

  assert.deepEqual(otherOnBlock, []);
});
