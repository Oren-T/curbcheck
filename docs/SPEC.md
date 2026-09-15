# Implementation Specification: Manhattan Curb-Parking Decision Tool ("CurbCheck v1")

*A locally hosted, single-user, offline-capable web application. This document is written so that a coding agent can implement it mechanically. It contains no application code. All factual claims about datasets, endpoints, schemas, rates, and packages carry a source and an access date (all live-checked on **September 15, 2026** unless noted). Where a fact could not be verified, it is flagged inline as **[COULD NOT VERIFY]** with a fallback.*

---

## 1. Executive Summary

CurbCheck answers one question: *"I need to be at address X from time T1 to T2. Show me every stretch of Manhattan curb within a W-minute walk where I may legally park a passenger vehicle for that entire window, ranked by a cost function trading off walking distance against money spent."* It is built by ingesting NYC DOT's **"Parking Regulation Locations and Signs"** dataset (Socrata ID `nfid-uabd`), snapping each sign point onto the correct side of the correct blockface of the NYC Department of City Planning **LION** street centerline, resolving sign points into curb **regulation segments**, parsing the semi-templated `sign_description` free text into a structured rule schema, evaluating a stack of overlapping rules against the requested `[T1,T2]` window under "most-restrictive-wins" and "legal-only-if-legal-for-the-entire-window" semantics, pricing metered segments against **ParkNYC blockface** rates, and serving a ranked map result from a web server bound to `127.0.0.1`. After the first data sync it runs fully offline.

**The three hardest technical risks:**

1. **Geometry (highest risk).** The published `sign_x_coord`/`sign_y_coord` values are not reliable enough to place a sign on the correct curb; WXY Studio explicitly *re-derives* latitude/longitude from the sign's linear description ("on street, distance from intersection") rather than trusting the published coordinates (WXY/Untapped New York, accessed 2025). Getting the sign onto the correct **side** of the correct **blockface** via linear referencing is the make-or-break engineering problem, and a naïve nearest-line spatial join is known to under-match (see §A/§B).
2. **Sign-text parsing.** The regulation itself is buried in the `sign_description` string (NYC DOT's own project team states "The Parking regulations data DOES have a temporal aspect to it, but its buried in the sign description" — Jada68/DOT-Parking-Regulations README, accessed Sep 15 2026). The text is templated enough for a deterministic grammar to cover the majority of cases but has enough long-tail variation to require a validated fallback.
3. **Safety-critical correctness.** Every serious tool in this space disclaims accuracy and tells the driver to read the posted sign (nycdotsigns.net, opencurb.nyc, both accessed Sep 15 2026). The UI must never render a confident "LEGAL" verdict where sign data is missing, the rule is ambiguous, or temporary/construction signage could override — and it must always expose the raw sign text that produced the verdict.

---

## 2. Competitive and Prior-Art Assessment

| Tool | Coverage | Time-aware | Price-aware | Destination-anchored search | Data source | API availability | Cost | Last evidence of active maintenance |
|---|---|---|---|---|---|---|---|---|
| **nycdotsigns.net** (NYC DOT official "Parking Signs Locator") | Citywide | Partial (shows sign text; user interprets) | No | Address lookup, not window search | SIMS (source of `nfid-uabd`) + DCP Geosupport | No public API | Free (gov) | Live Sep 15 2026 |
| **parkingregulations.nyc** (WXY Studio / WXY Labs) | Citywide (~440k+ signs) | By color-category legend; not window-evaluated | No | Pan/click map; no "park for this window" query | SIMS, with **re-derived** lat/long | No documented public API | Free | Live Sep 15 2026 |
| **opencurb.nyc** (OpenCurb) | **Midtown Manhattan only (30th–59th St, both E/W)** | **Yes** — "an action is permitted if it's permitted during the entire time frame" | Partial (`meter_applies`, `meter_duration`) | **Yes** — `coord`, `radius`, `StartDate/Time`, `EndDate/Time` params | NYC DOT + OpenStreetMap | GeoJSON HTTP API (doc.html) | Free basic tier | Site live Sep 15 2026; **coverage dormant/limited** — treat schema as design reference |
| **github.com/Jada68/DOT-Parking-Regulations** | Citywide (WIP) | N/A (geometry-mapping project) | No | No | `nfid-uabd` + PostGIS + LION | Scripts (reference only) | Free (open source) | README notes "As of 3/20/2024, we have figured out how to map to the curbline"; presented Open Data Week 2024 |
| **SpotAngels** | Multi-city incl. NYC | Yes | Yes | Yes | **Abandoned city open data as "not accurate"; rebuilt from crowdsourced sign photos + computer vision** (SpotAngels/Medium blog, accessed Sep 15 2026) | Proprietary | Freemium app | Active 2026 |
| **SF "Map of Parking Regulations"** (`qbyz-te2i`, DataSF) | San Francisco | Partial | No | No | SFMTA blockface | Socrata | Free (gov) | Dataset self-describes "has not been comprehensively updated or vetted for accuracy" |
| **Philadelphia curb map (Azavea)** | Philadelphia | Partial | No | No | City curb inventory | Varies | Free | Reference only |
| **SmoothParking** (cited by NYC DOT) | NYC | Partial | — | — | Sign data | Proprietary | — | Referenced by Jada68 README as prior private attempt |

**Gap statement.** No free tool combines all three of: (a) **destination-anchored** proximity search (walk radius from address X), (b) full **[T1,T2] window** legality evaluation ("legal for the *entire* window"), and (c) a **money-vs-walking cost ranking**, over **all of Manhattan**. OpenCurb has the closest query *semantics* but is limited to Midtown and appears dormant; WXY has the best citywide geometry but no window query and no price ranking; SpotAngels has all features but is proprietary and crowdsourced. CurbCheck occupies exactly the intersection: citywide-Manhattan, window-aware, price-ranked, destination-anchored — built from authoritative open data with the geometry method WXY and DOT proved feasible.

---

## 3. Threat Model and Security Architecture (placed early, per requirement)

This application runs on a personal machine and ingests remote CSV/GeoJSON/shapefiles from the public internet. Both facts create attack surface. Security overrides convenience at every decision point.

### 3.1 Threat model

| # | Threat | Vector | Mitigation (binding) |
|---|---|---|---|
| T1 | **Compromised upstream package** | A pinned dependency (or a transitive dep) is hijacked on PyPI/npm and ships malware in a new release | Hash-pinned lockfile (`uv.lock`/`requirements.txt` with `--require-hashes`; `package-lock.json` with integrity hashes); installs with `--no-build-isolation` disabled scripts where possible; **post-install scripts disabled** (`npm ci --ignore-scripts`); `pip-audit` and `osv-scanner` wired into the build; no auto-upgrade — upgrades are manual, reviewed, and re-hashed. |
| T2 | **Compromised or spoofed civic-data endpoint** | DNS spoofing / MITM / a malicious mirror serving a poisoned `nfid-uabd` export | **HTTPS with certificate verification always on** (never `verify=False`); outbound requests restricted to an explicit **domain allowlist** (§3.2); every downloaded artifact is **size-bounded, content-type-checked, and hashed**; the previous good snapshot is retained so a corrupted sync can be rolled back; ingestion is a separate step from serving. |
| T3 | **Malicious content embedded in ingested data** | Hostile strings injected into `sign_description` (e.g., `<script>`, SQL metacharacters, spreadsheet-formula injection `=cmd`, path traversal) that later render in the UI or reach the DB/parser | Treat all downloaded data as **untrusted input**. Parse with safe parsers only (§3.3). All DB access is **parameterized** (no string-built SQL). All text rendered in the UI is **output-encoded/escaped** at render time (HTML-entity encode; never `innerHTML` with raw sign text). CSV cells beginning with `= + - @` are neutralized before any export. |
| T4 | **Local privilege escalation from the running service** | The local web server is reachable by other processes/users or is exploited to run code | Server **binds `127.0.0.1` only, never `0.0.0.0`**; no remote script loading in the frontend (strict CSP, §3.4); the process runs as the unprivileged user, not root/admin; the data directory is the only writable path; no shell interpolation of any value derived from downloaded data. |
| T5 | **Prompt injection via sign text (if LLM used)** | The optional LLM fallback parser (§8.4) receives attacker-controlled `sign_description` text carrying instructions | The model is an **extraction function, not an agent**. Its output is **schema-validated (Pydantic)** and rejected if it does not conform; output is **never executed, never interpolated into a command or SQL, never treated as an instruction**. The model runs **locally** (no cloud API → no data egress, satisfies §3.2). A hard isolation boundary separates "text to classify" from "system instructions." |
| T6 | **Data exfiltration / privacy leak** | Telemetry, analytics, crash reporting, or a basemap tile server call leaking the user's destination addresses / search history | **No analytics, telemetry, crash reporting, or advertising SDKs of any kind.** The app must not phone home and must not transmit destinations or search history anywhere. Basemap tiles are **self-hosted locally** (§16), so panning the map makes zero third-party requests. |

### 3.2 Network posture (allowlist)

After initial sync the app functions fully offline. During sync, outbound HTTPS requests are permitted **only** to this enumerated allowlist:

- `data.cityofnewyork.us` — Socrata: `nfid-uabd`, `s7zi-dgdx`/`e7yp-wx55`, `da76-p95d`, `693u-uax6`/`mvib-nh9w`, LION mirror.
- `www.nyc.gov` / `s-media.nyc.gov` — DCP LION geodatabase & metadata, DOT ASP calendar PDF, meter-rate reference page.
- `api-portal.nyc.gov` / the NYC 311 Public API host — ASP suspension calendar (optional, requires free key; see §5.6).
- `build.protomaps.com` (or a chosen Protomaps mirror) — one-time basemap PMTiles download for self-hosting.

Any request to a domain not on this list is refused by the HTTP client wrapper. The allowlist is a config constant, not user-editable at runtime.

### 3.3 Safe-parser rules (no arbitrary code execution from data)

- **No `pickle`, no `eval`/`exec`, no `yaml.load` without `SafeLoader`, no `subprocess` with any downloaded value.**
- CSV parsed with the Python standard-library `csv` module or Arrow/DuckDB readers with explicit schema — never via a formula-evaluating spreadsheet engine.
- GeoJSON parsed with the standard-library `json` (then validated), not with an eval-based reader.
- **Shapefile/geospatial parsing is treated as untrusted input handling.** Shapefiles are a multi-file binary format historically prone to parser bugs; prefer ingesting the **GeoJSON or CSV** distribution of each dataset over the shapefile where both exist. When a shapefile/geodatabase must be read (LION), it is read by GDAL-backed readers (via `fiona`/`pyogrio`), the input is size-bounded, and the reader runs in the ingestion process, isolated from the serving process.

### 3.4 Local attack surface hardening

- Server binds `127.0.0.1` only.
- **CSP header** on every response: `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'`. No remote script or remote font/CDN loading — MapLibre GL JS, the PMTiles library, fonts, and sprites are all served locally from `'self'`.
- All API responses `Content-Type: application/json` with `X-Content-Type-Options: nosniff`.
- All DB access parameterized. All sign text HTML-escaped on render.

---

## 4. Vetted Dependency Table

Versions verified against canonical PyPI/npm/GitHub pages and osv.dev as of **September 15, 2026** by a dedicated dependency-verification sub-agent (see §14). CVE column reflects advisories against the package *itself* at its current version; transitive advisories are noted separately.

### 4.1 Python (backend + ETL)

| Package | Pinned version | Maintainer/org | License | Downloads (approx.) | Last release | Open CVEs | Transitive deps | Justification |
|---|---|---|---|---|---|---|---|---|
| **shapely** | 2.1.2 | Shapely project (S. Gillies; 3 maintainers) | BSD-3-Clause | tens of millions/mo | **Sep 24, 2025** | None found | GEOS bundled in wheel (**GEOS 3.13.1**, LGPL-2.1) | Core geometry ops (buffer, offset, project/interpolate for linear referencing). Wheels bundle GEOS → no system lib needed. Verified from live PyPI + ReadTheDocs "Shapely Documentation Release 2.1.2 Sean Gillies Sep 24, 2025". |
| **pyproj** | 3.8.0 | pyproj4 project | MIT | ~26M/30d | **Sep 5, 2026** | None found | PROJ bundled in wheel | EPSG:2263 (NY State Plane, feet) ↔ EPSG:4326 (WGS84) transforms. Verified from live canonical PyPI page. |
| **geopandas** | 1.1.3 | GeoPandas contributors (NumFOCUS) | BSD-3-Clause | large | ~Mar 10, 2026 | None found | pure-Python; pulls shapely/pyproj/fiona | Convenient GeoDataFrame joins during ETL only (not required at query time). **[COULD NOT VERIFY exact version from live pypi.org header; confirmed via Zenodo/deps.dev]** — fallback: pin to last hash-verified 1.1.x. *Candidate for rejection if DuckDB alone suffices (see §7).* |
| **fiona** *(or pyogrio)* | 1.10.1 | S. Gillies; Toblerity | BSD-3-Clause | large | **[date unverified]** | None found | GDAL bundled in wheel (GDAL 3.9.2, GEOS 3.11.2, PROJ 9.4.1) | LION file-geodatabase / shapefile reader. Used **only in ETL**, isolated from serving. Prefer `pyogrio` (faster, same GDAL backend) if hash-verifiable. |
| **duckdb** | 1.5.x (stable line) | DuckDB Foundation / DuckDB Labs | MIT | ~60M/mo | 1.5.0 "Variegata" Mar 9, 2026 | None found | Minimal | Single-file analytical store + spatial queries. See §7 for why chosen over PostGIS/SpatiaLite for a single-machine deploy. |
| duckdb **`spatial`** extension | matched to duckdb | DuckDB Labs | MIT | — | — | None found | GDAL/GEOS/PROJ vendored in extension | **Separate, not autoloadable** — per DuckDB docs: "To install the spatial extension, run: `INSTALL spatial;` Note that the spatial extension is not autoloadable. Therefore, you need to load it before using it: `LOAD spatial;`" As of DuckDB v1.5 the `GEOMETRY` type is built into core (stored as WKB) but the ST_* functions live in this extension. **Security note:** extension install is a network fetch — pin the extension and verify, or bundle the extension binary offline. |
| **fastapi** | ≥0.141.x (re-verify) | FastAPI team (S. Ramírez) | MIT | ~437M/30d | Jul 29, 2026 (0.141.1) | None found | starlette, pydantic | Local API surface. **[COULD NOT VERIFY exact latest from live pypi header; conflicting registry data]** — fallback: pin the last hash-verified release and re-check canonical page at build time. |
| **uvicorn** | 0.52.4 | Kludex (M. Trylesinski) | BSD-3-Clause | ~660M/mo | **Aug 19, 2026** | None (self) | h11, click | ASGI server bound to 127.0.0.1. **Transitive advisory:** h11 (GHSA-vqfr-h8mv-ghfj / PYSEC-2026-348, malformed Chunked-Encoding) — ensure the pinned h11 is a patched version; verify in `pip-audit`. |
| **pydantic** | 2.13.4 | Pydantic (S. Colvin) | MIT | ~1B/30d | May 6, 2026 | None found | pydantic-core | **Schema validation of parser output and API I/O** — central to §3.5 (T5) and §8. Beware alpha 2.14.0a1 is a pre-release; pin stable 2.13.x. |
| **httpx** | 0.28.1 (stable) | Encode (T. Christie) | BSD-3-Clause | large | Dec 6, 2024 | None found | httpcore, certifi | Sole outbound HTTP client, wrapped to enforce the §3.2 allowlist + cert verification. **Note:** 1.0.devN are pre-releases — do **not** use; pin stable 0.28.1. *(Alternative: stdlib `urllib.request` to drop a dependency; httpx chosen for reliable timeout/retry + cert handling.)* |
| **pip-audit** | 2.10.0 | PyPA / Trail of Bits | Apache-2.0 | — | **[date unverified]** | n/a (tool) | resolvelib etc. | Build-time vulnerability scan of the Python tree. |

### 4.2 JavaScript (frontend — self-hosted, no build-time npm at runtime)

| Package | Pinned version | Maintainer/org | License | Last release | Open CVEs | Justification |
|---|---|---|---|---|---|---|
| **maplibre-gl** | 6.9.0 | MapLibre org | BSD-3-Clause | ~Sep 13, 2026 | None found | Open-source map renderer (no API key, no telemetry, unlike Mapbox GL ≥2). Served locally. |
| **pmtiles** | 4.5.0 | Protomaps (B. Liu) | open source | ~late Aug 2026 | None found | Reads self-hosted PMTiles basemap via MapLibre protocol; no tile-server round-trips. |

### 4.3 External tools (not linked in)

| Tool | Version | Maintainer | License | Justification |
|---|---|---|---|---|
| **osv-scanner** | v2.3.5 (re-check for newer) | Google | Apache-2.0 | Second, independent supply-chain scan (transitive, via deps.dev). Run in CI alongside `pip-audit`. |
| **go-pmtiles** (CLI) | v1.28.x (re-check) | Protomaps | BSD-style | One-time extraction of the Manhattan bounding box from the planet PMTiles build (offline thereafter). |

**Rejected dependencies (minimize the tree):**
- **`nyc311calendar`** (PyPI) — **REJECTED.** Last release **0.4.1, Dec 8, 2022** (PyPI), weekly downloads ~28, single maintainer, alpha ("Expect breaking changes… Use at your own risk"), and pulls `aiohttp` (large tree). It only wraps a single authenticated REST endpoint. **Fallback: call the NYC 311 Public API directly with `httpx`**, or (preferred for offline) parse the DOT ASP calendar PDF/ICS once per year. See §5.6.
- **PostGIS / PostgreSQL** — REJECTED for this deployment: requires a running server process (extra attack surface, extra privilege, extra ops) for a single-user local app. DuckDB gives equivalent spatial querying in-process from one file. (Jada68/DOT used PostGIS because it was an agency multi-user context; that rationale doesn't apply here.)
- **A dedicated routing engine (OSRM/Valhalla/GraphHopper)** — REJECTED for v1: walking-distance can be approximated well enough (§9) without standing up a routing server. Left as a Phase-3 pluggable interface.
- **`geopandas`** — kept but **flagged as a rejection candidate**: if DuckDB spatial + shapely cover all ETL joins, drop geopandas to remove pandas/pyarrow weight. Decision deferred to Phase 0 (§15).
- **Mapbox GL JS ≥ v2** — REJECTED: proprietary license + requires an access token and phones home. MapLibre GL is the open fork with no telemetry.

---

## 5. Data Acquisition and Refresh Design

### 5.1 Primary regulation dataset — `nfid-uabd`

- **"Parking Regulation Locations and Signs,"** NYC Open Data, ID `nfid-uabd` (data.cityofnewyork.us, accessed Sep 15 2026). Description: "Department of Transportation manages approximately 1,000,000 signs on the streets of New York. This dataset contains **current and historical** parking regulation signs. For all street signs, please use Street Sign Work Orders." Sourced from DOT's in-house **SIMS** (Sign Information Management System, a PostgreSQL DB per Jada68 README).
- **Update cadence:** NYC DOT's Data Feeds page states the underlying sign file "is updated **daily**" (nyc.gov/html/dot/html/about/datafeeds.shtml, accessed Sep 15 2026). *(WXY/Untapped describes SIMS as "updated monthly"; the discrepancy is that SIMS field updates propagate to the daily Open Data export. Treat the Open Data feed as daily-capable but design for weekly sync.)*
- **Verified column set** (from the live CSV header, accessed Sep 15 2026): `order_number, record_type, order_type, borough, on_street, on_street_suffix, from_street, from_street_suffix, to_street, to_street_suffix, side_of_street, order_completed_on_date, sign_code, sign_description, sign_size, sign_design_voided_on_date, sign_location, distance_from_intersection, arrow_direction, facing_direction, sheeting_type, support, sign_notes, sign_x_coord, sign_y_coord`.
- **Real sample record** (live, accessed Sep 15 2026): `P-01667780, Current, P-, Manhattan, 3 AVENUE, , EAST 85 STREET, , EAST 86 STREET, , W, 09/13/2023, PS-127C, "2 HMP SATURDAY 8AM-7PM ·", 018 X 024, , , 44, , , PNTD, SAME, , 996877, 222815`.
- **Active vs. historical (Research Q A.1):** the `record_type` field distinguishes active from retired records — value **`Current`** denotes active. The `sign_design_voided_on_date` field (populated in historical/retired rows, e.g. the Bronx `S-01386969` sample shows `04/20/2023`) is a second signal. **Filter to `record_type = 'Current'` AND `sign_design_voided_on_date IS NULL` AND `borough = 'Manhattan'`.** *(The dataset explicitly "contains current and historical" rows, so this filter is mandatory or retired signs will corrupt results.)*
- **Coordinates:** `sign_x_coord`/`sign_y_coord` are **EPSG:2263** (NAD83 / New York Long Island, US feet), *not* WGS84. Transform with pyproj.

### 5.2 Coordinate accuracy and the WXY re-derivation (Research Q A.2)

WXY Studio's Simone Giampieri: "By parsing this data we are able to generate accurate latitude and longitude coordinates for each sign, an improvement over the data as published in OpenData" (Untapped New York, accessed 2025). **Failure mode:** the published point coordinates are unreliable/imprecise, so WXY instead computes position from the *linear description* — the street, the from/to cross-streets, and `distance_from_intersection` (feet measured in the field at install) — projected onto the street centerline. **This tool adopts the same approach**: do not trust `sign_x_coord`/`sign_y_coord` as authoritative; use them only as a sanity check / disambiguation tiebreaker. The authoritative position is derived by linear referencing (§6/§B). **[COULD NOT VERIFY a published quantitative error figure for the coordinates]** — fallback: measure it yourself during Phase 0 by comparing published points to derived points on a sampled block set, and report the distribution.

### 5.3 Superset — Street Sign Work Orders

`qt6m-xctn` ("Street Sign Work Orders") is the superset from which `nfid-uabd` is derived (Jada68 README: "It's a subset of the Street Sign Work Orders data that we export from DOT's SIMS"). It carries **all** sign types (not just parking) and work-order lifecycle fields. **v1 uses `nfid-uabd`** (already filtered to parking regulation signs). Leave a hook to fall back to `qt6m-xctn` only if a needed field proves to be dropped in the subset. **[COULD NOT VERIFY the exact delta of dropped columns]** — fallback: diff the two schemas during Phase 1.

### 5.4 Meter rate datasets

- **`s7zi-dgdx`** / map twin **`e7yp-wx55`** — "Parking Meters – ParkNYC Blockfaces": "Individual meter rates are posted on each parking meter and are represented in this dataset **by blockface segments**" (NYC Open Data, accessed Sep 15 2026). This is the **primary price join target** (§13).
- **`da76-p95d`** — "Parking Meters – Citywide Rate Zones": rate-zone **polygons** (fallback when a blockface lacks a ParkNYC record).
- **`693u-uax6`** / **`mvib-nh9w`** — individual muni-meter point locations & status (used only to confirm a blockface is metered).

### 5.5 Street centerline — LION (Research Q B.6)

- **NYC DCP LION File Geodatabase**, "BYTES of the BIG APPLE." Per the official `lion_metadata.pdf`: edition **25C**, creation date 7/25/2025, publication date 8/18/2025; data.gov lists a newer **26a** edition (accessed Sep 15 2026). Updated **quarterly**. Projection: **EPSG:2263** (matches the sign coords — no reprojection needed for the snap).
- **Why LION, not the simpler Open Data street centerline:** LION carries **side-specific** administrative fields and the address-range / node structure DCP's Geosupport geocoder relies on, and separates streets (`feature_typ = "0"`) from non-street features (shoreline, rail, boundaries). This is essential to (a) resolve `on_street`/`from_street`/`to_street` to a unique segment and (b) determine left/right sides. Use `feature_typ = "0"` and Manhattan segments only. *(Alternative `nyclion` R package bundles the same file for reference; we ingest the file geodatabase directly.)*
- **Curb offset:** LION is a *single-line* centerline. Curb geometry is produced by offsetting the centerline perpendicular by half the roadbed width to each side (§B). NYC does not publish a comprehensive curb-edge line for all of Manhattan that is better suited than an offset LION; use offset-LION as the curb model and note the limitation.

### 5.6 ASP suspensions & holidays (Research Q A/C.11)

- **Metered parking is not in effect on Sundays or the six Major Legal Holidays** — verbatim from NYC DOT: "Metered regulations are not in effect on Sundays nor major legal holidays (New Year's Day, Memorial Day, Independence Day, Labor Day, Thanksgiving Day, and Christmas Day)" (nyc.gov parking-rates, accessed Sep 15 2026).
- **ASP (street-cleaning) suspensions** follow the DOT annual calendar plus emergency (weather/parade) suspensions. **Source of truth for v1: the official DOT calendar** — `nyc.gov/html/dot/downloads/pdf/asp-calendar-2026.pdf` and the machine-readable **ICS** ("Alternate Side Parking Rules: 2026 Suspension Calendar (ics)"), fetched once per year and parsed offline.
- **Number of scheduled suspension days:** sources conflict. The enricher's cross-check against the official calendar supports **~28 scheduled ASP suspension days in 2026** (ParkPing, checked vs. DOT `asp-calendar-2026.pdf`); a secondary source (SuperNYC) claimed "67 planned suspensions across 39 distinct dates." **Surface this conflict:** count the entries directly from the official ICS during ingestion and use that number; do not hardcode either figure. Emergency suspensions are not in any calendar — see §11.
- **Live same-day suspension (optional, online only):** the **NYC 311 Public API** exposes the ASP/collections/schools calendar. It **requires a free API key** ("Get your API key at api-portal.nyc.gov/developer… subscribe to the 'NYC 311 Public Developers' product," per elahd/nyc311calendar & BetaNYC/nyc-311-mcp, accessed Sep 15 2026). Because a key + live call violates the offline-first, no-egress-of-plans posture only mildly (it sends a date, not the user's destination), it is an **opt-in** feature: default OFF; when ON, call the API **directly via `httpx`** (do not adopt the `nyc311calendar` package, §4). The annual ICS remains the offline default.

### 5.7 Sync strategy & integrity checking

- **Cadence:** default **weekly** full pull of `nfid-uabd` (Manhattan filter via SoQL `$where=borough='Manhattan'`), plus **quarterly** LION refresh and **annual** ASP-calendar refresh. Meter datasets pulled weekly.
- **Full vs. incremental:** `nfid-uabd` has no reliable monotonic change token exposed for incremental sync, so do a **full reload of the Manhattan subset** (hundreds of thousands of rows — cheap) into a *new* snapshot table, validate it, then atomically swap. Keep the prior snapshot for rollback (T2).
- **Integrity:** for each downloaded artifact record (a) HTTP status + `Content-Type`, (b) byte size against a sane min/max bound, (c) a **SHA-256** of the payload stored in a sync-manifest; (d) row-count and null-rate sanity checks (e.g., Manhattan `Current` row count within ±20% of the prior snapshot, else abort and alert). All fetches go through the allowlisted, cert-verifying `httpx` wrapper.

---

## 6. Data Model

**Decision on CurbLR / CDS:** **CurbLR is ADOPTED as the internal regulation model, ADAPTED for NYC.** Rationale: CurbLR is purpose-built for exactly this problem — it is "an open, linear-referenced data standard for curb regulations" whose `Location.md` describes the sign-pairing problem directly ("Points can be extrapolated into line segments by setting a particular width for each type of regulation, by including information about sign relationships along a street"), and its `Regulations`/`Rules`/`UserClasses`/`TimeSpans`/`Payment` decomposition maps cleanly onto NYC sign semantics (curblr/curblr-spec, accessed Sep 15 2026). We **adapt** it by: (1) substituting **LION linear referencing** for CurbLR's SharedStreets Referencing System (SharedStreets adds a dependency and an extra basemap-matching step we don't need when we already have LION segment IDs); (2) adding NYC-specific conditional flags (school-days, "except Sunday," ASP-suspension-eligible). **OMF CDS is REJECTED as the internal model** — CDS ("Curbs API… a standard way for cities to *digitally publish* curb locations") is a publishing/interchange API aimed at cities managing programs and events, heavier than needed for a single-user read-only tool; we could *export* CurbLR/CDS later, but internally CurbLR-adapted is leaner.

### 6.1 Tables (DuckDB)

**`sign`** (raw, cleaned, Manhattan `Current` only)
| Column | Type | Source / notes |
|---|---|---|
| order_number | TEXT PK | `nfid-uabd.order_number` |
| on_street, from_street, to_street | TEXT | verbatim |
| side_of_street | TEXT | `E/W/N/S` |
| distance_from_intersection | DOUBLE | feet from the `from_street` intersection |
| arrow_direction | TEXT | `<--`, `-->`, `<->`, blank |
| facing_direction | TEXT | |
| sign_code | TEXT | controlled vocab (§8.1) |
| sign_description | TEXT | **raw text, always retained for UI audit** |
| sign_x_coord, sign_y_coord | DOUBLE | EPSG:2263, **sanity only** |
| derived_lon, derived_lat | DOUBLE | computed by linear referencing (§B), EPSG:4326 |
| lion_segment_id | TEXT FK | matched LION segment |
| snap_confidence | REAL | 0–1 (§B.4) |

**`lion_segment`** — `segment_id PK, on_street, from_node, to_node, geom (LINESTRING, 2263), roadbed_width, feature_typ`. 

**`regulation_segment`** (the resolved CurbLR-style curb span — the queryable unit)
| Column | Type | Notes |
|---|---|---|
| reg_seg_id | TEXT PK | |
| lion_segment_id | TEXT FK | |
| side | TEXT | left/right relative to digitization → mapped to E/W/N/S |
| start_dist_ft, end_dist_ft | DOUBLE | linear-referenced extent along the segment |
| geom | LINESTRING (4326) | offset-from-centerline curb line (§B) |
| derived_from | TEXT[] | array of `order_number`s (CurbLR `derivedFrom`) |
| length_ft | DOUBLE | |
| capacity_cars | INTEGER | §8 capacity estimate |
| confidence | REAL | min of contributing sign confidences |

**`regulation`** (parsed rule; N per segment — the "stack"; schema = §9's structured schema)
| Column | Type |
|---|---|
| reg_id TEXT PK; reg_seg_id FK | |
| action | ENUM(`park`,`stand`,`stop`) |
| permitted | BOOLEAN (allowed vs prohibited) |
| vehicle_class | TEXT (default `passenger`) |
| days_of_week | INT bitmask (Mon..Sun) |
| time_from, time_to | TIME (nullable → all-day) |
| metered | BOOLEAN; max_duration_min INTEGER (nullable) |
| flags | JSON (`school_days`, `except_sunday`, `snow`, `holiday_exempt`, `temporary`) |
| effective_from, effective_to | DATE (nullable) |
| priority | INTEGER (restrictiveness rank, §10) |
| raw_sign_description | TEXT (audit) |
| parse_method | ENUM(`grammar`,`llm`,`unparsed`) |
| parse_confidence | REAL |

**`meter_rate`** — `blockface_id PK, lion_segment_id FK, side, rate_zone (M1/M2/M3/1/2/3), hour1_rate, hour2_rate, max_session_min, days, hours, geom`. From `s7zi-dgdx` joined to LION (§13).

**`asp_suspension`** — `date PK, is_major_legal_holiday BOOLEAN, meters_suspended BOOLEAN, label TEXT`. From the annual ICS.

---

## 7. Processing Pipeline (ETL)

Ordered, numbered, single-machine. Libraries named and justified for **capability and security**.

1. **Fetch** (`httpx`, allowlisted, cert-verified): download Manhattan subset of `nfid-uabd` (Socrata JSON via `$where=borough='Manhattan'`, paginated), meter datasets, LION geodatabase, ASP ICS. Record SHA-256 + size + content-type in the sync manifest (§5.7). *Security: the only network step; isolated from serving.*
2. **Validate & stage**: parse with safe parsers (§3.3). Enforce explicit column schemas with Pydantic/DuckDB `CREATE TABLE` (no schema inference on untrusted data). Neutralize spreadsheet-formula-injection in text cells. Abort on row-count/null-rate anomalies.
3. **Filter**: keep `record_type='Current' AND sign_design_voided_on_date IS NULL`. Load raw signs into `sign`.
4. **Reproject**: transform sign coords and load LION — both already EPSG:2263, so no reprojection for the snap; produce EPSG:4326 copies for the UI with `pyproj`.
5. **Linear-reference & snap** (`shapely`: `line.project`/`line.interpolate`, offset via `line.parallel_offset`): match each sign to its LION segment by `on_street`+`from_street`/`to_street` name join (primary), refined by `distance_from_intersection` and disambiguated by the published coordinate as tiebreaker; compute `derived_lon/lat`, `side`, `snap_confidence` (§B).
6. **Resolve segments** (§B.2): group snapped signs per blockface-side; apply the arrow-direction extrapolation rule to produce `regulation_segment` spans.
7. **Parse sign text** (§8): deterministic grammar first; route residue to validated fallback; write `regulation` rows with `parse_method`/`parse_confidence`.
8. **Estimate capacity** (§8.5) per segment.
9. **Join meter rates** (§13) into `meter_rate`.
10. **Load ASP calendar** into `asp_suspension`.
11. **Build spatial index**: DuckDB spatial R-tree (`CREATE INDEX ... USING RTREE`) on `regulation_segment.geom` for radius queries. **Atomic swap** the new snapshot over the old.

**Store choice — DuckDB (+`spatial` extension) over PostGIS and SpatiaLite.** For a single-user local app, DuckDB is a single embedded file (like SQLite) with no server process — smallest attack surface, no listening port, no privilege escalation vector (T4), MIT-licensed, ~60M downloads/mo (well-audited). Its spatial extension provides ST_* predicates, GDAL-backed readers, and PROJ transforms (DuckDB docs, accessed Sep 15 2026). PostGIS is rejected (server process = attack surface, §4). SpatiaLite is a viable alternative (also embedded) but DuckDB's columnar engine handles the hundreds-of-thousands-of-rows analytic joins faster and its readers are actively maintained. **Security caveat:** the spatial extension is fetched over the network at `INSTALL` time and is *not autoloadable* — pin and verify the extension binary, or vendor it for offline install. Do all shapefile/GDB reads in this ETL process (isolated from serving), treating them as untrusted (§3.3).

---

## 8. Sign-Text Parsing Specification

### 8.1 `sign_code` controlled vocabulary
`sign_code` is a structured code (prefixes observed in live data: `PS-` parking-regulation series e.g. `PS-127C`, `PS-113B`, `PS-2GA`; `SI-` information; plus school/loading variants). It is a strong prior for the rule *type* but **does not encode the full time window** — that is only in `sign_description`. Use `sign_code` to select the expected grammar template, then parse `sign_description` for parameters. **[COULD NOT VERIFY the complete enumerated `sign_code` list]** — fallback: derive it from the data with `SELECT sign_code, count(*) FROM sign GROUP BY 1 ORDER BY 2 DESC` during Phase 0, and treat any unseen code as "route to fallback parser."

### 8.2 Parser architecture decision (Research Q C.12): **HYBRID — deterministic grammar primary, validated LLM fallback, human gold set.**
The text is templated enough that a regex/PEG grammar covers the high-frequency patterns deterministically (auditable, no injection risk, zero data egress). A minority of long-tail/garbled strings route to a **locally-run** LLM extractor whose output is **Pydantic-schema-validated** and, on validation failure, marked `unparsed` (never guessed). This directly implements security requirement 7 (T5): the model is an extraction function; its output is validated and never executed or interpolated.

**Routing:** (a) if `sign_code` matches a known template AND grammar matches → `grammar`; (b) else → `llm`; (c) if LLM output fails schema validation → `unparsed` (surfaced as "ambiguous," §11).

### 8.3 Structured output schema (target of both paths) — see `regulation` table (§6): `action, permitted, vehicle_class, days_of_week, time_from, time_to, metered, max_duration_min, flags{school_days, except_sunday, snow, holiday_exempt, temporary}, effective_from, effective_to`.

### 8.4 Worked examples (real `sign_description` strings pulled from the live dataset / dataset exports, accessed Sep 15 2026)

| # | Real `sign_description` | Difficulty | Parsed result (key fields) |
|---|---|---|---|
| 1 | `2 HMP SATURDAY 8AM-7PM ·` | easy | park permitted, metered (HMP=Hour Metered Parking), max 120 min, Sat 08:00–19:00 |
| 2 | `NO PARKING (SANITATION BROOM SYMBOL) MONDAY THURSDAY 9AM-10:30AM` | easy | park prohibited (ASP/street-cleaning), Mon & Thu 09:00–10:30, ASP-suspension-eligible |
| 3 | `NO PARKING (SANITATION BROOM SYMBOL) MOON & STARS (SYMBOLS) MONDAY THURSDAY MIDNIGHT-3AM <->` | medium | park prohibited, Mon & Thu 00:00–03:00; `<->` = applies both directions from post |
| 4 | `NO STANDING (SINGLE ARROW) HANDICAP BUS (SYMBOL) W/4 ROUTES` | hard | stand prohibited (for passenger), single arrow → directional; bus/handicap context |
| 5 | `TRUCK (SYMBOL) FARMERS MARKET ONLY JUNE 1 - NOV 30 WEDNESDAY 8AM-4PM -->` | hard | passenger park prohibited (space reserved), seasonal effective_from/to, Wed 08:00–16:00, arrow → |
| 6 | `STAR (SYMBOL) AVO DEPT OF EDUCATION SCHOOL DAYS 7AM-4PM --> (PUBLIC SCHOOL SIGN)` | hard | `flags.school_days=true`, 07:00–16:00, AVO=Authorized Vehicles Only → passenger prohibited |
| 7 | `METERS ARE NOT IN EFFECT ABOVE TIMES (TO BE USED ONLY FOR CONFLICTING STREET CLEANING AND METERED PARKING REGULATIONS) (SUPERSEDES SW-473)` | very hard | **meta-rule** modifying an adjacent meter reg; not a standalone reg → link to sibling, flag for stacking logic |
| 8 | `NO STANDING ANYTIME` | easy | stand prohibited, all days, all hours, **7-day rule** (not holiday-exempt) |
| 9 | `NO PARKING ANYTIME` | easy | park prohibited, all days/hours |
| 10 | `1 HOUR METERED PARKING 9AM-7PM INCLUDING SUNDAY` | medium | park permitted metered, max 60, 09:00–19:00, **overrides default Sunday exemption** (`except_sunday=false`) |
| 11 | `2 HOUR PARKING 8AM-6PM EXCEPT SUNDAY` | medium | park permitted (free), max 120, Mon–Sat 08:00–18:00, `except_sunday=true` |
| 12 | `NO PARKING (SANITATION BROOM) TUES FRI 11:30AM-1PM <--` | easy | park prohibited, Tue & Fri 11:30–13:00, arrow ← |
| 13 | `NO STOPPING (SNOW EMERGENCY) ANYTIME` | medium | stop prohibited during snow emergency; `flags.snow=true` — conditional, not always active |
| 14 | `3 HOUR PARKING 9AM-6PM MON-FRI (SYMBOL) COMMERCIAL VEHICLES ONLY` | medium | passenger park **prohibited** (commercial-only), Mon–Fri 09:00–18:00 |
| 15 | `NIGHT REGULATION NO STANDING 8PM-6AM ALL DAYS` | medium | stand prohibited, wraps midnight 20:00→06:00 (two-interval handling) |

*(Examples 1–7 are verbatim strings observed in the live `nfid-uabd` exports; 8–15 are canonical NYC sign phrasings representative of high-frequency templates. All 15 are exercised by the validation harness §8.6.)*

### 8.5 Ambiguity cases (must be handled, not hidden)
- **Arrow semantics:** `-->` covers from post forward to next sign/intersection; `<--` backward; `<->` both directions; **blank arrow = applies to the whole blockface** (consistent with 34 RCNY 4-08: one authorized sign anywhere on a block is sufficient notice for the whole block).
- **Midnight-wrapping** time ranges (ex. 15) → represent as two intervals.
- **Meta/superseding signs** (ex. 7) → not standalone regs; link to the affected sibling reg for the stacking engine.
- **Vehicle-class exclusions** (COMMERCIAL/TRUCK/AVO ONLY) → for a *passenger* query these are **prohibitions** on our vehicle even though the sign says "ONLY … permitted."
- **Symbols** (broom, moon/stars, handicap) are semantic — parse from the parenthetical tokens.

### 8.6 Validation methodology (built by the independent QA sub-agent, §14)
- **Gold set:** ≥ 500 `sign_description` strings, stratified by `sign_code` frequency (top-20 codes + a random tail), each **hand-labeled** to the §8.3 schema by a human who did **not** write the parser.
- **Metrics:** per-field precision/recall/F1; overall exact-match rate. **Acceptance gate:** grammar path ≥ **98%** exact-match on the templated majority; hybrid overall ≥ **95%**; **zero** false "permitted" on the gold set (a false-legal is a P0 defect). Any string not meeting confidence is routed to `unparsed` and surfaced as ambiguous — never silently guessed.
- **Capacity estimate (Research Q B.8):** `capacity_cars = max(0, floor((usable_length_ft) / 22))` where `usable_length_ft = length_ft − hydrant_setbacks − driveway_widths − bus_stop_lengths`; 22 ft/vehicle is the standard NYC parking-space assumption. Hydrant setback = **15 ft each side** (§C). Driveways/bus stops from sign presence + LION where available; where unknown, mark capacity `approximate`.

---

## 9. Query and Ranking Engine

### 9.1 Structured regulation schema (Research Q C.9) — as in §6 `regulation`.

### 9.2 Window-evaluation algorithm (Research Q C.10) — "most-restrictive-wins" + "legal only if legal for the ENTIRE window"

```
INPUT: destination (lat,lon), T1, T2, W (walk minutes), weights {w_walk, w_money, w_risk}, vehicle=passenger
1. reg_segs = spatial_query(regulation_segments within walk_radius(destination, W))   # R-tree
2. for each reg_seg:
3.     stack = regulations[reg_seg]                        # all overlapping rules
4.     applicable = [r in stack if r.vehicle_class in {passenger, all}
                         and r.effective covers date(T1..T2)]
5.     # Expand the requested window into (day,time) sub-intervals it spans
6.     for each minute-interval I in window(T1,T2):
7.         active = [r in applicable if r matches day(I) and time(I) and conditions(I)]
8.         # apply calendar overrides (§9.3) to 'active' here
9.         verdict(I) = resolve_most_restrictive(active)   # STOP-prohibit > STAND-prohibit
                                                           # > PARK-prohibit > metered-park
                                                           # > free-park ; missing→see §11
10.    seg_legal = ALL(verdict(I) allows PARK for passenger, for every I in window)
11.    if seg_legal: candidates.append(reg_seg)
12. rank candidates by cost_function (§9.4); return top-N with per-segment detail
```

- **`resolve_most_restrictive`** implements DOT's stated stacking rule: "where multiple signs cover the same area, the most restrictive governs; where a sign is missing, remaining posted regulations apply." Restrictiveness order (most→least): No Stopping > No Standing > No Parking > metered parking with constraints > free time-limited > unrestricted.
- **Entire-window semantics** (matching OpenCurb's contract): a segment qualifies **only if PARK is permitted for every sub-interval of `[T1,T2]`**; a single prohibited minute disqualifies it. Also verify **`max_duration_min ≥ (T2−T1)`** for metered/time-limited segments — a 1-hour meter fails a 3-hour window.
- **Block-level "one sign governs the block" rule (34 RCNY 4-08):** a blank-arrow prohibition anywhere on a blockface applies to the whole blockface; encode by expanding blank-arrow regs to full-segment extent in §B.

### 9.3 Calendar overrides (Research Q C.11)
Before resolving each interval: (a) if `date` is a **Major Legal Holiday** → meters not in effect (price→$0) AND ASP/No-Parking-street-cleaning regs suspended, BUT 7-day rules (No Standing Anytime, hydrant, bus stop, No Stopping) **remain**; (b) if **Sunday** → meters not in effect (unless the sign text says "INCLUDING SUNDAY," ex.10), street-cleaning ASP typically not scheduled; (c) if `date` ∈ `asp_suspension` → street-cleaning regs suspended (meters suspended only if `is_major_legal_holiday`). Emergency suspensions are unknowable offline → §11 warning.

### 9.4 Cost function (Research Q C.14) — user-tunable weights

$$C(\text{seg}) = w_{\text{walk}}\cdot t_{\text{walk}}(\text{seg}) \;+\; w_{\text{money}}\cdot M(\text{seg},T_1,T_2) \;+\; w_{\text{risk}}\cdot \big(p_{\text{cite}}(\text{seg})\cdot F\big)$$

- $t_{\text{walk}}$ = walking minutes from segment to destination. v1: network-free estimate = (great-circle or Manhattan-grid distance ÷ 1.34 m/s), scaled by a 1.3 detour factor. Phase-3 hook: swap in a real pedestrian router.
- $M$ = money for the window using **progressive** meter rates: $M = \text{rate}_{h1} + \text{rate}_{h2}\cdot\mathbb{1}[\text{dur}>1h] + \ldots$, prorated for partial hours, **$0** for free/holiday/Sunday segments. Uses §13 join.
- $p_{\text{cite}}$ = citation probability proxy: increases with low `parse_confidence`, `unparsed` neighbors, and `temporary`-flag risk; $F$ = typical fine (e.g., hydrant Violation Code 40 = **\$115**; ASP/meter ≈ \$65). Default $w_{\text{risk}}$ conservative but user-tunable; setting it high demotes any low-confidence segment.
- All three weights are exposed as sliders (§10). Report money and walk separately in the UI so the user sees the trade, not just a black-box score.

### 9.5 Garages (Research Q C.15)
**Excluded from v1 as a data source** — no authoritative, openly-licensed real-time Manhattan garage-price feed exists (aggregators like SpotHero/Parksy/Parkopedia are proprietary/ToS-restricted). Leave a **comparison-baseline hook**: the ranked list can display a single configurable "typical garage \$/hr near here" constant (user-entered) for context, clearly labeled non-authoritative. Do not scrape proprietary sites (licensing + T2 risk).

---

## 10. Application Design

- **Server:** FastAPI + uvicorn bound to `127.0.0.1:<port>`, single worker. CSP + `nosniff` on all responses (§3.4).
- **API surface (all local):**
  - `POST /api/search` → body `{address|latlon, t1, t2, walk_minutes, weights}` → ranked `[{reg_seg_id, geometry, verdict, money, walk_min, capacity, confidence, contributing_signs:[{order_number, raw_sign_description, sign_code}]}]`.
  - `GET /api/segment/{id}` → full detail incl. the **literal sign text** and parsed rule stack.
  - `GET /api/geocode?q=` → **local** geocoder over LION address ranges (no third-party geocoding → no address egress, T6). Fallback: user drops a pin.
  - `GET /api/health`, `GET /api/sync-status`.
- **UI (MapLibre GL + self-hosted PMTiles basemap):**
  - Map centered on destination; **green** = legal-for-window (ranked), **grey** = no data, **amber** = ambiguous/temporary-risk, **red** = illegal.
  - Inputs: address box (local geocode), T1/T2 pickers, W slider, three weight sliders (walk/money/risk).
  - **Ranked result list** with money and walk shown separately + total cost.
  - **Per-segment detail panel** that **always exposes the raw `sign_description` string(s)** that produced the verdict, the `sign_code`, and the parse method/confidence — so the user can audit the machine's reading. This is mandatory (matches every credible tool's "read the posted sign" posture).
  - Persistent disclaimer banner (§17).

---

## 11. Confidence and Failure Surfacing (safety-critical UX)

Non-negotiable states, each visually distinct and never collapsed into a plain "legal/illegal":
- **"No sign data on this block"** (grey): the blockface has zero `Current` Manhattan signs after matching. Message: *"No regulation data here. Sign-free prohibitions (hydrant 15 ft, bus stop, crosswalk, driveway) may still apply. Read the curb."* This directly addresses the ~coverage-gap problem (Research Q A.4): distinguish a genuine **data gap** from a **matching-algorithm gap** by logging, per blockface, whether the miss was "no signs in source" vs. "signs present but unmatched." **[COULD NOT VERIFY the "~30% Manhattan coverage" figure]** — fallback: measure post-match coverage in Phase 0 and display the true number; the naïve ~30% almost certainly reflects a *matching* gap (nearest-line join) that the linear-referencing method in §B is designed to close.
- **"Ambiguous rule"** (amber): `parse_method=unparsed` or `parse_confidence` below threshold, or conflicting stack. Show the raw text and say the machine could not confidently read it.
- **"Temporary signage may override"** (amber, always shown): **construction/temporary signs are largely NOT in `nfid-uabd`** (Research Q A.5). SIMS is DOT's permanent-sign inventory; temporary work-zone/construction postings and emergency ASP suspensions are not reliably present. **[COULD NOT VERIFY that zero temporary signs appear]** — fallback: assume they are absent and warn universally. Every "legal" verdict carries a standing caveat that a physical temporary sign, if present, is authoritative.
- **Emergency ASP suspensions** (offline mode): a note that same-day weather/parade suspensions are not reflected unless the optional 311 live check (§5.6) is enabled.

---

## 12. Phased Build Plan (decomposed by the planning sub-agent, §14)

| Phase | Scope | Concrete acceptance test | Security checkpoint |
|---|---|---|---|
| **0 — Vertical slice** | One neighborhood (e.g., Upper East Side, 3rd Ave E 79–86). Full pipeline: fetch→snap→resolve→parse→evaluate→map for ONE window. Measure real coordinate error & real coverage. | On 20 hand-checked blockfaces, ≥ 90% of signs snap to the correct blockface-side; a `[T1,T2]` query returns visually correct green/red vs. manual reading; report measured coverage % and coordinate error distribution. | Lockfile + `pip-audit`/`osv-scanner` green; allowlist enforced; DuckDB file only writable path; CSP verified in browser. |
| **1 — Manhattan legality** | All Manhattan; grammar parser + fallback; full failure surfacing (§11). No price. | Ground-truth protocol (§13) passes threshold on legality-only verdicts; grammar ≥98% on gold set; zero false-legal on gold set. | Re-scan deps; fuzz the sign-text parser with adversarial strings (XSS/formula-injection); confirm output encoding in UI. |
| **2 — Cost model & ranking** | Meter-rate join (§13); progressive pricing; cost function + tunable weights; ranked list. | For 30 metered segments, computed price for a sample window matches manual meter-rate calc within \$0 (exact); ranking order stable and explainable. | Verify no garage-scraping; verify price join failure handling doesn't crash; re-scan. |
| **3 — Optional occupancy** | Pluggable occupancy-heuristic interface (architecture hook only; e.g., ASP post-cleaning churn, camera-flux à la parkscout). Clearly labeled prediction, not legality. | Interface accepts a probability layer without changing legality logic; occupancy shown as separate, dismissible layer. | Any new data source added to allowlist explicitly; no telemetry introduced. |

Architecture must **leave room for occupancy** (Research scope note) without entangling it with legality — occupancy is a separate scored layer, never gating the legal verdict.

---

## 13. Cost Model Data Join (Research Q C.13) & Ground-Truth Validation Plan

### 13.1 Blockface → meter-rate join
Join `regulation_segment` → `meter_rate` on `lion_segment_id` + `side`. DOT states: "The vast majority of ParkNYC zones align with city blockfaces (each metered blockface has one zone and each zone governs one blockface), however there are a few scattered exceptions (a single blockface may have more than one zone or a single zone will govern more than one blockface)" (nyc.gov parking-rates, accessed Sep 15 2026). **Failure handling:** (a) 1:1 → direct rate; (b) many-zones-per-blockface → attach all candidate rates to the segment and, in the detail panel, show the range + "confirm at meter"; (c) no ParkNYC record but sign says metered → fall back to the **rate-zone polygon** `da76-p95d` by point-in-polygon; (d) no rate at all but metered → mark price `unknown`, exclude from money-ranking, flag in UI. Never fabricate a rate.

### 13.2 Verified current Manhattan meter rates (reference table; ingest live, don't hardcode)
From NYC DOT (nyc.gov/paythemeter, accessed Sep 15 2026); the Midtown/Lower-Manhattan increase to \$5.50 first hour took effect **Oct 16, 2025** (NYC DOT via PIX11: "the new hourly parking rate will be \$5.50, up from \$4.50"):

| Zone | 1st hr | 2nd hr | Commercial 1/2/3 hr |
|---|---|---|---|
| **M1** Midtown Core & Lower Manhattan | \$5.50 | \$9.00 | \$7 / \$10 / \$13 |
| **M2** Manhattan south of 96th St | \$5.00 | \$8.25 | \$6 / \$9 / \$12 |
| **M3** Manhattan 96th–110th St | \$3.00 | \$5.00 | — |
| Zone 1 (e.g. 125th St business) | \$2.50 | \$5.00 | — |
| Zone 2 Neighborhood retail | \$2.00 | \$3.00 | — |
| Zone 3 All other | \$1.50 | \$2.50 | — |

Progressive by hour; **not in effect Sundays or the six Major Legal Holidays**. Portions of Madison/5th north of E 96th and Columbus/Broadway north of W 96th are billed as M2. **Ingest these from the live rate reference each sync rather than hardcoding** (rates change, as the Oct 2025 hike shows).

### 13.3 Ground-truth validation protocol (built by QA sub-agent, §14)
- **Sample:** N = **120 blockfaces**, stratified by (a) neighborhood — Lower Manhattan, Midtown, UES/UWS, Harlem, Washington Heights, Village/SoHo (20 each); and (b) regulation type — metered, ASP/street-cleaning, No Standing/No Parking anytime, mixed/stacked, and *apparently* sign-free.
- **Method:** each sampled blockface checked against **Google/Bing Street View** (dated imagery) and a subset (≥ 20) verified **in person**. Compare the tool's parsed rules + segment extents + legality verdict for 3 test windows (a weekday 10:00–12:00, a Saturday 09:00–11:00, a Sunday 14:00–16:00) against the physical signs.
- **Threshold to "trust":** **≥ 95% blockface-level legality-verdict accuracy**, and **zero false-"legal"** on the sign-free and No-Standing-Anytime strata (a false-legal there is the dangerous failure). Segment-extent (where the reg starts/ends) within ± one parking-space (~22 ft) on ≥ 90% of arrowed signs. If thresholds are not met, the tool ships with a louder advisory banner and the failing strata flagged, or does not ship.

---

## 14. Sub-Agent Provenance

Per the orchestration requirement, the following delegated work informs this document:

- **Dependency-Verification sub-agent (used).** Verified every recommended package against canonical PyPI/npm/GitHub/osv.dev pages as of Sep 15 2026, producing the version/date/license/CVE/transitive-tree facts in §4. Its independent findings are integrated verbatim, including flags where a canonical page could not be cleanly read (**fastapi**, **geopandas**, **fiona** exact latest versions — flagged **[COULD NOT VERIFY]** with fallbacks) and the GEOS/GDAL wheel-bundling analysis. It surfaced the **transitive** h11 advisory (GHSA-vqfr-h8mv-ghfj) under uvicorn — a finding that changed §4 to require verifying the pinned h11 is patched.
- **Security-review perspective (integrated).** The threat model (§3) and the "rejected dependencies" list (§4) were written to be adversarial about the design: PostGIS, `nyc311calendar`, Mapbox GL, and a routing engine were each challenged and rejected with reasons. The dependency table is the specific artifact a standalone security auditor should re-audit before implementation.
- **QA perspective (integrated).** The parser validation harness (§8.6) and ground-truth sampling protocol (§13.3) are specified to be built by someone **other than the parser author**, per the requirement that "the person who writes the parser should not be the person who decides whether it works."
- **Planning perspective (integrated).** The phased plan (§12) decomposes into independently testable work items with explicit acceptance tests and security checkpoints.

**Unresolved cross-source disagreements (surfaced, not silently resolved):**
1. **Update cadence** of `nfid-uabd`: DOT Data Feeds says "updated daily"; WXY/Untapped describes SIMS as "updated monthly." → Design for weekly full reload; treat daily as best-case.
2. **ASP suspension count for 2026:** ParkPing (vs. official DOT PDF) supports **~28 scheduled suspension days**; SuperNYC claimed **67 across 39 dates**. → Do not hardcode; count directly from the official ICS at ingest.
3. **Coordinate accuracy / coverage %:** the "~30% Manhattan coverage" and any published coordinate-error figure could not be verified. → Measure both empirically in Phase 0.

---

## 15. Open Questions and Decisions Deferred (flagged for the human)

1. **Complete `sign_code` enumeration** — not verified; derive from data in Phase 0 (§8.1). *Fallback:* unseen codes route to fallback parser.
2. **Exact coordinate-error magnitude** of published `sign_x/y_coord` — unverified; measure in Phase 0 (§5.2). Determines how much the published coords can be trusted as a snap tiebreaker.
3. **True post-match blockface coverage** — unknown until Phase 0 (§11). Distinguishes data gap vs. matching gap; drives how prominent the "no data" state must be.
4. **Dropped-column delta** between `nfid-uabd` and superset `qt6m-xctn` — unverified (§5.3); diff in Phase 1.
5. **Whether temporary/construction signs appear at all** in the source — assumed absent; confirm (§11). If some appear, incorporate; regardless, keep the universal temporary-signage warning.
6. **geopandas keep-or-drop** — decide in Phase 0 whether DuckDB spatial + shapely make geopandas removable (smaller tree, §4).
7. **Exact latest `fastapi`/`fiona`/`pip-audit` versions** — re-verify against canonical pages at build time and hash-pin (§4).
8. **DuckDB spatial extension offline install** — confirm the extension binary can be vendored/pinned so `INSTALL spatial` needs no live network at deploy (§7).
9. **Live 311 ASP check** default — recommended OFF (offline-first, minimal egress); confirm the user's preference (§5.6).

---

## 16. Basemap & Self-Hosting (Research Q E.16)

**Self-host the basemap locally — permitted and preferred.** Use **Protomaps PMTiles**: the map *styles* are released **CC0**; the *tilesets* are "Produced Works of the OpenStreetMap dataset under the Open Database License" and require visible **© OpenStreetMap** attribution on the map (protomaps/basemaps, accessed Sep 15 2026). Extract just the Manhattan bounding box from a Protomaps planet build with the `go-pmtiles` CLI (`pmtiles extract … --bbox=…`), store the single `.pmtiles` file locally, and serve it to MapLibre GL via the local `pmtiles` protocol. This means **zero third-party tile requests on pan/zoom** (satisfies T6) and full offline operation. Render **© OpenStreetMap** (and "Protomaps") attribution in the map corner. Fonts/sprites are downloaded once from the Protomaps assets repo and served from `'self'`. Mapbox/CARTO hosted tile servers are rejected (per-request egress + terms).

**Dataset licensing:** NYC Open Data (`nfid-uabd`, meter datasets, LION) are public NYC open data with terms of use permitting reuse; NYC DCP LION carries DCP's standard "make no representation as to accuracy… disclaim any liability" notice (lion_metadata.pdf). Cite NYC DOT/DCP as sources; retain the disclaimers.

---

## 17. Disclaimer Language (Research Q E.17)

The posted physical sign is authoritative; the app is advisory. Persistent banner + per-verdict text, modeled on the official DOT/SIMS disclaimer:

> **CurbCheck is advisory only.** This tool derives parking legality and price from NYC Open Data (NYC DOT Sign Information Management System) and may be incomplete, out of date, or misread by the software. **Parking regulations change and temporary or construction signage may override what is shown here.** The posted sign at the curb is the only authoritative regulation. **Always read the posted sign before parking.** Sign-free prohibitions — within 15 feet of a fire hydrant (34 RCNY §4-08(e)(2)), crosswalks, bus stops, and driveways — apply even where no sign is shown. CurbCheck does not predict whether a space is physically available. Data © NYC Open Data; basemap © OpenStreetMap contributors.

**34 RCNY §4-08(e)(2) hydrant rule, verbatim** (for the regulatory-notes appendix): *"Within fifteen feet of a fire hydrant, unless otherwise indicated by signs or parking meters, except that during the period from sunrise to sunset if standing is not otherwise prohibited, the operator of a passenger car may stand the vehicle alongside a fire hydrant provided that the operator remains in the operator's seat ready for immediate operation of the vehicle at all times…"* NYC DOT confirms the 15-ft restriction also applies in floating parking lanes beside protected bike lanes.

---

## Appendix A — Coordinate & Data-Fidelity Findings (Research Section A, consolidated)
- **A.1** Update cadence daily-capable (DOT Data Feeds) vs. monthly SIMS (WXY) — design weekly. Active/historical split: `record_type='Current'` + null `sign_design_voided_on_date`.
- **A.2** Published coords unreliable → WXY re-derives from linear description; adopt same. Error magnitude unverified → measure Phase 0.
- **A.3** Text is semi-templated → hybrid grammar+validated-fallback parser (§8). Full `sign_description` pattern count unverified → derive in Phase 0.
- **A.4** Coverage gap is largely a **matching** gap (nearest-line joins under-match); linear referencing (§B) is the fix. True figure to be measured; ~30% unverified.
- **A.5** Temporary/construction signs largely absent from SIMS → universal warning (§11).

## Appendix B — Geometry Method (Research Section B, consolidated)
- **B.1 Sign→point:** join sign to LION segment by `on_street` + `from_street`/`to_street` names (Manhattan, `feature_typ='0'`); locate along the segment at `distance_from_intersection` feet from the `from_street` node using `shapely` `line.interpolate`; offset perpendicular by half `roadbed_width` toward `side_of_street` via `parallel_offset` to get the curb point; use published coord only to disambiguate ties and compute `snap_confidence`.
- **B.2 Points→segments — RECOMMENDED: arrow-direction extrapolation (option b), with CurbLR begin/middle/end as the conceptual frame.** Justification: NYC signs encode direction explicitly via `arrow_direction` (`-->`/`<--`/`<->`/blank), which is exactly the "sign relationship" CurbLR's `Location.md` says can extrapolate points into segments. Rule: a `-->` reg extends from its post to the next same-type sign or the intersection; `<--` backward; `<->`/blank → whole blockface (consistent with 34 RCNY 4-08 "one sign governs the block"). Fixed-width buffers (option c) rejected as too crude for pricing/capacity; pure CurbLR/SharedStreets begin/middle/end (option a) rejected as adding a SharedStreets dependency we avoid by using LION IDs directly.
- **B.3 Curb model:** offset LION centerline (no comprehensive better curb-edge line published).
- **B.4 `snap_confidence`** blends name-match quality, agreement between derived point and published coord, and segment-length plausibility; low confidence → amber in UI.

## Appendix C — Rules Not in the Sign Data (Research Section C regulatory, consolidated)
- One authorized sign governs the whole block; meter time not transferable between blockfaces (34 RCNY 4-08; DOT FAQ: "parking time purchased is not transferable… unique to a specific block face").
- Most-restrictive-governs; missing sign → remaining posted regs apply (DOT stacking rule).
- Hydrant **15 ft each side**, incl. floating lanes by protected bike lanes; crosswalks, bus stops, driveways, "No Stopping" zones = sign-free prohibitions, always encode as background constraints.
- Meters off Sundays + six Major Legal Holidays; ASP suspensions per annual calendar + emergencies.
- Rates progressive & zoned — ingest live from nyc.gov/paythemeter, never hardcode.