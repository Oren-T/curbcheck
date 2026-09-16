/**
 * Human labels for a stretch of curb: the street, the side, the cross streets.
 *
 * Shared by the result cards (`engine/search.js`) and the geocoder's candidate
 * list, so a block is named the same way wherever the user meets it. CSCL
 * writes street names in capitals ("3 AVENUE") and they are shown as stored,
 * because a title-cased "3 Avenue" would be our text rather than DOT's. Port of
 * `curbcheck/engine/labels.py`.
 */

import { jsonStringList } from "./model.js";

export const SIDE_WORDS = Object.freeze({ N: "north", S: "south", E: "east", W: "west" });

/**
 * CSCL writes the same street as `E 85 ST` on one row and `E  85 ST` on another.
 *
 * The label is the one place a street name reaches the user's eye, so the runs
 * of spaces the source carries are collapsed there rather than in the ETL,
 * which keeps the stored name byte-identical to DOT's.
 */
export function singleSpaced(name) {
  return String(name).trim().split(/\s+/).join(" ");
}

/** "E 85 ST → E 86 ST", "at E 85 ST", or null when neither node names a cross street. */
export function betweenPhrase(streetName, fromNames, toNames) {
  const start = crossStreet(fromNames, streetName);
  const end = crossStreet(toNames, streetName);
  if (start && end) {
    return `${start} → ${end}`;
  }
  if (start || end) {
    return `at ${start || end}`;
  }
  return null;
}

/**
 * The first name on the node that is not the street the span runs along.
 *
 * Compared on collapsed whitespace: CSCL writes the same street as `E 85 ST`
 * on the node and `E  85 ST` on the segment often enough that an exact match
 * would label a corner as its own cross street.
 */
export function crossStreet(names, streetName) {
  const own = collapse(streetName);
  for (const name of jsonStringList(names)) {
    if (name && collapse(name) !== own) {
      return singleSpaced(name);
    }
  }
  return null;
}

/** "3 AVENUE, west side, E 85 ST → E 86 ST", with each part dropped when unknown. */
export function spanLabel(streetName, side, phrase) {
  const parts = [singleSpaced(streetName)];
  // `side` comes out of the pack, so an own-property check rather than plain
  // indexing: a cell reading "constructor" must not find a side word.
  const sideWord = Object.hasOwn(SIDE_WORDS, side ?? "") ? SIDE_WORDS[side] : null;
  if (sideWord) {
    parts.push(`${sideWord} side`);
  }
  if (phrase) {
    parts.push(phrase);
  }
  return parts.join(", ");
}

// Python's `str.casefold()`; every stored name is ASCII (measured: 0 non-ASCII
// rows), where casefold and toLowerCase agree.
function collapse(name) {
  return singleSpaced(name).toLowerCase();
}
