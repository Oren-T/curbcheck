# Decision Log

Short records of choices that depart from `docs/SPEC.md` or that the spec left
open. Each entry says what was decided, the evidence, and what would reverse it.
Newest at the bottom. Keep entries to a paragraph.

## D1. Street Centerline (CSCL) replaces LION as the street model

**Decided:** 2026-09-14. Use NYC DCP's Street Centerline dataset on Open Data
(`inkn-q76z`) as the centerline, fetched as GeoJSON over Socrata. LION is the
documented fallback.

**Why:** LION ships as a file geodatabase that needs GDAL, which the spec's own
safe-parser rule (§3.3) says to avoid where a GeoJSON alternative exists. The
LION page on nyc.gov also returns 403 to scripted fetches. CSCL is a Socrata
dataset with `full_street_name`, `streetwidth`, left/right blockface ids, and
left/right address ranges, which is everything the snap step and the local
geocoder need. Manhattan has 9,297 street segments.

**Would reverse it:** finding that CSCL lacks a field the snap step needs that
LION carries, such as from/to node ids that cannot be reconstructed from shared
endpoint coordinates.

## D2. No LLM parser fallback in v1

**Decided:** 2026-09-14. The sign-text parser is a deterministic grammar only.
Strings it cannot parse are written to a residue report and marked `unparsed`.

**Why:** All of Manhattan's 75,865 current sign rows use only 1,893 distinct
`sign_description` strings. That is small enough to reach near-total coverage
with templates and to review the residue by hand. Removing the LLM removes
threat T5 entirely and a large local-inference dependency. The `parse_method`
column keeps the `llm` value reserved so a fallback can be added later.

**Would reverse it:** a residue that stays large after the grammar is mature,
or expansion to other boroughs with a much larger vocabulary.

## D3. Arrow direction comes from the description glyph

**Decided:** 2026-09-14. The `-->`, `<--`, `<->` glyphs inside
`sign_description` are the primary arrow source. The `arrow_direction`
column is a cross-check only.

**Why:** In the live data `arrow_direction` is blank on 74% of Manhattan rows
and, when present, holds a compass word (North/South/East/West), not the
glyphs the spec describes. The glyph is reliably at the end of the description.

## D4. SQLite (stdlib) replaces DuckDB plus the spatial extension

**Decided:** 2026-09-14. The store is a single SQLite file via the standard
library `sqlite3` module. Geometry is stored as GeoJSON text plus a bounding
box; radius queries filter on the bbox in SQL and refine with shapely.

**Why:** The spec chose DuckDB for analytic speed on "hundreds of thousands of
rows." Manhattan has 75,865 sign rows and under 10,000 street segments. At that
size SQLite is instant, adds zero dependencies, and avoids the DuckDB spatial
extension, which is fetched over the network at install time and which the
spec itself flagged as an unresolved supply-chain question (§15.8).

**Would reverse it:** expansion to all five boroughs with heavy analytic joins.

## D5. Sign rows get a synthetic primary key

**Decided:** 2026-09-14. `sign.sign_id` is a hash of the full source row.
`order_number` is kept as a plain column.

**Why:** The spec names `order_number` as the primary key, but one order covers
many signs. Manhattan has 11,807 distinct order numbers across 75,865 rows.

## D6. Meter rates come from `e7yp-wx55`, not `s7zi-dgdx`

**Decided:** 2026-09-14. The spec has the ids swapped. `s7zi-dgdx` is an
empty map view. `e7yp-wx55` is the data table, and it carries on/from/to street
names, side, and a `meter_rate` column, so the join can be by normalized street
names with geometry as a check.

## D7. Python dependencies are hash-pinned with pip, not conda

**Decided:** 2026-09-14. `environment.yml` supplies only the interpreter, pip,
and the Jupyter kernel the container requires. Application packages are pinned
with hashes in `requirements.txt` and `requirements-dev.txt` and installed with
`pip install --require-hashes`.

**Why:** The spec requires hash-pinned installs (threat T1). Conda environment
files cannot express hashes.

## D8. geopandas is dropped from the dependency tree

**Decided:** 2026-09-14. ETL uses shapely and plain dicts. No geopandas, and
therefore no pandas or pyarrow.

**Why:** The spec (§4.1) already listed geopandas as "kept but flagged as a
rejection candidate," conditional on DuckDB spatial covering the ETL joins.
D4 removed DuckDB, so the question became whether geopandas earns its tree on
its own. It does not: the joins are over 75,865 sign rows and 9,297 street
segments, which a dict keyed by blockface handles directly, and every geometry
operation needed is a shapely call. Dropping it removes pandas, pyarrow, and
fiona from the install.

**Would reverse it:** an ETL step that genuinely needs grouped tabular
operations at a size where dict-based code gets slow or unreadable.

## D3 (refined). Arrow arity from the glyph, bearing from `arrow_direction`

**Decided:** 2026-09-14, refining D3. The glyph in the description says only
whether the sign has one arrow or two (`-->` with any dash count, or the words
`SINGLE ARROW`, versus `<->`). The compass word in `arrow_direction` says
which way a single arrow points. It is populated on every single-arrow row and
blank on double-arrow and no-arrow rows.

**Why:** `<--` occurs on 2 of 74,590 active rows. DOT writes every single
arrow as `-->` and records the bearing separately. See `docs/DATA.md`.

## D9. Active filter is `sign_design_voided_on_date IS NULL` only

**Decided:** 2026-09-14. `record_type` is the literal `Current` on every row
citywide, so it carries no information. The active set is Manhattan rows with
a null voided date: 74,590 rows. The `record_type` predicate is kept as an
assertion in staging so a future change in the export is noticed.

## D10. Non-regulation panels are classified out before parsing

**Decided:** 2026-09-14. About 22% of active rows are MTA bus route panels,
pay-by-cell locator plates, and blank location panels. They are kept in the
`sign` table for audit but never produce a `regulation` row and do not count
against parser coverage.

## D11. Calendar and basemap sources

**Decided:** 2026-09-14. The DOT ASP suspension calendar is fetched from
nyc.gov as ICS with a browser User-Agent (the 403 is UA-based only). The
2026 ICS yields 47 suspended days over 42 dates; the seven "meters not in
effect" days in its descriptions are the six Major Legal Holidays, so that
rule is read from the calendar rather than hardcoded. The basemap is a
Manhattan extract of the Protomaps daily build, obtained with `pmtiles
extract` over range requests (23 MB, no planet download), with fonts and
sprites vendored from the protomaps basemaps-assets repository and the style
vendored from the `@protomaps/basemaps` npm package. `build-metadata.protomaps.dev`
and `raw.githubusercontent.com` are added to the allowlist for the basemap
step only.

**Corrected 2026-09-15:** they were not, and should not be.
`config.ALLOWED_HOSTS` still holds six hosts and neither of these is among
them. Nothing in `curbcheck sync` or `curbcheck serve` fetches a basemap: the
tiles come from `scripts/fetch_basemap_tiles.py` and the style, glyphs and
sprites from `scripts/fetch_basemap_assets.py`, both developer-time scripts
outside the package, each carrying its own https-and-host check over its own
two- or one-host list. Adding these hosts to the application allowlist would
widen the app's egress surface for a step the app never performs
(docs/SECURITY.md residual 6).

## D12. Engine readings of three under-specified rules

**Decided:** 2026-09-14. (a) `including_sunday` overrides the holiday meter
exemption as well as the Sunday one: a sign that states its own calendar is
taken at its word. (b) A posted `max_duration_min` is enforced for the whole
window even on days when the meter is not running, because DOT does not say
the time limit lifts with the charge. (c) Where several ParkNYC zones cover one
blockface (SPEC §13.1b) the search quotes the dearest and sets
`price_known=false` with a "confirm at the meter" caveat.

**Why:** each case can only err towards a false "legal" or a false "illegal",
and SPEC §8.6 makes a false legal the P0 defect. (a) and (b) both choose the
reading that keeps a spot off the legal list; (c) never understates cost.

**Corrected 2026-09-15 (b):** "the whole window" was read as the wall-clock
length of [T1, T2], which is wrong in the other direction. A `2 HMP 8AM-7PM`
sign states nothing about 7PM onwards, so a stay from 18:00 to 21:00 spends 60
of its 180 minutes under the limit and is legal; the engine called it illegal
and cost the user the spot. The limit is now measured against the minutes the
rule is *in force* for, summed over the window rather than taken per stretch so
that two separated limited periods do not each draw a fresh allowance. The
original point of (b) stands: a rule in force on a day its meter is not running
still counts, because the rule is still in force.

**Would reverse it:** a DOT statement that a time limit applies only while the
meter is in operation, which would make (b) wrong and cost the user spots.

## D13. Unparsed signs still get a `regulation` row

**Decided:** 2026-09-14. A sign the grammar cannot read is written as a
`regulation` row with `parse_method='unparsed'`, `parse_confidence=0`, and its
raw description. The engine reads only that row's metadata, never its fields,
and forces the whole segment to AMBIGUOUS.

**Why:** the stacking engine sees a segment as its list of `regulation` rows.
If an unreadable sign produced no row, a blockface whose only other sign says
"2 HOUR PARKING" would come back a confident LEGAL with the dangerous sign
invisible. See `docs/ARCHITECTURE.md` and SPEC §11.

## D14. The CSP adds `worker-src 'self' blob:`

**Decided:** 2026-09-14. The Content-Security-Policy the server sets is SPEC
§3.4's string plus one directive: `worker-src 'self' blob:`.

**Why:** the vendored `web/vendor/maplibre-gl/maplibre-gl.mjs` starts its
worker from `URL.createObjectURL(new Blob([...], {type: 'text/javascript'}))`.
Under `default-src 'self'` with no `worker-src`, that URL is blocked and the
map never renders. The directive is scoped to workers, so it does not widen
`script-src`, and `blob:` was already allowed for `img-src` by the spec's own
string. Verified by reading the vendored file rather than assumed;
`tests/test_api.py` pins the exact header.

**Would reverse it:** revendoring MapLibre as a build that loads its worker
module by URL (`new Worker(url, {type: 'module'})` against a real path), which
would make `worker-src 'self'` sufficient.

## D15. `curbcheck serve` starts without a database

**Decided:** 2026-09-14. A missing `data/curbcheck.sqlite` prints a line
naming `curbcheck sync` and the server starts anyway. `/api/health` reports
`status: "degraded"`, and every data endpoint answers 503 with the same
instruction.

**Why:** SPEC §11 is about surfacing failure rather than hiding it, and the
surface the user is looking at is the page. Refusing to boot leaves a
first-time user with a terminal message and no app; booting leaves them with a
loaded UI that says what to run next. It also keeps `/api/health` honest,
which is the endpoint whose whole job is to answer questions about the
database.

## D16. Three basemap wiring facts the vendored libraries forced

**Decided:** 2026-09-15, refining D11. (a) `web/map.js` imports MapLibre as a
namespace (`import * as maplibregl`), not as a default: the vendored v6 build
exports named bindings only, and a default import fails the module load with
"does not provide an export named 'default'". The wiring snippet in
`web/vendor/MANIFEST.md` showed the default form and was wrong; it was
corrected on 2026-09-15 and now shows the namespace import. (b) The
style file keeps the host-free `sprite: "/basemap/sprites/v4/light"`, and
`loadStyle()` prefixes the origin at load time, because MapLibre 6 parses the
sprite URL with `new URL(value)` and throws "Invalid sprite URL …, must be
absolute" for a root-relative path. Glyph URLs have no such requirement. (c)
Only glyph ranges `0-255`, `256-511`, and `8192-8447` are vendored, per
fontstack. The third is not optional: Manhattan labels contain U+2013 and
MapLibre 404s without it.

**Corrected 2026-09-15 (c):** three ranges were not enough. The validation run
logged a 404 for `768-1023` and for `7680-7935` on *every* map load
(docs/VALIDATION.md U2) — Combining Diacritical Marks and Latin Extended
Additional, which Manhattan's Vietnamese and accented business names are written
with. Both are now in `GLYPH_RANGES`, for each of the three fontstacks the style
uses; `web/basemap/` grows from 1.13 MB to 1.76 MB. The rest of (c) stands: a
range outside Latin/Greek/Cyrillic still 404s, by choice.

**Why:** all three were measured in the browser against the running server, not
inferred. (b) matters beyond convenience — putting an absolute URL in the style
file would bake a host and port into a committed artifact and would break the
"nothing in `web/` points off-host" test in `tests/test_web_static.py`.

**Would reverse it:** a MapLibre release that resolves sprite URLs against the
style URL, or a revendored build with a default export.

## D17. A meta sign makes its post's rule stack ambiguous

**Decided:** 2026-09-15. `METERS ARE NOT IN EFFECT ABOVE TIMES` (594 active
rows, 1 distinct string) is stored as a `regulation` row like any other sign,
but with a placeholder rule — `action=park, permitted=False, all days, no
times, flags.meta=True` — written at `parse_confidence=0.7`. It is inserted
into every regulation segment that carries a sign from the *same post*, not
only the segments its own whole-blockface span produced. The metered rules on
those segments get a `parse_notes` line naming the sibling.
`engine.resolve.ambiguity_reason` already turns `flags.meta` into AMBIGUOUS, so
the whole stack reaches the user as "a sign here modifies another sign".

**Why:** SPEC §8.5 says a meta sign is not a standalone regulation and must be
linked to the sibling it modifies. The sibling is the panel above it on the
same post, and posts are where the link is visible: the meta sign carries no
arrow, so `segments` gives it the whole blockface-side while the metered sign it
modifies keeps a shorter arrow span, and the two would otherwise never meet in
one stack. We cannot yet tell which times "ABOVE TIMES" points at — the longer
form of the string says it is "TO BE USED ONLY FOR CONFLICTING STREET CLEANING
AND METERED PARKING REGULATIONS" — so the honest answer is that the combination
is unreadable, not a recomputed price. The placeholder is prohibitive for the
same reason D13's is: a reader that somehow missed `flags.meta` must still err
towards keeping the spot off the legal list. 1,207 of 34,092 segments are
affected (920 before D25 cut the spans a meta sign's post reaches).

**Would reverse it:** reading the meter hours off the sibling panel reliably
enough to compute "meters off during the times above", which would turn these
920 segments from ambiguous into priced.

## D18. The meter join writes one row per centerline segment, and falls back on the passenger rate

**Decided:** 2026-09-15. Three things about `etl/meters.py`. (a) A ParkNYC
blockface writes one `meter_rate` row per centerline segment its chain covers,
not one row per blockface. (b) Prices come from `all_vehi_2` / `commerci_2`,
which hold the progressive rate string; `all_vehicl` is the session limit,
`all_vehi_1` the hours in effect and `all_vehi_3` the maximum session *dollars*.
(c) The rate-zone polygon fallback fires when a metered segment has no
*passenger* rate, which includes the 620 Manhattan blockfaces ParkNYC lists
with commercial rates only.

**Why:** (a) `regulation_segment.segment_id` is the segment covering that
span's midpoint, and `engine.search` looks a rate up by `(segment_id, side)`,
so a blockface spanning a three-segment chain would be invisible to two thirds
of its own spans. (b) measured against the snapshot: `all_vehi_2` is the only
column of the four that contains a rate per hour. (c) a commercial-only row
answers no question a passenger query asks, and the zone polygon is published
DOT data, not a guess — it lands with `source='rate_zone'` and confidence 0.6,
below the ambiguity threshold. Result on the 2026-09-15 snapshot: 3,561 ParkNYC
rows and 842 zone rows, and no metered segment in Manhattan is left unpriced.

**Would reverse it:** DOT publishing a blockface id on the sign or centerline
data, which would make the name join and the per-segment explosion unnecessary.

## D19. Staging and the parser share one panel classifier

**Decided:** 2026-09-15. `etl/stage.py` calls `etl/parse.panel_class` and keeps
its label verbatim, so `sign.panel_class` now holds the parser's richer values
(`regulation`, `panel:pay_by_cell`, `panel:mta_route`, `panel:location`,
`panel:template`, `panel:blank`, `panel:supersedes_only`,
`panel:parking_geometry`) instead of the four-value enum it carried before.
The dependency runs stage -> parse and never the other way.

**Why:** the two classifiers disagreed. A sign staged as a regulation that the
parser then labelled a panel produced no rule but still counted as a regulation
row, so the coverage percentages and the row counts described different sets.
One classifier cannot disagree with itself. The cost is a wider `panel_class`
vocabulary in the API's `/api/segment/{id}` response, which is audit
information the UI shows verbatim.

## D20. A two-way arrow stops at the next sign, not at the corner

**Decided:** 2026-09-15. A `<->` post extends in each direction independently:
to the nearest post of the same family, failing that to the nearest post of any
other regulation family, and only where a direction holds no post at all to the
corner. The single-arrow rule is unchanged.

**Why:** the old rule fell back to the whole blockface-side as soon as one
direction had no same-family post, and on 8 AVE side E that put a bus stop over
the whole face and made a metered curb read illegal in all three validation
windows — the only verdict error in the 30-side ground-truth sample
(`docs/VALIDATION.md` §4 D1). A new sign is where a new regime starts, which is
the reading a traffic agent takes and the one DOT's own posts support. Measured
over the 2026-09-15 snapshot: whole-side spans 23,234 -> 1,964, arrow-extended
spans 31,438 -> 53,622, and 110 of 11,695 centerline sides that were illegal for
the Wednesday window read legal afterwards (99 went the other way).

**Would reverse it:** evidence that DOT intends a lone `<->` to govern past the
next sign of another kind, which would make the old whole-side fallback right
and this a source of false "legal" verdicts.

## D21. Divided roadways pick a carriageway by which way the curb faces

**Decided:** 2026-09-15. Where two equally short chains span a block, the snap
keeps the one for which offsetting towards `side_of_street` moves *away* from
the sibling chain. The published x/y is the secondary tiebreaker and
`snap_notes` records which of the two decided.

**Why:** CSCL models Park Ave, Lenox Ave and Peck Slip as two parallel
centerlines, and the breadth-first walk took whichever it reached first: 5,109
of 70,671 snapped rows sat on a block where two chains competed, and on PARK AVE
E 50->E 51 all seven `Side: W` signs landed on the east carriageway, 77 ft into
the avenue (`docs/VALIDATION.md` §4 D3). "The outer curb of a divided roadway"
is a geometric statement, so it is computed from the offset direction relative
to the sibling rather than from the compass, and holds on the tilted grid.

**Would reverse it:** a CSCL release carrying a roadway-type or blockface id that
names the carriageway outright, which would beat any inference from geometry.

## D22. `DEAD END` resolves to the street's own terminal node

**Decided:** 2026-09-15. When exactly one of `from_street`/`to_street` is a
non-street token, the chain is walked away from the named corner until the
street stops — a dangling endpoint, a name change, or a fork, where it refuses —
and that node is the other end of the block. The resolved end scores 0.85 on
name quality instead of 1.0.

**Why:** DOT writes `DEAD END` on 610 name references, and none of them
resolved, so 604 sign rows over 147 blockface-sides were dropped whole,
including a blockface DOT posts `NO STANDING ANYTIME` on
(`docs/VALIDATION.md` §4 D2). A dead end is a fact about the street's own
geometry, which the centerline graph already holds. A fork is refused because
which branch dead-ends cannot be told from the names, and a guess would put the
signs on the wrong block half the time. Dead-end misses fell 604 -> 22.

**Would reverse it:** a DOT clarification that `DEAD END` can mean a physical
barrier mid-block rather than the end of the street's run.

## D23. A side with no rules gets a placeholder span, not silence

**Decided:** 2026-09-15. After the spans are resolved, every `rw_type` 1
centerline side that no span covers gets a `regulation_segment` with
`derived_from=[]`, `confidence=0`, `capacity_cars=NULL`, the offset curb line
for the whole side, and `gap_kind` — `no_signs` where the source lists no active
sign for that blockface-side, `unmatched_signs` where it lists some and none
could be snapped. Highways, bridges and connectors get none: signs are posted
along them, but there is no parkable curb to leave grey.

**Why:** SPEC §11 makes "no sign data on this block" a non-negotiable state, and
it was reaching only the 32 spans that had a span and no rules. 10,208 of
Manhattan's 22,204 centerline sides drew nothing at all, and a blank map is read
by a driver as "nothing here", not as "unknown" — the same hazard SPEC §11
exists to prevent, arriving through a different door (`docs/VALIDATION.md` §5).
The placeholder also makes the data-gap/matching-gap split SPEC §11 asks for
visible per side rather than only in a citywide histogram: 5,298 `no_signs` and
358 `unmatched_signs` on the 2026-09-15 snapshot.

**Would reverse it:** a curb-edge dataset that says where parking is physically
possible, which would let a side be left out honestly instead of drawn grey.

## D24. A legal span a prohibition overlaps is ambiguous, not legal

**Superseded in practice by D25, kept as a guard.** D25 flattens the spans on a
blockface-side so the prohibition and the permission arrive in one stack and
`resolve` settles them per foot of curb, which is the reversal this entry names
below. `_demote_contested_spans` stays because the engine opens whatever
database file it is pointed at, `curbcheck.sqlite.prev` is a pre-D25 one, and
D25 does not reach the 175 pairs of spans that overlap across two *different*
chains through one segment (docs/VALIDATION.md §10). It now logs a warning
whenever it fires. Everything below is the reasoning as it was written.

**Decided:** 2026-09-15. `engine.search._demote_contested_spans` turns a span
the window reads LEGAL into AMBIGUOUS when another span on the same
`(segment_id, side)` reads ILLEGAL and its curb line overlaps by more than a
foot. The span geometry is not moved and D20 is unchanged.

**Why:** D20 has a `<->` post extend to the next post of *any* family, so two
adjacent posts of different families each claim the whole gap between them and
the spans overlap by design (`tests/test_etl_segments.py`
`test_a_different_rule_keeps_its_own_span_however_much_it_overlaps`). Each span
is a separate `regulation_segment` row with its own rule stack, so
`resolve.evaluate_segment` — which stacks most-restrictive-wins *within* one
stack — never sees the other post's rule. A `2 HMP <->` reaching back over a
`NO STANDING ANYTIME <->` therefore read LEGAL over curb that is not: measured
on the 2026-09-15 snapshot for a Wednesday 10:00–12:00 window, 234 of 9,164
legal spans, about 44,500 ft of curb. SPEC §8.6 makes a false "legal" the P0
defect, so this is the one class of bug that cannot be left for a later sync.

The contest is resolved as "unknown" rather than by moving a boundary because
the two posts genuinely disagree about where the boundary is; SPEC §11's
ambiguous state is defined as "could not read the signs **or the signs
conflict**", and the frontend already words it that way. Doing it at query time
rather than in `segments` keeps D20's measured span output — and every figure
derived from it — intact.

**Would reverse it:** a span model that holds overlapping rules in one stack, so
the prohibition and the permission could be resolved most-restrictive-wins per
foot of curb and the uncontested remainder stay legal. That is the right fix and
is more than a query-time guard; this is the safe reading until then.

## D25. A blockface-side is stored as one non-overlapping stack, not as overlapping spans

**Decided:** 2026-09-15. After the D5 merge, `etl.segments` cuts every span on a
blockface-side at every other span's boundary and writes one `regulation_segment`
per resulting stretch, carrying the union of the signs that cover it. The
pre-flatten per-post spans stay in memory only; `derived_from` is the provenance,
and `regulation` rows are per (span, sign) as before.

**Why:** D20 has a `<->` post extend to the next post of *any* family, so a
permission and a prohibition each claim the curb between them and their spans
overlap by design. Each span was its own row with its own rule stack, and
`resolve.evaluate_segment` stacks most-restrictive-wins only *within* one stack,
so a `2 HMP <->` reaching back over a `NO STANDING ANYTIME <->` read LEGAL over
curb that is not. D24 answered that at query time by demoting the whole
permissive span to AMBIGUOUS, which is safe but throws away the legal remainder
— the curb past the ban, which DOT's own posts say is parkable — and leaves the
model saying something false while the query patches over it. Cutting at the
boundaries says the true thing once: 0–50 ft the ban alone, 50–150 ft both rules
in one stack, 150–364 ft the meter alone. Measured on the 2026-09-15 snapshot:
overlapping span pairs 14,814 -> 175 (the residue is cross-chain, below), legal
spans a prohibition overlaps in the Wednesday window 2,267 -> 59, real spans
28,360 -> 28,436, `regulation` rows 38,604 -> 54,750, and the curb counted twice
falls from 6,270,750 ft to 4,733,456 ft. No sampled side's verdict moved
(`scripts/validation_regress.py`), and the six spot checks still pass — two of
them on new blocks, one because the block it used to test was itself a false
legal this fixes.

A stretch no span covers is dropped rather than turned into a placeholder. The
flatten does not change which curb is covered — `coverage.sides_with_rules` is
13,114 before and after — so a gap inside a side with rules is not new here, and
D23's placeholder stays one row per side, which is what makes "a side with a
real span never draws grey" a checkable invariant. Painting partial gaps is a
separate change with its own count to measure.

**What it does not reach:** two *chains* through one centerline segment. DOT
writes some signs against `E 16 ST -> E 18 ST` and others against
`E 16 ST -> E 17 ST`, which are two blockface-sides here and are flattened
independently, so 175 pairs of spans still cover the same curb from different
chains. That is why D24's guard stays, and why it now logs.

**Would reverse it:** a finding that one `regulation_segment` row per stretch is
too coarse for provenance — that a user needs to see which post a rule came from
per stretch, rather than the set of signs on it. `regulation` rows are already
per (span, sign), so this would be a display change, not a model change.

## D26. A curb span is stacked per centerline segment-side, not per chain-side

**Decided:** 2026-09-15. After the arrow rules resolve a blockface-side's spans
in chain feet, every span is cut at the boundaries of the centerline segments it
runs over and re-expressed in each segment's own digitization direction, with
the curb named as the left or right of that direction and the compass letter
kept only as a label. D25's flatten then runs per `(segment_id, local side)`, so
the spans that reach one piece of curb from *any* chain land in one stack.
`regulation_segment.segment_id` is now exact and `start_ft`/`end_ft` are
segment-local; both were chain quantities filed under "the segment at the
midpoint" before.

**Why:** D25 flattened per blockface-side, and DOT writes some signs against
`1 AVENUE, E 16 ST -> E 18 ST` and others against `1 AVENUE, E 16 ST -> E 17 ST`.
Those are two chains over one piece of curb, flattened independently, so 175
pairs of spans still covered the same curb and the permission in one of them
still read LEGAL over the prohibition in the other — SPEC §8.6's P0 defect,
surviving D25 (`docs/VALIDATION.md` §10.2). The segment is the only frame the
two chains share, and it is not the chain's: CSCL digitizes each segment in its
own arbitrary direction, so a chain crossing one backwards has to convert with
`length - x`, and the compass letter has to become left-or-right of the
segment's own direction before "west curb" from either chain is one key.

Measured on the 2026-09-15 snapshot: overlapping span pairs 175 -> 16, real
spans 28,436 -> 30,524, `regulation` rows 54,750 -> 59,020, and the curb counted
twice falls a further 66,729 ft to 4,666,727. 1 AVE and E 82/E 84 ST, which were
136 of the 204 spans in the residue, hold none: segment 2572 carried 8 spans
over 906 ft of a 259 ft segment and now carries 10 over 518 ft, which is both
its curbs tiled exactly once. `_demote_contested_spans` has nothing to do.

Two smaller things fall out. Nine segments whose two chains lettered the curbs
by different axes — FDR DR, JANE ST, BATTERY PL and six more — were drawing a
D23 placeholder over a curb that already had rules, because the placeholder key
was the compass letter; the segment-local key ends that, and grey rows fall
5,656 -> 5,648. And `coverage.sides_with_rules` reads 13,111 rather than 13,114
because three curbs that two chains lettered two ways (E 120 ST, RIVERSIDE DR,
JANE ST) were being counted twice; no curb lost its rules.

**What it does not reach:** CSCL carries one roadway twice. The Riverside Drive
viaduct is both `RIVERSIDE DR` and `12 AVE` with two `physicalid`s and identical
geometry, DOT posts signs under both names, and both draw spans over the same
curb — the 16 pairs left. That is a centerline deduplication, a different change
with its own count, and `--overlaps` measures it because it compares geometry;
`_demote_contested_spans` keys on `segment_id` and cannot see it.

**Would reverse it:** a finding that a driver needs the span measured from the
corner DOT names rather than from the centerline segment's own start. The chain
and its from-node are still in `street_segment`, so that is a presentation
change, but it would be a reason to store both.

## D27. Legality by absence is a separate state from legality by permission

**Decided:** 2026-09-15. `SearchResult.basis` says why a LEGAL verdict is legal:
`posted` when at least one rule in the stack permitted parking somewhere in the
window, `absence` when no rule was in force for any part of it, `null` on the
three non-legal verdicts. An absence verdict reads "No posted rule is in effect during this
window" and carries `confidence_shown: false`, as does every `no_data` span, so
the percentage is in the audit block and not beside the verdict. Within the
ranked legal list, spans whose cost differs by less than 0.5 min-equivalents
(`engine.search.COST_TIE_BAND`) are ordered `posted` before `absence`; the band
is anchored on the first span in each band rather than on the previous one, so a
dense run cannot chain into one tie and stop the cost ranking meaning anything.

**Why:** absence of a rule really is a permission — 34 RCNY 4-08 allows parking
where no posted sign applies, which is why `resolve` returns LEGAL for it — but
it is also the engine reporting that it read nothing. The UI had no way to tell
the two apart, so it printed the same green badge and the same "100% confidence"
for both: all 100 ranked results for 1519 3rd Ave, Wednesday 10:00–12:00 read
"Legal · 100% confidence · no posted rule covers this window", the app
announcing that it found nothing in its most confident voice
(`docs/ux/UX_AUDIT.md` P0-1). The confidence number makes it worse, because it
is the confidence of an *empty* stack: nothing was parsed, so nothing could be
parsed badly. Splitting the state in the engine rather than in the frontend
keeps the distinction on the contract, where the ranking can use it too.

The tie-break is deliberately narrow. Rank order was already being decided by
walk-time differences of tenths of a minute (`UX_AUDIT.md` P2-3), so preferring
a span with a sign the driver can go and read, among spans that cost the same,
spends nothing the user could perceive. It never reorders spans whose cost
differs by more than the band.

**What it does not reach:** the caveat "Part of this window has no posted rule in
effect; read the curb." still fires on a `posted` verdict whose window is only partly
covered, and that mixed case is one word in a caveat list rather than a state of
its own. `basis` answers "was anything posted at all", not "how much of the
window did it cover"; `intervals` already carries the latter and nothing renders
it yet.

**Would reverse it:** a finding that drivers read "no rule found" as "not
allowed" and walk past legal curb. The verdict would stay LEGAL either way — this
is presentation — but the ranking preference would have to go.

## D28. A destination outside the centerline is refused, not answered with zero results

**Decided:** 2026-09-15. `POST /api/search` and `GET /api/reverse` return HTTP
422 `outside_coverage` for a point further than 250 m from every
`street_segment` geometry (`engine.coverage`), `GET /api/geocode` never offers a
candidate outside it, and `GET /api/health` publishes
`coverage: {area, bbox}` computed from the same table.

**Why:** a pin dropped in West New York, NJ was accepted and answered "Nothing
within that walk radius. Try a longer walk or a different time", advising the
user to widen a radius that was never the problem, and nothing in the UI said
CurbCheck is Manhattan-only (`docs/ux/UX_AUDIT.md` P0-3). Coverage is measured
against the centerline rather than a borough polygon because the centerline is
what actually decides whether a query can be answered, and it needs no second
dataset to stay in step.

Measured on the 2026-09-15 database (11,102 segments), distance to the nearest
centerline: Great Lawn 184 m, the Ramble 177 m, Sheep Meadow 92 m, Inwood Hill
Park 137 m, Roosevelt Island 18 m, Randalls Island 19 m — all in. Hudson
midstream 1,090 m, Long Island City 1,068 m, West New York NJ (nothing within a
kilometre) — all out. The bbox is
`[-74.046770, 40.684050, -73.906821, 40.879046]`.

**What it does not reach:** two edges, both of which fail in the safe direction.
The centre of the Central Park reservoir is 287 m from any centerline and is
refused, which is a wrong answer about a stretch of water. And the
Manhattan-registered bridge centerlines reach far enough over the East River
that a point in DUMBO is 240 m from one and is accepted; it then returns no
spans, because DOT letters no Manhattan curb there.

**Would reverse it:** a coverage polygon in the data — a borough boundary from
Open Data would let the test be point-in-polygon, which would put the reservoir
back in and DUMBO out. That is a new dataset with its own sync and licence
entry, and the distance test needs none.

## D29. Address autocomplete is served from a local index, with no online option

**Decided:** 2026-09-15. Add OTI's `AddressPoint` (`uf93-f8nk`, 63,245 Manhattan
doors) and `CommonPlace` (`t95h-5fsr`, 5,827 names) to the ETL, fold them into a
vocabulary index in SQLite, and serve `/api/geocode` from it per keystroke. Take
no online geocoder, not even opt-in and off by default.

**Why:** three reasons, in the order they matter.

*Privacy.* Autocomplete sends a prefix of the destination on every keypress,
which is threat T6 — it leaks the typing, not just the answer — and SPEC §3.2
makes the egress allowlist a config constant precisely so "it probably works"
is not a reason to add a host. Ten services were read in full
(`docs/ux/AUTOCOMPLETE_RESEARCH.md` §3). Nominatim forbids autocomplete in
writing; Mapbox's free tier forbids storing the result, and we must store the
destination to search it; Google requires results not be shown beside a
non-Google map, and ours is MapLibre. The closest call is NYC Planning Labs
GeoSearch — same data lineage, free, no key, run by the city that publishes our
inputs — and it publishes no terms of service, no acceptable-use policy and no
rate limit at all. Self-hosted Pelias is the only option with no privacy cost
and it needs Elasticsearch, Node and ~4 GB of libpostal data to replace 13 MB
of SQLite.

*Accuracy.* Address points are surveyed, and they are what made the local
option better rather than merely cheaper. Over 800 random Manhattan doors the
old centerline interpolation landed a median 95 ft from the real door (p95
1,634 ft, worst 45,832 ft); over a 400-door sample the index-backed ladder is at
a median of 0 m, p95 0 m, worst 29 m, with nothing unmatched. The old rung
survives as the last one, for the 233 centerline streets AddressPoint files no
door on.

*Speed.* 0.32 ms median per suggestion on local disk, 10.9 ms on this
container's 9p mount, against a 20 ms target. FTS5 was measured and rejected: it matches
literal text, so every spelling (`THIRD AVENUE`, `86`, `LEX`) would have to be
pre-expanded into the indexed string anyway, it cannot express "these two
streets meet at a node", and `MATCH` is a query language the user types into,
where a stray `"` is an error and a stray `*` is a wildcard scan. A covering
index has no syntax to escape (T3).

**Cost:** +13.2 MB on a 79.0 MB database (+16.7%) and 54 s of a 143 s sync.
The centerline and unsnapped-sign indexes it needed alongside cost a further
1.6 MB and pay for themselves several times over in the rest of the app.

**Would reverse it:** a need to geocode outside Manhattan, where AddressPoint's
other 904,626 rows would still serve but the centerline, the signs and the
meters would not — so it would not be this decision that changed.

## D30. SPEC §17 is rendered as a persistent strip plus a full-notice disclosure

**Decided:** 2026-09-15. The §17 disclaimer is no longer a 121-word paragraph
at the top of the page. An undismissible 36 px strip carries the two
load-bearing sentences — *"Advisory only. Read the posted sign."* and *"Grey
means no data, not no restriction."* — and a `Full notice` disclosure expands
the complete §17 text in place. The same text is printed verbatim at the foot
of **every** detail sheet, so it is inside every verdict as well as at the top
of every screen.

**Why:** the old banner was 106 px (12%) of a 900 px viewport and 275 px (33%)
of a 390×844 phone, pushing the map entirely off the first screen
(`docs/ux/UX_AUDIT.md` P1-11, `docs/ux/screens/06-first-load-390.png`). Nine
bolded clauses in one paragraph are read as boilerplate on the second visit;
the sentence that changes behaviour is read every time.

**Why this is still SPEC §17.** The spec requires a persistent banner plus
per-verdict text, and both survive. The strip cannot be dismissed — there is no
close control and nothing is persisted to storage, which `tests/test_web_static.py`
pins — the full text is one click from any screen, and the per-verdict text is
the "Before you park" block, which is never behind a disclosure.

**What it does not reach:** `docs/ux/DESIGN_DIRECTION.md` §3 wanted the full
notice *expanded* on the first load of each session. It ships collapsed,
because an amber wall on first load is the failure mode the compaction exists
to fix, and the brief for the build asked for a 36 px strip with the full text
one click away. No session state is stored either way.

**Would reverse it:** evidence that the disclosure is never opened. If the
short strip is the only §17 anyone reads, the compaction has replaced the
notice rather than fronting it, and the full text goes back to being expanded
on the first load of a session — the cost the audit measured, paid once per
session instead of once per visit.

## D31. A name is found by any word in it, and ranked by where the words landed

**Decided:** 2026-09-15. The suggester no longer matches a place or a street
only from the start of its name. Every non-stopword a person types must
prefix-match some word of some *spelling* of the name, in any order, and the
answer is scored by where those words landed: the name typed whole, then the
names the query starts, then the names one of its words starts, then the names
that merely contain them. A second index — `place_token` and `street_token`,
one row per (word, position, spelling) — is what makes that a range scan
rather than a `LIKE '%…%'` over 24,000 names.

**Why:** the old rule matched the words in typed order and treated only the
last one as a prefix, so `high school of fashion` autocompleted and `fashion`
returned nothing at all, which is the one word anybody remembers about the High
School of Fashion Industries. The same rule hid Avenue of the Americas behind
its first word (`ave`), the Guggenheim behind `solomon`, and every school
behind the honorific in front of its name. Requiring the typed order buys
nothing: a person types the word they remember, not the word the city filed the
name under.

**Why a spelling and not a synonym list.** A name has several spellings and
they are all real: `E 86 ST` is also `86 ST`, `W 125 ST` is also
`DR M L KING JR BLVD`, `HIGH SCHOOL OF FASHION INDUSTRIES` is also
`HS FASHION INDUSTRIES`. Indexing each spelling as its own word sequence means
the position of a word is the position it has *in the spelling the user typed*,
so "HOUSTON" is the first word of a name rather than the second, and the
`streets.NAME_ALIASES` keys put `KING` and `MALCOLM` in the index without a
single new alias being invented. Six landmark aliases are hand-written
(`etl.addresses.PLACE_ALIASES`) and they are the only invented text in the
index: each one is a name whose colloquial form shares no word with the legal
one (`MOMA`, `MSG`) or whose legal form belongs to a neighbouring building
(`PORT AUTHORITY` is the bus terminal, not the post office).

**What it costs.** `place_token` carries the spelling's word sequence on every
row, which is 1.2 MB rather than 0.4 MB, and `street_token` is 0.2 MB more:
**+1.1 MB on 93.5 MB**. It is bought deliberately. Scoring a candidate from the
row means the scan is covering, and on this container's 9p data mount reading
the 200 matching `place` rows instead cost 55 ms warm and 2.5 s cold against
1.5 ms for the scan — the same trade as the `ix_sign_unmatched` covering index
in `db.py`.

**Why streets still outrank places.** The two bands of the ladder do not
overlap: a street matched by a word scores 0.69–0.67 and a place 0.66–0.64, so
`lex` is Lexington Avenue and not the Lex Hotel, and `americas` is the avenue
and not the Americas Society. A street is a destination this app can search
both sides of for its whole length; a place is one point. The one rung above
that is a place named *exactly* what was typed (0.85), which cannot collide
with a street because `etl.addresses.stage_places` drops a place whose name
repeats a spelling of a street the centerline actually carries — 31 of them
(`MADISON`, `PARK`, `HIGH`), against 10 under the old whole-name rule.

**Would reverse it:** a popularity signal. Three of the 33 ranking cases in
`tests/test_geocode_real.py` are satisfied at rank 3 rather than rank 1 —
`fashion` puts the Fashion Institute above the High School of Fashion
Industries, `guggenheim` the bandshell above the museum — because the index has
no way to know which name a New Yorker means and will not invent one. Anything
that did (a visit count, an OSM `wikidata` tag) would replace the
shorter-name tie-break, not the word matching.

## D32. The public copy is a static site with the engine in the browser, not a hosted server

**Decided:** 2026-09-15. CurbCheck is published at `curbcheck.orentirschwell.com`
as files on GitHub Pages: the same `web/` frontend, a JavaScript port of
`engine/` and `geocode/` running in a Web Worker, and a data pack that
`curbcheck pack` compiles from the SQLite file. A weekly GitHub Actions job
runs the ETL, compiles the pack, replays the Python engine's answers through
the browser engine on that exact data, and deploys only if they agree.
`docs/STATIC_SITE.md` is the contract.

**Why not host the server.** SPEC §3 and `docs/SECURITY.md` T6 promise that a
destination never leaves the machine. A hosted `curbcheck serve` would break
that for every visitor at once — the operator terminates every request and
sees every typed address whether or not it is logged — and would put a public
listener in front of an engine written for one local user, with no rate limit,
no body cap and a 5–7 s worst-case query. Fixing all of that is about six
engineering days; it still ends with a privacy statement that says the
opposite of T6. The static site keeps T6 except for what any host sees (the
IP and which map tiles were fetched), costs nothing to run, and is faster:
the engine holds the whole pack in memory, so a search that took 0.9 s over
SQLite takes tens of milliseconds.

**Why a port and not Pyodide.** Pyodide can run the existing Python in the
browser — shapely, numpy, sqlite3 and pydantic are all packaged — but the
runtime is about 11 MB before any data, boots in seconds, and needs
`wasm-unsafe-eval` in the CSP. The information content of the database is
small: the 91 MB SQLite file is 27 MB of columnar JSON and about 9 MB gzipped
once the GeoJSON text, the repeated ids and the 59,020 copies of 1,751 sign
descriptions are factored out. The runtime reads four shapely operations,
none of which needs a library, and never imports pyproj.

**What keeps two engines honest.** The Python engine stays the reference and
the differential harness (`site/tests/harness/`) is a deploy gate, not a unit
test: 600 searches, 200 typed strings, 100 pins and every detail panel behind
them, compared exactly — verdicts, reasons, caveats, money, ranking, counts —
with a 1e-9 relative tolerance on floats and nothing else. The pack carries
exact doubles rather than quantized coordinates so that "any difference is a
bug" is literally true. Two things the review of this decision found in the
reference and fixed there: the geocoder's driver rows came out of a `set`, so
a suggestion list could differ between processes, and the money rule the
first draft wrote down (`ROUND_HALF_EVEN`) was not the one `cost.py` uses.

**What the host cannot do.** GitHub Pages sets its own headers: no
`X-Content-Type-Options`, no `frame-ancestors`, `Cache-Control: max-age=600` on
everything, and byte-range requests that Firefox caches wrongly, which is why
the basemap is sliced into 474 tile files rather than served as one PMTiles
archive. The CSP is a `<meta>` tag with everything it can express. Clickjacking
is accepted and written down (`docs/SECURITY.md`). A host that sets headers
would close it; none that does is free, and the site is a hobby.

**Would reverse it:** a change in the rules that only a server could apply
(a live 311 suspension feed the browser could not fetch same-origin), or a
Pages limit the pack outgrows — the whole site is about 60 MB against a 1 GB
cap, so not soon.
