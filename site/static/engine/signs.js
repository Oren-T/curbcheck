/**
 * The signs behind one curb span, split into the ones that govern it and the rest.
 *
 * Port of `curbcheck/engine/signs.py`. SPEC §10 requires the raw sign text next
 * to every verdict; what this module adds is that the span says which of the
 * blockface's signs produced its verdict, and in what order they stand along the
 * curb (UX audit P0-2). Grouping is allowed, deleting is not: every sign the old
 * list showed for the blockface-side is still returned, in `otherOnBlock`.
 */

import { jsonStringList } from "./model.js";
import { UNMATCHED_SIGNS } from "./resolve.js";
import { normalizeStreetName } from "../geocode/normalize.js";

/**
 * The signs behind one span, split into governing and merely nearby, in curb order.
 *
 * `governing` is the span's `derived_from` set: the posts whose text became its
 * rules. `otherOnBlock` is every other sign DOT posts on the same blockface-side
 * — the same `(on, from, to, side)` name tuple as a governing sign, or, when the
 * span has no governing sign to take a tuple from, the signs snapped to this
 * centerline segment on the same side letter.
 *
 * A placeholder span (`gap_kind` set) has no governing signs at all. When its
 * gap is `unmatched_signs`, DOT does publish signs for the blockface and none of
 * them could be placed on the centerline; those are found by name, because 332
 * of the 499 unmatched blockface-sides carry a NO STANDING/PARKING/STOPPING
 * ANYTIME sign (docs/VALIDATION.md §5).
 *
 * `derivedFromRows` and `segmentRow` are the span's own columns, already
 * decoded; `gap_kind` is read back off the span, which is the one field the
 * caller has no reason to carry.
 *
 * @returns {{governing: object[], otherOnBlock: object[]}}
 */
export function blockfaceSigns(pack, regSegId, derivedFromRows, segmentRow, side) {
  if (segmentRow === undefined || segmentRow === null || segmentRow < 0) {
    return { governing: [], otherOnBlock: [] };
  }
  const gapKind = spanGapKind(pack, regSegId);
  const resolvedSide = side === undefined ? null : side;

  const onSegment = Array.from(pack.index.signsBySegment[segmentRow] || [], (row) =>
    _detail(pack, row),
  );
  const wanted = new Set(Array.from(derivedFromRows, (row) => String(pack.signs.signId[row])));
  const governing = gapKind ? [] : onSegment.filter((sign) => wanted.has(sign.sign_id));

  const keys = new Set(governing.map(blockfaceKey));
  let rest = keys.size
    ? onSegment.filter((sign) => !wanted.has(sign.sign_id) && keys.has(blockfaceKey(sign)))
    : onSegment.filter((sign) => sign.side_of_street === resolvedSide);
  if (gapKind === UNMATCHED_SIGNS) {
    rest = rest.concat(_unmatchedOnBlockface(pack, segmentRow, resolvedSide));
  }

  return { governing: _alongTheCurb(governing), otherOnBlock: _alongTheCurb(rest) };
}

/** DOT's `(on, from, to, side)` group, which is what it calls a blockface-side. */
function blockfaceKey(sign) {
  return JSON.stringify([sign.on_street, sign.from_street, sign.to_street, sign.side_of_street]);
}

function spanGapKind(pack, regSegId) {
  const row = pack.index.spanByRegSegId.get(regSegId);
  if (row === undefined) {
    return null;
  }
  const gapKind = pack.spans.gapKind[row];
  return gapKind === undefined ? null : gapKind;
}

/** In order of distance from the intersection DOT measured them from, unmeasured last. */
function _alongTheCurb(signs) {
  return signs.slice().sort((a, b) => {
    const aMissing = a.distance_from_intersection === null;
    const bMissing = b.distance_from_intersection === null;
    if (aMissing !== bMissing) {
      return aMissing ? 1 : -1;
    }
    const aDistance = a.distance_from_intersection || 0.0;
    const bDistance = b.distance_from_intersection || 0.0;
    if (aDistance !== bDistance) {
      return aDistance < bDistance ? -1 : 1;
    }
    if (a.sign_id === b.sign_id) {
      return 0;
    }
    return a.sign_id < b.sign_id ? -1 : 1;
  });
}

/**
 * The signs DOT posts on this blockface-side that never snapped to a centerline.
 *
 * The same name test `etl.segments._gap_kind` uses to decide that this side's
 * gap is `unmatched_signs`: the on-street has to be this street, and both cross
 * streets have to be streets that meet this segment — unless one of the names is
 * in no centerline row at all, which is the case that produced the miss in the
 * first place (docs/VALIDATION.md §4 D4).
 *
 * `_UNMATCHED_SIGNS_SQL` reads `ix_sign_unmatched`, so SQLite hands the rows
 * back ordered by (on_street, from_street, to_street, sign_id) within the side.
 * That order is not reproduced because it is not observable: everything this
 * returns goes through `_alongTheCurb`, whose key ends in `sign_id`.
 */
function _unmatchedOnBlockface(pack, segmentRow, side) {
  if (side === null) {
    return [];
  }
  const streetNorm = String(pack.segments.streetNorm[segmentRow]);
  const crossing = new Set();
  for (const nodeRow of [pack.segments.fromNode[segmentRow], pack.segments.toNode[segmentRow]]) {
    if (nodeRow === undefined || nodeRow < 0) {
      continue;
    }
    for (const name of jsonStringList(pack.nodes.streetNames[nodeRow])) {
      crossing.add(normalizeStreetName(name));
    }
  }

  let knownStreets = null;
  const unmatched = [];
  for (const row of unmatchedSignRows(pack, side)) {
    const sign = _detail(pack, row);
    if (normalizeStreetName(sign.on_street || "") !== streetNorm) {
      continue;
    }
    const crosses = new Set([
      normalizeStreetName(sign.from_street || ""),
      normalizeStreetName(sign.to_street || ""),
    ]);
    if ([...crosses].every((name) => crossing.has(name))) {
      unmatched.push(sign);
      continue;
    }
    if ([...crosses].some((name) => crossing.has(name))) {
      if (knownStreets === null) {
        knownStreets = new Set();
        for (let segment = 0; segment < pack.segments.n; segment += 1) {
          knownStreets.add(String(pack.segments.streetNorm[segment]));
        }
      }
      if ([...crosses].some((name) => !knownStreets.has(name))) {
        unmatched.push(sign);
      }
    }
  }
  return unmatched;
}

/**
 * `WHERE segment_id IS NULL AND side_of_street = ?`, matched on the string
 * column the SQL tests rather than on the pack's resolved row index.
 *
 * A full pass over the signs table where the Python has a partial index over
 * the 2,700 rows that never snapped. Only the 499 `unmatched_signs`
 * placeholders reach it, once per detail panel, so the scan is cheaper than an
 * index the loader would build for every pack.
 */
function unmatchedSignRows(pack, side) {
  const rows = [];
  for (let row = 0; row < pack.signs.n; row += 1) {
    const segmentId = pack.signs.segmentId[row];
    if ((segmentId === null || segmentId === undefined) && pack.signs.sideOfStreet[row] === side) {
      rows.push(row);
    }
  }
  return rows;
}

/** One `sign` row as the detail panel needs it. `sign_description` is untrusted. */
function _detail(pack, row) {
  const signs = pack.signs;
  const distanceFt = optionalFloat(signs.distanceFromIntersection[row]);
  return {
    sign_id: String(signs.signId[row]),
    order_number: optionalStr(signs.orderNumber[row]),
    sign_code: optionalStr(signs.signCode[row]),
    sign_description: String(signs.signDescription[row]),
    on_street: optionalStr(signs.onStreet[row]),
    from_street: optionalStr(signs.fromStreet[row]),
    to_street: optionalStr(signs.toStreet[row]),
    side_of_street: optionalStr(signs.sideOfStreet[row]),
    distance_from_intersection: distanceFt,
    // DOT's `distance_from_intersection` is in feet; the unit is in the name so
    // the UI can print it without going back to the dataset to find out.
    distance_ft: distanceFt,
    // DOT's own bearing word for the arrow on the post, or null on the 73.5% of
    // signs with no arrow. Which way that points *along this curb* is the
    // resolved `arrow` on each rule, not this.
    arrow: optionalStr(signs.arrowDirection[row]),
    snap_confidence: optionalFloat(signs.snapConfidence[row]),
    snap_notes: optionalStr(signs.snapNotes[row]),
    is_regulation: Boolean(signs.isRegulation[row]),
    panel_class: String(signs.panelClass[row]),
  };
}

function optionalStr(value) {
  return value === null || value === undefined ? null : String(value);
}

function optionalFloat(value) {
  return value === null || value === undefined ? null : Number(value);
}
