/**
 * Which street the user means, and the street itself as an answer.
 *
 * The only hard part of a Manhattan query is the street name, so the ETL
 * pre-expands every street into the spellings a person might type
 * (`etl.addresses`) and this module resolves a typed one in a prefix range-scan.
 * Every other rung asks it first: an address, a corner and the street rung all
 * start from the same `resolveStreet` result.
 *
 * `streetNameCandidates` is the second way in, for the word a person remembers
 * rather than the one the name starts with: "americas" is Avenue of the Americas
 * and "king" is the boulevard the centerline files as W 125 ST. It reads the
 * word index instead of the variant table and shares its scoring with the place
 * rung (`geocode/names.js`).
 */

import { singleSpaced } from "../engine/labels.js";
import { nameWords } from "./normalize.js";
import {
  FUZZY_CONFIDENCE_PENALTY,
  GeocodeKind,
  STREET_NAME_CONFIDENCE,
  geocodeCandidate,
} from "./candidates.js";
import { compareText, variantRange } from "./index.js";
import { searchNames } from "./names.js";
import { prefixBound } from "./query.js";

// How far the street resolver will fan out. A half-typed street matches many
// variants ("3" is a prefix of 3 AVE, 3 ST, 30 AVE…); these bound the work a
// single keystroke can cause, and the confidence ladder sorts out what survives.
export const MAX_PREFIX_VARIANTS = 40;
export const MAX_STREETS_PER_ADDRESS = 6;
export const MAX_STREETS_PER_SIDE = 8;
export const MAX_FUZZY_STREETS = 10;
// Below this, an edit-distance-1 guess is not a correction: nearly every
// short variant is within one edit of every other, so "3 A" would "correct"
// to dozens of streets the user did not type.
export const MIN_FUZZY_CHARS = 4;

/** Whether the whole query is, exactly, a spelling of a street we index. */
export function namesAStreet(pack, index, folded) {
  return Boolean(folded) && _variantExact(pack, index, folded).length > 0;
}

/**
 * Street norms `folded` could mean, as `[streetNorm, isFuzzy]`, best first.
 *
 * Exact variant match first, then prefix, then — only if both came back
 * empty — an edit-distance-1 scan over the whole variant vocabulary. Keeping
 * the fuzzy pass last is what holds the common case inside the latency
 * budget: a typo is rare, and a correct prefix must never pay for one.
 */
export function resolveStreet(pack, index, folded) {
  if (!folded) {
    return [];
  }
  const exact = _variantExact(pack, index, folded);
  const prefixed = _variantPrefix(pack, index, folded);
  const seen = new Set(exact);
  const ordered = [...exact, ...prefixed.filter((norm) => !seen.has(norm))];
  if (ordered.length) {
    return ordered.map((norm) => [norm, false]);
  }
  return _fuzzyStreets(pack, index, folded).map((norm) => [norm, true]);
}

/**
 * `_VARIANT_EXACT_SQL`: `… WHERE variant = ? ORDER BY street_norm`.
 *
 * The plan is `SEARCH street_variant USING PRIMARY KEY (variant=?)`, and
 * `(variant, street_norm)` is that key, so walking the range is already the
 * `ORDER BY`.
 */
function _variantExact(pack, index, folded) {
  const table = pack.geocode.streetVariants;
  const [start] = variantRange(pack, index, folded, folded);
  const found = [];
  for (let i = start; i < index.variantOrder.length; i += 1) {
    const row = index.variantOrder[i];
    if (table.variant[row] !== folded) {
      break;
    }
    found.push(table.streetNorm[row]);
  }
  return found;
}

/**
 * `_VARIANT_PREFIX_SQL`: `SELECT DISTINCT street_norm … WHERE variant >= ? AND
 * variant < ? ORDER BY variant, street_norm LIMIT ?`.
 *
 * The plan is the same primary-key range plus `USE TEMP B-TREE FOR DISTINCT`,
 * so the `ORDER BY` is free and the `DISTINCT` is applied to rows arriving in
 * `(variant, street_norm)` order: keep the first occurrence of each street and
 * stop once `MAX_PREFIX_VARIANTS` distinct ones have been emitted, rather than
 * limiting the rows and then deduplicating them.
 */
function _variantPrefix(pack, index, folded) {
  const table = pack.geocode.streetVariants;
  const [start, end] = variantRange(pack, index, folded, prefixBound(folded));
  const found = [];
  const seen = new Set();
  for (let i = start; i < end && found.length < MAX_PREFIX_VARIANTS; i += 1) {
    const norm = table.streetNorm[index.variantOrder[i]];
    if (!seen.has(norm)) {
      seen.add(norm);
      found.push(norm);
    }
  }
  return found;
}

/**
 * Street norms whose variant is within one edit of `folded`.
 *
 * A scan over the ~2,800 variants, narrowed to the ones whose length could be
 * within one edit — `_VARIANTS_SQL`, a full `SCAN street_variant` whose rows go
 * straight into a set, so its order does not reach the answer. It only runs
 * when the exact and prefix passes found nothing at all, which is what keeps a
 * correctly-typed prefix from ever paying for someone else's typo.
 *
 * SQLite's `length()` counts characters and `len()` counts code points; every
 * variant and every folded query is ASCII, where `String.length` is both.
 */
function _fuzzyStreets(pack, index, folded) {
  if (folded.length < MIN_FUZZY_CHARS) {
    return [];
  }
  const table = pack.geocode.streetVariants;
  const matches = new Set();
  for (let row = 0; row < table.n; row += 1) {
    const variant = table.variant[row];
    if (variant.length < folded.length - 1 || variant.length > folded.length + 1) {
      continue;
    }
    if (_withinOneEdit(variant, folded)) {
      matches.add(table.streetNorm[row]);
    }
  }
  return [...matches].sort(compareText).slice(0, MAX_FUZZY_STREETS);
}

/** True when the two differ by at most one insert, delete or substitution. */
export function _withinOneEdit(left, right) {
  if (left === right) {
    return true;
  }
  if (left.length === right.length) {
    let differences = 0;
    for (let i = 0; i < left.length; i += 1) {
      if (left[i] !== right[i]) {
        differences += 1;
      }
    }
    return differences <= 1;
  }
  const [longer, shorter] = left.length > right.length ? [left, right] : [right, left];
  for (let i = 0; i < longer.length; i += 1) {
    if (longer.slice(0, i) + longer.slice(i + 1) === shorter) {
      return true;
    }
  }
  return false;
}

/** `_STREET_SQL`: one row by `street`'s primary key. */
export function streetDisplay(pack, index, streetNorm) {
  const row = index.streetByNorm.get(streetNorm);
  return row === undefined ? streetNorm : singleSpaced(pack.geocode.streets.display[row]);
}

/** The street itself. `base` drops when the street is only half of what was typed. */
export function streetCandidates(pack, index, folded, limit, base) {
  return streetsFrom(pack, index, resolveStreet(pack, index, folded).slice(0, limit), base);
}

/**
 * Streets every one of whose typed words prefix-matches a word of some spelling.
 *
 * The rung that answers a name the user knows a word of rather than the
 * start of. It overlaps `streetCandidates` on purpose — a street found both
 * ways keeps the better of the two scores, which `suggest._rank` does when it
 * folds the duplicate labels together.
 */
export function streetNameCandidates(pack, index, cleaned, limit) {
  const tokens = nameWords(cleaned);
  if (!tokens.length) {
    return [];
  }
  const hits = searchNames(index.streetTokens, tokens, limit);
  if (!hits.length) {
    return [];
  }
  // `_STREETS_BY_NORM_SQL`, one seek per key into `street`'s primary key; the
  // Python collects the rows into a dict, so the order it read them in is lost.
  const found = [];
  for (const hit of hits) {
    const row = index.streetByNorm.get(hit.nameId);
    if (row === undefined) {
      continue;
    }
    found.push(
      geocodeCandidate({
        label: singleSpaced(pack.geocode.streets.display[row]),
        lat: pack.geocode.streets.lat[row],
        lon: pack.geocode.streets.lon[row],
        kind: GeocodeKind.STREET,
        confidence: STREET_NAME_CONFIDENCE[hit.match],
      }),
    );
  }
  return found;
}

export function streetsFrom(pack, index, streets, base) {
  const found = [];
  for (const [streetNorm, fuzzy] of streets) {
    const row = index.streetByNorm.get(streetNorm);
    if (row === undefined) {
      continue;
    }
    const penalty = fuzzy ? FUZZY_CONFIDENCE_PENALTY : 1.0;
    found.push(
      geocodeCandidate({
        label: singleSpaced(pack.geocode.streets.display[row]),
        lat: pack.geocode.streets.lat[row],
        lon: pack.geocode.streets.lon[row],
        kind: GeocodeKind.STREET,
        confidence: base * penalty,
      }),
    );
  }
  return found;
}
