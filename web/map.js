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

/**
 * Verdict colour, dash pattern, and width ramp — the light-scheme values from
 * tokens.css, repeated here because MapLibre paint properties cannot read CSS
 * custom properties.
 *
 * They are the *light* values in both colour schemes on purpose: the vendored
 * basemap is a light Protomaps style and there is no dark one in the repo, so a
 * dark verdict palette would be drawn over a light map and lose its measured
 * contrast (DESIGN_DIRECTION §6, tokens.css dark block).
 *
 * `dash` is in line-width units. `no_data` is the widest of the four and is
 * dotted rather than dashed: UX_AUDIT P0-6 measured the old grey at 0.7 px
 * marks on a grey basemap, which made the one verdict a driver must not
 * overlook the one they could not see.
 */
const VERDICT_STYLE = {
  legal: { color: "#15855a", dash: null, cap: "round", widths: [2, 3.5, 5.5, 8] },
  ambiguous: { color: "#c78700", dash: [2, 1.25], cap: "butt", widths: [2, 3.5, 5.5, 8] },
  illegal: { color: "#a01b12", dash: [0.9, 0.7], cap: "butt", widths: [2, 3.5, 5.5, 8] },
  no_data: { color: "#4c525d", dash: [0, 2.2], cap: "round", widths: [3, 4, 6, 9] },
};

// Drawn bottom to top: a legal span must not hide an illegal one, and grey has
// to survive being under all three.
const VERDICT_ORDER = ["legal", "ambiguous", "illegal", "no_data"];

const MAP_HALO = "#ffffff";
const SELECT_HALO = "#1a1917";
const ACCENT = "#1a56b0";

const ZOOM_STOPS = [13, 15, 16.5, 18];

export { VERDICT_STYLE };

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

/** A width ramp in the design's zoom stops (DESIGN_DIRECTION §6). */
function widthExpression(widths, extra = 0) {
  const stops = [];
  ZOOM_STOPS.forEach((zoom, index) => stops.push(zoom, widths[index] + extra));
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
        geometry: result.geometry,
        properties: { reg_seg_id: result.reg_seg_id, verdict: result.verdict },
      }));
    this.#setData("segments", { type: "FeatureCollection", features });
  }

  clearResults() {
    this.geometries = new Map();
    this.selectedId = null;
    this.hoverId = null;
    this.#setData("segments", EMPTY_COLLECTION);
    this.#applyFilters();
  }

  setSelected(regSegId) {
    this.selectedId = regSegId;
    this.#applyFilters();
  }

  setHover(regSegId) {
    if (this.hoverId === regSegId) {
      return;
    }
    this.hoverId = regSegId;
    this.#applyFilters();
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
    for (const id of ["walk-radius", "coverage", "segments"]) {
      this.map.addSource(id, { type: "geojson", data: EMPTY_COLLECTION });
    }

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

    // Selection: a dark halo and a soft accent glow under the line, so the span
    // keeps its own verdict colour and dash while being unmistakably picked.
    this.map.addLayer({
      id: "segments-select-glow",
      type: "line",
      source: "segments",
      filter: ["==", ["get", "reg_seg_id"], ""],
      layout: { "line-cap": "round" },
      paint: {
        "line-color": ACCENT,
        "line-width": widthExpression([10, 14, 20, 26]),
        "line-blur": 8,
        "line-opacity": 0.45,
      },
    });
    this.map.addLayer({
      id: "segments-select-halo",
      type: "line",
      source: "segments",
      filter: ["==", ["get", "reg_seg_id"], ""],
      layout: { "line-cap": "round" },
      paint: {
        "line-color": SELECT_HALO,
        "line-width": widthExpression([6, 8, 12, 17]),
        "line-opacity": 0.9,
      },
    });
    this.map.addLayer({
      id: "segments-hover",
      type: "line",
      source: "segments",
      filter: ["==", ["get", "reg_seg_id"], ""],
      layout: { "line-cap": "round" },
      paint: {
        "line-color": MAP_HALO,
        "line-width": widthExpression([6, 8, 11, 15]),
        "line-opacity": 0.95,
      },
    });

    // Casing first, then the line, one pair per verdict. The white casing is
    // what makes a dark red line legible over #e2dfda earth and a grey dotted
    // line legible over #ebebeb road fill.
    for (const verdict of VERDICT_ORDER) {
      const style = VERDICT_STYLE[verdict];
      this.map.addLayer({
        id: `segments-casing-${verdict}`,
        type: "line",
        source: "segments",
        filter: ["==", ["get", "verdict"], verdict],
        layout: { "line-cap": "round", "line-join": "round" },
        paint: {
          "line-color": MAP_HALO,
          "line-width": widthExpression(style.widths, 3),
          "line-opacity": 0.9,
        },
      });
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
          "line-width": widthExpression(style.widths),
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

  #applyFilters() {
    const match = (id) => ["==", ["get", "reg_seg_id"], id === null ? "" : id];
    for (const layer of ["segments-select-glow", "segments-select-halo"]) {
      if (this.map.getLayer(layer)) {
        this.map.setFilter(layer, match(this.selectedId));
      }
    }
    if (this.map.getLayer("segments-hover")) {
      this.map.setFilter(
        "segments-hover",
        match(this.hoverId === this.selectedId ? null : this.hoverId),
      );
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
