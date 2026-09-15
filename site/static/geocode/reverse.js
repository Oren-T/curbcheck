/**
 * What a dropped pin is nearest to.
 *
 * The inverse of `suggest`, and the only geocoder path that starts from a point
 * rather than from text: a surveyed door if one is close enough to be the honest
 * answer, otherwise the corner, otherwise the street the pin is on.
 *
 * The Python reaches for shapely here rather than for `engine.geo.measure`,
 * because it needs a point *on* the nearest line and not only its distance. The
 * three GEOS calls it makes — `LineString.distance`, `.project`, `.interpolate`
 * — are written out at the foot of this file so the two engines agree to the
 * ulp; the differential harness compares coordinates exactly.
 */

import { M_PER_DEG_LAT, degreePadding, metersPerDegreeLon, pointToSegment } from "../engine/geo.js";
import { singleSpaced } from "../engine/labels.js";
import { jsonStringList } from "../engine/model.js";
import { lineOf } from "../engine/ranges.js";
import { DEFAULT_SECONDARY, GeocodeKind, reverseMatch } from "./candidates.js";
import { addressLonRange, compareText } from "./index.js";

// A pin closer than this to a surveyed door is answered with that door.
// 60 m is about one and a half Manhattan lots: further out, the corner is the
// honest answer, because a door across the street is not where the pin is.
export const REVERSE_ADDRESS_MAX_M = 60.0;

// How far a reverse lookup will look for a corner or a block at all. Beyond
// `engine.coverage.COVERAGE_RADIUS_M` the point is not in coverage anyway.
export const REVERSE_SEARCH_M = 250.0;

// Bounding boxes the reverse lookup tries in turn, in degrees (~110 m, ~440 m,
// ~1.8 km). The first covers `REVERSE_ADDRESS_MAX_M` with room to spare; the
// wider two only ever run over the rivers and the middle of the parks.
const REVERSE_BOX_DEGREES = [0.001, 0.004, 0.016];

/**
 * What a dropped pin is nearest to, or null when nothing is near it at all.
 *
 * The nearest surveyed door within `REVERSE_ADDRESS_MAX_M`, otherwise the
 * nearest corner, otherwise the street the pin is on. A pin is the one
 * destination the user cannot read back to themselves, and
 * "40.778830, -73.953985" in the status line is not a place (UX audit P1-4,
 * P1-6).
 *
 * @returns {import("./candidates.js").ReverseMatch | null}
 */
export function reverseGeocode(pack, index, lon, lat) {
  if (!Number.isFinite(lon) || !Number.isFinite(lat)) {
    return null;
  }
  const door = _nearestAddressPoint(pack, index, lon, lat);
  if (door !== null) {
    return door;
  }
  const corner = _nearestCorner(pack, lon, lat);
  if (corner !== null) {
    return corner;
  }
  return _nearestStreet(pack, _segmentsNear(pack, lon, lat), lon, lat);
}

/**
 * The closest AddressPoint within `REVERSE_ADDRESS_MAX_M`, as "near <door>".
 *
 * Replaces `_ADDRESS_IN_BOX_SQL`, whose plan is `SEARCH address_point USING
 * COVERING INDEX ix_address_point_lon (lon>? AND lon<?)`: the longitude is the
 * seek and the latitude is a filter, so the rows arrive in `(lon, lat, display,
 * rowid)` order. That is the order `min(rows, key=…)` breaks its tie in — it
 * keeps the first of several equally close doors — so it is the order
 * `index.addressByLon` is walked in here. The box is widened twice, which only
 * matters over the rivers and the middle of the parks, and the distance test at
 * the end is what enforces the 60 m rule.
 */
function _nearestAddressPoint(pack, index, lon, lat) {
  const table = pack.geocode.addressPoints;
  const scaleLon = metersPerDegreeLon(lat);
  for (const span of REVERSE_BOX_DEGREES) {
    const rows = _addressesInBox(pack, index, lon - span, lon + span, lat - span, lat + span);
    if (!rows.length) {
      continue;
    }
    let best = rows[0];
    let bestDistance = _distanceM(lon, lat, table.lon[best], table.lat[best], scaleLon);
    for (let i = 1; i < rows.length; i += 1) {
      const distance = _distanceM(lon, lat, table.lon[rows[i]], table.lat[rows[i]], scaleLon);
      if (distance < bestDistance) {
        best = rows[i];
        bestDistance = distance;
      }
    }
    // Measured again from the winner, as the Python does, so the rounding
    // below sees the same double whichever row won.
    const distanceM = _distanceM(lon, lat, table.lon[best], table.lat[best], scaleLon);
    if (distanceM > REVERSE_ADDRESS_MAX_M) {
      return null;
    }
    return reverseMatch({
      label: `near ${singleSpaced(table.display[best])}`,
      secondary: DEFAULT_SECONDARY,
      kind: GeocodeKind.ADDRESS,
      lat: table.lat[best],
      lon: table.lon[best],
      distanceM: roundTenth(distanceM),
    });
  }
  return null;
}

/** `lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?`, both bounds inclusive. */
function _addressesInBox(pack, index, minLon, maxLon, minLat, maxLat) {
  const table = pack.geocode.addressPoints;
  const order = index.addressByLon;
  const [start, end] = addressLonRange(pack, index, minLon, maxLon);
  const rows = [];
  for (let i = start; i < end; i += 1) {
    const row = order[i];
    if (table.lat[row] >= minLat && table.lat[row] <= maxLat) {
      rows.push(row);
    }
  }
  return rows;
}

/**
 * The nearest centerline node that names at least one street.
 *
 * Replaces `_NODES_IN_BBOX_SQL`, whose plan is `SCAN street_node`: the rows
 * arrive in rowid order, which is the pack's row order, and the Python keeps
 * only a *strictly* closer node than the best so far, so the first of two
 * equally close corners wins in exactly that order.
 */
function _nearestCorner(pack, lon, lat) {
  const pad = degreePadding(lat, REVERSE_SEARCH_M);
  const scaleLon = metersPerDegreeLon(lat);
  const nodes = pack.nodes;
  let best = null;
  for (let row = 0; row < nodes.n; row += 1) {
    const nodeLon = nodes.lon[row];
    const nodeLat = nodes.lat[row];
    if (
      nodeLon < lon - pad.lon ||
      nodeLon > lon + pad.lon ||
      nodeLat < lat - pad.lat ||
      nodeLat > lat + pad.lat
    ) {
      continue;
    }
    const names = jsonStringList(nodes.streetNames[row]);
    if (!names.length) {
      continue;
    }
    const distanceM = _distanceM(lon, lat, nodeLon, nodeLat, scaleLon);
    if (distanceM > REVERSE_SEARCH_M || (best !== null && distanceM >= best.distance_m)) {
      continue;
    }
    best = reverseMatch({
      label: _nodeLabel(names),
      secondary: DEFAULT_SECONDARY,
      kind: GeocodeKind.INTERSECTION,
      lat: nodeLat,
      lon: nodeLon,
      distanceM: roundTenth(distanceM),
    });
  }
  return best;
}

/**
 * Every centerline within `REVERSE_SEARCH_M`, nearest first, in local metres.
 *
 * Replaces `_SEGMENTS_IN_BBOX_SQL`. Its plan seeks `ix_street_segment_bbox`, but
 * the Python sorts what survives by `(distance_m, segment_id)` and `segment_id`
 * is a primary key, so the order the rows arrived in never reaches the answer:
 * the grid's superset, filtered by the same four bounds, is enough.
 *
 * @returns {Array<{row: number, line: number[][], distanceM: number}>}
 */
function _segmentsNear(pack, lon, lat) {
  const pad = degreePadding(lat, REVERSE_SEARCH_M);
  const minLon = lon - pad.lon;
  const maxLon = lon + pad.lon;
  const minLat = lat - pad.lat;
  const maxLat = lat + pad.lat;
  const bbox = pack.segments.bbox;

  const nearby = [];
  for (const row of pack.index.segmentGrid.query(minLon, minLat, maxLon, maxLat)) {
    if (bbox.maxLon[row] < minLon || bbox.minLon[row] > maxLon) {
      continue;
    }
    if (bbox.maxLat[row] < minLat || bbox.minLat[row] > maxLat) {
      continue;
    }
    const line = _localLine(pack, row, lon, lat);
    if (line === null) {
      continue;
    }
    const distanceM = lineDistanceToOrigin(line);
    if (distanceM <= REVERSE_SEARCH_M) {
      nearby.push({ row, line, distanceM });
    }
  }
  const segmentId = pack.segments.segmentId;
  nearby.sort(
    (a, b) => a.distanceM - b.distanceM || compareText(segmentId[a.row], segmentId[b.row]),
  );
  return nearby;
}

/** The street the pin is on, for a block with no door and no corner near it. */
function _nearestStreet(pack, nearby, lon, lat) {
  if (!nearby.length) {
    return null;
  }
  const segment = nearby[0];
  const point = interpolateLength(segment.line, projectOrigin(segment.line));
  return reverseMatch({
    label: singleSpaced(pack.segments.streetName[segment.row]),
    secondary: DEFAULT_SECONDARY,
    kind: GeocodeKind.STREET,
    lat: lat + point[1] / M_PER_DEG_LAT,
    lon: lon + point[0] / metersPerDegreeLon(lat),
    distanceM: roundTenth(segment.distanceM),
  });
}

/** One segment's geometry in metres from the query point, or null when it has none. */
function _localLine(pack, row, lon0, lat0) {
  const line = lineOf(pack.segments, row);
  if (line === null) {
    return null;
  }
  const scaleLon = metersPerDegreeLon(lat0);
  return line.map(([x, y]) => [(x - lon0) * scaleLon, (y - lat0) * M_PER_DEG_LAT]);
}

function _nodeLabel(names) {
  if (names.length >= 2) {
    return names
      .slice(0, 2)
      .map((name) => singleSpaced(name))
      .join(" & ");
  }
  return names.length ? singleSpaced(names[0]) : "";
}

/** Flat-earth metres between two nearby points. Under a metre of error over 1 km. */
function _distanceM(lonA, latA, lonB, latB, scaleLon) {
  return Math.hypot((lonA - lonB) * scaleLon, (latA - latB) * M_PER_DEG_LAT);
}

// --- the three GEOS calls shapely makes here ------------------------------

/**
 * `LineString.distance(Point(0, 0))`: GEOS minimises `Distance::pointToSegment`
 * over the line's segments, in vertex order and keeping a strictly smaller one.
 */
function lineDistanceToOrigin(line) {
  let nearest = Infinity;
  for (let i = 1; i < line.length; i += 1) {
    const distance = pointToSegment(0, 0, line[i - 1][0], line[i - 1][1], line[i][0], line[i][1]);
    if (distance < nearest) {
      nearest = distance;
    }
  }
  return nearest;
}

/**
 * `LineString.project(Point(0, 0))`: the length along the line of the closest
 * point on it.
 *
 * GEOS finds the segment with the smallest `pointToSegment` (strictly smaller,
 * so the first of several wins), takes that segment's clamped projection
 * fraction, and then converts the `(segment, fraction)` back into a length by
 * re-walking the line — `LocationIndexOfPoint::indexOf` followed by
 * `LengthLocationMap::getLength`. Going through the length is what makes the
 * round trip lossy, so it is reproduced rather than short-circuited.
 */
function projectOrigin(line) {
  let bestIndex = 0;
  let bestFraction = -1.0;
  let bestDistance = Infinity;
  for (let i = 1; i < line.length; i += 1) {
    const distance = pointToSegment(0, 0, line[i - 1][0], line[i - 1][1], line[i][0], line[i][1]);
    if (distance < bestDistance) {
      bestIndex = i - 1;
      bestFraction = segmentFraction(line[i - 1], line[i]);
      bestDistance = distance;
    }
  }
  let total = 0.0;
  for (let i = 1; i < line.length; i += 1) {
    const length = pointDistance(line[i - 1], line[i]);
    if (i - 1 === bestIndex) {
      return total + length * bestFraction;
    }
    total += length;
  }
  return total;
}

/** GEOS `LineSegment::segmentFraction` of the origin, clamped into [0, 1]. */
function segmentFraction(a, b) {
  if (a[0] === 0 && a[1] === 0) {
    return 0.0;
  }
  if (b[0] === 0 && b[1] === 0) {
    return 1.0;
  }
  const dx = b[0] - a[0];
  const dy = b[1] - a[1];
  const len2 = dx * dx + dy * dy;
  if (len2 <= 0.0) {
    return 1.0;
  }
  const fraction = ((0 - a[0]) * dx + (0 - a[1]) * dy) / len2;
  if (fraction < 0.0) {
    return 0.0;
  }
  return fraction > 1.0 || Number.isNaN(fraction) ? 1.0 : fraction;
}

/**
 * `LineString.interpolate(distance)`: GEOS `LengthLocationMap::getLocationForward`
 * walks the segments and stops at the first whose running total *passes* the
 * distance, then reads the point off that segment.
 */
function interpolateLength(line, distance) {
  if (distance <= 0.0) {
    return [line[0][0], line[0][1]];
  }
  let total = 0.0;
  for (let i = 1; i < line.length; i += 1) {
    const length = pointDistance(line[i - 1], line[i]);
    if (total + length > distance) {
      const fraction = (distance - total) / length;
      return [
        line[i - 1][0] + fraction * (line[i][0] - line[i - 1][0]),
        line[i - 1][1] + fraction * (line[i][1] - line[i - 1][1]),
      ];
    }
    total += length;
  }
  const last = line[line.length - 1];
  return [last[0], last[1]];
}

/** GEOS `Coordinate::distance`: `sqrt(dx*dx + dy*dy)`, not `Math.hypot`. */
function pointDistance(a, b) {
  const dx = b[0] - a[0];
  const dy = b[1] - a[1];
  return Math.sqrt(dx * dx + dy * dy);
}

/**
 * Python's `round(value, 1)`, which is half-to-even on the double's *exact*
 * value — not `Math.round(x * 10) / 10`, which is half-away-from-zero on a
 * value the multiplication has already perturbed. `toFixed(20)` is correctly
 * rounded by specification and twenty digits separate any two doubles this
 * small, so the tie below is a tie in the double and not in its printing.
 */
function roundTenth(value) {
  if (!Number.isFinite(value) || Math.abs(value) >= 1e21) {
    return value;
  }
  const text = value.toFixed(20);
  const negative = text.startsWith("-");
  const [whole, fraction] = (negative ? text.slice(1) : text).split(".");
  const kept = `${whole}${fraction[0]}`;
  const rest = fraction.slice(1);
  const roundUp =
    rest > "5".padEnd(rest.length, "0") ||
    (rest === "5".padEnd(rest.length, "0") && (Number(fraction[0]) & 1) === 1);
  const scaled = BigInt(kept) + (roundUp ? 1n : 0n);
  return Number(`${negative ? "-" : ""}${scaled}e-1`);
}
