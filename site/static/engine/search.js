/**
 * Radius query over regulation segments, evaluated and ranked.
 *
 * Port of `curbcheck/engine/search.py`. Shape of the work: a bounding-box
 * prefilter over the span grid (the SQL's `ix_reg_seg_bbox`), the exact distance
 * in `engine/geo.js`, then verdict, price and rank. The street label and the raw
 * sign text are attached last, for the spans the caps kept, because nothing
 * before the cap reads them.
 *
 * A `SearchResult` here is a plain object with the API's own snake_case keys:
 * `routes._result_payload` is `asdict()` plus two enum values, and building the
 * payload shape directly saves the static site a rename pass the server never
 * makes.
 */

import { loadCalendar } from "./calendar.js";
import { Weights, riskDollars, totalCost, walkMinutesForMeters, walkRadiusMeters } from "./cost.js";
import { degreePadding, measure } from "./geo.js";
import { betweenPhrase, singleSpaced, spanLabel } from "./labels.js";
import { jsonStringList, regulationWithMeta } from "./model.js";
import { Verdict, VerdictBasis, evaluateSegment } from "./resolve.js";
import { centsToString, meterPriceCents, parseCents } from "../money.js";

// The ranked list the user reads is short; the map layer is not. The two caps
// are separate because ranking both together is what hid the illegal curb
// (docs/VALIDATION.md U1).
export const DEFAULT_LIMIT = 100;
export const DEFAULT_MAP_LIMIT = 2000;
// 5,000 line features is about where MapLibre's first paint starts to lag.
export const MAX_MAP_LIMIT = 5000;

// Two legal spans whose cost differs by less than this are the same price as
// far as the ranking is concerned. Inside a band a posted permission outranks
// mere absence of a rule (decision D27).
export const COST_TIE_BAND = 0.5;

// Curb has to overlap by more than this before two spans are treated as
// covering the same stretch. ~1 ft in degrees, so a shared endpoint does not
// count.
const OVERLAP_EPS_DEG = 3e-6;

// Two sub-segments count as collinear when each endpoint of one is this close
// to the other's infinite line, in square degrees of cross product. GEOS uses
// an exact orientation predicate, which would answer "not collinear" for a
// vertex interpolated along the shared centerline in floating point; the
// tolerance is ~0.1 micrometre and the whole test fires on no current database.
const COLLINEAR_EPS_DEG2 = 1e-24;

export const CONTESTED_REASON =
  "another sign on this block prohibits parking over part of this stretch; the signs conflict";

// `search._price`, verbatim: the harness compares caveat strings exactly.
const NO_PUBLISHED_RATE_CAVEAT = "Metered, but no published rate for this blockface.";
const MULTIPLE_METER_ZONES_CAVEAT =
  "More than one meter zone covers this blockface; confirm at the meter.";

/**
 * Rank the curb spans within `walkMinutesMax` of (lon, lat) for the window [t1, t2).
 *
 * Returns the ranked legal spans capped at `limit`, every other verdict in
 * radius capped separately at `mapLimit` (nearest first, so the cap drops the
 * farthest curb rather than a whole verdict), and the counts of all four
 * verdicts taken before either cap.
 *
 * @returns {{legal: object[], others: object[], counts: object}}
 */
export function search(
  pack,
  { lon, lat, t1, t2, walkMinutesMax, weights = null, limit, mapLimit, calendar = null } = {},
) {
  const resolvedLimit = limit === undefined ? DEFAULT_LIMIT : limit;
  let resolvedMapLimit = mapLimit === undefined ? DEFAULT_MAP_LIMIT : mapLimit;
  if (resolvedLimit < 1) {
    throw new RangeError("limit must be at least 1");
  }
  if (resolvedMapLimit < 1) {
    throw new RangeError("map_limit must be at least 1");
  }
  resolvedMapLimit = Math.min(resolvedMapLimit, MAX_MAP_LIMIT);
  const resolvedWeights = weights || Weights;
  const resolvedCalendar = calendar || loadCalendar(pack);

  const candidates = _candidatesInRadius(pack, lon, lat, walkRadiusMeters(walkMinutesMax));
  if (candidates.length === 0) {
    return { legal: [], others: [], counts: _countVerdicts([]) };
  }

  const stacks = _loadStacks(pack, candidates);
  const meters = _loadMeterRates(pack, candidates);

  const verdicts = new Map();
  for (const candidate of candidates) {
    verdicts.set(
      candidate.regSegId,
      evaluateSegment(stacks.get(candidate.regSegId) || [], t1, t2, resolvedCalendar, {
        gapKind: candidate.gapKind,
      }),
    );
  }
  _demoteContestedSpans(candidates, verdicts);

  const results = candidates.map((candidate) =>
    _buildResult(
      candidate,
      verdicts.get(candidate.regSegId),
      meters.get(faceKey(candidate.segmentId, candidate.side)) || [],
      resolvedWeights,
    ),
  );
  const found = _splitAndCap(results, resolvedLimit, resolvedMapLimit);
  return _attachDetails(pack, found, candidates, stacks);
}

/**
 * Turn a legal span that a prohibition also covers into AMBIGUOUS, in place.
 *
 * Since docs/DECISIONS.md D25 and D26 this should never fire, and there are
 * zero such pairs on the 2026-09-15 database (docs/VALIDATION.md §11). It is
 * kept as defence in depth against a pack built from an older snapshot, where a
 * permissive span reaching back over a `NO STANDING ANYTIME` span would read
 * LEGAL over curb that is not — SPEC §8.6's P0 defect.
 */
function _demoteContestedSpans(candidates, verdicts) {
  const byFace = new Map();
  for (const candidate of candidates) {
    const key = faceKey(candidate.segmentId, candidate.side);
    const face = byFace.get(key);
    if (face === undefined) {
      byFace.set(key, [candidate]);
    } else {
      face.push(candidate);
    }
  }

  let contested = 0;
  for (const face of byFace.values()) {
    if (face.length < 2) {
      continue;
    }
    const banned = face.filter((c) => verdicts.get(c.regSegId).verdict === Verdict.ILLEGAL);
    const allowed = face.filter((c) => verdicts.get(c.regSegId).verdict === Verdict.LEGAL);
    if (banned.length === 0 || allowed.length === 0) {
      continue;
    }
    const shapes = new Map(face.map((c) => [c.regSegId, _lineOrNone(c.geometry)]));
    for (const candidate of allowed) {
      const line = shapes.get(candidate.regSegId);
      if (line === null) {
        continue;
      }
      if (banned.some((other) => _overlaps(line, shapes.get(other.regSegId)))) {
        contested += 1;
        const verdict = verdicts.get(candidate.regSegId);
        verdicts.set(candidate.regSegId, {
          ...verdict,
          verdict: Verdict.AMBIGUOUS,
          reason: CONTESTED_REASON,
          // `basis` and `deciding` are derived properties on the Python
          // dataclass, so `replace(verdict=AMBIGUOUS)` recomputes them to None.
          basis: null,
          deciding: null,
        });
      }
    }
  }
  if (contested) {
    console.warn(
      `demoted ${contested} overlapping legal span(s) to ambiguous; this pack predates` +
        " docs/DECISIONS.md D25 and should be rebuilt with `curbcheck sync`",
    );
  }
}

/** The vertex list of a LineString, or null for anything shapely would refuse. */
function _lineOrNone(geometry) {
  if (
    geometry === null ||
    geometry.type !== "LineString" ||
    !Array.isArray(geometry.coordinates) ||
    geometry.coordinates.length < 2
  ) {
    return null;
  }
  return geometry.coordinates;
}

/** `line.intersection(other).length > _OVERLAP_EPS_DEG`, as a collinear overlap. */
function _overlaps(line, other) {
  if (other === null || other === undefined) {
    return false;
  }
  return collinearOverlapLength(line, other) > OVERLAP_EPS_DEG;
}

/**
 * The length two polylines share, counting only the stretches where they run
 * along the same line.
 *
 * `ranges.linemerge` is not reached here: two spans that merely cross meet at a
 * point, whose length is zero, so only a collinear overlap can pass the
 * threshold. Summing per segment pair is the length of the intersection for the
 * only shape this can see — two spans cut from one centerline.
 */
function collinearOverlapLength(line, other) {
  let total = 0;
  for (let i = 0; i + 1 < line.length; i += 1) {
    const ax = line[i][0];
    const ay = line[i][1];
    const bx = line[i + 1][0];
    const by = line[i + 1][1];
    const dx = bx - ax;
    const dy = by - ay;
    const len2 = dx * dx + dy * dy;
    if (len2 === 0) {
      continue;
    }
    for (let j = 0; j + 1 < other.length; j += 1) {
      const cx = other[j][0];
      const cy = other[j][1];
      const ex = other[j + 1][0];
      const ey = other[j + 1][1];
      const offC = (cx - ax) * dy - (cy - ay) * dx;
      const offE = (ex - ax) * dy - (ey - ay) * dx;
      if (offC * offC > COLLINEAR_EPS_DEG2 * len2 || offE * offE > COLLINEAR_EPS_DEG2 * len2) {
        continue;
      }
      const tC = ((cx - ax) * dx + (cy - ay) * dy) / len2;
      const tE = ((ex - ax) * dx + (ey - ay) * dy) / len2;
      const low = Math.max(0, Math.min(tC, tE));
      const high = Math.min(1, Math.max(tC, tE));
      if (high > low) {
        total += (high - low) * Math.sqrt(len2);
      }
    }
  }
  return total;
}

function _splitAndCap(results, limit, mapLimit) {
  const legal = _rankLegal(results.filter((result) => result.verdict === Verdict.LEGAL));
  const others = results.filter((result) => result.verdict !== Verdict.LEGAL);
  // Choose *which* others survive the cap by distance, so a dense band of one
  // verdict cannot crowd out another, then order the survivors for display.
  const kept = others
    .slice()
    .sort((a, b) => compareBy(a.walk_min, b.walk_min) || compareBy(a.reg_seg_id, b.reg_seg_id))
    .slice(0, mapLimit);
  kept.sort(
    (a, b) =>
      compareBy(verdictOrder(a.verdict), verdictOrder(b.verdict)) ||
      compareBy(a.score, b.score) ||
      compareBy(a.reg_seg_id, b.reg_seg_id),
  );
  return { legal: legal.slice(0, limit), others: kept, counts: _countVerdicts(results) };
}

/**
 * Add the street label and the raw sign text to the spans that survived the caps.
 *
 * Neither one reaches the ranking, and both are the expensive half of the
 * query, so they are fetched for the kept spans only. The Python rebuilds the
 * result objects with `dataclasses.replace`; here every kept object is filled in
 * place, which is the same set of objects and the same values.
 */
function _attachDetails(pack, found, candidates, stacks) {
  const kept = [...found.legal, ...found.others];
  const byId = new Map(candidates.map((candidate) => [candidate.regSegId, candidate]));
  const wanted = kept.map((result) => byId.get(result.reg_seg_id));
  _labelCandidates(pack, wanted);
  const signs = _loadSigns(pack, wanted, stacks);

  for (const result of kept) {
    result.signs = signs.get(result.reg_seg_id) || [];
    result.street_name = byId.get(result.reg_seg_id).streetName;
  }
  return found;
}

/**
 * Cheapest first, but a posted permission ahead of mere absence at the same cost.
 *
 * The ranking stays a cost ranking: only spans whose cost the user could not
 * tell apart are reordered, and only to put a span with a sign to read above one
 * where nothing is posted (decision D27, UX audit P0-1).
 */
function _rankLegal(legal) {
  const byCost = legal
    .slice()
    .sort((a, b) => compareBy(a.score, b.score) || compareBy(a.reg_seg_id, b.reg_seg_id));
  const ranked = [];
  for (const band of _costBands(byCost)) {
    band.sort(
      (a, b) => compareBy(basisOrder(a.basis), basisOrder(b.basis)) || compareBy(a.score, b.score),
    );
    ranked.push(...band);
  }
  return ranked;
}

/**
 * Runs of results within COST_TIE_BAND of the *first* result in the run.
 *
 * Anchoring each band on its own first element rather than on the previous one
 * is what keeps a dense list from chaining into one band: 600 spans a tenth of a
 * point apart would otherwise all tie, and the cost ranking would stop meaning
 * anything.
 */
function _costBands(byCost) {
  const bands = [];
  let band = [];
  for (const result of byCost) {
    if (band.length && result.score - band[0].score > COST_TIE_BAND) {
      bands.push(band);
      band = [];
    }
    band.push(result);
  }
  if (band.length) {
    bands.push(band);
  }
  return bands;
}

function _countVerdicts(results) {
  const tally = { legal: 0, illegal: 0, ambiguous: 0, no_data: 0 };
  for (const result of results) {
    tally[result.verdict] += 1;
  }
  return { ...tally, total: results.length };
}

function _candidatesInRadius(pack, lon, lat, radiusM) {
  const pad = degreePadding(lat, radiusM);
  const minLon = lon - pad.lon;
  const maxLon = lon + pad.lon;
  const minLat = lat - pad.lat;
  const maxLat = lat + pad.lat;
  const spans = pack.spans;
  const bbox = spans.bbox;

  const candidates = [];
  let unreadable = 0;
  // The grid stands in for `_CANDIDATE_SQL`'s bbox `WHERE`, which has no
  // `ORDER BY`; rows arrive in ascending row order. Every list this feeds is
  // sorted on a key ending in `reg_seg_id`, so the scan order is not observable.
  for (const row of pack.index.spanGrid.query(minLon, minLat, maxLon, maxLat)) {
    if (bbox.maxLon[row] < minLon || bbox.minLon[row] > maxLon) {
      continue;
    }
    if (bbox.maxLat[row] < minLat || bbox.minLat[row] > maxLat) {
      continue;
    }
    const measured = measure(spans.geom, row, lon, lat);
    if (measured === null) {
      unreadable += 1;
      continue;
    }
    if (measured.distanceM > radiusM) {
      continue;
    }
    const segmentRow = spans.segment[row];
    candidates.push({
      spanRow: row,
      regSegId: String(spans.regSegId[row]),
      segmentId: spans.segmentId[row] === undefined ? null : spans.segmentId[row],
      segmentRow: segmentRow === undefined ? -1 : segmentRow,
      side: spans.side[row] === undefined ? null : spans.side[row],
      geometry: pack.lineString(spans, row),
      capacityCars: spans.capacityCars[row] === undefined ? null : spans.capacityCars[row],
      snapConfidence: spans.confidence[row] || 0.0,
      signRows: listAt(spans.derivedFrom, row),
      walkMin: walkMinutesForMeters(measured.distanceM),
      gapKind: spans.gapKind[row] === undefined ? null : spans.gapKind[row],
      streetName: null,
    });
  }
  if (unreadable) {
    // One line for the whole query, not one per row: a truncated pack can hold
    // thousands of these and the point is the count, not each id.
    console.warn(`skipped ${unreadable} regulation_segment row(s) with unreadable geometry`);
  }
  return candidates;
}

/**
 * Give every candidate a human label: the centerline's own street name, the
 * side, and the cross streets at the two ends of the segment it lies on.
 *
 * Stands in for `_STREET_SQL`, whose `LEFT JOIN street_node` is the two node row
 * indexes on the segment; a node index of -1 is the join finding no row, which
 * `json_string_list` reads as no names.
 */
function _labelCandidates(pack, candidates) {
  const labels = new Map();
  for (const candidate of candidates) {
    const segmentRow = candidate.segmentRow;
    if (segmentRow < 0 || candidate.segmentId === null || labels.has(candidate.segmentId)) {
      continue;
    }
    const streetName = singleSpaced(String(pack.segments.streetName[segmentRow] || ""));
    if (!streetName) {
      continue;
    }
    labels.set(candidate.segmentId, [
      streetName,
      betweenPhrase(
        streetName,
        nodeStreetNames(pack, pack.segments.fromNode[segmentRow]),
        nodeStreetNames(pack, pack.segments.toNode[segmentRow]),
      ),
    ]);
  }
  for (const candidate of candidates) {
    const named = labels.get(candidate.segmentId === null ? "" : candidate.segmentId);
    if (named === undefined) {
      continue;
    }
    candidate.streetName = spanLabel(named[0], candidate.side, named[1]);
  }
}

function nodeStreetNames(pack, nodeRow) {
  return nodeRow === undefined || nodeRow < 0 ? null : pack.nodes.streetNames[nodeRow];
}

/**
 * The rule stack behind each candidate, in rowid order within a span.
 *
 * `_REGULATION_SQL` has no `ORDER BY`; `ix_regulation_seg` is on `reg_seg_id`
 * alone, so SQLite returns each span's rules in rowid order, which is the order
 * `pack.index.regulationsBySpan` keeps.
 */
function _loadStacks(pack, candidates) {
  const stacks = new Map();
  for (const candidate of candidates) {
    const rows = pack.index.regulationsBySpan[candidate.spanRow];
    if (rows === undefined || rows.length === 0) {
      continue;
    }
    stacks.set(
      candidate.regSegId,
      Array.from(rows, (row) => regulationWithMeta(pack, row)),
    );
  }
  return stacks;
}

/**
 * The sign rows behind each span, with the parse metadata of their rules.
 *
 * A sign is tied to its rules by `raw_sign_description`: the parser reads each
 * distinct description once (docs/ARCHITECTURE.md step 6), so the description is
 * what a `regulation` row and a `sign` row have in common. The pack drops a
 * `derived_from` id that names no sign row, which is what the Python's
 * `rows_by_id.get(...) is None` skip does.
 */
function _loadSigns(pack, candidates, stacks) {
  const signs = new Map();
  for (const candidate of candidates) {
    const parses = _parseMetadata(stacks.get(candidate.regSegId) || []);
    const refs = [];
    for (const row of candidate.signRows) {
      const description = String(pack.signs.signDescription[row]);
      const parse = parses.get(description);
      refs.push({
        sign_id: String(pack.signs.signId[row]),
        order_number: optional(pack.signs.orderNumber[row]),
        sign_code: optional(pack.signs.signCode[row]),
        sign_description: description,
        parse_method: parse === undefined ? null : parse[0],
        parse_confidence: parse === undefined ? null : parse[1],
      });
    }
    signs.set(candidate.regSegId, refs);
  }
  return signs;
}

/** Per description, the least confident parse of it in this stack. */
function _parseMetadata(stack) {
  const parses = new Map();
  for (const item of stack) {
    const existing = parses.get(item.rawSignDescription);
    if (existing === undefined || (existing[1] !== null && item.parseConfidence < existing[1])) {
      parses.set(item.rawSignDescription, [item.parseMethod, item.parseConfidence]);
    }
  }
  return parses;
}

/**
 * Rates keyed by (segment_id, side). SPEC §13.1: a blockface may carry more
 * than one zone.
 *
 * `_load_meter_rates` reads them through `ix_meter_rate_segment`
 * (segment_id, side, rowid), so within one key the order is rowid order, which
 * is the order `pack.index.meterRatesBySegment` keeps. Only the within-key order
 * is observable: `_price` takes the first of the dearest.
 */
function _loadMeterRates(pack, candidates) {
  const rates = new Map();
  const seen = new Set();
  for (const candidate of candidates) {
    if (candidate.segmentRow < 0 || seen.has(candidate.segmentRow)) {
      continue;
    }
    seen.add(candidate.segmentRow);
    const rows = pack.index.meterRatesBySegment[candidate.segmentRow];
    for (const row of rows === undefined ? [] : rows) {
      const hourRates = _decimalList(pack.meterRates.hourRates[row]);
      if (hourRates === null || hourRates.length === 0) {
        continue;
      }
      const key = faceKey(
        pack.meterRates.segmentId[row] === undefined ? null : pack.meterRates.segmentId[row],
        pack.meterRates.side[row] === undefined ? null : pack.meterRates.side[row],
      );
      const bucket = rates.get(key);
      const entry = [optional(pack.meterRates.rateLabel[row]), hourRates];
      if (bucket === undefined) {
        rates.set(key, [entry]);
      } else {
        bucket.push(entry);
      }
    }
  }
  return rates;
}

/** Everything the ranking needs. `signs` and `street_name` arrive in `_attachDetails`. */
function _buildResult(candidate, verdict, rates, weights) {
  const caveats = [...verdict.caveats];
  const priced = _price(verdict, rates, caveats, candidate.gapKind !== null);
  const riskCents = riskDollars(verdict.confidence, candidate.snapConfidence);
  const moneyValue = priced.cents === null ? null : priced.cents / 100;
  const riskValue = riskCents / 100;
  const basis = verdict.basis === undefined ? null : verdict.basis;
  return {
    reg_seg_id: candidate.regSegId,
    geometry: candidate.geometry,
    verdict: verdict.verdict,
    reason: verdict.reason,
    caveats,
    walk_min: candidate.walkMin,
    money: priced.cents === null ? null : centsToString(priced.cents),
    price_known: priced.known,
    capacity_cars: candidate.capacityCars,
    confidence: Math.min(verdict.confidence, candidate.snapConfidence),
    signs: [],
    charged_minutes: verdict.chargedMinutes,
    metered: verdict.metered,
    // `money or Decimal("0.00")`: an unknown price counts as nothing in the
    // score, and the three terms are all in the response so a client can
    // re-rank under its own weights without a round trip.
    score: totalCost(candidate.walkMin, moneyValue === null ? 0 : moneyValue, riskValue, weights),
    money_value: moneyValue,
    risk: riskValue,
    basis,
    confidence_shown: _confidenceIsMeaningful(verdict.verdict, basis),
    rate_label: priced.label,
    street_name: null,
    gap_kind: candidate.gapKind,
  };
}

/**
 * Whether the confidence number says anything the user should be shown.
 *
 * On NO_DATA it is 0 next to a reason that says nothing was read, and on an
 * absence-based LEGAL verdict it is the confidence of an empty stack, i.e. 1.
 * Both printed as a percentage read as certainty about parking rather than as
 * certainty about a reading (UX audit P0-1, P2-1).
 */
function _confidenceIsMeaningful(verdict, basis) {
  return verdict !== Verdict.NO_DATA && basis !== VerdictBasis.ABSENCE;
}

/**
 * Meter cost in cents for the window, or null when we have no rate. Never
 * fabricate one (SPEC §13.1d).
 *
 * Curb with no rules is unpriced, not free: "$0.00" and "no meter" were being
 * printed on grey spans the app knows nothing about (UX audit P0-5, SPEC §11).
 */
function _price(verdict, rates, caveats, placeholder) {
  if (placeholder || verdict.verdict === Verdict.NO_DATA) {
    return { cents: null, known: false, label: null };
  }
  if (!verdict.metered || verdict.chargedMinutes === 0) {
    return { cents: 0, known: true, label: null };
  }
  if (rates.length === 0) {
    caveats.push(NO_PUBLISHED_RATE_CAVEAT);
    return { cents: null, known: false, label: null };
  }

  const priced = rates.map(([label, hourRates]) => [
    meterPriceCents(hourRates, verdict.chargedMinutes),
    label,
  ]);
  // `max(..., key=...)` keeps the first of equal maxima.
  let highest = priced[0];
  for (const item of priced) {
    if (item[0] > highest[0]) {
      highest = item;
    }
  }
  if (new Set(priced.map((item) => item[0])).size > 1) {
    // SPEC §13.1(b): a few blockfaces carry several zones. Quote the dearest
    // and tell the user to confirm rather than guessing which one applies.
    caveats.push(MULTIPLE_METER_ZONES_CAVEAT);
    return { cents: highest[0], known: false, label: highest[1] };
  }
  return { cents: highest[0], known: true, label: highest[1] };
}

const BASIS_ORDER = { posted: 0, absence: 1 };
const VERDICT_ORDER = { legal: 0, ambiguous: 1, illegal: 2, no_data: 3 };

function basisOrder(basis) {
  const order = BASIS_ORDER[basis];
  return order === undefined ? 1 : order;
}

function verdictOrder(verdict) {
  const order = VERDICT_ORDER[verdict];
  return order === undefined ? VERDICT_ORDER.no_data : order;
}

/**
 * Hourly rates in cents, or [] when the column cannot be read.
 *
 * `search._decimal_list`: rates are stored as JSON strings so money never round
 * trips through a float, and a cell that is not a list of decimals means "no
 * known rate", which every caller already handles.
 */
export function _decimalList(value) {
  const rates = [];
  for (const item of jsonStringList(value)) {
    let cents = null;
    try {
      cents = parseCents(item);
    } catch {
      return [];
    }
    if (!Number.isInteger(cents)) {
      return [];
    }
    rates.push(cents);
  }
  return rates;
}

/** `(segment_id, side)`, the key `_load_meter_rates` and `_demote_contested_spans` group on. */
function faceKey(segmentId, side) {
  return JSON.stringify([segmentId, side]);
}

function listAt(column, row) {
  return column.lists.subarray(column.offsets[row], column.offsets[row + 1]);
}

function optional(value) {
  return value === null || value === undefined ? null : String(value);
}

/** Python's tuple comparison, one element at a time; strings by UTF-16 code unit. */
function compareBy(a, b) {
  if (a < b) {
    return -1;
  }
  return a > b ? 1 : 0;
}
