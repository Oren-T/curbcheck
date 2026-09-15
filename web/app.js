/**
 * State, form handling, and the search flow.
 *
 * The module wiring is deliberate (STYLE_GUIDE §4): `api.js` talks to the
 * server, `map.js` owns MapLibre, `results.js` renders the list, `detail.js`
 * renders one segment, `format.js` turns API values into words, and this file
 * is the only place that holds mutable state.
 */

import * as api from "./api.js";
import { TEMPORARY_SIGNAGE_CAVEAT } from "./copy.js";
import { hideDetail, renderDetail, streetLabel } from "./detail.js";
import { clear, el, replaceChildren } from "./dom.js";
import { nextTopOfHour, toLocalInputValue } from "./format.js";
import { CurbMap, loadStyle } from "./map.js";
import { markSelected, renderResults } from "./results.js";

const WINDOW_HOURS = 2;

// How many result cards get their street name looked up (see `hydrateLabels`).
const LABEL_PREFETCH = 8;

const dom = {
  form: document.getElementById("search-form"),
  destination: document.getElementById("destination"),
  pinToggle: document.getElementById("pin-toggle"),
  pinReadout: document.getElementById("pin-readout"),
  t1: document.getElementById("t1"),
  t2: document.getElementById("t2"),
  walkMinutes: document.getElementById("walk-minutes"),
  searchButton: document.getElementById("search-button"),
  status: document.getElementById("status"),
  suggestions: document.getElementById("suggestions"),
  caveats: document.getElementById("search-caveats"),
  results: document.getElementById("results"),
  resultsHeading: document.getElementById("results-heading"),
  detail: document.getElementById("detail"),
  disclaimer: document.getElementById("disclaimer-text"),
  mapContainer: document.getElementById("map"),
};

const SLIDERS = [
  { input: dom.walkMinutes, output: document.getElementById("walk-minutes-out"), digits: 0 },
  {
    input: document.getElementById("weight-walk"),
    output: document.getElementById("weight-walk-out"),
    digits: 1,
  },
  {
    input: document.getElementById("weight-money"),
    output: document.getElementById("weight-money-out"),
    digits: 1,
  },
  {
    input: document.getElementById("weight-risk"),
    output: document.getElementById("weight-risk-out"),
    digits: 1,
  },
];

const state = {
  /** [lon, lat] when the user dropped a pin or a search resolved an address. */
  destination: null,
  destinationLabel: "",
  pinMode: false,
  results: [],
  resultsById: new Map(),
  cardsById: new Map(),
  selectedId: null,
  details: new Map(),
  searching: false,
};

function init() {
  const arrive = nextTopOfHour();
  const leave = new Date(arrive.getTime() + WINDOW_HOURS * 60 * 60 * 1000);
  dom.t1.value = toLocalInputValue(arrive);
  dom.t2.value = toLocalInputValue(leave);

  for (const slider of SLIDERS) {
    const update = () => {
      slider.output.textContent = Number(slider.input.value).toFixed(slider.digits);
    };
    slider.input.addEventListener("input", update);
    update();
  }
  dom.walkMinutes.addEventListener("input", () => {
    if (state.destination) {
      curbMap.setWalkRadius(state.destination, Number(dom.walkMinutes.value));
    }
  });

  dom.pinToggle.addEventListener("click", () => setPinMode(!state.pinMode));
  dom.destination.addEventListener("input", () => {
    if (dom.destination.value !== "") {
      setPinMode(false);
    }
  });
  dom.form.addEventListener("submit", onSubmit);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && state.selectedId !== null) {
      closeDetail();
    }
  });

  reportHealth();
}

async function reportHealth() {
  try {
    const health = await api.health();
    if (health.status !== "ok") {
      setStatus(
        "No parking database yet. Run `curbcheck sync` to build data/curbcheck.sqlite.",
        "error",
      );
      return;
    }
    const signs = typeof health.sign_count === "number" ? health.sign_count : 0;
    setStatus(`Ready. ${signs.toLocaleString("en-US")} signs loaded.`);
  } catch (error) {
    setStatus(error.message, "error");
  }
}

function setPinMode(enabled) {
  state.pinMode = enabled;
  dom.pinToggle.setAttribute("aria-pressed", String(enabled));
  dom.pinToggle.textContent = enabled ? "Click the map to place the pin" : "Drop a pin instead";
  curbMap.setPinMode(enabled);
  if (enabled) {
    dom.destination.value = "";
  }
}

function dropPin(lonlat) {
  state.destination = lonlat;
  state.destinationLabel = `${lonlat[1].toFixed(6)}, ${lonlat[0].toFixed(6)}`;
  dom.pinReadout.textContent = `Pin at ${state.destinationLabel}`;
  curbMap.setDestination(lonlat, state.destinationLabel);
  curbMap.setWalkRadius(lonlat, Number(dom.walkMinutes.value));
  setPinMode(false);
}

function onSubmit(event) {
  event.preventDefault();
  if (state.searching) {
    return;
  }
  const address = dom.destination.value.trim();
  if (address === "" && state.destination === null) {
    setStatus("Type an address or an intersection, or drop a pin on the map.", "error");
    return;
  }
  if (dom.t2.value <= dom.t1.value) {
    setStatus("The departure time has to be after the arrival time.", "error");
    return;
  }
  runSearch(
    address === "" ? { lat: state.destination[1], lon: state.destination[0] } : { address },
  );
}

async function runSearch(where) {
  const walkMinutes = Number(dom.walkMinutes.value);
  const body = {
    ...where,
    t1: dom.t1.value,
    t2: dom.t2.value,
    walk_minutes: walkMinutes,
    weights: {
      walk: Number(document.getElementById("weight-walk").value),
      money: Number(document.getElementById("weight-money").value),
      risk: Number(document.getElementById("weight-risk").value),
    },
  };

  setSearching(true);
  clear(dom.suggestions);
  setStatus("Searching…");
  try {
    const response = await api.search(body);
    showResponse(response, walkMinutes);
  } catch (error) {
    handleSearchError(error, where);
  } finally {
    setSearching(false);
  }
}

function showResponse(response, walkMinutes) {
  // The banner already carries the SPEC §17 text, marked up with the emphasis
  // the spec puts on it. Replace it only if the server's wording differs.
  const disclaimer = response.disclaimer;
  if (typeof disclaimer === "string" && !sameText(disclaimer, dom.disclaimer.textContent)) {
    dom.disclaimer.textContent = disclaimer;
  }
  renderCaveats(response.caveats);

  const results = Array.isArray(response.results) ? response.results : [];
  state.results = results;
  state.resultsById = new Map(results.map((result) => [result.reg_seg_id, result]));
  state.details = new Map();
  closeDetail();

  const destination = response.destination;
  if (destination && typeof destination.lat === "number" && typeof destination.lon === "number") {
    state.destination = [destination.lon, destination.lat];
    state.destinationLabel = destination.label || "Destination";
    dom.pinReadout.textContent = `Searching near ${state.destinationLabel}`;
    curbMap.setDestination(state.destination, state.destinationLabel);
    curbMap.setWalkRadius(state.destination, walkMinutes);
    curbMap.centerOn(state.destination, zoomForWalkMinutes(walkMinutes));
  }

  curbMap.setResults(results);
  dom.resultsHeading.textContent = results.length === 0 ? "Results" : `Results (${results.length})`;
  state.cardsById = renderResults(dom.results, results, {
    onSelect: (regSegId) => selectSegment(regSegId, { fly: true }),
    detailFor: (regSegId) => state.details.get(regSegId) || null,
  });
  hydrateLabels(results);

  const legal = results.filter((result) => result.verdict === "legal").length;
  setStatus(
    results.length === 0
      ? "Nothing within that walk radius. Try a longer walk or a different time."
      : `${results.length} stretches within a ${walkMinutes} min walk · ${legal} legal for the whole window.`,
  );
}

function handleSearchError(error, where) {
  setStatus(error.message, "error");
  if (error.code === "address_not_found" && where.address) {
    offerSuggestions(where.address);
  }
}

async function offerSuggestions(query) {
  try {
    const response = await api.geocode(query);
    const candidates = Array.isArray(response.candidates) ? response.candidates : [];
    if (candidates.length === 0) {
      setStatus(
        "No match for that address. Try a cross street like “3 Ave & E 85 St”, or drop a pin.",
        "error",
      );
      return;
    }
    replaceChildren(
      dom.suggestions,
      candidates.map((candidate) => {
        const button = el("button", {
          className: "suggestion",
          text: `${candidate.label} (${candidate.kind})`,
          attrs: { type: "button" },
        });
        button.addEventListener("click", () => {
          dom.destination.value = candidate.label;
          dropPin([candidate.lon, candidate.lat]);
          clear(dom.suggestions);
          runSearch({ lat: candidate.lat, lon: candidate.lon });
        });
        return el("li", {}, [button]);
      }),
    );
  } catch {
    // The address simply could not be resolved; the status line already says so.
    clear(dom.suggestions);
  }
}

function sameText(left, right) {
  return left.replace(/\s+/g, " ").trim() === right.replace(/\s+/g, " ").trim();
}

function renderCaveats(caveats) {
  const items = Array.isArray(caveats) && caveats.length > 0 ? caveats : [TEMPORARY_SIGNAGE_CAVEAT];
  replaceChildren(
    dom.caveats,
    items.map((caveat) => el("li", { text: caveat })),
  );
}

/**
 * Fill in street names for the top of the list.
 *
 * A search result carries no street name (docs/API.md), so a card falls back to
 * the opaque `reg_seg_id`. One detail request per card would be wasteful for a
 * hundred results, but the few the user actually reads are worth the round
 * trips, and the responses are the same ones the detail panel needs next.
 */
async function hydrateLabels(results) {
  for (const result of results.slice(0, LABEL_PREFETCH)) {
    if (state.resultsById.get(result.reg_seg_id) !== result) {
      return;
    }
    try {
      const detail = await api.segment(result.reg_seg_id);
      state.details.set(result.reg_seg_id, detail);
      const card = state.cardsById.get(result.reg_seg_id);
      if (card) {
        card.title.textContent = streetLabel(result, detail);
      }
    } catch {
      // A label is a nicety; the card already shows the id.
      return;
    }
  }
}

function selectSegment(regSegId, { fly }) {
  const result = state.resultsById.get(regSegId) || null;
  state.selectedId = regSegId;
  curbMap.setSelected(regSegId);
  if (fly) {
    curbMap.flyToSegment(regSegId);
  }
  markSelected(state.cardsById, regSegId);
  const card = state.cardsById.get(regSegId);
  if (card) {
    card.card.scrollIntoView({ block: "nearest" });
  }

  const cached = state.details.get(regSegId);
  showDetail({ result, detail: cached || null, error: null, loading: !cached, regSegId });
  if (!cached) {
    loadDetail(regSegId, result);
  }
}

async function loadDetail(regSegId, result) {
  try {
    const detail = await api.segment(regSegId);
    state.details.set(regSegId, detail);
    if (state.selectedId !== regSegId) {
      return;
    }
    showDetail({ result, detail, error: null, loading: false, regSegId });
    const card = state.cardsById.get(regSegId);
    if (card) {
      card.title.textContent = streetLabel(result, detail);
    }
  } catch (error) {
    if (state.selectedId === regSegId) {
      showDetail({ result, detail: null, error: error.message, loading: false, regSegId });
    }
  }
}

function showDetail({ result, detail, error, loading, regSegId }) {
  renderDetail(dom.detail, {
    result,
    detail,
    error,
    loading,
    label: streetLabel(result || { reg_seg_id: regSegId }, detail),
    onClose: closeDetail,
  });
}

function closeDetail() {
  state.selectedId = null;
  curbMap.setSelected(null);
  hideDetail(dom.detail);
  markSelected(state.cardsById, null);
}

function setSearching(searching) {
  state.searching = searching;
  dom.searchButton.disabled = searching;
  dom.searchButton.textContent = searching ? "Searching…" : "Search";
}

function setStatus(message, kind = "info") {
  dom.status.textContent = message;
  dom.status.classList.toggle("status-error", kind === "error");
}

function reportMapError(message) {
  // Missing glyph ranges are expected for non-Latin labels (web/basemap/MANIFEST.md)
  // and are not worth a banner; anything else means the basemap is broken.
  if (message.includes(".pbf")) {
    return;
  }
  setStatus(`Basemap problem: ${message}`, "error");
}

function zoomForWalkMinutes(walkMinutes) {
  if (walkMinutes <= 5) {
    return 16;
  }
  if (walkMinutes <= 12) {
    return 15;
  }
  return 14;
}

// Top-level await: the map cannot be constructed until the style is in hand,
// and a missing basemap must not stop the search from working.
const style = await loadStyle().catch((error) => {
  setStatus(`Basemap unavailable: ${error.message}. Search still works.`, "error");
  return null;
});

const curbMap = new CurbMap(dom.mapContainer, style, {
  onSelect: (regSegId) => selectSegment(regSegId, { fly: false }),
  onPinDrop: (lonlat) => dropPin(lonlat),
  onError: (message) => reportMapError(message),
});

init();
