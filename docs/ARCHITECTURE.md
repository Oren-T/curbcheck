# Architecture

CurbCheck is two programs sharing one SQLite file: an **ETL** that runs
occasionally with network access and writes the database, and a **server** that
runs with no network access and reads it. The split is deliberate: the only
code that touches the internet or parses downloaded files never runs in the
process that serves the browser.

```
              (weekly, online)                        (always, offline)
  NYC Open Data ──► curbcheck sync ──► data/curbcheck.sqlite ──► curbcheck serve ──► browser
                       ETL                                          API + static UI
```

## Package map

```
curbcheck/
  config.py        Paths, allowlist, physical constants with sources
  net.py           The only outbound HTTP client. Allowlist, cert check, byte cap, SHA-256
  model.py         Pydantic contract for parsed regulations (shared by parser, gold set, DB, API)
  db.py            SQLite schema, connection helper, parameterized access
  cli.py           `curbcheck sync | serve | parse-report`
  etl/
    fetch.py       Socrata pulls into data/raw with manifest records
    stage.py       Schema validation, formula-injection neutralization, active-row filter
    streets.py     Street-name normalization, centerline graph, intersection lookup
    snap.py        Sign -> (segment, side, distance) via linear referencing; snap_confidence
    segments.py    Signs on a blockface-side -> regulation segments via arrow extrapolation
    parse/         Deterministic grammar for sign_description -> list[Regulation]
    meters.py      ParkNYC blockface rates -> meter_rate rows
    calendar.py    ASP suspension / holiday calendar -> asp_suspension rows
    build.py       Runs the steps in order, then atomically swaps the new DB in
  engine/
    window.py      Expand [T1,T2] into per-day sub-intervals; calendar overrides
    resolve.py     Most-restrictive-wins over a rule stack for one sub-interval
    cost.py        Walk-time estimate, progressive meter price, risk term
    search.py      Radius query + per-segment verdict + ranking
  geocode.py       Local address / intersection lookup over centerline address ranges
  api/
    app.py         FastAPI app, security headers, static mount, range-served basemap
    routes.py      /api/search, /api/segment/{id}, /api/geocode, /api/health, /api/sync-status
    schemas.py     Pydantic request models; every HTTP input passes through one
    errors.py      The single `{error: {code, message}}` shape
web/
  index.html       Single page
  app.js           UI state and form handling
  map.js           MapLibre setup, PMTiles protocol, layer styling by verdict
  api.js           fetch wrappers for the local API
  styles.css
  vendor/          maplibre-gl, pmtiles (checked in, hashes in MANIFEST.md)
```

## Data flow in the ETL

1. **fetch** pulls signs (`nfid-uabd`), centerline (`inkn-q76z`), ParkNYC
   blockfaces (`e7yp-wx55`), rate zones, and the ASP calendar. Every download
   is recorded in `data/raw/manifest.jsonl` with size, content type, SHA-256.
2. **stage** validates each row against an explicit schema, keeps only active
   Manhattan signs, neutralizes cells that begin with `= + - @`, and aborts if
   the row count moved more than 20% from the previous manifest entry.
3. **streets** builds the centerline graph: segments keyed by id, nodes keyed
   by rounded endpoint coordinates, and a normalized-name index so
   `EAST   85 STREET` and `E 85 ST` resolve to the same street.
4. **snap** locates each sign: find the segment(s) of `on_street` whose
   endpoint nodes touch `from_street` and `to_street`, place the post at
   `distance_from_intersection` feet from the `from_street` node, pick the
   curb side from `side_of_street`, and offset by half the street width. The
   published x/y is only a tiebreaker and a confidence input.
5. **segments** turns the posts on each blockface-side into regulation
   segments: an arrow extends a rule from its post to the next post or the
   corner; no arrow means the whole blockface.
6. **parse** reads each distinct `sign_description` once and caches the
   result. Unparsed strings are reported, never guessed.
7. **meters** joins ParkNYC rates by normalized street names and side, with
   geometry as a check and the rate-zone polygon as the fallback.
8. **calendar** loads the year's suspension dates.
9. **build** writes everything to a fresh SQLite file, then renames it over
   the old one. The previous file is kept as `curbcheck.sqlite.prev`.

## SQLite schema (summary)

Geometry is stored as GeoJSON text in EPSG:4326 plus `min_lon, min_lat,
max_lon, max_lat` columns. Radius queries filter on the bbox in SQL and refine
with shapely in Python. At Manhattan scale (under 10k street segments, under
100k signs) this is instant and needs no spatial extension.

| Table | One row per | Key columns |
|---|---|---|
| `sign` | source sign row (active Manhattan) | `sign_id` (hash of row), `order_number`, streets, side, `distance_from_intersection`, `sign_code`, `sign_description`, published x/y, `derived_lon/lat`, `segment_id`, `snap_confidence`, `snap_notes`, `is_regulation`, `panel_class` |
| `street_segment` | centerline segment | `segment_id`, `street_name`, `street_norm`, `from_node`, `to_node`, `width_ft`, `length_ft`, geom, address ranges |
| `street_node` | intersection | `node_id`, lon/lat, street names meeting there |
| `regulation_segment` | resolved curb span | `reg_seg_id`, `segment_id`, `side`, `start_ft`, `end_ft`, geom, bbox, `length_ft`, `capacity_cars`, `capacity_approximate`, `confidence`, `derived_from` (JSON list of sign_ids) |
| `regulation` | one parsed rule on one span | `reg_id`, `reg_seg_id`, all `Regulation` fields, `raw_sign_description`, `parse_method`, `parse_confidence` |
| `meter_rate` | ParkNYC blockface | `blockface_id`, `segment_id`, `side`, `rate_label`, `hour_rates` (JSON), geom |
| `asp_suspension` | calendar date | `date`, `is_major_legal_holiday`, `meters_suspended`, `label` |
| `sync_meta` | key/value | last sync time, source hashes, row counts, coverage stats |

## Query path

`POST /api/search` → geocode or accept lat/lon → bbox prefilter on
`regulation_segment` → shapely distance filter within the walk radius → for
each candidate, load its rule stack → `window.expand(T1, T2)` → for each
sub-interval `resolve.verdict(stack, interval, calendar)` → segment is legal
only if every sub-interval permits parking and `max_duration_min` covers the
window → `cost.score` → sort → return with raw sign text attached.

Verdict states are `legal`, `illegal`, `ambiguous`, `no_data`. The UI never
collapses these into two colors.

## Security boundaries

- `net.py` is the only module that imports httpx. `ruff` has a custom check
  for this in `tests/test_boundaries.py`.
- `etl/` never imports from `api/` and vice versa.
- `api/` sets CSP, `X-Content-Type-Options: nosniff`, and binds `127.0.0.1`.
- Everything in `data/` is treated as untrusted at read time, including the
  SQLite file's text columns, which are escaped by the frontend before display.

## Extension points

- **Occupancy** (spec Phase 3): `engine/occupancy.py` may return a probability
  per segment that the UI shows as a separate layer. It never changes a
  verdict.
- **Routing**: `cost.walk_minutes()` is the single place to swap the
  straight-line estimate for a pedestrian router.
- **Live ASP check** (opt-in, off by default): `calendar.live_check()` would be
  the only caller of the 311 API and must go through `net.py`.
