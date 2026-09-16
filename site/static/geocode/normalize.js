/**
 * The one normalizer both sides of the index share, ported for the browser.
 *
 * Two Python modules meet here because the geocoder only ever uses these
 * pieces of them: `etl.streets.normalize_street_name` (the canonical street
 * spelling) and `etl.addresses.fold` (the same, plus the two folds that exist
 * only because people type differently than the city writes). The pack is
 * built by the Python ETL, so a difference between these functions and theirs
 * is a query key that can never meet an index key.
 *
 * Three places where Python's `re` and JS's `RegExp` disagree, and what is
 * done about each:
 *
 * - Python's `\s` for `str` patterns is not JS's: it includes U+001C-U+001F
 *   and U+0085 and excludes U+FEFF, which JS matches. Measured against
 *   CPython 3.12, `\s` and `str.strip()` cover exactly `PY_SPACE`, so every
 *   whitespace class below is written out rather than spelled `\s`.
 * - Python's `\d` matches every Unicode decimal digit, JS's matches `[0-9]`.
 *   Where a pattern below sees only text that has already been reduced to
 *   `[A-Z0-9 ]`, the two are the same and the comment says so; `query.js`
 *   spells the difference out where it is reachable from user text.
 * - `str.upper()` and `toUpperCase()` differ on 55 code points (measured over
 *   all of Unicode 15). None of the 55 differs in the `[A-Z0-9 ]` characters
 *   that survive `fold`'s punctuation strip, so a folded key is identical in
 *   both languages whatever the user typed.
 */

/**
 * The character class Python's `\s` and `str.strip()` cover for `str`.
 * Used everywhere this port would otherwise write `\s`; see the module header.
 */
export const PY_SPACE =
  "\\t\\n\\v\\f\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";

const PY_STRIP_EDGES = new RegExp(`^[${PY_SPACE}]+|[${PY_SPACE}]+$`, "gu");

/** Python's `str.strip()`: the ends trimmed of exactly `PY_SPACE`, not JS's `\s`. */
export function pyStrip(text) {
  return text.replace(PY_STRIP_EDGES, "");
}

// --- etl/streets.py -------------------------------------------------------

/** Suffix and directional words, sign spelling -> centerline spelling. */
export const WORD_FORMS = new Map(
  Object.entries({
    STREET: "ST",
    AVENUE: "AVE",
    BOULEVARD: "BLVD",
    PLACE: "PL",
    DRIVE: "DR",
    DRIVEWAY: "DR",
    PARKWAY: "PKWY",
    TERRACE: "TER",
    SQUARE: "SQ",
    ROAD: "RD",
    LANE: "LN",
    COURT: "CT",
    ALLEY: "ALY",
    PLAZA: "PLZ",
    BRIDGE: "BRG",
    EXPRESSWAY: "EXPY",
    HIGHWAY: "HWY",
    CIRCLE: "CIR",
    TUNNEL: "TUNL",
    EAST: "E",
    WEST: "W",
    NORTH: "N",
    SOUTH: "S",
    FT: "FORT",
    SAINT: "ST",
    MT: "MOUNT",
    FIRST: "1",
    SECOND: "2",
    THIRD: "3",
    FOURTH: "4",
    FIFTH: "5",
    SIXTH: "6",
    SEVENTH: "7",
    EIGHTH: "8",
    NINTH: "9",
    TENTH: "10",
    ELEVENTH: "11",
    TWELFTH: "12",
  }),
);

/**
 * Whole-name aliases the word map cannot reach, measured from the residue in
 * data/explore/street_names.txt. Keys and values are already in word-normalized
 * form. Three kinds live here: honorific renamings the two datasets disagree
 * about, DOT shorthands, and outright typos in the sign data.
 */
export const NAME_ALIASES = new Map(
  Object.entries({
    // Honorific renamings: the sign data uses one official name, CSCL the other.
    "6 AVE": "AVE OF THE AMERICAS",
    "AVE OF AMERICAS": "AVE OF THE AMERICAS",
    "MALCOLM X BLVD": "LENOX AVE",
    // OTI's AddressPoint files 43 Manhattan doors under this name; CSCL has no
    // segment for it and carries the same roadway as W 125 ST
    // (docs/ux/AUTOCOMPLETE_RESEARCH.md §1.3).
    "DR M L KING JR BLVD": "W 125 ST",
    "LUIS MUNOZ MARIN BLVD": "E 116 ST",
    "W 110 ST": "CATHEDRAL PKWY",
    "WILLETT ST": "BIALYSTOKER PL",
    "ADAM C POWELL BLVD": "ADAM CLAYTON POWELL JR BLVD",
    "ADAM CLAYTON POWELL BLVD": "ADAM CLAYTON POWELL JR BLVD",
    "A C POWELL BLVD": "ADAM CLAYTON POWELL JR BLVD",
    "FRED DOUGLASS BLVD": "FREDERICK DOUGLASS BLVD",
    "FRED DOUGLAS BLVD": "FREDERICK DOUGLASS BLVD",
    "FREDERICK DOUGLAS BLVD": "FREDERICK DOUGLASS BLVD",
    "FRED DOUGLASS CIR": "FREDERICK DOUGLASS CIR",
    // DOT shorthands and initialisms.
    "FDR DR": "FRANKLIN D ROOSEVELT DR",
    "FDR DRIVE": "FRANKLIN D ROOSEVELT DR",
    "F D R DR": "FRANKLIN D ROOSEVELT DR",
    "G WASHINGTON BRG": "GEORGE WASHINGTON BRG",
    "QUEENSBORO BRG": "ED KOCH QUEENSBORO BRG",
    "QUEENSBOROUGH BRG": "ED KOCH QUEENSBORO BRG",
    "N D PERLMAN PL": "NATHAN D PERLMAN PL",
    "ROBERT F WAGNER PL": "R F WAGNER SR PL",
    "ROBERT F WAGNER SR PL": "R F WAGNER SR PL",
    "CORBIN DR": "MARGARET CORBIN DR",
    // Word-spacing disagreements between the two datasets.
    "MACDOUGAL ST": "MAC DOUGAL ST",
    "MACDOUGAL ALY": "MAC DOUGAL ALY",
    "LAGUARDIA PL": "LA GUARDIA PL",
    "LASALLE ST": "LA SALLE ST",
    "VAN DAM ST": "VANDAM ST",
    // Typos in the sign data.
    "BLEEKER ST": "BLEECKER ST",
    "CUMMINGS ST": "CUMMING ST",
    "THEATRE ALY": "THEATER ALY",
    "MARGRET CORBIN DR": "MARGARET CORBIN DR",
    "AUDOBON AVE": "AUDUBON AVE",
    "BENSON ST": "BENSON PL",
    // DOT adds a directional to a roadway CSCL carries under one name. Each
    // target was checked against `street_segment.street_norm` before it was
    // added, and the names with no CSCL entry at all — ROCKEFELLER PLZ,
    // COENTIES SLIP, THELONIOUS SPHERE MONK CIR — are deliberately absent
    // (docs/DATA.md §1.9, docs/VALIDATION.md §4 D4).
    "MAIN ST N": "MAIN ST",
    "CENTRAL RD N": "CENTRAL RD",
    "DELANCEY ST N": "DELANCEY ST",
    "DELANCEY ST S": "DELANCEY ST",
  }),
);

// `streets._PUNCTUATION` and `streets._WHITESPACE`, renamed because
// `etl.addresses` has two of its own with the same names and both live here.
const STREETS_PUNCTUATION = /[.,]/gu;
const STREETS_WHITESPACE = new RegExp(`[${PY_SPACE}]+`, "gu");

/**
 * Canonical uppercase form: collapsed whitespace, abbreviated words, aliases applied.
 *
 * Applied to both sides of the sign/centerline join. Idempotent, so it is safe
 * to call on an already-normalized name.
 */
export function normalizeStreetName(raw) {
  const words = normalizeWords(raw);
  return NAME_ALIASES.get(words) ?? words;
}

/** `normalizeStreetName` without the alias table, so the two can be told apart. */
function normalizeWords(raw) {
  // A few rows qualify the roadway after an asterisk ("PARK AVENUE*WEST RDWY");
  // the qualifier duplicates the on_street_suffix column, so drop it.
  let text = String(raw).split("*")[0].replace(STREETS_PUNCTUATION, "").toUpperCase();
  text = pyStrip(text.replace(STREETS_WHITESPACE, " "));
  return text
    .split(" ")
    .filter((token) => token)
    .map((token) => WORD_FORMS.get(token) ?? token)
    .join(" ");
}

// --- etl/addresses.py -----------------------------------------------------

/**
 * Buildings are written "1 WORLD TRADE CENTER" in CommonPlace and said "One
 * World Trade Center" by everyone else. Only applied to place names, never to
 * street names, where these words do not appear as numbers.
 */
export const CARDINALS = new Map(
  Object.entries({
    ONE: "1",
    TWO: "2",
    THREE: "3",
    FOUR: "4",
    FIVE: "5",
    SIX: "6",
    SEVEN: "7",
    EIGHT: "8",
    NINE: "9",
    TEN: "10",
  }),
);

/**
 * Tokens that end a street name rather than name it, so a variant with the last
 * one dropped still points at the same street ("3 AVE" -> "3"). `WORD_FORMS`'s
 * own values cover the spelled-out suffixes and the directionals; the literals
 * are the abbreviations the source writes directly.
 */
export const STREET_SUFFIXES = new Set([
  ...WORD_FORMS.values(),
  "ST",
  "AVE",
  "PL",
  "DR",
  "BLVD",
  "RD",
  "LN",
  "CT",
]);

/**
 * The part of a name New Yorkers leave off, on named streets ("HOUSTON") as
 * much as on numbered ones ("86 ST").
 */
export const DIRECTIONALS = new Set(["E", "W", "N", "S"]);

/**
 * Words too common in a name to narrow anything down. Dropped from the index
 * and from the query alike, so they can neither be required nor get in the way:
 * "high school of fashion" and "fashion high school" ask the same question.
 */
export const NAME_STOPWORDS = new Set(["THE", "OF", "AND", "AT", "A"]);

const FOLD_PUNCTUATION = /[^A-Z0-9 ]/gu;
const FOLD_WHITESPACE = new RegExp(`[${PY_SPACE}]+`, "gu");
// "85TH" -> "85". The ETL normalizer folds the spelled-out ordinals (FIFTH -> 5)
// but not the digit ones, because the source datasets never write those; people
// typing into the address box do.
// `\b` and `\d` are safe unspelled here: `FOLD_PUNCTUATION` has already reduced
// the text to `[A-Z0-9 ]`, where Python's Unicode-aware `\d` and `\b` and JS's
// ASCII ones agree.
const DIGIT_ORDINAL = /\b([0-9]+)(?:ST|ND|RD|TH)\b/gu;
// Matched against an already-folded name, so `\d` is `[0-9]` for the same reason.
const NUMBERED_STREET = /^(?:([EW]) )?([0-9]{1,3})(?: (ST|AVE|PL|DR|WALK))?$/u;

/**
 * Source text or user text -> the one canonical spelling both sides look up by.
 *
 * Two folds on top of `normalizeStreetName`, both there only because people
 * type differently than the city writes: digit ordinals ("3RD" -> "3", which
 * the ETL never sees) and punctuation ("W. 86th St.").
 */
export function fold(raw) {
  let text = String(raw).toUpperCase().replace(FOLD_PUNCTUATION, " ");
  text = text.replace(DIGIT_ORDINAL, "$1");
  text = pyStrip(text.replace(FOLD_WHITESPACE, " "));
  if (!text) {
    return "";
  }
  return normalizeStreetName(text);
}

/**
 * Every spelling of one street that should resolve to it, as a Set.
 *
 * Three families beyond the canonical name: the name with its suffix type
 * dropped ("3 AVE" -> "3"); the name without the leading directional the user
 * did not type ("E HOUSTON ST" -> "HOUSTON ST", "HOUSTON"); and, for a
 * numbered cross street, the bare number and the number with either half of
 * its qualifiers ("E 86 ST" -> "86", "86 ST", "E 86"). The spelled-out
 * ordinals cost nothing here because `fold` already collapses them.
 */
export function streetVariants(streetNorm) {
  const variants = new Set([streetNorm]);
  const tokens = streetNorm.split(" ");
  if (tokens.length > 1 && STREET_SUFFIXES.has(tokens[tokens.length - 1])) {
    variants.add(tokens.slice(0, -1).join(" "));
  }
  if (tokens.length > 2 && DIRECTIONALS.has(tokens[0])) {
    variants.add(tokens.slice(1).join(" "));
    if (STREET_SUFFIXES.has(tokens[tokens.length - 1])) {
      variants.add(tokens.slice(1, -1).join(" "));
    }
  }
  const numbered = NUMBERED_STREET.exec(streetNorm);
  if (numbered) {
    const [, directional, number, suffix] = numbered;
    variants.add(number);
    if (suffix) {
      variants.add(`${number} ${suffix}`);
    }
    if (directional) {
      variants.add(`${directional} ${number}`);
    }
  }
  variants.delete("");
  return variants;
}

/**
 * Source text or user text -> the words it is searched by, in the order written.
 *
 * The one word list both sides share, so a typed word and an indexed word can
 * only ever meet in the same form. Order is kept because it is what separates
 * a name the query starts ("bryant park") from one it merely appears in
 * ("five bryant park").
 */
export function nameWords(name) {
  return spellingWords(fold(name));
}

/**
 * The same, for text `fold` has already been over.
 *
 * Street variants and `NAME_ALIASES` keys are stored in folded form already,
 * and folding them again would apply the alias map a second time and turn the
 * spelling back into the name it is an alternative to.
 */
export function spellingWords(folded) {
  const words = [];
  for (const word of folded.split(" ")) {
    if (word && !NAME_STOPWORDS.has(word)) {
      words.push(CARDINALS.get(word) ?? word);
    }
  }
  return words;
}
