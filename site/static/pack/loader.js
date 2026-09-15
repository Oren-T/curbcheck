/**
 * The data pack, inflated into typed arrays and the lookups the engine needs.
 *
 * `curbcheck/pack.py` writes what this reads; the two files are one format and
 * change together. A table arrives as parallel columns in SQLite `rowid` order
 * and stays that way here: a row is an index, a join is an array lookup, and
 * the order a Python query relied on is the order the JS can reproduce
 * (docs/STATIC_SITE.md, "Porting rules").
 *
 * Nothing here fetches. `loadPack` takes a `readText`, so the worker can use
 * `fetch` plus `DecompressionStream` and the Node tests can use `zlib`.
 */

import { buildGrid } from "./grid.js";

/** The only pack format this loader understands (`pack.FORMAT_VERSION`). */
export const FORMAT_VERSION = 1;

const META_NAME = "meta.json";

const INT32_MIN = -2147483648;
const INT32_MAX = 2147483647;

const NO_ROWS = new Int32Array(0);

// Pack table name -> the property the rest of the code reads.
const RULES_TABLES = {
  spans: "spans",
  regulations: "regulations",
  signs: "signs",
  meter_rates: "meterRates",
  calendar: "calendar",
};
const STREETS_TABLES = { segments: "segments", nodes: "nodes" };
const GEOCODE_TABLES = {
  address_points: "addressPoints",
  intersections: "intersections",
  places: "places",
  place_tokens: "placeTokens",
  streets: "streets",
  street_variants: "streetVariants",
  street_tokens: "streetTokens",
  zip_centroids: "zipCentroids",
};

/**
 * Read a whole pack.
 *
 * @param {(name: string) => Promise<string>} readText the inflated text of one pack file
 * @returns {Promise<Object>} the loaded pack
 */
export async function loadPack(readText) {
  const meta = JSON.parse(await readText(META_NAME));
  if (meta.format !== FORMAT_VERSION) {
    throw new Error(`pack: format ${meta.format}, and this build reads ${FORMAT_VERSION}`);
  }
  const [rules, streets, geocode] = await Promise.all([
    readFile(readText, meta, "rules"),
    readFile(readText, meta, "streets"),
    readFile(readText, meta, "geocode"),
  ]);

  const pack = { meta, geocode: {}, lineString };
  decodeInto(pack, rules, RULES_TABLES);
  decodeInto(pack, streets, STREETS_TABLES);
  decodeInto(pack.geocode, geocode, GEOCODE_TABLES);
  pack.spans.bbox = coordsBbox(pack.spans.geom, pack.spans.n);
  pack.segments.bbox = coordsBbox(pack.segments.geom, pack.segments.n);
  pack.index = buildIndex(pack);
  return pack;
}

/**
 * One row of a geometry column as GeoJSON, which is what the API returns.
 *
 * @param {Object} table a table with a `geom` column, e.g. `pack.spans`
 * @param {number} row
 * @returns {{type: string, coordinates: number[][]}}
 */
export function lineString(table, row) {
  const { coords, offsets } = table.geom;
  const coordinates = [];
  for (let at = offsets[row]; at < offsets[row + 1]; at += 1) {
    coordinates.push([coords[2 * at], coords[2 * at + 1]]);
  }
  return { type: "LineString", coordinates };
}

async function readFile(readText, meta, label) {
  const name = meta.files[label];
  if (typeof name !== "string") {
    throw new Error(`pack: meta.json names no ${label} file`);
  }
  return JSON.parse(await readText(name));
}

function decodeInto(target, file, names) {
  for (const [name, property] of Object.entries(names)) {
    const table = file.tables[name];
    if (table === undefined) {
      throw new Error(`pack: no ${name} table`);
    }
    target[property] = decodeTable(table, file.dicts);
  }
}

function decodeTable(table, dicts) {
  const decoded = { n: table.rows };
  for (const [name, column] of Object.entries(table.columns)) {
    decoded[name] = decodeColumn(column, dicts, name);
  }
  return decoded;
}

function decodeColumn(column, dicts, name) {
  if (Array.isArray(column)) {
    return decodeValues(column);
  }
  if (column.dict !== undefined) {
    return decodeDictionary(column, dicts);
  }
  if (column.coords !== undefined) {
    return { coords: new Float64Array(column.coords), offsets: new Uint32Array(column.offsets) };
  }
  if (column.lists !== undefined) {
    return { lists: new Int32Array(column.lists), offsets: new Uint32Array(column.offsets) };
  }
  throw new Error(`pack: column ${name} is in no encoding this loader knows`);
}

/**
 * A plain column as the narrowest array that holds it without changing a value.
 *
 * Nulls and strings keep the parsed array: a typed array has no null, and the
 * engine distinguishes "no rule" from "a rule of zero".
 */
function decodeValues(values) {
  switch (numericKind(values)) {
    case "boolean":
      return new Uint8Array(values);
    case "int32":
      return new Int32Array(values);
    case "float64":
      return new Float64Array(values);
    default:
      return values;
  }
}

function numericKind(values) {
  let integral = true;
  let bits = values.length > 0;
  for (let at = 0; at < values.length; at += 1) {
    const value = values[at];
    if (typeof value !== "number") {
      return "mixed";
    }
    if (!Number.isInteger(value) || value < INT32_MIN || value > INT32_MAX) {
      integral = false;
    }
    if (value !== 0 && value !== 1) {
      bits = false;
    }
  }
  if (bits) {
    return "boolean";
  }
  return integral ? "int32" : "float64";
}

/**
 * Dictionary codes back to strings, `null` for -1.
 *
 * The strings are shared, not copied: 59,020 rules quote 1,751 distinct sign
 * descriptions, and holding one string per row would be most of the heap.
 */
function decodeDictionary(column, dicts) {
  const strings = dicts[column.dict];
  if (strings === undefined) {
    throw new Error(`pack: no ${column.dict} dictionary`);
  }
  const { codes } = column;
  const decoded = new Array(codes.length);
  for (let at = 0; at < codes.length; at += 1) {
    decoded[at] = codes[at] < 0 ? null : strings[codes[at]];
  }
  return decoded;
}

/**
 * Per-row bounds of a geometry column.
 *
 * `curbcheck/pack.py` does not ship `min_lon`/… : SQLite computed them from
 * these same doubles with `db.geojson_bbox`, so recomputing them is equal and
 * a megabyte cheaper.
 */
function coordsBbox(geom, n) {
  const bbox = {
    minLon: new Float64Array(n),
    minLat: new Float64Array(n),
    maxLon: new Float64Array(n),
    maxLat: new Float64Array(n),
  };
  const { coords, offsets } = geom;
  for (let row = 0; row < n; row += 1) {
    let minLon = Infinity;
    let minLat = Infinity;
    let maxLon = -Infinity;
    let maxLat = -Infinity;
    for (let at = offsets[row]; at < offsets[row + 1]; at += 1) {
      const lon = coords[2 * at];
      const lat = coords[2 * at + 1];
      minLon = Math.min(minLon, lon);
      minLat = Math.min(minLat, lat);
      maxLon = Math.max(maxLon, lon);
      maxLat = Math.max(maxLat, lat);
    }
    bbox.minLon[row] = minLon;
    bbox.minLat[row] = minLat;
    bbox.maxLon[row] = maxLon;
    bbox.maxLat[row] = maxLat;
  }
  return bbox;
}

function buildIndex(pack) {
  return {
    spanByRegSegId: mapById(pack.spans.regSegId),
    segmentById: mapById(pack.segments.segmentId),
    nodeById: mapById(pack.nodes.nodeId),
    signById: mapById(pack.signs.signId),
    regulationsBySpan: groupRows(pack.regulations.span, pack.spans.n),
    signsBySegment: groupRows(pack.signs.segment, pack.segments.n),
    meterRatesBySegment: groupRows(pack.meterRates.segment, pack.segments.n),
    spanGrid: buildGrid(pack.spans.bbox, pack.spans.n),
    segmentGrid: buildGrid(pack.segments.bbox, pack.segments.n),
  };
}

function mapById(ids) {
  const byId = new Map();
  for (let row = 0; row < ids.length; row += 1) {
    byId.set(ids[row], row);
  }
  return byId;
}

/**
 * The rows of one table grouped by the row they point at, each group in rowid
 * order — which is what `search._load_stacks` and `signs.signs_for_segment`
 * got from SQLite. A -1 owner (no row) is filed nowhere.
 *
 * @returns {Int32Array[]} one entry per owner row, empty where nothing points at it
 */
function groupRows(owner, groups) {
  const starts = new Int32Array(groups + 1);
  for (let row = 0; row < owner.length; row += 1) {
    if (owner[row] >= 0) {
      starts[owner[row] + 1] += 1;
    }
  }
  for (let group = 1; group <= groups; group += 1) {
    starts[group] += starts[group - 1];
  }
  const items = new Int32Array(starts[groups]);
  const cursor = starts.slice(0, groups);
  for (let row = 0; row < owner.length; row += 1) {
    if (owner[row] >= 0) {
      items[cursor[owner[row]]] = row;
      cursor[owner[row]] += 1;
    }
  }
  const grouped = new Array(groups);
  for (let group = 0; group < groups; group += 1) {
    grouped[group] =
      starts[group] === starts[group + 1]
        ? NO_ROWS
        : items.subarray(starts[group], starts[group + 1]);
  }
  return grouped;
}
