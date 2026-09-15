# Ground-Truth Validation (SPEC §13.3)

Run 2026-09-15 against `data/curbcheck.sqlite` as synced at 2026-09-15T04:12:40Z
(74,389 sign rows, 36,518 regulation segments) and `curbcheck serve` on
127.0.0.1:8765.

## What this run can and cannot prove

SPEC §13.3 asks for a physical check: Street View on 120 blockfaces and at least
20 visited in person. That is not available here. Both references used —
[nycdotsigns.net](https://nycdotsigns.net) (NYC DOT's own Parking Signs Locator)
and [parkingregulations.nyc](https://parkingregulations.nyc) (WXY) — are
rendered from the **same DOT SIMS export** that `nfid-uabd` comes from.

So this run validates **our pipeline against its own source**: snapping,
blockface and side assignment, span extents, stacking, the window evaluation,
and completeness of the match. It cannot detect a sign that is on the street and
missing from SIMS, a sign that has been changed since the export, or temporary
and construction signage. SPEC §11's universal "temporary signage may override"
caveat is therefore **not** discharged by anything below, and neither is the
"~30% coverage" question in its physical form. Where this document says
"correct", it means "agrees with what DOT publishes".

DOT's viewer is a map, so its result set is clipped to the search radius. Where
it returned a subset of a blockface's posts, only the shared posts are compared;
that is noted per row.

## 1. Sample

Drawn by `scripts/validation_sample.py`, **seed 20260915**, from a population of
17,540 classifiable blockface-sides. Stratified six neighbourhoods × five
regulation types (metered, street cleaning, No Standing/Parking Anytime,
stacked/mixed, apparently sign-free), one per cell, 30 in total.

A blockface-side here is `(centerline segment_id, side)`, the unit the engine
works in. DOT's unit is `(on street, from street, to street, side)`, which can
span several centerline segments; where they differ the row says so.

```
python scripts/validation_sample.py --seed 20260915
python scripts/validation_dossier.py --seed 20260915   # needs the server running
```

| # | Stratum | On street | Side | Between | seg |
|---|---|---|---|---|---|
| 1 | lower/metered | JOHN ST | S | CLIFF ST / PEARL ST | 79748 |
| 2 | lower/cleaning | LEONARD ST | S | CENTRE ST / LAFAYETTE ST | 79860 |
| 3 | lower/anytime | JOHN ST | S | FRONT ST / SOUTH ST | 79751 |
| 4 | lower/stacked | PECK SLIP | S | FRONT ST / WATER ST | 166357 |
| 5 | lower/no_data | FORSYTH ST | E | CANAL ST / HESTER ST | 206056 |
| 6 | village/metered | BOND ST | N | BOWERY / LAFAYETTE ST | 159545 |
| 7 | village/cleaning | E 7 ST | S | AVE A / AVE B | 81202 |
| 8 | village/anytime | MERCER ST | W | BLEECKER ST / W HOUSTON ST | 191147 |
| 9 | village/stacked | 8 AVE | E | BLEECKER ST / W 12 ST | 1100 |
| 10 | village/no_data | E 5 ST | S | AVE C / (dead end) | 4091 |
| 11 | midtown/metered | BROADWAY | E | W 40 ST / W 41 ST | 4214 |
| 12 | midtown/cleaning | W 45 ST | S | 10 AVE / 11 AVE | 196726 |
| 13 | midtown/anytime | FDR DRIVE | W | E 30 ST / E 34 ST | 1529 |
| 14 | midtown/stacked | E 36 ST | N | 1 AVE / FDR DRIVE | 4046 |
| 15 | midtown/no_data | PARK AVE | W | E 50 ST / E 51 ST | 2372 |
| 16 | ues_uws/metered | 2 AVE | W | E 83 ST / E 84 ST | 2162 |
| 17 | ues_uws/cleaning | W 73 ST | N | RIVERSIDE DR / W END AVE | 172036 |
| 18 | ues_uws/anytime | E 96 ST | N | 1 AVE / 2 AVE | 171923 |
| 19 | ues_uws/stacked | E 68 ST | S | 3 AVE / LEXINGTON AVE | 190846 |
| 20 | ues_uws/no_data | W 64 ST | S | FREEDOM PL S / RIVERSIDE BLVD | 197733 |
| 21 | harlem/metered | FREDERICK DOUGLASS BLVD | W | W 120 ST / W 121 ST | 19242 |
| 22 | harlem/cleaning | FREDERICK DOUGLASS BLVD | E | W 145 ST / W 146 ST | 19268 |
| 23 | harlem/anytime | E 119 ST | S | LEXINGTON AVE / PARK AVE | 27960 |
| 24 | harlem/stacked | E 135 ST | S | 5 AVE / MADISON AVE | 117593 |
| 25 | harlem/no_data | LENOX AVE | E | W 138 ST / W 139 ST | 19368 |
| 26 | heights/metered | W 207 ST | S | POST AVE / SHERMAN AVE | 26819 |
| 27 | heights/cleaning | ISHAM ST | S | BROADWAY / VERMILYEA AVE | 92684 |
| 28 | heights/anytime | WADSWORTH AVE | E | CROSS BRONX EXPY / W 178 ST | 26761 |
| 29 | heights/stacked | BROADWAY | W | W 177 ST / W 178 ST | 3022 |
| 30 | heights/no_data | BROADWAY | S | W 225 ST / W 228 ST | 155475 |

Windows, per SPEC §13.3: Wed 2026-09-16 10:00–12:00, Sat 2026-09-19 09:00–11:00,
Sun 2026-09-20 14:00–16:00, each queried through `POST /api/search` at the
blockface midpoint with a 2-minute walk radius.

## 2. Results per blockface-side

`set` compares the sign texts DOT lists for the blockface-side against ours;
`side` is whether DOT's `Side:` matches ours; `W/S/Su` are the three windows.

| # | set | side | W | S | Su | Note |
|---|---|---|---|---|---|---|
| 1 | ✓ 3/3 shared | ✓ | ✓ | ✓ | ✓ | DOT files this as one blockface Gold→Pearl; our 3 posts (267, 267, 326 ft) are the Cliff→Pearl half, distances identical |
| 2 | ✓ 4/4 | ✓ | ✓ | ✓ | ✓ | |
| 3 | ✓ 3/3 | ✓ | ✓ | ✓ | ✓ | |
| 4 | ✓ 4/4 | ✓ | ✓ | ✓ | ✓ | DOT files the two orders in opposite from/to directions; the ETL re-references both onto one chain |
| 5 | — | — | gap | gap | gap | **data gap**: DOT lists no Forsyth St Canal→Hester blockface either |
| 6 | ✓ 3/3 shared | ✓ | ✓ | ✓ | ✓ | |
| 7 | ✓ 4/4 shared | ✓ | ✓ | ✓ | ✓ | |
| 8 | ✓ 4/4 | ✓ | ✓ | ✓ | ✓ | three arrowed extents exact |
| 9 | ✓ 6/6 | ✓ | ✗ | ✗ | ✗ | **D1 below**: the whole blockface reads illegal; DOT's posts put a bus stop on 99–209 ft and metered curb beyond |
| 10 | ✗ 0/7 | — | gap | gap | gap | **matching gap (D2)**: `from_street = DEAD END` |
| 11 | ✓ 3/3 | ✓ | ✓ | ✓ | ✓ | Sat price $14.50 = M1 $5.50 + $9.00 (SPEC §13.2) |
| 12 | ✓ 6/6 shared | ✓ | ✓ | ✓ | ✓ | both arrowed extents exact |
| 13 | n/v | n/v | — | — | — | DOT's viewer will not geocode a point on the FDR Drive; verdict is illegal in all three windows, so no false-legal risk |
| 14 | ✓ 4/4 | ✓ | ✓ | ✓ | ✓ | all three arrowed extents exact |
| 15 | ✗ 0/7 | — | gap | gap | gap | **matching gap (D3)**: all 7 snapped to the *east* Park Ave carriageway, 77 ft away |
| 16 | ✓ 7/7 | ✓ | ✓ | ✓ | ✓ | |
| 17 | ✓ 4/4 | ✓ | ✓ | ✓ | ✓ | |
| 18 | ✓ 3/3 shared | ✓ | ✓ | ✓ | ✓ | |
| 19 | ✓ 7/7 | ✓ | ✓ | ✓ | ✓ | three arrowed extents exact |
| 20 | ✗ 0/2 | — | gap | gap | gap | **matching gap (D4)**: `THELONIOUS (SPHERE) MONK CIRCLE` is not in the centerline |
| 21 | ✓ 7/7 | ✓ | ✓ | ✓ | ✓ | |
| 22 | ✓ 5/5 | ✓ | ✓ | ✓ | ✓ | bus-stop extent exact |
| 23 | ✓ 6/6 | ✓ | ✓ | ✓ | ✓ | four arrowed extents consistent with DOT's post spacing |
| 24 | n/v | n/v | — | — | — | DOT's viewer returned only a location panel for this blockface-side in two searches; our 7 rows carry `E 135 ST / 5 AVE / MADISON AVE` straight from the export |
| 25 | artefact | — | — | — | — | Lenox Ave is a divided roadway; both DOT signs are in our DB on the **east** carriageway (seg 19332), which is the correct one. The sampled seg 19368 is the west carriageway, whose E curb is the median |
| 26 | ✓ 8/8 | ✓ | ✓ | ✓ | ✓ | |
| 27 | ✓ 2/2 | ✓ | ✓ | ✓ | ✓ | |
| 28 | ✓ 1/3 | ✓ | ✓ | ✓ | ✓ | the DOT blockface is chopped into 5 centerline pieces by the Cross Bronx ramps; the other 2 posts are on sibling pieces |
| 29 | ✓ 6/6 | ✓ | ✓ | ✓ | ✓ | |
| 30 | — | — | gap | gap | gap | **data gap**: the segment is the Broadway Bridge over the Harlem River; no curb, no signs, DOT's viewer will not geocode it |

## 3. Metrics against SPEC §12 / §13.3

Scored set: the 25 rows where DOT gives a readable answer and the sampled unit
maps one-to-one onto a DOT blockface-side (all but #13, #24, #25, #28, #30).

| Metric | SPEC threshold | Measured |
|---|---|---|
| Sign-set agreement per blockface-side | — | **21 / 21** where CurbCheck has signs (100%). Every shared post matched on text *and* `distance_from_intersection` to the foot |
| Side correctness | — | **21 / 21** (100%) |
| Blockface verdict accuracy, **where the tool issues a verdict** | ≥ 95% | **60 / 63 = 95.2%** (21 blockface-sides × 3 windows; only #9 wrong, on all three) |
| Same, counting #5 — no data, and no data is the right answer | ≥ 95% | **63 / 66 = 95.5%** |
| Blockface verdict accuracy, **counting coverage misses** | ≥ 95% | **63 / 75 = 84.0%** — #10, #15, #20 return no span at all where DOT posts signs |
| False-"legal" on the No-Standing-Anytime stratum | zero | **0** (#3, #8, #13, #18, #23, #28 all illegal in all three windows) |
| False-"legal" on the apparently sign-free stratum | zero | **0 literal false-legals.** But #10 and #20 are curb DOT posts `NO STANDING ANYTIME` on, and the tool draws *nothing* there — see §5 |
| Extent accuracy, arrowed signs, ±22 ft | ≥ 90% | **14 / 14 = 100%** on single-arrow spans (#8, #12, #14, #19, #22, #23). Caveat: our endpoints are by construction another post's own distance, so this measures whether the right neighbouring post was chosen, not an independent survey |
| Extent accuracy, two-way-arrow signs | — | **not met by design.** A `<->` post with no same-family post on one side takes the whole blockface-side (`segments._double_arrow_span`). On #9 that over-covers the bus stop by ~200 ft |
| `no_data` split: data gap vs matching gap | count both | **2 data gaps** (#5, #30) vs **3 matching gaps** (#10, #15, #20), plus 1 sampling artefact (#25) |

Per-window, where a verdict is issued: Wed 20/21, Sat 20/21, Sun 20/21.

## 4. Disagreements, with the raw text

### D1. A two-way-arrow bus stop swallows the metered curb (#9, 8 AVE side E, Bleecker→W 12)

DOT's posts on this blockface-side:

```
 99 FT  N/A    BUS STOP SIGN(NO STANDING)
209 FT  South  BUS STOP SIGN(NO STANDING)
209 FT  N/A    REAL TIME PUBLIC INFORMATION BOX (FOR FULL TIME BUS STOPS)
232 FT  N/A    NO PARKING (SANITATION BROOM SYMBOL) 7:30AM-8AM EXCEPT SUNDAY <->
232 FT  N/A    ZONE#106671 PAY BY CELL LOCATOR
232 FT  N/A    2 HMP 8AM-7PM EXCEPT SUNDAY <->
```

Ours, verbatim: `BUS STOP SIGN (BUS & HANDICAP SYMBOLS) NO STANDING <----->` at
99 ft and `... W/ SINGLE ARROW` (bearing South) at 209 ft. The pair is the
standard DOT way of saying the bus stop runs between them. `_double_arrow_span`
sees no same-family post *before* 99 ft, falls back to the whole side, and the
blockface comes back **illegal in all three windows**. Reading the posts as a
traffic agent would, 0–99 ft and ~214–313 ft are parkable: metered at $5.00 +
$8.25 on Wed and Sat, free on Sunday.

**Who is right:** DOT. Ours is a false *illegal*, which costs the user a legal
spot but never invites a ticket, and it is the documented conservative reading
(SPEC §B.2, 34 RCNY 4-08, decision D12's reasoning). It is a correctness defect
against the §13.3 threshold all the same, and it is the sole verdict error in
the sample.

**Reproducer:** `POST /api/search` at 40.737221,-74.005183, any of the three
windows, walk 2 min; every span on segment 1100 side E is illegal.

### D2. `from_street = DEAD END` drops the whole blockface (#10, E 5 ST side S, Ave C → dead end)

DOT lists 7 signs on `ON EAST 5 STREET FROM DEAD END TO AVENUE C (Side: S)`:

```
 92 FT  West  NO STANDING ANYTIME -->
 92 FT  East  NO PARKING (SANITATION BROOM SYMBOL) TUESDAY FRIDAY 11AM-12:30PM -->
171 FT  N/A   NO PARKING (SANITATION BROOM SYMBOL) TUESDAY FRIDAY 11AM-12:30PM <->
278 FT  N/A   NO PARKING (SANITATION BROOM SYMBOL) TUESDAY FRIDAY 11AM-12:30PM <->
380 FT  N/A   NO PARKING (SANITATION BROOM SYMBOL) TUESDAY FRIDAY 11AM-12:30PM <->
467 FT  West  NO PARKING (SANITATION BROOM SYMBOL) TUESDAY FRIDAY 11AM-12:30PM -->
467 FT  East  NO STANDING ANYTIME -->
```

We have none of them. `DEAD END` is not a street, so
`streets.find_block_detail(on, from, to)` cannot resolve the chain and every
sign on the face is dropped with `snap_notes = "unmatched: cross_street_is_dead_end"`.
`sync_meta.snap_unmatched_reasons` puts the citywide figure at **604 sign rows
over 147 blockface-sides**; `docs/DATA.md` §1.9 already flagged the value as
"not a street" without drawing the consequence.

**Who is right:** DOT. This is a matching gap, not a data gap.

**Reproducer:**

```
sqlite3 data/curbcheck.sqlite "SELECT COUNT(*) FROM sign
  WHERE segment_id IS NULL AND snap_notes LIKE '%dead_end%'"   -- 604
```

**Shape of a fix (not applied — it needs a re-sync):** a dead end is the
terminal node of the named street's own connected run. When exactly one of
`from_street`/`to_street` is `DEAD END`, walk the `on_street` chain from the
named node away from it until the street's degree-1 endpoint, and use that as
the other end. This lives in `etl/streets.find_block_detail` and `etl/snap`.

### D3. Divided roadways pick a carriageway by coin flip (#15, PARK AVE side W, E 50→E 51)

DOT: `ON PARK AVENUE FROM EAST 51 STREET TO EAST 50 STREET (Side: W)`, 7 signs,
including `NO STANDING MONDAY-FRIDAY 7AM-10AM 4PM-7PM <->` and
`3 HMP COMMERCIAL VEHICLES ONLY MONDAY-FRIDAY 10AM-4PM <->` at 85 and 173 ft and
`TAXI HAILING (SYMBOL) TAXI STAND -->` at 49 ft.

Our DB has all 7 — snapped to segment **2385**, with
`snap_notes = "2 equally short chains span this block"`. CSCL models Park Ave as
two parallel centerlines 77 ft apart. Segment 2372 carries address range 310–336
(the west roadway); 2385 carries 321–339 (the east roadway). A sign whose
`side_of_street` is `W` belongs to the west curb of the **west** carriageway, so
the correct chain was 2372. The tool draws this curb 77 ft away, in the middle of
Park Avenue, and leaves the real west curb blank.

The tie-break is not biased, it is blind: on #25 (Lenox Ave, also divided) it
picked the east carriageway for a `Side: E` sign, which is right. **5,109 of the
70,671 snapped sign rows (7.2%)** sit on a block where two equally short chains
compete.

**Who is right:** DOT. Ours is a snap error of roughly half a roadway width.

**Reproducer:**

```
sqlite3 data/curbcheck.sqlite "SELECT segment_id, side_of_street, distance_from_intersection,
  sign_description FROM sign WHERE on_street LIKE 'PARK AV%'
  AND from_street LIKE '%51 STREET%' AND to_street LIKE '%50 STREET%'"
-- all 7 rows come back on segment 2385; segment 2372 is the west carriageway
```

**Shape of a fix (not applied):** when two chains tie, prefer the one for which
offsetting toward `side_of_street` moves *away* from the sibling chain. That is
what "the outer curb of a divided roadway" means geometrically, and it needs no
new data. `etl/snap.py`, at the point that logs `2 equally short chains`.

### D4. A cross street missing from the centerline drops the blockface (#20, W 64 ST side S)

DOT: `ON WEST 64 STREET FROM RIVERSIDE BOULEVARD TO THELONIOUS MONK CIRCLE (Side: S)`

```
 82 FT  West  NO STANDING ANYTIME -->
227 FT  N/A   NO PARKING (SANITATION BROOM SYMBOL) TUESDAY FRIDAY 8AM-9:30AM <->
```

Both are in our `sign` table with `segment_id IS NULL` and
`snap_notes = "unmatched: cross_street_not_in_centerline"`. `THELONIOUS SPHERE
MONK CIRCLE` appears in `docs/DATA.md` §1.9's list of 63 unresolved names, again
without the consequence being drawn. Citywide this reason accounts for **739
sign rows**. The adjacent face (`FREEDOM PLACE SOUTH TO WEST END AVENUE`, Side S)
matched fine and holds the same `NO STANDING ANYTIME` text.

**Who is right:** DOT. Matching gap. Fix is an alias-table entry plus the
missing circle in the centerline extract.

### D5 (not a verdict error). Overlapping duplicate spans

Every blockface produces one span per post, so a blockface-side with five
identical `<->` posts returns five overlapping spans with identical verdicts
(#6 four, #12 eight, #13 seven, #18 four). The verdicts agree with each other and
with DOT, so nothing is wrong; but the ranked list repeats the same stretch of
curb several times, and the count line inherits the inflation.

## 5. The state SPEC §11 requires and the tool does not reach

SPEC §11 makes "**No sign data on this block**" (grey) a non-negotiable state.
In the running app it exists and reads correctly — see
`data/screenshots/validation_ui_no_data_detail.png` — but it only fires for the
**32** `regulation_segment` rows that have a span and no rules.

- **722 blockface-sides** (6.2% of the 11,613 with signs) have every sign
  unmatched, so no span exists and nothing at all is returned. **520 of those
  722 (72%) carry a `NO STANDING`, `NO PARKING` or `NO STOPPING ANYTIME` sign.**
- Only **11,996 of the 22,204** centerline sides in Manhattan have any span at
  all. The other 10,208 draw nothing.

The tool never says "legal" there — but it never says anything, and a blank map
is read by a driver as "nothing here", not as "unknown". This is the same hazard
SPEC §11 was written to prevent, arriving through a different door.

**Since this run:** the ETL writes a placeholder span for a centerline side with
no rules, carrying `gap_kind` — `no_signs` where DOT lists no sign, and
`unmatched_signs` where signs exist and none could be placed. The grey state
says which of the two it is, and the legend and the banner say that a blank or
grey curb means no data, not no restriction.

```
sqlite3 data/curbcheck.sqlite "SELECT COUNT(*) FROM (
  SELECT DISTINCT on_street, from_street, to_street, side_of_street
  FROM sign WHERE segment_id IS NULL)"                          -- 722
```

## 6. UI probe

`http://127.0.0.1:8765`, two searches driven through the form.

1. **Madison St & Rutgers St, Wed 2026-09-16 10:00–12:00, 2 min walk.**
   55 stretches: 38 legal, 16 illegal, 1 no-data.
2. **E 7 St & 2 Ave, Sat 2026-09-19 09:00–11:00, 2 min walk.**
   62 stretches: 24 legal, 21 ambiguous, 17 illegal.

Detail panels opened for one of each verdict. All four show the persistent
disclaimer, the per-result "BEFORE YOU PARK" caveat block including the universal
temporary-signage caveat, and the raw sign text verbatim in a monospace block
under the heading "The sign text below is quoted verbatim from NYC Open Data.
Where it disagrees with the reading underneath it, the sign wins." The ambiguous
panel names its reason ("a sign here modifies another sign; the combination is
not machine-readable", decision D17) and prints the meta sign's text. The no-data
panel prints SPEC §11's sentence about hydrants, bus stops, crosswalks and
driveways. Screenshots: `data/screenshots/validation_ui_{legal,illegal,ambiguous,no_data}_detail.png`.

**U1 — the ranked list truncates the map layer (fixed after this run).** `POST /api/search`
sorts legal first and then applies `limit`, and the frontend never sends `limit`,
so it gets the default 100. Measured at 2026-09-16 10:00–12:00:

| Point | walk | spans in range | returned at limit 500 |
|---|---|---|---|
| 3 Ave & E 85 St | 10 min | 500+ | 500 legal, **0 illegal, 0 ambiguous** |
| Bryant Park | 10 min | 500+ | 23 legal, 7 ambiguous, 470 illegal |
| Madison & Rutgers | 6 min | 354 | 188 legal, 158 illegal, 7 ambiguous, 1 no-data |

At the UI's own default (10-minute walk, limit 100) a search on the Upper East
Side returns a hundred green spans and nothing else, and the map has no red or
amber curb to draw — while the status line reads "100 stretches within a 10 min
walk · 100 legal for the whole window", which states as fact something the query
never established. `docs/API.md` says `results` is "every span in radius,
whatever its verdict, because the map colours illegal and ambiguous curb too";
at these densities it is not. Raising the cap does not fix it, as the first row
shows. The fix belongs in the engine — cap the *ranked legal list*, return the
rest for the map — and that is what was done after this run: `search()` returns
the ranked legal spans capped at `limit`, every other verdict capped separately
at `map_limit`, and `counts` taken before either cap, which the status line is
built from. The table above measures the behaviour before that change.

**U2 — two basemap glyph ranges 404 (fixed after this run).** Console errors on every map
load:

```
GET /basemap/fonts/Noto Sans Regular/768-1023.pbf   404
GET /basemap/fonts/Noto Sans Regular/7680-7935.pbf  404
```

MapLibre falls back to client-side rendering for U+0301, U+0306, U+1EA1, U+1ED9,
U+1EDF, so labels still appear. Decision D16(c) said three vendored ranges are
enough; Manhattan's Vietnamese and accented business names need two more. Both
are vendored now, for each fontstack, and D16(c) carries the correction. No
other console errors appeared. (A third console error, `404 /api/search`, was the
documented `address_not_found` for the query below.)

**U3 — geocoder, not a defect.** `Broadway & W 46 St` returns no candidates. There
is no such node: in CSCL, Broadway between W 45 and W 47 is the Times Square
pedestrian plaza, and the node at that corner carries only `W 46 ST`. The UI's
message ("Try a cross street like …, or drop a pin") is the right answer.

**Fixed in this run — the detail panel cried "unreadable" at signs it had read.**
`/api/segment/{id}` lists every sign on the parent centerline segment, including
non-regulation panels (D10) and signs governing the other side or another span.
`web/detail.js` rendered all of them with "The software could not read this sign.
Only the raw text above is trustworthy." That is SPEC §11's amber ambiguity
signal, and spending it on correctly-parsed signs teaches the user to ignore it:
before the fix it appeared on **every** panel opened, including the legal one
whose own rule parsed at 100% confidence. D13 guarantees a sign in this stretch's
own stack always has a `regulation` row, so the no-rule branch can never be a
parse failure. It now says either "This panel states no parking rule…" or "This
sign is posted on this block but does not govern this stretch…", in neutral
styling. `tests/test_web_static.py::test_unparsed_warning_is_reserved_for_signs_the_parser_failed_on`
pins it.

## 7. Verdict-logic spot checks

`python scripts/verdict_spot_checks.py` — six cases whose answer is settled by
the posted sign and the published calendar. All six pass.

| Case | Segment / sign | Window | Expected | Got |
|---|---|---|---|---|
| Meters off on a Major Legal Holiday (SPEC §5.6, §13.2) | `3dc27dd9e77b6179` · `2 HMP 9AM-MIDNIGHT EXCEPT SUNDAY` | Thu 2026-11-26 10:00–12:00 | legal, $0.00 | legal, $0.00, caveat "meters are not in effect for part of this window" |
| Overnight ban, all days | `267900ccaef7f809` · `MOON & STARS (SYMBOLS) NO STANDING 11PM-7AM ALL DAYS -->` | Mon 2026-09-21 00:00–03:00 | illegal | illegal, "no standing Mon 00:00-03:00" |
| ASP suspension lifts street cleaning (SPEC §9.3c) | `64a79e8c0273121e` · `NO PARKING (SANITATION BROOM SYMBOL) MONDAY THURSDAY 11AM-12:30PM -->` | Thu 2026-04-02 (Holy Thursday) 11:00–12:00 | legal + suspension caveat | legal, $0.00, caveat "street cleaning is suspended on this date" |
| Window longer than the posted limit (SPEC §9.2) | `3dc27dd9e77b6179` · `2 HMP 9AM-MIDNIGHT EXCEPT SUNDAY` | Wed 2026-09-16 10:00–13:00 | illegal, duration reason | illegal, "posted limit of 120 min is shorter than the 180 min window" |
| Same, on a 1-hour meter | `cc02e52e071c8824` · `1 HMP COMMERCIAL VEHICLES ONLY 7AM-10PM EXCEPT SUNDAY` | Wed 2026-09-16 10:00–13:00 | illegal | illegal, "reserved for commercial vehicles" |
| A holiday does not lift a standing ban | `46ff88541bc0a44d` · `NO STANDING ANYTIME <->` | Fri 2026-12-25 10:00–12:00 | illegal | illegal, "no standing Fri 10:00-12:00" |

The spec's "3-hour window on a 1-hour meter" is written for a passenger car, but
**all 360 of Manhattan's one-hour metered rules are commercial-only**, so that
case refuses for the vehicle class before the duration rule is reached. The
two-hour passenger meter above exercises the duration rule on its own, and the
one-hour case is kept to show the verdict is still illegal when two reasons
apply.

## 8. Judgement against the SPEC §13.3 trust bar

**Where CurbCheck answers, it is trustworthy.** 21 of 21 verifiable
blockface-sides matched DOT's sign set, its side, and its post distances exactly;
95.2% of issued window verdicts were right; every single-arrow extent was exact; zero
false "legal" verdicts on either dangerous stratum; all six calendar and duration
spot checks pass. The parse-and-evaluate half of the pipeline clears the bar.

**Coverage does not clear it.** Counting the blockfaces where DOT publishes signs
and CurbCheck returns nothing, accuracy is 84.0%, below the 95% threshold. Two
of the three coverage misses in a 30-sample are curb that DOT posts
`NO STANDING ANYTIME` on, and citywide 520 blockface-sides in that state are
invisible. The UI compounds it: at the default walk radius a search in a
legal-rich neighbourhood returns a hundred green spans and drops every red one
(U1).

So, against SPEC §13.3's last sentence: **ship with the louder advisory banner
and the failing strata flagged, not as-is.** Specifically —

1. The banner must say that a blockface with no drawn curb means *no data*, not
   *no restriction*, naming the 6.2% of blockface-sides that are unmatched.
2. U1 must be fixed before any release. A map that silently drops illegal curb
   is worse than one that draws nothing, because it looks complete.
3. D2 and D4 are bounded and mechanical (604 + 739 sign rows, one chain rule and
   one alias). Fixing them and re-syncing would move the coverage number most.
4. D3 changes no verdict but puts curb on the wrong side of a 77-ft median on up
   to 7.2% of snapped signs; it should be fixed before anyone navigates by the
   map rather than by the list.
5. D1 costs the user legal spots and should be revisited when a two-way arrow's
   bounding post can be identified across families.

None of this discharges SPEC §11's temporary-signage caveat or the physical
survey §13.3 asks for. Both remain outstanding.
