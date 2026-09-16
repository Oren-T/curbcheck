/**
 * The differential gate: the Python engine's answers, replayed through the JS one.
 *
 * `make_fixtures.py` has already called `curbcheck.api.routes`' own handlers for
 * every query in `queries.py` and written the API JSON. This file loads the pack
 * the same way the worker does, runs the same queries through `handlers.handle`,
 * and compares. Floats are equal within a relative 1e-9 -- an ulp between GEOS
 * and the ported point-to-segment distance is allowed and nothing else is.
 * Verdicts, strings, ids, key order, counts and error codes are exact.
 *
 * Three steps, in order (site/tests/harness/README.md):
 *   curbcheck pack --out build/pack
 *   python site/tests/harness/make_fixtures.py --pack build/pack
 *   node --test site/tests
 *
 * Skips, loudly, when either input is missing, so `make check` on a clone with
 * no database still runs the rest of the suite.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = new URL("./", import.meta.url);
// The workflow builds the pack into `build/pack` at the repository root and
// leaves the fixture next to this file; both are overridable for a local run.
const PACK_DIR = process.env.PACK_DIR || fileURLToPath(new URL("../../../build/pack", HERE));
const EXPECTED = process.env.EXPECTED || fileURLToPath(new URL("expected.json", HERE));

// GEOS and the JS agree to the last few bits on a point-to-segment distance,
// not to the last bit; every other number in a payload is either integer or
// copied through, so this only ever forgives a distance.
const RELATIVE_TOLERANCE = 1e-9;
// Reported per op. A wholly wrong op would otherwise print a line per field.
const MAX_REPORTED = 20;
// Stop descending into one query once it has this many differences: the first
// few name the cause, the rest are the same cause seen again.
const MAX_PER_QUERY = 8;

const OPS = ["search", "segment", "geocode", "reverse"];

const missing = [];
if (!existsSync(join(PACK_DIR, "meta.json"))) {
  missing.push(`no pack at ${PACK_DIR} (run \`curbcheck pack --out build/pack\`, or set PACK_DIR)`);
}
if (!existsSync(EXPECTED)) {
  missing.push(
    `no fixture at ${EXPECTED} (run site/tests/harness/make_fixtures.py, or set EXPECTED)`,
  );
}

if (missing.length > 0) {
  test.skip(`differential harness: ${missing.join("; ")}`, () => {});
} else {
  const { loadPack } = await import(new URL("../../static/pack/loader.js", HERE).href);
  const { readPackText } = await import(new URL("../helpers/read_pack.js", HERE).href);
  const handlers = await import(new URL("../../static/handlers.js", HERE).href);

  const pack = await loadPack(readPackText(PACK_DIR));
  const state = await buildState(pack, handlers);
  const fixture = JSON.parse(readFileSync(EXPECTED, "utf8"));

  for (const op of OPS) {
    const queries = fixture.queries.filter((query) => query.op === op);
    test(`${op}: ${queries.length} queries match the Python engine`, () => {
      const failures = [];
      let matched = 0;
      for (const query of queries) {
        const differences = compareQuery(query, pack, state, handlers);
        if (differences.length === 0) {
          matched += 1;
        } else {
          failures.push(...differences.map((line) => `${query.id} ${line}`));
        }
      }
      const summary = `${op}: ${matched} of ${queries.length} matched`;
      if (failures.length > 0) {
        assert.fail([summary, ...failures.slice(0, MAX_REPORTED)].join("\n"));
      }
      console.log(summary);
    });
  }
}

/**
 * What the worker builds once after `loadPack`, and what `handle` expects as
 * its fourth argument. `handlers.js` may grow a state builder of its own; until
 * it does, this is `worker.js:start` line for line.
 */
async function buildState(pack, handlers) {
  if (typeof handlers.buildState === "function") {
    return await handlers.buildState(pack);
  }
  const { buildGeocodeIndex } = await import(new URL("../../static/geocode/index.js", HERE).href);
  const { loadCalendar } = await import(new URL("../../static/engine/calendar.js", HERE).href);
  return { geocodeIndex: buildGeocodeIndex(pack), calendar: loadCalendar(pack) };
}

/** The differences between one fixture entry and what the JS engine answers. */
function compareQuery(query, pack, state, handlers) {
  let actual;
  let thrown = null;
  try {
    actual = handlers.handle(query.op, query.args, pack, state);
  } catch (error) {
    thrown = error;
  }

  if (query.error) {
    if (thrown === null) {
      return [`expected error ${query.error.code}, got a result ${describe(actual)}`];
    }
    if (thrown.code !== query.error.code) {
      return [`expected error ${query.error.code}, got ${errorText(thrown)}`];
    }
    return [];
  }
  if (thrown !== null) {
    return [`expected a result, got ${errorText(thrown)}`];
  }
  const differences = [];
  compare(query.result, actual, "", differences);
  return differences;
}

/**
 * Walk both values together, appending "path: expected X, got Y" for each
 * difference. Numbers are compared with the tolerance above; objects by key
 * order as well as by key, because the API's key order is part of its contract
 * and a missing field is easier to see next to the field that displaced it.
 */
function compare(expected, actual, path, out) {
  if (out.length >= MAX_PER_QUERY) {
    return;
  }
  const where = path || "$";
  if (typeof expected === "number" && typeof actual === "number") {
    if (!numbersMatch(expected, actual)) {
      out.push(`${where}: expected ${expected}, got ${actual}`);
    }
    return;
  }
  if (Array.isArray(expected) || Array.isArray(actual)) {
    if (!Array.isArray(expected) || !Array.isArray(actual)) {
      out.push(`${where}: expected ${describe(expected)}, got ${describe(actual)}`);
      return;
    }
    if (expected.length !== actual.length) {
      out.push(`${where}: expected ${expected.length} items, got ${actual.length}`);
      return;
    }
    for (let index = 0; index < expected.length; index += 1) {
      compare(expected[index], actual[index], `${path}[${index}]`, out);
    }
    return;
  }
  if (isObject(expected) && isObject(actual)) {
    const expectedKeys = Object.keys(expected);
    const actualKeys = Object.keys(actual);
    if (expectedKeys.join(",") !== actualKeys.join(",")) {
      out.push(`${where}: expected keys [${expectedKeys}], got [${actualKeys}]`);
      return;
    }
    for (const key of expectedKeys) {
      compare(expected[key], actual[key], `${path}.${key}`, out);
    }
    return;
  }
  if (expected !== actual) {
    out.push(`${where}: expected ${describe(expected)}, got ${describe(actual)}`);
  }
}

function numbersMatch(expected, actual) {
  if (Object.is(expected, actual)) {
    return true;
  }
  if (!Number.isFinite(expected) || !Number.isFinite(actual)) {
    return false;
  }
  const scale = Math.max(1, Math.abs(expected), Math.abs(actual));
  return Math.abs(expected - actual) <= RELATIVE_TOLERANCE * scale;
}

function isObject(value) {
  return typeof value === "object" && value !== null;
}

function errorText(error) {
  return error && error.code ? `${error.code} (${error.message})` : `${error && error.stack}`;
}

function describe(value) {
  const text = value === undefined ? "undefined" : JSON.stringify(value);
  return text.length > 120 ? `${text.slice(0, 120)}...` : text;
}
