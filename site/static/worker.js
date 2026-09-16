/**
 * The shell the six operations run inside.
 *
 * It owns exactly two things: reading the pack files that sit next to it, and
 * moving messages between `api.js` and `handlers.js`. Everything it fetches is
 * same-origin and relative to this file, so the worker makes no request the page
 * itself could not (docs/SECURITY.md T6).
 *
 * Envelope, one message per call:
 *   -> { id, op, args }
 *   <- { id, ok: true, result } | { id, ok: false, error: { code, message } }
 */

import { loadPack } from "./pack/loader.js";
import { loadCalendar } from "./engine/calendar.js";
import { buildGeocodeIndex } from "./geocode/index.js";
import { HandlerError, handle } from "./handlers.js";

const PACK_UNAVAILABLE_MESSAGE =
  "CurbCheck could not load its parking data. Reload the page; if it keeps failing the " +
  "published data files are missing or corrupt.";

// One rejected promise for the whole worker: if the pack cannot be read, every
// message gets `pack_unavailable` rather than the first one getting the real
// failure and the rest getting nothing.
const ready = start().catch(() => {
  throw new HandlerError("pack_unavailable", PACK_UNAVAILABLE_MESSAGE);
});

async function start() {
  const pack = await loadPack(readText);
  // Both of these cost more to build than any single request is allowed to, and
  // neither changes while the page is open, so the first keystroke pays nothing.
  return { pack, state: { geocodeIndex: buildGeocodeIndex(pack), calendar: loadCalendar(pack) } };
}

/**
 * The inflated text of one pack file.
 *
 * `meta.json` is small and names the hashed files, so it is fetched fresh; the
 * hashed files are content-addressed and may be served from the HTTP cache.
 * A `.gz` file is inflated here rather than by the host: GitHub Pages documents
 * no size ceiling for its own compression, and a 27 MB file that ships
 * uncompressed is a 27 MB first load.
 */
async function readText(name) {
  const url = new URL(`pack/${name}`, import.meta.url);
  const response = await fetch(url, name.endsWith(".gz") ? {} : { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`pack file ${name}: HTTP ${response.status}`);
  }
  if (!name.endsWith(".gz")) {
    return response.text();
  }
  const inflated = response.body.pipeThrough(new DecompressionStream("gzip"));
  return new Response(inflated).text();
}

self.addEventListener("message", async (event) => {
  const message = event.data || {};
  try {
    const { pack, state } = await ready;
    self.postMessage({
      id: message.id,
      ok: true,
      result: handle(message.op, message.args, pack, state),
    });
  } catch (error) {
    self.postMessage({ id: message.id, ok: false, error: errorPayload(error) });
  }
});

/**
 * The failure the caller sees. An error that is not a `HandlerError` is a bug in
 * this build, so it is reported as the server's `internal_error` with the
 * server's constant message: the details go to the worker console, never into
 * the answer (SPEC §3.4).
 */
function errorPayload(error) {
  if (error instanceof HandlerError) {
    return { code: error.code, message: error.message };
  }
  console.error(error);
  return { code: "internal_error", message: "internal error" };
}
