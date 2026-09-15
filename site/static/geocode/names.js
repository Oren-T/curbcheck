/**
 * Finding a name by the words in it, and deciding how well each one was named.
 *
 * The street and place rungs both answer the same question — which of these
 * names did the user mean? — so they ask it the same way: every typed word has
 * to prefix-match a word of some spelling of the name, in any order, and where
 * the words landed is what the answer is worth. `etl.addresses` writes the word
 * index both read (`docs/ux/AUTOCOMPLETE_RESEARCH.md` §6, `docs/DECISIONS.md` D31).
 *
 * The search is two passes over one index and never a join. The first pass
 * pulls the candidates of the typed word that matches fewest rows; the second
 * checks the remaining words against the spelling that came back with each
 * candidate, which is why a common word like "school" can never truncate the
 * answer a rare one like "fashion" already found.
 */

import { compareText, tokenRange } from "./index.js";
import { prefixBound } from "./query.js";

// What one typed word may pull out of the token index. Bounded because a single
// letter is a real query: "C" leads 2,722 of the 24,000 place-token rows, and
// the index is ordered so that the rows this stops at are the ones where the
// word is the whole name or starts it.
export const MAX_TOKEN_ROWS = 200;

// A candidate set this small is not worth scanning a second word to beat, and
// most real queries reach it on the first: "fashion" matches 2 rows.
export const ENOUGH_TOKEN_ROWS = 40;

// How many of the typed words may be looked up before one of them has to be the
// driver. A 120-character query can hold twenty words and each one is a range
// scan; every word is still *checked*, in JS, against every candidate.
export const MAX_TOKEN_SCANS = 3;

/**
 * Where the typed words landed in the name. Ordered: higher is a better match.
 *
 * The order is the one a person scans a suggestion list in — the name they
 * typed whole, then the names that begin with what they typed, then the names
 * that begin with one of their words, then the names that merely contain
 * them. Google Maps ranks this way and the UX audit's testers expected it.
 */
export const NameMatch = Object.freeze({
  INNER: 0,
  LEADING: 1,
  PREFIX: 2,
  WHOLE: 3,
});

// Joins the two halves of a driver row into one Map key. Every stored name is
// ASCII (measured: 0 non-ASCII rows), so a NUL can never occur inside one.
const KEY_SEPARATOR = "\u0000";

/**
 * @typedef {object} NameHit One name the query could mean.
 * @property {string} nameId the row's key
 * @property {string} searchName the spelling it matched
 * @property {number} match a `NameMatch` rank
 */

/**
 * How `tokens` match one spelling's `words`, or null when they do not.
 *
 * Every token must prefix-match some word, which is what makes the match
 * order-independent: "fashion high" and "high fashion" both find HIGH SCHOOL
 * OF FASHION INDUSTRIES. Stopwords are already gone from both sides
 * (`normalize.nameWords`), so "of" can neither be required nor get in the way.
 */
export function matchName(tokens, words) {
  if (!tokens.length || !words.length) {
    return null;
  }
  if (!tokens.every((token) => words.some((word) => word.startsWith(token)))) {
    return null;
  }
  if (tokens.length === words.length && tokens.every((token, i) => token === words[i])) {
    return NameMatch.WHOLE;
  }
  if (tokens.length <= words.length && tokens.every((token, i) => words[i].startsWith(token))) {
    return NameMatch.PREFIX;
  }
  if (tokens.some((token) => words[0].startsWith(token))) {
    return NameMatch.LEADING;
  }
  return NameMatch.INNER;
}

/**
 * The `limit` names best matching every one of `tokens`, best first.
 *
 * `tokenTable` is one of `buildGeocodeIndex`'s two word indexes and stands in
 * for the Python's `token_sql`; it is a constant of the calling module, never
 * built from anything typed.
 *
 * Ties are broken by the shorter name and then alphabetically, so BRYANT PARK
 * comes before FIVE BRYANT PARK and the order never depends on which row
 * SQLite reached first.
 *
 * @returns {NameHit[]}
 */
export function searchNames(tokenTable, tokens, limit) {
  /** @type {Map<string, NameHit>} */
  const best = new Map();
  for (const [nameId, spelling] of _driverRows(tokenTable, tokens)) {
    const match = matchName(tokens, spelling.split(" "));
    if (match === null) {
      continue;
    }
    const previous = best.get(nameId);
    if (previous === undefined || match > previous.match) {
      best.set(nameId, { nameId, searchName: spelling, match });
    }
  }
  // A Map iterates in insertion order and a JS sort is stable, which together
  // are Python's `sorted(best.values(), ...)` over an insertion-ordered dict.
  const ordered = [...best.values()].sort(
    (a, b) =>
      b.match - a.match ||
      a.searchName.length - b.searchName.length ||
      compareText(a.searchName, b.searchName),
  );
  return ordered.slice(0, limit);
}

/**
 * Candidates from the typed word that matched fewest rows, longest word first.
 *
 * Longest first because a longer prefix is usually the rarer one, and the
 * scan stops as soon as a word has come back with few enough rows that
 * another word could not narrow the work that follows.
 *
 * Sorted on the way out: `searchNames` keeps the first spelling it sees at an
 * equal match rank, so the rows have to arrive in an order that does not depend
 * on the language's hash tables. `geocode.names._driver_rows` returns
 * `sorted(driver)` for exactly that reason and this reproduces it —
 * `(nameId, searchName)` ascending, compared by code unit. Note that a place's
 * id is sorted as the *text* of the integer there, because the Python builds
 * the tuple from `str(row[0])`.
 *
 * @returns {Array<[string, string]>}
 */
export function _driverRows(tokenTable, tokens) {
  /** @type {Map<string, [string, string]>} */
  let driver = new Map();
  // `sorted(tokens, key=len, reverse=True)`: both languages' sorts are stable,
  // so equal-length words keep the order they were typed in.
  const byLength = [...tokens].sort((a, b) => b.length - a.length).slice(0, MAX_TOKEN_SCANS);
  for (const [index, token] of byLength.entries()) {
    /** @type {Map<string, [string, string]>} */
    const rows = new Map();
    for (const row of tokenRange(tokenTable, token, prefixBound(token), MAX_TOKEN_ROWS)) {
      const nameId = tokenTable.nameId[row];
      const searchName = tokenTable.searchName[row];
      rows.set(nameId + KEY_SEPARATOR + searchName, [nameId, searchName]);
    }
    if (index === 0 || rows.size < driver.size) {
      driver = rows;
    }
    if (driver.size <= ENOUGH_TOKEN_ROWS) {
      break;
    }
  }
  return [...driver.values()].sort((a, b) => compareText(a[0], b[0]) || compareText(a[1], b[1]));
}
