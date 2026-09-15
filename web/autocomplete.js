/**
 * The destination combobox: a debounced `/api/geocode` listbox under the input.
 *
 * ARIA 1.2 combobox with a listbox popup — the input keeps focus and owns
 * `aria-activedescendant`, so ↑/↓/Enter/Escape work without moving focus into
 * the list. UX_AUDIT P1-4 and P1-5: candidates used to appear only *after* a
 * failed search, and the geocoder silently picked candidate #1 and reported it
 * in a 12 px hint, so a house number that resolved to the nearest corner at
 * confidence 0.5 looked exactly like an exact match.
 *
 * The rows render whatever `/api/geocode` returns, in its order. The richer
 * local index that is coming later changes the list, not this file.
 */

import * as api from "./api.js";
import { DROP_A_PIN, NO_CANDIDATES } from "./copy.js";
import { el, replaceChildren } from "./dom.js";

const DEBOUNCE_MS = 150;
const MIN_QUERY_CHARS = 2;

/** A glyph per candidate kind. Decoration: the kind is also read out as text. */
const KIND_GLYPH = {
  address: "⌂",
  intersection: "✛",
  street: "—",
  zip: "▦",
  place: "★",
  pin: "◎",
};

/**
 * Wire an input and a listbox together.
 *
 * @param {{input: HTMLElement, list: HTMLElement, onPick: Function,
 *          onDropPin: Function, onError?: Function}} options
 * @returns {{close: Function, cancel: Function}}
 */
export function createAutocomplete({ input, list, onPick, onDropPin, onError = () => {} }) {
  let candidates = [];
  let rows = [];
  let activeIndex = -1;
  let timer = null;
  let inFlight = null;

  function close() {
    list.hidden = true;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    activeIndex = -1;
  }

  function cancel() {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
    if (inFlight) {
      inFlight.abort();
      inFlight = null;
    }
  }

  function setActive(index) {
    activeIndex = index;
    rows.forEach((row, position) => {
      row.setAttribute("aria-selected", String(position === index));
    });
    if (index < 0) {
      input.removeAttribute("aria-activedescendant");
      return;
    }
    input.setAttribute("aria-activedescendant", rows[index].id);
    rows[index].scrollIntoView({ block: "nearest" });
  }

  function choose(index) {
    const row = rows[index];
    if (!row) {
      return;
    }
    close();
    if (row.dataset.pin === "true") {
      onDropPin();
      return;
    }
    onPick(candidates[Number(row.dataset.index)]);
  }

  function render(found, emptyMessage) {
    candidates = found;
    rows = found.map((candidate, index) => optionRow(candidate, index));
    if (emptyMessage) {
      rows.unshift(el("li", { className: "ac-option ac-option-empty", text: emptyMessage }));
      rows[0].id = "ac-option-empty";
      rows[0].setAttribute("role", "option");
      rows[0].setAttribute("aria-disabled", "true");
      rows[0].setAttribute("aria-selected", "false");
    }
    rows.push(pinRow(rows.length));
    replaceChildren(list, rows);
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
    setActive(-1);
  }

  function optionRow(candidate, index) {
    const row = el(
      "li",
      {
        className: "ac-option",
        attrs: {
          role: "option",
          id: `ac-option-${index}`,
          "aria-selected": "false",
          "data-index": String(index),
        },
      },
      [
        el("span", {
          className: "ac-glyph",
          text: KIND_GLYPH[candidate.kind] || "•",
          attrs: { "aria-hidden": "true" },
        }),
        el("span", { className: "ac-body" }, [
          el("span", { className: "ac-label", text: candidate.label || "" }),
          candidate.secondary
            ? el("span", { className: "ac-secondary", text: candidate.secondary })
            : null,
        ]),
        el("span", { className: "visually-hidden", text: `, ${candidate.kind || "place"}` }),
      ],
    );
    row.addEventListener("mousedown", (event) => event.preventDefault());
    row.addEventListener("click", () => choose(rows.indexOf(row)));
    return row;
  }

  function pinRow(index) {
    const row = el(
      "li",
      {
        className: "ac-option ac-option-pin",
        attrs: {
          role: "option",
          id: `ac-option-pin-${index}`,
          "aria-selected": "false",
          "data-pin": "true",
        },
      },
      [
        el("span", { className: "ac-glyph", text: "◎", attrs: { "aria-hidden": "true" } }),
        el("span", { className: "ac-body" }, [
          el("span", { className: "ac-label", text: DROP_A_PIN }),
        ]),
      ],
    );
    row.addEventListener("mousedown", (event) => event.preventDefault());
    row.addEventListener("click", () => choose(rows.indexOf(row)));
    return row;
  }

  async function query(text) {
    cancel();
    const controller = new AbortController();
    inFlight = controller;
    try {
      const response = await api.geocode(text, { signal: controller.signal });
      const found = Array.isArray(response.candidates) ? response.candidates.slice(0, 8) : [];
      render(found, found.length === 0 ? NO_CANDIDATES : null);
    } catch (error) {
      if (error.code === "aborted") {
        return;
      }
      // A geocode failure is not a search failure: keep the "drop a pin"
      // escape hatch on screen rather than leaving the user with nothing.
      render([], NO_CANDIDATES);
      onError(error);
    } finally {
      if (inFlight === controller) {
        inFlight = null;
      }
    }
  }

  input.addEventListener("input", () => {
    const text = input.value.trim();
    cancel();
    if (text.length < MIN_QUERY_CHARS) {
      close();
      return;
    }
    timer = setTimeout(() => query(text), DEBOUNCE_MS);
  });

  // Focusing an empty field opens the list on the pin row alone: without it the
  // "drop a pin" escape hatch would only exist once you had typed something,
  // which is exactly backwards for the user who cannot name where they are.
  input.addEventListener("focus", () => {
    if (input.value.trim().length < MIN_QUERY_CHARS && list.hidden) {
      render([], null);
    }
  });

  input.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      if (list.hidden) {
        return;
      }
      event.preventDefault();
      const step = event.key === "ArrowDown" ? 1 : -1;
      const start = activeIndex < 0 ? (step === 1 ? 0 : rows.length - 1) : activeIndex + step;
      setActive(selectable((start + rows.length) % rows.length, step));
      return;
    }
    if (event.key === "Enter" && !list.hidden && activeIndex >= 0) {
      event.preventDefault();
      choose(activeIndex);
      return;
    }
    if (event.key === "Escape" && !list.hidden) {
      event.preventDefault();
      cancel();
      close();
    }
  });

  /** Skip the "no match" row, which is a message and not a choice. */
  function selectable(index, step) {
    let position = index;
    for (let guard = 0; guard < rows.length; guard += 1) {
      if (rows[position].getAttribute("aria-disabled") !== "true") {
        return position;
      }
      position = (position + step + rows.length) % rows.length;
    }
    return -1;
  }

  input.addEventListener("blur", () => {
    // The click handler runs on mousedown-prevented rows, so by blur time a
    // pick has already been made; anything else means the user left the field.
    setTimeout(close, 0);
  });

  return { close, cancel };
}
