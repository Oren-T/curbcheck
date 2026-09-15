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
