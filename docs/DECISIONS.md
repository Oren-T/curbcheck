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
