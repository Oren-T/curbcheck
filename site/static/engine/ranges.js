/**
 * House numbers placed along a centerline segment's published address ranges.
 *
 * The last rung of the address ladder. OTI's AddressPoint files doors on 784 of
 * the 1,017 streets the centerline knows (docs/DATA.md §5.1); for the other 233
 * the only thing that has an opinion about where number 12 is, is CSCL's own
 * `l_low_hn`/`l_high_hn` pair, and 36 of those streets publish one. Accuracy is
 * the reason it ranks last: over 800 random doors this lands a median 95 ft from
 * the surveyed point, p95 1,634 ft, against 0 ft for a door AddressPoint has.
 *
 * The direction assumption: CSCL states the low house number at a segment's first
 * geometry vertex, so a house number's position within its range is used directly
 * as the normalized position along the line. Where a block was digitized against
 * its address direction the point lands at the wrong end of that one block —
 * under 300 ft of error, and never on the wrong street.
 */

import { betweenPhrase, singleSpaced } from "./labels.js";
import { PY_SPACE } from "../geocode/normalize.js";

// `\d` is spelled `[0-9]` because Python's matches every Unicode decimal digit;
// the cell is a CSCL house-number string and every one of them is ASCII.
const LEADING_DIGITS = new RegExp(`^[${PY_SPACE}]*([0-9]+)`, "u");

/**
 * One side of one centerline segment, with the house-number range it publishes.
 *
 * `geom` is the segment's vertices as `[[lon, lat], …]`. The Python holds the
 * GeoJSON string the database stores and parses it in `point_along`; the pack
 * ships a coordinates column, so the parse happens once in `lineOf` instead.
 */
export class Blockface {
  constructor({ segmentId, streetName, geom, low, high, crossStreets }) {
    this.segmentId = segmentId;
    this.streetName = streetName;
    this.geom = geom;
    this.low = low;
    this.high = high;
    this.crossStreets = crossStreets;
  }

  /**
   * Whether this side carries `houseNumber`, parity included.
   *
   * NYC puts odd numbers on one side of the street and even on the other,
   * so a range whose ends agree on parity only claims numbers of that
   * parity. Ranges whose ends disagree are data errors; those claim both.
   */
  contains(houseNumber) {
    if (!(this.low <= houseNumber && houseNumber <= this.high)) {
      return false;
    }
    if (this.low % 2 !== this.high % 2) {
      return true;
    }
    return houseNumber % 2 === this.low % 2;
  }

  /** Where in [0, 1] `houseNumber` falls within the range. */
  position(houseNumber) {
    if (this.high === this.low) {
      return 0.5;
    }
    return (houseNumber - this.low) / (this.high - this.low);
  }
}

/**
 * Both sides of every segment of `streetNorm` that publishes a house-number range.
 *
 * Replaces `_SEGMENTS_BY_NAME_SQL`, whose plan is `SEARCH ss USING INDEX
 * ix_street_segment_norm (street_norm=?)` with two `LEFT JOIN`s on the node
 * primary key and no `ORDER BY`: a single-column index on one key yields rowid
 * order, which is what `index.segmentsByNorm` holds. The cross-street names
 * come along on every segment read: they are the second line under a candidate,
 * and a second query per candidate to fetch them would be one per keystroke
 * once the box autocompletes.
 *
 * @returns {Blockface[]}
 */
export function blockfaces(pack, index, streetNorm) {
  const segments = pack.segments;
  const faces = [];
  for (const row of index.segmentsByNorm.get(streetNorm) ?? []) {
    if (segments.leftLowAddress[row] === null && segments.rightLowAddress[row] === null) {
      continue;
    }
    for (const [lowColumn, highColumn] of [
      ["leftLowAddress", "leftHighAddress"],
      ["rightLowAddress", "rightHighAddress"],
    ]) {
      const low = houseInt(segments[lowColumn][row]);
      const high = houseInt(segments[highColumn][row]);
      if (low === null || high === null) {
        continue;
      }
      faces.push(
        new Blockface({
          segmentId: segments.segmentId[row],
          streetName: segments.streetName[row],
          geom: lineOf(segments, row),
          low: Math.min(low, high),
          high: Math.max(low, high),
          crossStreets: _crossStreets(pack, row),
        }),
      );
    }
  }
  return faces;
}

/**
 * (lon, lat) a fraction of the way along a segment's geometry.
 *
 * GEOS reached through `shapely.interpolate(normalized=True)`: the distance is
 * the fraction times the line's own length, both summed as
 * `sqrt(dx*dx + dy*dy)` per segment and in vertex order, and the point is the
 * first segment whose running total passes it.
 */
export function pointAlong(geom, position) {
  if (geom === null || geom.length < 2) {
    return null;
  }
  const clamped = Math.min(Math.max(position, 0.0), 1.0);
  const target = clamped * lineLength(geom);
  if (target <= 0.0) {
    return [geom[0][0], geom[0][1]];
  }
  let total = 0.0;
  for (let i = 1; i < geom.length; i += 1) {
    const segmentLength = pointDistance(geom[i - 1], geom[i]);
    if (total + segmentLength > target) {
      const fraction = (target - total) / segmentLength;
      return [
        geom[i - 1][0] + fraction * (geom[i][0] - geom[i - 1][0]),
        geom[i - 1][1] + fraction * (geom[i][1] - geom[i - 1][1]),
      ];
    }
    total += segmentLength;
  }
  const last = geom[geom.length - 1];
  return [last[0], last[1]];
}

/**
 * A row's geometry as one line, `[[lon, lat], …]`, or null when it has none.
 *
 * The Python parses the stored GeoJSON string and merges the MultiLineStrings
 * CSCL publishes (docs/DATA.md §2.2). The pack ships a coordinates column that
 * is already one LineString per row — all 47,274 geometries were measured to be
 * LineStrings (docs/STATIC_SITE.md) — so there is nothing to merge and anything
 * with no vertices is null, which is what an unreadable geometry was.
 */
export function lineOf(table, row) {
  const { coords, offsets } = table.geom;
  const start = offsets[row];
  const end = offsets[row + 1];
  if (end <= start) {
    return null;
  }
  const line = new Array(end - start);
  for (let i = start; i < end; i += 1) {
    line[i - start] = [coords[2 * i], coords[2 * i + 1]];
  }
  return line;
}

/** Leading digits of a house-number cell. `'1510'`, `'1510 REAR'`, `''`, null. */
export function houseInt(value) {
  if (value === null || value === undefined) {
    return null;
  }
  const match = LEADING_DIGITS.exec(String(value));
  return match ? Number.parseInt(match[1], 10) : null;
}

/** The block's cross streets as a phrase, or null when neither node names one. */
export function _crossStreets(pack, row) {
  const fromNode = pack.segments.fromNode[row];
  const toNode = pack.segments.toNode[row];
  return betweenPhrase(
    singleSpaced(pack.segments.streetName[row]),
    fromNode < 0 ? null : pack.nodes.streetNames[fromNode],
    toNode < 0 ? null : pack.nodes.streetNames[toNode],
  );
}

/** GEOS `Length::ofLine`, summed in vertex order. */
function lineLength(line) {
  let total = 0.0;
  for (let i = 1; i < line.length; i += 1) {
    total += pointDistance(line[i - 1], line[i]);
  }
  return total;
}

/** GEOS `Coordinate::distance`: `sqrt(dx*dx + dy*dy)`, not `hypot`. */
function pointDistance(a, b) {
  const dx = b[0] - a[0];
  const dy = b[1] - a[1];
  return Math.sqrt(dx * dx + dy * dy);
}
