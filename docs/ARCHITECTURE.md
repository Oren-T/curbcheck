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
  db.py            Schema, connect(readonly=), Regulation <-> row, placeholders(), swap_in()
  cli.py           `curbcheck sync | serve | parse-report`
  geocode/         Local address, corner and place lookup over the ETL's vocabulary index
    candidates.py    The answer records, the kinds, and the confidence ladder
    query.py         Cleaning and classifying what was typed
    street.py        Which street a spelling means; the street as an answer
    address.py       The house-number rungs, incl. neighbour interpolation
    intersection.py  The corner rung     places.py   Place names and ZIP centres
    suggest.py       Which rungs run, in what order, what survives coverage
    reverse.py       The inverse: what a dropped pin is nearest to
  etl/
    fetch.py       Socrata pulls into data/raw; manifest records and the drift check
    stage.py       Schema validation, formula-injection neutralization, active-row filter
    streets.py     Street-name normalization, centerline graph, intersection lookup
    snap.py        Sign -> (segment, side, distance) via linear referencing; snap_confidence
    parse/         Deterministic grammar for sign_description -> list[Regulation]
      tokens.py      Lexer: case, whitespace, arrow glyphs, time and date literals
      grammar.py     Left-to-right scan producing the Regulation list
      days.py        Day expressions      times.py    Clock times and seasonal ranges
      vocabulary.py  Phrase tables        panels.py   `panel_class`, the non-regulation strings
      report.py      Coverage of the grammar over the live description corpus
    segments.py    Signs on a blockface-side -> curb spans, stacked per centerline segment-side
    meters.py      ParkNYC blockface rates -> meter_rate rows
    calendar.py    ASP suspension / holiday calendar -> asp_suspension rows
    build.py       Runs the steps in order, then atomically swaps the new DB in
  engine/
    window.py      Expand [T1,T2] into per-day sub-intervals; calendar overrides
    resolve.py     Most-restrictive-wins over a rule stack for one sub-interval; `Verdict`
    cost.py        Walk-time estimate, progressive meter price, risk term, `Weights`
    search.py      Radius query + per-segment verdict + ranking; `SearchResult`, `SignRef`
  api/
    app.py         FastAPI app, security headers, static mount, range-served basemap
    routes.py      /api/search, /api/segment/{id}, /api/geocode, /api/health, /api/sync-status
    schemas.py     Pydantic request models, `extra="forbid"`; every HTTP input passes through one
    errors.py      The single `{error: {code, message}}` shape
web/               16 plain ES modules, 3 stylesheets, no bundler
  index.html       Single page: the §17 strip, the map, the rail, the sheet
  app.js           UI state, the search lifecycle, and what each control does
  map.js           MapLibre setup, PMTiles protocol, layer styling by verdict
  api.js           fetch wrappers for the local API
  searchcard.js    Destination, window, radius and weight controls
  autocomplete.js  The ARIA 1.2 combobox over /api/geocode, one call per 150 ms
  results.js       Ranked list      detail.js   Per-segment panel with the raw sign text
  verdicts.js      The four verdict states: colour, label, and what each means
  rank.js          Client-side re-ranking from the weight chips (no new request)
  legend.js        Map legend      drawer.js   The phone drawer's three detents
  about.js         The About & data sheet    states.js   Empty, loading, error states
  dom.js           `el()` — the one place text reaches the page, always via textContent
  format.js        Money, minutes, confidence     copy.js   User-facing strings in one file
  tokens.css       Design tokens (verdict hues, inks, spacing), from docs/ux/tokens.css
  components.css   Component rules      styles.css   Layout and the responsive breakpoints
  favicon.svg
  fonts/           Inter 4.1, one 72 KB Latin subset, OFL licence and hashes in MANIFEST.md
  vendor/          maplibre-gl, pmtiles (checked in, hashes in MANIFEST.md)
  basemap/         style.json, glyphs, sprites (checked in, hashes in MANIFEST.md).
                   The .pmtiles archive is not here; it lives in data/basemap/.
```

## Data flow in the ETL

### Build pipeline order

```
fetch -> [ stage -> streets -> snap -> parse -> segments ] -> meters -> calendar -> swap
                    \_________ run_geometry _________/
```

**parse runs before segments**, which is the one ordering that is not obvious.
`segments.resolve_segments` groups the posts on a blockface-side into families
before extending each arrow, and the parsed `action` is a better answer to
"same kind of sign?" than the description's first two words. So
`build.run_geometry` parses every description first and hands
`resolve_segments` the result; `parse.parse_description` is cached per distinct
string, so `run_parse` reading them again afterwards costs nothing.

There is a second, smaller dependency in the same direction: `stage` calls
`parse.panel_class` so that staging and the parser cannot disagree about which
strings carry a regulation at all (`docs/DECISIONS.md` D19). `etl/parse/`
therefore imports nothing else in `etl/`.

1. **fetch** pulls signs (`nfid-uabd`), centerline (`inkn-q76z`), ParkNYC
   blockfaces (`e7yp-wx55`), rate zones (`f72k-2u3b`), meter locations
   (`693u-uax6`), and the ASP calendar. Every download is recorded in
   `data/raw/manifest.jsonl` with size, content type and SHA-256, and
   `fetch.check_row_count_drift` aborts if the row count moved more than 20%
   from the previous manifest entry for the same URL (SPEC §5.7).
2. **stage** validates each row against an explicit Pydantic schema, keeps only
   active Manhattan signs (`sign_design_voided_on_date IS NULL`, D9),
   neutralizes cells that begin with `= + - @`, and labels non-regulation
   panels with `parse.panel_class`.
3. **streets** builds the centerline graph: segments keyed by id, nodes keyed
   by rounded endpoint coordinates, and a normalized-name index so
   `EAST   85 STREET` and `E 85 ST` resolve to the same street.
4. **snap** locates each sign: find the segment(s) of `on_street` whose
   endpoint nodes touch `from_street` and `to_street`, place the post at
   `distance_from_intersection` feet from the `from_street` node, pick the
   curb side from `side_of_street`, and offset by half the street width. The
   published x/y is only a tiebreaker and a confidence input.
5. **parse** reads each distinct `sign_description` once and caches the
   result. Unparsed strings are reported, never guessed — they still get a
   prohibitive `regulation` row so the engine sees them in the stack (D13), as
   do "meters are not in effect above times" meta panels, which force every
   span carrying a sign from the same post to AMBIGUOUS (D17).
6. **segments** turns the posts on each blockface-side into regulation
   segments. Posts are grouped into families by the action step 5 read, and each
   arrow is extended by arity: a single arrow runs from its post to the next
   post of its own family or to the corner; a `<->` runs each way to the nearest
   same-family post, failing that to the nearest post of *any* family, and
   reaches the corner only where a direction holds no post at all (D20); no
   arrow means the whole blockface-side (34 RCNY 4-08). Then four passes:
   spans whose posts state an identical rule and that touch or overlap are
   unioned into one (16,113 merged, D5 in `docs/VALIDATION.md` §4); every span
   is **projected** off its chain onto the centerline segments it runs over,
   re-measured in each segment's own digitization direction and re-sided as the
   left or right of it, so the spans two different DOT blockface tuples put on
   one piece of curb share a key (D26); the spans on one segment-side are
   **flattened** — cut at every boundary, each resulting stretch carrying the
   union of the signs that cover it, stretches nothing covers dropped (D25) —
   so no two rows ever govern the same foot of curb and a ban and a permission
   that overlap reach `resolve` in one stack; and every `rw_type` 1 street side
   still uncovered gets a placeholder (D23). The pre-projection per-post spans
   exist only in memory. The parse cache then fills `regulation` once the spans
   exist, one row per (span, sign), which is where the provenance a flattened
   span's `derived_from` names is written out.
7. **meters** joins ParkNYC rates by normalized street names and side, with
   geometry as a check and the rate-zone polygon as the fallback. One row per
   *centerline segment* a blockface covers, not one per blockface, because
   `engine.search` looks a rate up by `(segment_id, side)` (D18).
8. **calendar** loads the year's suspension dates.
9. **build** writes everything to a fresh SQLite file, then renames it over
   the old one. The previous file is kept as `curbcheck.sqlite.prev`.

## SQLite schema (summary)

Geometry is stored as GeoJSON text in EPSG:4326 plus `min_lon, min_lat,
max_lon, max_lat` columns. Radius queries filter on the bbox in SQL and refine
with shapely in Python. At Manhattan scale — 11,102 street segments, 74,389
signs and 36,172 curb spans (30,524 real plus 5,648 grey placeholders) on the
2026-09-15 snapshot — this is instant and needs no spatial extension. `db.create_schema` writes every column up front;
there is no migration path and none is needed, because `curbcheck sync` always
builds a fresh file and `db.swap_in` renames it over the old one.

| Table | One row per | Key columns |
|---|---|---|
| `sign` | source sign row (active Manhattan) | `sign_id` (truncated SHA-256 of the row, D5), `order_number`, streets, side, `distance_from_intersection`, `sign_code`, `sign_description`, published x/y, `derived_lon/lat`, `segment_id`, `snap_confidence`, `snap_notes`, `is_regulation`, `panel_class` (the parser's `regulation` / `panel:*` labels, D19) |
| `street_segment` | centerline segment | `segment_id`, `street_name`, `street_norm`, `from_node`, `to_node`, `width_ft`, `length_ft`, geom, address ranges |
| `street_node` | intersection | `node_id`, lon/lat, street names meeting there |
| `regulation_segment` | resolved curb span, real or placeholder. Real spans on one centerline segment-side tile it and never overlap (D25, D26) | `reg_seg_id`, `segment_id` (**exact**: a span never crosses a segment), `side` (the compass letter DOT lettered the curb with; the stack key behind it is the left/right of the segment's own digitization direction, D26), `start_ft`, `end_ft` (**segment-local**, from the segment's own start, not from the corner DOT measures a post from), geom, bbox, `length_ft`, `capacity_cars`, `capacity_approximate`, `confidence`, `derived_from` (JSON list of sign_ids — every sign governing the stretch, from every chain that reaches it, not only the post the span started from), `gap_kind` |
| — placeholder rows | `rw_type` 1 street side no span covers (D23) | `derived_from='[]'`, `confidence=0`, `capacity_cars` NULL, the whole side's curb line, and `gap_kind` — `no_signs` where the source lists no active sign for that blockface-side (5,298), `unmatched_signs` where it lists some and none snapped (358). NULL on a real span. An empty rule stack is always `no_data`, never `legal` |
| `regulation` | one parsed rule on one span | `reg_id`, `reg_seg_id`, the `Regulation` fields (`action`, `permitted`, `vehicle_class`, `exclusive`, `days_mask`, `time_from/to`, `metered`, `max_duration_min`, `flags`, `effective_from/to`, `arrow`), `raw_sign_description`, `parse_method`, `parse_confidence`, `parse_notes` |
| `meter_rate` | ParkNYC blockface × centerline segment | `blockface_id`, `segment_id`, `side`, `rate_label`, `hour_rates` (JSON), `commercial_hour_rates`, `max_session_min`, `source`, `confidence`, geom |
| `asp_suspension` | calendar date | `date`, `is_major_legal_holiday`, `meters_suspended`, `label` |
| `street` | centerline street, by normalized name | `street_norm`, `display`, a lon/lat on one of its own segments |
| `street_variant` | spelling a person might type | `variant`, `street_norm`. `WITHOUT ROWID`, `variant` first, so resolving a half-typed street is a range scan inside the key (2,814 rows) |
| `address_point` | surveyed door from OTI AddressPoint | `street_norm`, `house_number`, `display`, `zipcode`, lon/lat (63,245 rows) |
| `intersection` | ordered pair of streets meeting at a node | `a_norm`, `b_norm`, `display`, lon/lat. Both orders stored, so a lookup never tries the pair twice (5,645 corners) |
| `place` / `place_token` | CommonPlace name / one word of one | `place_id`, `display`, lon/lat, `token_count`; `token`, `place_id` (5,817 names, 23,659 tokens) |
| `zip_centroid` | ZIP code | `zipcode`, lon/lat, `address_points` — the count is what says how coarse the centre is (91 rows) |
| `sync_meta` | key/value | `last_sync_at`, row counts, per-step coverage and failure-reason histograms (57 keys). Source SHA-256s are *not* here; they live in each `data/raw/*.meta.json` and in `data/raw/manifest.jsonl`. |

## Query path

`POST /api/search` → geocode or accept lat/lon → bbox prefilter on
`regulation_segment` → shapely distance filter within the walk radius → for
each candidate, load its rule stack → `window.expand_window(T1, T2)` → for each
sub-interval `resolve.resolve_interval(stack, interval, calendar)` → segment is
legal only if every sub-interval permits parking and any posted
`max_duration_min` covers the minutes it is itself in force for (D12(b), as
corrected) → `search._demote_contested_spans` → `cost.score` → sort → return
with raw sign text attached.

`_demote_contested_spans` turns a legal span that a prohibitive span on the same
centerline side geometrically overlaps into `ambiguous`. Since D25 and D26 the
ETL writes no such pair at all, so it has nothing to do on a current database
and is defence in depth for one built before them; it logs a warning whenever it
fires. Its key is `(segment_id, side)`, so it does not reach the 16 pairs left
on the one roadway CSCL draws twice — `scripts/validation_regress.py --overlaps`
compares geometry and does (`docs/VALIDATION.md` §11.2).

Verdict states are `legal`, `illegal`, `ambiguous`, `no_data`. The UI never
collapses these into two colors. A span with no rules at all — every
placeholder, and the 56 real spans whose only signs state no curb rule — is
`no_data`; nothing reaches `legal` on an empty stack.

**Geocoding is local and degrades in steps.** `geocode/` reads the
vocabulary index `etl/addresses.py` builds — 63,245 surveyed doors, 5,645
corners, 5,817 places, 2,814 street spellings — so a destination address never
leaves the machine, and neither does the typing that led to it (T6, D29). The
ladder is a surveyed door at confidence 0.98, a corner at 0.95, a place at
0.85, a number placed between two surveyed neighbours at 0.75, the street
itself at 0.70, `near <closest door>` at 0.60, CSCL's own address range at 0.50
for a street with no door at all, and a ZIP centre at 0.25. A low confidence is
returned and shown, never rounded up, and the user can always drop a pin
instead.

**The server holds one read-only connection per worker thread, not one per
request.** `api/routes.open_database` caches it in a `threading.local`, so
`sqlite3`'s thread affinity is respected and `check_same_thread` stays at its
default; the cache is dropped and reopened when the file's inode, mtime or size
changes, which is how a `curbcheck sync` that renames a new database into place
is picked up without a restart. The connection is worth keeping because
`db.connect(readonly=True)` gives it a 48 MB page cache: at SQLite's 2 MB
default the vocabulary index and the coverage geometry evict each other inside
a single `/api/geocode`, which cost 488 ms a keystroke on this container's data
mount and costs 22 ms now; and a 30-minute walk radius touches ~30 MB of
scattered pages, so anything under that makes every repeat search re-read the
file (3.1 s at 8 MB, 0.9 s at 48 MB). `engine.coverage` caches the centerline's extent
against the same file identity, because both the radius prefilter and
`/api/health` want that aggregate on every request.

**The basemap is served, not proxied.** `data/basemap/manhattan.pmtiles` is a
gitignored 23 MB archive that `api/app.py` serves at
`/basemap/manhattan.pmtiles` through Starlette's `FileResponse`, which answers
RFC 7233 Range requests with 206 and a `Content-Range` — pmtiles.js reads the
127-byte header and then individual tiles as byte ranges, so that is a
requirement, and `tests/test_api.py` pins it. A missing archive is a 404 and
nothing else; the app still runs. The style, glyphs and sprites are checked in
under `web/basemap/` and served by the static mount, so panning the map makes
zero third-party requests.

## Security boundaries

- `net.py` is the only module that imports httpx. This is not a ruff rule:
  `tests/test_boundaries.py` walks the AST of every module in the package and
  fails on a network import anywhere else.
- `etl/` never imports from `api/` and vice versa. The same test enforces it,
  and that `BIND_HOST` is a constant no flag or environment variable can widen.
- `geocode/` is the one seam that crosses: it imports the ETL's `fold` so a
  name typed by the user is folded exactly the way the ETL folded the data.
  One direction only.
- `api/` sets CSP, `X-Content-Type-Options: nosniff`, and binds `127.0.0.1`.
- Everything in `data/` is treated as untrusted at read time, including the
  SQLite file's text columns, which are escaped by the frontend before display.

## Extension points

- **Occupancy** (spec Phase 3, not built): a future `engine/occupancy.py`
  would return a probability per segment that the UI shows as a separate
  layer. It never changes a verdict.
- **Routing**: `cost.walk_minutes()` is the single place to swap the
  straight-line estimate for a pedestrian router.
- **Live ASP check** (opt-in, off by default, not built): a
  `calendar.live_check()` would be the only caller of the 311 API and must
  go through `net.py`. `api-portal.nyc.gov` is already in the allowlist.

## Deployment

`Dockerfile` builds the same two programs into one image; `docker-compose.yml`
splits them by what they may reach — `sync` gets network egress and a
writable `./data`, `serve` gets host networking (the only way a browser can
reach a process that binds `127.0.0.1` inside a container) and a read-only
mount. The tradeoff is written out in the compose file.
