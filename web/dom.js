/**
 * DOM construction helpers.
 *
 * Every string that reaches the page goes through `textContent` here. Sign text
 * is attacker-controlled (docs/API.md: the API does not sanitize, the frontend
 * escapes), so there is no code path in this app that builds markup from data.
 */

import { verdictInfo, verdictKey } from "./format.js";

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
 * The verdict badge: colour, symbol, and the written word together.
 *
 * SPEC §11 requires the four states to stay visually distinct, and the word is
 * what makes them distinguishable without colour vision.
 */
export function verdictBadge(verdict) {
  const info = verdictInfo(verdict);
  return el("span", { className: `badge badge-${verdictKey(verdict)}` }, [
    el("span", { className: "badge-symbol", text: info.symbol, attrs: { "aria-hidden": "true" } }),
    el("span", { text: info.label }),
  ]);
}
