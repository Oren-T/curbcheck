/**
 * The ranked result list: one card per curb span, in the order the API returned.
 *
 * A card is a button so the list is usable from the keyboard; the map is the
 * second way to reach the same segment, not the only one.
 */

import { streetLabel } from "./detail.js";
import { el, replaceChildren, verdictBadge } from "./dom.js";
import {
  formatCapacity,
  formatConfidence,
  formatWalkMinutes,
  moneyLabel,
  verdictKey,
} from "./format.js";

/**
 * Render every result into `list`.
 *
 * @param {HTMLElement} list
 * @param {Array<Object>} results
 * @param {{onSelect: Function, detailFor: Function}} handlers
 * @returns {Map<string, {card: HTMLElement, title: HTMLElement}>} card handles by id
 */
export function renderResults(list, results, { onSelect, detailFor }) {
  const cards = new Map();
  replaceChildren(
    list,
    results.map((result, index) => {
      const entry = resultCard(result, index + 1, detailFor(result.reg_seg_id), onSelect);
      cards.set(result.reg_seg_id, entry);
      return el("li", {}, [entry.card]);
    }),
  );
  return cards;
}

/** Mark one card as the selected one and leave the rest unselected. */
export function markSelected(cards, regSegId) {
  for (const [id, entry] of cards) {
    const selected = id === regSegId;
    entry.card.setAttribute("aria-pressed", String(selected));
    entry.card.classList.toggle("card-selected", selected);
  }
}

function resultCard(result, rank, detail, onSelect) {
  const title = el("span", { className: "card-title", text: streetLabel(result, detail) });
  const card = el(
    "button",
    {
      className: `card card-${verdictKey(result.verdict)}`,
      attrs: { type: "button", "aria-pressed": "false", "data-id": result.reg_seg_id },
    },
    [
      el("span", { className: "card-head" }, [
        el("span", { className: "card-rank", text: `#${rank}` }),
        verdictBadge(result.verdict),
      ]),
      title,
      el("span", { className: "card-facts" }, [
        el("span", { text: formatWalkMinutes(result.walk_min) }),
        el("span", { text: moneyLabel(result) }),
        el("span", { text: formatCapacity(result.capacity_cars) }),
        el("span", { text: formatConfidence(result.confidence) }),
      ]),
      el("span", { className: "card-reason", text: result.reason || "" }),
    ],
  );
  card.addEventListener("click", () => onSelect(result.reg_seg_id));
  return { card, title };
}
