/**
 * Thin wrappers over the in-browser engine (docs/API.md).
 *
 * The drop-in replacement for `web/api.js`: same exports, same `ApiError`, same
 * JSON shapes. The frontend cannot tell which one it loaded — the only
 * difference is that a call crosses a `postMessage` boundary to a same-origin
 * worker instead of a socket, so nothing leaves the machine at all.
 *
 * Every call is one-shot and abandoned on a timeout so a hung answer cannot
 * leave the UI stuck in "searching". Errors always arrive as an `ApiError`
 * carrying the engine's `code`, so callers can branch on the code rather than on
 * message text.
 */

const DEFAULT_TIMEOUT_MS = 20_000;
// The four endpoints a keystroke or a page load waits on. The server's api.js
// gives them the same shorter budget, and so does this one — once the pack is
// loaded. The first `health()` is the call whose job is to wait for the pack:
// 8 MB gzipped, so on a slow connection it is the download, not the engine,
// that takes the time, and every other call queues behind it without a timer.
const QUICK_TIMEOUT_MS = 5000;
const PACK_LOAD_TIMEOUT_MS = 120_000;

/** An error from the engine, or from failing to reach it. */
export class ApiError extends Error {
  constructor(message, { code = "network_error", status = 0 } = {}) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
  }
}

let worker = null;
let dead = false;
let nextId = 1;
const pending = new Map();

/**
 * The worker, started on the first call.
 *
 * Lazily, because the pack is tens of megabytes and a page that never searches
 * should not pay for it, and because starting it inside a call gives the failure
 * somewhere to be reported.
 */
function engine() {
  if (worker === null) {
    worker = new Worker(new URL("./worker.js", import.meta.url), { type: "module" });
    worker.addEventListener("message", (event) => {
      const answer = event.data || {};
      const waiting = pending.get(answer.id);
      if (waiting === undefined) {
        // An aborted or timed-out call: its answer arrives anyway and is dropped.
        return;
      }
      pending.delete(answer.id);
      waiting.finish();
      if (answer.ok) {
        waiting.resolve(answer.result);
        return;
      }
      const detail = answer.error || {};
      waiting.reject(
        new ApiError(detail.message || "The request failed.", {
          code: detail.code || "http_error",
        }),
      );
    });
    worker.addEventListener("error", () => {
      // The worker script itself failed to start, so there is no engine at all.
      // Later calls fail at once rather than posting into the void and timing out.
      dead = true;
      failAll(
        new ApiError("CurbCheck could not start its parking engine.", {
          code: "pack_unavailable",
        }),
      );
    });
  }
  return worker;
}

function failAll(error) {
  for (const waiting of [...pending.values()]) {
    pending.delete(waiting.id);
    waiting.finish();
    waiting.reject(error);
  }
}

let ready = null;

/** Resolves once the worker has answered its first `health`, i.e. the pack is loaded. */
function packReady() {
  if (ready === null) {
    ready = send("health", {}, { timeoutMs: PACK_LOAD_TIMEOUT_MS });
  }
  return ready;
}

async function call(op, args, options = {}) {
  if (op !== "health") {
    // A failed load is reported by the call itself: the worker answers every
    // message with `pack_unavailable` once loading has failed.
    await packReady().catch(() => null);
  }
  return send(op, args, options);
}

function send(op, args, { timeoutMs = DEFAULT_TIMEOUT_MS, signal = null } = {}) {
  return new Promise((resolve, reject) => {
    if (dead) {
      reject(
        new ApiError("CurbCheck could not start its parking engine.", { code: "pack_unavailable" }),
      );
      return;
    }
    const id = nextId;
    nextId += 1;

    const timer = setTimeout(() => {
      pending.delete(id);
      finish();
      reject(
        new ApiError(`The engine did not answer within ${Math.round(timeoutMs / 1000)} seconds.`, {
          code: "timeout",
        }),
      );
    }, timeoutMs);

    // A caller that supersedes its own request (the autocomplete does, on every
    // keystroke) passes a signal; the worker's answer for this id is then
    // dropped when it arrives.
    const onAbort = () => {
      pending.delete(id);
      finish();
      reject(new ApiError("superseded", { code: "aborted" }));
    };
    const finish = () => {
      clearTimeout(timer);
      if (signal) {
        signal.removeEventListener("abort", onAbort);
      }
    };

    if (signal && signal.aborted) {
      finish();
      reject(new ApiError("superseded", { code: "aborted" }));
      return;
    }
    if (signal) {
      signal.addEventListener("abort", onAbort, { once: true });
    }

    pending.set(id, { id, resolve, reject, finish });
    try {
      engine().postMessage({ id, op, args });
    } catch (error) {
      pending.delete(id);
      finish();
      reject(
        new ApiError(`Could not start the CurbCheck engine: ${error.message}`, {
          code: "pack_unavailable",
        }),
      );
    }
  });
}

/** Rank the curb near a destination. `body` is `{lat, lon} | {address}` plus t1, t2, … */
export function search(body, options = {}) {
  return call("search", body, options);
}

/**
 * The full rule stack and raw sign text behind one verdict.
 *
 * `window` is the same `{t1, t2}` the search was run with. With it the engine
 * also says, per rule, how much of the window it is in force for and which one
 * the verdict rests on (docs/API.md).
 */
export function segment(regSegId, window = null, options = {}) {
  const args = window
    ? { reg_seg_id: regSegId, t1: window.t1, t2: window.t2 }
    : { reg_seg_id: regSegId };
  return call("segment", args, options);
}

/** Local geocoder; an empty candidate list is a result, not an error. */
export function geocode(query, options = {}) {
  return call("geocode", { q: query }, { timeoutMs: QUICK_TIMEOUT_MS, ...options });
}

/** The label for a dropped pin ("near 1519 3 AVE"). */
export function reverse(lat, lon, options = {}) {
  return call("reverse", { lat, lon }, { timeoutMs: QUICK_TIMEOUT_MS, ...options });
}

/** Answers once the pack has loaded, and rejects with `pack_unavailable` if it cannot. */
export function health(options = {}) {
  return packReady().then(() => send("health", {}, { timeoutMs: QUICK_TIMEOUT_MS, ...options }));
}

/** The pack's sync_meta, as strings. */
export function syncStatus(options = {}) {
  return call("syncStatus", {}, { timeoutMs: QUICK_TIMEOUT_MS, ...options });
}
