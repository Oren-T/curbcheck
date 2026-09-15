/**
 * The per-segment detail panel: the verdict, every caveat, the raw sign text,
 * and the rule stack the software read out of it.
 *
 * SPEC §10 makes the raw `sign_description` mandatory here, and CLAUDE.md
 * forbids showing a confident verdict without the caveats beside it — so the
 * caveat list is rendered before anything else the panel says, and an
 * ambiguous or no-data segment gets the §11 explanation in place of a reading.
 */

import {
  ASP_SUSPENSION_CAVEAT,
  TEMPORARY_SIGNAGE_CAVEAT,
  UNPARSED_RULE,
  VERDICT_EXPLANATION,
} from "./copy.js";
import { clear, definition, el, verdictBadge } from "./dom.js";
import {
  describeRegulation,
  flagLabels,
  formatCapacity,
  formatConfidence,
  formatDays,
  formatDuration,
  formatHourRates,
  moneyLabel,
  formatTimeRange,
  formatWalkMinutes,
  verdictKey,
} from "./format.js";

/**
 * Render the panel for one segment.
 *
 * @param {HTMLElement} container
 * @param {{result: Object|null, detail: Object|null, error: string|null,
 *          loading: boolean, label: string, onClose: Function}} view
 */
export function renderDetail(container, view) {
  clear(container);
  container.hidden = false;
  container.append(header(view), ...body(view));
  container.scrollTop = 0;
}

export function hideDetail(container) {
  clear(container);
  container.hidden = true;
}

function header(view) {
  const close = el("button", {
    className: "detail-close",
    text: "Close",
    attrs: { type: "button" },
  });
  close.addEventListener("click", () => view.onClose());
  return el("div", { className: "detail-header" }, [
    el("h2", { className: "detail-title", text: view.label }),
    close,
  ]);
}

function body(view) {
  const result = view.result;
  const detail = view.detail;
  const verdict = result ? result.verdict : null;
  const nodes = [];

  if (verdict) {
    nodes.push(
      el("div", { className: "detail-verdict" }, [
        verdictBadge(verdict),
        el("p", { className: "detail-reason", text: result.reason || "" }),
      ]),
    );
    nodes.push(caveatList(result.caveats));
    const explanation = VERDICT_EXPLANATION[verdictKey(verdict)];
    if (explanation) {
      nodes.push(el("p", { className: `notice notice-${verdictKey(verdict)}`, text: explanation }));
    }
    nodes.push(factList(result));
  }

  if (view.loading) {
    nodes.push(el("p", { className: "status", text: "Loading the signs on this stretch…" }));
  }
  if (view.error) {
    nodes.push(el("p", { className: "status status-error", text: view.error }));
  }
  if (!detail) {
    return nodes;
  }

  nodes.push(...signSection(detail, result));
  nodes.push(...meterSection(detail.meter_rates));
  nodes.push(...segmentSection(detail.segment));
  return nodes;
}

/**
 * Every caveat, always. A "legal" verdict with the caveats hidden behind a
 * disclosure triangle would be exactly the false confidence CLAUDE.md bans.
 */
function caveatList(caveats) {
  const items = Array.isArray(caveats) && caveats.length > 0 ? caveats : [TEMPORARY_SIGNAGE_CAVEAT];
  return el("section", { className: "detail-caveats" }, [
    el("h3", { text: "Before you park" }),
    el(
      "ul",
      {},
      items.map((caveat) => el("li", { text: caveat })),
    ),
  ]);
}

function factList(result) {
  return el("dl", { className: "facts" }, [
    ...definition("Walk", formatWalkMinutes(result.walk_min)),
    ...definition("Money", moneyLabel(result)),
    ...definition(
      "Meter running",
      result.metered ? `${result.charged_minutes} min of the window` : "not metered",
    ),
    ...definition("Capacity", formatCapacity(result.capacity_cars)),
    ...definition("Confidence", formatConfidence(result.confidence)),
    ...definition("Meter zone", result.rate_label),
    ...definition("Segment id", result.reg_seg_id),
  ]);
}

/** Signs first, then what the parser made of each one (SPEC §10). */
function signSection(detail, result) {
  const signs = Array.isArray(detail.signs) ? detail.signs : [];
  const regulations = Array.isArray(detail.regulations) ? detail.regulations : [];
  const searchSigns = result && Array.isArray(result.signs) ? result.signs : [];

  if (signs.length === 0) {
    return [
      el("section", { className: "detail-signs" }, [
        el("h3", { text: "Signs on this stretch" }),
        el("p", {
          className: "notice notice-no_data",
          text: VERDICT_EXPLANATION.no_data,
        }),
      ]),
    ];
  }

  const byDescription = groupRegulations(regulations);
  const used = new Set();
  const cards = signs.map((sign) => {
    const rules = byDescription.get(sign.sign_description) || [];
    rules.forEach((rule) => used.add(rule));
    const fallback = searchSigns.find((ref) => ref.sign_id === sign.sign_id) || {};
    return signCard(sign, rules, fallback);
  });

  const orphans = regulations.filter((rule) => !used.has(rule));
  if (orphans.length > 0) {
    cards.push(
      el("div", { className: "sign-card" }, [
        el("h4", { text: "Rules with no matching sign row" }),
        ...orphans.map((rule) => ruleBlock(rule)),
      ]),
    );
  }

  return [
    el("section", { className: "detail-signs" }, [
      el("h3", { text: "Signs on this stretch" }),
      el("p", {
        className: "hint",
        text: "The sign text below is quoted verbatim from NYC Open Data. Where it disagrees with the reading underneath it, the sign wins.",
      }),
      ...cards,
    ]),
  ];
}

function signCard(sign, rules, fallback) {
  const parseMethod = rules.length > 0 ? rules[0].parse_method : fallback.parse_method;
  const parseConfidence = rules.length > 0 ? rules[0].parse_confidence : fallback.parse_confidence;
  return el("div", { className: "sign-card" }, [
    // Untrusted text, rendered as text in a monospace block so the user sees
    // exactly the bytes DOT published, arrows and all.
    el("pre", { className: "sign-text", text: sign.sign_description }),
    el("dl", { className: "facts" }, [
      ...definition("Sign code", sign.sign_code),
      ...definition("Order number", sign.order_number),
      ...definition("Parse method", parseMethod),
      ...definition(
        "Parse confidence",
        parseConfidence === null || parseConfidence === undefined
          ? null
          : formatConfidence(parseConfidence),
      ),
      ...definition("On street", sign.on_street),
      ...definition(
        "Between",
        sign.from_street && sign.to_street ? `${sign.from_street} and ${sign.to_street}` : null,
      ),
      ...definition("Side", sign.side_of_street),
      ...definition(
        "From the corner",
        sign.distance_from_intersection === null || sign.distance_from_intersection === undefined
          ? null
          : `${Math.round(sign.distance_from_intersection)} ft`,
      ),
      ...definition(
        "Snap confidence",
        sign.snap_confidence === null || sign.snap_confidence === undefined
          ? null
          : formatConfidence(sign.snap_confidence),
      ),
      ...definition("Snap notes", sign.snap_notes),
    ]),
    ...(rules.length > 0
      ? [
          el("h4", {
            text:
              rules.length === 1
                ? "What the software read"
                : "What the software read (two rules on one sign)",
          }),
          ...rules.map(ruleBlock),
        ]
      : [el("p", { className: "notice notice-ambiguous", text: UNPARSED_RULE })]),
  ]);
}

/** One parsed rule in plain English, with every field spelled out beneath it. */
function ruleBlock(entry) {
  const regulation = entry.regulation;
  if (entry.parse_method === "unparsed" || !regulation) {
    return el("div", { className: "rule rule-unparsed" }, [
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
    ]),
  ]);
}

function meterSection(rates) {
  if (!Array.isArray(rates) || rates.length === 0) {
    return [];
  }
  return [
    el("section", { className: "detail-meters" }, [
      el("h3", { text: rates.length === 1 ? "Meter rate" : "Meter rates (more than one zone)" }),
      ...rates.map((rate) =>
        el("dl", { className: "facts" }, [
          ...definition("Zone", rate.rate_label),
          ...definition("Rate", formatHourRates(rate.hour_rates)),
          ...definition("Maximum session", formatDuration(rate.max_session_min)),
          ...definition("Blockface", rate.blockface_id),
          ...definition("Side", rate.side),
        ]),
      ),
    ]),
  ];
}

function segmentSection(segment) {
  if (!segment) {
    return [];
  }
  return [
    el("section", { className: "detail-segment" }, [
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
          segment.confidence === null || segment.confidence === undefined
            ? null
            : formatConfidence(segment.confidence),
        ),
      ]),
      el("p", { className: "hint", text: ASP_SUSPENSION_CAVEAT }),
    ]),
  ];
}

function groupRegulations(regulations) {
  const grouped = new Map();
  for (const entry of regulations) {
    const key = entry.raw_sign_description;
    if (!grouped.has(key)) {
      grouped.set(key, []);
    }
    grouped.get(key).push(entry);
  }
  return grouped;
}

/**
 * The street label for a segment, best source first: the API's own street name,
 * then the streets the signs name, then the opaque id (SPEC §10 wants a human
 * label, but never one that is invented).
 */
export function streetLabel(result, detail) {
  const segment = detail && detail.segment ? detail.segment : null;
  if (segment && segment.street_name) {
    return segment.side ? `${segment.street_name} (${segment.side} side)` : segment.street_name;
  }
  const signs = (detail && detail.signs) || (result && result.signs) || [];
  const sign = signs.find((candidate) => candidate.on_street);
  if (sign) {
    const between =
      sign.from_street && sign.to_street
        ? ` between ${sign.from_street} and ${sign.to_street}`
        : "";
    return `${sign.on_street}${between}`;
  }
  return result ? result.reg_seg_id : "Segment";
}
