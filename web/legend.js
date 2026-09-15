/**
 * The map legend, built from the same table the map draws with.
 *
 * DESIGN_DIRECTION §8.2: render the swatches from `VERDICT_STYLE` so a change
 * to a dash pattern or a hue cannot leave the legend describing the previous
 * one. The dash proportions below are the MapLibre `line-dasharray` values
 * scaled by the swatch height, which is what the units mean on the map.
 */

import { NO_DATA_LEGEND_NOTE } from "./copy.js";
import { el, replaceChildren } from "./dom.js";
import { VERDICT_STYLE } from "./map.js";
import { verdictInfo } from "./format.js";

const ROWS = ["legal", "ambiguous", "illegal", "no_data"];
const SWATCH_HEIGHT = { legal: 4, ambiguous: 4, illegal: 4, no_data: 5 };

/** A CSS gradient with the same on/off rhythm as the map's `line-dasharray`. */
function swatchBackground(style, height) {
  if (!style.dash) {
    return style.color;
  }
  const [on, off] = style.dash;
  // A zero-length dash with a round cap is a dot whose diameter is the line
  // width, so the swatch draws it one height wide.
  const onPx = Math.max(on * height, on === 0 ? height : 0);
  const offPx = Math.max(off * height - (onPx - on * height), 2);
  return `repeating-linear-gradient(to right, ${style.color} 0 ${onPx}px, transparent ${onPx}px ${onPx + offPx}px)`;
}

export function renderLegend(container) {
  const rows = ROWS.map((verdict) => {
    const style = VERDICT_STYLE[verdict];
    const height = SWATCH_HEIGHT[verdict];
    const swatch = el("span", { className: "legend-swatch", attrs: { "aria-hidden": "true" } });
    swatch.style.height = `${height}px`;
    swatch.style.borderRadius = `${height}px`;
    swatch.style.background = swatchBackground(style, height);
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
