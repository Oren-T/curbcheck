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
import {
  capacityLabel,
  confidenceText,
  priceLabel,
  streetLabelParts,
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

/**
 * Render the whole results region.
 *
 * @param {HTMLElement} container
 * @param {{results: Array, counts: Object, walkMinutes: number, shown: number,
 *          openGroups: Set<string>, onSelect: Function, onHover: Function,
 *          onShowMore: Function}} view
 * @returns {Map<string, HTMLElement>} the card element for each rendered result
 */
export function renderResults(container, view) {
  const cards = new Map();
  const legal = view.results.filter((result) => verdictKey(result.verdict) === "legal");
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
    nodes.push(groupSection(group, view, cards));
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
  const list = el("ol", { className: "results" });
  for (const result of members) {
    const card = resultCard(result, null, view);
    cards.set(result.reg_seg_id, card);
    list.append(el("li", {}, [card]));
  }
  body.append(list);
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
