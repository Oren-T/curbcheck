/**
 * The ranked suggestion engine: which rungs to try, in what order, and what survives.
 *
 * `suggest` is the whole ladder and `geocode` is `suggest` minus anything the
 * rest of the app could not answer about. The order the rungs are tried in is
 * the latency budget: a correctly-typed prefix must never pay for a rung that
 * exists for a query which is not what was typed.
 */

import { withinCoverage } from "../engine/coverage.js";
import { fold } from "./normalize.js";
import { addressCandidates } from "./address.js";
import {
  CONFIDENCE_STREET_EXACT,
  CONFIDENCE_STREET_HALF_QUERY,
  CONFIDENCE_STREET_WHOLE_QUERY,
  GeocodeKind,
  MAX_CANDIDATES,
} from "./candidates.js";
import { compareText } from "./index.js";
import { intersectionCandidates } from "./intersection.js";
import { placeCandidates, zipCandidates } from "./places.js";
import { cleanQuery, parseQuery } from "./query.js";
import {
  MAX_STREETS_PER_ADDRESS,
  namesAStreet,
  resolveStreet,
  streetCandidates,
  streetNameCandidates,
  streetsFrom,
} from "./street.js";

// A street's pin is a vertex of one of its own centerline segments and a corner
// is a segment endpoint (`etl.addresses._street_points`, `_write_intersections`),
// so both are at distance zero from the centerline by construction and asking
// `withinCoverage` costs a scan to learn nothing. The kinds read off another
// dataset -- a surveyed door, a place, a ZIP centroid -- are still checked. On
// the data mount this is the difference between 15 ms and 1,135 ms for
// "broadwa", whose eight street candidates are spread the length of the island.
const ON_THE_CENTERLINE = new Set([GeocodeKind.STREET, GeocodeKind.INTERSECTION]);

/**
 * Ranked suggestions for a partly-typed query, best first, at most `limit`.
 *
 * Returns an empty list for anything unparseable or unmatched: not finding an
 * address is an answer, not an error. Never throws on hostile input, and
 * never spends more than one prefix scan on the common case — the fuzzy pass
 * only runs when the exact and prefix passes found nothing.
 *
 * The Python wraps the lookup in `db.read_snapshot`, one read transaction so
 * the whole answer comes off one file snapshot; the pack is already one
 * immutable snapshot, so there is nothing to open here.
 */
export function suggest(pack, index, text, limit = MAX_CANDIDATES) {
  if (limit < 1) {
    throw new RangeError("limit must be at least 1");
  }
  const cleaned = cleanQuery(text);
  const query = parseQuery(cleaned);
  if (query === null) {
    return [];
  }
  return _rank(_lookup(pack, index, query, cleaned, limit), limit);
}

/**
 * `suggest`, minus anything the rest of the app could not answer about.
 *
 * This is what the worker's `geocode` and `search` operations call. Offering a
 * destination and then refusing to search it is the shape of failure the
 * coverage rule exists to end (UX audit P0-3), so the filter lives between the
 * two rather than in either.
 */
export function geocode(pack, index, text, limit = MAX_CANDIDATES) {
  return _inCoverage(pack, suggest(pack, index, text, limit), limit);
}

function _lookup(pack, index, query, cleaned, limit) {
  if (query.kind === "zip") {
    return zipCandidates(pack, index, query.zipcode);
  }
  if (query.kind === "intersection") {
    const corners = intersectionCandidates(pack, index, query, limit);
    if (corners.length) {
      return corners;
    }
    // Two streets that never share a centerline node. Offering each one
    // separately is honest; claiming a corner that is not in the data
    // would not be. The word rungs run too, because "and" separates two
    // streets and joins two words of a name — "art and design" is a school,
    // not a corner — and only an empty corner says which was meant.
    return [
      ...streetCandidates(pack, index, query.first, limit, CONFIDENCE_STREET_HALF_QUERY),
      ...streetCandidates(pack, index, query.second, limit, CONFIDENCE_STREET_HALF_QUERY),
      ...streetNameCandidates(pack, index, cleaned, limit),
      ...placeCandidates(pack, index, cleaned, limit),
    ];
  }
  const whole = fold(cleaned);
  const namedExactly = namesAStreet(pack, index, whole);
  // "5 AVE" and "86 ST" parse as a house number on a street called "AVE" or
  // "ST", and the prefix scan would answer with 5 AVE A and 86 ST NICHOLAS
  // AVE. When the whole string is itself a street name, that is what was
  // typed, so the house-number reading is not taken at all.
  if (query.kind === "address" && !namedExactly) {
    return _addressOrItsStreet(pack, index, query, cleaned, limit);
  }
  // "1519 3rd ave" is neither a street name nor a place name, so these two
  // passes run only once the address reading has come back empty: running
  // them anyway makes every keystroke pay for the street pass's fuzzy scan.
  const base = namedExactly ? CONFIDENCE_STREET_EXACT : CONFIDENCE_STREET_WHOLE_QUERY;
  return [
    ...streetCandidates(pack, index, whole, limit, base),
    ...streetNameCandidates(pack, index, cleaned, limit),
    ...placeCandidates(pack, index, cleaned, limit),
  ];
}

/**
 * The house number if anything can place it, otherwise the street it named.
 *
 * The streets are resolved once and reused, so a query that matches nothing
 * pays for the edit-distance scan once rather than twice. "1 police plaza"
 * and "350 5th" both parse as house numbers and only one of them is one, so
 * the word-matching rungs run here too once the address reading is empty.
 */
function _addressOrItsStreet(pack, index, query, cleaned, limit) {
  const streets = resolveStreet(pack, index, query.street).slice(0, MAX_STREETS_PER_ADDRESS);
  const found = addressCandidates(pack, index, streets, query.houseNumber, limit);
  if (found.length) {
    return found;
  }
  // "200 E 85 ST" on a street with neither a surveyed door nor a published
  // range is E 85 ST, not nothing.
  return [
    ...streetsFrom(pack, index, streets, CONFIDENCE_STREET_HALF_QUERY),
    ...placeCandidates(pack, index, cleaned, limit),
  ];
}

/** Best first, one row per (kind, label), capped at `limit`. */
function _rank(candidates, limit) {
  /** @type {Map<string, object>} */
  const best = new Map();
  for (const candidate of candidates) {
    const key = `${candidate.kind}\u0000${candidate.label}`;
    const previous = best.get(key);
    if (previous === undefined || candidate.confidence > previous.confidence) {
      best.set(key, candidate);
    }
  }
  // A Map iterates in insertion order and a JS sort is stable, which together
  // are Python's `sorted()` over an insertion-ordered dict. Every label is
  // ASCII (measured: 0 non-ASCII rows), so `String.length` is `len()`.
  const ordered = [...best.values()].sort(
    (a, b) =>
      b.confidence - a.confidence ||
      a.label.length - b.label.length ||
      compareText(a.label, b.label) ||
      a.lon - b.lon ||
      a.lat - b.lat,
  );
  return ordered.slice(0, limit);
}

/**
 * Drop anything the rest of the app could not answer about, then cap.
 *
 * Every candidate comes off a Manhattan dataset, so this never fires today.
 * It is the guarantee rather than the filter (`docs/DECISIONS.md` D28).
 */
export function _inCoverage(pack, candidates, limit) {
  const kept = [];
  for (const candidate of candidates) {
    if (kept.length >= limit) {
      break;
    }
    if (
      ON_THE_CENTERLINE.has(candidate.kind) ||
      withinCoverage(pack, candidate.lon, candidate.lat)
    ) {
      kept.push(candidate);
    }
  }
  return kept;
}
