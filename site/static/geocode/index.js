/**
 * The geocoder's lookups, built once from the pack's tables.
 *
 * This module has no Python counterpart: it is the SQLite indexes
 * `curbcheck/db.py` declares, rebuilt in memory so every rung can do the
 * selection its SQL constant did. Each ordering below is the order SQLite
 * actually produced for the query that reads it — checked with
 * `EXPLAIN QUERY PLAN` against `data/curbcheck.sqlite`, because a query with no
 * `ORDER BY` comes back in the order of whichever index the planner chose, and
 * three of the geocoder's rungs keep only the first row.
 *
 * Every stored name is ASCII (measured: 0 non-ASCII rows), so `<` on UTF-16
 * code units is SQLite's `BINARY` collation and Python's `str` ordering at the
 * same time. `localeCompare` is never used.
 *
 * `buildGeocodeIndex` is called once by the worker after `loadPack`, so the
 * first keystroke pays nothing.
 */

/**
 * @typedef {object} TokenTable One of the two word indexes, as `names.js` reads it.
 * @property {Int32Array} order rows in PRIMARY KEY order
 * @property {string[]} token
 * @property {string[]} nameId the key as text, which is what `_driver_rows` sorts
 * @property {string[]} searchName
 */

/**
 * @typedef {object} GeocodeIndex
 * @property {Map<string, Int32Array>} addressByStreet `ix_address_point_street` order
 * @property {Int32Array} addressByLon `ix_address_point_lon` order
 * @property {Int32Array} variantOrder `street_variant`'s PRIMARY KEY order
 * @property {Map<string, number>} streetByNorm
 * @property {TokenTable} streetTokens
 * @property {TokenTable} placeTokens
 * @property {Map<number, number>} placeById
 * @property {Map<string, number>} zipByCode
 * @property {Int32Array} intersectionOrder `ix_intersection_pair` order
 * @property {Map<string, Int32Array>} segmentsByNorm `ix_street_segment_norm` order
 */

/** @returns {GeocodeIndex} */
export function buildGeocodeIndex(pack) {
  const g = pack.geocode;
  return {
    addressByStreet: addressByStreet(g.addressPoints),
    addressByLon: addressByLon(g.addressPoints),
    variantOrder: variantOrder(g.streetVariants),
    streetByNorm: byKey(g.streets.n, g.streets.streetNorm),
    streetTokens: streetTokenTable(g.streetTokens),
    placeTokens: placeTokenTable(g.placeTokens),
    placeById: byKey(g.places.n, g.places.placeId),
    zipByCode: byKey(g.zipCentroids.n, g.zipCentroids.zipcode),
    intersectionOrder: intersectionOrder(g.intersections),
    segmentsByNorm: groupRows(pack.segments.n, pack.segments.streetNorm),
  };
}

/**
 * The first row index for which `isBefore(row)` is false, over `length` rows.
 * Every prefix range scan in this package is two of these.
 */
export function lowerBound(length, isBefore) {
  let low = 0;
  let high = length;
  while (low < high) {
    const middle = (low + high) >> 1;
    if (isBefore(middle)) {
      low = middle + 1;
    } else {
      high = middle;
    }
  }
  return low;
}

/**
 * Rows of a word index whose token is in `[prefix, bound)`, at most `limit`.
 *
 * Replaces `street._STREET_TOKEN_SQL` and `places._PLACE_TOKEN_SQL`. Both are
 * `SEARCH ... USING PRIMARY KEY (token>? AND token<?)` with `ORDER BY token,
 * position` — which the key already satisfies — so the rows arrive in full
 * PRIMARY KEY order and `LIMIT` cuts the tail of that order, not of a sort.
 *
 * @returns {number[]} table rows
 */
export function tokenRange(tokenTable, prefix, bound, limit) {
  const { order, token } = tokenTable;
  const start = lowerBound(order.length, (i) => token[order[i]] < prefix);
  const rows = [];
  for (let i = start; i < order.length && rows.length < limit; i += 1) {
    if (!(token[order[i]] < bound)) {
      break;
    }
    rows.push(order[i]);
  }
  return rows;
}

/**
 * `[start, end)` into `index.variantOrder` for variants in `[low, high)`.
 *
 * Replaces the range in `street._VARIANT_EXACT_SQL` (with `high` the variant's
 * own successor) and `street._VARIANT_PREFIX_SQL`. Both are
 * `SEARCH street_variant USING PRIMARY KEY (variant…)`, so rows arrive in
 * `(variant, street_norm)` order and the `ORDER BY` costs nothing.
 */
export function variantRange(pack, index, low, high) {
  const variant = pack.geocode.streetVariants.variant;
  const order = index.variantOrder;
  return [
    lowerBound(order.length, (i) => variant[order[i]] < low),
    lowerBound(order.length, (i) => variant[order[i]] < high),
  ];
}

/**
 * `[start, end)` into `index.addressByLon` for `lon` in `[minLon, maxLon]`.
 *
 * Replaces the driving half of `reverse._ADDRESS_IN_BOX_SQL`, whose plan is
 * `SEARCH address_point USING COVERING INDEX ix_address_point_lon
 * (lon>? AND lon<?)`: the longitude is the seek, the latitude is a filter the
 * caller applies, and the rows come back in `(lon, lat, display, rowid)` order.
 * That order is what decides the tie in the Python's `min(rows, key=…)`, which
 * keeps the first of several equally close doors.
 */
export function addressLonRange(pack, index, minLon, maxLon) {
  const lon = pack.geocode.addressPoints.lon;
  const order = index.addressByLon;
  return [
    lowerBound(order.length, (i) => lon[order[i]] < minLon),
    lowerBound(order.length, (i) => lon[order[i]] <= maxLon),
  ];
}

/**
 * `[start, end)` into `index.intersectionOrder` for one ordered street pair.
 *
 * Replaces `intersection._INTERSECTION_SQL`, whose plan is `SEARCH intersection
 * USING COVERING INDEX ix_intersection_pair (a_norm=? AND b_norm=?)`: no
 * `ORDER BY`, so the `LIMIT` takes the first rows in `(lon, lat, display,
 * rowid)` order within the pair.
 */
export function intersectionRange(pack, index, aNorm, bNorm) {
  const { aNorm: a, bNorm: b } = pack.geocode.intersections;
  const order = index.intersectionOrder;
  const pairBefore = (i) => compareText(a[order[i]], aNorm) || compareText(b[order[i]], bNorm);
  return [
    lowerBound(order.length, (i) => pairBefore(i) < 0),
    lowerBound(order.length, (i) => pairBefore(i) <= 0),
  ];
}

// --- building the orders --------------------------------------------------

/**
 * `ix_address_point_street (street_norm, house_number, lon, lat, display,
 * zipcode)`, split by street because every query that uses it is equality on
 * `street_norm`. The trailing rowid is SQLite's, appended to a non-unique
 * index key; it is what settles two doors that agree on all six columns.
 */
function addressByStreet(table) {
  const grouped = groupRows(table.n, table.streetNorm);
  for (const [streetNorm, rows] of grouped) {
    const sorted = [...rows].sort(
      (x, y) =>
        table.houseNumber[x] - table.houseNumber[y] ||
        table.lon[x] - table.lon[y] ||
        table.lat[x] - table.lat[y] ||
        compareText(table.display[x], table.display[y]) ||
        compareNullableText(table.zipcode[x], table.zipcode[y]) ||
        x - y,
    );
    grouped.set(streetNorm, Int32Array.from(sorted));
  }
  return grouped;
}

/** `ix_address_point_lon (lon, lat, display)`, plus the rowid SQLite appends. */
function addressByLon(table) {
  return sortedRows(
    table.n,
    (x, y) =>
      table.lon[x] - table.lon[y] ||
      table.lat[x] - table.lat[y] ||
      compareText(table.display[x], table.display[y]) ||
      x - y,
  );
}

/**
 * `street_variant`'s `PRIMARY KEY (variant, street_norm)`. The table is
 * WITHOUT ROWID, so this is also the order a full scan produces — which is
 * what `street._VARIANTS_SQL` walks.
 */
function variantOrder(table) {
  return sortedRows(
    table.n,
    (x, y) =>
      compareText(table.variant[x], table.variant[y]) ||
      compareText(table.streetNorm[x], table.streetNorm[y]),
  );
}

/** `street_token`'s `PRIMARY KEY (token, position, street_norm, search_name)`. */
function streetTokenTable(table) {
  return {
    order: sortedRows(
      table.n,
      (x, y) =>
        compareText(table.token[x], table.token[y]) ||
        table.position[x] - table.position[y] ||
        compareText(table.streetNorm[x], table.streetNorm[y]) ||
        compareText(table.searchName[x], table.searchName[y]),
    ),
    token: table.token,
    nameId: table.streetNorm,
    searchName: table.searchName,
  };
}

/**
 * `place_token`'s `PRIMARY KEY (token, position, place_id, search_name)`.
 *
 * `place_id` is an INTEGER column, so the index orders it numerically even
 * though `_driver_rows` later sorts the same value as `str(place_id)`. The two
 * orders differ ("10" < "2") and both matter: the numeric one decides which
 * rows `LIMIT 200` keeps, the text one decides `sorted(driver)`.
 */
function placeTokenTable(table) {
  return {
    order: sortedRows(
      table.n,
      (x, y) =>
        compareText(table.token[x], table.token[y]) ||
        table.position[x] - table.position[y] ||
        table.placeId[x] - table.placeId[y] ||
        compareText(table.searchName[x], table.searchName[y]),
    ),
    token: table.token,
    nameId: Array.from(table.placeId, (id) => String(id)),
    searchName: table.searchName,
  };
}

/** `ix_intersection_pair (a_norm, b_norm, lon, lat, display)` plus the rowid. */
function intersectionOrder(table) {
  return sortedRows(
    table.n,
    (x, y) =>
      compareText(table.aNorm[x], table.aNorm[y]) ||
      compareText(table.bNorm[x], table.bNorm[y]) ||
      table.lon[x] - table.lon[y] ||
      table.lat[x] - table.lat[y] ||
      compareText(table.display[x], table.display[y]) ||
      x - y,
  );
}

/**
 * key -> rows carrying it, each in ascending row order.
 *
 * Ascending row order is rowid order, which is what `ix_street_segment_norm`
 * (`street_norm` alone) and every other single-column index produce for one
 * key: `ranges._SEGMENTS_BY_NAME_SQL` has no `ORDER BY` and walks its street's
 * segments in exactly this order.
 */
function groupRows(n, keys) {
  /** @type {Map<any, number[] | Int32Array>} */
  const grouped = new Map();
  for (let row = 0; row < n; row += 1) {
    const existing = grouped.get(keys[row]);
    if (existing === undefined) {
      grouped.set(keys[row], [row]);
    } else {
      existing.push(row);
    }
  }
  for (const [key, rows] of grouped) {
    grouped.set(key, Int32Array.from(rows));
  }
  return grouped;
}

/** key -> the single row carrying it, for the tables whose key is a PRIMARY KEY. */
function byKey(n, keys) {
  const found = new Map();
  for (let row = 0; row < n; row += 1) {
    found.set(keys[row], row);
  }
  return found;
}

function sortedRows(n, compare) {
  const rows = new Array(n);
  for (let row = 0; row < n; row += 1) {
    rows[row] = row;
  }
  rows.sort(compare);
  return Int32Array.from(rows);
}

/** SQLite's `BINARY` collation: `<` on code units, which is Python's `str` order. */
export function compareText(left, right) {
  if (left < right) {
    return -1;
  }
  return left > right ? 1 : 0;
}

/** The same, with SQLite's rule that NULL sorts before every other value. */
function compareNullableText(left, right) {
  if (left === null || left === undefined) {
    return right === null || right === undefined ? 0 : -1;
  }
  if (right === null || right === undefined) {
    return 1;
  }
  return compareText(left, right);
}
