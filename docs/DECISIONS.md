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
towards keeping the spot off the legal list. 799 of 36,518 segments are
affected.

**Would reverse it:** reading the meter hours off the sibling panel reliably
enough to compute "meters off during the times above", which would turn these
799 segments from ambiguous into priced.

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
