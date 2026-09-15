# Address autocomplete: options, evidence, and a plan

What it would take to give CurbCheck a Google-Maps-feel suggestion list, at
zero cost, without breaking threats T2 (allowlisted egress) or T6 (no address
egress). Answers `docs/ux/UX_AUDIT.md` P1-4 and P1-5, which ask for candidates
to always be offered and for the resolved address to be shown honestly.

Everything here was measured on 2026-09-15 with `scripts/explore_addresses.py`.

## Recommendation

**Build it locally. Add OTI's `AddressPoint` (`uf93-f8nk`) and `CommonPlace`
(`t95h-5fsr`) to the ETL and serve suggestions from SQLite. Take no online
option, not even opt-in.**

1. **It is more accurate than what we have, not just faster.** Address points
   are surveyed coordinates. Over 800 random Manhattan address points, the
   current centerline interpolation lands a **median 95 ft** from the real door
   (p90 295 ft, p95 1,634 ft, worst 45,832 ft); only 52.5% are within 100 ft
   and 7.3% are more than 500 ft out. The prototype's median error is **0 ft**,
   99.8% within 100 ft, worst 373 ft.
2. **Every online option costs privacy per keystroke.** Autocomplete sends a
   prefix of the destination on every keypress. That is threat T6, and it is
   worse than a single geocode call because it leaks the typing, not just the
   answer. No free tier changes that.
3. **The local option is invisible-fast.** Median **0.20 ms** per query, worst
   4.9 ms, on a 12.4 MB index. The 20 ms target is not close.

One correction to make along the way: the "54% of segments publish address
ranges" line in `curbcheck/geocode.py`, `docs/API.md` and `docs/DATA.md` §2.2
is true but reads as a coverage number, and it is not one. The geocoder matches
a house number against *every* segment of the street, so **98.6%** of real
Manhattan addresses already fall inside some published range. The 46% is an
accuracy problem, and address points are what fix it.

---

## 1. The local dataset

### 1.1 Candidates

From `api.us.socrata.com/api/catalog/v1?domains=data.cityofnewyork.us&q=address+points`
(81 results), accessed 2026-09-15.

| Dataset | Id | Type | Verdict |
|---|---|---|---|
| **AddressPoint** (OTI) | `uf93-f8nk` | dataset | **Chosen.** Per-door points, SoQL-pageable. (`6xyb-j5pk` is the same data as a map view.) |
| **CommonPlace** (OTI) | `t95h-5fsr` | dataset | **Chosen** for POI names: 5,827 Manhattan `feature_name` values, 2.8 MB, weekly. |
| DCP PAD | `bc8t-ecyu` | `file`, `application/zip` | **Rejected.** A zip of two ASCII files, not a SoQL resource — it cannot be paged the way `net.py` pages everything else, and unpacking a network archive is the untrusted-binary handling SPEC §3.3 says to avoid when a JSON alternative exists. |
| PLUTO | `64uk-42ks` | dataset | **Rejected as primary.** 42,504 Manhattan tax lots, one `address`/`latitude`/`longitude`/`bbl` each: coarser than 63,245 doors, and no alias names. |
| LION | `2v4z-66xt` | file (geodatabase) | Already rejected by D1. |

### 1.2 `uf93-f8nk` AddressPoint

- **Publisher:** NYC Office of Technology and Innovation, marked `official`.
  Socrata reports `license: null`; published under NYC's Open Data programme
  (Local Law 11 of 2012) with the general nyc.gov terms and no explicit open
  licence — the same footing as the five datasets we already ingest.
- **Cadence:** `Update Frequency: Weekly`, `Automation: No`. Our weekly sync
  (SPEC §5.7) covers it.
- **Size:** 967,871 rows citywide; **63,245 with `boroughcode='1'`**, 32.8 MB of
  JSON, fetched in two 50k pages by exactly the `socrata_fetch_all` paging in
  `net.py`. Snapshot at `data/raw/address_points_manhattan.json` with a
  `.meta.json` sidecar (sha256 `9d28046101e9…`).
- **Fields**, population over the Manhattan subset: `the_geom` 100% (GeoJSON
  `Point` in WGS-84 — no EPSG:2263 conversion needed), `house_number` 100%
  (text; 40 hyphenated rows, none in the grid), `full_street_name` 100%
  (`"W  48 ST"` — CSCL's own double-spaced abbreviations), `boroughcode` 100%,
  `zipcode` 100.0% (91 ZIPs, 2 nulls), `bin` 100.0%, `street_name` 100% with
  `pre_directional` 53.6% / `post_type` 93.7% as the parts,
  `house_number_suffix` 3.8% (`7A JANE ST`), `house_number_range` 4.9%
  (`557–559 BROADWAY`), and coded quality flags `validation`, `address_status`
  15.2%, `address_source`, `collectionmethod`, `sosindicator` (side of street,
  31,663/31,582), `special_condition`. **No BBL**, and `b7sc_vanity` (431 rows)
  is a street code, **not** a readable alias — no place names live here.
- **Junk:** none material. Zero rows outside the Manhattan bounding box, zero
  missing geometry. 1,635 duplicate `(house_number, full_street_name)` keys over
  2,257 extra rows are multiple doors of one building, offered as separate
  candidates rather than deduplicated.

### 1.3 Do our normalizers already unify the names?

Yes. AddressPoint has 872 distinct `full_street_name` values; after
`normalize_street_name`, **785 (90.0%) match a `street_segment.street_norm`
exactly**, covering **99.23% of rows**. The 87 unmatched names cover 486 rows
and are mostly things the centerline has no segment for: `GOVERNORS ISLAND`
(167), `STUYVESANT OVAL` (21), `POMANDER WALK` (16), `PENN PLZ`, `HUDSON YARDS`,
`TIMES SQ`, `ROCKEFELLER PLZ`, `WORLD TRADE CENTER`, `COENTIES SLIP`. One is a
real alias worth adding to `NAME_ALIASES`: `DR M L KING JR BLVD` (43 rows) is
CSCL's `W 125 ST`.

### 1.4 What address points add, and what they do not

- **The 46% gap is mostly already covered.** Of 60,965 distinct
  `(house number, street)` pairs in AddressPoint, 98.6% already fall inside a
  published centerline range; 1.4% (867) are new, and 449 sit on a street with
  no range anywhere. The win is accuracy, per the Recommendation's numbers.
- **Address points are not a complete door list.** Of the 250,096 same-parity
  house numbers implied by the centerline ranges, only 61,070 (24.4%) have an
  address point — `1519 3 AVE` is absent while 1517 and 1529 are present. That
  is why the prototype keeps an interpolation rung.
- **Place names come from CommonPlace:** `BRYANT PARK`, `1 WORLD TRADE CENTER`,
  `ONE POLICE PLAZA HELIPORT`, `3 WORLD TRADE CENTER`.

---

## 2. The prototype

`scripts/explore_addresses.py` — `fetch`, `profile`, `build`, `demo`, `bench`,
`accuracy`, `query`. Index build and query path are stdlib + `sqlite3`; the only
package import is `normalize_street_name`, so user text is folded exactly the
way the ETL folded the data.

### 2.1 A vocabulary index, not a text index

The hard part of a Manhattan query is the street name; the house number is an
integer and the grammar (`<n> <street>`, `<street> & <street>`, ZIP, place) is
tiny. The build expands each street into a few spellings — canonical
(`E 86 ST`), suffix-dropped (`E 86`), directional-dropped (`86 ST`, `86`,
`HOUSTON`), plus a nickname table (`LEX`, `CPW`, `FDR`, `BWAY`) — into a
2,816-row `street_variant` table. The query parses first and looks up second:
one prefix range-scan resolves the street, an index seek does the rest.

Tolerance falls out of that. `1519 third avenue` and `1519 3rd ave` fold to one
key (`WORD_FORMS` already handles `THIRD`→`3`; the script adds digit ordinals and
punctuation); `86 & 3`, `lex & 86` and `e 86th st and 3rd` hit the same node;
`&`, `and`, `at`, `@`, `/` all split. Typos run an edit-distance-1 scan over the
2,816 variants, but **only after** the exact and prefix passes come back empty —
that ordering is what keeps the common case under a millisecond.

**Why not FTS5.** It is available (SQLite 3.53.4; `trigram` and `unicode61` both
compile) and fast: a trigram index over the 63,245 address strings is 6.6 MB and
answers in 0.03–0.85 ms. It is still wrong here. It matches literal text, so
every variant (`THIRD AVENUE`, `86`, `LEX`) would have to be pre-expanded into
the indexed string anyway — the same work, done twice — and it cannot express
"these two streets meet at a node". It also adds a query language the user types
into: `MATCH` syntax must be escaped or a stray `"` or `*` becomes an error or a
wildcard scan. A covering index has no syntax to escape (T3).

### 2.2 Index contents — 12.4 MB

`address` 63,245 · `intersection` 5,645 pairs stored both ways, from 4,898
multi-name `street_node` rows · `street` 1,017 · `street_variant` 2,816 over
1,105 streets · `place` 5,817 with 23,659 tokens (10 names that only repeat a
street were dropped) · `zipcode` 91 centroids. Includes covering indexes on
`(street_norm, house, …)` and `(lon, lat, …)`. Against today's 79 MB
`curbcheck.sqlite` that is **+16%**.

### 2.3 Fifteen queries, top three each

```
'1519 3rd ave'        address      0.75  1519 3 AVE       (interpolated between 1517 and 1529)
'1519 third avenue'   address      0.75  1519 3 AVE
'1519 3rd av'         address      0.75  1519 3 AVE
'e 86th st and 3rd'   intersection 0.95  3 AVE & E  86 ST
'86 & 3'              intersection 0.95  3 AVE & E  86 ST
'lex & 86'            intersection 0.95  E  86 ST & LEXINGTON AVE
'86th st'             street       0.45  E  86 ST / W  86 ST / 86 ST TRANSVERSE
'10021'               zip          0.25  10021 (ZIP centre, 1732 addresses)
'w 4 st and bleeker'  street       0.30  W  4 ST   (typo fixed: BLEECKER ST at 0.24)
'350 5th'             address      0.98  350 5 AVE / 350A 5 AVE / 350B 5 AVE
'one world trade'     place        0.64  1 WORLD TRADE CENTER
'bryant park'         place        0.85  BRYANT PARK / FIVE BRYANT PARK / FOUR BRYANT PARK
'broadwa'             street       0.45  BROADWAY / BROADWAY ALY / BROADWAY BRG
'fdr dr & 96'         intersection 0.95  E  96 ST & FRANKLIN D ROOSEVELT DR
'1 police plaza'      address      0.98  1 POLICE PLZ
```

Two honest failures are visible. `w 4 st and bleeker` corrects the typo but
finds no corner, because W 4 St and Bleecker St share no centerline node in
CSCL; it degrades to the two streets rather than inventing a corner. `86th st`
cannot know which side of Fifth Avenue you mean, so it offers both.

**Ladder:** `address` 0.98 surveyed · `intersection` 0.95 node · `place` 0.85 ×
name coverage · `address` 0.75 interpolated between two same-parity neighbours
inside one hundred-block · `address` 0.60 "near \<nearest door\>" · `street`
0.45 named outright, 0.30 as half of a failed intersection · `zip` 0.25. A fuzzy
street match multiplies by 0.8.

### 2.4 Latency — 20 queries, 50 runs each, median of each

| Store | median | mean | max |
|---|---|---|---|
| Local disk | **0.20 ms** | 0.53 ms | 4.90 ms |
| `data/` (9p bind mount in this container) | 17.8 ms | 24.4 ms | 59.4 ms |

The 200× gap is the container's Windows 9p mount, where one SQLite page read
costs 2.5 ms instead of 12 µs — an artifact of the dev environment, not of the
index. Worth knowing anyway: it is the same mount `curbcheck.sqlite` sits on,
and it is why `/api/geocode` takes ~190 ms per call here. Reverse lookup is
1.1 ms local, 17.7 ms mounted.

---

## 3. Online options

Docs read 2026-09-15. Nothing was signed up for; no address was sent anywhere.

| Service | Cost / key | Rate limit | ToS red flags | Privacy consequence |
|---|---|---|---|---|
| **NYC Planning Labs GeoSearch** `geosearch.planninglabs.nyc/v2/autocomplete?text=` | Free, no key | None published | **No terms of service, no acceptable-use policy, no rate limit, no SLA** — not on the service page, not in `/docs`, not in `NYCPlanning/labs-geosearch-docs`. The only operational advice is "throttle requests when using the autocomplete endpoint". | Every keystroke of the destination leaves the machine. Host is not on the §3.2 allowlist. |
| **NYC GeoClient** `api.nyc.gov` via `api-portal.nyc.gov` | Free, **key required** | 2,500 req/min, 500,000/day | Azure APIM subscription; every request attributed to a named developer. Not an autocomplete API. | Same egress, now tied to an identity. |
| **OSM Nominatim** | Free, no key | "absolute maximum of 1 request per second" | **Explicitly forbidden:** "Auto-complete search — This is not yet supported by Nominatim and you must not implement such a service on the client side using the API." Also demands a `User-Agent`/`Referer` and result caching. | Moot; using it would violate the policy. |
| **Photon** `photon.komoot.io` | Free, no key | "please be fair — extensive usage will be throttled" | "We do not guarantee for the availability and usage might be subject of change in the future." No terms. | Keystrokes to a third party abroad. |
| **Pelias self-hosted** | Free (compute) | n/a | None — it is ours. | None. |
| **OpenCage** | Free trial, key | 2,500/day, 1 req/s | Free tier is "testing only"; autocomplete is a separate paid product. | Keystrokes to a vendor under a commercial account. |
| **Geoapify** | Free, key | 3,000 credits/day, 5 req/s | "Limited Commercial Use"; requires a visible "Powered by Geoapify" credit. | Same. |
| **Mapbox** | 100,000 temporary geocodes/mo, key | — | **Temporary geocoding forbids storing the result**, and we must store the destination to run a search. Permanent geocoding has no free tier ($5.00/1,000). | Same, plus a billing identity. |
| **Google Places / Geocoding** | Key + billing account | — | Lat/lng may be cached "for up to 30 consecutive calendar days", then must be deleted; results must not be used "in conjunction with a non-Google map" — ours is MapLibre + Protomaps (SPEC §16). Session tokens exist to bill per keystroke-session. | Worst case: keystrokes to Google, tied to billing. |

**Verdict: no online fallback, not even opt-in and off by default.**

The closest call is GeoSearch: same data lineage as ours (PAD via Pelias), free,
no key, run by the same city that publishes our inputs. It still fails twice. It
publishes no terms at all, and SPEC §3.2 makes the allowlist a config constant
precisely so that "it probably works" is not a reason to add a host. And it
cannot be made safe by a toggle — an autocomplete fallback firing on every
keystroke turns the search box into a continuous egress channel, and T6 names
"search history" as the thing not to transmit.

**Pelias self-hosted** is the only option with no privacy cost, and it loses on
weight: Elasticsearch 7.5+/8, Node 22, and libpostal's data alone is ~4 GB, to
replace a 12.4 MB SQLite table that is already faster — plus six more services
to keep patched, a strictly worse T1 position.

If some future need ever justifies one of these, the route is fixed: a new host
in `config.ALLOWED_HOSTS`, the call through `net.py`, default off. Nothing else
may open a socket (`tests/test_boundaries.py`).

---

## 4. Pin fallback

When nothing matches, the answer is a pin, not a sentence.

- The no-results state shows a **"Drop a pin instead" button**, not prose
  (UX_AUDIT P1-5), putting the map into crosshair mode.
- A dropped pin is **reverse-geocoded against the local index** — what
  `GeocodeKind.PIN` and `ReverseMatch` in `curbcheck/geocode.py` are already
  reserved for. The label is `near <nearest address point>` when a door is
  within 150 ft, else the nearest intersection, else the street. Never a bare
  coordinate.
- That label is what the results header echoes, which is the other half of
  P1-4: "Searching near 1519 3rd Ave" reads the same whether it was typed or
  dropped, with the match confidence beside it.
- Out-of-coverage gets its own message, not an empty result (P0-3): "CurbCheck
  only has data for Manhattan", never "try a longer walk".
- Mechanically: a bounding-box prefilter (~1,100 ft, widened twice if empty)
  over the `(lon, lat)` covering index, then exact nearest. 1.1 ms.

---

## 5. Implementation plan

### 5.1 ETL

1. `etl/fetch.py`: two `Dataset` entries — `("address_points_manhattan",
   "uf93-f8nk", "boroughcode='1'")` and `("common_places_manhattan",
   "t95h-5fsr", "boroughcode='1'")`. No new network code: `socrata_fetch_all`
   already pages, `data.cityofnewyork.us` is already allowlisted, and the
   row-count drift check applies unchanged.
2. `etl/stage.py`: two Pydantic row models in the existing pattern.
   `house_number` stays text in the raw model; the integer is derived.
3. New `etl/addresses.py`: fold `full_street_name` with
   `normalize_street_name`, derive the integer house number, drop rows with no
   geometry, write `address`, `place`, `place_token`, `zipcode` and
   `street_variant`. Variants are generated from `street_segment.street_norm`
   plus a `NICKNAMES` constant. Add `DR M L KING JR BLVD → W 125 ST` to
   `NAME_ALIASES`.
4. `etl/build.py`: run it after the geometry step (it reads `street_segment`
   and `street_node`) and record counts in `sync_meta`.
5. **DB growth: +12.4 MB on 79 MB (+16%).**

### 5.2 API

`GET /api/geocode?q=` stays the one endpoint and grows suggestions.

- `q` is 1–200 chars (`MAX_QUERY_CHARS`, unchanged, enforced by the existing
  `Query(min_length=1, max_length=…)`); **at most 8** candidates
  (`MAX_CANDIDATES` is already 8).
- No new response structure: `label`, `lat`, `lon`, `kind`, `confidence`,
  `secondary` all exist. `kind` widens to the values `GeocodeKind` already
  declares — `address`, `intersection`, `street`, `zip`, `place`, `pin`.
- **No session token, and none is needed.** Session tokens exist to let a
  vendor bill a keystroke sequence as one geocode. There is no vendor and no
  billing, so no per-request state, no cookie, nothing tying two requests
  together. The endpoint stays a pure function of `q`.
- **Debounce in the frontend at 120 ms** and abort the in-flight request on a
  new keystroke. At 0.2 ms a query this is not about server load, it is about
  not re-rendering the dropdown mid-keystroke. Do not debounce longer: the list
  should feel synchronous.
- Rewrite the `docs/API.md` `GET /api/geocode` section, including the
  "only 54% of segments publish address ranges" sentence (see Recommendation).

New `GET /api/reverse?lat=&lon=`: one `ReverseMatch` — `label`, `secondary`,
`kind`, `lat`, `lon`, `distance_m` — or 200 with `{"match": null}` outside
coverage. Bounds validated like `POST /api/search` (40.68–40.90, -74.05–-73.88).

### 5.3 Tests

- `tests/test_geocode.py`: one case per line of §2.3 asserting kind and top
  label; the ordinal, missing-directional, nickname, `@`/`and`/`&`, ZIP and
  edit-distance-1 folds; a 201-char `q` returns `[]`, not a 500; SQL
  metacharacters and `<script>` in `q` return `[]` and are never interpolated.
- `tests/test_etl_addresses.py`: the fold agrees with `normalize_street_name`
  over a fixture of real `full_street_name` values; hyphenated and suffixed
  house numbers parse; a row with no geometry is dropped.
- `tests/test_api.py`: `/api/geocode` returns ≤ 8; `/api/reverse` validates
  bounds and returns `null` outside coverage.
- An accuracy regression: over a fixed sample of address points, the median
  error of the new ladder stays under 50 ft.
- `tests/test_boundaries.py` needs nothing new — no module gains a socket.
