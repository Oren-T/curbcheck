/**
 * The "About & data" sheet: where every number on screen came from, when it was
 * last refreshed, who owns it, and the whole of SPEC §17.
 *
 * DESIGN_DIRECTION §3 asks for this next to the attribution, and UX_AUDIT (f) 1
 * lets the strip be short only while the full notice stays one interaction
 * away. Until now the disclosure in the strip was the only place it lived; this
 * is the second, and the one that also answers "how old is this?".
 *
 * The snapshot date is read from `/api/sync-status` when the sheet opens rather
 * than at boot: it is one small request, it is only interesting to someone who
 * asked this question, and a stale answer here would be its own joke.
 */

import * as api from "./api.js";
import { DISCLAIMER } from "./copy.js";
import { clear, definition, el, replaceChildren } from "./dom.js";

/**
 * The datasets behind a verdict, in the order they contribute to one
 * (docs/DATA.md). Socrata ids are printed because they are how a reader checks
 * the claim themselves.
 */
const SOURCES = [
  {
    name: "Parking Regulation Locations and Signs",
    id: "nfid-uabd",
    role: "every sign, its description text, and where DOT says it stands",
  },
  {
    name: "Street Centerline (LION)",
    id: "inkn-q76z",
    role: "the block the sign is snapped to and the geometry drawn on the map",
  },
  {
    name: "Parking Meters — ParkNYC Block Faces",
    id: "e7yp-wx55",
    role: "which stretches are metered",
  },
  {
    name: "Parking Meters — Citywide Rate Zones",
    id: "f72k-2u3b",
    role: "the hourly rate a price is computed from",
  },
  {
    name: "AddressPoint and CommonPlace",
    id: "uf93-f8nk, t95h-5fsr",
    role: "the address search",
  },
  {
    name: "ASP suspension calendar (nyc.gov)",
    id: "not on Open Data",
    role: "holiday and street-cleaning suspensions",
  },
];

/** "15 Sep 2026, 08:06" from an ISO timestamp, or null when there is none. */
function formatTimestamp(raw) {
  if (typeof raw !== "string" || raw === "") {
    return null;
  }
  const when = new Date(raw);
  if (Number.isNaN(when.getTime())) {
    return raw;
  }
  return when.toLocaleString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function sourceList() {
  const list = el("ul", { className: "source-list" });
  for (const source of SOURCES) {
    list.append(
      el("li", {}, [
        el("span", { className: "source-name", text: source.name }),
        el("span", { className: "source-id", text: source.id }),
        el("span", { className: "source-role", text: source.role }),
      ]),
    );
  }
  return list;
}

/**
 * Open the sheet. Returns the Close button so the caller can move focus to it.
 *
 * @param {HTMLElement} container the `role="dialog"` element
 * @param {{onClose: Function}} view
 * @returns {HTMLElement}
 */
export function renderAbout(container, view) {
  clear(container);
  container.hidden = false;

  const close = el("button", {
    className: "detail-close",
    text: "Close",
    attrs: { type: "button" },
  });
  close.addEventListener("click", () => view.onClose());

  const snapshot = el("dl", { className: "facts" }, definition("Data snapshot", "Reading…"));
  // Filled in only when the backend says this is the static copy; removed
  // otherwise, so the local server's sheet is exactly what it was.
  const hosting = el("section", { className: "about-hosting" });
  const scroll = el("div", { className: "detail-scroll" }, [
    el("section", {}, [
      el("h3", { text: "Data snapshot" }),
      el("p", {
        className: "about-lede",
        text:
          "CurbCheck answers from a local copy of NYC Open Data. Nothing on this page " +
          "is fetched live, and no search leaves this browser.",
      }),
      snapshot,
    ]),
    hosting,
    el("section", {}, [el("h3", { text: "Where the answer comes from" }), sourceList()]),
    el("section", {}, [
      el("h3", { text: "Credit and licence" }),
      el("p", {
        text:
          "Data © NYC Open Data (NYC DOT, NYC DCP), used under the NYC Open Data terms of use. " +
          "Basemap © OpenStreetMap contributors, tiles built with Protomaps (BSD-3-Clause). " +
          "Map rendering by MapLibre GL JS (BSD-3-Clause). Typeface: Inter, SIL Open Font " +
          "License 1.1. CurbCheck itself is open source under the MIT License.",
      }),
    ]),
    el("section", {}, [
      el("h3", { text: "The full notice" }),
      el("p", { className: "about-disclaimer", text: DISCLAIMER }),
    ]),
  ]);

  container.append(
    el("div", { className: "detail-header" }, [
      el("div", { className: "detail-heading" }, [
        el("h2", { className: "detail-title", text: "About & data", attrs: { id: "about-title" } }),
        el("p", { className: "detail-sub", text: "Sources, snapshot, and the full notice" }),
      ]),
      close,
    ]),
    scroll,
  );

  loadSnapshot(snapshot, container);
  loadHosting(hosting, container);
  return close;
}

/**
 * Fill in "How this copy is served" when `health` carries a `hosting` block.
 *
 * Only the static build sets one (docs/STATIC_SITE.md); it is where a visitor
 * reads what the host can see about them, which on a machine serving itself is
 * nothing and therefore not worth a paragraph. The sentences are the worker's,
 * printed as sent — `site/PRIVACY.md` is the long form of the same thing.
 */
async function loadHosting(node, container) {
  let hosting = null;
  try {
    hosting = (await api.health()).hosting;
  } catch {
    // app.js already surfaced a failing health call; a second copy of that
    // message inside this sheet would not tell the reader anything new.
    hosting = null;
  }
  if (container.hidden || !hosting || hosting.kind !== "static" || !hosting.text) {
    node.remove();
    return;
  }
  const built = formatTimestamp(hosting.built_at);
  replaceChildren(node, [
    el("h3", { text: "How this copy is served" }),
    el("p", { text: hosting.text }),
    built ? el("dl", { className: "facts" }, definition("Data build", built)) : null,
  ]);
}

/**
 * Fill in the snapshot row once `/api/sync-status` answers.
 *
 * A failure prints a sentence rather than a blank: "we do not know how old this
 * is" is itself the answer to the question the sheet was opened to ask.
 */
async function loadSnapshot(node, container) {
  let rows;
  try {
    const status = await api.syncStatus();
    const text = formatTimestamp(status && status.last_sync_at);
    rows = [
      ...definition("Last synced", text || "unknown"),
      ...definition("Signs loaded", countText(status, "signs_loaded")),
      ...definition("Street segments", countText(status, "street_segments")),
      ...definition(
        "Suspension dates in the calendar",
        countText(status, "calendar_distinct_dates"),
      ),
    ];
  } catch {
    rows = definition("Last synced", "The server did not say. Treat this copy as undated.");
  }
  if (!container.hidden) {
    replaceChildren(node, rows);
  }
}

/** `sync_meta` values arrive as strings (docs/API.md); print them or nothing. */
function countText(status, key) {
  const raw = status && status[key];
  if (raw === undefined || raw === null || raw === "") {
    return null;
  }
  const count = Number(raw);
  return Number.isFinite(count) ? count.toLocaleString("en-US") : String(raw);
}

export function hideAbout(container) {
  clear(container);
  container.hidden = true;
}
