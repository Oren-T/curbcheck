/**
 * Safety-critical wording, kept in one file so it can be diffed against the spec.
 *
 * SPEC §11 makes these four states non-negotiable and requires that they are
 * never collapsed into a plain legal/illegal pair. The strings below are the
 * spec's own words. The API sends its own caveat strings with every result;
 * these are what the page says on its own behalf, and the fallback when the
 * API is unreachable.
 */

export const VERDICT_EXPLANATION = {
  // SPEC §11, "No sign data on this block". Said when the span does not record
  // *why* it is empty; `NO_DATA_EXPLANATION` below is said when it does.
  no_data:
    "No regulation data here. Sign-free prohibitions (hydrant 15 ft, bus stop, crosswalk, " +
    "driveway) may still apply. Read the curb.",
  // SPEC §11, "Ambiguous rule": show the raw text and say the machine could not read it.
  ambiguous:
    "Ambiguous rule. The software could not confidently read the signs on this stretch, or the " +
    "signs conflict. The raw sign text is below — read it yourself, and read the curb.",
};

/**
 * Why a grey stretch is grey, keyed by `SearchResult.gap_kind` (docs/API.md).
 *
 * The two are not the same state and must not read the same. `no_signs` is
 * curb DOT publishes no sign for; `unmatched_signs` is curb DOT *does* publish
 * signs for that CurbCheck could not place on the centerline — 722 blockface-
 * sides, 520 of them carrying a NO STANDING/PARKING/STOPPING ANYTIME sign
 * (docs/VALIDATION.md §5). Telling the second one "no signs here" would be a
 * false statement about the curb, not merely a vague one.
 */
export const NO_DATA_EXPLANATION = {
  no_signs:
    "No regulation data on this stretch. NYC DOT's inventory lists no signs here. Sign-free " +
    "prohibitions (hydrant 15 ft, bus stop, crosswalk, driveway) may still apply. Read the curb.",
  unmatched_signs:
    "Signs exist on this block but CurbCheck could not place them. Treat as unknown. Read the " +
    "posted signs.",
};

/** The `no_data` wording for one span: gap-kind specific where the span says which. */
export function noDataExplanation(gapKind) {
  return NO_DATA_EXPLANATION[gapKind] || VERDICT_EXPLANATION.no_data;
}

// SPEC §11, and the legend: an empty map is read as "nothing here", which is
// the hazard SPEC §11 exists to prevent arriving through another door
// (docs/VALIDATION.md §5).
export const NO_DATA_LEGEND_NOTE = "A blank or grey curb means no data, not no restriction.";

// SPEC §11, "Temporary signage may override": shown on every verdict, always.
export const TEMPORARY_SIGNAGE_CAVEAT =
  "Temporary or construction signage may override what is shown here. The posted sign at the " +
  "curb is the only authoritative regulation.";

// The engine sends this with every result when the database has no ASP and
// holiday calendar (curbcheck/engine/resolve.py). The page says it once more,
// on load, because by then the user has not searched yet.
export const CALENDAR_MISSING_CAVEAT =
  "Holiday and street-cleaning suspension calendar is missing; holiday and ASP verdicts " +
  "may be wrong";

// SPEC §11, "Emergency ASP suspensions (offline mode)".
export const ASP_SUSPENSION_CAVEAT =
  "Emergency ASP suspensions are not reflected. Same-day weather and parade suspensions are " +
  "only visible if the optional 311 live check is enabled, and it is off by default.";

// SPEC §17. index.html carries the same text; this is the copy app.js falls back
// to if the banner element is ever rebuilt from script.
export const DISCLAIMER =
  "CurbCheck is advisory only. This tool derives parking legality and price from NYC Open Data " +
  "(NYC DOT Sign Information Management System) and may be incomplete, out of date, or misread " +
  "by the software. Parking regulations change and temporary or construction signage may " +
  "override what is shown here. The posted sign at the curb is the only authoritative " +
  "regulation. Always read the posted sign before parking. Sign-free prohibitions — within 15 " +
  "feet of a fire hydrant (34 RCNY §4-08(e)(2)), crosswalks, bus stops, and driveways — apply " +
  "even where no sign is shown. CurbCheck does not predict whether a space is physically " +
  "available. A blank or grey curb means no data, not no restriction. " +
  "Data © NYC Open Data; basemap © OpenStreetMap contributors.";

// D13: an unparsed sign's `regulation` object is a placeholder whose fields mean
// nothing, so the panel shows this instead of pretending to read it. It is the
// amber "the machine could not read it" signal of SPEC §11 and must be reserved
// for that case: a sign the parser really did fail on. The two strings below
// cover the other two reasons a sign can carry no rule here, and neither of them
// is a parser failure.
export const UNPARSED_RULE =
  "The software could not read this sign. Only the raw text above is trustworthy.";

// D10: a panel that states no regulation (bus route, pay-by-cell locator,
// location plate) never reaches the parser. `/api/segment` lists it for audit.
export const PANEL_STATES_NO_RULE =
  "This panel states no parking rule. It is shown because it is posted on this block.";

// `/api/segment` lists every sign on the parent centerline segment, including
// the ones governing the other side or a different stretch of the same side.
export const SIGN_NOT_ON_THIS_STRETCH =
  "This sign is posted on this block but does not govern this stretch. Read it anyway if you " +
  "are parking near it.";
