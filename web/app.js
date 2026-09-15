/**
 * State, wiring, and the search flow.
 *
 * The module split is deliberate (STYLE_GUIDE §4): `api.js` talks to the
 * server, `map.js` owns MapLibre, `searchcard.js` owns the form controls,
 * `results.js` renders the list, `detail.js` renders one span, `format.js` and
 * `copy.js` own the words, and this file is the only place that holds mutable
 * state.
 */

import * as api from "./api.js";
import { hideAbout, renderAbout } from "./about.js";
import { createAutocomplete } from "./autocomplete.js";
import {
  CALENDAR_MISSING_CAVEAT,
  NEEDS_DESTINATION,
  OUTSIDE_COVERAGE,
  OUTSIDE_COVERAGE_DETAIL,
  PIN_MODE_NEEDS_A_CLICK,
  PIN_MODE_PROMPT,
  SERVER_UNREACHABLE,
  TEMPORARY_SIGNAGE_CAVEAT,
  WINDOW_BACKWARDS,
  WINDOW_TOO_LONG,
  errorSentence,
} from "./copy.js";
import { hideDetail, renderDetail, streetLabel } from "./detail.js";
import { clear, collapsible, el, replaceChildren } from "./dom.js";
import { createDrawer } from "./drawer.js";
import { nearLabel, placeLabel, statusLine, walkText } from "./format.js";
import { renderLegend } from "./legend.js";
import { CurbMap, loadStyle } from "./map.js";
import { rankLegal } from "./rank.js";
import {
  SHORTLIST_STEP,
  markSelected,
  renderEmpty,
  renderResults,
  renderSkeleton,
  renderStats,
  updateStats,
} from "./results.js";
import { createSearchCard } from "./searchcard.js";
import { renderNotice, showToast } from "./states.js";
import { VERDICT_ORDER } from "./verdicts.js";

// `limit` caps the ranked legal list, `map_limit` everything else (docs/API.md).
// Both are sent explicitly so the numbers the map draws are a decision here and
// not a server default that can move under the UI.
const SEARCH_LIMITS = { limit: 100, mapLimit: 2000 };

const byId = (id) => document.getElementById(id);

const dom = {
  form: byId("search-form"),
  destination: byId("destination"),
  autocompleteList: byId("autocomplete-list"),
  autocompleteStatus: byId("autocomplete-status"),
  pinReadout: byId("pin-readout"),
  date: byId("date"),
  startTime: byId("start-time"),
  endTime: byId("end-time"),
  durationChips: byId("duration-chips"),
  customDuration: byId("custom-duration"),
  walkChips: byId("walk-chips"),
  customWalk: byId("custom-walk"),
  walkCustom: byId("walk-custom"),
  preferChips: byId("prefer-chips"),
  weightWalk: byId("weight-walk"),
  weightWalkOut: byId("weight-walk-out"),
  weightMoney: byId("weight-money"),
  weightMoneyOut: byId("weight-money-out"),
  weightRisk: byId("weight-risk"),
  weightRiskOut: byId("weight-risk-out"),
  windowSummary: byId("window-summary"),
  searchButton: byId("search-button"),
  staleNote: byId("stale-note"),
  summary: byId("search-summary"),
  summaryText: byId("search-summary-text"),
  summaryEdit: byId("search-edit"),
  notices: byId("notices"),
  status: byId("status"),
  stats: byId("stats"),
  caveats: byId("search-caveats"),
  results: byId("results"),
  resultsHeading: byId("results-heading"),
  detail: byId("detail"),
  about: byId("about"),
  aboutOpen: byId("about-open"),
  legend: byId("legend"),
  toasts: byId("toasts"),
  noticeToggle: byId("notice-toggle"),
  fullNotice: byId("full-notice"),
  rail: document.querySelector(".rail"),
  railScroll: byId("rail-scroll"),
  railProgress: byId("rail-progress"),
  drawerHandle: byId("drawer-handle"),
  drawerSummary: byId("drawer-summary"),
  mapContainer: byId("map"),
  mapArea: document.querySelector(".map-area"),
  recentre: byId("recentre"),
};

dom.resultsHeading.hidden = true;

const state = {
  /** The resolved destination, and the text that was in the box when it resolved. */
  resolved: null,
  resolvedText: "",
  pinMode: false,
  results: [],
  counts: null,
  walkMinutes: 10,
  shown: SHORTLIST_STEP,
  openGroups: new Set(),
  cardsById: new Map(),
  /** Which verdicts the count pills are showing. Every one, until pressed. */
  visibleVerdicts: new Set(VERDICT_ORDER),
  resultsById: new Map(),
  details: new Map(),
  selectedId: null,
  hoverId: null,
  lastFocused: null,
  aboutOpener: null,
  searching: false,
  coverage: null,
  /**
   * The window the results on screen answer for: the API strings that were
   * sent, and the two local `Date`s the panel prints. Held here because every
   * verdict is a claim about it and the detail sheet has to be able to say so
   * in words (`whyLine`), which it could not while the window lived only in
   * the form.
   */
  window: null,
};

/* ---- Search ------------------------------------------------------------ */

function onSubmit(event) {
  event.preventDefault();
  if (state.searching) {
    return;
  }
  const text = dom.destination.value.trim();
  const where = destinationFor(text);
  if (!where) {
    // `dropPin` disarms pin mode, so still being armed means no pin was placed
    // since it was turned on. Searching would silently reuse the old answer.
    setStatus(state.pinMode ? PIN_MODE_NEEDS_A_CLICK : NEEDS_DESTINATION, "error");
    dom.destination.focus();
    return;
  }
  const window_ = searchCard.window();
  if (window_.error) {
    setStatus(windowErrorSentence(window_.error), "error");
    return;
  }
  // A pin can be dropped anywhere the map goes, and `/api/search` bounds its
  // inputs to a Manhattan-ish box: a pin in Staten Island came back 422
  // `validation_error` and was reported as "check the date, the time, and the
  // walk radius", which is a sentence about three things that were all fine.
  // Inside the box the server's own 250 m centerline test still decides
  // (docs/API.md, docs/DECISIONS.md D28) and lands on the same notice.
  if (outsideCoverage(where)) {
    clearResults();
    searchCard.expand();
    showOutsideCoverage();
    return;
  }
  runSearch(where, window_);
}

/** Clearly outside the coverage box `/api/health` published. Never the last word. */
function outsideCoverage(where) {
  if (!state.coverage || typeof where.lat !== "number" || typeof where.lon !== "number") {
    return false;
  }
  const [minLon, minLat, maxLon, maxLat] = state.coverage;
  return where.lon < minLon || where.lon > maxLon || where.lat < minLat || where.lat > maxLat;
}

function destinationFor(text) {
  if (state.resolved && text === state.resolvedText) {
    return { lat: state.resolved.lat, lon: state.resolved.lon };
  }
  if (text !== "") {
    return { address: text };
  }
  if (state.resolved && !state.pinMode) {
    return { lat: state.resolved.lat, lon: state.resolved.lon };
  }
  return null;
}

function windowErrorSentence(code) {
  if (code === "too_long") {
    return WINDOW_TOO_LONG;
  }
  if (code === "backwards") {
    return WINDOW_BACKWARDS;
  }
  return "Pick a date, a start time, and how long you are staying.";
}

async function runSearch(where, window_) {
  // The centred first-load card is a one-time state: once a question has been
  // asked, the rail is a list and the search collapses to its summary.
  dom.railScroll.classList.remove("is-first-load");
  const walkMinutes = searchCard.walkMinutes();
  state.walkMinutes = walkMinutes;
  setSearching(true);
  renderNotice(dom.notices, null);
  // UX_AUDIT P1-9: a second search used to replace the answer with skeletons
  // before the server had said anything, so the screen went blank for 2.6 s and
  // the user could not compare the two. The previous answer stays and dims.
  if (state.results.length > 0) {
    setStale(true);
  } else {
    renderSkeleton(dom.results);
  }
  dom.resultsHeading.hidden = false;
  try {
    const response = await api.search({
      ...where,
      t1: window_.t1,
      t2: window_.t2,
      walk_minutes: walkMinutes,
      weights: searchCard.weights(),
      limit: SEARCH_LIMITS.limit,
      map_limit: SEARCH_LIMITS.mapLimit,
    });
    state.window = {
      t1: window_.t1,
      t2: window_.t2,
      start: window_.start,
      end: window_.end,
    };
    showResponse(response, walkMinutes);
  } catch (error) {
    handleSearchError(error);
  } finally {
    setSearching(false);
  }
}

function showResponse(response, walkMinutes) {
  renderCaveats(response.caveats);
  const results = Array.isArray(response.results) ? response.results : [];
  state.results = rankLegal(results, searchCard.weights());
  state.counts = response.counts || null;
  state.resultsById = new Map(results.map((result) => [result.reg_seg_id, result]));
  state.details = new Map();
  state.shown = SHORTLIST_STEP;
  closeDetail({ restoreFocus: false });

  const destination = response.destination;
  if (destination && typeof destination.lat === "number" && typeof destination.lon === "number") {
    // A lat/lon search comes back labelled with its own coordinates, so the
    // label the user picked (or the pin's reverse lookup) wins when it is still
    // the one in the box — UX_AUDIT P1-4 wants the resolved place echoed, not
    // six decimal places.
    const picked = state.resolved && dom.destination.value.trim() === state.resolvedText;
    const label = picked ? state.resolved.label : placeLabel(destination.label || "");
    const raw = picked ? state.resolved.raw || label : destination.label || "";
    state.resolved = { lat: destination.lat, lon: destination.lon, label, raw };
    curbMap.setDestination([destination.lon, destination.lat], state.resolved.label);
    curbMap.setWalkRadius([destination.lon, destination.lat], walkMinutes);
    dom.recentre.hidden = false;
    dom.recentre.setAttribute("aria-label", `Recentre on ${state.resolved.label || "destination"}`);
  }
  curbMap.setCoverage(null);
  curbMap.setResults(state.results);
  curbMap.fitResults();

  searchCard.markSearched();
  // Collapsing the card removes whatever the keyboard was standing on — the
  // Search button that was just pressed, or the destination field Enter was
  // pressed in — and focus falls to <body>. Tab then resumes from the middle of
  // the document: it skipped both skip links and landed on a count pill, where
  // the next Enter presses a verdict filter. The answer is where focus belongs.
  const hadFocus = dom.form.contains(document.activeElement);
  searchCard.collapse(
    state.resolved ? state.resolved.label : dom.destination.value.trim(),
    state.resolved ? state.resolved.raw : "",
  );
  if (hadFocus) {
    dom.results.focus({ preventScroll: true });
  }

  if (state.results.length === 0) {
    dom.resultsHeading.hidden = true;
    renderEmpty(dom.results, walkMinutes);
    setStatus(statusLine(state.counts, 0, walkMinutes));
    drawer.setSummary("Nothing found");
    return;
  }

  dom.resultsHeading.hidden = false;
  renderStats(dom.stats, {
    counts: state.counts,
    visible: state.visibleVerdicts,
    onToggle: (verdict) => toggleVerdict(verdict),
  });
  paintResults();
  setStatus(statusLine(state.counts, state.results.length, walkMinutes));
  const nearest = state.results.find((result) => result.verdict === "legal");
  drawer.setSummary(
    nearest
      ? `${state.counts ? state.counts.legal : state.results.length} can park · nearest ${walkText(nearest.walk_min)}`
      : `${state.results.length} stretches`,
  );
  drawer.open("half");
}

function paintResults() {
  state.cardsById = renderResults(dom.results, {
    results: state.results,
    counts: state.counts,
    walkMinutes: state.walkMinutes,
    shown: state.shown,
    openGroups: state.openGroups,
    visible: state.visibleVerdicts,
    onSelect: (regSegId) => selectSegment(regSegId, { fly: true }),
    onHover: (regSegId) => setHover(regSegId),
    onShowMore: () => {
      state.shown += SHORTLIST_STEP;
      paintResults();
    },
    // A group's cards are built the first time it opens, so the selection ring
    // has to be re-applied to cards that did not exist a moment ago.
    onGroupFilled: () => markSelected(state.cardsById, state.selectedId),
  });
  markSelected(state.cardsById, state.selectedId);
}

/**
 * Press one count pill: stop drawing that verdict and stop listing it.
 *
 * The last pill cannot be pressed out — an empty map after four presses is the
 * "nothing here" reading SPEC §11 exists to prevent, and there is no way back
 * from it that does not look like a bug.
 */
function toggleVerdict(verdict) {
  if (state.visibleVerdicts.has(verdict)) {
    if (state.visibleVerdicts.size === 1) {
      return;
    }
    state.visibleVerdicts.delete(verdict);
  } else {
    state.visibleVerdicts.add(verdict);
  }
  curbMap.setVisibleVerdicts(state.visibleVerdicts);
  updateStats(dom.stats, state.visibleVerdicts);
  paintResults();
}

/**
 * An error clears the previous answer.
 *
 * Results for the old destination left on the map under an error banner read as
 * the answer to the new question (UX_AUDIT (f) 8). The form keeps its values so
 * the search can be retried without retyping.
 */
function handleSearchError(error) {
  clearResults();
  searchCard.expand();
  if (error.code === "outside_coverage") {
    showOutsideCoverage();
    return;
  }
  renderNotice(dom.notices, {
    kind: "error",
    title: error.code === "network_error" ? "No answer from the server" : "That search did not run",
    text: error.code === "network_error" ? SERVER_UNREACHABLE : errorSentence(error.code),
    actionLabel: "Retry",
    onAction: () => dom.form.requestSubmit(),
  });
  // The notice block is the live region for failures, so the status line does
  // not repeat it; it goes back to saying nothing until there is an answer.
  setStatus("");
  if (error.code === "network_error" || error.code === "timeout") {
    showToast(dom.toasts, "CurbCheck could not reach its server.");
  }
}

function showOutsideCoverage() {
  renderNotice(dom.notices, {
    kind: "error",
    title: OUTSIDE_COVERAGE,
    text: OUTSIDE_COVERAGE_DETAIL,
  });
  setStatus("");
  if (state.coverage) {
    curbMap.setCoverage(state.coverage);
    curbMap.fitCoverage(state.coverage);
  }
}

function clearResults() {
  state.results = [];
  state.counts = null;
  state.window = null;
  state.resultsById = new Map();
  state.details = new Map();
  state.cardsById = new Map();
  state.shown = SHORTLIST_STEP;
  closeDetail({ restoreFocus: false });
  clear(dom.results);
  clear(dom.stats);
  clear(dom.caveats);
  dom.resultsHeading.hidden = true;
  curbMap.clearResults();
  drawer.setSummary("Search");
}

/**
 * The search-level caveats, collapsed to one line.
 *
 * UX_AUDIT (f) 5 forbids hiding a caveat behind a disclosure **on a displayed
 * verdict**, and that is untouched: every detail sheet still prints all of them
 * open, above the signs, in "Before you park". These are the same sentences
 * repeated for the whole search, and as two paragraphs at the top of the rail
 * they pushed the first result off the screen.
 */
function renderCaveats(caveats) {
  const items = Array.isArray(caveats) && caveats.length > 0 ? caveats : [TEMPORARY_SIGNAGE_CAVEAT];
  const { root, body } = collapsible({
    label: `${items.length} ${items.length === 1 ? "thing" : "things"} to know`,
    className: "caveat-disclosure",
    toggleClassName: "caveat-toggle",
  });
  body.append(
    el(
      "ul",
      { className: "caveats" },
      items.map((caveat) => el("li", { text: caveat })),
    ),
  );
  replaceChildren(dom.caveats, [root]);
}

/* ---- Selection and the detail sheet ------------------------------------ */

function selectSegment(regSegId, { fly }) {
  const result = state.resultsById.get(regSegId) || null;
  if (state.selectedId !== regSegId) {
    state.lastFocused = document.activeElement;
  }
  state.selectedId = regSegId;
  curbMap.setSelected(regSegId);
  if (fly) {
    curbMap.flyToSegment(regSegId);
  }
  markSelected(state.cardsById, regSegId);
  const card = state.cardsById.get(regSegId);
  if (card) {
    card.scrollIntoView({ block: "nearest" });
  }

  const cached = state.details.get(regSegId);
  showDetail({ result, detail: cached || null, error: null, loading: !cached, regSegId });
  if (!cached) {
    loadDetail(regSegId, result);
  }
}

async function loadDetail(regSegId, result) {
  try {
    const detail = await api.segment(regSegId, state.window);
    state.details.set(regSegId, detail);
    if (state.selectedId !== regSegId) {
      return;
    }
    showDetail({ result, detail, error: null, loading: false, regSegId, keepFocus: true });
  } catch (error) {
    if (state.selectedId === regSegId) {
      showDetail({
        result,
        detail: null,
        error: errorSentence(error.code),
        loading: false,
        regSegId,
        keepFocus: true,
      });
    }
  }
}

function showDetail({ result, detail, error, loading, regSegId, keepFocus = false }) {
  // The sheet is rebuilt when the segment call returns, which destroys whatever
  // inside it had focus; re-anchoring on Close keeps the keyboard in the sheet.
  const focusWasInside = dom.detail.contains(document.activeElement);
  const close = renderDetail(dom.detail, {
    result,
    detail,
    error,
    loading,
    label: streetLabel(result || { reg_seg_id: regSegId }, detail),
    window: state.window,
    onClose: () => closeDetail({ restoreFocus: true }),
  });
  updatePadding();
  // Focus moves into the sheet on open so a screen reader lands on the verdict
  // rather than announcing nothing at all (UX_AUDIT P1-7).
  if (!keepFocus || focusWasInside) {
    close.focus();
  }
}

function closeDetail({ restoreFocus }) {
  const had = state.selectedId !== null;
  state.selectedId = null;
  curbMap.setSelected(null);
  hideDetail(dom.detail);
  markSelected(state.cardsById, null);
  updatePadding();
  if (had && restoreFocus && state.lastFocused && state.lastFocused.isConnected) {
    state.lastFocused.focus();
  }
}

/* ---- The About & data sheet -------------------------------------------- */

function openAbout() {
  state.aboutOpener = document.activeElement;
  const close = renderAbout(dom.about, { onClose: () => closeAbout() });
  updatePadding();
  close.focus();
}

function closeAbout() {
  hideAbout(dom.about);
  updatePadding();
  if (state.aboutOpener && state.aboutOpener.isConnected) {
    state.aboutOpener.focus();
  }
  state.aboutOpener = null;
}

/* ---- Chrome ------------------------------------------------------------ */

/**
 * One hover, both directions (UX_AUDIT P2-9).
 *
 * Hovering a card lit its line already; hovering a line did nothing to the
 * list, so the two halves of the same answer never pointed at each other. The
 * card is highlighted, not scrolled to: a list that jumps under the cursor on
 * every mousemove across 588 spans is worse than one that does not move.
 */
function setHover(regSegId) {
  if (state.hoverId === regSegId) {
    return;
  }
  const previous = state.cardsById.get(state.hoverId);
  if (previous) {
    previous.classList.remove("is-hovered");
  }
  state.hoverId = regSegId;
  curbMap.setHover(regSegId);
  const card = state.cardsById.get(regSegId);
  if (card) {
    card.classList.add("is-hovered");
  }
}

function setStatus(message, kind = "info") {
  dom.status.textContent = message;
  dom.status.classList.toggle("status-error", kind === "error");
}

function setSearching(searching) {
  state.searching = searching;
  dom.searchButton.disabled = searching;
  dom.searchButton.textContent = searching ? "Searching…" : "Search";
  dom.railProgress.hidden = !searching;
  if (!searching) {
    setStale(false);
  }
}

/** Dim the answer on screen — in the rail and on the map — but keep it. */
function setStale(stale) {
  // The count sentence and the caveats describe the answer being replaced, so
  // they dim with it. Left bright they were the most authoritative-looking line
  // in the rail while being the one most certainly about the previous search.
  for (const node of [dom.results, dom.stats, dom.status, dom.caveats]) {
    node.classList.toggle("is-stale", stale);
  }
  curbMap.setStale(stale);
}

function setPinMode(enabled) {
  state.pinMode = enabled;
  curbMap.setPinMode(enabled);
  dom.pinReadout.textContent = enabled ? PIN_MODE_PROMPT : "";
  if (enabled) {
    dom.destination.value = "";
    state.resolved = null;
    state.resolvedText = "";
    drawer.collapse();
  }
}

async function dropPin([lon, lat]) {
  const coordinates = `${lat.toFixed(5)}, ${lon.toFixed(5)}`;
  state.resolved = { lat, lon, label: coordinates, raw: coordinates };
  curbMap.setDestination([lon, lat], state.resolved.label);
  curbMap.setWalkRadius([lon, lat], searchCard.walkMinutes());
  setPinMode(false);
  showToast(dom.toasts, "Pin set");
  try {
    const place = await api.reverse(lat, lon);
    const raw = place && place.label ? nearLabel(place.label) : coordinates;
    const label = placeLabel(raw);
    state.resolved.label = label;
    state.resolved.raw = raw;
    state.resolvedText = label;
    dom.destination.value = label;
    dom.destination.title = raw;
    dom.pinReadout.textContent = place && place.secondary ? place.secondary : "";
    curbMap.setDestination([lon, lat], label);
  } catch {
    // No reverse label is not a failure: the coordinates are the destination,
    // and the search runs on them either way.
    state.resolvedText = state.resolved.label;
    dom.destination.value = state.resolved.label;
  }
}

function pickCandidate(candidate) {
  // Display label in the box, the geocoder's own string kept beside it: DOT and
  // CSCL shout every name, and the box is read next to the title-cased cards.
  const label = placeLabel(candidate.label);
  state.resolved = { lat: candidate.lat, lon: candidate.lon, label, raw: candidate.label || "" };
  state.resolvedText = label;
  dom.destination.value = label;
  dom.destination.title = candidate.label || "";
  // setPinMode clears the readout line, so the secondary label goes in after it.
  setPinMode(false);
  dom.pinReadout.textContent = candidate.secondary || "";
  curbMap.setDestination([candidate.lon, candidate.lat], label);
  curbMap.setWalkRadius([candidate.lon, candidate.lat], searchCard.walkMinutes());
  curbMap.centerOn([candidate.lon, candidate.lat], 15);
  // Focus stays in the field: moving it to Search during the Enter keydown lets
  // the same keystroke's keypress reach the button and submit the form before
  // the user has seen what was picked.
}

/** Keep the map's fitBounds — and the map's own chrome — clear of the sheet. */
function updatePadding() {
  const phone = drawer.isPhone();
  const railWidth = phone ? 0 : dom.rail.getBoundingClientRect().width + 32;
  const sheet = dom.detail.hidden ? dom.about : dom.detail;
  const sheetWidth = sheet.hidden || phone ? 0 : sheet.getBoundingClientRect().width + 32;
  // The sheet floats over the map's right edge, where the attribution, the
  // Recentre button and MapLibre's zoom controls live. On a phone it covers the
  // whole map by design and there is nowhere to step aside to.
  dom.mapArea.style.setProperty("--sheet-offset", `${sheetWidth}px`);
  curbMap.setPadding({
    top: 40,
    bottom: phone ? dom.rail.getBoundingClientRect().height + 24 : 40,
    left: Math.max(40, railWidth),
    right: Math.max(40, sheetWidth),
  });
}

async function reportHealth() {
  try {
    const health = await api.health();
    if (health.coverage && health.coverage.bbox) {
      state.coverage = health.coverage.bbox;
    }
    const signs = typeof health.sign_count === "number" ? health.sign_count : 0;
    if (!health.db_present || signs === 0) {
      renderNotice(dom.notices, {
        kind: "error",
        title: "No parking database yet",
        text: "Run `curbcheck sync` to build data/curbcheck.sqlite, then reload.",
      });
      return;
    }
    // `degraded` with a database present means the calendar did not survive the
    // sync; searching still works, so this is a warning, not a refusal.
    if (health.calendar_missing) {
      renderNotice(dom.notices, {
        kind: "warn",
        title: "Holiday calendar missing",
        text: `${CALENDAR_MISSING_CAVEAT}. Searching still works.`,
      });
      return;
    }
    setStatus(`Ready. ${signs.toLocaleString("en-US")} signs loaded.`);
  } catch (error) {
    renderNotice(dom.notices, {
      kind: "error",
      title: "No answer from the server",
      text: error.code === "network_error" ? SERVER_UNREACHABLE : errorSentence(error.code),
    });
  }
}

function reportMapError(message) {
  // Missing glyph ranges are expected for non-Latin labels (web/basemap/MANIFEST.md)
  // and are not worth a banner; anything else means the basemap is broken.
  if (message.includes(".pbf")) {
    return;
  }
  setStatus(`Basemap problem: ${message}`, "error");
}

/* ---- Boot -------------------------------------------------------------- */

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
  onHover: (regSegId) => setHover(regSegId),
});

const drawer = createDrawer({
  rail: dom.rail,
  handle: dom.drawerHandle,
  summary: dom.drawerSummary,
  onDetent: () => updatePadding(),
});

const searchCard = createSearchCard({
  dom,
  onRerank: (weights) => {
    if (state.results.length === 0) {
      return;
    }
    state.results = rankLegal(state.results, weights);
    state.shown = SHORTLIST_STEP;
    paintResults();
  },
  onWalkChange: (minutes) => {
    if (state.resolved) {
      curbMap.setWalkRadius([state.resolved.lon, state.resolved.lat], minutes);
    }
  },
  onStaleChange: () => {},
});

createAutocomplete({
  input: dom.destination,
  list: dom.autocompleteList,
  status: dom.autocompleteStatus,
  onPick: (candidate) => pickCandidate(candidate),
  onDropPin: () => setPinMode(true),
});

renderLegend(dom.legend);

dom.aboutOpen.addEventListener("click", () => openAbout());

dom.recentre.addEventListener("click", () => {
  if (state.resolved) {
    curbMap.recentre([state.resolved.lon, state.resolved.lat]);
  }
});

dom.form.addEventListener("submit", onSubmit);
dom.summaryEdit.addEventListener("click", () => searchCard.expand());
dom.destination.addEventListener("input", () => {
  if (dom.destination.value !== state.resolvedText) {
    state.resolved = null;
    dom.destination.title = "";
  }
  if (dom.destination.value !== "" && state.pinMode) {
    setPinMode(false);
  }
});

dom.noticeToggle.addEventListener("click", () => {
  const open = dom.noticeToggle.getAttribute("aria-expanded") === "true";
  dom.noticeToggle.setAttribute("aria-expanded", String(!open));
  dom.fullNotice.hidden = open;
  updatePadding();
});

document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") {
    return;
  }
  if (!dom.about.hidden) {
    closeAbout();
    return;
  }
  if (state.selectedId !== null) {
    closeDetail({ restoreFocus: true });
  }
});

window.addEventListener("resize", () => {
  curbMap.resize();
  updatePadding();
});

// On a phone the drawer's peek detent shows two lines, which is not enough to
// reach the destination field. First load is the one moment the user has
// nothing to look at on the map, so the drawer opens on the question.
drawer.open("half");
drawer.setSummary("Where to?");

updatePadding();
reportHealth();
