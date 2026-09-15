/**
 * The map legend, built from the same table the map draws with.
 *
 * DESIGN_DIRECTION §8.2: the swatches come from `web/verdicts.js`, so a change
 * to a dash pattern or a hue cannot leave the legend describing the previous
 * one.
 */

import { NO_DATA_LEGEND_NOTE } from "./copy.js";
import { el, replaceChildren } from "./dom.js";
import { verdictInfo } from "./format.js";
import { VERDICT_ORDER, swatchBackground } from "./verdicts.js";

const SWATCH_HEIGHT = { legal: 4, ambiguous: 4, illegal: 4, no_data: 5 };

export function renderLegend(container) {
  const rows = VERDICT_ORDER.map((verdict) => {
    const height = SWATCH_HEIGHT[verdict];
    const swatch = el("span", { className: "legend-swatch", attrs: { "aria-hidden": "true" } });
    swatch.style.height = `${height}px`;
    swatch.style.borderRadius = `${height}px`;
    swatch.style.background = swatchBackground(verdict, height);
    return el("div", { className: "legend-row" }, [
      swatch,
      el("span", { className: "legend-word", text: verdictInfo(verdict).label }),
    ]);
  });
  replaceChildren(container, [
    ...rows,
    el("p", { className: "legend-note", text: NO_DATA_LEGEND_NOTE }),
  ]);
}
