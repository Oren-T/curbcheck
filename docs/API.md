# CurbCheck local API

The server (`curbcheck serve`) binds `127.0.0.1:8765` and serves two things:
the static frontend under `/`, and this JSON API under `/api`. There is no
CORS middleware and there never will be — the UI is same-origin by design
(SPEC §3.4), so a page from any other origin cannot read these responses.

Every field name below matches the Python dataclass it comes from
(`curbcheck.engine.search.SearchResult`, `SignRef`, `curbcheck.model.Regulation`).
They are not renamed on the way out.

## Conventions

- **Content type** is `application/json` on every `/api` response.
- **Times** are ISO 8601. A naive value (`2026-09-15T09:00:00`) is interpreted
  in `America/New_York`, because every parking sign states local time. An
  aware value is converted to `America/New_York`. Times in responses are
  returned as sent.
- **Money is a string**, never a JSON number: `"12.50"`. It is a `Decimal`
  server-side and the frontend must not do arithmetic on it as a float.
  `null` means the price is unknown, which is not the same as free (`"0.00"`).
- **Coordinates** are WGS-84 decimal degrees, `lon` before `lat` inside GeoJSON
  `geometry` objects and as named `lat` / `lon` fields everywhere else.
- **Geometry** is a raw GeoJSON geometry object (`{"type": "LineString",
  "coordinates": [[lon, lat], …]}`), ready to drop into a MapLibre source.

### Output encoding: the API does not sanitize, the frontend escapes

`sign_description`, `rate_label`, `street_name` and every other text column
originate in downloaded NYC Open Data and are **untrusted**. The API returns
them byte-for-byte as they are stored, JSON-escaped by the JSON encoder and
nothing more. A description containing `<script>alert(1)</script>` comes back
as the literal string `"<script>alert(1)</script>"`.

This is deliberate. Escaping in two places produces double-escaped text
(`&amp;lt;`) and hides what the sign really says, and CLAUDE.md requires the
raw sign text to be visible next to every verdict. **The frontend is the single
escaping boundary**: assign untrusted text with `textContent`, never
`innerHTML` (STYLE_GUIDE §4). The CSP below is the backstop if that slips.

### Security headers

Set on *every* response, including static files, errors, and 404s:

| Header | Value |
|---|---|
| `Content-Security-Policy` | `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; worker-src 'self' blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'` |
| `X-Content-Type-Options` | `nosniff` |
| `Referrer-Policy` | `no-referrer` |
| `Cache-Control` | `no-store` — on `/api/*` only |

This is SPEC §3.4's CSP with one addition: `worker-src 'self' blob:`. The
vendored `web/vendor/maplibre-gl/maplibre-gl.mjs` starts its worker from
`URL.createObjectURL(new Blob([…], {type: 'text/javascript'}))`, which
`worker-src 'self'` alone blocks. Verified by reading the vendored file, not
assumed. Drop `blob:` if MapLibre is ever revendored in a build that imports
the worker module by URL.

`Access-Control-Allow-Origin` is never set.

## Errors

Every error is a JSON object with the same shape. Stack traces, file paths, and
SQL are never included; they go to the server log instead.

```json
{ "error": { "code": "validation_error", "message": "t2: window must end after it starts" } }
```

| Status | `code` | When |
|---|---|---|
| 400 | `invalid_request` | Malformed path parameter, e.g. a `reg_seg_id` that is not in the id charset. Rejected before any query runs. |
| 404 | `not_found` | No such `reg_seg_id`. |
| 404 | `address_not_found` | `POST /api/search` was given an `address` the local geocoder could not resolve. |
| 422 | `validation_error` | Body failed Pydantic validation: unknown field, out-of-range value, `t2 <= t1`, coordinates outside Manhattan. |
| 503 | `database_unavailable` | `data/curbcheck.sqlite` is missing. Message: ``database not found; run `curbcheck sync` ``. |
| 500 | `internal_error` | Anything unexpected. Message is the constant `"internal error"`. |

`GET /api/health` never returns 503; it is the endpoint you ask *about* the
database, so it answers even when there is none.

---

## `POST /api/search`

Rank the curb spans near a destination for a time window.

### Request

Either `lat` + `lon`, or `address` — exactly one of the two. Unknown fields are
rejected (`extra="forbid"`).

| Field | Type | Required | Bounds |
|---|---|---|---|
| `lat` | number | with `lon` | 40.68 – 40.90 |
| `lon` | number | with `lat` | -74.05 – -73.88 |
| `address` | string | instead of lat/lon | 1–200 chars |
| `t1` | ISO 8601 datetime | yes | |
| `t2` | ISO 8601 datetime | yes | `t2 > t1`, window 5 min – 24 h |
| `walk_minutes` | number | no (default 10) | 1 – 30 |
| `weights` | object | no | `{walk, money, risk}`, each 0 – 10, defaults 1.0 / 1.0 / 0.5 |
| `limit` | integer | no (default 100) | 1 – 500 |
| `map_limit` | integer | no (default 2000) | 1 – 5000 |

`limit` caps the **ranked legal list**; `map_limit` caps **everything else**.
They are separate caps on separate things: ranking all four verdicts together
and then cutting at `limit` is what made a legal-rich neighbourhood return a
hundred green spans and no red ones (`docs/VALIDATION.md` U1). `map_limit` is
applied to the non-legal spans nearest the destination first, so a cap drops
the farthest curb rather than a whole verdict.

The lat/lon bounds are a Manhattan-ish bounding box. They are an input sanity
check, not a service area promise: the database only holds Manhattan, so a
point in the Bronx corner of the box simply returns no results.

```json
{
  "lat": 40.778147,
  "lon": -73.954484,
  "t1": "2026-09-15T09:00:00",
  "t2": "2026-09-15T11:00:00",
  "walk_minutes": 8,
  "weights": { "walk": 1.0, "money": 1.0, "risk": 0.5 },
  "limit": 100,
  "map_limit": 2000
}
```

### Response

```json
{
  "destination": { "lat": 40.778147, "lon": -73.954484, "label": "40.778147, -73.954484" },
  "results": [
    {
      "reg_seg_id": "3681:W:0",
      "geometry": { "type": "LineString", "coordinates": [[-73.954561, 40.778149], [-73.954063, 40.778831]] },
      "verdict": "legal",
      "reason": "metered parking permitted",
      "caveats": [
        "Temporary or construction signage may override what is shown here. The posted sign at the curb is the only authoritative regulation."
      ],
      "walk_min": 1.4,
      "money": "9.00",
      "price_known": true,
      "capacity_cars": 12,
      "confidence": 0.94,
      "signs": [
        {
          "sign_id": "9f2c…",
          "order_number": "1-11111",
          "sign_code": "PRK-9",
          "sign_description": "2 HOUR METERED PARKING 8:30AM-7PM EXCEPT SUNDAY",
          "parse_method": "grammar",
          "parse_confidence": 0.98
        }
      ],
      "charged_minutes": 120,
      "metered": true,
      "score": 10.4,
      "rate_label": "Area 1"
    }
  ],
  "counts": { "legal": 150, "illegal": 50, "ambiguous": 0, "no_data": 3, "total": 203 },
  "disclaimer": "CurbCheck is advisory only. …",
  "caveats": [
    "Temporary or construction signage may override what is shown here. The posted sign at the curb is the only authoritative regulation.",
    "Emergency ASP suspensions are not reflected. Same-day weather and parade suspensions are only visible if the optional 311 live check is enabled, and it is off by default."
  ],
  "sync": { "last_sync": "2026-09-15T03:18:02+00:00", "sign_count": 74389, "coverage_pct": 93.78 }
}
```

`results` is **one array in two parts**: the ranked legal spans, at most
`limit` of them, ordered by ascending `score`; then every other verdict within
the radius, at most `map_limit` of them, ordered `ambiguous`, `illegal`,
`no_data` and by `score` within each. One array rather than two because the map
draws all of it and the result list renders all of it, so two arrays would only
make the frontend concatenate them again.

`counts` counts **everything in the radius, before either cap**:

```json
"counts": { "legal": 150, "illegal": 50, "ambiguous": 0, "no_data": 3, "total": 203 }
```

The status line must be built from `counts`, never from `results.length`:
`results` can be a subset, and saying "100 stretches … 100 legal" about a
capped response states as fact something the query never established (SPEC §11,
`docs/VALIDATION.md` U1). When `results` is shorter than `counts.total`, the
rows you did not get are the farthest ones.

#### `SearchResult` fields

| Field | Type | Meaning |
|---|---|---|
| `reg_seg_id` | string | Id of the curb span; pass to `/api/segment/{reg_seg_id}`. |
| `geometry` | GeoJSON geometry | The span, in WGS-84. |
| `verdict` | `"legal"` \| `"illegal"` \| `"ambiguous"` \| `"no_data"` | Never collapse these into two colours (SPEC §11). |
| `reason` | string | One sentence explaining the verdict, generated by the engine. |
| `caveats` | string[] | Per-span warnings, always including the universal temporary-signage caveat. |
| `walk_min` | number | Straight-line walk estimate, minutes. |
| `money` | string \| null | Meter cost for the window. `null` = unknown, `"0.00"` = free. |
| `price_known` | boolean | `false` when the rate is missing or several meter zones cover the blockface. |
| `capacity_cars` | integer \| null | Cars the span holds at 22 ft each. |
| `confidence` | number | 0–1, the lower of parse confidence and snap confidence. |
| `signs` | `SignRef[]` | Raw sign text behind the verdict. Always shown in the UI. |
| `charged_minutes` | integer | Minutes of the window the meter is actually running. |
| `metered` | boolean | Whether any governing rule is metered. |
| `score` | number | Ranking score: the weighted sum of walk, money, and risk. |
| `rate_label` | string \| null | Meter zone label, when known. |

#### `SignRef` fields

`sign_id`, `order_number` (string \| null), `sign_code` (string \| null),
`sign_description` (string, **untrusted, escape on render**), `parse_method`
(`"grammar"` \| `"unparsed"` \| … \| null), `parse_confidence` (number \| null).

#### `sync`

`last_sync` (string \| null), `sign_count` (integer), `coverage_pct` (number \|
null). These are read from the `sync_meta` key/value table, which the ETL fills
with its own key names (`last_sync_at`, `signs_loaded`,
`blockface_sides_matched_share`, the last of which is a 0-1 share and is
multiplied by 100 here). A key the ETL stops writing comes back as `null` / `0`
rather than an error, so a half-built database does not break search. Use
`GET /api/sync-status` for the whole table.

---

## `GET /api/segment/{reg_seg_id}`

Everything behind one verdict, for the detail panel. This is the endpoint that
satisfies SPEC §10's mandatory "expose the literal sign text and the parse
method" requirement.

`reg_seg_id` must match `^[A-Za-z0-9:_.-]{1,80}$`. Anything else is a 400
`invalid_request` and never reaches SQL.

```json
{
  "segment": {
    "reg_seg_id": "3681:W:0",
    "segment_id": "3681",
    "street_name": "3 AVE",
    "side": "W",
    "start_ft": 0.0,
    "end_ft": 284.8,
    "length_ft": 284.8,
    "capacity_cars": 12,
    "capacity_approximate": true,
    "confidence": 0.94,
    "derived_from": ["9f2c…"]
  },
  "geometry": { "type": "LineString", "coordinates": [[-73.954561, 40.778149]] },
  "regulations": [
    {
      "reg_id": "3681:W:0:1",
      "raw_sign_description": "2 HOUR METERED PARKING 8:30AM-7PM EXCEPT SUNDAY",
      "parse_method": "grammar",
      "parse_confidence": 0.98,
      "regulation": {
        "action": "park",
        "permitted": true,
        "vehicle_class": "all",
        "exclusive": false,
        "days": [0, 1, 2, 3, 4, 5],
        "time_from": "08:30",
        "time_to": "19:00",
        "metered": true,
        "max_duration_min": 120,
        "flags": { "street_cleaning": false, "school_days": false, "except_sunday": true, "including_sunday": false, "snow_emergency": false, "holiday_exempt": false, "temporary": false, "meta": false },
        "effective_from": null,
        "effective_to": null,
        "arrow": "none"
      }
    }
  ],
  "signs": [
    {
      "sign_id": "9f2c…",
      "order_number": "1-11111",
      "sign_code": "PRK-9",
      "sign_description": "2 HOUR METERED PARKING 8:30AM-7PM EXCEPT SUNDAY",
      "on_street": "3 AVENUE",
      "from_street": "EAST 85 STREET",
      "to_street": "EAST 86 STREET",
      "side_of_street": "W",
      "distance_from_intersection": 44.0,
      "snap_confidence": 0.94,
      "snap_notes": "",
      "is_regulation": true,
      "panel_class": "regulation"
    }
  ],
  "meter_rates": [
    { "blockface_id": "100234:3681", "side": "W", "rate_label": "Zone M2", "hour_rates": ["5.00", "8.25"], "commercial_hour_rates": ["6.00", "9.00", "12.00"], "max_session_min": 120, "source": "parknyc", "confidence": 1.0 }
  ]
}
```

`regulation` is a serialized `curbcheck.model.Regulation`; `days` is a list of
weekday numbers with **Monday = 0** through Sunday = 6. `hour_rates` are money
strings, first hour then second hour; stays longer than the listed hours bill
at the last rate.

A `meter_rate` row's `source` is `parknyc` when it came from the ParkNYC
blockface join and `rate_zone` when it came from the citywide zone polygon,
which covers a metered blockface ParkNYC does not price (SPEC §13.1c, decision
D18). A zone row carries `confidence: 0.6`; quote it, but say where it came
from. `commercial_hour_rates` is what the meter charges commercial plates and
never applies to a passenger query.

`capacity_approximate` is always true in v1: hydrant, driveway, and crosswalk
setbacks are not in the data, so a car count is an upper bound (SPEC §8.5).
`signs` lists every sign on the parent centerline segment, including the
non-regulation panels decision D10 classifies out (`is_regulation: false`) —
they are kept visible for audit but produce no rule. `panel_class` is
`regulation` for a sign that states a rule, and otherwise the parser's own
`panel:<kind>` label (`panel:pay_by_cell`, `panel:mta_route`, `panel:location`,
`panel:template`, `panel:parking_geometry`, `panel:blank`,
`panel:supersedes_only`); treat it as an opaque string, not a closed set
(decision D19).

A rule with `parse_method: "unparsed"` carries a placeholder `regulation` whose
fields mean nothing — read only `raw_sign_description`, `parse_method`, and
`parse_confidence`, and show the raw text (decision D13).

---

## `GET /api/geocode?q=`

Local geocoder over the centerline address ranges. No network, no third-party
geocoder, so no address ever leaves the machine (threat T6).

`q` is 1–200 characters. Returns at most 5 candidates, best first. An
unparseable or unmatched query returns `{"query": …, "candidates": []}` with
status 200 — an empty result is an answer, not an error.

```json
{
  "query": "123 E 85 St",
  "candidates": [
    { "label": "123 E 85 ST", "lat": 40.778455, "lon": -73.956201, "kind": "address", "confidence": 0.9 },
    { "label": "E 85 ST & LEXINGTON AVE", "lat": 40.778901, "lon": -73.956998, "kind": "intersection", "confidence": 0.5 }
  ]
}
```

Accepted forms: `123 E 85 St`, `123 East 85th Street`, `1500 3rd Ave`,
`Lexington Ave & 86th St`, `E 86 St and 3 Ave`, and a bare street name.

`kind` is `"address"` (interpolated within a house-number range) or
`"intersection"` (a centerline node, or a fallback). `confidence` is 0–1;
see the coverage limits in `curbcheck/geocode.py`'s module docstring — only
54% of Manhattan centerline segments publish address ranges, so many house
numbers resolve only to the nearest hundred-block corner at confidence ≈ 0.5.

---

## `GET /api/health`

Answers even with no database. `status` is `"ok"` when the database is present
and readable, `"degraded"` otherwise.

```json
{ "status": "ok", "db_present": true, "db_readonly": true, "sign_count": 74590 }
```

`db_readonly` reports that the server opened the file with SQLite's `mode=ro`
URI, i.e. that a bug in a request handler cannot write to it. It is `false`
only when there is no database to open.

## `GET /api/sync-status`

The `sync_meta` table as a flat JSON object. Values are returned as the strings
they are stored as; they are never parsed, evaluated, or coerced. Keys are
whatever the ETL wrote.

```json
{
  "last_sync_at": "2026-09-15T03:18:02+00:00",
  "signs_loaded": "74389",
  "signs_snapped_share": "0.95",
  "blockface_sides_matched_share": "0.9378",
  "street_segments": "11102"
}
```

Returns 503 when the database is missing.

---

## Static routes

| Path | Serves |
|---|---|
| `/` | `web/index.html` |
| `/*` | anything under `web/`, including `web/vendor/` and `web/basemap/` (fonts, sprites, style JSON) |
| `/basemap/manhattan.pmtiles` | the PMTiles archive, **with HTTP Range support** |

The PMTiles route answers `Range` requests with `206 Partial Content`, a
`Content-Range: bytes start-end/total` header, and `Accept-Ranges: bytes`,
which is how pmtiles.js reads the archive header and individual tiles without
downloading 23 MB. Starlette 1.6.0's `FileResponse` implements RFC 7233 ranges
including multipart and `416`, so the route is a plain `FileResponse` rather
than a hand-rolled one; there is a test that pins that behaviour so a Starlette
upgrade that drops it fails loudly.

When the basemap file is absent the route returns 404 `not_found`; the map
should degrade to no basemap rather than break.

## Notes for the frontend

- Poll nothing. Every endpoint is a one-shot request.
- `/api/*` responses are `Cache-Control: no-store`; do not add your own cache.
- Treat `verdict` as a closed set of four values but render an unknown value as
  `no_data` rather than crashing.
- `money` and `hour_rates` are strings. Format them, do not `parseFloat` them
  into arithmetic.
- Show `disclaimer` persistently and `caveats` next to the result list
  (SPEC §11 and §17 make both mandatory).
