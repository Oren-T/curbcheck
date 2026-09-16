/**
 * Collapse a stack of rules into one verdict, most-restrictive-wins, for a passenger car.
 *
 * DOT's stacking rule: where several signs cover the same curb, the most
 * restrictive governs; where a sign is missing, the remaining posted regulations
 * apply (SPEC §9.2). A segment is LEGAL only if parking is permitted for every
 * sub-interval of the window and any posted time limit covers the whole window.
 *
 * The asymmetry that drives the design: a false "illegal" costs the user a parking
 * spot, a false "legal" costs them a tow. Anything we cannot read confidently is
 * AMBIGUOUS (SPEC §8.6, §11). Port of `curbcheck/engine/resolve.py`.
 */

import { epochFromWall, formatWhen } from "../time.js";
import { Action, ParseMethod, appliesToPassenger } from "./model.js";
import { expandWindow, meterIsCharged, roundHalfEven, ruleIsActive } from "./window.js";

// A parse we trust less than this makes the whole segment ambiguous. SPEC §8.6
// sets the gold-set gate at 98% exact match for the grammar path; 0.8 is the
// per-rule floor below which we refuse to show a verdict.
export const AMBIGUITY_THRESHOLD = 0.8;

// SPEC §11: a missing calendar is a failure state the user has to see, because
// every holiday and street-cleaning verdict silently becomes a guess without it.
export const CALENDAR_MISSING_CAVEAT =
  "Holiday and street-cleaning suspension calendar is missing; holiday and ASP verdicts " +
  "may be wrong.";

// The reason on a span that is legal only because nothing is posted. Written as
// a sentence, not as a phrase like the other reasons, because the UI leads the
// card with it rather than appending it to a verdict word (UX audit P0-1).
export const ABSENCE_REASON = "No posted rule is in effect during this window";

// Two caveats that restate the verdict rather than adding to it. Named so the
// panel can drop the one its own "why" line already says, instead of printing
// the same fact as a chip, a headline, a reason and a caveat -- the duplication
// the owner read as "still not quite clear enough" (docs/ux/IMPLEMENTATION_NOTES.md
// §19). Any client that reads only `caveats` still gets them.
export const ABSENCE_CAVEAT = "No posted rule is in effect during this window; read the curb.";
export const PARTIAL_ABSENCE_CAVEAT =
  "Part of this window has no posted rule in effect; read the curb.";

// `regulation_segment.gap_kind`: why a placeholder span carries no rules at all.
export const NO_SIGNS = "no_signs";
export const UNMATCHED_SIGNS = "unmatched_signs";

// What grey means on this stretch. The two gaps are different facts about the
// curb and must not read the same: `unmatched_signs` is a blockface DOT does
// publish signs for and we could not place, and 332 of those 499 sides carry a
// NO STANDING/PARKING/STOPPING ANYTIME panel (docs/VALIDATION.md §5), so
// "no sign data on this block" is false there rather than merely vague.
export const NO_DATA_REASON = Object.freeze({
  [NO_SIGNS]: "NYC DOT lists no signs on this stretch",
  [UNMATCHED_SIGNS]: "Signs exist here that CurbCheck could not place",
});
// A stack that is empty without a `gap_kind` to explain it: a snapshot built
// before the column, or a span whose rules did not load. Neither sentence above
// can be claimed about it, so it says only what we know.
export const UNKNOWN_GAP_REASON = "No sign data for this stretch";

// The same two facts in the caveat list, which is what a client that reads only
// `caveats` sees. Each carries the consequence the reason has no room for.
export const NO_DATA_CAVEAT = Object.freeze({
  [NO_SIGNS]: "NYC DOT lists no signs on this stretch; unknown is not the same as unrestricted.",
  [UNMATCHED_SIGNS]:
    "DOT publishes signs for this blockface that CurbCheck could not place;" +
    " read the posted signs.",
});

// Least to most restrictive when prohibited, for picking what to report.
const PROHIBITION_RANK = Object.freeze({
  [Action.PARK]: 0,
  [Action.STAND]: 1,
  [Action.STOP]: 2,
});

const PROHIBITION_PHRASE = Object.freeze({
  [Action.PARK]: "no parking",
  [Action.STAND]: "no standing",
  [Action.STOP]: "no stopping",
});

/** The four states the UI must keep visually distinct (SPEC §11). */
export const Verdict = Object.freeze({
  LEGAL: "legal",
  ILLEGAL: "illegal",
  AMBIGUOUS: "ambiguous",
  NO_DATA: "no_data",
});

/**
 * Why a LEGAL verdict is legal: a rule that permits, or no rule at all.
 *
 * Absence really is a permission under 34 RCNY 4-08, but it is evidence of
 * nothing having been read, and the two must not reach the user as one thing:
 * every ranked result on the Upper East Side read "Legal - 100% confidence -
 * no posted rule covers this window", which is the app announcing that it
 * found nothing in its most confident voice (UX audit P0-1).
 */
export const VerdictBasis = Object.freeze({
  POSTED: "posted",
  ABSENCE: "absence",
});

/**
 * Most-restrictive-wins over the rules active in `interval`, read for a passenger car.
 *
 * Ambiguity is deliberately not considered here: it is a property of the whole
 * segment, handled in `evaluateSegment`.
 */
export function resolveInterval(stack, interval, calendar) {
  const active = stack.filter((item) => ruleIsActive(item.regulation, interval, calendar));
  const prohibitions = active.filter((item) => appliesToPassenger(item.regulation) === false);
  if (prohibitions.length > 0) {
    const worst = maxBy(prohibitions, (item) => PROHIBITION_RANK[item.regulation.action]);
    return outcome({
      interval,
      permitted: false,
      reason: `${prohibitionReason(worst.regulation)} ${when(interval)}`,
      prohibitingAction: worst.regulation.action,
      deciding: worst,
    });
  }

  const permits = active.filter(
    (item) =>
      item.regulation.action === Action.PARK && appliesToPassenger(item.regulation) === true,
  );
  if (permits.length > 0) {
    const limits = permits
      .map((item) => item.regulation.maxDurationMin)
      .filter((limit) => limit !== null);
    const metered = permits.some((item) => item.regulation.metered);
    const charged = permits.some((item) => meterIsCharged(item.regulation, interval, calendar));
    return outcome({
      interval,
      permitted: true,
      reason: metered ? "metered parking permitted" : "parking permitted",
      metered,
      meterCharged: charged,
      maxDurationMin: limits.length > 0 ? Math.min(...limits) : null,
      // The tightest limit is the one that can make the stay illegal, so
      // it is the permission worth quoting; an unlimited one only wins
      // when nothing on the stretch posts a limit at all.
      deciding: minBy(permits, limitRank),
    });
  }

  // 34 RCNY 4-08: where no posted sign applies, parking is allowed. We still
  // mark it, because "no rule is in force right now" reads differently to a
  // user than "a sign says you may park here".
  return outcome({
    interval,
    permitted: true,
    reason: "no posted rule is in force",
    byAbsence: true,
  });
}

/**
 * Verdict for one segment over [t1, t2): legal only if every sub-interval permits parking.
 *
 * `gapKind` is the span's `regulation_segment.gap_kind`, which is the only
 * thing that can say what an empty stack means. Without it the NO_DATA reason
 * claims nothing about the curb beyond our own ignorance.
 */
export function evaluateSegment(stack, t1, t2, calendar, { gapKind = null } = {}) {
  if (stack.length === 0) {
    return segmentVerdict({
      verdict: Verdict.NO_DATA,
      reason: gapReason(gapKind),
      caveats: [...calendarCaveats(calendar), ...gapCaveats(gapKind)],
      windowMinutes: windowMinutes(t1, t2),
      confidence: 0.0,
    });
  }

  const intervals = expandWindow(
    t1,
    t2,
    stack.map((item) => item.regulation),
  );
  const outcomes = intervals.map((interval) => resolveInterval(stack, interval, calendar));
  const totalMinutes = sum(outcomes.map((o) => o.interval.minutes));
  const chargedMinutes = sum(outcomes.filter((o) => o.meterCharged).map((o) => o.interval.minutes));
  const limits = outcomes.map((o) => o.maxDurationMin).filter((limit) => limit !== null);
  const maxDurationMin = limits.length > 0 ? Math.min(...limits) : null;
  // Only the minutes a posted limit is actually in force count against it. A
  // "2 HMP 8AM-7PM" sign says nothing about 7PM onwards, so a 18:00-21:00 stay
  // spends 60 of its 180 minutes under the limit. Summing them all rather than
  // taking the longest run keeps two separated limited stretches from each
  // getting a full allowance. D12(b) is unchanged: a rule in force on a day
  // its meter is not running still counts, because the rule is still in force.
  const limitedMinutes = sum(
    outcomes.filter((o) => o.maxDurationMin !== null).map((o) => o.interval.minutes),
  );
  const confidence = Math.min(...stack.map((item) => item.parseConfidence));
  const caveats = allCaveats(stack, outcomes, calendar);

  const decided = (verdict, reason, offending = null) =>
    segmentVerdict({
      verdict,
      reason,
      intervals: outcomes,
      caveats,
      windowMinutes: totalMinutes,
      chargedMinutes,
      metered: outcomes.some((o) => o.metered),
      maxDurationMin,
      confidence,
      firstOffending: offending,
    });

  const ambiguity = ambiguityReason(stack);
  if (ambiguity !== null) {
    return decided(Verdict.AMBIGUOUS, ambiguity);
  }

  const prohibited = outcomes.find((o) => !o.permitted) ?? null;
  if (prohibited !== null) {
    return decided(Verdict.ILLEGAL, prohibited.reason, prohibited);
  }

  if (maxDurationMin !== null && maxDurationMin < limitedMinutes) {
    const tooShort = outcomes.find((o) => o.maxDurationMin === maxDurationMin);
    return decided(
      Verdict.ILLEGAL,
      `posted limit of ${maxDurationMin} min is shorter than` +
        ` the ${limitedMinutes} min it is in force for`,
      tooShort,
    );
  }

  if (outcomes.every((o) => o.byAbsence)) {
    return decided(Verdict.LEGAL, ABSENCE_REASON);
  }
  return decided(Verdict.LEGAL, "parking permitted for the whole window");
}

/**
 * Why this stack cannot be read confidently, or null if it can.
 *
 * One unreadable sign poisons the whole segment: we cannot know whether the
 * sign we failed to read is the one that prohibits parking (SPEC §11).
 */
export function ambiguityReason(stack) {
  if (stack.some((item) => item.parseMethod === ParseMethod.UNPARSED)) {
    return "a sign on this block could not be read";
  }
  if (stack.some((item) => item.regulation.flags.meta)) {
    return "a sign here modifies another sign; the combination is not machine-readable";
  }
  if (stack.some((item) => item.parseConfidence < AMBIGUITY_THRESHOLD)) {
    return "a sign on this block was read with low confidence";
  }
  return null;
}

/**
 * What the stack says about one sub-interval of the window.
 *
 * `deciding` is the rule this outcome rests on -- the prohibition that won
 * most-restrictive-wins, or the permission that carries the tightest limit.
 * null when nothing was in force, which is what `byAbsence` means. It is
 * what lets the panel name the sign on the pole rather than paraphrase a
 * verdict: "No parking, Mon-Fri 8 AM-6 PM applies for all of it".
 */
function outcome({
  interval,
  permitted,
  reason,
  prohibitingAction = null,
  byAbsence = false,
  metered = false,
  meterCharged = false,
  maxDurationMin = null,
  deciding = null,
}) {
  return Object.freeze({
    interval,
    permitted,
    reason,
    prohibitingAction,
    byAbsence,
    metered,
    meterCharged,
    maxDurationMin,
    deciding,
  });
}

/**
 * The verdict for one regulation segment over the whole requested window.
 *
 * `basis`, `deciding` and `chargedIntervals` are Python properties; they are
 * computed once here because the objects they read are frozen.
 */
function segmentVerdict({
  verdict,
  reason,
  intervals = [],
  caveats = [],
  windowMinutes: minutes = 0,
  chargedMinutes = 0,
  metered = false,
  maxDurationMin = null,
  confidence = 1.0,
  firstOffending = null,
}) {
  const basis = basisOf(verdict, intervals);
  return Object.freeze({
    verdict,
    reason,
    intervals,
    caveats,
    windowMinutes: minutes,
    chargedMinutes,
    metered,
    maxDurationMin,
    confidence,
    firstOffending,
    basis,
    deciding: decidingOf(verdict, basis, intervals, firstOffending),
    chargedIntervals: intervals.filter((o) => o.meterCharged),
  });
}

/**
 * Why this span is legal, or null when the verdict is not LEGAL.
 *
 * ABSENCE only when *no* posted rule was in force for any part of the window;
 * one permitting rule anywhere in it makes the verdict POSTED, because there is
 * then a sign the user can go and read.
 */
function basisOf(verdict, intervals) {
  if (verdict !== Verdict.LEGAL) {
    return null;
  }
  if (intervals.length > 0 && intervals.every((o) => o.byAbsence)) {
    return VerdictBasis.ABSENCE;
  }
  return VerdictBasis.POSTED;
}

/**
 * The one rule a driver should be told about, or null when there is not one.
 *
 * ILLEGAL names the rule that bit first; a LEGAL verdict that rests on a posted
 * permission names that permission. AMBIGUOUS and NO_DATA name nothing, because
 * the point of both is that no rule was read with enough confidence to be
 * quoted, and legality by absence names nothing because there was no rule in
 * force to name.
 */
function decidingOf(verdict, basis, intervals, firstOffending) {
  if (verdict === Verdict.ILLEGAL) {
    return firstOffending === null ? null : firstOffending.deciding;
  }
  if (verdict !== Verdict.LEGAL || basis !== VerdictBasis.POSTED) {
    return null;
  }
  return intervals.find((o) => o.deciding !== null)?.deciding ?? null;
}

function allCaveats(stack, outcomes, calendar) {
  const caveats = calendarCaveats(calendar);
  const regulations = stack.map((item) => item.regulation);
  const dates = [...new Set(outcomes.map((o) => o.interval.date))].sort();

  if (regulations.some((reg) => reg.flags.snowEmergency)) {
    caveats.push(
      calendar.snowEmergency
        ? "Snow emergency declared; snow rules are in force."
        : "Snow emergency rule present; in force only when the city declares one.",
    );
  }
  // Only when a school-day rule actually overlaps the window: a 7AM-4PM school
  // rule says nothing about an evening search, so the caveat would mislead.
  const schoolRules = regulations.filter((reg) => reg.flags.schoolDays);
  const schoolRuleBites = schoolRules.some((reg) =>
    outcomes.some((o) => ruleIsActive(reg, o.interval, calendar)),
  );
  if (schoolRuleBites && dates.some((day) => !calendar.isKnownNonSchoolDay(day))) {
    caveats.push("School-day rule assumed active.");
  }
  if (regulations.some((reg) => reg.flags.temporary)) {
    caveats.push("A sign here marks itself temporary.");
  }
  if (
    regulations.some((reg) => reg.flags.streetCleaning) &&
    dates.some((day) => calendar.isAspSuspended(day))
  ) {
    caveats.push("Street cleaning is suspended on this date.");
  }
  if (outcomes.some((o) => o.metered && !o.meterCharged)) {
    caveats.push("Meters are not in effect for part of this window.");
  }
  if (outcomes.every((o) => o.byAbsence)) {
    caveats.push(ABSENCE_CAVEAT);
  } else if (outcomes.some((o) => o.byAbsence)) {
    caveats.push(PARTIAL_ABSENCE_CAVEAT);
  }
  return caveats;
}

/** The caveats that are true of every span in the database, not of this one. */
function calendarCaveats(calendar) {
  return calendar.calendarMissing ? [CALENDAR_MISSING_CAVEAT] : [];
}

// `gap_kind` comes out of the pack, so both lookups are own-property checks
// rather than plain indexing: a cell reading "constructor" must not find one.
function gapReason(gapKind) {
  const key = gapKind ?? "";
  return Object.hasOwn(NO_DATA_REASON, key) ? NO_DATA_REASON[key] : UNKNOWN_GAP_REASON;
}

/** What the caveat list says about a stretch with no rules on it. */
function gapCaveats(gapKind) {
  const key = gapKind ?? "";
  return Object.hasOwn(NO_DATA_CAVEAT, key) ? [NO_DATA_CAVEAT[key]] : [];
}

/** Sort key putting the shortest posted limit first, no limit last. */
function limitRank(item) {
  const limit = item.regulation.maxDurationMin;
  return limit === null ? 1 << 30 : limit;
}

function prohibitionReason(reg) {
  if (reg.permitted && reg.exclusive) {
    return `reserved for ${reg.vehicleClass} vehicles`;
  }
  return PROHIBITION_PHRASE[reg.action];
}

function when(interval) {
  return formatWhen(interval.start, interval.end);
}

function windowMinutes(t1, t2) {
  return Math.max(0, roundHalfEven((epochFromWall(t2) - epochFromWall(t1)) / 60_000));
}

/** Python's `max(..., key=)` keeps the first of equal keys; so does this. */
function maxBy(items, key) {
  let best = items[0];
  let bestKey = key(best);
  for (const item of items.slice(1)) {
    const itemKey = key(item);
    if (itemKey > bestKey) {
      best = item;
      bestKey = itemKey;
    }
  }
  return best;
}

/** Python's `min(..., key=)` keeps the first of equal keys; so does this. */
function minBy(items, key) {
  let best = items[0];
  let bestKey = key(best);
  for (const item of items.slice(1)) {
    const itemKey = key(item);
    if (itemKey < bestKey) {
      best = item;
      bestKey = itemKey;
    }
  }
  return best;
}

function sum(values) {
  return values.reduce((total, value) => total + value, 0);
}
