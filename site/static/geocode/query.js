/**
 * Query text in, an index key out: normalization, then classification.
 *
 * Everything the user typed passes through here before a single row is read.
 * `cleanQuery` is the sanitizer and `parseQuery` the tiny grammar — a ZIP, two
 * streets meeting, a house number on a street, or a bare name — and the result
 * is what decides which rung `suggest` tries first.
 *
 * The four query classes are plain objects tagged with `kind`, because a
 * `ParsedQuery` is only ever read: `suggest` switches on the tag exactly where
 * the Python does `isinstance`.
 */

import { PY_SPACE, fold, pyStrip } from "./normalize.js";

/**
 * Longest query we will parse. A Manhattan address is never close to this; the
 * cap keeps a pathological string out of the regexes and out of the index.
 */
export const MAX_QUERY_CHARS = 120;

// "&", "and", "at", "@" or a slash between two street names.
const INTERSECTION_SPLIT = new RegExp(
  `[${PY_SPACE}]+(?:&|AND|AT)[${PY_SPACE}]+|[${PY_SPACE}]*[&@/][${PY_SPACE}]*`,
  "u",
);

// A leading house number, optionally hyphenated (Queens style, rare in
// Manhattan) or with a letter suffix ("123A REAR"), then the street.
// `\p{Nd}` rather than `[0-9]`: Python's `\d` matches every Unicode decimal
// digit, so "３ AVE" is a house number to the server and has to be one here.
const HOUSE_NUMBER = new RegExp(`^(\\p{Nd}{1,6})(?:-\\p{Nd}{1,6})?[A-Z]?[${PY_SPACE}]+(.+)$`, "u");

const ZIP = /^\p{Nd}{5}$/u;

// One Unicode decimal digit, for reading a house number the way `int()` does.
const DECIMAL_DIGIT = /\p{Nd}/u;

// C0 and C1 controls, including the NULs and bidi-adjacent bytes the fuzzer
// splices in. Replaced with a space rather than removed, so "A\x00B" cannot
// become the word "AB".
const CONTROL_CHARS = /[\x00-\x1f\x7f-\x9f]/gu;
const WHITESPACE = new RegExp(`[${PY_SPACE}]+`, "gu");

/** @typedef {{kind: "address", houseNumber: number, street: string}} AddressQuery */
/** @typedef {{kind: "intersection", first: string, second: string}} IntersectionQuery */
/** @typedef {{kind: "street", street: string}} StreetQuery */
/** @typedef {{kind: "zip", zipcode: string}} ZipQuery */
/** @typedef {AddressQuery | IntersectionQuery | StreetQuery | ZipQuery} ParsedQuery */

/** A house number on a named street, the street already folded. */
export function addressQuery(houseNumber, street) {
  return { kind: "address", houseNumber, street };
}

/** Two folded street names that should meet at a node. */
export function intersectionQuery(first, second) {
  return { kind: "intersection", first, second };
}

/** A bare folded street name, with no house number. Also the place-name case. */
export function streetQuery(street) {
  return { kind: "street", street };
}

/** Five digits, which in Manhattan can only be a ZIP code. */
export function zipQuery(zipcode) {
  return { kind: "zip", zipcode };
}

/**
 * Upper-cased, control-free, single-spaced query text, or "" when there is none.
 *
 * Over `MAX_QUERY_CHARS` returns "", which every caller reads as "no query":
 * a 400-character paste is not an address and is not worth a regex.
 */
export function cleanQuery(text) {
  const cleaned = pyStrip(
    String(text).replace(CONTROL_CHARS, " ").replace(WHITESPACE, " "),
  ).toUpperCase();
  // Python counts code points, so an astral character is one character of the
  // budget rather than the two UTF-16 units `String.length` would charge.
  return [...cleaned].length > MAX_QUERY_CHARS ? "" : cleaned;
}

/**
 * Classify a query as a ZIP, an intersection, an address, or a bare street.
 *
 * Returns null when the query is empty, over `MAX_QUERY_CHARS`, or has no
 * street name left after folding. The classification is the *primary* reading
 * only: `suggest` falls back to the street and place passes when an address
 * reading finds nothing, because "350 5th" and "1 police plaza" both parse as
 * addresses and only one of them is one.
 */
export function parseQuery(text) {
  const cleaned = cleanQuery(text);
  if (!cleaned) {
    return null;
  }

  if (ZIP.test(cleaned)) {
    return zipQuery(cleaned);
  }

  const parts = cleaned
    .split(INTERSECTION_SPLIT)
    .map((part) => pyStrip(part))
    .filter((part) => part);
  if (parts.length >= 2) {
    const first = fold(parts[0]);
    const second = fold(parts[1]);
    return first && second ? intersectionQuery(first, second) : null;
  }

  const houseMatch = HOUSE_NUMBER.exec(cleaned);
  if (houseMatch) {
    const street = fold(houseMatch[2]);
    if (street) {
      return addressQuery(digitsToInt(houseMatch[1]), street);
    }
  }

  const street = fold(cleaned);
  return street ? streetQuery(street) : null;
}

/**
 * The exclusive upper end of a `LIKE prefix%` range, so it can be a range scan.
 *
 * A range scan has no pattern language in it, which is the point: there is no
 * metacharacter for a user to escape or forget to escape (threat T3).
 *
 * The increment is on the last *code point*, as Python's `chr(ord(x) + 1)` is;
 * an empty prefix throws, exactly as `prefix[-1]` raises IndexError.
 */
export function prefixBound(prefix) {
  const points = [...prefix];
  const last = points.pop();
  if (last === undefined) {
    throw new RangeError("prefix_bound of an empty prefix");
  }
  return points.join("") + String.fromCodePoint(last.codePointAt(0) + 1);
}

/**
 * `int()` over a run of Unicode decimal digits, which is what Python's `int`
 * accepts and `Number` does not: `int("３")` is 3. Every `Nd` code point sits
 * in a run of ten starting at that script's zero, so the value of a digit is
 * its distance from the first `Nd` code point at or below it.
 */
function digitsToInt(digits) {
  let value = 0;
  for (const character of digits) {
    const code = character.codePointAt(0);
    let zero = code;
    while (code - zero < 9 && DECIMAL_DIGIT.test(String.fromCodePoint(zero - 1))) {
      zero -= 1;
    }
    value = value * 10 + (code - zero);
  }
  return value;
}
