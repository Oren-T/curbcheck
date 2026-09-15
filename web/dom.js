/**
 * DOM construction helpers.
 *
 * Every string that reaches the page goes through `textContent` here. Sign text
 * is attacker-controlled (docs/API.md: the API does not sanitize, the frontend
 * escapes), so there is no code path in this app that builds markup from data.
 */

import { verdictChip } from "./format.js";

/**
 * Build an element. `text` is always assigned with textContent, never parsed.
 *
 * @param {string} tag
 * @param {{className?: string, text?: string|number|null, attrs?: Object}} [options]
 * @param {Array<Node|string|null|undefined>} [children]
 * @returns {HTMLElement}
 */
export function el(tag, options = {}, children = []) {
  const node = document.createElement(tag);
  if (options.className) {
    node.className = options.className;
  }
  if (options.text !== undefined && options.text !== null) {
    node.textContent = String(options.text);
  }
  if (options.attrs) {
    for (const [name, value] of Object.entries(options.attrs)) {
      if (value === false || value === null || value === undefined) {
        continue;
      }
      node.setAttribute(name, value === true ? "" : String(value));
    }
  }
  for (const child of children) {
    if (child === null || child === undefined) {
      continue;
    }
    node.append(child);
  }
  return node;
}

/** Remove every child of `node`. */
export function clear(node) {
  while (node.firstChild) {
    node.removeChild(node.firstChild);
  }
}

/** Replace the contents of `node` with `children`. */
export function replaceChildren(node, children) {
  clear(node);
  for (const child of children) {
    if (child !== null && child !== undefined) {
      node.append(child);
    }
  }
}

/** A `<dt>`/`<dd>` pair, skipped entirely when the value is missing. */
export function definition(term, value) {
  if (value === null || value === undefined || value === "") {
    return [];
  }
  return [el("dt", { text: term }), el("dd", { text: value })];
}

/**
 * The verdict chip: colour, symbol, written word, and the map's dash pattern.
 *
 * SPEC §11 requires the four states to stay visually distinct and UX_AUDIT (e) 1
 * requires four channels, so the pattern bar repeats the `line-dasharray` the
 * same verdict is drawn with on the map.
 */
export function verdictChipNode(result) {
  const chip = verdictChip(result);
  return el("span", { className: `verdict-chip ${chip.className}` }, [
    el("span", { className: "verdict-pattern", attrs: { "aria-hidden": "true" } }),
    el("span", {
      className: "verdict-symbol",
      text: chip.symbol,
      attrs: { "aria-hidden": "true" },
    }),
    el("span", { text: chip.label }),
  ]);
}

/**
 * A labelled disclosure: a `aria-expanded` button over a `hidden` body.
 *
 * Used for the verdict groups and for "Other signs on this block". Native
 * `<details>` is not used where the summary has to carry a live count and a
 * second line, because Safari's summary styling fights both.
 *
 * @param {{label: string, count?: number|null, expanded?: boolean,
 *          className?: string, toggleClassName?: string}} options
 * @returns {{root: HTMLElement, toggle: HTMLElement, body: HTMLElement}}
 */
export function collapsible({
  label,
  count = null,
  expanded = false,
  className = "group",
  toggleClassName = "group-toggle",
}) {
  const body = el("div", { className: "group-body", attrs: { hidden: !expanded } });
  const toggle = el("button", { className: toggleClassName, attrs: { type: "button" } }, [
    el("span", { text: label }),
    count === null ? null : el("span", { className: "group-count", text: `(${count})` }),
  ]);
  toggle.setAttribute("aria-expanded", String(expanded));
  toggle.addEventListener("click", () => {
    const open = toggle.getAttribute("aria-expanded") === "true";
    toggle.setAttribute("aria-expanded", String(!open));
    body.hidden = open;
  });
  return { root: el("div", { className }, [toggle, body]), toggle, body };
}
