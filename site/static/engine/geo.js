/**
 * The local flat-earth projection every radius query in the engine shares.
 *
 * Port of `curbcheck/engine/geo.py`. Over a 1 km radius at Manhattan's latitude
 * the error against a proper projection is under a metre. Values are the WGS-84
 * metres per degree at 40.75 N.
 *
 * The one difference from the Python: a geometry arrives as a row of a packed
 * coords/offsets column rather than as a GeoJSON string, so `measure` returns
 * only the distance — the caller that needs the geometry asks `pack.lineString`
 * for it.
 */

export const M_PER_DEG_LAT = 111_132.0;
export const M_PER_DEG_LON_AT_EQUATOR = 111_320.0;

// `math.radians(deg)` is `deg * (pi / 180)`; the parentheses keep the constant
// the one Python multiplies by, so the product is bit-for-bit the same double.
const RADIANS_PER_DEGREE = Math.PI / 180;

/**
 * @param {number} lat
 * @returns {number}
 */
export function metersPerDegreeLon(lat) {
  return M_PER_DEG_LON_AT_EQUATOR * Math.cos(lat * RADIANS_PER_DEGREE);
}

/**
 * The (lon, lat) half-widths of a bbox `meters` wide around a point at `lat`.
 *
 * @param {number} lat
 * @param {number} meters
 * @returns {{lon: number, lat: number}}
 */
export function degreePadding(lat, meters) {
  return { lon: meters / metersPerDegreeLon(lat), lat: meters / M_PER_DEG_LAT };
}

/**
 * One row of a coords column with (lon, lat) replaced by metres from (lon0, lat0).
 *
 * Null when the row holds fewer than two vertices, which is what shapely
 * refuses to build a LineString from — `data/` is untrusted at read time
 * (CLAUDE.md) and the pack is read the same way the database was.
 *
 * @param {{coords: ArrayLike<number>, offsets: ArrayLike<number>}} geom
 * @param {number} row
 * @param {number} lon0
 * @param {number} lat0
 * @returns {Float64Array | null}
 */
export function toLocalMeters(geom, row, lon0, lat0) {
  if (row < 0 || row + 1 >= geom.offsets.length) {
    return null;
  }
  const start = geom.offsets[row];
  const end = geom.offsets[row + 1];
  if (end - start < 2) {
    return null;
  }
  const scaleLon = metersPerDegreeLon(lat0);
  const local = new Float64Array((end - start) * 2);
  for (let vertex = start; vertex < end; vertex += 1) {
    const at = (vertex - start) * 2;
    local[at] = (geom.coords[2 * vertex] - lon0) * scaleLon;
    local[at + 1] = (geom.coords[2 * vertex + 1] - lat0) * M_PER_DEG_LAT;
  }
  return local;
}

/**
 * The distance in metres from (lon, lat) to one geometry row, or null when it
 * cannot be read.
 *
 * The query point is the origin of the local frame, which is what the Python
 * passes as `Point(0, 0)`.
 *
 * @param {{coords: ArrayLike<number>, offsets: ArrayLike<number>}} geom
 * @param {number} row
 * @param {number} lon
 * @param {number} lat
 * @returns {{distanceM: number} | null}
 */
export function measure(geom, row, lon, lat) {
  const local = toLocalMeters(geom, row, lon, lat);
  if (local === null) {
    return null;
  }
  let nearest = Infinity;
  for (let at = 0; at + 3 < local.length; at += 2) {
    const distance = pointToSegment(0, 0, local[at], local[at + 1], local[at + 2], local[at + 3]);
    if (distance < nearest) {
      nearest = distance;
    }
  }
  if (!Number.isFinite(nearest)) {
    return null;
  }
  return { distanceM: nearest };
}

/**
 * GEOS's `Distance::pointToSegment`, written out so the two engines agree to
 * the ulp: shapely's `distance` is this function minimised over the segments of
 * the line, and the differential harness compares distances exactly.
 *
 * @param {number} px
 * @param {number} py
 * @param {number} ax
 * @param {number} ay
 * @param {number} bx
 * @param {number} by
 * @returns {number}
 */
export function pointToSegment(px, py, ax, ay, bx, by) {
  // GEOS: if start = end, the segment is a point, so use one endpoint.
  if (ax === bx && ay === by) {
    return distanceToPoint(px, py, ax, ay);
  }
  const dx = bx - ax;
  const dy = by - ay;
  const len2 = dx * dx + dy * dy;
  const r = ((px - ax) * dx + (py - ay) * dy) / len2;
  if (r <= 0.0) {
    return distanceToPoint(px, py, ax, ay);
  }
  if (r >= 1.0) {
    return distanceToPoint(px, py, bx, by);
  }
  const s = ((ay - py) * dx - (ax - px) * dy) / len2;
  return Math.abs(s) * Math.sqrt(len2);
}

/** `Coordinate::distance`: sqrt of the sum of the squared deltas, not `Math.hypot`. */
function distanceToPoint(px, py, ax, ay) {
  const ddx = px - ax;
  const ddy = py - ay;
  return Math.sqrt(ddx * ddx + ddy * ddy);
}
