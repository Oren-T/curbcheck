/**
 * The map: basemap, the coloured curb segments, the destination pin, and the
 * walk-radius circle. Knows nothing about the API or the form.
 *
 * Everything it loads is local: the style, glyphs, and sprites come from
 * `/basemap/` and the tiles from `pmtiles:///basemap/manhattan.pmtiles` through
 * the PMTiles protocol, so panning makes no third-party request (SPEC §16).
 */

// A namespace import, not a default one: the vendored v6 build exports only
// named bindings (`Map`, `Marker`, `addProtocol`, …) and no default, so
// `import maplibregl from` throws "does not provide an export named default".
import * as maplibregl from "./vendor/maplibre-gl/maplibre-gl.mjs";

const STYLE_URL = "/basemap/style.json";
const MANHATTAN_CENTER = [-73.9712, 40.7831];
const INITIAL_ZOOM = 12.5;
const SEGMENT_ZOOM = 16.5;

// Same constants as curbcheck/config.py, so the circle the user sees matches the
// radius the engine searched: 1.34 m/s mean walking speed, 1.3 grid detour.
const WALK_SPEED_M_PER_S = 1.34;
const WALK_DETOUR_FACTOR = 1.3;

// WGS-84 metres per degree at Manhattan's latitude, matching engine/search.py.
const M_PER_DEG_LAT = 111132.0;
const M_PER_DEG_LON_AT_EQUATOR = 111320.0;

/**
 * Colour plus a line pattern for each verdict. The pattern is not decoration:
 * red and green are the pair most often confused (deuteranopia/protanopia), so
 * every verdict is also distinguishable by dash pattern and width here, and by
 * its written label in the legend and the result cards.
 */
const VERDICT_STYLE = {
  legal: { color: "#0f7b3f", width: 6, dash: null },
  ambiguous: { color: "#b45309", width: 5, dash: [2, 1.4] },
  illegal: { color: "#b42318", width: 4.5, dash: [0.6, 0.9] },
  no_data: { color: "#6b7280", width: 3.5, dash: [0.2, 1.6] },
};

const VERDICT_ORDER = ["no_data", "illegal", "ambiguous", "legal"];

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

const EMPTY_COLLECTION = { type: "FeatureCollection", features: [] };

// Shown when the style cannot be fetched: the curb data still ranks and draws,
// there is just nothing underneath it.
const BLANK_STYLE = { version: 8, sources: {}, layers: [] };

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
   * @param {{onSelect?: Function, onPinDrop?: Function, onError?: Function}} handlers
   */
  constructor(
    container,
    style,
    { onSelect = () => {}, onPinDrop = () => {}, onError = () => {} } = {},
  ) {
    this.onSelect = onSelect;
    this.onPinDrop = onPinDrop;
    this.onError = onError;
    this.pinMode = false;
    this.selectedId = null;
    this.geometries = new Map();
    this.marker = null;
    this.reportedErrors = new Set();

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
    this.map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
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

  /** Draw one feature per search result and remember its geometry for flyTo. */
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
    this.#setData("segments", EMPTY_COLLECTION);
    this.#applySelection();
  }

  setSelected(regSegId) {
    this.selectedId = regSegId;
    this.#applySelection();
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
    this.map.fitBounds(bounds, { padding: 140, maxZoom: SEGMENT_ZOOM, duration: 800 });
  }

  setDestination(lonlat, label = "Destination") {
    if (!lonlat) {
      if (this.marker) {
        this.marker.remove();
        this.marker = null;
      }
      return;
    }
    if (!this.marker) {
      this.marker = new maplibregl.Marker({ color: "#1d4ed8" });
    }
    this.marker.setLngLat(lonlat).addTo(this.map);
    const element = this.marker.getElement();
    element.setAttribute("aria-label", label);
    element.setAttribute("title", label);
  }

  setWalkRadius(lonlat, walkMinutes) {
    if (!lonlat) {
      this.#setData("walk-radius", EMPTY_COLLECTION);
      return;
    }
    this.#setData("walk-radius", {
      type: "FeatureCollection",
      features: [
        {
          type: "Feature",
          geometry: circlePolygon(lonlat, walkRadiusMeters(walkMinutes)),
          properties: {},
        },
      ],
    });
  }

  centerOn(lonlat, zoom = 15) {
    this.map.easeTo({ center: lonlat, zoom: Math.max(this.map.getZoom(), zoom), duration: 700 });
  }

  setPinMode(enabled) {
    this.pinMode = enabled;
    this.map.getCanvas().style.cursor = enabled ? "crosshair" : "";
  }

  resize() {
    this.map.resize();
  }

  #addLayers() {
    this.map.addSource("walk-radius", { type: "geojson", data: EMPTY_COLLECTION });
    this.map.addSource("segments", { type: "geojson", data: EMPTY_COLLECTION });

    this.map.addLayer({
      id: "walk-radius-fill",
      type: "fill",
      source: "walk-radius",
      paint: { "fill-color": "#1d4ed8", "fill-opacity": 0.06 },
    });
    this.map.addLayer({
      id: "walk-radius-outline",
      type: "line",
      source: "walk-radius",
      paint: { "line-color": "#1d4ed8", "line-width": 1.5, "line-dasharray": [3, 2] },
    });

    this.map.addLayer({
      id: "segments-selected",
      type: "line",
      source: "segments",
      filter: ["==", ["get", "reg_seg_id"], ""],
      layout: { "line-cap": "round" },
      paint: { "line-color": "#111827", "line-width": 12, "line-opacity": 0.85 },
    });

    for (const verdict of VERDICT_ORDER) {
      const style = VERDICT_STYLE[verdict];
      this.map.addLayer({
        id: `segments-${verdict}`,
        type: "line",
        source: "segments",
        filter: ["==", ["get", "verdict"], verdict],
        layout: { "line-cap": style.dash ? "butt" : "round" },
        paint: {
          "line-color": style.color,
          "line-width": style.width,
          ...(style.dash ? { "line-dasharray": style.dash } : {}),
        },
      });
    }

    // An invisible fat line makes segments clickable without making them ugly.
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
    this.map.on("mouseenter", "segments-hit", () => {
      if (!this.pinMode) {
        this.map.getCanvas().style.cursor = "pointer";
      }
    });
    this.map.on("mouseleave", "segments-hit", () => {
      this.map.getCanvas().style.cursor = this.pinMode ? "crosshair" : "";
    });
  }

  #applySelection() {
    if (!this.map.getLayer("segments-selected")) {
      return;
    }
    this.map.setFilter("segments-selected", [
      "==",
      ["get", "reg_seg_id"],
      this.selectedId === null ? "" : this.selectedId,
    ]);
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
