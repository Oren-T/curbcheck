/**
 * What a geocode answer is, and what each rung of the ladder is worth.
 *
 * `suggest` and `reverseGeocode` both hand back one of these two records, and
 * every rung that can produce one scores it against the confidence constants
 * here, so the ladder is readable in one place rather than spread over the
 * modules that implement its rungs.
 *
 * The two records are the API payloads (`api/routes._candidate_payload`,
 * `_reverse_payload`): the field names are the Python dataclasses' own, and
 * `kind` is the string value of `GeocodeKind` rather than an enum member, so a
 * record can be posted back from the worker without a conversion step.
 */

import { COVERAGE_AREA } from "../engine/coverage.js";
import { NameMatch } from "./names.js";
import { pyStrip } from "./normalize.js";

// Eight is the longest list a person scans without reading it as a search
// result page rather than as a disambiguation (docs/ux/UX_AUDIT.md P1-5 asks
// for candidates to always be offered, which only works if the list stays
// glanceable).
export const MAX_CANDIDATES = 8;

// Everything in the database is in one borough, so the second line of a
// candidate says so when it has nothing more specific to say.
export const DEFAULT_SECONDARY = COVERAGE_AREA;

export const CONFIDENCE_ADDRESS_POINT = 0.98;
export const CONFIDENCE_INTERSECTION = 0.95;
export const CONFIDENCE_PLACE = 0.85;
export const CONFIDENCE_ADDRESS_INTERPOLATED = 0.75;
export const CONFIDENCE_NEAR_ADDRESS = 0.6;
export const CONFIDENCE_ADDRESS_RANGE = 0.5;
// A street whose spelling the query matches exactly ("5 ave", "broadway") is
// what the user is naming, and has to outrank a place whose name merely
// contains those words: at 0.45 the query "5 ave" answered 5 AVE SYNAGOGUE
// before Fifth Avenue. A street reached by prefix ("broadwa") is still a guess
// about what is being typed, and one offered because half of an unmatched
// intersection hit it is a consolation prize that ranks below a partial
// place-name match.
export const CONFIDENCE_STREET_EXACT = 0.7;
export const CONFIDENCE_STREET_WHOLE_QUERY = 0.45;
export const CONFIDENCE_STREET_HALF_QUERY = 0.3;
export const CONFIDENCE_ZIP = 0.25;

// What a word match is worth, by where the typed words landed in the name
// (`geocode/names.js`). Two bands, and the street band sits above the place
// band at every rung on purpose: a street is a destination this app can search
// both sides of for its whole length, a place is one point, so when the same
// word names both the street is the safer answer — "lex" is Lexington Avenue,
// not the Lex Hotel. The bands stay under CONFIDENCE_STREET_EXACT, so a street
// the user spelled out whole still beats every name merely containing that
// word; the only exception is a place named exactly what was typed, which
// cannot collide with a street because `etl.addresses.stage_places` drops a
// place whose name repeats a spelling of one.
export const STREET_NAME_CONFIDENCE = {
  [NameMatch.WHOLE]: CONFIDENCE_STREET_EXACT,
  [NameMatch.PREFIX]: 0.69,
  [NameMatch.LEADING]: 0.68,
  [NameMatch.INNER]: 0.67,
};
export const PLACE_NAME_CONFIDENCE = {
  [NameMatch.WHOLE]: CONFIDENCE_PLACE,
  [NameMatch.PREFIX]: 0.66,
  [NameMatch.LEADING]: 0.65,
  [NameMatch.INNER]: 0.64,
};

// A fuzzy street match is a guess about what the user meant, so it costs a
// fifth of the candidate's confidence rather than being silently as good.
export const FUZZY_CONFIDENCE_PENALTY = 0.8;

/**
 * What a candidate *is*, which is what decides the icon and the second line.
 *
 * PIN is the one value the geocoder never returns: it is what the frontend
 * labels a crosshair the user has not dropped yet. Everything else here is
 * produced by `suggest` or `reverseGeocode`.
 */
export const GeocodeKind = Object.freeze({
  ADDRESS: "address",
  INTERSECTION: "intersection",
  STREET: "street",
  ZIP: "zip",
  PLACE: "place",
  PIN: "pin",
});

/**
 * @typedef {object} GeocodeCandidate One place the query might mean.
 * @property {string} label the line the user reads; the city's own capitals, untrusted
 * @property {number} lat
 * @property {number} lon
 * @property {string} kind a `GeocodeKind` value
 * @property {number} confidence 0-1, higher is better
 * @property {string | null} secondary the muted line under it: the ZIP, how the
 *   point was arrived at, or the borough when there is nothing narrower to say
 */

/** @returns {GeocodeCandidate} */
export function geocodeCandidate({
  label,
  lat,
  lon,
  kind,
  confidence,
  secondary = DEFAULT_SECONDARY,
}) {
  return { label, lat, lon, kind, confidence, secondary };
}

/**
 * @typedef {object} ReverseMatch What a dropped pin is nearest to.
 * @property {string} label
 * @property {string | null} secondary
 * @property {string} kind a `GeocodeKind` value
 * @property {number} lat
 * @property {number} lon
 * @property {number} distance_m to the returned point
 */

/** @returns {ReverseMatch} */
export function reverseMatch({ label, secondary, kind, lat, lon, distanceM }) {
  return { label, secondary, kind, lat, lon, distance_m: distanceM };
}

export function zipSecondary(zipcode) {
  // `str(zipcode or "")`: a null, an empty string and a 0 all read as "no ZIP".
  const text = pyStrip(String(zipcode || ""));
  return text ? `${DEFAULT_SECONDARY} ${text}` : DEFAULT_SECONDARY;
}
