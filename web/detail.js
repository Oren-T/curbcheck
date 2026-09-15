/**
 * The detail sheet: the verdict, every caveat, the raw sign text, and the rule
 * stack the software read out of it.
 *
 * The order is fixed and is the whole point of the rewrite. UX_AUDIT P0-2 found
 * result #1's sheet opening with `NO STANDING ANYTIME` — a sign that does not
 * govern the stretch — and burying the sentence that neutralises it in 12 px
 * grey under a nine-row table. So: verdict, then the caveats, then the signs
 * that *produced* the verdict, then everything else on the block behind a
 * disclosure, then the parsed reading, then the meter and the provenance.
 *
 * SPEC §10 makes the verbatim `sign_description` mandatory here and UX_AUDIT
 * (f) 4 forbids deleting the non-governing signs — grouping them is allowed.
 */

import {
  ASP_SUSPENSION_CAVEAT,
  CONFIDENCE_EXPLANATION,
  CONFIDENCE_LABEL,
  DISCLAIMER,
  GOVERNING_SIGNS_NOTE,
  ILLEGAL_SENTENCE,
  BASIS_SENTENCE,
  PANEL_STATES_NO_RULE,
  SIGN_NOT_ON_THIS_STRETCH,
  TEMPORARY_SIGNAGE_CAVEAT,
  UNPARSED_RULE,
  VERDICT_EXPLANATION,
  noDataExplanation,
} from "./copy.js";
import { clear, collapsible, definition, el, verdictChipNode } from "./dom.js";
import {
  capacityLabel,
  confidenceShown,
  describeRegulation,
  flagLabels,
  formatCapacity,
  formatConfidence,
  formatDays,
  formatDuration,
  formatHourRates,
  formatTimeRange,
  priceLabel,
  streetLabelParts,
  verdictKey,
  walkText,
} from "./format.js";

/**
 * Render the sheet for one segment.
 *
 * @param {HTMLElement} container the `role="dialog"` element
 * @param {{result: Object|null, detail: Object|null, error: string|null,
 *          loading: boolean, label: {primary: string, secondary: string},
 *          onClose: Function}} view
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
    nodes.push(verdictBlock(result));
    nodes.push(caveatBlock(result.caveats));
    const explanation = verdictExplanation(result);
    if (explanation) {
      nodes.push(
        el("p", {
          className: `notice notice-${verdictKey(result.verdict)}`,
          text: explanation,
        }),
      );
    }
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
    nodes.push(...governingSection(signs.governing, result));
    nodes.push(...otherSignsSection(signs.other, result));
    nodes.push(...readingSection(detail.regulations, signs));
    nodes.push(...meterSection(detail.meter_rates));
    nodes.push(...segmentSection(detail.segment, result));
  }

  // SPEC §17 in full, inside every verdict, so the strip at the top of the page
  // can be a summary without the notice ever being more than a scroll away.
  nodes.push(el("p", { className: "detail-fineprint", text: DISCLAIMER }));
  return nodes;
}

/** Verdict chip, the engine's sentence, and what the verdict rests on. */
function verdictBlock(result) {
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
    ...(confidenceShown(result)
      ? definition(
          CONFIDENCE_LABEL,
          `${formatConfidence(result.confidence)} — ${CONFIDENCE_EXPLANATION}`,
        )
      : []),
  ]);

  return el("div", { className: `detail-verdict detail-verdict-${key}` }, [
    verdictChipNode(result),
    el("p", { className: "detail-reason", text: result.reason || "" }),
    el("p", { className: "detail-basis", text: basisSentence(result) }),
    facts,
  ]);
}

/**
 * The sentence that says what the verdict rests on.
 *
 * `basis: "absence"` is the case UX_AUDIT P0-1 is about: no rule was found, and
 * the UI used to call that "Legal · 100% confidence". It gets the absence
 * sentence and no confidence figure at all.
 */
function basisSentence(result) {
  const key = verdictKey(result.verdict);
  if (key === "legal") {
    return BASIS_SENTENCE[result.basis] || BASIS_SENTENCE.absence;
  }
  if (key === "illegal") {
    return ILLEGAL_SENTENCE;
  }
  return "";
}

/** The §11 explanation for a verdict: for `no_data`, the one its gap kind earns. */
function verdictExplanation(result) {
  const key = verdictKey(result.verdict);
  return key === "no_data" ? noDataExplanation(result.gap_kind) : VERDICT_EXPLANATION[key];
}

/**
 * Every caveat, always, directly under the verdict. A "legal" verdict with the
 * caveats behind a disclosure triangle is the false confidence CLAUDE.md bans
 * and UX_AUDIT (f) 5 forbids.
 */
function caveatBlock(caveats) {
  const items = Array.isArray(caveats) && caveats.length > 0 ? caveats : [TEMPORARY_SIGNAGE_CAVEAT];
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

function governingSection(governing, result) {
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
      ...governing.map((sign) => signCard(sign)),
    ]),
  ];
}

/**
 * The rest of the block, collapsed. UX_AUDIT P0-2: 9 of the 11 signs on result
 * #1 did not govern the stretch, and two of them read `NO STANDING ANYTIME`
 * directly under a green verdict.
 */
function otherSignsSection(other, result) {
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
    ...other.map((sign) => signCard(sign)),
  );
  return [el("section", {}, [el("h3", { text: "Elsewhere on this block" }), root])];
}

/**
 * One sign, quoted verbatim.
 *
 * The text is untrusted (docs/API.md) and is assigned with textContent into a
 * monospace block that wraps and is never truncated, so the user sees exactly
 * the bytes DOT published, arrows and all.
 */
function signCard(sign) {
  const meta = [
    sign.sign_code ? `Code ${sign.sign_code}` : null,
    sign.order_number ? `Order ${sign.order_number}` : null,
    typeof sign.distance_ft === "number"
      ? `${Math.round(sign.distance_ft)} ft from the corner`
      : null,
    sign.arrow && sign.arrow !== "none" ? `Arrow ${sign.arrow}` : null,
    sign.parse_method ? `Read by ${sign.parse_method}` : null,
  ].filter((entry) => entry !== null);

  return el("div", { className: "sign-card" }, [
    el("span", { className: "as-posted-eyebrow", text: "As posted" }),
    el("pre", { className: "as-posted", text: sign.sign_description || "" }),
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
 * What the parser made of each sign, grouped under the sign it came from.
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

  return [el("section", {}, [el("h3", { text: "What the software read" }), ...blocks])];
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
  return el("div", { className: "rule" }, [
    el("p", { className: "rule-summary", text: describeRegulation(regulation) }),
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
