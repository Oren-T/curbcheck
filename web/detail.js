/**
 * The detail sheet: the answer, why, what could still go wrong, and then the
 * raw sign text with the rule stack the software read out of it.
 *
 * The order is fixed and is the whole point. UX_AUDIT P0-2 found result #1's
 * sheet opening with `NO STANDING ANYTIME` — a sign that does not govern the
 * stretch — and burying the sentence that neutralises it in 12 px grey under a
 * nine-row table. So: verdict, then the caveats, then the signs that *produced*
 * the verdict, then everything else on the block behind a disclosure, then the
 * parsed reading, then the meter and the provenance.
 *
 * What the clarity pass changed is the first three lines, which now answer the
 * driver's three questions in order and each exactly once:
 *
 *   1. can I park here?      the chip and the headline
 *   2. why, in terms of the  the "why" line: the window in real days and
 *      sign on the pole and     times, and the rule that decided it, in plain
 *      the time I asked for?    English
 *   3. what could still      "Before you park", carrying only the caveats that
 *      go wrong?                add something the first two lines did not
 *
 * Before it, a `no_rule` stretch said the same thing four times — chip "NO RULE
 * IN EFFECT", headline "No posted rule covers this window", sentence "none is
 * in effect during your window", caveat "part of this window has no posted
 * rule" — and never once printed the window or the hours on the sign.
 *
 * SPEC §10 makes the verbatim `sign_description` mandatory here and UX_AUDIT
 * (f) 4 forbids deleting the non-governing signs — grouping them is allowed.
 */

import {
  ABSENCE_IS_NOT_A_PERMISSION,
  ALSO_MEANS,
  ASP_SUSPENSION_CAVEAT,
  CONFIDENCE_EXPLANATION,
  CONFIDENCE_LABEL,
  COULD_NOT_BE_READ,
  DISCLAIMER,
  GOVERNING_SIGNS_NOTE,
  HEADLINE,
  MEANS,
  META_RULE,
  NOTHING_IN_EFFECT,
  PANEL_STATES_NO_RULE,
  RESERVED_FOR_OTHERS,
  RULES_NOT_IN_EFFECT,
  RULE_APPLIES,
  RULE_COVERS_WINDOW,
  RULE_COVERS_WINDOW_WITH_LIMIT,
  SIGN_NOT_ON_THIS_STRETCH,
  UNPARSED_RULE,
  VERDICT_EXPLANATION,
  YOUR_WINDOW,
  engineReason,
  informativeCaveats,
  noDataExplanation,
  postedAt,
} from "./copy.js";
import { clear, collapsible, definition, el, verdictChipNode } from "./dom.js";
import {
  capacityLabel,
  confidenceShown,
  describeRegulation,
  durationAdjective,
  flagLabels,
  formatCapacity,
  formatConfidence,
  formatDays,
  formatDuration,
  formatHourRates,
  formatTimeRange,
  inEffectLabel,
  priceLabel,
  reasonLine,
  streetLabelParts,
  verdictKey,
  walkText,
  windowMinutes,
  windowSentence,
} from "./format.js";

/**
 * Render the sheet for one segment.
 *
 * @param {HTMLElement} container the `role="dialog"` element
 * @param {{result: Object|null, detail: Object|null, error: string|null,
 *          loading: boolean, label: {primary: string, secondary: string},
 *          window: {start: Date, end: Date}|null, onClose: Function}} view
 * @returns {HTMLElement} the close button, so the caller can move focus to it
 */
export function renderDetail(container, view) {
  clear(container);
  container.hidden = false;
  const close = el("button", {
    className: "detail-close",
    text: "Close",
    attrs: { type: "button" },
  });
  close.addEventListener("click", () => view.onClose());

  const scroll = el("div", { className: "detail-scroll" }, body(view));
  const label = view.label;
  container.append(
    el("div", { className: "detail-header" }, [
      el("div", { className: "detail-heading" }, [
        el("h2", {
          className: "detail-title",
          text: label.primary,
          attrs: { id: "detail-title" },
        }),
        el("p", {
          className: "detail-sub",
          text: label.secondary === "" ? "Stretch of curb" : label.secondary,
        }),
      ]),
      close,
    ]),
    scroll,
  );
  scroll.scrollTop = 0;
  return close;
}

export function hideDetail(container) {
  clear(container);
  container.hidden = true;
}

function body(view) {
  const result = view.result;
  const detail = view.detail;
  const nodes = [];

  if (result) {
    nodes.push(verdictBlock(result, detail, view.window));
    nodes.push(caveatBlock(result.caveats));
  }

  if (view.loading) {
    nodes.push(el("p", { className: "progress-text", text: "Reading the signs on this stretch…" }));
  }
  if (view.error) {
    nodes.push(
      el("div", { className: "notice-block notice-error" }, [el("p", { text: view.error })]),
    );
  }

  if (detail) {
    const signs = splitSigns(detail);
    const rules = rulesByDescription(detail.regulations);
    nodes.push(...governingSection(signs.governing, result, rules));
    nodes.push(...otherSignsSection(signs.other, result, rules));
    nodes.push(...readingSection(detail.regulations, signs));
    nodes.push(...meterSection(detail.meter_rates));
    nodes.push(...segmentSection(detail.segment, result));
  }

  // SPEC §17 in full, inside every verdict, so the strip at the top of the page
  // can be a summary without the notice ever being more than a scroll away.
  nodes.push(el("p", { className: "detail-fineprint", text: DISCLAIMER }));
  return nodes;
}

/** The chip, the practical answer, the window and the rule that decided it. */
function verdictBlock(result, detail, window) {
  const key = verdictKey(result.verdict);
  const price = priceLabel(result);
  const capacity = capacityLabel(result);
  const facts = el("dl", { className: "facts" }, [
    ...definition("Walk", `${walkText(result.walk_min)} from your destination`),
    ...definition("Cost for your window", price),
    ...definition("Capacity", capacity ? formatCapacity(result.capacity_cars) : null),
    ...definition(
      "Meter running",
      result.metered ? `${result.charged_minutes} min of the window` : null,
    ),
    ...definition("Meter zone", result.rate_label),
  ]);

  return el("div", { className: `detail-verdict detail-verdict-${key}` }, [
    verdictChipNode(result),
    el("p", { className: "detail-headline", text: HEADLINE[key] }),
    el("p", { className: "detail-why", text: whyLine(result, detail, window) }),
    facts,
  ]);
}

/**
 * The "why" line: the window in words, then the rule that decided it.
 *
 * This is the sentence the panel was missing. Every verdict is a claim about
 * two times and (usually) one rule, and the panel used to print neither: it
 * said "your window" without saying what the window was, and "a posted sign
 * prohibits parking" without saying which sign or what it says. The rule comes
 * from `/api/segment`'s `deciding` and `in_effect` (docs/API.md), so the rule
 * the panel names is the one the engine actually decided on.
 *
 * While `/api/segment` is in flight the line still opens with the window and
 * falls back to the engine's own reason, so the first three lines are never
 * blank and never move.
 */
function whyLine(result, detail, window) {
  const key = verdictKey(result.verdict);
  const parts = window ? [YOUR_WINDOW(windowSentence(window.start, window.end))] : [];

  if (key === "no_data") {
    parts.push(noDataExplanation(result.gap_kind));
    return parts.join(" ");
  }
  if (key === "ambiguous") {
    parts.push(VERDICT_EXPLANATION.ambiguous);
    return parts.join(" ");
  }

  const rules = readRules(detail);
  if (rules.length === 0) {
    parts.push(engineReason(reasonLine(result)));
    return parts.join(" ").trim();
  }
  if (key === "illegal") {
    parts.push(prohibitionSentence(rules, result));
    return parts.join(" ");
  }
  if (result.basis === "absence") {
    parts.push(absenceSentence(rules), ABSENCE_IS_NOT_A_PERMISSION);
    return parts.join(" ");
  }
  parts.push(permissionSentence(rules, result, window));
  return parts.join(" ");
}

/** The rules `/api/segment` parsed for this stretch, or [] before it has answered. */
function readRules(detail) {
  return detail && Array.isArray(detail.regulations) ? detail.regulations : [];
}

/** The one rule the engine's verdict rests on, or null when it named none. */
function decidingRule(rules) {
  return rules.find((entry) => entry.deciding === true) || null;
}

function prohibitionSentence(rules, result) {
  const rule = decidingRule(rules);
  if (!rule || !rule.regulation) {
    return engineReason(reasonLine(result));
  }
  const applies = RULE_APPLIES(describeRegulation(rule.regulation), rule.in_effect === "all");
  return rule.regulation.permitted ? `${applies} ${RESERVED_FOR_OTHERS}` : applies;
}

/**
 * Absence: name the rules that *are* posted, and say none of them is on.
 *
 * Naming them is the point. "No posted rule covers this window" left the driver
 * looking at a pole that says NO PARKING MONDAY-FRIDAY 8AM-6PM with no way to
 * connect the two; "the rule here — No parking, Mon–Fri 8 AM–6 PM — is not in
 * effect then" is the same fact tied to the thing in front of them.
 */
function absenceSentence(rules) {
  const sentences = [];
  for (const entry of rules) {
    if (unreadableReason(entry) !== null) {
      continue;
    }
    const text = describeRegulation(entry.regulation);
    if (text !== "" && !sentences.includes(text)) {
      sentences.push(text);
    }
  }
  return sentences.length === 0 ? NOTHING_IN_EFFECT : RULES_NOT_IN_EFFECT(sentences);
}

function permissionSentence(rules, result, window) {
  const rule = decidingRule(rules);
  if (!rule || !rule.regulation) {
    return engineReason(reasonLine(result));
  }
  const sentence = describeRegulation(rule.regulation);
  const limit = formatDuration(rule.regulation.max_duration_min);
  const stay = window ? durationAdjective(windowMinutes(window.start, window.end)) : "";
  if (limit === null || stay === "") {
    return RULE_COVERS_WINDOW(sentence);
  }
  return RULE_COVERS_WINDOW_WITH_LIMIT(sentence, limit, stay);
}

/**
 * Every caveat that adds something, always, directly under the verdict.
 *
 * A "legal" verdict with the caveats behind a disclosure triangle is the false
 * confidence CLAUDE.md bans and UX_AUDIT (f) 5 forbids, so this block is never
 * collapsed. What `informativeCaveats` drops is only the caveat that repeats
 * the verdict the "why" line has just explained — the box used to open with
 * "Part of this window has no posted rule; read the curb" under a chip and a
 * headline that both already said so, which taught the driver to skip the box
 * and with it the temporary-signage warning underneath.
 */
function caveatBlock(caveats) {
  const items = informativeCaveats(caveats);
  return el("section", { className: "caveat-block" }, [
    el("h3", { text: "Before you park" }),
    el(
      "ul",
      {},
      items.map((caveat) => el("li", { text: caveat })),
    ),
  ]);
}

/**
 * Split `/api/segment`'s signs into the ones that produced the verdict and the
 * rest of the block.
 *
 * The server sends `governing` and `other_on_block` as two top-level lists. A
 * server built before that split sends one flat `signs` array; rather than
 * render an empty sheet against it, the flat array is split on `is_regulation`,
 * which is the only signal the old shape carries.
 */
function splitSigns(detail) {
  const grouped = detail.signs && !Array.isArray(detail.signs) ? detail.signs : detail;
  if (Array.isArray(grouped.governing) || Array.isArray(grouped.other_on_block)) {
    return {
      governing: Array.isArray(grouped.governing) ? grouped.governing : [],
      other: Array.isArray(grouped.other_on_block) ? grouped.other_on_block : [],
    };
  }
  const flat = Array.isArray(detail.signs) ? detail.signs : [];
  return {
    governing: flat.filter((sign) => sign.is_regulation !== false),
    other: flat.filter((sign) => sign.is_regulation === false),
  };
}

/**
 * The parsed rules for this stretch, keyed by the sign text they came from.
 *
 * The description is what a `regulation` row and a `sign` row have in common
 * (docs/API.md), and it is also what makes six identical posts one quoted
 * block rather than six.
 */
function rulesByDescription(regulations) {
  const rules = new Map();
  for (const entry of Array.isArray(regulations) ? regulations : []) {
    const key = entry.raw_sign_description || "";
    if (!rules.has(key)) {
      rules.set(key, []);
    }
    rules.get(key).push(entry);
  }
  return rules;
}

/**
 * One block per distinct sign text, in curb order.
 *
 * Six posts of `NO PARKING MONDAY-FRIDAY 8AM-6PM` are one regulation and one
 * thing to read, and the panel used to quote each of them in its own bordered
 * block — the same 56 characters, six times, under a verdict that said the rule
 * was not in effect. Grouping is allowed; deleting is not (UX_AUDIT (f) 4), so
 * every post is still accounted for, in the line that says where they are.
 */
function signGroups(signs, rules = null) {
  const groups = new Map();
  for (const sign of signs) {
    const key = sign.sign_description || "";
    if (!groups.has(key)) {
      groups.set(key, []);
    }
    groups.get(key).push(sign);
  }
  const ordered = [...groups.values()];
  if (rules === null) {
    return ordered;
  }
  // The sign the verdict rests on leads. The rest keep curb order, which is how
  // you meet them walking the block. Reading "3 HMP COMMERCIAL VEHICLES ONLY —
  // not in effect for your window" first, under a verdict that turns on the
  // second sign down, is the P0-2 failure in miniature.
  const decides = (group) =>
    (rules.get(group[0].sign_description || "") || []).some((entry) => entry.deciding === true);
  return [...ordered.filter(decides), ...ordered.filter((group) => !decides(group))];
}

function governingSection(governing, result, rules) {
  const heading = el("h3", { text: "Signs governing this stretch" });
  if (governing.length === 0) {
    // UX_AUDIT P2-8: the §11 explanation is printed once, at the top, and this
    // section only says what is missing here.
    const unmatched = result && result.gap_kind === "unmatched_signs";
    return [
      el("section", {}, [
        heading,
        el("p", {
          className: "notice notice-no_data",
          text: unmatched
            ? "None. The signs DOT publishes for this block are listed below, unplaced."
            : "None. No sign in DOT's inventory sits on this stretch.",
        }),
      ]),
    ];
  }
  return [
    el("section", {}, [
      heading,
      el("p", { className: "group-note", text: GOVERNING_SIGNS_NOTE }),
      ...signGroups(governing, rules).map((group) => signCard(group, rules)),
    ]),
  ];
}

/**
 * The rest of the block, collapsed. UX_AUDIT P0-2: 9 of the 11 signs on result
 * #1 did not govern the stretch, and two of them read `NO STANDING ANYTIME`
 * directly under a green verdict.
 */
function otherSignsSection(other, result, rules) {
  if (other.length === 0) {
    return [];
  }
  const unmatched = result && result.gap_kind === "unmatched_signs";
  const { root, body: inner } = collapsible({
    label: unmatched
      ? "Signs DOT publishes here that could not be placed"
      : "Other signs on this block",
    count: other.length,
    expanded: unmatched,
    className: "group",
  });
  inner.append(
    el("p", { className: "group-note", text: SIGN_NOT_ON_THIS_STRETCH }),
    // No "applies to your window" line down here: these signs do not govern
    // this stretch, so the question the line answers is not being asked of
    // them, and answering it anyway is how a NO STANDING ANYTIME panel ends up
    // reading like a verdict on curb it has nothing to do with (P0-2).
    ...signGroups(other).map((group) => signCard(group, rules, { governing: false })),
  );
  return [el("section", {}, [el("h3", { text: "Elsewhere on this block" }), root])];
}

/**
 * One sign text, quoted verbatim, with what it means underneath.
 *
 * The verbatim text comes first and is untouched: it is untrusted (docs/API.md)
 * and is assigned with textContent into a monospace block that wraps and is
 * never truncated, so the user sees exactly the bytes DOT published, arrows and
 * all (SPEC §11). Everything below it is the software's reading of those bytes,
 * and is labelled as such — "Means:", never a second quotation.
 *
 * `group` is every post carrying this same text on this stretch.
 */
function signCard(group, rules, { governing = true } = {}) {
  const sign = group[0];
  const description = sign.sign_description || "";
  const codes = [...new Set(group.map((post) => post.sign_code).filter(Boolean))];
  const orders = [...new Set(group.map((post) => post.order_number).filter(Boolean))];
  const meta = [
    codes.length === 0 ? null : `${codes.length === 1 ? "Code" : "Codes"} ${codes.join(", ")}`,
    orders.length === 0 ? null : `${orders.length === 1 ? "Order" : "Orders"} ${orders.join(", ")}`,
    postedAt(group.map((post) => post.distance_ft)),
    sign.arrow && sign.arrow !== "none" ? `Arrow ${sign.arrow}` : null,
    sign.parse_method ? `Read by ${sign.parse_method}` : null,
  ].filter((entry) => entry !== null);

  return el("div", { className: "sign-card" }, [
    el("span", { className: "as-posted-eyebrow", text: "As posted" }),
    el("pre", { className: "as-posted", text: description }),
    ...signReadings(rules.get(description) || [], governing),
    meta.length === 0
      ? null
      : el(
          "p",
          { className: "sign-meta" },
          meta.map((entry) => el("span", { text: entry })),
        ),
  ]);
}

/**
 * The one-line plain-English reading under a quoted sign, and whether it is on.
 *
 * "Means: no parking, Mon–Fri 8 AM–6 PM · Not in effect for your window" is the
 * whole of what a driver needs from a sign they have already read on the pole:
 * a check that the software read the same thing, and the connection between it
 * and the time they asked about. The field tables stay in the audit section.
 */
function signReadings(entries, governing) {
  return entries.map((entry, index) => {
    const unreadable = unreadableReason(entry);
    if (unreadable !== null) {
      return el("p", { className: "sign-reading sign-reading-unread", text: unreadable });
    }
    const sentence = describeRegulation(entry.regulation);
    const applies = governing ? inEffectLabel(entry.in_effect) : null;
    const reading = index === 0 ? MEANS(lowerFirst(sentence)) : ALSO_MEANS(lowerFirst(sentence));
    return el("p", {
      className: `sign-reading${applies === null ? "" : ` sign-reading-${entry.in_effect}`}`,
      text: applies === null ? reading : `${reading} · ${applies}`,
    });
  });
}

/** "No parking, Mon–Fri 8 AM–6 PM" reads as a quotation after "Means:" unless lowered. */
function lowerFirst(sentence) {
  return sentence.charAt(0).toLowerCase() + sentence.slice(1);
}

/**
 * Why this rule cannot be stated in English, or null when it can.
 *
 * Two cases, and neither may be rendered as a sentence about the curb: a sign
 * the parser failed on (D13), whose `regulation` is a placeholder, and a meta
 * panel, whose `regulation` is a placeholder *and* whose meaning depends on the
 * sign it modifies.
 */
function unreadableReason(entry) {
  if (entry.parse_method === "unparsed" || !entry.regulation) {
    return `${COULD_NOT_BE_READ} — ${UNPARSED_RULE}`;
  }
  return entry.regulation.flags && entry.regulation.flags.meta ? META_RULE : null;
}

/**
 * The audit trail: every parsed field of every rule, behind a disclosure.
 *
 * The plain sentence each rule reduces to is now printed under the sign it came
 * from, where the driver needs it; what is left here is the machine reading —
 * days masks, arrows, parse confidence — which is what DESIGN_DIRECTION §3
 * lists as progressive disclosure. It opens itself whenever a sign on this
 * stretch could not be read, because "the software could not read this sign" is
 * never allowed to be a thing you have to click for (UX_AUDIT (f) 5).
 *
 * `regulations[].sign_id` is what makes the grouping honest: matching on the
 * description text alone merged two different posts that happen to carry the
 * same words.
 */
function readingSection(regulations, signs) {
  const rules = Array.isArray(regulations) ? regulations : [];
  if (rules.length === 0) {
    return [];
  }
  const bySign = new Map();
  const orphans = [];
  for (const rule of rules) {
    if (!rule.sign_id) {
      orphans.push(rule);
      continue;
    }
    if (!bySign.has(rule.sign_id)) {
      bySign.set(rule.sign_id, []);
    }
    bySign.get(rule.sign_id).push(rule);
  }

  const known = [...signs.governing, ...signs.other];
  const blocks = [];
  for (const [signId, group] of bySign) {
    const sign = known.find((candidate) => candidate.sign_id === signId);
    blocks.push(
      el("div", { className: "sign-card" }, [
        el("h4", { text: signHeading(sign, group.length) }),
        ...group.map((rule) => ruleBlock(rule)),
      ]),
    );
  }
  if (orphans.length > 0) {
    blocks.push(
      el("div", { className: "sign-card" }, [
        el("h4", { text: "Rules with no matching sign row" }),
        ...orphans.map((rule) => ruleBlock(rule)),
      ]),
    );
  }

  const nonRegulation = known.filter((sign) => sign.is_regulation === false);
  if (nonRegulation.length > 0) {
    blocks.push(el("p", { className: "notice", text: PANEL_STATES_NO_RULE }));
  }

  const unreadable = rules.some((rule) => rule.parse_method === "unparsed");
  const { root, body: inner } = collapsible({
    label: "Every parsed field",
    count: rules.length,
    expanded: unreadable,
    className: "group",
  });
  inner.append(...blocks);
  return [el("section", {}, [el("h3", { text: "What the software read" }), root])];
}

function signHeading(sign, ruleCount) {
  const name = sign && sign.sign_code ? `Sign ${sign.sign_code}` : "One sign";
  return ruleCount === 1 ? name : `${name} — ${ruleCount} rules on one sign`;
}

/** One parsed rule in plain English, with every field spelled out beneath it. */
function ruleBlock(entry) {
  const regulation = entry.regulation;
  if (entry.parse_method === "unparsed" || !regulation) {
    return el("div", { className: "rule" }, [
      el("p", { className: "notice notice-ambiguous", text: UNPARSED_RULE }),
    ]);
  }
  const unreadable = unreadableReason(entry);
  return el("div", { className: "rule" }, [
    unreadable === null
      ? el("p", { className: "rule-summary", text: describeRegulation(regulation) })
      : el("p", { className: "notice notice-ambiguous", text: unreadable }),
    el("dl", { className: "facts" }, [
      ...definition("Days", formatDays(regulation.days)),
      ...definition("Times", formatTimeRange(regulation.time_from, regulation.time_to)),
      ...definition("Action", regulation.action),
      ...definition("Permitted", regulation.permitted ? "yes" : "no"),
      ...definition(
        "Vehicles",
        regulation.exclusive ? `${regulation.vehicle_class} only` : regulation.vehicle_class,
      ),
      ...definition("Metered", regulation.metered ? "yes" : "no"),
      ...definition(
        "Maximum stay",
        formatDuration(regulation.max_duration_min) || "no posted limit",
      ),
      ...definition("Flags", flagLabels(regulation.flags).join(", ") || "none"),
      ...definition("Arrow", regulation.arrow),
      ...definition(
        "Parse confidence",
        entry.parse_confidence === null || entry.parse_confidence === undefined
          ? null
          : formatConfidence(entry.parse_confidence),
      ),
    ]),
  ]);
}

function meterSection(rates) {
  if (!Array.isArray(rates) || rates.length === 0) {
    return [];
  }
  return [
    el("section", {}, [
      el("h3", { text: rates.length === 1 ? "Meter rate" : "Meter rates (more than one zone)" }),
      ...rates.map((rate) =>
        el("div", { className: "sign-card" }, [
          el("dl", { className: "facts" }, [
            ...definition("Zone", rate.rate_label),
            ...definition("Rate", formatHourRates(rate.hour_rates)),
            ...definition("Maximum session", formatDuration(rate.max_session_min)),
            ...definition("Source", rate.source),
            ...definition("Blockface", rate.blockface_id),
            ...definition("Side", rate.side),
          ]),
        ]),
      ),
    ]),
  ];
}

/** Provenance last: ids and raw confidences are audit material, not a decision. */
function segmentSection(segment, result) {
  if (!segment) {
    return [];
  }
  return [
    el("section", {}, [
      el("h3", { text: "This stretch of curb" }),
      el("dl", { className: "facts" }, [
        ...definition("Street", segment.street_name),
        ...definition("Side", segment.side),
        ...definition(
          "Length",
          segment.length_ft === null || segment.length_ft === undefined
            ? null
            : `${Math.round(segment.length_ft)} ft`,
        ),
        ...definition("Capacity", formatCapacity(segment.capacity_cars)),
        ...definition(
          "Snap confidence",
          segment.confidence ? formatConfidence(segment.confidence) : null,
        ),
        // UX_AUDIT P2-1: a percentage is audit material and belongs with the
        // other audit numbers, not in the block a driver reads to decide. Next
        // to the "why" line it read as confidence that you can park, which is
        // the one thing it does not measure.
        ...(confidenceShown(result)
          ? definition(
              CONFIDENCE_LABEL,
              `${formatConfidence(result.confidence)} — ${CONFIDENCE_EXPLANATION}`,
            )
          : []),
        ...definition("Segment id", result ? result.reg_seg_id : segment.reg_seg_id),
      ]),
      el("p", { className: "group-note", text: ASP_SUSPENSION_CAVEAT }),
    ]),
  ];
}

/**
 * The street label for a segment as its two display lines, best source first: the label the search
 * result already carries, then the segment's own street name, then the streets
 * the signs name, then the opaque id (SPEC §10 wants a human label, but never
 * one that is invented).
 */
export function streetLabel(result, detail) {
  if (result && result.street_name) {
    return streetLabelParts(result.street_name);
  }
  const segment = detail && detail.segment ? detail.segment : null;
  if (segment && segment.street_name) {
    const side = segment.side ? `, ${segment.side} side` : "";
    return streetLabelParts(`${segment.street_name}${side}`);
  }
  return { primary: result ? result.reg_seg_id : "Segment", secondary: "" };
}
