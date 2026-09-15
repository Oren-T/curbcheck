/**
 * Is a point inside the only area this pack can answer about?
 *
 * Port of `curbcheck/engine/coverage.py`. The pack holds Manhattan and nothing
 * else, so a destination in New Jersey is not a search that found nothing — it
 * is a question this build cannot answer (UX audit P0-3).
 */

import { degreePadding, measure } from "./geo.js";

export const COVERAGE_AREA = "Manhattan";

// A point further than this from every centerline is out of coverage. Measured
// on the 2026-09-15 database: the deepest points of Central Park are 184 m
// (Great Lawn) and 177 m (the Ramble) from a centerline, so the park stays in;
// the Hudson midstream is 1,090 m, so it stays out.
export const COVERAGE_RADIUS_M = 250.0;

/**
 * Whether (lon, lat) is within `radiusM` of any centerline segment.
 *
 * Two passes, the shape `coverage._PREFILTER_SQL` has: a bounding-box prefilter
 * that decodes no geometry, then the exact distance over whatever survives it.
 * The grid stands in for `ix_street_segment_bbox` and returns a superset, so the
 * bbox test below is the SQL's `WHERE` written out — the SQL's extra lower
 * bounds on `min_lon` / `min_lat` are an index-seek trick (a row that reaches
 * the query box cannot begin more than one segment-width before it) and select
 * exactly the same rows as plain box intersection.
 *
 * @param {object} pack
 * @param {number} lon
 * @param {number} lat
 * @param {number} [radiusM]
 * @returns {boolean}
 */
export function withinCoverage(pack, lon, lat, radiusM = COVERAGE_RADIUS_M) {
  const pad = degreePadding(lat, radiusM);
  const minLon = lon - pad.lon;
  const maxLon = lon + pad.lon;
  const minLat = lat - pad.lat;
  const maxLat = lat + pad.lat;
  const segments = pack.segments;
  const bbox = segments.bbox;

  for (const row of pack.index.segmentGrid.query(minLon, minLat, maxLon, maxLat)) {
    if (bbox.minLon[row] > maxLon || bbox.maxLon[row] < minLon) {
      continue;
    }
    if (bbox.minLat[row] > maxLat || bbox.maxLat[row] < minLat) {
      continue;
    }
    const measured = measure(segments.geom, row, lon, lat);
    if (measured !== null && measured.distanceM <= radiusM) {
      return true;
    }
  }
  return false;
}

/**
 * `[minLon, minLat, maxLon, maxLat]` over every centerline, or null when there
 * is none.
 *
 * The map draws this as the edge of what CurbCheck knows. It is the bounding
 * box of the data, not of the borough: Roosevelt and Randalls Islands are in it
 * because CSCL files them under Manhattan.
 *
 * The Python caches this per database file because the radius prefilter wants
 * the same aggregate on every request; here the prefilter is the grid, so the
 * only caller is `/api/health` and a pass over 11,102 rows is cheaper than the
 * cache that would hold it.
 *
 * @param {object} pack
 * @returns {[number, number, number, number] | null}
 */
export function coverageBbox(pack) {
  const bbox = pack.segments.bbox;
  if (pack.segments.n === 0) {
    return null;
  }
  let minLon = Infinity;
  let minLat = Infinity;
  let maxLon = -Infinity;
  let maxLat = -Infinity;
  for (let row = 0; row < pack.segments.n; row += 1) {
    if (bbox.minLon[row] < minLon) {
      minLon = bbox.minLon[row];
    }
    if (bbox.minLat[row] < minLat) {
      minLat = bbox.minLat[row];
    }
    if (bbox.maxLon[row] > maxLon) {
      maxLon = bbox.maxLon[row];
    }
    if (bbox.maxLat[row] > maxLat) {
      maxLat = bbox.maxLat[row];
    }
  }
  return [minLon, minLat, maxLon, maxLat];
}
