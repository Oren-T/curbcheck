# Sign-text gold set

`gold_set.jsonl` is 520 Manhattan `sign_description` strings hand-labeled to the
`ParsedSign` schema in `curbcheck/model.py`. It is the acceptance gate for the
sign-text parser (`docs/SPEC.md` §8.6): per-field precision/recall, exact-match
rate, and the "zero false permitted" rule are all measured against this file.

The labels were written by an agent that never read the parser, and the parser
was written concurrently by an agent that never read these labels.

## Format

One JSON object per line:

| key | meaning |
|---|---|
| `description` | the raw `sign_description`, verbatim |
| `count` | active Manhattan sign rows carrying this description |
| `codes` | the `sign_code`(s) the description appears under |
| `stratum` | `top100` or `tail` |
| `label` | a full `ParsedSign` (validated with `ParsedSign.model_validate`) |
| `labeler_notes` | why a non-obvious call was made; equal to `label.notes` |

## Sampling

`python tests/gold/sample_gold.py > sample.tsv` regenerates the sample from
`data/explore/descriptions.tsv` (1,790 distinct active descriptions). Rows are
sorted by count descending with ties broken by description, then:

- **`top100`** — all 100 most common descriptions, which cover 78% of active
  sign rows.
- **`tail`** — 420 of the remaining 1,690, drawn with `random.Random(20260914)`.

The seed and the tie-break make the draw reproducible; re-running the script on
the same corpus file reproduces the exact 520 rows in `gold_set.jsonl`.

## Labeling conventions

Read the sign the way a traffic agent standing in front of it would.

- **One `Regulation` per rule.** Two time windows, two day groups, or two
  maximum durations on one sign give two regulations.
- **Days.** Monday = 0. `MONDAY THURSDAY` is two named days; `MONDAY-FRIDAY`
  and `MON THRU FRI` are ranges; `ALL DAYS`, `ANYTIME` and signs with no day
  word are all seven; `EXCEPT SUNDAY` is Monday–Saturday plus
  `flags.except_sunday`; `SCHOOL DAYS` is Monday–Friday plus
  `flags.school_days`.
- **Times.** `"HH:MM"`, `MIDNIGHT` = `00:00`, `NOON` = `12:00`, `ANYTIME` and
  unposted hours are both `None`. `time_to` earlier than `time_from` wraps past
  midnight.
- **Metered.** `N HMP`, `N HOUR METERED PARKING` → `metered`, permitted,
  `max_duration_min = N * 60`. `N HOUR PARKING` is the same without `metered`.
- **Symbols.** Broom → `flags.street_cleaning`. `(SNOW EMERGENCY)` →
  `flags.snow_emergency`. Moon & stars is only a night marker and sets no field.
- **Reserved curb.** `X ONLY` signs (commercial, truck loading, AVO and every
  agency/plate class, taxi and FHV stands, bus stops and layovers, farmers
  market, microhub) are `permitted: true` with `vehicle_class: X` and
  `exclusive: true`, so `applies_to_passenger()` returns `False`. Class mapping:
  every authorized-plate or agency sign (AVO, police, consul/diplomat, USPS,
  NYP press, doctor plates, ambulance, ambulette) is `authorized`; FHV is
  `taxi`; farmers market and microhub are `other`.
- **Prohibition with a useless exception.** `NO STANDING EXCEPT TRUCKS
  LOADING`, `NO PARKING EXCEPT AUTHORIZED VEHICLES`, hotel and fire zones →
  `permitted: false`, `vehicle_class: all`, exception recorded in the notes.
- **Arrows.** Strip a trailing `(SUPERSEDES …)` first. `<->` with any dash
  count → `both`; `-->`, `<--`, `SINGLE ARROW` or an `O'CLOCK ARROW` → `forward`
  (the sign row's compass field carries the bearing); otherwise `none`.
- **Seasons.** `JUNE 1 - NOV 30` → `effective_from` `06-01`, `effective_to`
  `11-30`. `MARCH-NOVEMBER` is read as the whole of both months.
- **Non-regulation panels** (MTA route and destination panels, pay-by-cell
  locators, location panels, information and warning signs) get
  `regulations: []`, `parse_method: "grammar"`, `confidence: 1.0` and notes
  `panel:<class>`.
- **Riders and meta signs** ("METERS ARE NOT IN EFFECT ABOVE TIMES", the
  construction rider, the consul plate-code rider) modify a sibling sign and
  also carry no regulation of their own; notes start with `meta:`.
- **Manner-of-parking signs** (`BACK IN ANGLE PARKING ONLY`, `PARALLEL PARKING
  ONLY`) carry no time, day or class rule and the schema has no manner field,
  so they too yield `regulations: []` rather than a bare "permitted anytime".
- **Confidence.** 1.0 when the sign is unambiguous; 0.7–0.9 when a rider or a
  suspected typo leaves a real choice; a string whose core rule cannot be read
  at all is `parse_method: "unparsed"`, `regulations: []`, `confidence: 0`.

## Adjudications

`ADJUDICATIONS.md` records every disagreement between these labels and the
parser: which side was corrected and why, and which spelling differences were
accepted as having the same meaning for a passenger car. One label has been
corrected since the set was written; the entry says which and on what evidence.

## Known limits

- `flags.snow_emergency`, `flags.including_sunday`, `flags.holiday_exempt` and
  `flags.temporary` are not exercised: no sampled string contains `SNOW`,
  `INCLUDING SUNDAY` or a holiday exemption, and the one temporary-construction
  string is a rider with no rule of its own.
- `arrow: "backward"` never appears. `<--` occurs on 2 of 74,590 active rows
  citywide and on none of the sampled strings (`docs/DECISIONS.md` D3 refined).
- Signs reading `OTHER TIMES …` need "the complement of the sibling window",
  which the schema cannot express; those labels record the residual rule as an
  all-day prohibition and carry confidence 0.8.
