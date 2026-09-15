# CurbCheck local API

The server (`curbcheck serve`) binds `127.0.0.1:8765` and serves two things:
the static frontend under `/`, and this JSON API under `/api`. There is no
CORS middleware and there never will be — the UI is same-origin by design
(SPEC §3.4), so a page from any other origin cannot read these responses.

Every field name below matches the Python dataclass it comes from
(`curbcheck.engine.search.SearchResult`, `SignRef`,
`curbcheck.engine.signs.SignDetail`, `curbcheck.geocode.GeocodeCandidate`,
`ReverseMatch`, `curbcheck.model.Regulation`). They are not renamed on the way
out.

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
| 422 | `validation_error` | Body or query failed Pydantic validation: unknown field, out-of-range value, `t2 <= t1`, coordinates outside the input bounding box. |
| 422 | `outside_coverage` | The destination resolved to a point more than 250 m from every street centerline. Message: "That location is outside Manhattan, the only area CurbCheck covers." Never advise a longer walk for this — the radius is not the problem (`docs/DECISIONS.md` D28). |
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

The lat/lon bounds are a Manhattan-ish bounding box, and they are only an input
sanity check. The service area is a separate, narrower test: a destination
further than 250 m from every street centerline is refused with 422
`outside_coverage` before any search runs, and `GET /api/health` publishes the
area and its bounding box (`docs/DECISIONS.md` D28). A point inside the input
box but outside coverage — the Hudson, Long Island City — is an error, not an
empty result.

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
      "basis": "posted",
      "confidence_shown": true,
      "reason": "metered parking permitted",
      "caveats": [
        "Temporary or construction signage may override what is shown here. The posted sign at the curb is the only authoritative regulation."
      ],
      "walk_min": 1.4,
      "money": "9.00",
      "money_value": 9.0,
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
      "risk": 1.95,
      "rate_label": "Area 1",
      "street_name": "3 AVENUE, west side, E 85 ST → E 86 ST",
      "gap_kind": null
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
`limit` of them, ordered by ascending `score` — except that spans whose `score`
differs by less than 0.5 are ordered `basis: "posted"` before
`basis: "absence"`, so a stretch with a sign to read outranks one where nothing
is posted at the same cost (`docs/DECISIONS.md` D27); then every other verdict within
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
| `reg_seg_id` | string | Id of the curb span; pass to `/api/segment/{reg_seg_id}`. Derived from the centerline segment, the curb, and the span's extent, so it moves whenever the span rules change — never persist one. |
| `geometry` | GeoJSON geometry | The span, in WGS-84. |
| `verdict` | `"legal"` \| `"illegal"` \| `"ambiguous"` \| `"no_data"` | Never collapse these into two colours (SPEC §11). |
| `basis` | `"posted"` \| `"absence"` \| null | Why a `legal` verdict is legal. `posted`: at least one rule in the stack permits parking somewhere in the window. `absence`: no rule is in force for any part of it, which 34 RCNY 4-08 makes legal but which is the engine reporting that it read *nothing*. `null` on every non-legal verdict. Render the two differently: an absence verdict is not a permission, and it was being drawn with the same green badge and the same confidence percentage as one (`docs/DECISIONS.md` D27). |
| `confidence_shown` | boolean | Whether `confidence` means anything on this span. `false` on `basis: "absence"` (the confidence of an empty stack) and on `no_data` (0 next to a reason that says nothing was read). Do not print a percentage where it is false; the number is still there for the audit block. |
| `reason` | string | One sentence explaining the verdict, generated by the engine. `"No posted rule is in effect during this window"` exactly when `basis` is `"absence"`. On `no_data` it follows `gap_kind`: `"NYC DOT lists no signs on this stretch"` for `no_signs`, `"Signs exist here that CurbCheck could not place"` for `unmatched_signs`, and `"No sign data for this stretch"` when the span says neither. Print it as it arrives; a client that rewrites it makes the UI and the API disagree about the same curb. An `ambiguous` span whose reason mentions conflicting signs is one a prohibitive span on the same centerline side overlaps: the two posts disagree about where the boundary is, and the engine will not call either stretch legal (SPEC §8.6). The ETL writes no such pair since `docs/DECISIONS.md` D25 and D26, so this reason should not appear on a current database. |
| `caveats` | string[] | Per-span warnings, always including the universal temporary-signage caveat. Every entry is a sentence — leading capital, closing full stop — so it can be printed verbatim in a list. A `no_data` span carries the caveat that matches its `gap_kind`. |
| `walk_min` | number | Straight-line walk estimate, minutes. |
| `money` | string \| null | Meter cost for the window. `null` = unknown, `"0.00"` = free. Always `null` on a `no_data` verdict and on a placeholder span (`gap_kind` set): unpriced curb is not free curb, and "$0.00"/"no meter" on a grey span asserts what the data never said. |
| `money_value` | number \| null | The numeric twin of `money`, for arithmetic only — ranking, sorting, a re-ranked score. `null` whenever `money` is. Display `money`, never this: it is a float and money is a decimal. |
| `price_known` | boolean | `false` when the rate is missing, several meter zones cover the blockface, or the span is `no_data`/a placeholder. |
| `capacity_cars` | integer \| null | Cars the span holds at 22 ft each. |
| `confidence` | number | 0–1, the lower of parse confidence and snap confidence. |
| `signs` | `SignRef[]` | Raw sign text behind the verdict. Always shown in the UI. |
| `charged_minutes` | integer | Minutes of the window the meter is actually running. |
| `metered` | boolean | Whether any governing rule is metered. |
| `risk` | number | Expected fine in dollars, 2 dp: how likely a ticket is if the software misread this span, times the standard $65 fine. The third term of `score`. |
| `score` | number | Ranking score under the weights in the request: `w_walk*walk_min + w_money*money_value + w_risk*risk`, with an unknown price counted as 0. The three terms are all in the response, so a client can re-rank locally when the sliders move without a new request. |
| `rate_label` | string \| null | Meter zone label, when known. |
| `street_name` | string \| null | A human label for the span: the centerline's street name, the side, and the cross streets at the ends of the centerline segment the span lies on — `"3 AVENUE, west side, E 85 ST → E 86 ST"`. Names are as CSCL stores them (capitals) and are **untrusted**, like every other text column. `null` when the span has no centerline segment to name it from. Render this on the card; do not fetch `/api/segment` for a label. |
| `gap_kind` | `"no_signs"` \| `"unmatched_signs"` \| null | Only on a `no_data` span, and only when the database records why it is empty. `no_signs`: DOT's inventory lists no sign on this centerline side. `unmatched_signs`: DOT does publish signs here and none could be placed on the centerline. The two need different words — 332 of the 499 unmatched blockface-sides carry a NO STANDING/PARKING/STOPPING ANYTIME sign on the 2026-09-15 snapshot, so "no signs here" would be false about them (`docs/VALIDATION.md` §5). `null` on a real span, and on any database built before the column existed. |

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
    "derived_from": ["9f2c…"],
    "gap_kind": null
  },
  "geometry": { "type": "LineString", "coordinates": [[-73.954561, 40.778149]] },
  "regulations": [
    {
      "reg_id": "3681:W:0:1",
      "sign_id": "9f2c…",
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
  "governing": [
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
      "distance_ft": 44.0,
      "arrow": null,
      "snap_confidence": 0.94,
      "snap_notes": "",
      "is_regulation": true,
      "panel_class": "regulation"
    }
  ],
  "other_on_block": [
    {
      "sign_id": "4b81…",
      "order_number": "1-11112",
      "sign_code": "PS-2G",
      "sign_description": "NO STANDING ANYTIME",
      "on_street": "3 AVENUE",
      "from_street": "EAST 85 STREET",
      "to_street": "EAST 86 STREET",
      "side_of_street": "W",
      "distance_from_intersection": 232.0,
      "distance_ft": 232.0,
      "arrow": "North",
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

`gap_kind` on the `segment` object is the same value the search result carries:
null on a real span, `no_signs` or `unmatched_signs` on a placeholder. A
placeholder has no `governing` signs and no `regulations`.

`segment_id` names the one centerline segment the span lies on — a span never
crosses a segment boundary — and `start_ft`/`end_ft` are measured along that
segment from its own start, in the direction CSCL digitized it, which is not
necessarily the corner DOT measures a sign's `distance_from_intersection` from
(`docs/DECISIONS.md` D26). `side` is the compass letter DOT letters the curb
with; the stack behind it is keyed on which side of the segment's own direction
that curb is, so two DOT blockfaces over one piece of curb reach you as one row.

`capacity_approximate` is always true in v1: hydrant, driveway, and crosswalk
setbacks are not in the data, so a car count is an upper bound (SPEC §8.5).

**`governing` and `other_on_block`** are the same sign rows in two groups, each
ordered along the curb by `distance_from_intersection` (unmeasured last).

- `governing` is the span's own `derived_from` set: the posts whose text became
  its rules, and the only signs that produced this verdict. Empty on a
  placeholder span (`gap_kind` set), which has no rules at all.
- `other_on_block` is every other sign DOT posts on the same blockface-side —
  the same `(on_street, from_street, to_street, side_of_street)` name tuple as a
  governing sign. These do **not** govern the stretch: 9 of the 11 signs the old
  single list showed under the audited green verdict did not, and the first of
  them read `NO STANDING ANYTIME` (`docs/ux/UX_AUDIT.md` P0-2). Show them, say
  they do not govern, and collapse them if you like — but never drop them
  (SPEC §10). When a span has no governing sign to take a name tuple from, this
  is every sign snapped to the parent centerline segment on the same side
  letter. On a `gap_kind: "unmatched_signs"` placeholder it also lists the signs
  DOT publishes for that blockface that could never be placed on the centerline;
  332 of the 499 unmatched blockface-sides carry a NO STANDING/PARKING/STOPPING
  ANYTIME sign (`docs/VALIDATION.md` §5), so the grey state is not an empty one.

Both groups include the non-regulation panels decision D10 classifies out
(`is_regulation: false`) — kept visible for audit, producing no rule.
`distance_ft` is `distance_from_intersection` under a name that states DOT's
unit; both are returned, and they are always equal. `arrow` is DOT's own
`arrow_direction` compass word for the arrow on the post — `"North"`,
`"South"`, `"East"` or `"West"` — and null on the 73.5% of signs that carry no
arrow — which way that points *along this curb* is the
resolved `arrow` on each rule, not this one. `panel_class` is
`regulation` for a sign that states a rule, and otherwise the parser's own
`panel:<kind>` label (`panel:pay_by_cell`, `panel:mta_route`, `panel:location`,
`panel:template`, `panel:parking_geometry`, `panel:blank`,
`panel:supersedes_only`); treat it as an opaque string, not a closed set
(decision D19).

Each rule carries the `sign_id` it was read from, so the UI can file rules under
their post. A rule is tied to its sign by the raw description — the parser reads
each distinct description once — so two posts on one span carrying identical
text collapse to one rule named after the first of them in curb order.
`sign_id` is null when no sign in `governing` carries that description.

A rule with `parse_method: "unparsed"` carries a placeholder `regulation` whose
fields mean nothing — read only `raw_sign_description`, `parse_method`, and
`parse_confidence`, and show the raw text (decision D13).

---

## `GET /api/geocode?q=`

Address suggestions from the local index. No network, no third-party geocoder,
so no address ever leaves the machine — and because a suggestion list fires on
every keystroke, that matters more here than anywhere else in the API (threat
T6). `docs/ux/AUTOCOMPLETE_RESEARCH.md` §3 is the survey of the online options
and why none of them is offered, not even opt-in.

`q` is 1–120 characters. Returns at most **8** candidates, best first. An
unparseable or unmatched query returns `{"query": …, "candidates": []}` with
status 200 — an empty result is an answer, not an error. A candidate outside
coverage is never returned, so anything in this list can be searched
(`docs/DECISIONS.md` D28).

There is **no session token and no cookie**. Session tokens exist so a vendor
can bill a keystroke sequence as one geocode; there is no vendor, so the
endpoint is a pure function of `q` with nothing tying two requests together.
Every response carries `Cache-Control: no-store`.

```json
{
  "query": "350 5th",
  "candidates": [
    {
      "label": "350 5 AVE",
      "secondary": "Manhattan 10118",
      "lat": 40.748377,
      "lon": -73.984854,
      "kind": "address",
      "confidence": 0.98
    },
    {
      "label": "350A 5 AVE",
      "secondary": "Manhattan 10118",
      "lat": 40.74817,
      "lon": -73.985005,
      "kind": "address",
      "confidence": 0.98
    },
    {
      "label": "near 347 E 5 ST",
      "secondary": "Manhattan",
      "lat": 40.725992,
      "lon": -73.986816,
      "kind": "address",
      "confidence": 0.6
    }
  ]
}
```

A building with several surveyed doors is several candidates rather than one,
because AddressPoint files them separately and they are up to 100 m apart.

Accepted forms, all measured (`docs/ux/AUTOCOMPLETE_RESEARCH.md` §2.3):

| typed | answers |
|---|---|
| `1519 3rd ave`, `1519 third avenue`, `1519 3rd av` | `1519 3 AVE` |
| `e 86th st and 3rd`, `86 & 3`, `lex & 86`, `fdr dr & 96` | the corner |
| `86th st` | `E 86 ST` and `W 86 ST`, because the side is not stated |
| `10021` | the centre of that ZIP |
| `bryant park`, `one world trade`, `1 police plaza` | the place |
| `broadwa` | every street that spelling is a prefix of |
| `w 4 st and bleeker` | the typo is corrected; the two streets are offered, because CSCL has no node where they meet |

`kind` is one of `address`, `intersection`, `street`, `zip`, `place`, `pin`.
This endpoint produces the first five; `pin` is what the frontend calls a
crosshair the user has not dropped yet. Treat an unknown value as `place`.

`confidence` is 0–1 and says **how** the point was found, which is the only
honest thing to rank on:

| confidence | `kind` | what it is |
|---|---|---|
| 0.98 | `address` | a surveyed door from OTI AddressPoint |
| 0.95 | `intersection` | a centerline node both streets meet at |
| ≤ 0.85 | `place` | a CommonPlace name, scaled by how much of it the query accounted for |
| 0.75 | `address` | placed between two surveyed same-parity neighbours; `secondary` names them |
| 0.70 | `street` | the query is exactly this street's name |
| 0.60 | `address` | `near <the closest surveyed number>` |
| 0.50 | `address` | interpolated along CSCL's own published range, for one of the 233 streets with no surveyed door |
| 0.45 / 0.30 | `street` | reached by prefix, or offered as half of a corner that does not exist |
| 0.25 | `zip` | the mean of a ZIP's doors |

A fuzzy street match — an edit-distance-1 correction of a typo — multiplies the
row's confidence by 0.8. It only runs when the exact and prefix passes found
nothing, and never on a street fragment shorter than four characters, where
almost every spelling is within one edit of almost every other.

`secondary` is the muted second line and is `string | null` in the contract,
though no route emits `null` today: `"Manhattan 10028"` for a surveyed door,
`"Manhattan · between 1517 and 1529"` for an interpolated one,
`"Manhattan · ZIP centre of 1732 addresses"` for a ZIP, the block's cross
streets for the centerline-range rung, and `"Manhattan"` when there is nothing
narrower to say.

---

## `GET /api/reverse?lat=&lon=`

What a dropped pin is nearest to, so the UI can echo a place rather than
`40.778830, -73.953985` (`docs/ux/UX_AUDIT.md` P1-4).

`lat` is 40.68–40.90 and `lon` is -74.05 – -73.88, both required; anything else
is 422 `validation_error`. A point outside coverage is 422 `outside_coverage`.

```json
{
  "label": "near 1519 3 AVE",
  "secondary": "Manhattan",
  "kind": "address",
  "lat": 40.778402,
  "lon": -73.955249,
  "distance_m": 12.4
}
```

`label`, `secondary` and `kind` mean what they do on a geocode candidate. The
answer is the nearest surveyed door when one is within **60 m** of the pin,
otherwise the nearest centerline node (`kind: "intersection"`), otherwise the
street the pin is on (`kind: "street"`). A pin with nothing within 250 m — the
middle of the Hudson — is 404 `not_found`. `lat`/`lon` are the returned place,
not the pin, and `distance_m` is how far the pin is from it, to one decimal.
The label says "near" because it names the closest door, not the building the
pin is on.

---

## `GET /api/health`

Answers even with no database. `status` is `"ok"` only when the database is
present, readable, **and** has an ASP/holiday calendar; otherwise `"degraded"`.

```json
{
  "status": "ok",
  "db_present": true,
  "db_readonly": true,
  "sign_count": 74590,
  "calendar_missing": false,
  "coverage": {
    "area": "Manhattan",
    "bbox": [-74.046770, 40.684050, -73.906821, 40.879046]
  }
}
```

`coverage` is the area this database can answer about: `area` is the name to
put in front of a user, and `bbox` is `[min_lon, min_lat, max_lon, max_lat]`
over every `street_segment` row, which is the edge of what CurbCheck knows and
what the map should outline. It is `null` when there is no readable database.
The box is the *data's*, not the borough's — Roosevelt and Randalls Islands are
inside it because CSCL files them under Manhattan — and being inside it is
necessary but not sufficient: the service-area test is 250 m from a centerline
(`docs/DECISIONS.md` D28).

`calendar_missing` is true when `sync_meta.calendar_missing` is set or
`asp_suspension` is empty. Such a database answers every query and gets every
holiday and street-cleaning suspension wrong while doing it, so it is
`degraded` rather than `ok`, and every `SearchResult` from it carries the
caveat "Holiday and street-cleaning suspension calendar is missing; holiday and
ASP verdicts may be wrong." (SPEC §11).

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
