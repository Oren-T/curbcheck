/**
 * `web/format.js`'s window helpers on the two daylight-saving Sundays.
 *
 * A parking window is a wall clock; the browser's clock has an hour that does
 * not happen and an hour that happens twice. The helpers carry the wall clock
 * in a `Date`'s UTC fields so that "midnight plus 24 hours" is the next
 * midnight on both days. This runs in Node under TZ=America/New_York because
 * that is the zone the bug showed in.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

process.env.TZ = "America/New_York";

const format = await import("../../web/format.js");

test("midnight plus 24 hours is the next midnight on the fall-back Sunday", () => {
  const start = format.parseLocal("2026-11-01", "00:00");
  const end = format.addMinutes(start, 24 * 60);
  assert.equal(format.toDateInputValue(end), "2026-11-02");
  assert.equal(format.toTimeInputValue(end), "00:00");
  assert.equal(format.windowSummary(start, end), "Sun Nov 1, 00:00 → Mon Nov 2, 00:00");
});

test("a two-hour chip is two wall hours across both transitions", () => {
  for (const [date, expected] of [
    ["2026-11-01", "03:00"],
    ["2027-03-14", "03:00"],
    ["2026-09-16", "03:00"],
  ]) {
    const start = format.parseLocal(date, "01:00");
    assert.equal(format.toTimeInputValue(format.addMinutes(start, 120)), expected, date);
  }
});

test("the window is sent as the naive strings the inputs held", () => {
  const start = format.parseLocal("2027-03-14", "00:00");
  const end = format.addMinutes(start, 24 * 60);
  assert.equal(
    format.toApiDateTime(format.toDateInputValue(end), format.toTimeInputValue(end)),
    "2027-03-15T00:00",
  );
});

test("the default arrival keeps the browser's own wall clock", () => {
  const now = new Date(2026, 10, 1, 1, 31); // local 01:31 on the fall-back Sunday
  const next = format.nextQuarterHour(now);
  assert.equal(format.toTimeInputValue(next), "01:45");
  assert.equal(format.toDateInputValue(next), "2026-11-01");
});
