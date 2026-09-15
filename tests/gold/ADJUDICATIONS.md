# Gold-set adjudications

The parser (`curbcheck.etl.parse`) and the labels in `gold_set.jsonl` were
written by two agents who never saw each other's work. `scripts/eval_gold.py`
scored one against the other; every disagreement is settled below by reading the
sign the way a traffic agent standing in front of it would, with `docs/SPEC.md`
§8.4/§8.5, `README.md` in this directory, and `curbcheck/model.py` as the
evidence. Three outcomes are possible: the parser was wrong and was fixed, the
label was wrong and was fixed, or the two write the same sign down differently
and the comparison was made semantic instead.

Scores before and after, weighted by the active Manhattan sign rows carrying
each description (520 descriptions, 62,073 rows):

| | exact | semantic | false permitted |
|---|---|---|---|
| before | 95.99% (top100 96.95%) | 99.04% | 2 descriptions, 3 rows |
| after | 97.01% (top100 97.97%) | 100.00% | none |

The remaining 111 exact mismatches are all class (c) below: the two sides write
the same curb down differently and a passenger car is affected identically.

## (a) Parser was wrong — fixed, with a regression test in `tests/test_parse.py`

1. **`DAY - DAY XYY-XYY (FOR BUS STOP ONLY)`** (42 rows) — the parser called it
   `panel:template` at confidence 1.0, which tells the engine no rule stands
   here. The label says `unparsed`, and the label is right: the placeholders are
   the *schedule*, and the sign still says a bus stop is there. `panels.py` now
   sends a blank template that names a rule head to the grammar, and from there
   to `unparsed`. This also rescues `NO STANDING W/ SINGLE ARROW ZZZ THRU ZZZ
   XX:XXYY-XX:XXYY …` (3 rows), which was silently classified as a panel.
2. **`… FOR HIRE VEHICLES ONLY MONDAY -FRIDAY 8AM-MIDNIGHT <->`** (2 rows) — a
   space on one side of the range joiner only. The day scanner read `MONDAY`
   alone and left Tuesday to Friday unposted: a **false permitted**.
   `days.py::_scan_range` now joins the split range.
3. **`NO STANDING 8AM-6PM EXCEPT SUNDAY --> W/ ACCESS-A-RIDE (SYMBOL)
   ACCESS-A-RIDE BUS STOP`** (2 rows, plus a sibling string) — the grammar
   folded the bus-stop reservation into the timed prohibition, so the whole
   reservation inherited 8AM–6PM and the rest of the week came back unposted:
   the second **false permitted**. A class head now folds into a *permission*
   wherever it appears (SPEC §8.4 ex. 14 puts it after the schedule) but into a
   *prohibition* only before that clause has hours of its own.
4. **`FHV (SYMBOL) FOR-HIRE VEHICLES ONLY PICK-UP / DROP-OFF ONLY`,
   `MICROHUB ZONE VEHICLES WITH PERMIT ONLY`, and three like them** — two names
   for one reservation produced two identical or near-identical regulations.
   A second reservation head now folds into a reservation clause that has no
   schedule yet, keeping the class the first head named.
5. **`BIKE (SYMBOL) LATOURETTE PARK GREENWAY W/ 9M O'CLOCK ARROW`** (1 row) —
   `unparsed`, which makes the whole block ambiguous for a bike-path guide sign
   that regulates no curb. The label reads it as a guide panel and is right;
   `GREENWAY` joined the advisory vocabulary, which is only consulted after the
   grammar has found no rule head.
6. **`NO STANDING 5PM-MIDNIGHT MON-FRI EXCEPT TLC LICENSED VEHICLES PRE-ARRANGED
   SERVICE ONLY`** (1 row, not sampled into the gold set; found while checking
   fix 3 against the whole corpus) — the `EXCEPT` rider stopped at the
   reservation head, which then became a second, all-week rule. `EXCEPT <class>`
   now swallows a reservation head too, leaving the one timed prohibition the
   sign states.

## (b) Label was wrong — fixed in `gold_set.jsonl`

1. **`METERS ARE NOT IN EFFECT ABOVE TIMES (TO BE USED ONLY FOR CONFLICTING
   STREET CLEANING AND METERED PARKING REGULATIONS) (SUPERSEDES SW-473)`**
   (594 rows, `top100`). The label was `regulations: []` with the note
   `meta: …`, following this directory's "riders and meta signs carry no
   regulation of their own" convention. Changed to one all-day placeholder
   regulation carrying `flags.meta`.

   Why: `Flags.meta` exists in `model.py` for exactly this sign ("§8.4 ex. 7"),
   `engine.resolve.ambiguity_reason` reads it, and `etl/build.py` writes such a
   rule at `META_CONFIDENCE = 0.7` so its span comes out AMBIGUOUS. A regulation
   is the only carrier the schema offers. With `regulations: []` the sign is
   indistinguishable from a blank panel, nothing reaches the `regulation` table,
   and the meter sign beside it is then read at face value — which is what
   SPEC §8.5 ("link to the affected sibling reg for the stacking engine")
   forbids.

   Caveat worth carrying forward: the placeholder is `permitted: true,
   vehicle_class: all`, so it reads as a blanket permission to anything that
   does not check `flags.meta` first. Both readers that exist today do check,
   and `build.py` additionally caps its confidence, but a prohibitive
   placeholder would be the safer default if this rule ever grows a third
   reader.

## (c) Same curb, two spellings — accepted, comparison made semantic

`scripts/eval_gold.py` scores these by the passenger-car effect of the rules
over a canonical week (7 days × 5-minute slots, seasons sampled at every
`effective_from`/`effective_to` boundary): may a passenger car park, is it
metered, what limit applies, and which calendar-conditional flags are live.
Both spellings below produce the identical matrix, so both are counted correct.

1. **`park` vs `stand` on a curb reservation** — 100 descriptions, 1,271 rows.
   The labeler wrote `TRUCK LOADING ONLY`, `TAXI RELIEF STAND`, `BUS LAYOVER
   ONLY`, `FARMERS MARKET ONLY` and `AMBULETTE ONLY` as `action: park`
   ("drivers may leave the vehicle"); the parser writes them as `action: stand`.
   `Regulation.applies_to_passenger()` returns `False` for both, and
   `resolve.resolve_interval` treats any such rule as a prohibition regardless
   of its action, so the action only changes the reason string a truck driver
   would read. Neither side was rewritten.
2. **Reservation vs plain prohibition on a useless exception** — 5
   descriptions, 570 rows: `NO STANDING HOTEL LOADING ZONE`, `NO STANDING FIRE
   ZONE`, and the `OTHER TIMES HOTEL LOADING ZONE` pair. This directory's
   README records these as `permitted: false, vehicle_class: all`; the parser's
   package docstring records them as exclusives. Both make
   `applies_to_passenger()` `False` for every minute the rule covers.
3. **The confidence scale** — 8 descriptions, 16 rows. The README uses 0.7–0.9
   for "a rider or a suspected typo leaves a real choice"; the parser uses a
   flat 0.7 for "established rule, something unread". Confidence is compared in
   the exact match and in the per-field table but not in the effect matrix.

   One consequence deserves attention rather than a rewrite: five `OTHER TIMES`
   signs are labeled 0.8, which is exactly `resolve.AMBIGUITY_THRESHOLD`, while
   the parser gives 0.7. The deployed engine will therefore say AMBIGUOUS where
   the gold set implies a confident ILLEGAL. The parser's side is the
   conservative one and matches SPEC §8.6 ("never silently guessed"), since both
   sides are recording an over-restriction the schema cannot express. The
   converse appears once: `MOON & STARS (SYMBOLS) NO STANDING 8PM-5PM ALL DAYS`
   (1 row), where the labeler suspected a typo for `8PM-5AM` and dropped to 0.7
   while the parser reads the posted hours literally at 1.0. Both prohibit
   standing for the hours in dispute, so no verdict can turn on it.

## Left open

- `flags.temporary` never reaches a regulation. `HARD HAT (SYMBOL) TEMPORARY
  CONSTRUCTION REGULATION (RIDER)` (129 rows) is a rider with no rule of its
  own, and both sides drop it to `regulations: []`, so the sign it hangs with
  is not marked temporary. Both sides agree, so no gold-set disagreement records
  it; it is a schema-level gap, not a parser defect.
- `arrow: backward`, `flags.snow_emergency`, `flags.including_sunday` and
  `flags.holiday_exempt` are unexercised by the sample (README "Known limits"),
  so this gold set says nothing about them.
