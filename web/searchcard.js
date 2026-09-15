/**
 * The search card: where, when, how far, and what to optimise for.
 *
 * Two UX_AUDIT findings shape this file. P0-7: `datetime-local` clipped its own
 * value at every width, so the window is now a date, a start time, and a
 * duration chip group, with the result written out in words underneath. P1-3:
 * the ranking weights only applied on the next search, so `Prefer` re-ranks
 * what is already on screen, while the two inputs the server owns — the walk
 * radius and the window — mark the Search button stale instead.
 */

import { STALE_SEARCH_NOTE } from "./copy.js";
import { el, replaceChildren } from "./dom.js";
import {
  addMinutes,
  nextQuarterHour,
  parseLocal,
  toApiDateTime,
  toDateInputValue,
  toTimeInputValue,
  windowSummary,
} from "./format.js";
import { DEFAULT_PREFER, PREFER_PRESETS, presetFor, presetWeights } from "./rank.js";

const DURATIONS = [
  { id: "60", label: "1h" },
  { id: "120", label: "2h" },
  { id: "180", label: "3h" },
  { id: "240", label: "4h" },
  { id: "custom", label: "Custom" },
];

const WALK_RADII = [
  { id: "5", label: "5 min" },
  { id: "10", label: "10 min" },
  { id: "15", label: "15 min" },
  { id: "20", label: "20 min" },
  { id: "custom", label: "Custom" },
];

const DEFAULT_DURATION = "120";
const DEFAULT_WALK = "10";
const MAX_WINDOW_MINUTES = 24 * 60;

/**
 * A `role="radiogroup"` of pill buttons with a roving tabindex: one Tab stop,
 * arrows to move. Native radios cannot carry this shape without a label hack,
 * and UX_AUDIT (e) 3 asks for one tab stop per group.
 */
function chipGroup(container, items, { value, onChange }) {
  let current = value;
  const buttons = new Map();

  function paint() {
    for (const [id, button] of buttons) {
      const checked = id === current;
      button.setAttribute("aria-checked", String(checked));
      button.tabIndex = checked ? 0 : -1;
    }
  }

  function select(id, { notify = true } = {}) {
    if (!buttons.has(id)) {
      return;
    }
    current = id;
    paint();
    if (notify) {
      onChange(id);
    }
  }

  function move(step) {
    const ids = [...buttons.keys()].filter((id) => buttons.get(id).dataset.disabled !== "true");
    const index = ids.indexOf(current);
    const next = ids[(index + step + ids.length) % ids.length];
    select(next);
    buttons.get(next).focus();
  }

  replaceChildren(
    container,
    items.map((item) => {
      const button = el("button", {
        className: "chip",
        text: item.label,
        attrs: { type: "button", role: "radio", "aria-checked": "false", "data-id": item.id },
      });
      button.addEventListener("click", () => select(item.id));
      button.addEventListener("keydown", (event) => {
        if (event.key === "ArrowRight" || event.key === "ArrowDown") {
          event.preventDefault();
          move(1);
        } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
          event.preventDefault();
          move(-1);
        }
      });
      buttons.set(item.id, button);
      return button;
    }),
  );
  paint();

  return {
    value: () => current,
    select,
    /** Add or reveal a chip that is shown but not choosable (`Prefer: Custom`). */
    setPlaceholder(id, label, shown) {
      let button = buttons.get(id);
      if (!button && shown) {
        button = el("button", {
          className: "chip",
          text: label,
          attrs: {
            type: "button",
            role: "radio",
            "aria-checked": "true",
            "aria-disabled": "true",
            "data-id": id,
            "data-disabled": "true",
            tabindex: "-1",
          },
        });
        buttons.set(id, button);
        container.append(button);
      }
      if (button) {
        button.hidden = !shown;
        if (shown) {
          current = id;
        }
        paint();
      }
    },
  };
}

/**
 * Build the card's behaviour over the markup in index.html.
 *
 * @param {{dom: Object, onSearch: Function, onRerank: Function,
 *          onWalkChange: Function, onStaleChange: Function}} options
 */
export function createSearchCard({ dom, onRerank, onWalkChange, onStaleChange }) {
  const now = nextQuarterHour();
  dom.date.value = toDateInputValue(now);
  dom.startTime.value = toTimeInputValue(now);
  dom.endTime.value = toTimeInputValue(addMinutes(now, Number(DEFAULT_DURATION)));
  dom.walkCustom.value = DEFAULT_WALK;

  let stale = false;
  let weights = presetWeights(DEFAULT_PREFER);

  const duration = chipGroup(dom.durationChips, DURATIONS, {
    value: DEFAULT_DURATION,
    onChange: (id) => {
      dom.customDuration.hidden = id !== "custom";
      if (id !== "custom") {
        const dates = windowDates();
        if (dates) {
          dom.endTime.value = toTimeInputValue(dates.end);
        }
      }
      windowChanged();
    },
  });

  const walk = chipGroup(dom.walkChips, WALK_RADII, {
    value: DEFAULT_WALK,
    onChange: (id) => {
      dom.customWalk.hidden = id !== "custom";
      if (id === "custom") {
        dom.walkCustom.focus();
      }
      walkChanged();
    },
  });

  const prefer = chipGroup(dom.preferChips, PREFER_PRESETS, {
    value: DEFAULT_PREFER,
    onChange: (id) => {
      weights = presetWeights(id);
      prefer.setPlaceholder("custom", "Custom", false);
      paintSliders();
      onRerank(weights);
    },
  });

  const sliders = [
    { input: dom.weightWalk, output: dom.weightWalkOut, key: "walk" },
    { input: dom.weightMoney, output: dom.weightMoneyOut, key: "money" },
    { input: dom.weightRisk, output: dom.weightRiskOut, key: "risk" },
  ];

  function paintSliders() {
    for (const slider of sliders) {
      slider.input.value = String(weights[slider.key]);
      slider.output.textContent = weights[slider.key].toFixed(1);
    }
  }

  for (const slider of sliders) {
    slider.input.addEventListener("input", () => {
      weights = { ...weights, [slider.key]: Number(slider.input.value) };
      slider.output.textContent = weights[slider.key].toFixed(1);
      const preset = presetFor(weights);
      if (preset) {
        prefer.setPlaceholder("custom", "Custom", false);
        prefer.select(preset, { notify: false });
      } else {
        prefer.setPlaceholder("custom", "Custom", true);
      }
      onRerank(weights);
    });
  }
  paintSliders();

  function windowDates() {
    const start = parseLocal(dom.date.value, dom.startTime.value);
    if (!start) {
      return null;
    }
    if (duration.value() !== "custom") {
      return { start, end: addMinutes(start, Number(duration.value())) };
    }
    const end = parseLocal(dom.date.value, dom.endTime.value);
    if (!end) {
      return null;
    }
    // An end time earlier than the start means the next morning, which is the
    // only reading that is ever true of a parking window.
    return { start, end: end <= start ? addMinutes(end, MAX_WINDOW_MINUTES) : end };
  }

  function paintWindow() {
    const dates = windowDates();
    dom.windowSummary.textContent = dates ? windowSummary(dates.start, dates.end) : "";
  }

  function walkMinutes() {
    const id = walk.value();
    if (id !== "custom") {
      return Number(id);
    }
    const typed = Number(dom.walkCustom.value);
    return Number.isFinite(typed) && typed >= 1 && typed <= 30 ? typed : 10;
  }

  function setStale(next) {
    if (stale === next) {
      return;
    }
    stale = next;
    dom.searchButton.classList.toggle("is-stale", next);
    dom.staleNote.hidden = !next;
    dom.staleNote.textContent = next ? STALE_SEARCH_NOTE : "";
    onStaleChange(next);
  }

  function windowChanged() {
    paintWindow();
    setStale(true);
  }

  function walkChanged() {
    setStale(true);
    onWalkChange(walkMinutes());
  }

  for (const input of [dom.date, dom.startTime, dom.endTime]) {
    input.addEventListener("input", windowChanged);
  }
  dom.walkCustom.addEventListener("input", walkChanged);
  paintWindow();

  return {
    weights: () => ({ ...weights }),
    walkMinutes,
    windowText: () => dom.windowSummary.textContent,

    /** The window as the API wants it, or an error code the caller can word. */
    window() {
      const dates = windowDates();
      if (!dates) {
        return { error: "incomplete" };
      }
      const minutes = (dates.end - dates.start) / 60000;
      if (minutes <= 0) {
        return { error: "backwards" };
      }
      if (minutes > MAX_WINDOW_MINUTES) {
        return { error: "too_long" };
      }
      return {
        t1: toApiDateTime(dom.date.value, toTimeInputValue(dates.start)),
        t2: toApiDateTime(toDateInputValue(dates.end), toTimeInputValue(dates.end)),
        start: dates.start,
        end: dates.end,
      };
    },

    markSearched() {
      setStale(false);
    },

    isStale: () => stale,

    /** After a search the card folds to one line; Edit brings it back. */
    collapse(destinationLabel) {
      dom.form.hidden = true;
      dom.summary.hidden = false;
      replaceChildren(dom.summaryText, [
        el("b", { text: destinationLabel }),
        el("span", { text: `${dom.windowSummary.textContent} · ${walkMinutes()} min walk` }),
      ]);
    },

    expand() {
      dom.form.hidden = false;
      dom.summary.hidden = true;
      dom.destination.focus();
    },

    isCollapsed: () => dom.form.hidden,
  };
}
