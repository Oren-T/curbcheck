/**
 * The corner rung: a node both named streets meet at.
 *
 * The pairs are pre-written by the ETL, so a query that names two streets is two
 * street resolutions and one indexed lookup per pair, never a graph walk.
 */

import { singleSpaced } from "../engine/labels.js";
import {
  CONFIDENCE_INTERSECTION,
  FUZZY_CONFIDENCE_PENALTY,
  GeocodeKind,
  geocodeCandidate,
} from "./candidates.js";
import { intersectionRange } from "./index.js";
import { MAX_STREETS_PER_SIDE, resolveStreet } from "./street.js";

/**
 * Nodes where both streets meet, from the pre-paired `intersection` table.
 *
 * Replaces `_INTERSECTION_SQL` (`… WHERE a_norm = ? AND b_norm = ? LIMIT ?`),
 * whose plan is `SEARCH intersection USING COVERING INDEX ix_intersection_pair`
 * with no `ORDER BY`: the `LIMIT` therefore takes the first rows of the index's
 * own `(lon, lat, display, rowid)` order within the pair, which
 * `index.intersectionOrder` holds.
 */
export function intersectionCandidates(pack, index, query, limit) {
  const table = pack.geocode.intersections;
  const firsts = resolveStreet(pack, index, query.first).slice(0, MAX_STREETS_PER_SIDE);
  const seconds = resolveStreet(pack, index, query.second).slice(0, MAX_STREETS_PER_SIDE);
  const found = [];
  for (const [aNorm, aFuzzy] of firsts) {
    for (const [bNorm, bFuzzy] of seconds) {
      const penalty = aFuzzy || bFuzzy ? FUZZY_CONFIDENCE_PENALTY : 1.0;
      const [start, end] = intersectionRange(pack, index, aNorm, bNorm);
      for (let i = start; i < end && i - start < limit; i += 1) {
        const row = index.intersectionOrder[i];
        found.push(
          geocodeCandidate({
            label: singleSpaced(table.display[row]),
            lat: table.lat[row],
            lon: table.lon[row],
            kind: GeocodeKind.INTERSECTION,
            confidence: CONFIDENCE_INTERSECTION * penalty,
          }),
        );
      }
    }
  }
  return found;
}
