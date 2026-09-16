# The static site

A second way to run CurbCheck: the same frontend, the same verdicts, with no
server. The engine and the geocoder run in the browser, in a Web Worker, over a
data pack the ETL compiles from `data/curbcheck.sqlite`. GitHub Pages serves
the result at `curbcheck.orentirschwell.com`, and a weekly GitHub Actions job
re-runs the ETL, recompiles the pack, checks the browser engine against the
Python one on that exact data, and redeploys.

The local server (`curbcheck serve`) stays the reference implementation. It is
what the differential harness checks the browser engine against, and it is
what a developer runs. `web/` is one frontend for both; nothing in it changes
meaning between the two. `docs/DECISIONS.md` D32 records the decision.

## Why static, in one paragraph

The threat model (SPEC §3, `docs/SECURITY.md` T6) promises that a destination
never leaves the machine. A hosted server would break that promise for every
visitor: the operator terminates every request and sees every typed address.
A static site keeps most of it — no search, no address, no coordinate ever
leaves the browser — and what it gives up (the host sees the IP and which map
tiles were fetched) is stated in "Security, restated" below. The price is a
second implementation of `engine/` and `geocode/` in JavaScript, kept honest
by replaying the Python engine's answers through it on every deploy.

## Layout

```
site/
  README.md            how to build, test and deploy; the go-live checklist; measured sizes
  PRIVACY.md           what GitHub sees, what the site does not do
  build.py             assembles dist/ (stdlib only)
  slice_basemap.py     PMTiles archive -> dist/basemap/tiles/{z}/{x}/{y}.pbf (stdlib only)
  static/              overlaid onto web/ by build.py
    api.js             drop-in for web/api.js: same exports, same ApiError, talks to the worker
    worker.js          thin shell: onmessage -> handlers.js -> postMessage
    handlers.js        the six operations as pure functions over a loaded pack
    schemas.js         request validation mirroring curbcheck/api/schemas.py
    time.js            America/New_York wall clock <-> epoch, with zoneinfo's gap and fold rules
    money.js           cents arithmetic that reproduces cost.py's Decimal results
    pack/loader.js     pack files -> typed arrays, string arrays, maps, grid indexes
    pack/grid.js       uniform grid over bounding boxes
    engine/            geo.js window.js calendar.js resolve.js cost.js coverage.js labels.js
                       ranges.js search.js signs.js         one file per Python module
    geocode/           normalize.js (etl.streets.normalize_street_name + etl.addresses.fold,
                       street_variants, name_words) candidates.js query.js names.js street.js
                       address.js intersection.js places.js suggest.js reverse.js index.js
  tests/
    *.test.js          unit tests, `node --test site/tests`
    fixtures/          small, committed: time_cases.json and money_cases.json from Python
    harness/
      queries.py       the query set, deterministic
      make_fixtures.py Python engine -> expected.json (gitignored; regenerated per run)
      compare.test.js  JS engine over the same queries, exact comparison
curbcheck/pack.py      the pack compiler; `curbcheck pack --out DIR`
.github/workflows/pages.yml
```

## Changes to shared code

Kept to what the static variant cannot do without, so the local server is the
same program it was:

- `curbcheck/geocode/names.py:_driver_rows` returns a sorted list rather than a
  set. `search_names` keeps the first spelling it sees at equal match rank, so
  a set made the fourth suggestion for `p` depend on `PYTHONHASHSEED`. The
  harness needs the reference to be deterministic; so does the user.
- `curbcheck/cli.py` gains `pack`. `curbcheck/pack.py` is new.
- `web/app.js:reportHealth` renders `health.notice` when present (the pack
  staleness warning; the server never sets it). `web/about.js` renders
  `health.hosting` when present (the "how this copy is served" paragraph).
- `web/copy.js` gains a sentence for `pack_unavailable`.
- `tests/test_web_static.py` runs its forbidden-call and no-remote-URL checks
  over `site/static/` as well.
- `eslint.config.js` gains blocks for `site/static/**` (worker globals) and
  `site/tests/**` (Node globals). `.prettierignore` and the CI `frontend` job
  cover `site/`.
- `Makefile`: a `js` target (`node --test site/tests`) inside `check`.
- `.gitignore`: `site/tests/harness/expected.json`.

## The pack

`curbcheck pack --out DIR` reads the SQLite file and writes:

```
meta.json                       small, never compressed, always fetched fresh
rules.<sha256[:12]>.json.gz     spans, span geometry, regulations, signs, meter rates, calendar
streets.<hash>.json.gz          centerline segments and nodes
geocode.<hash>.json.gz          the eight address tables
```

Gzip is applied by the compiler, not left to the host: GitHub Pages gzips
small JSON on the wire but documents no size ceiling, and a 27 MB file that
ships uncompressed is a 27 MB first load. The worker inflates with
`DecompressionStream("gzip")`. The hash in the name is for correctness across
deploys, not for cache lifetime — Pages sends `Cache-Control: max-age=600` on
everything and allows no custom headers, so every file is revalidated by ETag
every ten minutes whatever it is called.

`meta.json`:

```json
{
  "format": 1,
  "built_at": "2026-09-15T18:02:11+00:00",
  "sync": { "...every sync_meta row whose value is not a filesystem path, as strings..." },
  "coverage": { "area": "Manhattan", "bbox": [min_lon, min_lat, max_lon, max_lat] },
  "calendar_missing": false,
  "counts": { "spans": 36172, "regulations": 59020, "signs": 74389, "segments": 11102 },
  "files": { "rules": "rules.3fa9c1e2b0d4.json.gz", "streets": "…", "geocode": "…" }
}
```

`sync` drops `calendar_source_path` (an absolute path on the build machine;
`docs/SECURITY.md` says messages never carry one) and any other key whose value
starts with `/`. The static `syncStatus()` therefore returns one key fewer than
the server's; the About sheet reads none of the dropped ones.

### Table encoding

Each `.json.gz` inflates to `{"tables": {name: table}, "dicts": {name: [strings]}}`.
A table is `{"rows": N, "columns": {name: column}}` and a column is one of:

- a plain JSON array of length N: numbers, strings, `null`, or 0/1 for booleans;
- `{"dict": "<dict name>", "codes": [ints]}` — dictionary-coded strings, `-1` for null;
- `{"coords": [doubles, lon lat lon lat …], "offsets": [ints, length N+1]}` — a
  column of LineStrings; row i's vertices are `coords[2*offsets[i] … 2*offsets[i+1])`;
- `{"lists": [ints…], "offsets": [ints, N+1]}` — a column of int lists (foreign-key lists).

Numbers are written by Python's `json` (shortest round-trip repr), so every
double survives exactly. Coordinates are **not** quantized: the harness
compares distances and ranks exactly, and 1 MB of transfer is a fair price for
"any difference is a bug". Every table is in SQLite `rowid` order, and the
loader keeps that order; where a Python query relies on an index's order
rather than an `ORDER BY`, the JS sorts explicitly and says which query it is
matching (see "Porting rules").

| file | table | columns (SQLite name → JSON name, camelCase) |
|---|---|---|
| rules | `spans` | `reg_seg_id`, `segment` (row index into `segments`, -1 when null), `segment_id` (string, kept for the API), `side`, `start_ft`, `end_ft`, `geom` (coords column), `length_ft`, `capacity_cars`, `capacity_approximate`, `confidence`, `derived_from` (lists column of row indexes into `signs`; an id that names no sign row is dropped, counted in meta), `gap_kind` |
| rules | `regulations` | `reg_id`, `span` (row index), `action` (dict), `permitted`, `vehicle_class` (dict), `exclusive`, `days_mask`, `time_from`, `time_to`, `metered`, `max_duration_min`, `flags` (int bitmask, bit i = field i of `model.Flags` in declaration order), `effective_from`, `effective_to`, `arrow` (dict), `raw_sign_description` (dict `descriptions`), `parse_method` (dict), `parse_confidence` |
| rules | `signs` | every column of `sign` except the two `sign_x/y_coord` and `derived_lon/lat`: `sign_id`, `order_number`, `on_street` (dict `street_names`), `from_street` (same dict), `to_street` (same dict), `side_of_street`, `distance_from_intersection`, `arrow_direction` (dict), `facing_direction` (dict), `sign_code` (dict), `sign_description` (dict `descriptions`), `segment` (row index or -1), `segment_id`, `snap_confidence`, `snap_notes` (dict), `is_regulation`, `panel_class` (dict) |
| rules | `meter_rates` | `blockface_id`, `segment` (row index or -1), `segment_id`, `side`, `rate_label` (dict), `hour_rates` (JSON string as stored), `commercial_hour_rates` (as stored), `max_session_min`, `source`, `confidence` |
| rules | `calendar` | `date`, `is_major_legal_holiday`, `meters_suspended`, `label` |
| streets | `segments` | `segment_id`, `street_name`, `street_norm`, `from_node` (row index into `nodes` or -1), `to_node`, `width_ft`, `length_ft`, `geom` (coords), `left_low_address`, `left_high_address`, `right_low_address`, `right_high_address` |
| streets | `nodes` | `node_id`, `lon`, `lat`, `street_names` (JSON string as stored) |
| geocode | `address_points` | `street_norm` (dict), `house_number`, `display`, `zipcode`, `lon`, `lat` |
| geocode | `intersections` | `a_norm`, `b_norm`, `display`, `lon`, `lat` |
| geocode | `places` | `place_id`, `display`, `lon`, `lat` |
| geocode | `place_tokens` | `token`, `position`, `place_id`, `search_name` |
| geocode | `streets` | `street_norm`, `display`, `lon`, `lat` |
| geocode | `street_variants` | `variant`, `street_norm` |
| geocode | `street_tokens` | `token`, `position`, `street_norm`, `search_name` |
| geocode | `zip_centroids` | `zipcode`, `lon`, `lat`, `address_points` |

The bounding-box columns are not shipped; the loader derives them from the
coordinates. `min_lon`/… in SQLite were computed by `db.geojson_bbox` from the
same doubles, so they are equal.

### The loader API (`site/static/pack/loader.js`)

`loadPack(readText)` takes one function, `readText(name) → Promise<string>`
(the inflated text of a pack file; the worker implements it with `fetch` plus
`DecompressionStream`, the Node tests with `zlib`), fetches `meta.json` then
the three files in parallel, and resolves to a `pack`:

```js
pack.meta                       // meta.json as parsed
pack.spans, pack.regulations, pack.signs, pack.meterRates, pack.calendar,
pack.segments, pack.nodes, pack.geocode.{addressPoints, intersections, places,
  placeTokens, streets, streetVariants, streetTokens, zipCentroids}
```

Each table is an object with `n` and one property per column, camelCased:
numeric columns with no nulls are `Float64Array` or `Int32Array`; columns that
may hold null are plain arrays; dictionary columns are plain arrays of the
decoded strings (shared string objects, so cheap); booleans are `Uint8Array`;
a coords column is `{coords: Float64Array, offsets: Uint32Array}` and a lists
column is `{lists: Int32Array, offsets: Uint32Array}`. The loader also builds:

```js
pack.spans.bbox            // {minLon, minLat, maxLon, maxLat}: Float64Array each
pack.segments.bbox         // same
pack.index.spanByRegSegId  // Map<string, row>
pack.index.segmentById     // Map<string, row>
pack.index.nodeById        // Map<string, row>
pack.index.signById        // Map<string, row>
pack.index.regulationsBySpan   // Int32Array[]  rows of `regulations`, rowid order, [] when none
pack.index.signsBySegment      // Int32Array[]  rows of `signs` per segment row, rowid order
pack.index.meterRatesBySegment // Int32Array[]  rows of `meterRates` per segment row, rowid order
pack.index.spanGrid, pack.index.segmentGrid   // grid.query(minLon, minLat, maxLon, maxLat)
                           // -> Int32Array of candidate rows in ascending row order, a superset
                           // the caller filters by bbox, exactly as the SQL bbox WHERE did
pack.lineString(table, row) // {type: "LineString", coordinates: [[lon, lat], …]} for output
```

The geocoder builds its own lookups from the `geocode` tables in
`geocode/index.js` (`buildGeocodeIndex(pack)`), called by the worker once
after `loadPack`, so the first keystroke pays nothing.

Sizes are measured by `curbcheck pack` and printed; `site/README.md` records
them. The reviewer's estimate for this encoding with exact doubles is about
30 MB inflated and 9–10 MB gzipped; the worker holds roughly 60–80 MB of heap
on top of MapLibre. Measure on a phone before go-live.

## The worker and `api.js`

`site/static/api.js` exports what `web/api.js` exports — `ApiError`,
`search`, `segment`, `geocode`, `reverse`, `health`, `syncStatus` — with the
same signatures and the same JSON shapes `docs/API.md` specifies. The frontend
cannot tell which one it loaded. The worker is created on first use:
`new Worker("./worker.js", { type: "module" })`.

Envelope, one message per call:

```
→ { id, op: "search" | "segment" | "geocode" | "reverse" | "health" | "syncStatus", args }
← { id, ok: true, result }
← { id, ok: false, error: { code, message } }
```

`args` mirror the HTTP inputs: `search` takes the request body object;
`segment` takes `{ reg_seg_id, t1, t2 }` (`t1`/`t2` optional, both or neither);
`geocode` takes `{ q }`; `reverse` takes `{ lat, lon }`; the last two take
nothing. `handlers.js` exports `handle(op, args, pack)` returning the result or
throwing `HandlerError(code, message)`; `worker.js` is the fifteen lines that
move messages. The Node harness imports `handlers.js` directly.

Error codes are the server's (`docs/API.md`): `validation_error`,
`invalid_request`, `not_found`, `address_not_found`, `outside_coverage`,
`internal_error`, plus `pack_unavailable` where the server would say
`database_unavailable` (the pack failed to load or is malformed). `api.js`
also raises `aborted` when the caller's `AbortSignal` fires (the worker's
answer for that id is dropped on arrival) and `timeout` after 20 s, so the
`app.js` branches on `network_error`/`timeout` still mean something.

`health()` answers only once the pack has loaded, with the server's shape
(`status`, `db_present: true`, `db_readonly: true`, `sign_count`,
`calendar_missing`, `coverage`) plus two fields the server never sets:

- `notice`: `{ kind: "warn", title, text }` when `built_at` is more than
  `STALE_AFTER_DAYS` (14) ago. A weekly job that silently stops must not leave
  a page that looks current. GitHub disables `schedule` triggers after 60 days
  without a commit, which is exactly this case.
- `hosting`: `{ kind: "static", built_at, text }`, the paragraph the About
  sheet shows (see `site/PRIVACY.md`).

If the pack cannot be loaded, `health()` rejects with `pack_unavailable` and
so does everything else.

`schemas.js` mirrors `curbcheck/api/schemas.py`: same bounds, same window
limits, unknown fields rejected, `t1`/`t2` parsed as below, and the same
"either lat and lon or address" rules. Messages match the server's where the
server's are one sentence; the harness compares codes, not messages.

## Time (`site/static/time.js`)

Python's `zoneinfo` semantics have to be reproduced, not approximated, and
`engine/window.py` leans on three of them that epoch arithmetic gets wrong.

A **wall clock** is `{ year, month, day, hour, minute, second, fold }` in
America/New_York. An **instant** is an epoch millisecond.

- `wallFromEpoch(ms)`: via `Intl.DateTimeFormat` with `timeZone:
  "America/New_York"`, `hourCycle: "h23"`; `fold` is 1 when `ms - 3_600_000`
  has the same wall clock (the second occurrence on the fall-back Sunday),
  else 0. This is what `datetime.astimezone(NYC_TZ)` produces.
- `epochFromWall(wall)`: candidate offsets are the zone offsets 24 h before
  and 24 h after the wall clock read as UTC. If exactly one candidate maps back
  to the wall clock, use it. If both do (fall back), `fold: 0` takes the earlier
  instant, `fold: 1` the later. If neither does (spring forward), `fold: 0`
  uses the offset in force *before* the transition, `fold: 1` the one after —
  which is `zoneinfo`'s rule, so 02:30 EST on 2026-03-08 is 07:30 UTC.
- `wallKey(wall)` = `"YYYY-MM-DDTHH:MM"`; `fold` is not part of it.
- `atMinute(year, month, day, minuteOfDay)`: local midnight plus
  `minuteOfDay` minutes of *wall* time, `fold: 0` — `datetime(..., tzinfo) +
  timedelta` is wall-clock arithmetic. Minute 1440 is the next day's 00:00.
- `parseRequestTime(text)`: ISO 8601. No offset → that wall clock with
  `fold: 0` (the server's "naive means New York"). An offset or `Z` → the
  instant, then `wallFromEpoch`. Anything else → `validation_error`.
- `formatWhen(start, end)` = `"%a %H:%M-%H:%M"` of the two wall clocks with
  English three-letter weekdays.

`expandWindow(t1, t2, regulations)` must then do exactly what `window.py` does
with a **set of aware datetimes**: Python compares, sorts, deduplicates and
range-filters them by wall clock (same tzinfo ⇒ `fold` and offset ignored),
and the first inserted wins a tie — `{start, end}` first, then every day's
rule minutes, then each day's minute 1440. `Interval.minutes` is
`round((endMs - startMs) / 60000)` from the two boundaries' own `fold`s, so
the fall-back Sunday is 1,500 minutes long and an interval can be 0 minutes on
the spring-forward Sunday and still exist. `weekday`, `date` and
`startMinuteOfDay` come from the start wall clock. `Interval.date` keys the
calendar as `"YYYY-MM-DD"`.

`site/tests/fixtures/time_cases.json` is generated once by
`site/tests/harness/make_time_fixtures.py` from Python (both DST Sundays of
2026 and 2027, a window across each, a `t1` that arrives as `fold: 1`, minute
1440, a 24-hour window at each transition) and is committed; `time.test.js`
replays it.

## Money (`site/static/money.js`)

`cost.py` keeps `Decimal` at 28 digits and rounds **once**, `ROUND_HALF_UP`,
to cents. The JS reproduces the results, not the mechanism:

- `parseCents("8.25") → 825`. A rate with more than two decimals is refused by
  the pack compiler, so the pack never carries one.
- `meterPriceCents(hourRatesCents, chargedMinutes)`: walk the hours as
  `meter_price` does, accumulating the integer `N = Σ rateCents_i × minutes_i`,
  and return `Math.floor((2 * N + 60) / 120)`. Every term but the last is a
  whole hour, so the only non-terminating term is the partial one, and a sum
  that lands exactly on half a cent is then always a terminating sum, which
  Decimal computes exactly; the integer formula is therefore identical to the
  Python for every input `meter_price` can receive. `money` is
  `centsToString(cents)` (`"12.50"`), `money_value` is `cents / 100` — the
  correctly rounded double of the same rational as Python's `float(Decimal)`.
- `riskCents(parseConfidence, snapConfidence, fineDollars = 65)`: the
  probability is the double `min(1, max(0, 1 - min(pc, sc))) * 0.5`; Python
  then multiplies `Decimal(str(p))` by the fine and rounds half up. Take
  `String(p)`, parse it as `mantissa × 10^exponent` with `BigInt` (both
  `"0.00005"` and `"5e-05"` forms), multiply exactly by the fine, round half
  up to cents. `risk` is `cents / 100`.
- `totalCost(walkMin, moneyValue, riskValue, weights)` is
  `weights.walk * walkMin + weights.money * moneyValue + weights.risk * riskValue`
  in that order, `moneyValue` 0 when unknown.

`site/tests/fixtures/money_cases.json` is generated from Python over every
distinct `hour_rates` list in the database × a spread of minutes, and every
`(parse, snap)` confidence pair in the database; committed and replayed.

## Porting rules

- One JS file per Python module, same name, same function names in camelCase,
  same order in the file. A reader with both open can follow line by line.
- Where SQL did the work, the JS does the same selection over the loaded
  tables and the comment names the SQL constant it replaces. Where a query has
  no `ORDER BY`, the order SQLite actually produced is the order of the index
  the planner used; check with `EXPLAIN QUERY PLAN`, sort explicitly to match,
  and say so. Known cases: `search._load_stacks` (rowid order within a span),
  `routes._SEGMENT_REGULATION_SQL` (`ORDER BY reg_id`), `search._load_meter_rates`
  (`ix_meter_rate_segment`: segment_id, side, rowid), `routes._METER_RATE_SQL`
  (`ORDER BY blockface_id`), `reverse._ADDRESS_IN_BOX_SQL` (covering index:
  lon, lat, display), `street._VARIANT_PREFIX_SQL` (`DISTINCT` applied in
  `variant` order, `LIMIT` after it: scan the whole prefix range, keep first
  occurrence).
- String comparison is by UTF-16 code unit (`a < b`), never `localeCompare`:
  SQLite's `BINARY` collation and Python's `str` ordering agree with it on
  ASCII, and every stored name is ASCII (measured: 0 non-ASCII rows). Prefix
  bounds (`query.prefix_bound`) are computed on code points.
- Floats are combined in the same order as the Python. Point-to-segment
  distance is GEOS's `Distance::pointToSegment`, literally:
  `len2 = dx*dx + dy*dy; r = ((px-ax)*dx + (py-ay)*dy)/len2; r<=0 → |p-a|;
  r>=1 → |p-b|; else s = ((ay-py)*dx - (ax-px)*dy)/len2; |s|*sqrt(len2)`,
  with `|p-a| = sqrt(ddx*ddx + ddy*ddy)`; a LineString's distance is the
  minimum over its segments. Radians are `deg * (Math.PI / 180)`, parenthesised
  so the constant is the one `math.radians` multiplies by. Sorting uses the
  same keys; both languages' sorts are stable.
- `_demote_contested_spans` is ported, with a collinear-overlap length for
  `line.intersection(other).length`. It does not fire on a current database.
  `ranges.linemerge` handles a MultiLineString the database never holds
  (measured: all 47,274 geometries are LineStrings); port the LineString path
  and return null for anything else.
- Strings the API returns (`reason`, `caveats`, labels, messages) are copied
  from the Python constants verbatim. The harness compares them exactly.
- Nothing in `site/static/` builds markup, touches `innerHTML`, fetches from
  another origin, or reads `localStorage`. `tests/test_web_static.py`'s checks
  run over it.

## The differential harness

`site/tests/harness/queries.py` builds a deterministic query set from the
database (fixed seed): 300 destinations spread over the coverage bbox and kept
only if within coverage, plus every address in `tests/test_geocode_real.py`;
windows covering a weekday, a Saturday, a Sunday, every ASP suspension date in
the calendar, both DST Sundays, midnight-crossing, 5 minutes and 24 hours;
walk radii 3, 10 and 30 minutes; the default weights and one skewed set;
`limit` 500 and `map_limit` 5000 for most (nothing cut), plus a handful at the
defaults. Then 200 typed strings for `geocode` (doors, corners, places,
streets, ZIPs, typos, single letters, partial words), 100 pins for `reverse`,
and for `segment` the first three legal and first three non-legal spans of
every search, with and without the window.

`make_fixtures.py` calls the Python engine directly (no HTTP, the same
functions `routes.py` calls) and writes `expected.json`. `compare.test.js`
loads the pack from disk, runs each query through `handlers.handle`, and
compares with `assert.deepStrictEqual` after one normalisation: every float
is compared with a relative tolerance of 1e-9 (an ulp of difference between
GEOS and the JS distance is allowed; anything larger is a bug). Verdicts,
strings, ids, order, counts, sets: exact. A failing case prints the query, the
field path, and both values.

The harness is a deploy gate: `pages.yml` runs it after the pack is built and
before `build.py`, on the data that ships.

## The build

```
python site/slice_basemap.py data/basemap/manhattan.pmtiles dist/basemap/tiles
python site/build.py --out dist --pack build/pack --base-path /
```

`slice_basemap.py` reads the PMTiles v3 archive (header, gzip'd root and
leaf directories, Hilbert tile ids) with the standard library and writes each
tile's MVT bytes, inflated, as `{z}/{x}/{y}.pbf`. Serving the archive itself
over HTTP range requests is a known-broken combination on GitHub Pages
(protomaps/PMTiles #584: Pages answers 206 with `max-age=600`, Firefox caches
the first partial response and decodes garbage), so the tiles are files. 474
tiles at zoom ≤ 15 for the Manhattan box. The static style therefore has
`"tiles": ["<base>basemap/tiles/{z}/{x}/{y}.pbf"]` instead of the
`pmtiles://` URL; `build.py` writes that style, and `pmtiles.js` is not
shipped.

`build.py`:

1. Copies `web/` to `dist/` byte for byte and re-verifies the three
   `MANIFEST.md`s over the copy.
2. Overlays `site/static/`. `api.js` replaces `web/api.js`.
3. Copies the pack to `dist/pack/`.
4. Rewrites the absolute basemap paths — `STYLE_URL` in `map.js`, `glyphs` and
   `sprite` in `style.json` — to `<base-path>basemap/…`, asserting it found
   exactly the occurrences it expects, and writes the vector source as above.
   With the custom domain the base path is `/`; on the project URL it is
   `/curbcheck/`, which `actions/configure-pages` reports as `base_path`
   (empty string or `/repo`, no trailing slash; the workflow appends `/`).
5. Injects, as the first child of `<head>`, `<meta http-equiv="Content-Security-Policy"
   content="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; worker-src 'self' blob:; object-src 'none'; base-uri 'none'">`
   and `<meta name="referrer" content="no-referrer">`. `frame-ancestors` is
   not expressible in a meta tag. `style-src` drops `'unsafe-inline'`: the
   vendored MapLibre styles through CSSOM property assignment, which CSP does
   not govern, and `web/index.html` has no `style=` attribute; the Playwright
   QA pass confirms zero CSP reports in the console before this is kept.
   `worker-src 'self' blob:` stays — MapLibre's fallback worker is a blob.
6. Removes the `pmtiles.js` script tag and file. Writes `.nojekyll` (harmless
   with an Actions deploy; protects a branch deploy).
7. Prints every file over 100 KB with its size, and the total.

## The workflow

`.github/workflows/pages.yml`: `schedule` Mondays 09:00 UTC, `workflow_dispatch`,
and `push` to `main` when `web/`, `site/`, `curbcheck/`, `requirements*.txt`
or the workflow changed. `contents: read` for the workflow and `pages: write`,
`id-token: write` on the `deploy` job only; every action pinned by commit SHA;
`concurrency: { group: pages, cancel-in-progress: false }`; `timeout-minutes:
30` on build, 10 on deploy. `--max-age-days 0` because the restored cache keeps
last week's mtimes and the default freshness rule would skip every download on
a run under a week after the last, then stamp `built_at` now.

```
build:
  checkout; setup-python 3.12; make setup
  actions/configure-pages  (id: pages)
  actions/cache data/raw           key: raw-<ISO week>, restore-keys: raw-
  actions/cache data/raw/manifest.jsonl separately   key: manifest-<run id>, restore-keys: manifest-
  curbcheck sync --max-age-days 0   # net.py allowlist; the drift check compares against the restored manifest
  actions/cache data/basemap        key: basemap-<YYYY-MM>
  (miss) download go-pmtiles 1.31.2 Linux x86_64, verify sha256
         3ed7dbf4ec2e6dfe5e25b6f70d1ffc932729f93c86db353bf514dd71010a312f, run scripts/fetch_basemap_tiles.py
  curbcheck pack --out build/pack
  python site/tests/harness/make_fixtures.py --pack build/pack
  node --test site/tests            # unit + differential
  python site/slice_basemap.py …; python site/build.py … --base-path "${{ steps.pages.outputs.base_path }}"
  upload-pages-artifact dist
deploy: deploy-pages, environment github-pages
```

The manifest cache is what makes `fetch.check_row_count_drift` mean something
on a runner: it only compares against the previous entry for the same URL, and
without the carried-forward manifest every run is a first run. A drift abort
fails the build; the previous deploy stays live. `actions/cache` evicts
entries unused for seven days, which a weekly cadence sits on; `restore-keys`
makes a miss a slower run rather than a broken one.

Nothing is committed by CI. The pack, `expected.json` and `dist/` are build
outputs (STYLE_GUIDE §6).

## Security, restated for a static host

| | Server (`docs/SECURITY.md`) | Static site |
|---|---|---|
| T1 dependencies | hash-pinned pip, vendored JS | the same code and the same lockfiles run on the runner; the build adds no npm package. Actions are pinned by commit SHA; go-pmtiles by sha256. The tile archive it cuts from the Protomaps daily build is the one shipped artifact with no recorded hash. |
| T2 data endpoints | `net.py` allowlist during sync | the same `net.py` on the runner; `scripts/fetch_basemap_tiles.py` runs outside it against its own two-host allowlist, which promotes that script from developer tool to release step (residual risk 6 in SECURITY.md is amended). The browser fetches nothing but same-origin files. |
| T3 hostile data | Pydantic at the ETL boundary; `textContent` in the UI | the same ETL; the same `web/dom.js`; the pack is data the worker reads, never code it evaluates |
| T4 local surface | loopback bind, no CORS, CSP header | no server. CSP as a meta tag, without `frame-ancestors`; no `X-Content-Type-Options` (Pages sends none). Clickjacking is therefore possible and accepted: an overlay could misrepresent a verdict. The only header-capable fix is a different host. |
| T5 prompt injection | no LLM | no LLM |
| T6 exfiltration | nothing leaves the machine after sync | no typed text, no coordinate, no search ever leaves the browser: the geocoder and the engine are same-origin files loaded whole. What GitHub Pages does see, as any host would: the visitor's IP, the page request, and which map tiles were fetched — a coarse trace of where the map was looked at. GitHub says the IP "is logged and stored for security purposes" and publishes no retention period. No cookies, no storage, no analytics, no third-party request. |

## Where the delivery departs from the contract above

Recorded so the doc and the code do not silently disagree:

- The window-length check (5 min – 24 h) is wall-clock, not elapsed:
  `SearchRequest` subtracts two datetimes that share a tzinfo, which CPython
  does without an offset correction, so the fall-back Sunday's midnight to
  midnight is 24 h and accepted. `schemas.wallSpanMs` reproduces it; the
  harness caught the elapsed-time version refusing 18 of 600 searches.
- `money.meterPriceCents` is what `search._price` calls (integer cents in);
  `cost.meterPrice` takes the rate strings and returns cents.
- `Interval.minutes` rounds half to even, as Python's `round` does.
- `meta.json` carries `dropped_sign_refs`, the count of `derived_from` ids that
  named no sign row (0 on the 2026-09-15 database).
- The typed-array choice per column follows the values, so a REAL column whose
  values happen to be integral arrives as `Int32Array`; read `column[row]` as
  a number and never branch on the array class.
- `segment.derived_from` returns only ids that name a sign row; the server
  returns the stored JSON list. Equal on every current row.
- `health()` in the static `api.js` waits up to 300 s for the pack to load;
  every other call queues behind that load without a timer of its own, then
  gets the server's budgets. A load that outlasts the budget is reported as
  `pack_unavailable` ("still downloading") and is retried by the next call,
  because at 3G speeds the 8 MB pack sits right on the old 120 s.
- The search card carries the window as a wall clock (a `Date` read through
  its UTC fields), so a 24-hour chip on either daylight-saving Sunday ends at
  the next midnight; this was a shared-UI bug the QA pass found and it is
  fixed for the local server too.
- `site/build.py` assembles into `<out>.building` and renames it into place,
  so a served directory is never half-rewritten.
- `site/tests/geocode.test.js` carries its own 400-line synthetic pack
  builder; a shared `site/tests/helpers/synthetic_pack.js` is the split to make
  if another test needs it.

## What stays the owner's call

The repository licence (README: "TBD"), the NYC Open Data terms review for the
seven datasets, and whether the §17 notice is shown in full on a first visit.
None of these blocks building; all three block publishing.

## Go-live, in order

1. Merge the `static-site` branch. Make the repository public (GitHub Pages on
   a free account requires it).
2. Settings → Pages → Build and deployment → Source: **GitHub Actions**. Run
   the `pages` workflow by hand and open `https://oren-t.github.io/curbcheck/`.
3. At Squarespace Domains: account.squarespace.com/domains → `orentirschwell.com`
   → **DNS** → **DNS Settings** → **Custom Records** → **Add Record**:
   Type `CNAME`, Host `curbcheck`, Data `oren-t.github.io` (no scheme, no
   trailing dot; Squarespace appends the domain to the host), TTL default →
   **Save**. Never point it at `orentirschwell.com` itself: GitHub warns that
   a subdomain aimed at the apex breaks HTTPS and may not resolve at all. The
   Google Sites records at the apex are untouched by this.
4. Settings → Pages → Custom domain: `curbcheck.orentirschwell.com` → Save.
   Wait for the DNS check (minutes to a day); tick **Enforce HTTPS** once the
   certificate exists (GitHub says the option can take up to 24 h to appear;
   if it never does, remove and re-add the domain).
5. Re-run the workflow (the base path becomes `/`). Confirm with
   `curl -sI https://curbcheck.orentirschwell.com/` — a 200, not a redirect.
