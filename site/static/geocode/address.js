/**
 * The house-number rungs, best first.
 *
 * A surveyed door, then the number placed between its two nearest same-parity
 * neighbours, then the nearest surveyed number on the street, then CSCL's own
 * published address range. Each rung is tried only when the one above it found
 * nothing, so a typed address costs the cheapest lookup that can answer it.
 */

import { blockfaces, pointAlong } from "../engine/ranges.js";
import { singleSpaced } from "../engine/labels.js";
import {
  CONFIDENCE_ADDRESS_INTERPOLATED,
  CONFIDENCE_ADDRESS_POINT,
  CONFIDENCE_ADDRESS_RANGE,
  CONFIDENCE_NEAR_ADDRESS,
  DEFAULT_SECONDARY,
  FUZZY_CONFIDENCE_PENALTY,
  GeocodeKind,
  geocodeCandidate,
  zipSecondary,
} from "./candidates.js";
import { compareText, lowerBound } from "./index.js";
import { streetDisplay } from "./street.js";

// A bracket wider than one hundred-block is two different blocks with a gap
// between them, not a run of missing doors; interpolating across it would
// invent a location rather than fill one in.
export const MAX_INTERPOLATION_SPAN = 100;

/**
 * The rows of one street, in `ix_address_point_street` order.
 *
 * Every lookup below is a seek into that index — `(street_norm, house_number,
 * lon, lat, display, zipcode)` and then the rowid — so the four neighbour
 * queries are a binary search on `house_number` and a step to one side. The
 * Python writes them out as four SQL literals rather than building them from a
 * template, which is what lets `tests/test_engine_sql_safety.py` enforce that
 * none of them is ever formatted; here the same four are four range bounds.
 */
function streetRows(index, streetNorm) {
  return index.addressByStreet.get(streetNorm) ?? new Int32Array(0);
}

/** The best rung each candidate street can offer for this house number. */
export function addressCandidates(pack, index, streets, houseNumber, limit) {
  const found = [];
  for (const [streetNorm, fuzzy] of streets) {
    const penalty = fuzzy ? FUZZY_CONFIDENCE_PENALTY : 1.0;
    for (const candidate of bestRung(pack, index, streetNorm, houseNumber, limit)) {
      found.push(
        geocodeCandidate({
          label: candidate.label,
          lat: candidate.lat,
          lon: candidate.lon,
          kind: candidate.kind,
          confidence: candidate.confidence * penalty,
          secondary: candidate.secondary,
        }),
      );
    }
  }
  return found;
}

export function bestRung(pack, index, streetNorm, houseNumber, limit) {
  const surveyed = _surveyedDoors(pack, index, streetNorm, houseNumber, limit);
  if (surveyed.length) {
    return surveyed;
  }
  const interpolated = _interpolatedDoor(pack, index, streetNorm, houseNumber);
  if (interpolated !== null) {
    return [interpolated];
  }
  const near = _nearestDoor(pack, index, streetNorm, houseNumber);
  if (near !== null) {
    return [near];
  }
  return _rangeCandidates(pack, index, streetNorm, houseNumber);
}

/**
 * Every AddressPoint row with this exact number on this street.
 *
 * Replaces `_ADDRESS_EXACT_SQL` (`… WHERE street_norm = ? AND house_number = ?
 * ORDER BY display LIMIT ?`), whose plan is a seek into
 * `ix_address_point_street` followed by `USE TEMP B-TREE FOR ORDER BY`: the
 * rows reach the sorter in index order and the sort is by `display` alone, so a
 * stable sort of the index range reproduces it, and `LIMIT` cuts the sorted
 * list. A building with several doors is several rows and is offered as several
 * candidates rather than deduplicated (docs/DATA.md §5.1).
 */
export function _surveyedDoors(pack, index, streetNorm, houseNumber, limit) {
  const table = pack.geocode.addressPoints;
  const rows = streetRows(index, streetNorm);
  const start = lowerBound(rows.length, (i) => table.houseNumber[rows[i]] < houseNumber);
  const end = lowerBound(rows.length, (i) => table.houseNumber[rows[i]] <= houseNumber);
  const exact = Array.from(rows.slice(start, end));
  exact.sort((x, y) => compareText(table.display[x], table.display[y]));
  return exact.slice(0, limit).map((row) =>
    geocodeCandidate({
      label: singleSpaced(table.display[row]),
      lat: table.lat[row],
      lon: table.lon[row],
      kind: GeocodeKind.ADDRESS,
      confidence: CONFIDENCE_ADDRESS_POINT,
      secondary: zipSecondary(table.zipcode[row]),
    }),
  );
}

/**
 * The number placed between its two nearest surveyed same-parity neighbours.
 *
 * NYC puts odd numbers on one side of the street and even on the other, so
 * interpolating between two surveyed points of the same parity stays on the
 * correct side. Only 24.4% of the house numbers the centerline implies have
 * an address point (docs/DATA.md §5.1) — 1519 3 AVE is absent while 1517 and
 * 1529 are there — so this rung carries most typed addresses.
 */
export function _interpolatedDoor(pack, index, streetNorm, houseNumber) {
  const table = pack.geocode.addressPoints;
  const parity = houseNumber % 2;
  const below = _neighbour(pack, index, streetNorm, houseNumber, parity, "below");
  const above = _neighbour(pack, index, streetNorm, houseNumber, parity, "above");
  if (below === null || above === null) {
    return null;
  }
  const low = table.houseNumber[below];
  const high = table.houseNumber[above];
  if (high - low > MAX_INTERPOLATION_SPAN) {
    return null;
  }
  const position = (houseNumber - low) / (high - low);
  return geocodeCandidate({
    label: `${houseNumber} ${streetDisplay(pack, index, streetNorm)}`,
    lat: table.lat[below] + (table.lat[above] - table.lat[below]) * position,
    lon: table.lon[below] + (table.lon[above] - table.lon[below]) * position,
    kind: GeocodeKind.ADDRESS,
    confidence: CONFIDENCE_ADDRESS_INTERPOLATED,
    secondary: `${DEFAULT_SECONDARY} · between ${low} and ${high}`,
  });
}

/**
 * The closest surveyed number on that street, either side. Two index seeks.
 *
 * The rung for a number outside every run of doors — 1 3 AVE, 9999 3 AVE.
 * Against real address points this is both simpler and closer than reaching
 * for a hundred-block corner, because the neighbouring number is surveyed.
 */
export function _nearestDoor(pack, index, streetNorm, houseNumber) {
  const table = pack.geocode.addressPoints;
  const neighbours = [
    _neighbour(pack, index, streetNorm, houseNumber, null, "atOrBelow"),
    _neighbour(pack, index, streetNorm, houseNumber, null, "atOrAbove"),
  ];
  const found = neighbours.filter((row) => row !== null);
  if (!found.length) {
    return null;
  }
  // `min(found, key=…)` keeps the first of two equally close neighbours.
  let nearest = found[0];
  for (const row of found.slice(1)) {
    if (
      Math.abs(table.houseNumber[row] - houseNumber) <
      Math.abs(table.houseNumber[nearest] - houseNumber)
    ) {
      nearest = row;
    }
  }
  return geocodeCandidate({
    label: `near ${singleSpaced(table.display[nearest])}`,
    lat: table.lat[nearest],
    lon: table.lon[nearest],
    kind: GeocodeKind.ADDRESS,
    confidence: CONFIDENCE_NEAR_ADDRESS,
    secondary: DEFAULT_SECONDARY,
  });
}

/**
 * The one row `_ADDRESS_BELOW_SQL`, `_ADDRESS_ABOVE_SQL`,
 * `_ADDRESS_AT_OR_BELOW_SQL` or `_ADDRESS_AT_OR_ABOVE_SQL` returns, or null.
 *
 * All four seek `ix_address_point_street` and take `LIMIT 1` off an
 * `ORDER BY house_number` the index already satisfies, so the row is the first
 * one the scan meets in that direction: ascending it is the lowest
 * `(lon, lat, display, zipcode, rowid)` of the chosen house number, descending
 * it is the highest. `parity` is the `house_number % 2 = ?` filter the two
 * interpolation queries carry and null for the two that do not.
 */
function _neighbour(pack, index, streetNorm, houseNumber, parity, direction) {
  const table = pack.geocode.addressPoints;
  const rows = streetRows(index, streetNorm);
  const descending = direction === "below" || direction === "atOrBelow";
  const inclusive = direction === "atOrBelow" || direction === "atOrAbove";
  // The two ends of the rows carrying `houseNumber` itself, which is the only
  // thing the four queries disagree about.
  const firstEqual = lowerBound(rows.length, (i) => table.houseNumber[rows[i]] < houseNumber);
  const pastEqual = lowerBound(rows.length, (i) => table.houseNumber[rows[i]] <= houseNumber);
  if (descending) {
    for (let i = (inclusive ? pastEqual : firstEqual) - 1; i >= 0; i -= 1) {
      if (parity === null || table.houseNumber[rows[i]] % 2 === parity) {
        return rows[i];
      }
    }
    return null;
  }
  for (let i = inclusive ? firstEqual : pastEqual; i < rows.length; i += 1) {
    if (parity === null || table.houseNumber[rows[i]] % 2 === parity) {
      return rows[i];
    }
  }
  return null;
}

/**
 * The last rung: CSCL's own address range, for a street AddressPoint skipped.
 *
 * 233 of the 1,017 centerline streets have no surveyed door and 36 of those
 * publish a range (docs/DATA.md §5.1). Two blockfaces claiming one number
 * means the source ranges overlap; both are offered rather than one picked.
 */
export function _rangeCandidates(pack, index, streetNorm, houseNumber) {
  const found = [];
  for (const face of blockfaces(pack, index, streetNorm)) {
    if (!face.contains(houseNumber)) {
      continue;
    }
    const candidate = _rangeCandidate(face, houseNumber);
    if (candidate !== null) {
      found.push(candidate);
    }
  }
  return found;
}

/** @param {import("../engine/ranges.js").Blockface} face */
export function _rangeCandidate(face, houseNumber) {
  const point = pointAlong(face.geom, face.position(houseNumber));
  if (point === null) {
    return null;
  }
  const [lon, lat] = point;
  return geocodeCandidate({
    label: `${houseNumber} ${singleSpaced(face.streetName)}`,
    lat,
    lon,
    kind: GeocodeKind.ADDRESS,
    confidence: CONFIDENCE_ADDRESS_RANGE,
    secondary: face.crossStreets || DEFAULT_SECONDARY,
  });
}
