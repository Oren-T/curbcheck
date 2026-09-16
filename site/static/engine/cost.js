/**
 * What a parking spot costs: minutes of walking, dollars at the meter, and risk.
 *
 * SPEC §9.4 defines the score as a weighted sum of the three so the user sees the
 * trade rather than a black-box number. Port of `curbcheck/engine/cost.py`, with
 * one change of unit: the Python's `Decimal` dollars are integer cents here, and
 * `../money.js` is what reproduces its rounding. Walk time is float minutes.
 */

import { meterPriceCents, parseCents, riskCents } from "../money.js";

// Mean adult walking speed, Bohannon & Williams Andrews 2011 meta-analysis
// (1.34 m/s across age groups). Used to turn walk distance into walk minutes.
export const WALK_SPEED_M_PER_S = 1.34;

// Manhattan's grid means the walked path is longer than the straight line.
// 1.3 is the standard circuity factor for dense gridded street networks.
export const WALK_DETOUR_FACTOR = 1.3;

// NYC parking fines in Manhattan below 96th St (NYC Finance parking violation
// schedule): code 40 "fire hydrant" is $115; ASP/street-cleaning and expired
// meter are $65. We use the higher figure only for hydrant-class prohibitions.
export const FINE_STANDARD = 65;
export const FINE_HYDRANT_CLASS = 115;

// Probability that parking somewhere we misread as legal actually draws a
// ticket during the window. A placeholder: we have no citation-outcome data to
// fit against, and the term only needs to rank low-confidence segments below
// confident ones. Change this one number to retune the risk term.
export const P_CITE_IF_WRONG = 0.5;

export const EARTH_RADIUS_M = 6_371_008.8; // IUGG mean radius

/** Default user-tunable weights for the ranking score (SPEC §9.4, exposed as sliders). */
export const Weights = Object.freeze({ walk: 1.0, money: 1.0, risk: 0.5 });

/** Great-circle distance between two [lon, lat] pairs in degrees. */
export function haversineMeters([lon1, lat1], [lon2, lat2]) {
  const phi1 = radians(lat1);
  const phi2 = radians(lat2);
  const dPhi = phi2 - phi1;
  const dLambda = radians(lon2 - lon1);
  const a = Math.sin(dPhi / 2) ** 2 + Math.cos(phi1) * Math.cos(phi2) * Math.sin(dLambda / 2) ** 2;
  return 2 * EARTH_RADIUS_M * Math.asin(Math.sqrt(a));
}

/**
 * Straight-line walk estimate: great-circle distance, detour factor, walking speed.
 *
 * No pedestrian router in v1 (SPEC §9.4); this function is the single seam
 * where one would be swapped in.
 */
export function walkMinutes(fromLonLat, toLonLat) {
  return walkMinutesForMeters(haversineMeters(fromLonLat, toLonLat));
}

/** Walk minutes for an already-computed straight-line distance. */
export function walkMinutesForMeters(straightLineMeters) {
  return (straightLineMeters * WALK_DETOUR_FACTOR) / WALK_SPEED_M_PER_S / 60;
}

/** Straight-line radius reachable within `walkMinutesMax`, the inverse of `walkMinutes`. */
export function walkRadiusMeters(walkMinutesMax) {
  if (walkMinutesMax < 0) {
    throw new Error("walk_minutes_max must not be negative");
  }
  return (walkMinutesMax * 60 * WALK_SPEED_M_PER_S) / WALK_DETOUR_FACTOR;
}

/**
 * Progressive meter price in cents for `chargedMinutes`, prorated by the minute.
 *
 * The first charged hour bills at `hourRates[0]`, the second at `hourRates[1]`,
 * and anything beyond the listed hours at the last rate -- NYC publishes a
 * first- and second-hour rate and charges the later rate for longer stays
 * (SPEC §13.2). `hourRates` are the decimal strings the `hour_rates` column
 * stores, so money never round-trips through a float. Only minutes when the
 * meter is actually running should be passed in; Sundays and holidays are free.
 */
export function meterPrice(hourRates, chargedMinutes) {
  if (chargedMinutes < 0) {
    throw new Error("charged_minutes must not be negative");
  }
  if (chargedMinutes === 0) {
    return 0;
  }
  if (hourRates.length === 0) {
    throw new Error("no meter rates for this segment; price is unknown, not zero");
  }
  return meterPriceCents(
    hourRates.map((rate) => parseCents(rate)),
    chargedMinutes,
  );
}

/**
 * Chance of a ticket if we park here, as a function of how sure we are of the rules.
 *
 * The weaker of the two confidences drives it: a perfectly parsed sign matched
 * to the wrong blockface is as dangerous as an unreadable sign on the right
 * one. Linear in doubt, capped by `P_CITE_IF_WRONG`.
 */
export function citationProbability(parseConfidence, snapConfidence) {
  const doubt = 1.0 - Math.min(parseConfidence, snapConfidence);
  return Math.min(1.0, Math.max(0.0, doubt)) * P_CITE_IF_WRONG;
}

/** Expected fine in cents: citation probability times the fine for this class of violation. */
export function riskDollars(parseConfidence, snapConfidence, fine = FINE_STANDARD) {
  return riskCents(parseConfidence, snapConfidence, fine);
}

/**
 * Weighted sum used for ranking. Minutes and dollars are added as raw numbers.
 *
 * They are not commensurable units, which is the point of the weights: the
 * default 1.0/1.0/0.5 reads as "one minute of walking is worth one dollar".
 * `moneyValue` and `riskValue` are dollars -- `cents / 100`, the correctly
 * rounded double of the same rational as Python's `float(Decimal)` -- and
 * `moneyValue` is 0 when the price is unknown.
 */
export function totalCost(walkMin, moneyValue, riskValue, weights = null) {
  const resolved = weights ?? Weights;
  return resolved.walk * walkMin + resolved.money * moneyValue + resolved.risk * riskValue;
}

// Parenthesised so the constant is the one `math.radians` multiplies by.
function radians(degrees) {
  return degrees * (Math.PI / 180);
}
