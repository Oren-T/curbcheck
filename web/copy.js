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
 * The practical answer, in the driver's words, one line per verdict.
 *
 * This is the whole of the second line of the panel. It used to be three
 * lines — a chip, a restated verdict, and a sentence about what the verdict
 * rested on — which is why the owner read a screen that said "no rule" four
 * times and still could not tell whether to park. What the verdict rests on is
 * now the *third* line, and it names the sign and the window instead of
 * describing the state of the software.
 */
export const HEADLINE = {
  legal: "You can park here for your window",
  illegal: "You can't park here for your window",
  // The two uncertain states name the app, not the curb: the honest practical
  // answer is that CurbCheck cannot tell you, and the driver has to. They then
  // differ in *why*, which is the only thing that changes what to do next:
  // `ambiguous` has signs to go and read, `no_data` has none in the inventory
  // at all. Echoing the chip ("Unclear — read the signs", "No data for this
  // stretch") spent the line saying the chip again.
  ambiguous: "CurbCheck can't tell — the signs here don't add up",
  no_data: "CurbCheck can't tell — there is no sign data here",
};

/* ---- The "why" line: the window, and the rule that decided it ----------- */

/** "Your window: Sat Sep 19, 2:00–4:00 PM." — said once, at the top. */
export const YOUR_WINDOW = (sentence) => `Your window: ${sentence}.`;

/**
 * Absence: the rules that are posted here, and the fact that none is on.
 *
 * SPEC §11 and UX_AUDIT P0-1: absence of a rule is not a permission from a
 * sign. That sentence belongs here, once, and nowhere else — the caveat that
 * used to repeat it below is dropped by `NOT_ADDED_BY_A_CAVEAT`.
 */
export const RULES_NOT_IN_EFFECT = (rules) =>
  rules.length === 1
    ? `The rule here — ${rules[0]} — is not in effect then.`
    : `The rules here — ${rules.join("; ")} — are not in effect then.`;

export const ABSENCE_IS_NOT_A_PERMISSION = "That is not a permission from a sign — read the curb.";

/** Absence with nothing parsed to name. Rare: a stack with rules the panel has not loaded. */
export const NOTHING_IN_EFFECT = "No posted rule is in effect then.";

/** Illegal: the prohibition that bit, and how much of the window it takes. */
export const RULE_APPLIES = (rule, whole) =>
  whole ? `${rule} applies for all of it.` : `${rule} applies for part of it.`;

// A curb reserved for another class reads as a *permission* in the data, and
// "Standing for trucks only, Mon–Sat 8 AM–7 PM applies for all of it" never
// says the thing that matters: that the permission is not yours.
export const RESERVED_FOR_OTHERS = "A passenger car is not in that class.";

/** Posted permission, with and without a time limit to check the stay against. */
export const RULE_COVERS_WINDOW = (rule) => `${rule} covers your window.`;
export const RULE_COVERS_WINDOW_WITH_LIMIT = (rule, limit, stay) =>
  `${rule} covers your window; the posted limit (${limit}) covers your ${stay} stay.`;

/** The fallback while `/api/segment` is still in flight: the engine's own reason. */
export const engineReason = (reason) =>
  reason === "" ? "" : `${reason.charAt(0).toUpperCase()}${reason.slice(1)}.`;

/**
 * Caveats that only restate the verdict the panel has already explained.
 *
 * Verbatim from `curbcheck/engine/resolve.py` (`ABSENCE_CAVEAT`,
 * `NO_DATA_CAVEAT`); `tests/test_web_static.py` fails if the two drift apart.
 * Every other caveat adds something the verdict does not say — temporary
 * signage, a school-day assumption, snow, a suspension, a meter that is not
 * charging, a missing calendar — and none of those is ever filtered.
 */
export const NOT_ADDED_BY_A_CAVEAT = new Set([
  "No posted rule is in effect during this window; read the curb.",
  "NYC DOT lists no signs on this stretch; unknown is not the same as unrestricted.",
  "DOT publishes signs for this blockface that CurbCheck could not place; read the posted signs.",
]);

/** The caveats worth a driver's attention: everything the "why" line has not said. */
export function informativeCaveats(caveats) {
  const items = Array.isArray(caveats) ? caveats : [];
  const kept = items.filter((caveat) => !NOT_ADDED_BY_A_CAVEAT.has(caveat));
  return kept.length > 0 ? kept : [TEMPORARY_SIGNAGE_CAVEAT];
}

/* ---- The quoted signs --------------------------------------------------- */

/** Under each verbatim sign: what the software read out of it. */
export const MEANS = (sentence) => `Means: ${sentence}`;
export const ALSO_MEANS = (sentence) => `and: ${sentence}`;
export const COULD_NOT_BE_READ = "Could not be read";

/** Where the identical panels on this stretch are posted, in curb order. */
export function postedAt(distancesFt) {
  const feet = distancesFt.filter((value) => typeof value === "number").sort((a, b) => a - b);
  if (feet.length === 0) {
    return null;
  }
  if (feet.length === 1) {
    return `posted ${Math.round(feet[0])} ft from the corner`;
  }
  if (feet.length > 3) {
    return (
      `${feet.length} posts, ${Math.round(feet[0])}–${Math.round(feet[feet.length - 1])} ft ` +
      "from the corner"
    );
  }
  const rounded = feet.map((value) => `${Math.round(value)} ft`);
  const last = rounded.pop();
  return `posted at ${rounded.join(", ")} and ${last} from the corner`;
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

// A meta panel ("METERS ARE NOT IN EFFECT ABOVE TIMES") changes the sign above
// it rather than stating a rule of its own. The parser records it as a rule
// with the `meta` flag set and placeholder hours, and rendering those hours as
// a sentence produced "Means: no parking at any time · Applies to your window"
// under a panel that says no such thing — a false statement about the curb,
// which is the one thing SPEC §11 exists to prevent. `resolve.ambiguity_reason`
// makes the whole stretch AMBIGUOUS for the same reason.
export const META_RULE =
  "This sign changes another sign on this stretch. The combination is not machine-readable — " +
  "read both.";

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
  `Nowhere you can park within a ${walkMinutes}-minute walk. Everything nearby is restricted, ` +
  `unreadable, or unsurveyed — the groups below are what there is.`;

/** The ranked list is the server's best `limit`, not every parkable stretch. */
export const LEGAL_SUBSET_NOTE = (shown, total) =>
  `Showing the ${shown.toLocaleString("en-US")} best-ranked of ` +
  `${total.toLocaleString("en-US")} stretches you can park on.`;

export const OUTSIDE_COVERAGE = "CurbCheck covers Manhattan only.";

/**
 * The second line under it.
 *
 * It used to read "your destination is outside the outlined area on the map",
 * and the outline is the coverage **bounding box** from `/api/health` — which
 * contains Hoboken, Jersey City and half of Queens. A pin dropped in Hoboken
 * was refused with a sentence saying it was outside a rectangle it was visibly
 * inside. The service-area test is 250 m from a street centerline, not the box
 * (`docs/API.md`, `docs/DECISIONS.md` D28), so the sentence names the street and
 * the box is a hint about where to look, not the claim.
 */
export const OUTSIDE_COVERAGE_DETAIL =
  "That point is not on a Manhattan street. Move it onto a block inside the dashed outline.";

export const SERVER_UNREACHABLE =
  "CurbCheck can't reach its local server, so these results are gone. Check that " +
  "`curbcheck serve` is still running.";

export const SEARCH_PROGRESS = "Reading the signs near your destination…";

export const STALE_SEARCH_NOTE = "Walk radius or time changed — press Search to update.";

export const NEEDS_DESTINATION = "Type an address or a cross street, or drop a pin on the map.";

export const DROP_A_PIN = "Drop a pin instead";

/**
 * What a screen reader hears when the suggestion list changes.
 *
 * The combobox keeps focus on the input (ARIA 1.2), so a list appearing under
 * it is silent until the user arrows into it — and the pin row means the list
 * is never empty, so "no matches" has to be said rather than inferred.
 */
export const CANDIDATE_COUNT = (found) =>
  found === 0
    ? "No matches. Drop a pin instead is the only suggestion."
    : `${found} suggestion${found === 1 ? "" : "s"}. Use the arrow keys to review.`;

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
  pack_unavailable:
    "The parking data pack could not be loaded. Reload the page; if it keeps failing, " +
    "the site's weekly data build is broken.",
  internal_error:
    "The server hit an unexpected error. Try again; the terminal running " +
    "`curbcheck serve` has the details.",
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
