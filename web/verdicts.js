/**
 * The four verdicts as drawn: hue, dash pattern, width ramp, weight.
 *
 * This is design data, not map code. The map paints from it, the legend builds
 * its swatches from it, and the verdict chip on every card and sheet builds its
 * swatch from it, so a change to a dash pattern or a hue cannot leave one of
 * the three describing a previous version of another (DESIGN_DIRECTION §8.2).
 *
 * The values are the *light*-scheme ones from tokens.css in both colour
 * schemes: the vendored basemap is a light Protomaps style and there is no dark
 * one in the repo, so a dark verdict palette would be drawn over a light map
 * and lose its measured contrast (DESIGN_DIRECTION §6, tokens.css dark block).
 *
 * The hues are untouched from the audited palette — they carry the contrast and
 * colour-vision guarantees. What the polish pass changed is everything around
 * them, because drawing all four at full weight with a white casing turned a
 * 600-span answer into noise:
 *
 *  - `legal` is the hero: solid, opaque, the widest ramp, and the only verdict
 *    with an outer glow. It is what the user came for.
 *  - `illegal` is thin, muted to 0.65, and has **no casing**. There are more
 *    illegal spans than anything else and a white-cased candy stripe on every
 *    one of them is what buried the map. It stays legible because the scrim
 *    the map draws under the results lifts the basemap away from it.
 *  - `ambiguous` keeps a faint casing: amber measures 2.56:1 on the grey road
 *    fill and is the one verdict that cannot hold its own edge (tokens.css).
 *  - `no_data` is never thinner than `legal` at any zoom and never under 3 px
 *    (UX_AUDIT (f) 3 — the fix for P0-6 makes grey *more* visible, not less).
 *    It is softened by opacity and by being dotted, never by width.
 *
 * `dash` and `halo.extra` are in line-width units. `glow` and `halo` widths are
 * absolute pixel ramps over the map's zoom stops.
 */
export const VERDICT_STYLE = {
  legal: {
    color: "#15855a",
    dash: null,
    cap: "round",
    widths: [4, 4.75, 5.5, 6],
    opacity: 1,
    halo: null,
    glow: { widths: [9, 11, 13, 15], blur: 6, opacity: 0.2 },
  },
  ambiguous: {
    color: "#c78700",
    dash: [2, 1.25],
    cap: "butt",
    widths: [2.5, 3, 3.5, 4],
    opacity: 0.9,
    halo: { extra: 2, opacity: 0.5 },
    glow: null,
  },
  illegal: {
    color: "#a01b12",
    dash: [0.9, 0.7],
    cap: "butt",
    widths: [2, 2.4, 2.8, 3],
    opacity: 0.65,
    halo: null,
    glow: null,
  },
  no_data: {
    color: "#4c525d",
    dash: [0, 2.2],
    cap: "round",
    widths: [4, 4.75, 5.5, 6],
    opacity: 0.55,
    halo: { extra: 1.5, opacity: 0.4 },
    glow: null,
  },
};

// Drawn bottom to top: a legal span must not hide an illegal one, and grey has
// to survive being under all three.
export const VERDICT_ORDER = ["legal", "ambiguous", "illegal", "no_data"];

/**
 * A CSS gradient with the same on/off rhythm as the map's `line-dasharray`.
 *
 * The dash units are multiples of the line width, which is what they mean to
 * MapLibre, so a swatch drawn `height` pixels tall repeats at the same rate the
 * map does at a line that thick.
 */
export function swatchBackground(verdict, height) {
  const style = VERDICT_STYLE[verdict];
  if (!style || !style.dash) {
    return style ? style.color : "transparent";
  }
  const [on, off] = style.dash;
  // A zero-length dash with a round cap is a dot whose diameter is the line
  // width, so the swatch draws it one height wide.
  const onPx = Math.max(on * height, on === 0 ? height : 0);
  const offPx = Math.max(off * height - (onPx - on * height), 2);
  return (
    `repeating-linear-gradient(to right, ${style.color} 0 ${onPx}px, ` +
    `transparent ${onPx}px ${onPx + offPx}px)`
  );
}
