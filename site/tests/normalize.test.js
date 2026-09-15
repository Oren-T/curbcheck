/**
 * The shared normalizer: the ETL folded the pack with it, the box folds the query.
 *
 * The cases are the normalisation ones from `tests/test_etl_streets.py` and
 * `tests/test_etl_addresses.py`. A difference between the two languages here is
 * a key that can never meet its index, so the last three tests are about
 * exactly where Python and JS disagree about text.
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  NAME_ALIASES,
  fold,
  nameWords,
  normalizeStreetName,
  spellingWords,
  streetVariants,
} from "../static/geocode/normalize.js";

test("normalize_street_name matches the centerline spelling", () => {
  const cases = [
    // docs/DATA.md §1.9: the sign data pads numbers and spells suffixes out.
    ["EAST   85 STREET", "E 85 ST"],
    ["E  85 ST", "E 85 ST"],
    ["west 42 street", "W 42 ST"],
    ["SEVENTH AVENUE", "7 AVE"],
    ["PARK AVENUE*WEST RDWY", "PARK AVE"],
    ["ST. NICHOLAS AVE", "ST NICHOLAS AVE"],
    // Whole-name aliases the word map cannot reach.
    ["6 AVENUE", "AVE OF THE AMERICAS"],
    ["FDR DRIVE", "FRANKLIN D ROOSEVELT DR"],
    ["ADAM C POWELL BLVD", "ADAM CLAYTON POWELL JR BLVD"],
    ["FRED DOUGLAS BLVD", "FREDERICK DOUGLASS BLVD"],
    ["WEST 110 STREET", "CATHEDRAL PKWY"],
    ["MALCOLM X BLVD", "LENOX AVE"],
    ["VAN DAM ST", "VANDAM ST"],
    ["WILLETT ST", "BIALYSTOKER PL"],
    ["QUEENSBORO BRG", "ED KOCH QUEENSBORO BRG"],
    ["BLEEKER ST", "BLEECKER ST"],
    ["CUMMINGS ST", "CUMMING ST"],
    ["THEATRE ALY", "THEATER ALY"],
  ];
  for (const [raw, expected] of cases) {
    assert.equal(normalizeStreetName(raw), expected, raw);
  }
});

test("normalize_street_name is idempotent", () => {
  for (const raw of ["EAST   85 STREET", "6 AVENUE", "FDR DRIVE"]) {
    const once = normalizeStreetName(raw);
    assert.equal(normalizeStreetName(once), once, raw);
  }
});

test("fold agrees with the ETL normalizer and adds the digit ordinals", () => {
  // Anything the ETL already folds has to fold the same way here, or the
  // index keys and the query keys stop meeting.
  for (const name of ["EAST 86 STREET", "W  48 ST", "FIFTH AVENUE", "FDR DRIVE"]) {
    assert.equal(fold(name), normalizeStreetName(name), name);
  }
  assert.equal(fold("e 86th st."), "E 86 ST");
  assert.equal(fold("3rd ave"), "3 AVE");
  assert.equal(fold("third avenue"), "3 AVE");
  assert.equal(fold(""), "");
});

test("the king boulevard alias reaches the centerline name", () => {
  // 43 AddressPoint doors are filed under this name and CSCL has no segment
  // for it (docs/ux/AUTOCOMPLETE_RESEARCH.md §1.3).
  assert.equal(fold("DR M L KING JR BLVD"), "W 125 ST");
});

test("street_variants cover the spellings a person types", () => {
  assert.deepEqual(streetVariants("E 86 ST"), new Set(["E 86 ST", "E 86", "86 ST", "86"]));
  assert.deepEqual(streetVariants("3 AVE"), new Set(["3 AVE", "3"]));
  assert.ok(streetVariants("E HOUSTON ST").has("HOUSTON"));
  assert.deepEqual(streetVariants("BROADWAY"), new Set(["BROADWAY"]));
});

test("name_words drop stopwords and fold the spoken cardinal", () => {
  assert.deepEqual(nameWords("One World Trade Center"), ["1", "WORLD", "TRADE", "CENTER"]);
  assert.deepEqual(nameWords("The Museum of Modern Art"), ["MUSEUM", "MODERN", "ART"]);
});

test("a name keeps its word order so the start of it can be recognized", () => {
  // `geocode/names.js` scores a name by where the typed words landed in it.
  assert.deepEqual(nameWords("Bryant Park"), ["BRYANT", "PARK"]);
  assert.deepEqual(nameWords("Five Bryant Park"), ["5", "BRYANT", "PARK"]);
});

test("spelling_words does not apply the alias map a second time", () => {
  // The alias keys are stored folded already; folding one again would turn the
  // spelling back into the name it is an alternative to.
  assert.deepEqual(spellingWords("6 AVE"), ["6", "AVE"]);
  assert.equal(fold("6 AVE"), NAME_ALIASES.get("6 AVE"));
});

test("a fold is ASCII whichever language upper-cased it", () => {
  // Python's `str.upper()` and JS's `toUpperCase()` differ on 55 code points
  // (measured over all of Unicode), but `fold` keeps only `[A-Z0-9 ]` and none
  // of the 55 differs there. The ligatures are the cases that would bite: both
  // languages expand them, and both expand them the same way.
  assert.equal(fold("ﬅ nicholas ave"), "ST NICHOLAS AVE");
  assert.equal(fold("é 86 st"), "86 ST");
  assert.equal(fold("straße"), "STRASSE");
});

test("fold collapses the whitespace Python collapses and no more", () => {
  // JS's `\s` is not Python's: it omits U+001C-U+001F and U+0085 and adds
  // U+FEFF. `normalize.PY_SPACE` is Python's set, so a byte-order mark stays a
  // character the punctuation pass turns into a space rather than vanishing.
  assert.equal(normalizeStreetName("E 85 ST"), "E 85 ST");
  assert.equal(normalizeStreetName("﻿E 85 ST"), "﻿E 85 ST");
  assert.equal(fold("﻿E 85 ST"), "E 85 ST");
});
