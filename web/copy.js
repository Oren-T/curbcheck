/**
 * Safety-critical wording, kept in one file so it can be diffed against the spec.
 *
 * SPEC §11 makes the four verdict states non-negotiable and requires that they
 * are never collapsed into a plain legal/illegal pair. The strings below are the
 * spec's own words, tightened to the tone in docs/ux/DESIGN_DIRECTION.md §7:
 * plain, second person, shortest true sentence, and an uncertain state always
 * ends with the action ("Read the curb").
 *
 * The API sends its own caveat strings with every result; these are what the
 * page says on its own behalf, and the fallback when the API is unreachable.
 */

/** The two load-bearing sentences the advisory strip carries at every size. */
export const STRIP_ADVISORY = "Advisory only. Read the posted sign.";
export const STRIP_NO_DATA = "Grey means no data, not no restriction.";

export const VERDICT_EXPLANATION = {
  // SPEC §11, "No sign data on this block". Said when the span does not record
  // *why* it is empty; `NO_DATA_EXPLANATION` below is said when it does.
  no_data:
    "No regulation data here. Sign-free prohibitions (hydrant 15 ft, bus stop, crosswalk, " +
    "driveway) may still apply. Read the curb.",
  // SPEC §11, "Ambiguous rule": show the raw text and say the machine could not read it.
  ambiguous:
    "The software could not read these signs, or they conflict. Read them yourself, below, " +
    "and read the curb.",
};

/**
 * Why a grey stretch is grey, keyed by `SearchResult.gap_kind` (docs/API.md).
 *
 * The two are not the same state and must not read the same. `no_signs` is
 * curb DOT publishes no sign for; `unmatched_signs` is curb DOT *does* publish
 * signs for that CurbCheck could not place on the centerline — 499 blockface-
 * sides, 332 of them carrying a NO STANDING/PARKING/STOPPING ANYTIME sign, on
 * the 2026-09-15 snapshot. Telling the second one "no signs here" would be a
 * false statement about the curb, not merely a vague one.
 */
export const NO_DATA_EXPLANATION = {
  no_signs:
    "NYC DOT lists no sign on this stretch. Unknown, not free. Sign-free prohibitions " +
    "(hydrant 15 ft, bus stop, crosswalk, driveway) may still apply. Read the curb.",
  unmatched_signs:
    "DOT publishes signs on this block but CurbCheck could not place them. Treat as unknown. " +
    "Read the posted signs.",
};

/** The `no_data` wording for one span: gap-kind specific where the span says which. */
export function noDataExplanation(gapKind) {
  return NO_DATA_EXPLANATION[gapKind] || VERDICT_EXPLANATION.no_data;
}

/**
 * The sentence under each verdict chip in the detail sheet.
 *
 * `basis` splits the green verdict in two (UX_AUDIT P0-1): a sign that was read
 * and permits parking, versus no sign covering the window at all. The second is
 * absence of evidence and must never be worded, coloured, or scored as a
 * permission.
 */
export const BASIS_SENTENCE = {
  posted: "A posted sign permits parking for your whole window.",
  absence: "No posted sign covers this window. That is not a permission — read the curb.",
};

export const ILLEGAL_SENTENCE = "A posted sign prohibits parking for part or all of your window.";

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

// Pin mode is armed but no pin has been placed yet. Reusing the previous
// destination here would answer a question about a block the user has already
// said they are leaving, and every verdict on the page would be about the wrong
// curb without saying so.
export const PIN_MODE_NEEDS_A_CLICK =
  "Pin mode is on but no pin is placed. Tap the map to set the destination, or type an " +
  "address instead.";

export const PIN_MODE_PROMPT = "Tap the map to set your destination.";

// SPEC §11, "Emergency ASP suspensions (offline mode)".
export const ASP_SUSPENSION_CAVEAT =
  "Emergency ASP suspensions are not reflected. Same-day weather and parade suspensions are " +
  "only visible if the optional 311 live check is enabled, and it is off by default.";

// SPEC §17. index.html carries the same text; this is what the detail sheet
// prints, so the full notice is inside every verdict as well as at the top of
// the page (UX_AUDIT (f) 1).
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

// `/api/segment` splits the signs into the ones that produced the verdict and
// the rest of the block. The second group is collapsed, never deleted
// (UX_AUDIT P0-2 and (f) 4).
export const SIGN_NOT_ON_THIS_STRETCH =
  "These signs are posted on this block but do not govern this stretch. Read them anyway if " +
  "you are parking near them.";

export const GOVERNING_SIGNS_NOTE =
  "Quoted from NYC Open Data, byte for byte. Where the text disagrees with the reading below " +
  "it, the sign wins.";

// UX_AUDIT P2-1: "confidence" mixed parse with snap confidence and was never
// defined. It is now labelled, shown only when the server says it means
// something, and explained in words rather than in a tooltip.
export const CONFIDENCE_LABEL = "Data confidence";
export const CONFIDENCE_EXPLANATION =
  "How sure the software is that it read these signs and placed them on the right stretch — " +
  "not how sure it is that you can park.";

/** States. One sentence each, and each one ends with what to do next. */
export const EMPTY_RESULTS = (walkMinutes) =>
  `Nothing within a ${walkMinutes}-minute walk. Try a longer walk or another spot.`;

/**
 * The answer to "where do I park" when the answer is "not here".
 *
 * A Midtown search at noon comes back 0 legal / 226 illegal / 6 no data, and
 * the rail printed a "Results" heading over three collapsed group headers and
 * nothing else: the count pill said `0 legal` and no sentence anywhere said so.
 * The question the page exists to answer went unanswered in words.
 */
export const NO_LEGAL_NEARBY = (walkMinutes) =>
  `No legal stretch within a ${walkMinutes}-minute walk. Everything nearby is restricted, ` +
  `unreadable, or unsurveyed — the groups below are what there is.`;

/** The ranked list is the server's best `limit`, not every legal stretch. */
export const LEGAL_SUBSET_NOTE = (shown, total) =>
  `Showing the ${shown.toLocaleString("en-US")} best-ranked of ` +
  `${total.toLocaleString("en-US")} legal stretches.`;

export const OUTSIDE_COVERAGE = "CurbCheck covers Manhattan only.";

export const SERVER_UNREACHABLE =
  "CurbCheck can't reach its local server, so these results are gone. Check that " +
  "`curbcheck serve` is still running.";

export const SEARCH_PROGRESS = "Reading the signs near your destination…";

export const STALE_SEARCH_NOTE = "Walk radius or time changed — press Search to update.";

export const NEEDS_DESTINATION = "Type an address or a cross street, or drop a pin on the map.";

export const DROP_A_PIN = "Drop a pin instead";

export const NO_CANDIDATES = "No Manhattan match for that. Try a cross street, or drop a pin.";

/**
 * A written sentence for every error code the API can return (docs/API.md).
 *
 * UX_AUDIT P2-6: the status line printed framework and browser phrasing
 * ("Value error, the parking window must be 24 hours or less", "Failed to
 * fetch"). The server's own message is kept as a second line only where it
 * names something the user can act on.
 */
const ERROR_SENTENCE = {
  address_not_found: "No Manhattan address matches that. Try a cross street, or drop a pin.",
  outside_coverage: OUTSIDE_COVERAGE,
  validation_error: "That search does not add up. Check the date, the time, and the walk radius.",
  invalid_request: "That stretch of curb could not be looked up.",
  not_found: "That stretch of curb is no longer in the database. Search again.",
  database_unavailable: "There is no parking database yet. Run `curbcheck sync` to build one.",
  internal_error: "The server hit an error it could not explain. Try again.",
  network_error: SERVER_UNREACHABLE,
  timeout: "The server took too long to answer. Try a shorter walk radius.",
  bad_response: "Something other than CurbCheck answered on this port.",
  http_error: "The server refused that search.",
};

/** The sentence for an `ApiError`. Never the exception's own phrasing. */
export function errorSentence(code) {
  return ERROR_SENTENCE[code] || "Something went wrong. Try again.";
}

/** Window too long is the one validation error worth naming exactly. */
export const WINDOW_TOO_LONG = "A parking window can be at most 24 hours.";
export const WINDOW_BACKWARDS = "The end time has to be after the start time.";
