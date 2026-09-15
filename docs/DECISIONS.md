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
