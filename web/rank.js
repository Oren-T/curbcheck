/**
 * Ranking preferences and the client-side re-rank.
 *
 * UX_AUDIT P1-3: the weight sliders did nothing until a fresh search, so a user
 * who set "money 3.0" watched the same five results sit there. Every input to
 * the score — `walk_min`, `money_value`, `risk` — is already on each result, so
 * changing a preference re-sorts what is on screen and never re-queries. The
 * two inputs that are *not* on the result (the walk radius and the window) do
 * need the server, and the search button says so.
 */

/**
 * Preset weights for the `Prefer` control. The numbers are the same scale the
 * API takes in `weights` and are sent with the next search so the server's own
 * ranking agrees with the one on screen.
 */
export const PREFER_PRESETS = [
  { id: "closer", label: "Closer", weights: { walk: 2, money: 1, risk: 0.5 } },
  { id: "cheaper", label: "Cheaper", weights: { walk: 1, money: 2, risk: 0.5 } },
  { id: "safer", label: "Safer bet", weights: { walk: 1, money: 1, risk: 2 } },
  { id: "balanced", label: "Balanced", weights: { walk: 1, money: 1, risk: 0.5 } },
];

export const DEFAULT_PREFER = "balanced";

export function presetWeights(id) {
  const preset = PREFER_PRESETS.find((candidate) => candidate.id === id);
  return preset ? { ...preset.weights } : { ...PREFER_PRESETS[3].weights };
}

/** The preset a set of weights matches exactly, or null for a custom mix. */
export function presetFor(weights) {
  const match = PREFER_PRESETS.find(
    (preset) =>
      near(preset.weights.walk, weights.walk) &&
      near(preset.weights.money, weights.money) &&
      near(preset.weights.risk, weights.risk),
  );
  return match ? match.id : null;
}

function near(left, right) {
  return Math.abs(left - right) < 0.001;
}

/**
 * The score the list is sorted by: the same weighted sum the engine computes
 * (`curbcheck.engine.cost.total_cost`), recomputed here so a preference change
 * is instant.
 *
 * `money_value` is the number twin of the `money` decimal string. docs/API.md
 * forbids doing arithmetic on `money`; `money_value` exists for exactly this
 * and is null when the price is unknown.
 */
export function clientScore(result, weights) {
  const walk = typeof result.walk_min === "number" ? result.walk_min : 0;
  const money = typeof result.money_value === "number" ? result.money_value : 0;
  const risk = typeof result.risk === "number" ? result.risk : 0;
  return weights.walk * walk + weights.money * money + weights.risk * risk;
}

function isPriced(result) {
  return typeof result.money_value === "number";
}

// Two spans whose scores differ by less than this are ranked by basis instead:
// a stretch with a sign to read outranks one where nothing is posted at the
// same cost (docs/API.md, docs/DECISIONS.md D27). The server orders its own
// list this way, so the client re-rank has to agree or the order would change
// meaning as soon as a preference chip was pressed.
const TIE_DOLLARS = 0.5;

function basisRank(result) {
  return result.basis === "posted" ? 0 : 1;
}

/**
 * Re-sort the legal results in place of the server's order and leave every
 * other verdict where the server put it.
 *
 * An unpriced legal span sorts after every priced one: with `money_value` null
 * its money term is zero, so it would otherwise win a "Cheaper" ranking by
 * being unknown, which is the "absence reads as a good answer" failure this UI
 * exists to avoid.
 */
export function rankLegal(results, weights) {
  const legal = [];
  const rest = [];
  for (const result of results) {
    (result.verdict === "legal" ? legal : rest).push(result);
  }
  const priced = legal.filter(isPriced);
  const unpriced = legal.filter((result) => !isPriced(result));
  const byScore = (left, right) => {
    const difference = clientScore(left, weights) - clientScore(right, weights);
    if (Math.abs(difference) < TIE_DOLLARS) {
      return basisRank(left) - basisRank(right);
    }
    return difference;
  };
  priced.sort(byScore);
  unpriced.sort(byScore);
  return [...priced, ...unpriced, ...rest];
}
