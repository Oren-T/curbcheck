/**
 * Cents arithmetic that reproduces `curbcheck/engine/cost.py`'s `Decimal` results.
 *
 * The Python keeps `Decimal` at 28 significant digits and rounds once,
 * ROUND_HALF_UP, to cents. Doing the same with doubles would round twice and
 * differ in the last cent, so the results are reproduced rather than the
 * mechanism: integer cents for the meter, an exact `BigInt` decimal multiply
 * for the risk term. Nothing here goes near `toFixed`.
 */

/**
 * `"8.25" -> 825`. A rate with more than two decimals is refused by the pack
 * compiler, so the pack never carries one and this refuses it too.
 */
export function parseCents(text) {
  const value = String(text).trim();
  if (!/^-?\d+(\.\d{1,2})?$/.test(value)) {
    throw new Error("a money amount must be a decimal string with at most two places");
  }
  const negative = value.startsWith("-");
  const [whole, fraction = ""] = value.replace("-", "").split(".");
  const cents = Number(whole) * 100 + Number(fraction.padEnd(2, "0"));
  return negative ? -cents : cents;
}

/** `825 -> "8.25"`, the `money` string the API returns. */
export function centsToString(cents) {
  const sign = cents < 0 ? "-" : "";
  const magnitude = Math.abs(cents);
  return `${sign}${Math.floor(magnitude / 100)}.${String(magnitude % 100).padStart(2, "0")}`;
}

/**
 * Progressive meter price in cents, for rates already in cents.
 *
 * Walks the hours exactly as `cost.meter_price` does, accumulating the integer
 * `N = sum(rateCents_i * minutes_i)`, and rounds `N / 60` half up to whole
 * cents. Every term but the last is a whole hour, so the only non-terminating
 * term is the partial one and a sum that lands exactly on half a cent is always
 * a terminating sum, which `Decimal` computes exactly; the integer formula is
 * therefore identical to the Python for every input `meter_price` can receive.
 * Callers are responsible for the empty-rates and negative-minutes refusals.
 */
export function meterPriceCents(hourRatesCents, chargedMinutes) {
  let scaled = 0;
  let remaining = chargedMinutes;
  let hourIndex = 0;
  while (remaining > 0) {
    const rate = hourRatesCents[Math.min(hourIndex, hourRatesCents.length - 1)];
    const minutes = Math.min(remaining, 60);
    scaled += rate * minutes;
    remaining -= minutes;
    hourIndex += 1;
  }
  return Math.floor((2 * scaled + 60) / 120);
}

/**
 * Expected fine in cents: the citation probability times the fine, rounded once.
 *
 * The probability is a double, and Python turns it into a `Decimal` through
 * `str()`, which is its shortest round-trip repr -- the same digits
 * `String(p)` gives, in a different exponential form. Parsing those digits
 * exactly and multiplying with `BigInt` reproduces the Decimal product without
 * a second float rounding.
 */
export function riskCents(parseConfidence, snapConfidence, fineDollars = 65) {
  const doubt = 1 - Math.min(parseConfidence, snapConfidence);
  const probability = Math.min(1, Math.max(0, doubt)) * 0.5;
  const p = decimalStringToScaled(String(probability));
  const fine = decimalStringToScaled(String(fineDollars));
  const numerator = p.value * fine.value * 100n;
  const denominator = 10n ** BigInt(p.scale + fine.scale);
  return Number(roundHalfUp(numerator, denominator));
}

/**
 * A decimal string as an exact `mantissa * 10 ** -scale`.
 *
 * Both the `"0.00005"` and the `"5e-05"` form have to be read: JavaScript
 * switches to exponential notation below 1e-6 and Python's `repr` below 1e-4,
 * so the same double reaches the two languages spelled differently.
 */
export function decimalStringToScaled(text) {
  const match = /^(-?)(\d+)(?:\.(\d+))?(?:[eE]([+-]?\d+))?$/.exec(String(text).trim());
  if (match === null) {
    throw new Error("not a decimal number");
  }
  const [, sign, whole, fraction = "", exponent = "0"] = match;
  const digits = BigInt(whole + fraction);
  const scale = fraction.length - Number(exponent);
  if (scale < 0) {
    return { value: (sign === "-" ? -digits : digits) * 10n ** BigInt(-scale), scale: 0 };
  }
  return { value: sign === "-" ? -digits : digits, scale };
}

/** `numerator / denominator` rounded half away from zero, as `Decimal.quantize` does. */
function roundHalfUp(numerator, denominator) {
  const negative = numerator < 0n;
  const magnitude = negative ? -numerator : numerator;
  const quotient = magnitude / denominator;
  const remainder = magnitude % denominator;
  const rounded = 2n * remainder >= denominator ? quotient + 1n : quotient;
  return negative ? -rounded : rounded;
}
