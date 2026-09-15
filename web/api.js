/**
 * Thin wrappers over the local JSON API (docs/API.md).
 *
 * Every call is one-shot, same-origin, and aborted on a timeout so a hung
 * request cannot leave the UI stuck in "searching". Errors always arrive as an
 * `ApiError` carrying the server's `code`, so callers can branch on the code
 * rather than on message text.
 */

const DEFAULT_TIMEOUT_MS = 20_000;

/** An error from the API, or from failing to reach it. */
export class ApiError extends Error {
  constructor(message, { code = "network_error", status = 0 } = {}) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
  }
}

async function request(
  path,
  { method = "GET", body = null, timeoutMs = DEFAULT_TIMEOUT_MS, signal = null } = {},
) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  // A caller that supersedes its own request (the autocomplete does, on every
  // keystroke) passes a signal; aborting it has to abort the fetch as well.
  if (signal) {
    if (signal.aborted) {
      controller.abort();
    } else {
      signal.addEventListener("abort", () => controller.abort(), { once: true });
    }
  }
  let response;
  try {
    response = await fetch(path, {
      method,
      headers:
        body === null
          ? { Accept: "application/json" }
          : {
              Accept: "application/json",
              "Content-Type": "application/json",
            },
      body: body === null ? undefined : JSON.stringify(body),
      signal: controller.signal,
      cache: "no-store",
      credentials: "same-origin",
    });
  } catch (error) {
    if (signal && signal.aborted) {
      throw new ApiError("superseded", { code: "aborted" });
    }
    const timedOut = controller.signal.aborted;
    throw new ApiError(
      timedOut
        ? `The server did not answer within ${Math.round(timeoutMs / 1000)} seconds.`
        : `Could not reach the CurbCheck server: ${error.message}`,
      { code: timedOut ? "timeout" : "network_error" },
    );
  } finally {
    clearTimeout(timer);
  }

  const payload = await readJson(response);
  if (!response.ok) {
    const detail = payload && payload.error ? payload.error : {};
    throw new ApiError(detail.message || `Request failed (HTTP ${response.status}).`, {
      code: detail.code || "http_error",
      status: response.status,
    });
  }
  return payload;
}

async function readJson(response) {
  const text = await response.text();
  if (text === "") {
    return null;
  }
  try {
    return JSON.parse(text);
  } catch {
    // A non-JSON body means something other than the API answered (a proxy, a
    // stray static file). Report it as such instead of leaking the body.
    throw new ApiError("The server returned a response that was not JSON.", {
      code: "bad_response",
      status: response.status,
    });
  }
}

/** POST /api/search. `body` is `{lat, lon} | {address}` plus t1, t2, walk_minutes, weights. */
export function search(body, options = {}) {
  return request("/api/search", { method: "POST", body, ...options });
}

/** GET /api/segment/{id} — the full rule stack and raw sign text behind one verdict. */
export function segment(regSegId, options = {}) {
  return request(`/api/segment/${encodeURIComponent(regSegId)}`, options);
}

/** GET /api/geocode?q= — local geocoder; an empty candidate list is a 200, not an error. */
export function geocode(query, options = {}) {
  return request(`/api/geocode?q=${encodeURIComponent(query)}`, { timeoutMs: 5000, ...options });
}

/** GET /api/reverse?lat=&lon= — the label for a dropped pin ("near 1519 3 AVE"). */
export function reverse(lat, lon, options = {}) {
  const query = new URLSearchParams({ lat: String(lat), lon: String(lon) });
  return request(`/api/reverse?${query.toString()}`, { timeoutMs: 5000, ...options });
}

/** GET /api/health — answers even when the database is missing. */
export function health(options = {}) {
  return request("/api/health", { timeoutMs: 5000, ...options });
}

/** GET /api/sync-status — the sync_meta table, as strings. */
export function syncStatus(options = {}) {
  return request("/api/sync-status", { timeoutMs: 5000, ...options });
}
