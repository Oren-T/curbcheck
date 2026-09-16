/**
 * Reading a pack off the disk, for `node --test`.
 *
 * The worker builds the same `readText` from `fetch` and
 * `DecompressionStream("gzip")`; here it is `zlib`, so the tests read exactly
 * the bytes `curbcheck pack` wrote.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { gunzipSync } from "node:zlib";

/**
 * @param {string} dir a directory holding meta.json and the three pack files
 * @returns {(name: string) => Promise<string>} the `readText` `loadPack` takes
 */
export function readPackText(dir) {
  return async function readText(name) {
    const bytes = readFileSync(join(dir, name));
    return name.endsWith(".gz") ? gunzipSync(bytes).toString("utf8") : bytes.toString("utf8");
  };
}
