/**
 * The two rungs that are not streets: a named place, and a ZIP centre.
 *
 * A place is matched word by word against the `place_token` index — every typed
 * word has to prefix-match a word of the name, in any order — and scored by where
 * those words landed (`geocode/names.js`). A ZIP is the coarsest answer the
 * geocoder gives and says so in its second line.
 */

import { singleSpaced } from "../engine/labels.js";
import { nameWords } from "./normalize.js";
import {
  CONFIDENCE_ZIP,
  DEFAULT_SECONDARY,
  GeocodeKind,
  PLACE_NAME_CONFIDENCE,
  geocodeCandidate,
} from "./candidates.js";
import { searchNames } from "./names.js";

/**
 * Places every one of whose typed words prefix-matches a word of the name.
 *
 * Best first: the name typed whole, then the names it starts, then the names
 * one of its words starts, then the names that contain them. Only the rows
 * that survive that ordering are read out of `place`, because reading the
 * candidates instead is what a one-letter query would pay for.
 */
export function placeCandidates(pack, index, cleaned, limit) {
  const table = pack.geocode.places;
  const tokens = nameWords(cleaned);
  if (!tokens.length) {
    return [];
  }
  const hits = searchNames(index.placeTokens, tokens, limit);
  if (!hits.length) {
    return [];
  }
  // `_PLACES_BY_ID_SQL`, one seek per key into `place`'s INTEGER PRIMARY KEY;
  // the Python collects the rows into a dict, so the order it read them is lost.
  const found = [];
  for (const hit of hits) {
    const row = index.placeById.get(Number(hit.nameId));
    if (row === undefined) {
      continue;
    }
    found.push(
      geocodeCandidate({
        label: singleSpaced(table.display[row]),
        lat: table.lat[row],
        lon: table.lon[row],
        kind: GeocodeKind.PLACE,
        confidence: PLACE_NAME_CONFIDENCE[hit.match],
      }),
    );
  }
  return found;
}

/** `_ZIP_SQL`: one row by `zip_centroid`'s primary key. */
export function zipCandidates(pack, index, zipcode) {
  const table = pack.geocode.zipCentroids;
  const row = index.zipByCode.get(zipcode);
  if (row === undefined) {
    return [];
  }
  return [
    geocodeCandidate({
      label: zipcode,
      lat: table.lat[row],
      lon: table.lon[row],
      kind: GeocodeKind.ZIP,
      // The count is what says how coarse this is: a ZIP centre is the
      // mean of a few thousand doors, not any one of them.
      secondary: `${DEFAULT_SECONDARY} · ZIP centre of ${table.addressPoints[row]} addresses`,
      confidence: CONFIDENCE_ZIP,
    }),
  ];
}
