/**
 * The result list: a short ranked legal shortlist, then the other three
 * verdicts as collapsed groups with honest counts.
 *
 * UX_AUDIT P1-1 measured the old list at 590 near-identical cards in one
 * 88,253 px column, ranked on tenths of a minute. What changed here is what is
 * *listed*, never what is drawn: the map still gets every span the server
 * returned (P1-2), and the group headings still come from the server's
 * `counts`, so a capped response says so instead of quietly shrinking the
 * neighbourhood (UX_AUDIT (f) 7).
 *
 * A card is a `<button>` inside an `<ol>`: the list is the keyboard equivalent
 * of the map, and every drawn span is reachable from it.
 */

import { EMPTY_RESULTS, SEARCH_PROGRESS } from "./copy.js";
import { collapsible, el, replaceChildren, verdictChipNode } from "./dom.js";
import { VERDICT_ORDER, swatchBackground } from "./verdicts.js";
import {
  capacityLabel,
  confidenceText,
  priceLabel,
  streetLabelParts,
  verdictInfo,
  verdictKey,
  walkText,
} from "./format.js";

const SHORTLIST_STEP = 12;

const GROUPS = [
  { key: "ambiguous", label: "Ambiguous" },
  { key: "illegal", label: "Illegal" },
  { key: "no_data", label: "No data" },
];

export { SHORTLIST_STEP };

const PILL_SWATCH_HEIGHT = 5;

/**
 * The four count pills: what is in the radius, and the filter for it.
 *
 * Two jobs, one control (DESIGN_DIRECTION §3). The numbers are the server's
 * `counts`, measured over everything in radius before either cap, never
 * `results.length` (UX_AUDIT (f) 7) — so a pressed-out pill changes what is
 * *drawn*, never what is *counted*, and the count of the verdict you just hid
 * stays on screen next to your thumb.
 *
 * It replaces a 44-word sentence and two caveat paragraphs that pushed the
 * first result below the fold.
 *
 * @param {HTMLElement} container
 * @param {{counts: Object|null, visible: Set<string>, onToggle: Function}} view
 */
export function renderStats(container, view) {
  if (!view.counts) {
    replaceChildren(container, []);
    return;
  }
  const pills = VERDICT_ORDER.map((verdict) => {
    const shown = view.visible.has(verdict);
    const swatch = el("span", { className: "pill-swatch", attrs: { "aria-hidden": "true" } });
    swatch.style.background = swatchBackground(verdict, PILL_SWATCH_HEIGHT);
    const count = typeof view.counts[verdict] === "number" ? view.counts[verdict] : 0;
    const pill = el(
      "button",
      {
        className: `stat-pill stat-${verdict}`,
        attrs: {
          type: "button",
          role: "switch",
          "aria-checked": String(shown),
          title: shown ? "Hide these on the map" : "Show these on the map",
        },
      },
      [
        swatch,
        el("span", { className: "pill-count", text: count.toLocaleString("en-US") }),
        el("span", { className: "pill-word", text: verdictInfo(verdict).label.toLowerCase() }),
      ],
    );
    pill.addEventListener("click", () => view.onToggle(verdict));
    return pill;
  });
  replaceChildren(container, pills);
}

/**
 * Re-mark the pills after a toggle without rebuilding them.
 *
 * Rebuilding would drop the keyboard focus of the pill that was just pressed,
 * which is the one place in this UI where focus is guaranteed to be.
 */
export function updateStats(container, visible) {
  for (const verdict of VERDICT_ORDER) {
    const pill = container.querySelector(`.stat-${verdict}`);
    if (!pill) {
      continue;
    }
    const shown = visible.has(verdict);
    pill.setAttribute("aria-checked", String(shown));
    pill.setAttribute("title", shown ? "Hide these on the map" : "Show these on the map");
  }
}

/**
 * Render the whole results region.
 *
 * @param {HTMLElement} container
 * @param {{results: Array, counts: Object, walkMinutes: number, shown: number,
 *          openGroups: Set<string>, visible: Set<string>, onSelect: Function,
 *          onHover: Function, onShowMore: Function}} view
 * @returns {Map<string, HTMLElement>} the card element for each rendered result
 */
export function renderResults(container, view) {
  const cards = new Map();
  const legal = view.visible.has("legal")
    ? view.results.filter((result) => verdictKey(result.verdict) === "legal")
    : [];
  const nodes = [];

  const shortlist = legal.slice(0, view.shown);
  const list = el("ol", { className: "results" });
  for (const [index, result] of shortlist.entries()) {
    const card = resultCard(result, index + 1, view);
    cards.set(result.reg_seg_id, card);
    list.append(el("li", {}, [card]));
  }
  nodes.push(list);

  if (legal.length > shortlist.length) {
    const more = el("button", {
      className: "ghost show-more",
      text: `Show ${Math.min(SHORTLIST_STEP, legal.length - shortlist.length)} more`,
      attrs: { type: "button" },
    });
    more.addEventListener("click", () => view.onShowMore());
    nodes.push(more);
  }

  for (const group of GROUPS) {
    if (view.visible.has(group.key)) {
      nodes.push(groupSection(group, view, cards));
    }
  }

  replaceChildren(container, nodes);
  return cards;
}

/**
 * One collapsed verdict group. It is rendered even when empty: a missing
 * "No data (0)" heading and a hidden "No data (53)" heading look the same from
 * the outside, and only one of them is true.
 */
function groupSection(group, view, cards) {
  const members = view.results.filter((result) => verdictKey(result.verdict) === group.key);
  const total =
    view.counts && typeof view.counts[group.key] === "number"
      ? view.counts[group.key]
      : members.length;
  const expanded = view.openGroups.has(group.key);
  const { root, toggle, body } = collapsible({ label: group.label, count: total, expanded });

  toggle.addEventListener("click", () => {
    if (toggle.getAttribute("aria-expanded") === "true") {
      view.openGroups.add(group.key);
    } else {
      view.openGroups.delete(group.key);
    }
  });

  if (members.length === 0) {
    body.append(
      el("p", {
        className: "group-note",
        text:
          total === 0
            ? "None within this walk radius."
            : "None of these were sent with this answer.",
      }),
    );
    return root;
  }

  if (members.length < total) {
    body.append(
      el("p", {
        className: "group-note",
        text: `${members.length} of ${total} drawn, nearest first.`,
      }),
    );
  }
  // Rendered on expand, not on load: a dense search carries ~500 group members
  // and building 500 buttons nobody has asked to see costs a visible pause on
  // every re-rank (IMPLEMENTATION_NOTES §11). `fill` is idempotent, so the
  // second expand reuses what the first one built.
  const fill = () => {
    if (body.dataset.filled === "yes") {
      return;
    }
    const list = el("ol", { className: "results" });
    for (const result of members) {
      const card = resultCard(result, null, view);
      cards.set(result.reg_seg_id, card);
      list.append(el("li", {}, [card]));
    }
    body.append(list);
    body.dataset.filled = "yes";
    view.onGroupFilled();
  };
  toggle.addEventListener("click", () => {
    if (toggle.getAttribute("aria-expanded") === "true") {
      fill();
    }
  });
  if (expanded) {
    fill();
  }
  return root;
}

/**
 * One card. Rank is only ever printed on the ranked legal shortlist — numbering
 * an illegal span "#537" put it in the same sequence as a recommendation
 * (UX_AUDIT P1-1).
 */
function resultCard(result, rank, view) {
  const key = verdictKey(result.verdict);
  const absence = key === "legal" && result.basis === "absence";
  const price = priceLabel(result);
  const capacity = capacityLabel(result);
  const confidence = confidenceText(result);
  const street = streetLabelParts(result.street_name);

  const card = el(
    "button",
    {
      className: `card card-${key}${absence ? " card-absence" : ""}`,
      attrs: { type: "button", "aria-pressed": "false", "data-id": result.reg_seg_id },
    },
    [
      el("span", { className: "card-head" }, [
        rank === null ? null : el("span", { className: "card-rank", text: rank }),
        verdictChipNode(result),
      ]),
      el("span", { className: "card-street", text: street.primary }),
      street.secondary === ""
        ? null
        : el("span", { className: "card-cross", text: street.secondary }),
      el("span", { className: "card-metrics" }, [
        el("span", { className: "card-walk", text: walkText(result.walk_min) }, [
          el("span", { className: "card-unit", text: " walk" }),
        ]),
        price === null ? null : el("span", { className: "card-price", text: price }),
        capacity === null ? null : el("span", { className: "card-capacity", text: capacity }),
        confidence === null ? null : el("span", { className: "card-capacity", text: confidence }),
      ]),
      el("span", {
        className: `card-reason${absence ? " card-reason-absence" : ""}`,
        text: reasonText(result, absence),
      }),
    ],
  );
  card.addEventListener("click", () => view.onSelect(result.reg_seg_id));
  card.addEventListener("mouseenter", () => view.onHover(result.reg_seg_id));
  card.addEventListener("mouseleave", () => view.onHover(null));
  card.addEventListener("focus", () => view.onHover(result.reg_seg_id));
  card.addEventListener("blur", () => view.onHover(null));
  return card;
}

/**
 * The reason sentence, prefixed on a legal-by-absence span so the line cannot
 * read as a permission — unless the engine's own sentence already says it
 * (DESIGN_DIRECTION §5).
 */
function reasonText(result, absence) {
  const reason = result.reason || "";
  if (!absence || /no posted rule/i.test(reason)) {
    return reason;
  }
  return `No posted rule — ${reason}`;
}

/** Mark one card as the selected one and leave the rest unselected. */
export function markSelected(cards, regSegId) {
  for (const [id, card] of cards) {
    card.setAttribute("aria-pressed", String(id === regSegId));
  }
}

/**
 * The loading state: skeletons plus a sentence, because a 30-minute window took
 * 6.5 s in the audit and an empty panel for six seconds reads as a failure.
 */
export function renderSkeleton(container) {
  replaceChildren(container, [
    el("p", { className: "progress-text", text: SEARCH_PROGRESS }),
    el("div", { className: "results" }, [
      el("div", { className: "skeleton-card" }),
      el("div", { className: "skeleton-card" }),
      el("div", { className: "skeleton-card" }),
    ]),
  ]);
}

/** Nothing in radius, but the server answered: a card, not an error. */
export function renderEmpty(container, walkMinutes) {
  replaceChildren(container, [
    el("div", { className: "notice-block notice-empty" }, [
      el("p", { text: EMPTY_RESULTS(walkMinutes) }),
    ]),
  ]);
}
