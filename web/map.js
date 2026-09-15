/**
 * The map: basemap, the coloured curb spans, the destination marker, and the
 * walk-radius ring. Knows nothing about the API or the form.
 *
 * Everything it loads is local: the style, glyphs, and sprites come from
 * `/basemap/` and the tiles from `pmtiles:///basemap/manhattan.pmtiles` through
 * the PMTiles protocol, so panning makes no third-party request (SPEC §16).
 *
 * The map draws **every** span the server returned. The list is capped; the map
 * is not, because the ranked-legal/everything-else split the server does
 * (docs/API.md `limit` vs `map_limit`, docs/VALIDATION.md U1) only means
 * anything if the red and grey spans are actually on screen.
 */

// A namespace import, not a default one: the vendored v6 build exports only
// named bindings (`Map`, `Marker`, `addProtocol`, …) and no default, so
// `import maplibregl from` throws "does not provide an export named default".
import * as maplibregl from "./vendor/maplibre-gl/maplibre-gl.mjs";
import { el } from "./dom.js";
import { VERDICT_ORDER, VERDICT_STYLE } from "./verdicts.js";

const STYLE_URL = "/basemap/style.json";
const MANHATTAN_CENTER = [-73.9712, 40.7831];
const INITIAL_ZOOM = 12.5;
const SEGMENT_ZOOM = 17;

// Same constants as curbcheck/config.py, so the circle the user sees matches the
// radius the engine searched: 1.34 m/s mean walking speed, 1.3 grid detour.
const WALK_SPEED_M_PER_S = 1.34;
const WALK_DETOUR_FACTOR = 1.3;

// WGS-84 metres per degree at Manhattan's latitude, matching engine/search.py.
const M_PER_DEG_LAT = 111132.0;
const M_PER_DEG_LON_AT_EQUATOR = 111320.0;

const MAP_HALO = "#ffffff";
const ACCENT = "#1a56b0";

/**
 * The wash that turns the basemap into ground.
 *
 * DESIGN_DIRECTION §6 asked for a desaturating scrim; a vector style has no
 * `raster-saturation` to reach for, so this is a full-extent `background` layer
 * inserted directly under the result lines. It lifts roads, buildings, water
 * and parks toward the paper tone in one step, which leaves the four verdict
 * hues as the most saturated thing on screen. It exists only while results do.
 */
const SCRIM_COLOR = "#f6f5f3";
const SCRIM_OPACITY = 0.42;

/**
 * Basemap symbol layers dimmed while results are drawn, and how far.
 *
 * The audit screenshot had ~40 restaurant pins competing with the answer. The
 * street-name layers are dimmed least: they are how a driver reads *where* a
 * green line is, so they stay legible while the pins recede.
 */
const LABEL_DIMMING = [
  { id: "pois", opacity: 0.3 },
  { id: "address_label", opacity: 0.25 },
  { id: "roads_oneway", opacity: 0.2 },
  { id: "roads_shields", opacity: 0.35 },
  { id: "water_waterway_label", opacity: 0.4 },
  { id: "roads_labels_minor", opacity: 0.7 },
  { id: "roads_labels_major", opacity: 0.75 },
  { id: "places_subplace", opacity: 0.5 },
];
const DIMMED_PROPERTIES = ["text-opacity", "icon-opacity"];

/**
 * Emphasis multiplier on a line's width, driven by `feature-state`.
 *
 * The source carries `promoteId: "reg_seg_id"`, so hover and selection are two
 * booleans on the feature rather than three duplicate overlay layers — which is
 * also the only way a bump can keep the span's own dash pattern, since
 * `line-dasharray` is not data-driven.
 */
const EMPHASIS_WIDTH = [
  "case",
  ["boolean", ["feature-state", "selected"], false],
  1.6,
  ["boolean", ["feature-state", "hover"], false],
  1.35,
  1,
];

const ZOOM_STOPS = [13, 15, 16.5, 18];

export function walkRadiusMeters(walkMinutes) {
  return (walkMinutes * 60 * WALK_SPEED_M_PER_S) / WALK_DETOUR_FACTOR;
}

/** An approximate circle of `radiusMeters` around a point, as a GeoJSON polygon. */
export function circlePolygon([lon, lat], radiusMeters, steps = 96) {
  const latitudeDegrees = radiusMeters / M_PER_DEG_LAT;
  const longitudeDegrees =
    radiusMeters / (M_PER_DEG_LON_AT_EQUATOR * Math.cos((lat * Math.PI) / 180));
  const ring = [];
  for (let step = 0; step <= steps; step += 1) {
    const angle = (step / steps) * 2 * Math.PI;
    ring.push([lon + longitudeDegrees * Math.cos(angle), lat + latitudeDegrees * Math.sin(angle)]);
  }
  return { type: "Polygon", coordinates: [ring] };
}

/** `[west, south, east, north]` from either array or object bbox shapes. */
export function bboxRing(bbox) {
  const box = Array.isArray(bbox)
    ? bbox
    : [
        bbox.west ?? bbox.min_lon,
        bbox.south ?? bbox.min_lat,
        bbox.east ?? bbox.max_lon,
        bbox.north ?? bbox.max_lat,
      ];
  const [west, south, east, north] = box.map(Number);
  if ([west, south, east, north].some((value) => !Number.isFinite(value))) {
    return null;
  }
  return {
    type: "Polygon",
    coordinates: [
      [
        [west, south],
        [east, south],
        [east, north],
        [west, north],
        [west, south],
      ],
    ],
  };
}

const EMPTY_COLLECTION = { type: "FeatureCollection", features: [] };

// Shown when the style cannot be fetched: the curb data still ranks and draws,
// there is just nothing underneath it.
const BLANK_STYLE = { version: 8, sources: {}, layers: [] };

/**
 * A width ramp in the design's zoom stops (DESIGN_DIRECTION §6), optionally
 * scaled by the hover/selection multiplier.
 *
 * The multiplier goes *inside* each stop rather than around the whole ramp:
 * MapLibre rejects `["*", ["interpolate", ["zoom"], …], …]` with `"zoom"
 * expression may only be used as input to a top-level "step" or "interpolate"
 * expression`, so zoom has to stay the outermost input.
 */
function widthExpression(widths, { extra = 0, emphasis = null } = {}) {
  const stops = [];
  ZOOM_STOPS.forEach((zoom, index) => {
    const width = widths[index] + extra;
    stops.push(zoom, emphasis ? ["*", width, emphasis] : width);
  });
  return ["interpolate", ["exponential", 1.4], ["zoom"], ...stops];
}

/**
 * A 0.9-unit dash on a 1.5 px line is invisible, so the pattern only switches
 * on at z14.5; below that the line reads as near-solid.
 */
function dashExpression(dash) {
  // A bare array inside an expression is read as an expression, so both dash
  // patterns have to be wrapped in `literal`.
  return ["step", ["zoom"], ["literal", [12, 0.6]], 14.5, ["literal", dash]];
}

/**
 * Fetch the vendored style and make its sprite URL absolute.
 *
 * MapLibre 6 parses `style.sprite` with `new URL(value)` and throws
 * "Invalid sprite URL …, must be absolute" for a root-relative path, so the
 * style file keeps the host-free `/basemap/...` path (nothing in it may point
 * off-host) and the origin is attached here, at load time.
 */
export async function loadStyle(url = STYLE_URL) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`could not load ${url} (HTTP ${response.status})`);
  }
  const style = await response.json();
  if (typeof style.sprite === "string" && style.sprite.startsWith("/")) {
    style.sprite = new URL(style.sprite, location.origin).toString();
  }
  return style;
}

export class CurbMap {
  /**
   * @param {HTMLElement} container
   * @param {Object|null} style a style object from `loadStyle`, or null for no basemap
   * @param {{onSelect?: Function, onPinDrop?: Function, onError?: Function, onHover?: Function}} handlers
   */
  constructor(
    container,
    style,
    { onSelect = () => {}, onPinDrop = () => {}, onError = () => {}, onHover = () => {} } = {},
  ) {
    this.onSelect = onSelect;
    this.onPinDrop = onPinDrop;
    this.onError = onError;
    this.onHover = onHover;
    this.pinMode = false;
    this.selectedId = null;
    this.hoverId = null;
    this.geometries = new Map();
    this.marker = null;
    this.ringLabel = null;
    this.ring = null;
    this.labelPaint = new Map();
    this.reportedErrors = new Set();
    this.padding = { top: 40, right: 40, bottom: 40, left: 40 };

    registerPmtilesProtocol();

    this.map = new maplibregl.Map({
      container,
      style: style || BLANK_STYLE,
      center: MANHATTAN_CENTER,
      zoom: INITIAL_ZOOM,
      // The page renders its own attribution (SPEC §16) next to the NYC Open
      // Data credit, so MapLibre's control would only duplicate it.
      attributionControl: false,
      maxZoom: 19,
    });
    this.map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "bottom-right");
    this.map.addControl(new maplibregl.ScaleControl({ unit: "imperial" }), "bottom-left");

    this.ready = new Promise((resolve) => {
      this.map.on("load", () => {
        this.#addLayers();
        resolve();
      });
    });

    this.map.on("error", (event) => this.#reportError(event));
    this.map.on("click", (event) => {
      if (this.pinMode) {
        this.onPinDrop([event.lngLat.lng, event.lngLat.lat]);
      }
    });
  }

  whenReady() {
    return this.ready;
  }

  /** How much of the map is covered by floating chrome, for fitBounds. */
  setPadding(padding) {
    this.padding = { ...this.padding, ...padding };
  }

  /** Draw one feature per search result — all of them — and remember geometries. */
  setResults(results) {
    this.geometries = new Map(results.map((result) => [result.reg_seg_id, result.geometry]));
    const features = results
      .filter((result) => result.geometry)
      .map((result) => ({
        type: "Feature",
        id: result.reg_seg_id,
        geometry: result.geometry,
        properties: { reg_seg_id: result.reg_seg_id, verdict: result.verdict },
      }));
    this.#setData("segments", { type: "FeatureCollection", features });
    this.#setBasemapToned(features.length > 0);
  }

  clearResults() {
    this.geometries = new Map();
    this.selectedId = null;
    this.hoverId = null;
    this.#setData("segments", EMPTY_COLLECTION);
    this.#setBasemapToned(false);
  }

  /**
   * Show or hide one verdict on the map.
   *
   * This is the only thing that ever removes a span from the map, it is always
   * a press the user made, and the count pill that made it stays on screen
   * saying so — UX_AUDIT (f) 3 forbids curb quietly disappearing, not a filter.
   */
  setVisibleVerdicts(verdicts) {
    for (const verdict of VERDICT_ORDER) {
      const visibility = verdicts.has(verdict) ? "visible" : "none";
      for (const id of [
        `segments-casing-${verdict}`,
        `segments-glow-${verdict}`,
        `segments-${verdict}`,
      ]) {
        if (this.map.getLayer(id)) {
          this.map.setLayoutProperty(id, "visibility", visibility);
        }
      }
    }
  }

  setSelected(regSegId) {
    this.#setFeatureFlag(this.selectedId, "selected", false);
    this.selectedId = regSegId;
    this.#setFeatureFlag(regSegId, "selected", true);
    const filter = ["==", ["get", "reg_seg_id"], regSegId === null ? "" : regSegId];
    if (this.map.getLayer("segments-select-glow")) {
      this.map.setFilter("segments-select-glow", filter);
    }
  }

  setHover(regSegId) {
    if (this.hoverId === regSegId) {
      return;
    }
    this.#setFeatureFlag(this.hoverId, "hover", false);
    this.hoverId = regSegId;
    this.#setFeatureFlag(regSegId, "hover", true);
  }

  /**
   * Frame every drawn span plus the walk ring, leaving room for the rail and
   * the sheet. The ring is included so the radius that produced the answer is
   * never off screen — UX_AUDIT P1-3 found a drawn radius disagreeing with the
   * data it framed.
   */
  fitResults() {
    const bounds = new maplibregl.LngLatBounds();
    if (this.ring) {
      for (const position of this.ring.coordinates[0]) {
        bounds.extend(position);
      }
    }
    for (const geometry of this.geometries.values()) {
      if (!geometry) {
        continue;
      }
      for (const position of flattenPositions(geometry.coordinates)) {
        bounds.extend(position);
      }
    }
    if (bounds.isEmpty()) {
      return;
    }
    this.map.fitBounds(bounds, {
      padding: this.padding,
      maxZoom: 16,
      duration: this.#duration(600),
    });
  }

  /** Centre the map on a segment without changing the user's zoom drastically. */
  flyToSegment(regSegId) {
    const geometry = this.geometries.get(regSegId);
    if (!geometry) {
      return;
    }
    const bounds = new maplibregl.LngLatBounds();
    for (const position of flattenPositions(geometry.coordinates)) {
      bounds.extend(position);
    }
    if (bounds.isEmpty()) {
      return;
    }
    this.map.fitBounds(bounds, {
      padding: this.padding,
      maxZoom: SEGMENT_ZOOM,
      duration: this.#duration(600),
    });
  }

  /**
   * The destination marker: a disc with a white ring, not MapLibre's teardrop,
   * which at a glance reads as another piece of verdict colour.
   */
  setDestination(lonlat, label = "Destination") {
    if (!lonlat) {
      if (this.marker) {
        this.marker.remove();
        this.marker = null;
      }
      return;
    }
    if (!this.marker) {
      const element = el("div", {
        className: "map-marker",
        attrs: { role: "img", tabindex: "-1" },
      });
      this.marker = new maplibregl.Marker({ element });
    }
    this.marker.setLngLat(lonlat).addTo(this.map);
    const element = this.marker.getElement();
    element.setAttribute("aria-label", `Destination: ${label}`);
    element.setAttribute("title", label);
  }

  setWalkRadius(lonlat, walkMinutes) {
    if (this.ringLabel) {
      this.ringLabel.remove();
      this.ringLabel = null;
    }
    if (!lonlat) {
      this.ring = null;
      this.#setData("walk-radius", EMPTY_COLLECTION);
      return;
    }
    const radius = walkRadiusMeters(walkMinutes);
    this.ring = circlePolygon(lonlat, radius);
    this.#setData("walk-radius", {
      type: "FeatureCollection",
      features: [{ type: "Feature", geometry: this.ring, properties: {} }],
    });
    // The circle was unexplained in the audit; the label says what it is.
    const north = lonlat[1] + radius / M_PER_DEG_LAT;
    this.ringLabel = new maplibregl.Marker({
      element: el("div", { className: "ring-label", text: `${walkMinutes} min walk` }),
    })
      .setLngLat([lonlat[0], north])
      .addTo(this.map);
  }

  /** Outline the covered area — shown when a destination falls outside it. */
  setCoverage(bbox) {
    const ring = bbox ? bboxRing(bbox) : null;
    if (!ring) {
      this.#setData("coverage", EMPTY_COLLECTION);
      return;
    }
    this.#setData("coverage", {
      type: "FeatureCollection",
      features: [{ type: "Feature", geometry: ring, properties: {} }],
    });
  }

  fitCoverage(bbox) {
    const ring = bbox ? bboxRing(bbox) : null;
    if (!ring) {
      return;
    }
    const bounds = new maplibregl.LngLatBounds();
    for (const position of ring.coordinates[0]) {
      bounds.extend(position);
    }
    this.map.fitBounds(bounds, { padding: this.padding, duration: this.#duration(600) });
  }

  centerOn(lonlat, zoom = 15) {
    this.map.easeTo({
      center: lonlat,
      zoom: Math.max(this.map.getZoom(), zoom),
      duration: this.#duration(600),
    });
  }

  /**
   * Put the destination and its walk ring back in frame (UX_AUDIT P1-8).
   *
   * Framing the ring rather than the point is deliberate: the radius is the
   * question the answer was computed for, and a recentre that shows the pin but
   * not its circle leaves the user without the scale of what they are looking
   * at.
   */
  recentre(lonlat) {
    if (!lonlat) {
      return;
    }
    if (!this.ring) {
      this.centerOn(lonlat, 15);
      return;
    }
    const bounds = new maplibregl.LngLatBounds();
    for (const position of this.ring.coordinates[0]) {
      bounds.extend(position);
    }
    this.map.fitBounds(bounds, { padding: this.padding, duration: this.#duration(600) });
  }

  setPinMode(enabled) {
    this.pinMode = enabled;
    this.map.getCanvas().style.cursor = enabled ? "crosshair" : "";
  }

  resize() {
    this.map.resize();
  }

  #duration(milliseconds) {
    // UX_AUDIT (e) 10: a camera flight is motion, and reduced-motion means jump.
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : milliseconds;
  }

  #addLayers() {
    for (const id of ["walk-radius", "coverage"]) {
      this.map.addSource(id, { type: "geojson", data: EMPTY_COLLECTION });
    }
    // `promoteId` makes `reg_seg_id` the feature id, which is what lets hover
    // and selection be feature-state rather than duplicate overlay layers.
    this.map.addSource("segments", {
      type: "geojson",
      data: EMPTY_COLLECTION,
      promoteId: "reg_seg_id",
    });

    this.#rememberLabelPaint();
    this.map.addLayer({
      id: "results-scrim",
      type: "background",
      paint: {
        "background-color": SCRIM_COLOR,
        "background-opacity": 0,
        "background-opacity-transition": { duration: this.#duration(280) },
      },
    });

    this.map.addLayer({
      id: "walk-radius-fill",
      type: "fill",
      source: "walk-radius",
      paint: { "fill-color": ACCENT, "fill-opacity": 0.05 },
    });
    this.map.addLayer({
      id: "walk-radius-outline",
      type: "line",
      source: "walk-radius",
      paint: {
        "line-color": ACCENT,
        "line-width": 1.5,
        "line-opacity": 0.45,
        "line-dasharray": [4, 3],
      },
    });

    this.map.addLayer({
      id: "coverage-outline",
      type: "line",
      source: "coverage",
      paint: {
        "line-color": ACCENT,
        "line-width": 2,
        "line-dasharray": [6, 3],
        "line-opacity": 0.8,
      },
    });

    // Selection is a soft accent glow under the span, not a hard casing over
    // it: the line keeps its own hue and dash — which are what say what the
    // verdict is — and gains a halo plus the width bump in EMPHASIS_WIDTH.
    this.map.addLayer({
      id: "segments-select-glow",
      type: "line",
      source: "segments",
      filter: ["==", ["get", "reg_seg_id"], ""],
      layout: { "line-cap": "round" },
      paint: {
        "line-color": ACCENT,
        "line-width": widthExpression([14, 18, 24, 30]),
        "line-blur": 10,
        "line-opacity": 0.5,
      },
    });

    // Bottom to top, per verdict: glow, then casing, then the line itself.
    // Only `legal` has a glow and only `ambiguous`/`no_data` have a casing —
    // see VERDICT_STYLE for why each one does or does not.
    for (const verdict of VERDICT_ORDER) {
      const style = VERDICT_STYLE[verdict];
      if (style.glow) {
        this.map.addLayer({
          id: `segments-glow-${verdict}`,
          type: "line",
          source: "segments",
          filter: ["==", ["get", "verdict"], verdict],
          layout: { "line-cap": "round", "line-join": "round" },
          paint: {
            "line-color": style.color,
            "line-width": widthExpression(style.glow.widths, { emphasis: EMPHASIS_WIDTH }),
            "line-blur": style.glow.blur,
            "line-opacity": style.glow.opacity,
          },
        });
      }
      if (style.halo) {
        this.map.addLayer({
          id: `segments-casing-${verdict}`,
          type: "line",
          source: "segments",
          filter: ["==", ["get", "verdict"], verdict],
          layout: { "line-cap": "round", "line-join": "round" },
          paint: {
            "line-color": MAP_HALO,
            "line-width": widthExpression(style.widths, {
              extra: style.halo.extra,
              emphasis: EMPHASIS_WIDTH,
            }),
            "line-opacity": style.halo.opacity,
          },
        });
      }
    }
    for (const verdict of VERDICT_ORDER) {
      const style = VERDICT_STYLE[verdict];
      this.map.addLayer({
        id: `segments-${verdict}`,
        type: "line",
        source: "segments",
        filter: ["==", ["get", "verdict"], verdict],
        layout: { "line-cap": style.cap, "line-join": "round" },
        paint: {
          "line-color": style.color,
          "line-width": widthExpression(style.widths, { emphasis: EMPHASIS_WIDTH }),
          "line-opacity": style.opacity,
          ...(style.dash ? { "line-dasharray": dashExpression(style.dash) } : {}),
        },
      });
    }

    // An invisible fat line makes spans clickable without making them ugly.
    this.map.addLayer({
      id: "segments-hit",
      type: "line",
      source: "segments",
      paint: { "line-color": "#000000", "line-width": 18, "line-opacity": 0 },
    });

    this.map.on("click", "segments-hit", (event) => {
      if (this.pinMode || !event.features || event.features.length === 0) {
        return;
      }
      this.onSelect(event.features[0].properties.reg_seg_id);
    });
    this.map.on("mousemove", "segments-hit", (event) => {
      if (this.pinMode || !event.features || event.features.length === 0) {
        return;
      }
      this.map.getCanvas().style.cursor = "pointer";
      this.onHover(event.features[0].properties.reg_seg_id);
    });
    this.map.on("mouseleave", "segments-hit", () => {
      this.map.getCanvas().style.cursor = this.pinMode ? "crosshair" : "";
      this.onHover(null);
    });
  }

  #setFeatureFlag(regSegId, flag, value) {
    if (regSegId === null || regSegId === undefined || !this.map.getSource("segments")) {
      return;
    }
    this.map.setFeatureState({ source: "segments", id: regSegId }, { [flag]: value });
  }

  /** The symbol layers' own opacity values, so the dimming can be undone exactly. */
  #rememberLabelPaint() {
    for (const { id } of LABEL_DIMMING) {
      if (!this.map.getLayer(id)) {
        continue;
      }
      for (const property of DIMMED_PROPERTIES) {
        this.labelPaint.set(`${id}/${property}`, this.map.getPaintProperty(id, property) ?? 1);
      }
    }
  }

  /**
   * Push the basemap back while results are on it, and restore it when they go.
   *
   * Without this the answer is the least visible thing on its own map: 40 POI
   * pins, every building outline and a cyan river all read louder than a 4 px
   * green line (DESIGN_DIRECTION §6).
   */
  #setBasemapToned(toned) {
    if (this.map.getLayer("results-scrim")) {
      this.map.setPaintProperty("results-scrim", "background-opacity", toned ? SCRIM_OPACITY : 0);
    }
    for (const { id, opacity } of LABEL_DIMMING) {
      if (!this.map.getLayer(id)) {
        continue;
      }
      for (const property of DIMMED_PROPERTIES) {
        const original = this.labelPaint.get(`${id}/${property}`);
        this.map.setPaintProperty(id, property, toned ? opacity : original);
      }
    }
  }

  #setData(sourceId, data) {
    const source = this.map.getSource(sourceId);
    if (source) {
      source.setData(data);
    }
  }

  #reportError(event) {
    const error = event && event.error ? event.error : event;
    const message = error && error.message ? error.message : String(error);
    if (this.reportedErrors.has(message)) {
      return;
    }
    this.reportedErrors.add(message);
    this.onError(message);
  }
}

function registerPmtilesProtocol() {
  if (typeof pmtiles === "undefined") {
    throw new Error("vendor/pmtiles/pmtiles.js must load before map.js");
  }
  if (registerPmtilesProtocol.done) {
    return;
  }
  const protocol = new pmtiles.Protocol();
  maplibregl.addProtocol("pmtiles", protocol.tile);
  registerPmtilesProtocol.done = true;
}

function* flattenPositions(coordinates) {
  if (typeof coordinates[0] === "number") {
    yield coordinates;
    return;
  }
  for (const child of coordinates) {
    yield* flattenPositions(child);
  }
}
