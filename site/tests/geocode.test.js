/**
 * The suggester over a synthetic index: the ladder, the folds, and hostile input.
 *
 * The cases are `tests/test_geocode.py`'s, and the fixture is that file's
 * `conn()` rebuilt as a pack: the same hand-built Upper East Side block, the
 * same doors, the same places, so the numbers in the assertions can still be
 * checked by hand. The vocabulary index between the two is built here the way
 * `etl.addresses.build_address_index` builds it in SQLite, because that is what
 * `curbcheck pack` ships and there is no ETL in the browser.
 *
 * `tests/test_geocode_real.py`'s cases, which only mean something against the
 * real database, live in the differential harness instead.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { buildGrid } from "../static/pack/grid.js";
import {
  CARDINALS,
  NAME_ALIASES,
  fold,
  nameWords,
  spellingWords,
  streetVariants,
} from "../static/geocode/normalize.js";
import { MAX_QUERY_CHARS, parseQuery } from "../static/geocode/query.js";
import { GeocodeKind } from "../static/geocode/candidates.js";
import { buildGeocodeIndex } from "../static/geocode/index.js";
import { _driverRows } from "../static/geocode/names.js";
import { _inCoverage, geocode, suggest } from "../static/geocode/suggest.js";
import { REVERSE_ADDRESS_MAX_M, reverseGeocode } from "../static/geocode/reverse.js";
import { M_PER_DEG_LAT, metersPerDegreeLon } from "../static/engine/geo.js";

// --- the fixture ----------------------------------------------------------

// A synthetic Upper East Side block, modelled on the worked example in
// docs/DATA.md §2.3: 3 AVE runs south to north between E 85 ST and E 86 ST,
// odd house numbers on the east side and even on the west.
const NODE_85 = [-73.9544835, 40.7781469];
const NODE_86 = [-73.953985, 40.7788304];
const NODE_87 = [-73.953, 40.7795];
const NODE_LEX_85 = [-73.956, 40.7779];

// Surveyed doors on the odd (east) side. 1519 is deliberately absent: it is the
// real gap the interpolation rung exists for (docs/DATA.md §5.1).
const ODD_DOORS = [
  [1509, 0.0],
  [1517, 0.5],
  [1529, 1.0],
];
// The even (west) side, offset so the two sides are distinguishable. 1518 is
// absent and 1528 is present, so an even number interpolates between 1510 and
// 1528 and never snaps across the street to 1517.
const EVEN_DOORS = [
  [1510, 0.02],
  [1528, 0.98],
];
const EVEN_SIDE_LON_OFFSET = -0.00005;

// `etl.addresses.NICKNAMES` and the two place tables, which the geocoder never
// reads but the index it reads is built from.
const NICKNAMES = {
  LEX: "LEXINGTON AVE",
  MAD: "MADISON AVE",
  BWAY: "BROADWAY",
  "B WAY": "BROADWAY",
  FDR: "FRANKLIN D ROOSEVELT DR",
  "WEST SIDE HWY": "W ST",
  "MLK BLVD": "W 125 ST",
  CPW: "CENTRAL PARK W",
  "ACP BLVD": "ADAM CLAYTON POWELL JR BLVD",
  AMSTERDAM: "AMSTERDAM AVE",
  COLUMBUS: "COLUMBUS AVE",
  PARK: "PARK AVE",
  PAS: "PARK AVE S",
  RSD: "RIVERSIDE DR",
  "MARTIN LUTHER KING JR BLVD": "W 125 ST",
};
const PLACE_ALIASES = {
  MOMA: "MUSEUM OF MODERN ART (MOMA)",
  "THE MET": "METROPOLITAN MUSEUM OF ART",
  "MET MUSEUM": "METROPOLITAN MUSEUM OF ART",
  "PORT AUTHORITY": "PORT AUTHORITY BUS TERMINAL",
  "GRAND CENTRAL": "GRAND CENTRAL TERMINAL",
  MSG: "MADISON SQUARE GARDEN",
};
const PLACE_SHORTHAND = [
  [["PUBLIC", "SCHOOL"], "PS"],
  [["HIGH", "SCHOOL"], "HS"],
  [["MIDDLE", "SCHOOL"], "MS"],
];

function between(start, end, fraction) {
  return [start[0] + (end[0] - start[0]) * fraction, start[1] + (end[1] - start[1]) * fraction];
}

function nodeId(lonlat) {
  return `${lonlat[0].toFixed(7)},${lonlat[1].toFixed(7)}`;
}

function addressRow(house, street, position, zipcode = "10028") {
  return { house_number: house, full_street_name: street, zipcode, coordinates: position };
}

/** `etl.addresses._middle_vertex` for the LineStrings this fixture builds. */
function middleVertex(coordinates) {
  return coordinates[Math.floor(coordinates.length / 2)];
}

/** `etl.addresses._street_points`: the vertex nearest the mean of the midpoints. */
function streetPoints(segments) {
  const displays = new Map();
  const midpoints = new Map();
  for (const segment of segments) {
    const point = middleVertex(segment.coordinates);
    if (!displays.has(segment.streetName)) {
      displays.set(segment.streetName, segment.streetName);
    }
    if (!midpoints.has(segment.streetName)) {
      midpoints.set(segment.streetName, []);
    }
    midpoints.get(segment.streetName).push(point);
  }
  const streets = new Map();
  for (const [norm, points] of midpoints) {
    const meanLon = points.reduce((sum, p) => sum + p[0], 0) / points.length;
    const meanLat = points.reduce((sum, p) => sum + p[1], 0) / points.length;
    let best = points[0];
    for (const point of points.slice(1)) {
      const here = (point[0] - meanLon) ** 2 + (point[1] - meanLat) ** 2;
      const there = (best[0] - meanLon) ** 2 + (best[1] - meanLat) ** 2;
      if (here < there) {
        best = point;
      }
    }
    streets.set(norm, { display: displays.get(norm), lon: best[0], lat: best[1] });
  }
  return streets;
}

/** `etl.addresses.place_spellings`, as an array of word arrays sorted like Python's. */
function placeSpellings(name) {
  const words = nameWords(name);
  const found = new Map([[words.join(" "), words]]);
  for (const [phrase, short] of PLACE_SHORTHAND) {
    for (let start = 0; start + phrase.length <= words.length; start += 1) {
      if (phrase.every((word, i) => words[start + i] === word)) {
        const spelling = [...words.slice(0, start), short, ...words.slice(start + phrase.length)];
        found.set(spelling.join(" "), spelling);
      }
    }
  }
  for (const [alias, target] of Object.entries(PLACE_ALIASES)) {
    if (nameWords(target).join(" ") === words.join(" ")) {
      const spelling = nameWords(alias);
      found.set(spelling.join(" "), spelling);
    }
  }
  return [...found.values()]
    .filter((spelling) => spelling.length)
    .sort((a, b) => (a.join(" ") < b.join(" ") ? -1 : a.join(" ") > b.join(" ") ? 1 : 0));
}

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

function coordsBbox(geom, n) {
  const bbox = {
    minLon: new Float64Array(n),
    minLat: new Float64Array(n),
    maxLon: new Float64Array(n),
    maxLat: new Float64Array(n),
  };
  for (let row = 0; row < n; row += 1) {
    let minLon = Infinity;
    let minLat = Infinity;
    let maxLon = -Infinity;
    let maxLat = -Infinity;
    for (let at = geom.offsets[row]; at < geom.offsets[row + 1]; at += 1) {
      minLon = Math.min(minLon, geom.coords[2 * at]);
      minLat = Math.min(minLat, geom.coords[2 * at + 1]);
      maxLon = Math.max(maxLon, geom.coords[2 * at]);
      maxLat = Math.max(maxLat, geom.coords[2 * at + 1]);
    }
    bbox.minLon[row] = minLon;
    bbox.minLat[row] = minLat;
    bbox.maxLon[row] = maxLon;
    bbox.maxLat[row] = maxLat;
  }
  return bbox;
}

/** Columns of a table, given one object per row and the column names. */
function columns(rows, names) {
  const table = { n: rows.length };
  for (const name of names) {
    table[name] = rows.map((row) => row[name]);
  }
  return table;
}

/**
 * The pack `curbcheck pack` would write for these rows.
 *
 * Every table is in the order the ETL inserts it, because that order is the
 * rowid the loader preserves and `buildGeocodeIndex` uses as its last tiebreak.
 * `etl.stage.neutralize_formula` is not applied: no name in this fixture starts
 * with a spreadsheet formula character, so it is the identity here.
 */
function syntheticPack({ segments, nodes, addressRows, placeRows, extraDoors = [] }) {
  const streets = streetPoints(segments);
  const centerline = new Set(streets.keys());
  const nodeRows = nodes.map(([lonlat, names]) => ({
    nodeId: nodeId(lonlat),
    lon: lonlat[0],
    lat: lonlat[1],
    streetNames: JSON.stringify(names),
  }));
  const nodeIndex = new Map(nodeRows.map((row, at) => [row.nodeId, at]));

  const segmentRows = segments.map((segment) => ({
    segmentId: segment.segmentId,
    streetName: segment.streetName,
    streetNorm: segment.streetName,
    fromNode: nodeIndex.get(nodeId(segment.coordinates[0])) ?? -1,
    toNode: nodeIndex.get(nodeId(segment.coordinates[segment.coordinates.length - 1])) ?? -1,
    leftLowAddress: segment.left ? segment.left[0] : null,
    leftHighAddress: segment.left ? segment.left[1] : null,
    rightLowAddress: segment.right ? segment.right[0] : null,
    rightHighAddress: segment.right ? segment.right[1] : null,
  }));

  // `stage_address_points`: the key is the folded street, the label is the
  // centerline's own spelling where it has one.
  const doors = addressRows.map((row) => {
    const streetNorm = fold(row.full_street_name);
    const display = streets.get(streetNorm)?.display ?? row.full_street_name;
    return {
      streetNorm,
      houseNumber: Number(row.house_number),
      display: `${row.house_number} ${display}`,
      zipcode: row.zipcode ?? null,
      lon: row.coordinates[0],
      lat: row.coordinates[1],
    };
  });

  // `_INSERT_ZIP`: one row per ZIP, in GROUP BY order, before the extra doors
  // the Python's own test inserts after the index was built.
  const zipGroups = new Map();
  for (const door of doors) {
    if (door.zipcode === null) {
      continue;
    }
    if (!zipGroups.has(door.zipcode)) {
      zipGroups.set(door.zipcode, []);
    }
    zipGroups.get(door.zipcode).push(door);
  }
  const zipRows = [...zipGroups.entries()]
    .sort((a, b) => (a[0] < b[0] ? -1 : 1))
    .map(([zipcode, group]) => ({
      zipcode,
      lon: group.reduce((sum, d) => sum + d.lon, 0) / group.length,
      lat: group.reduce((sum, d) => sum + d.lat, 0) / group.length,
      addressPoints: group.length,
    }));

  // `_write_intersections`: every ordered pair of names on a node, both ways.
  const intersectionRows = [];
  for (const [lonlat, names] of nodes) {
    if (names.length < 2) {
      continue;
    }
    for (let i = 0; i < names.length; i += 1) {
      for (const second of names.slice(i + 1)) {
        const aNorm = fold(names[i]);
        const bNorm = fold(second);
        if (!aNorm || !bNorm || aNorm === bNorm) {
          continue;
        }
        const label = `${streets.get(aNorm)?.display ?? names[i]} & ${
          streets.get(bNorm)?.display ?? second
        }`;
        const point = { display: label, lon: lonlat[0], lat: lonlat[1] };
        intersectionRows.push({ aNorm, bNorm, ...point });
        intersectionRows.push({ aNorm: bNorm, bNorm: aNorm, ...point });
      }
    }
  }

  // `_street_variant_pairs`, written `sorted(variants)`.
  const known = new Set([...centerline, ...doors.map((door) => door.streetNorm)]);
  const variantPairs = new Set();
  for (const streetNorm of known) {
    for (const variant of streetVariants(streetNorm)) {
      variantPairs.add(`${variant}\t${streetNorm}`);
    }
  }
  for (const [nickname, target] of Object.entries(NICKNAMES)) {
    if (known.has(target)) {
      variantPairs.add(`${fold(nickname)}\t${target}`);
    }
  }
  const variantRows = [...variantPairs]
    .sort()
    .map((pair) => ({ variant: pair.split("\t")[0], streetNorm: pair.split("\t")[1] }));

  // `_write_street_tokens`: the variants plus the alias keys, unfolded.
  const spellings = new Set(variantRows.map((row) => `${row.variant}\t${row.streetNorm}`));
  for (const [alias, target] of NAME_ALIASES) {
    if (centerline.has(target)) {
      spellings.add(`${alias}\t${target}`);
    }
  }
  const streetTokenKeys = new Set();
  for (const spelling of spellings) {
    const [text, streetNorm] = spelling.split("\t");
    if (!centerline.has(streetNorm)) {
      continue;
    }
    const words = spellingWords(text);
    words.forEach((word, position) => {
      streetTokenKeys.add([word, position, streetNorm, words.join(" ")].join("\t"));
    });
  }
  const streetTokenRows = [...streetTokenKeys].sort().map((key) => {
    const [token, position, streetNorm, searchName] = key.split("\t");
    return { token, position: Number(position), streetNorm, searchName };
  });

  // `_spelling_texts`, which is what a place may not repeat.
  const streetSpellings = new Set(
    variantRows
      .filter((row) => centerline.has(row.streetNorm))
      .map((row) => spellingWords(row.variant).join(" ")),
  );

  const placeRowsStaged = [];
  for (const place of placeRows) {
    const kept = placeSpellings(place.feature_name).filter(
      (words) => !streetSpellings.has(words.join(" ")),
    );
    if (!kept.length || streetSpellings.has(nameWords(place.feature_name).join(" "))) {
      continue;
    }
    placeRowsStaged.push({
      placeId: placeRowsStaged.length,
      display: place.feature_name,
      lon: place.coordinates[0],
      lat: place.coordinates[1],
      spellings: kept,
    });
  }
  const placeTokenRows = [];
  for (const place of placeRowsStaged) {
    for (const words of place.spellings) {
      words.forEach((word, position) => {
        placeTokenRows.push({
          token: word,
          position,
          placeId: place.placeId,
          searchName: words.join(" "),
        });
      });
    }
  }

  const geom = coordsColumn(segments.map((segment) => segment.coordinates));
  const pack = {
    segments: {
      ...columns(segmentRows, [
        "segmentId",
        "streetName",
        "streetNorm",
        "fromNode",
        "toNode",
        "leftLowAddress",
        "leftHighAddress",
        "rightLowAddress",
        "rightHighAddress",
      ]),
      geom,
      bbox: coordsBbox(geom, segments.length),
    },
    nodes: columns(nodeRows, ["nodeId", "lon", "lat", "streetNames"]),
    geocode: {
      addressPoints: columns(
        [...doors, ...extraDoors],
        ["streetNorm", "houseNumber", "display", "zipcode", "lon", "lat"],
      ),
      intersections: columns(intersectionRows, ["aNorm", "bNorm", "display", "lon", "lat"]),
      places: columns(placeRowsStaged, ["placeId", "display", "lon", "lat"]),
      placeTokens: columns(placeTokenRows, ["token", "position", "placeId", "searchName"]),
      streets: columns(
        [...streets.entries()]
          .sort((a, b) => (a[0] < b[0] ? -1 : 1))
          .map(([streetNorm, point]) => ({ streetNorm, ...point })),
        ["streetNorm", "display", "lon", "lat"],
      ),
      streetVariants: columns(variantRows, ["variant", "streetNorm"]),
      streetTokens: columns(streetTokenRows, ["token", "position", "streetNorm", "searchName"]),
      zipCentroids: columns(zipRows, ["zipcode", "lon", "lat", "addressPoints"]),
    },
  };
  pack.index = { segmentGrid: buildGrid(pack.segments.bbox, pack.segments.n) };
  return { pack, index: buildGeocodeIndex(pack) };
}

function upperEastSide(extraDoors = []) {
  const segments = [
    {
      segmentId: "3681",
      streetName: "3 AVE",
      coordinates: [NODE_85, NODE_86],
      left: ["1510", "1528"],
      right: ["1509", "1525"],
    },
    {
      segmentId: "3682",
      streetName: "3 AVE",
      coordinates: [NODE_86, NODE_87],
      left: ["1530", "1548"],
      right: ["1529", "1545"],
    },
    // A street with no published address range and no surveyed door: the shape
    // that degrades all the way to the street itself.
    { segmentId: "9001", streetName: "E 85 ST", coordinates: [NODE_LEX_85, NODE_85] },
    {
      segmentId: "9002",
      streetName: "E 86 ST",
      coordinates: [[NODE_86[0] - 0.002, NODE_86[1]], NODE_86],
    },
    {
      segmentId: "9003",
      streetName: "LEXINGTON AVE",
      coordinates: [[NODE_LEX_85[0], NODE_LEX_85[1] - 0.002], NODE_LEX_85],
    },
    // A street AddressPoint never files a door on, but which publishes a range:
    // the 36 streets the last rung exists for (docs/DATA.md §5.1).
    {
      segmentId: "9100",
      streetName: "CHISUM PL",
      coordinates: [
        [-73.935, 40.82],
        [-73.934, 40.821],
      ],
      left: ["2", "20"],
      right: ["1", "19"],
    },
    // A street nobody reaches by the start of its name: "AMERICAS" is its
    // third word and its only memorable one.
    {
      segmentId: "9200",
      streetName: "AVE OF THE AMERICAS",
      coordinates: [
        [-73.983, 40.76],
        [-73.9835, 40.761],
      ],
    },
  ];
  const nodes = [
    [NODE_85, ["3 AVE", "E 85 ST"]],
    [NODE_86, ["3 AVE", "E 86 ST"]],
    [NODE_LEX_85, ["E 85 ST", "LEXINGTON AVE"]],
  ];
  const addressRows = [
    ...ODD_DOORS.map(([house, fraction]) =>
      addressRow(String(house), "3 AVE", between(NODE_85, NODE_86, fraction)),
    ),
    ...EVEN_DOORS.map(([house, fraction]) => {
      const point = between(NODE_85, NODE_86, fraction);
      return addressRow(String(house), "3 AVE", [point[0] + EVEN_SIDE_LON_OFFSET, point[1]]);
    }),
  ];
  const placeRows = [
    { feature_name: "GRACIE MANSION", coordinates: [-73.9432, 40.776] },
    // Its last word sorts before its first, which is how a place search that
    // lost the typed order shows up.
    { feature_name: "WASHINGTON ARCH", coordinates: [-73.9973, 40.7308] },
    // The four rungs of a word match, on one word: ART is this name's fourth
    // word and the next one's first, and neither is the whole name.
    { feature_name: "HIGH SCHOOL OF ART AND DESIGN", coordinates: [-73.972, 40.759] },
    { feature_name: "ART STUDENTS LEAGUE", coordinates: [-73.98, 40.765] },
    // A place that starts with the word a street is buried in, which is the
    // collision the street band of the ladder exists to settle.
    { feature_name: "AMERICAS SOCIETY GALLERY", coordinates: [-73.964, 40.772] },
  ];
  return syntheticPack({ segments, nodes, addressRows, placeRows, extraDoors });
}

const uesFixture = upperEastSide();
const ues = (text, limit) => suggest(uesFixture.pack, uesFixture.index, text, limit);
const ofKind = (candidates, kind) => candidates.filter((c) => c.kind === kind);
const labels = (candidates) => candidates.map((c) => c.label);

// --- parsing --------------------------------------------------------------

test("address forms parse to the same folded street", () => {
  const cases = [
    ["123 E 85 St", { kind: "address", houseNumber: 123, street: "E 85 ST" }],
    ["123 East 85th Street", { kind: "address", houseNumber: 123, street: "E 85 ST" }],
    ["1500 3rd Ave", { kind: "address", houseNumber: 1500, street: "3 AVE" }],
    ["  1500   THIRD   AVENUE ", { kind: "address", houseNumber: 1500, street: "3 AVE" }],
    ["1500 3rd av", { kind: "address", houseNumber: 1500, street: "3 AV" }],
    ["123a E 85 St", { kind: "address", houseNumber: 123, street: "E 85 ST" }],
  ];
  for (const [text, expected] of cases) {
    assert.deepEqual(parseQuery(text), expected, text);
  }
});

test("intersection forms parse to two streets", () => {
  const cases = [
    ["Lexington Ave & 86th St", ["LEXINGTON AVE", "86 ST"]],
    ["E 86 St and 3 Ave", ["E 86 ST", "3 AVE"]],
    ["E 86 St / 3 Ave", ["E 86 ST", "3 AVE"]],
    ["E 86 St at 3 Ave", ["E 86 ST", "3 AVE"]],
    ["E 86 St @ 3 Ave", ["E 86 ST", "3 AVE"]],
    ["86 & 3", ["86", "3"]],
  ];
  for (const [text, [first, second]] of cases) {
    assert.deepEqual(parseQuery(text), { kind: "intersection", first, second }, text);
  }
});

test("a bare street name parses as a street", () => {
  assert.deepEqual(parseQuery("Third Avenue"), { kind: "street", street: "3 AVE" });
});

test("five digits parse as a zip", () => {
  assert.deepEqual(parseQuery("10021"), { kind: "zip", zipcode: "10021" });
});

test("empty and oversized queries parse to null", () => {
  for (const text of ["", "   ", "x".repeat(MAX_QUERY_CHARS + 1), "\x00\x01"]) {
    assert.equal(parseQuery(text), null, JSON.stringify(text));
  }
});

// --- the address ladder ---------------------------------------------------

test("a surveyed door is the top answer and carries its zip", () => {
  const [candidate] = ofKind(ues("1517 3rd Ave"), GeocodeKind.ADDRESS);

  assert.equal(candidate.label, "1517 3 AVE");
  assert.equal(candidate.confidence, 0.98);
  assert.equal(candidate.secondary, "Manhattan 10028");
  assert.deepEqual([candidate.lon, candidate.lat], between(NODE_85, NODE_86, 0.5));
});

test("a missing number is placed between its two surveyed neighbours", () => {
  // 1519 is absent; 1517 and 1529 are present, so it lands a sixth of the way
  // between them and says so on its second line.
  const [candidate] = ofKind(ues("1519 3rd ave"), GeocodeKind.ADDRESS);

  assert.equal(candidate.label, "1519 3 AVE");
  assert.equal(candidate.confidence, 0.75);
  assert.equal(candidate.secondary, "Manhattan · between 1517 and 1529");
  const fraction = 0.5 + 0.5 * ((1519 - 1517) / (1529 - 1517));
  const [lon, lat] = between(NODE_85, NODE_86, fraction);
  assert.ok(Math.abs(candidate.lon - lon) < 1e-12 && Math.abs(candidate.lat - lat) < 1e-12);
});

test("interpolation stays on the side the parity says", () => {
  // 1518 is even, so it is placed between 1510 and 1528, never between the odd doors.
  const [candidate] = ofKind(ues("1518 3 Ave"), GeocodeKind.ADDRESS);

  assert.equal(candidate.label, "1518 3 AVE");
  assert.equal(candidate.secondary, "Manhattan · between 1510 and 1528");
  assert.ok(candidate.lon < between(NODE_85, NODE_86, 0.5)[0] + EVEN_SIDE_LON_OFFSET / 2);
});

test("a number outside every run of doors reads as near the closest one", () => {
  const [candidate] = ofKind(ues("9999 3 Ave"), GeocodeKind.ADDRESS);

  assert.equal(candidate.label, "near 1529 3 AVE");
  assert.equal(candidate.confidence, 0.6);
});

test("a street with no doors falls back to the published range", () => {
  // The last rung: CSCL's own range, for one of the 36 streets with no door.
  const [candidate] = ofKind(ues("10 Chisum Pl"), GeocodeKind.ADDRESS);

  assert.equal(candidate.label, "10 CHISUM PL");
  assert.equal(candidate.confidence, 0.5);
  assert.ok(Math.abs(candidate.lon - (-73.935 + 0.001 * ((10 - 2) / (20 - 2)))) < 1e-9);
});

test("a street with neither doors nor ranges degrades to the street", () => {
  const [candidate] = ues("200 E 85 St");

  assert.equal(candidate.kind, GeocodeKind.STREET);
  assert.equal(candidate.label, "E 85 ST");
  assert.ok(candidate.confidence < 0.5);
});

test("an unknown street returns no candidates", () => {
  assert.deepEqual(ues("123 NOWHERE BLVD"), []);
});

// --- the other kinds ------------------------------------------------------

test("an intersection returns the shared node", () => {
  const [candidate] = ues("3 Ave & E 86 St");

  assert.equal(candidate.kind, GeocodeKind.INTERSECTION);
  assert.deepEqual([candidate.lon, candidate.lat], NODE_86);
  assert.equal(candidate.confidence, 0.95);
});

test("an intersection is order independent and survives spelled-out names", () => {
  const [writtenOut] = ues("East 86th Street and Third Avenue");
  const [abbreviated] = ues("3 AVE & E 86 ST");

  assert.deepEqual([writtenOut.lon, writtenOut.lat], [abbreviated.lon, abbreviated.lat]);
});

test("a numbered cross street resolves without its directional", () => {
  const [candidate] = ues("3 Ave & 86");

  assert.deepEqual([candidate.lon, candidate.lat], NODE_86);
});

test("two streets that never meet degrade to the two streets", () => {
  // Claiming a corner that is not in the data would be the dishonest answer.
  const candidates = ues("LEXINGTON AVE & 3 AVE");

  assert.deepEqual(
    candidates.map((c) => c.kind),
    [GeocodeKind.STREET, GeocodeKind.STREET],
  );
  assert.deepEqual(new Set(labels(candidates)), new Set(["LEXINGTON AVE", "3 AVE"]));
  assert.ok(candidates.every((c) => c.confidence === 0.3));
});

test("a typo in a street name still resolves at a lower confidence", () => {
  const [candidate] = ofKind(ues("CHISM PL"), GeocodeKind.STREET);

  assert.equal(candidate.label, "CHISUM PL");
  assert.ok(Math.abs(candidate.confidence - 0.45 * 0.8) < 1e-12);
});

test("a query too short to correct is not corrected", () => {
  // "A" is a prefix of AVE OF THE AMERICAS, so that street comes back; what
  // must not come back is 3 AVE or CHISUM PL, which an edit-distance pass over
  // a one-character name would reach.
  assert.deepEqual(labels(ues("3 A")), ["AVE OF THE AMERICAS"]);
});

test("a zip returns its centre and says how coarse that is", () => {
  const [candidate] = ues("10028");

  assert.equal(candidate.kind, GeocodeKind.ZIP);
  assert.equal(candidate.label, "10028");
  assert.equal(candidate.secondary, "Manhattan · ZIP centre of 5 addresses");
});

test("a place name is found by its words", () => {
  const [candidate] = ues("gracie mansion");

  assert.equal(candidate.kind, GeocodeKind.PLACE);
  assert.equal(candidate.label, "GRACIE MANSION");
});

test("a half-typed place name still matches", () => {
  // Every word is matched as a prefix, because any of them may still be typed.
  for (const [typed, label] of [
    ["gracie mans", "GRACIE MANSION"],
    ["washington ar", "WASHINGTON ARCH"],
  ]) {
    assert.equal(ues(typed)[0].label, label, typed);
  }
});

// --- matching a name by the words in it -----------------------------------

test("any word of a place name finds it in any order", () => {
  // The reported bug: "fashion" found nothing while "high school of fashion" did.
  for (const typed of [
    "design",
    "art design",
    "design art",
    "hs art",
    "school of art",
    "art and design",
  ]) {
    assert.ok(
      labels(ues(typed)).includes("HIGH SCHOOL OF ART AND DESIGN"),
      `${typed} -> ${labels(ues(typed))}`,
    );
  }
});

test("a stopword is neither required nor in the way", () => {
  assert.deepEqual(ues("art and design"), ues("art design"));
  assert.deepEqual(ues("the art students league"), ues("art students league"));
});

test("a name typed whole beats one it only starts", () => {
  assert.equal(labels(ues("art students league"))[0], "ART STUDENTS LEAGUE");
});

test("a name the query starts beats one the word sits inside", () => {
  // Google Maps ranks this way and the UX audit's testers expected it.
  const found = labels(ues("art"));

  assert.ok(found.indexOf("ART STUDENTS LEAGUE") < found.indexOf("HIGH SCHOOL OF ART AND DESIGN"));
});

test("a street is found by a word buried in its name", () => {
  const [candidate] = ofKind(ues("americas"), GeocodeKind.STREET);

  assert.equal(candidate.label, "AVE OF THE AMERICAS");
});

test("a street outranks a place that merely starts with the same word", () => {
  // A street is a destination with two sides and a length; a place is one point.
  const [first, second] = ues("americas");

  assert.deepEqual([first.kind, first.label], [GeocodeKind.STREET, "AVE OF THE AMERICAS"]);
  assert.deepEqual([second.kind, second.label], [GeocodeKind.PLACE, "AMERICAS SOCIETY GALLERY"]);
});

test("a one-word query that names a street still answers with the street", () => {
  // "lex" is Lexington Avenue, not the Lex Hotel, however short the query is.
  for (const typed of ["lexington", "lexingt", "chisum"]) {
    assert.equal(ues(typed)[0].kind, GeocodeKind.STREET, typed);
  }
});

test("a word that matches nothing takes the whole query with it", () => {
  // Every typed word has to land somewhere, or the name is not what was meant.
  assert.deepEqual(ues("art zeppelin"), []);
});

// --- hostile input --------------------------------------------------------

test("hostile queries never raise and never mutate", () => {
  const hostile = [
    "<script>alert(1)</script>",
    "=1+1",
    "+SUM(A1:A9)",
    "'; DROP TABLE street_segment; --",
    "123 E 85 St'; DELETE FROM street_node; --",
    "%",
    "_",
    "\\",
    `123 ${"A".repeat(190)}`,
    "\x00\x01",
    "３ AVE", // fullwidth digit three: not a house number
  ];
  for (const text of hostile) {
    assert.ok(Array.isArray(ues(text)), JSON.stringify(text));
    assert.equal(uesFixture.pack.segments.n, 7);
    assert.equal(uesFixture.pack.geocode.addressPoints.n, 5);
  }
});

test("limit bounds the candidate list", () => {
  assert.deepEqual(ues("3 Ave", 1), ues("3 Ave").slice(0, 1));
  assert.throws(() => ues("3 Ave", 0), /limit/);
});

test("the candidate list is never longer than eight", () => {
  const extra = Array.from({ length: 12 }, (_, at) => ({
    streetNorm: "3 AVE",
    houseNumber: 1517,
    display: `1517${String.fromCharCode("A".charCodeAt(0) + at)} 3 AVE`,
    zipcode: "10028",
    lon: NODE_85[0],
    lat: NODE_85[1],
  }));
  const crowded = upperEastSide(extra);

  assert.equal(suggest(crowded.pack, crowded.index, "1517 3 Ave").length, 8);
});

// --- coverage and the public entry point ----------------------------------

test("geocode is suggest filtered to what the app can answer about", () => {
  assert.deepEqual(geocode(uesFixture.pack, uesFixture.index, "1517 3rd Ave"), ues("1517 3rd Ave"));
});

test("a candidate outside coverage is never offered", () => {
  // Offering a destination and then refusing to search it is UX audit P0-3.
  // Written with `address` candidates because those are the kinds that come off
  // a dataset other than the centerline, and so the only ones that can land
  // outside coverage at all.
  const inside = {
    label: "1517 3 AVE",
    lat: NODE_85[1],
    lon: NODE_85[0],
    kind: GeocodeKind.ADDRESS,
    confidence: 0.9,
    secondary: "Manhattan",
  };
  const hoboken = {
    label: "1 WASHINGTON ST",
    lat: 40.744,
    lon: -74.0324,
    kind: GeocodeKind.ADDRESS,
    confidence: 0.9,
    secondary: "Manhattan",
  };

  assert.deepEqual(_inCoverage(uesFixture.pack, [hoboken, inside], 8), [inside]);
});

test("a street is in coverage without being asked", () => {
  // Its pin is a vertex of its own centerline, so the question has one answer.
  const farAway = {
    label: "3 AVE",
    lat: 0.0,
    lon: 0.0,
    kind: GeocodeKind.STREET,
    confidence: 0.3,
    secondary: "Manhattan",
  };

  assert.deepEqual(_inCoverage(uesFixture.pack, [farAway], 8), [farAway]);
});

// --- reverse --------------------------------------------------------------

test("a pin beside a door reads back as that door", () => {
  // A pin is the one destination the user cannot read back to themselves (P1-4).
  const [lon, lat] = between(NODE_85, NODE_86, 0.5);

  const match = reverseGeocode(uesFixture.pack, uesFixture.index, lon + 0.0001, lat);

  assert.notEqual(match, null);
  assert.equal(match.kind, GeocodeKind.ADDRESS);
  assert.equal(match.label, "near 1517 3 AVE");
  assert.ok(match.distance_m > 0 && match.distance_m < REVERSE_ADDRESS_MAX_M);
  assert.equal(match.distance_m, Math.round(match.distance_m * 10) / 10);
});

test("a pin far from any door falls back to the nearest corner", () => {
  const match = reverseGeocode(
    uesFixture.pack,
    uesFixture.index,
    NODE_LEX_85[0],
    NODE_LEX_85[1] + 0.0002,
  );

  assert.notEqual(match, null);
  assert.equal(match.kind, GeocodeKind.INTERSECTION);
  assert.ok(match.label.includes("&"));
});

test("a pin with no corner nearby reads back as the street", () => {
  const nodeless = {
    ...uesFixture.pack,
    nodes: { n: 0, nodeId: [], lon: [], lat: [], streetNames: [] },
  };

  const match = reverseGeocode(nodeless, uesFixture.index, -73.95585, 40.77792);

  assert.notEqual(match, null);
  assert.equal(match.kind, GeocodeKind.STREET);
  assert.equal(match.label, "E 85 ST");
});

test("a pin with nothing around it reverses to nothing", () => {
  assert.equal(reverseGeocode(uesFixture.pack, uesFixture.index, -74.0324, 40.744), null);
});

test("a pin at a nonsense coordinate reverses to nothing", () => {
  assert.equal(reverseGeocode(uesFixture.pack, uesFixture.index, NaN, Infinity), null);
});

// --- accuracy -------------------------------------------------------------

// The fast twin of the differential harness's accuracy check, so a `make check`
// with no pack still fails when the ladder drifts. One straight avenue with a
// surveyed door every 40 ft; a third of them are hidden from the index, and
// geocoding those exercises the interpolation rung against a location we know
// exactly.
const ACCURACY_DOORS = 90;
const ACCURACY_SPACING_DEG = 0.00012;
const ACCURACY_MEDIAN_MAX_M = 10.0;
const ACCURACY_P95_MAX_M = 60.0;

function doorPosition(at) {
  return [-73.97, 40.75 + at * ACCURACY_SPACING_DEG];
}

test("every door on a street geocodes to within ten metres", () => {
  const start = doorPosition(0);
  const end = doorPosition(ACCURACY_DOORS - 1);
  const truth = Array.from({ length: ACCURACY_DOORS }, (_, at) => [1 + 2 * at, doorPosition(at)]);
  const { pack, index } = syntheticPack({
    segments: [{ segmentId: "long", streetName: "LONG AVE", coordinates: [start, end] }],
    nodes: [],
    addressRows: truth
      .filter((_, order) => order % 3 !== 1)
      .map(([house, position]) => addressRow(String(house), "LONG AVE", position)),
    placeRows: [],
  });

  const errors = truth.map(([house, position]) => {
    const [candidate] = suggest(pack, index, `${house} LONG AVE`, 1);
    return Math.hypot(
      (candidate.lon - position[0]) * metersPerDegreeLon(position[1]),
      (candidate.lat - position[1]) * M_PER_DEG_LAT,
    );
  });
  errors.sort((a, b) => a - b);
  const median = (errors[(errors.length - 1) >> 1] + errors[errors.length >> 1]) / 2;

  assert.ok(median <= ACCURACY_MEDIAN_MAX_M, `median ${median}`);
  assert.ok(errors[Math.trunc(errors.length * 0.95)] <= ACCURACY_P95_MAX_M);
});

test("driver rows come back in one order whatever the hash seed", () => {
  // `searchNames` keeps the first spelling it meets at equal rank, so the rows
  // it walks have to arrive in an order that does not depend on the process; a
  // set here made a suggestion list differ between two runs.
  const rows = _driverRows(uesFixture.index.streetTokens, ["a"]);
  const sorted = [...rows].sort((a, b) =>
    a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0,
  );

  assert.ok(Array.isArray(rows));
  assert.deepEqual(rows, sorted);
  assert.equal(new Set(rows.map((row) => row.join("\t"))).size, rows.length);
});

test("the cardinal table is the one the ETL folds place names with", () => {
  // `spellingWords` is shared with the index builder above, so a drift here
  // would show up as a place that can no longer be found by its own name.
  assert.deepEqual(nameWords("One World Trade Center"), ["1", "WORLD", "TRADE", "CENTER"]);
  assert.equal(CARDINALS.get("ONE"), "1");
});
