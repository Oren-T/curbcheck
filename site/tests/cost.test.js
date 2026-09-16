/**
 * Walk estimate, progressive meter pricing, and the risk term.
 *
 * A port of `tests/test_engine_cost.py`, test for test. The Python's `Decimal`
 * dollars are integer cents here (docs/STATIC_SITE.md, "Money"), so every
 * amount the reference writes as `Decimal("2.75")` is 275.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  FINE_HYDRANT_CLASS,
  FINE_STANDARD,
  WALK_DETOUR_FACTOR,
  WALK_SPEED_M_PER_S,
  Weights,
  citationProbability,
  haversineMeters,
  meterPrice,
  riskDollars,
  totalCost,
  walkMinutes,
  walkMinutesForMeters,
  walkRadiusMeters,
} from "../static/engine/cost.js";

// Manhattan test points: 3rd Ave at E 85th, and a point one block north.
const E85 = [-73.955, 40.779];
const E86 = [-73.955, 40.788];

const M1_RATES = ["5.50", "9.00"];

function approx(actual, expected, tolerance = 1e-9) {
  assert.ok(
    Math.abs(actual - expected) <= tolerance * Math.max(1, Math.abs(expected)),
    `${actual} is not within ${tolerance} of ${expected}`,
  );
}

test("haversine matches a known north south distance", () => {
  // One hundredth of a degree of latitude is about 1,111 m anywhere on Earth.
  approx(haversineMeters([-73.955, 40.779], [-73.955, 40.789]), 1111, 2 / 1111);
});

test("walk minutes applies the detour factor and walking speed", () => {
  const meters = haversineMeters(E85, E86);

  const expected = (meters * WALK_DETOUR_FACTOR) / WALK_SPEED_M_PER_S / 60;

  approx(walkMinutes(E85, E86), expected);
  approx(walkMinutes(E85, E86), walkMinutesForMeters(meters));
});

test("walking nowhere takes no time", () => {
  approx(walkMinutes(E85, E85), 0.0);
});

test("walk radius is the inverse of walk minutes", () => {
  const radius = walkRadiusMeters(10);

  approx(walkMinutesForMeters(radius), 10.0);
});

test("walk radius rejects negative minutes", () => {
  assert.throws(() => walkRadiusMeters(-1), /must not be negative/);
});

test("half an hour costs half the first hour rate", () => {
  assert.strictEqual(meterPrice(M1_RATES, 30), 275);
});

test("a full first hour costs the first hour rate", () => {
  assert.strictEqual(meterPrice(M1_RATES, 60), 550);
});

test("the second hour bills at the second hour rate prorated", () => {
  assert.strictEqual(meterPrice(M1_RATES, 90), 1000);
  assert.strictEqual(meterPrice(M1_RATES, 120), 1450);
});

test("hours beyond the published list bill at the last rate", () => {
  // SPEC §13.2 publishes a first and second hour rate; a third hour repeats the last.
  assert.strictEqual(meterPrice(M1_RATES, 180), 2350);
});

test("a single rate applies to every hour", () => {
  assert.strictEqual(meterPrice(["1.50"], 150), 375);
});

test("zero charged minutes costs nothing", () => {
  assert.strictEqual(meterPrice(M1_RATES, 0), 0);
});

test("pricing without a rate is an error not a free spot", () => {
  assert.throws(() => meterPrice([], 60), /unknown, not zero/);
});

test("negative minutes are rejected", () => {
  assert.throws(() => meterPrice(M1_RATES, -10), /must not be negative/);
});

test("prices are quantized to cents", () => {
  const price = meterPrice(["5.50"], 7);

  assert.strictEqual(price, 64);
  assert.ok(Number.isInteger(price));
});

test("weights default to the spec values", () => {
  assert.deepStrictEqual(Weights, { walk: 1.0, money: 1.0, risk: 0.5 });
});

test("citation probability is zero when we are certain", () => {
  approx(citationProbability(1.0, 1.0), 0.0);
});

test("citation probability follows the weaker confidence", () => {
  assert.strictEqual(citationProbability(0.5, 1.0), citationProbability(1.0, 0.5));
  assert.ok(citationProbability(0.5, 1.0) > citationProbability(0.9, 1.0));
});

test("risk is the expected fine", () => {
  assert.strictEqual(riskDollars(1.0, 1.0), 0);
  assert.strictEqual(riskDollars(0.0, 1.0), (FINE_STANDARD * 100) / 2);
  assert.strictEqual(riskDollars(0.0, 1.0, FINE_HYDRANT_CLASS), 5750);
});

test("total cost is the weighted sum", () => {
  const score = totalCost(6.0, 10.0, 4.0, Weights);

  approx(score, 6.0 + 10.0 + 2.0);
});

test("weights can switch off a term", () => {
  const freeWalking = { walk: 0.0, money: 1.0, risk: 0.0 };

  approx(totalCost(20.0, 3.0, 9.0, freeWalking), 3.0);
});

test("total cost uses the default weights when none are given", () => {
  approx(totalCost(1.0, 1.0, 2.0), 3.0);
});
