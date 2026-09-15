# Source Data

Measured facts about the datasets CurbCheck reads. Every number here was
produced by a script in `scripts/` against a snapshot in `data/raw/`; the full
output lives in `data/explore/` and is referenced per section. Both directories
are on the mounted drive and gitignored.

Snapshot fetched **2026-09-15**. Re-run `python scripts/explore_fetch.py` to
refresh, then the `explore_*.py` profilers. Each raw file has a `.meta.json`
sidecar with the URL, fetch time, row count and SHA-256.

| Dataset | Socrata id | Filter | Rows | File |
|---|---|---|---|---|
| Parking Regulation Locations and Signs | `nfid-uabd` | `borough='Manhattan'` | 75,865 | `signs_manhattan.json` |
| Street Centerline | `inkn-q76z` | `boroughcode='1'` | 14,106 | `centerline_manhattan.json` |
| Parking Meters – ParkNYC Block Faces | `e7yp-wx55` | none (citywide) | 11,185 | `parknyc_blockfaces.json` |
| Parking Meters – Citywide Rate Zones | `f72k-2u3b` | none | 52 | `meter_rate_zones.json` |
| Parking Meters Locations and Status | `693u-uax6` | `borough='Manhattan'` | 5,082 | `meters_manhattan.json` |
| ASP 2026 suspension calendar (ICS + PDF) | — (nyc.gov) | — | 39 VEVENTs | `calendar/` |

Socrata omits null fields from resource JSON, so a missing key means null.
Paging uses `$limit=50000&$offset=…&$order=:id`.

---

## 1. Signs (`nfid-uabd`)

### 1.1 `record_type` does not separate active from historical

The dataset description says it contains "current and historical" signs, and
the spec (§5.1) builds the active filter on `record_type='Current'`. In the
live export that column is **constant**:

| Scope | `record_type='Current'` | other values |
|---|---|---|
| Citywide | 441,237 | 0 |
| Manhattan | 75,865 | 0 |

The only retirement signal that varies is `sign_design_voided_on_date`, set on
**1,275** Manhattan rows (4,482 citywide), with values from 2017-06-02 to
2026-02-09.

**Active filter: `borough='Manhattan' AND sign_design_voided_on_date IS NULL`**
→ **74,590 rows**. Keep the `record_type` predicate as a cheap assertion, but
it removes nothing today; if it ever starts removing rows, the export changed
and the snapshot should be re-examined.

Everything below is computed over those 74,590 active rows.

### 1.2 Shape

| Measure | Value |
|---|---|
| Active rows | 74,590 |
| Distinct `sign_description` | 1,790 |
| Distinct `sign_code` | 1,797 |
| Distinct `order_number` | 11,651 |
| `order_completed_on_date` range | 1957-11-13 … 2026-09-12 |

Column presence (active rows): `distance_from_intersection`, `side_of_street`,
`sign_code`, `sign_description`, `on/from/to_street`, `sign_size`, `support`
are 100% populated, as is `sheeting_type`. `sign_x_coord`/`sign_y_coord`
93.4%, `sign_notes` 31.4%, `arrow_direction` 26.5%, `on_street_suffix` 1.8%,
`facing_direction` 0.2%. `sign_location` is **empty on every row**.

### 1.3 `sign_code` is a key for `sign_description`

Within the active Manhattan rows the mapping is exact:

- **0 of 1,797** codes carry more than one distinct description.
- 6 of 1,790 descriptions appear under more than one code (a code plus its
  `…A` mirror variant).

So `sign_code` is a stable cache key and the right stratification axis for the
gold set. Prefixes: `PS-` 1,710 codes, `SI-` 33, `SP-` 25, `SR-` 15, `R7-` 6,
plus a dozen singletons (`W14-`, `SC-`, `R4-`, `SW-`, `INSERT`, `SPECIAL`).

Citywide the vocabulary is 3,506 descriptions over 3,523 codes, so Manhattan is
about half of it (`data/explore/descriptions_all_boroughs.tsv`).

### 1.4 Description vocabulary

`data/explore/descriptions.tsv` — `count`, `sign_code(s)`, `sign_description`,
sorted by count desc. This is the parser's and the gold set's input.

| Top N descriptions | Share of active rows |
|---|---|
| 10 | 34.3% |
| 50 | 65.2% |
| 100 | 78.0% |
| 200 | 86.8% |
| 500 | 95.1% |

500 descriptions occur exactly once.

Descriptions are **pure ASCII**. The `·` in the spec's §5.1 sample record does
not occur anywhere in the live data — no character above U+007E appears in any
description. Parentheses (91,383), `:` (20,652), `,` (15,590) and `&` (15,339)
are the common punctuation; `$`, `"`, `#`, `=`, `'`, `+`, `;`, `!` appear in
small numbers. Full census: `data/explore/arrows.txt`.

### 1.5 Not every row is a parking regulation

| Class | Rows | Share | Distinct strings |
|---|---|---|---|
| `NO PARKING…` | 24,856 | 33.3% | 287 |
| `NO STANDING…` | 11,519 | 15.4% | 234 |
| `N HOUR / HMP…` | 10,598 | 14.2% | 470 |
| MTA route/destination/location panel | 10,081 | 13.5% | 18 |
| other | 6,892 | 9.2% | 768 |
| pay-by-cell locator plate | 6,071 | 8.1% | 2 |
| `BUS STOP SIGN…` | 2,342 | 3.1% | 3 |
| `NO STOPPING…` | 1,637 | 2.2% | 7 |
| meta: "METERS ARE NOT IN EFFECT ABOVE TIMES" | 594 | 0.8% | 1 |

About **22% of active rows carry no regulation at all** (MTA panels, pay-by-cell
locator numbers, blank location panels). They must be classified out before any
coverage metric is computed, or the parser will look worse than it is. The
`other` bucket is where the interesting long tail sits: school AVO signs, truck
loading zones, taxi stands, angle-parking, carshare, consular plates.
`data/explore/description_classes.txt` has the top 60.

### 1.6 Arrows

Arrows are drawn with a variable number of dashes (`<----->` occurs 772 times),
so match `<-+>|<-+|-+>`, not three literals.

| Arrow | Rows | Share |
|---|---|---|
| `<->` both ways | 37,255 | 50.0% |
| `-->` one way | 18,024 | 24.2% |
| none | 17,643 | 23.7% |
| `SINGLE ARROW` / `(ARROW)` in words | 1,666 | 2.2% |
| `<--` | **2** | 0.003% |

A left-only arrow is effectively absent. DOT writes every single arrow as
`-->` and puts the bearing in `arrow_direction`:

| Arrow | `arrow_direction` populated |
|---|---|
| `-->` | 18,024 / 18,024 (100%) |
| `SINGLE ARROW` in text | 1,666 / 1,666 |
| `<->` | 57 / 37,255 |
| none (excluding the textual form) | 26 / 17,643 |
| `<--` | 0 / 2 |

`arrow_direction` holds a compass word and is aligned with the street's own
axis, never with the curb side:

| `on_street` type | `arrow_direction` values |
|---|---|
| …STREET (east–west) | West 5,630, East 5,590, South 783, North 711 |
| …AVENUE / BROADWAY (north–south) | South 2,758, North 2,629, East 57, West 57 |

`side_of_street` is perpendicular to that axis (E–W streets: N 18,885 / S
19,023; N–S avenues: E 12,235 / W 12,432). Overall `side_of_street` is S
19,814 / N 19,584 / W 17,653 / E 17,539, never blank.

**Position:** of the 55,281 rows with an arrow, **54,164 (98.0%)** end with the
arrow once the trailing `(SUPERSEDES …)` parenthetical is stripped; only 8,134
end with it in the raw string. Strip the supersedes tail first, then read the
arrow off the end. The 1,117 exceptions are riders appended after the arrow
(`D/S DECALS ONLY`, `(PUBLIC SCHOOL SIGN)`, `NO ENGINE IDLING MAX FINE $2000`).

This refines decision **D3**: the glyph gives arrow *arity* (one way / both ways
/ none), `arrow_direction` gives the *bearing*. Neither alone is sufficient, and
the glyph cannot be the sole source because it never says "left."

### 1.7 `distance_from_intersection` and coordinates

`distance_from_intersection` is clean: **0 missing, 0 non-numeric, 0 negative**
on all 74,590 rows. Feet.

| min | p25 | median | p75 | p95 | p99 | max |
|---|---|---|---|---|---|---|
| 0 | 105 | 183 | 345 | 731 | 1,438 | 5,366 |

156 rows are 0 ft; 381 exceed 2,000 ft (long blocks on Riverside Drive and the
drives, where from/to are not adjacent cross streets — see §2.4).

`sign_x_coord`/`sign_y_coord` (EPSG:2263, US feet): **4,936 rows (6.6%) have no
coordinate pair**. Where present, all 69,654 fall inside x 979,279–1,008,802
and y 194,845–257,353 — no zeros, no negatives, nothing outside Manhattan.
Junk coordinates are not a failure mode here; *absent* coordinates are, which
is one more reason the linear-referenced position (spec §5.2) is the primary.

### 1.8 How signs group

Keys are whitespace-collapsed and uppercased.

| Grouping | Count |
|---|---|
| Blockface-sides `(on, from, to, side)` | 11,613 |
| Posts `(blockface-side, distance_from_intersection)` | 43,170 |
| Mean signs per blockface-side | 6.42 |
| Mean posts per blockface-side | 3.72 |
| Mean signs per post | 1.73 |

482 blockface-sides carry a single sign; 1,074 carry more than 12. Only 38
blockface-sides are covered by more than one `order_number`, which supports
treating the order as a work unit rather than a regulation key (decision D5).
Histograms: `data/explore/grouping.txt`.

### 1.9 Street names

Sign rows spell names out and pad numbers (`'EAST   85 STREET'` — 32,386 rows
have two or more consecutive spaces in `on_street`); the centerline abbreviates
(`'E  85 ST'`, `stname_label` `'E 85 ST'`). Matching needs a normalizer on both
sides: collapse whitespace, uppercase, map suffix and directional words
(STREET→ST, AVENUE→AVE, EAST→E, SAINT→ST, FT→FORT, FIRST→1 …), and a small
alias table for honorific renamings. `scripts/explore_names.py` is the
prototype; it is what the ETL normalizer should start from.

| Measure | Raw uppercase + whitespace collapse | With `explore_names.normalize_street` |
|---|---|---|
| Distinct sign street names matched | 26 of 882 (2.9%) | 770 of 833 (92.4%) |
| Name references matched | 4.9% | **99.74%** (223,191 / 223,770) |
| Rows whose `on_street` resolves | — | **99.90%** |
| Rows whose `on`+`from`+`to` all resolve | — | **99.27%** (74,048 / 74,590) |

Aliases that the word map cannot reach and that must be table-driven (measured
from the residue, `data/explore/street_names.txt`):

| Sign name | Centerline name |
|---|---|
| `6 AVENUE` | `AVE OF THE AMERICAS` |
| `FDR DRIVE` | `FRANKLIN D ROOSEVELT DR` |
| `ADAM C POWELL BLVD`, `A C POWELL BLVD` | `ADAM CLAYTON POWELL JR BLVD` |
| `FRED DOUGLASS BLVD`, `FRED DOUGLAS BLVD` | `FREDERICK DOUGLASS BLVD` |
| `WEST 110 STREET` | `CATHEDRAL PKWY` |
| `MALCOLM X BLVD` | `LENOX AVE` |
| `MACDOUGAL ST`, `LAGUARDIA PL`, `VAN DAM ST` | `MAC DOUGAL ST`, `LA GUARDIA PL`, `VANDAM ST` |
| `WILLETT ST` | `BIALYSTOKER PL` |
| `QUEENSBORO BRG` | `ED KOCH QUEENSBORO BRG` |
| `BLEEKER ST`, `CUMMINGS ST`, `THEATRE ALY` | typos: `BLEECKER ST`, `CUMMING ST`, `THEATER ALY` |

Two more quirks:

- `from_street`/`to_street` is `DEAD END` on 610 references — not a street.
- 14 values carry a roadway qualifier after an asterisk
  (`PARK AVENUE*WEST RDWY`), duplicating `on_street_suffix`.

The 63 names still unresolved are mostly not streets: `BATTERY PARK`,
`STATEN ISLAND FERRY BUS LOOP`, `RIVERBANK STATE PARK`, `WATERSIDE PLZ`,
highway ramps, `MOYLAN PL`, `THELONIOUS SPHERE MONK CIR`.

---

## 2. Street centerline (`inkn-q76z`)

Full profile: `data/explore/centerline.txt`.

### 2.1 What is in it

14,106 Manhattan rows, one per `physicalid` (three ids repeat twice).

| `rw_type` | Meaning | Rows |
|---|---|---|
| 1 | Street | 9,297 |
| 6 | Sidewalk/path | 1,641 |
| 3 | Bridge | 1,125 |
| 9 | Ramp | 721 |
| 2 | Highway | 649 |
| 14 | Ferry route | 359 |
| 4 | Tunnel | 143 |
| 7, 8, 13, 10 | Ped path, driveway, connector, alley | 171 |

Signs also sit on highways and bridges (FDR Drive is `rw_type` 2 and 3), so the
snap candidate pool should be `rw_type ∈ {1, 2, 3, 10}` (11,102 segments), not
`rw_type = 1` alone. `status` is `2` on 14,103 rows.

`trafdir`: FT 4,744, TF 4,117, NV 2,674 (non-vehicular), TW 2,571.

Distinct names: 1,295 over all types, 910 over `rw_type=1`. BROADWAY is 388
segments, PARK AVE 204, 1 AVE 194.

### 2.2 Geometry

Every row is a **MultiLineString in EPSG:4326, lon,lat order** — *not* EPSG:2263
as the spec assumed for LION (§5.5). Sign coordinates are 2263, so one
reprojection is unavoidable; `pyproj` is already pinned.

- 12,811 rows are a single part; 1,295 have 2–27 parts.
- 8,777 rows are a bare two-vertex line.

**`segmentlength` is unreliable.** Comparing it to the geometry length:
median relative error 0.2%, but **4,747 of 12,818 rows are off by more than
5%**, sometimes wildly (`W 100 ST` declares 36.9 ft for a 440 ft block;
`CONVENT AVE` declares 255.8 ft for 538 ft). It is also null on 9.1% of rows.
**Compute length from the geometry.** `shape_length` is consistent with the
geometry but is in Web Mercator metres (ratio 0.403 to true feet at this
latitude), so it is not directly usable either.

`streetwidth` (feet, roadbed width, used for the curb offset): null on 17.4% of
all rows but only **181 of 9,297 `rw_type=1` rows**. For streets: min 0, p25 30,
median 34, p75 50, max 85. `streetwidth_irr` is null on every row.

Population of the other columns the snap and the geocoder want:
`physicalid` 100%, `trafdir` 100%, `l_zip`/`r_zip` 97.1%,
`segmentlength` 90.9%, `streetwidth` 82.7%, `number_park_lanes` 82.6%,
`l_blockfaceid`/`r_blockfaceid` **79.0%**, address ranges
(`l_low_hn`/`l_high_hn`/`r_low_hn`/`r_high_hn`) **54.0%**, `bike_lane` 30.6%.

The 54% address-range coverage is the number to watch for the local geocoder —
nearly half the segments cannot be reverse-geocoded from house number alone.

### 2.3 Intersections rebuild from shared endpoints

The Socrata export drops LION's from/to node ids, which decision D1 flagged as
the thing that would reverse the choice of CSCL. It does not: adjacent segments
share endpoint coordinates exactly (compared at 1e-7°, ~1 cm).

Over the 9,297 `rw_type=1` segments:

| Measure | Value |
|---|---|
| Distinct endpoint coordinates | 6,676 |
| Nodes joining ≥2 segments | 6,344 |
| Dangling endpoints (degree 1) | 332 |
| Nodes carrying more than one street name | 4,269 |
| Segments with ≥1 differently named neighbour | **8,602 (92.5%)** |

Node degree: 1→332, 2→2,046, 3→1,551, 4→2,686, 5→52, 6→8, 7→1.

Worked example, `data/explore/block_3ave_e85_e86.txt` — the block in the spec's
own sample sign record:

```
physicalid       3681           full_street_name '3 AVE'
segmentlength    284.72 ft      geometry length  284.8 ft
streetwidth      70 ft          trafdir          FT
l_blockfaceid    1322607130     house numbers 1510-1528
r_blockfaceid    1322604506     house numbers 1509-1525
start node (-73.9544835, 40.7781469)  neighbours ['E 85 ST']
end   node (-73.9539850, 40.7788304)  neighbours ['E 86 ST']
```

A single segment, its two endpoints naming exactly the two cross streets the
sign row cites. Digitization runs from E 85 to E 86, so `distance_from_
intersection = 44` on the W side places the sign 44 ft north of E 85 St,
offset 35 ft (half of 70) west of the centerline.

### 2.4 How far the name join gets

`scripts/explore_snap_feasibility.py` matches every distinct sign blockface
`(on, from, to)` to segments whose endpoints name both cross streets.
Full output: `data/explore/snap_feasibility.txt`.

| Outcome | Blockfaces | Sign rows |
|---|---|---|
| Exactly one segment spans the block | 5,961 (72.1%) | 54,780 (**73.4%**) |
| Two or more segments span it (ambiguous) | 694 (8.4%) | 4,090 (5.5%) |
| Both cross streets exist but need a multi-segment chain | 1,129 (13.7%) | 12,559 (16.8%) |
| Only one cross street found on this street | 295 (3.6%) | 2,245 (3.0%) |
| Cross street is `DEAD END` | 143 (1.7%) | 604 (0.8%) |
| Neither cross street found | 32 (0.4%) | 183 (0.2%) |
| `on_street` not in the centerline | 17 (0.2%) | 129 (0.2%) |

So a single-segment lookup covers 73%, and **walking the chain of same-named
segments between the two cross-street nodes is required for another 17%** —
those are blocks DOT describes across several centerline segments
(`RIVERSIDE DR | W 95 ST | W 79 ST`, `5 AVE | E 79 ST | E 72 ST`). Implement
the chain walk; it is not an edge case.

Sanity check on the result: only **146 of 58,870** matched rows (0.25%) have a
`distance_from_intersection` longer than the block they matched, and the worst
offenders are the same multi-block descriptions.

---

## 3. Meters

### 3.1 ParkNYC block faces (`e7yp-wx55`)

The spec has the ids swapped (decision D6): `s7zi-dgdx` is the map view,
`e7yp-wx55` is the data table. 11,185 rows citywide, **3,366 Manhattan** —
but `borough` is spelled inconsistently (`Manhattan` 3,362, `MANHATTAN` 3,
`manhattan` 1, and the same for other boroughs), so the filter must be
case-insensitive.

Geometry: MultiLineString, WGS84. Manhattan blockface length min 68 ft, median
231 ft, max 2,014 ft.

`meter_rate` is a zone label, not a price:

| Value | Manhattan rows |
|---|---|
| Zone M2 | 1,882 (55.9%) |
| Zone M1 | 965 (28.7%) |
| Zone 3 | 390 (11.6%) |
| Zone M3 | 68 (2.0%) |
| Zone 2 | 45 (1.3%) |
| Zone 1 | 16 (0.5%) |

Prices are in four text columns per vehicle class — `all_vehicl` ("2 Hours"),
`all_vehi_1` ("Monday-Saturday 9 AM-7 PM"), `all_vehi_2` ("$5.00 1st Hour /
$8.25 2nd Hour"), `all_vehi_3` (max session "$13.25"), and the `commerci_*`
mirror set. Absent values are the literal string `"N/A"`, not null.
`vehicle_ty`: All Vehicles 52.8%, Dual 29.2%, Commercial Only 17.9%, Charter
Bus Only 4 rows. `pay_by_cel` is populated on 100% of Manhattan rows.
`side_of_st` is a single letter (`W` 1,055, `E` 988, `S` 674, `N` 648, one
lowercase `s`). Street names are **Title Case** (`'William Street'`), unlike the
signs' upper case — the normalizer handles it.

### 3.2 The blockface join works by name

Joining each Manhattan ParkNYC blockface to the sign blockface-sides on
(normalized `on_street`, unordered cross-street pair, side letter):

| Outcome | Blockfaces | Share |
|---|---|---|
| street + cross pair + side all match | 3,213 | **95.45%** |
| street + cross pair match, side does not | 15 | 0.45% |
| street known, cross pair unknown | 96 | 2.85% |
| street unknown to the sign data | 42 | 1.25% |

The residue is the same alias problem as §1.9 plus ParkNYC's own typos
(`ADAM C POWELL JR BLVD`, `FORSYTHE ST`, `W 125 STRET`, `6TH AVE`, `3RD AVE`).
Geometry is available as a check but is not needed to get the join started.
Detail: `data/explore/meters_join.txt`.

### 3.3 Rate zones (`f72k-2u3b`)

`da76-p95d` is a map view with no columns; its `modifyingViewUid` points at
**`f72k-2u3b`**, which is the data table. 52 MultiPolygon rows citywide, **7 in
Manhattan**. The `rate_zone` string carries the prices, confirming the spec's
§13.2 table from the live data:

| Zone | `zone_name` | Rate |
|---|---|---|
| M1 | Financial District, Midtown | All $5.50 1st hr / $9.00 2nd / $5.50 add'l; Commercial $7 / $10 / $13 |
| M2 | Manhattan Neighborhood | All $5.00 / $8.25; Commercial $6 / $9 / $12 |
| M3 | 96th St to 110th St | All $3.00 / $5.00 |
| Zone 1 | 125th Street | All $2.50 / $5.00 |
| Zone 2 | Inwood | All $2.00 / $3.00 |
| Zone 3 | Outerborough | All $1.50 / $2.50 |

### 3.4 Meter points (`693u-uax6`)

5,082 Manhattan rows. `status`: Active 3,964 (78.0%), Inactive 1,105 (21.7%),
null 13. Carries `lat`/`long` (100%), `x`/`y`, `meter_hours` (99.7%), and the
same on/from/to/side naming as the signs. Use it only to confirm a blockface is
metered, per the spec. Detail: `data/explore/meters_points.txt`.

---

## 4. Calendars

**No NYC Open Data dataset publishes the ASP suspension calendar, the DOE
school calendar, or the legal-holiday list.** Catalog searches on
`api.us.socrata.com` for "alternate side" (2 results, both unrelated), "school
calendar" (21, all attendance or energy-disclosure datasets) and "holiday"
(9, all Holiday Construction Embargo) return nothing usable.

The DOT calendar on nyc.gov **is** reachable — it returns 403 only to requests
without a browser `User-Agent`:

- ICS `https://www.nyc.gov/html/dot/downloads/misc/2026-alternate-side.ics` (23 KB)
- PDF `https://www.nyc.gov/html/dot/downloads/pdf/asp-calendar-2026.pdf` (195 KB)

`scripts/explore_calendar.py` fetches and parses both. Measured from the ICS
(`data/explore/asp_calendar_2026.txt`):

| Measure | 2026 |
|---|---|
| VEVENTs | 39 |
| Suspended days after expanding `DTSTART`…`DTEND` | **47** |
| Distinct suspension dates | **42** |
| Dates where the ICS says meters are *not* in effect | 7 |

Eight events are two-day holidays whose `DTEND` is one day past the last
suspended day, so counting VEVENTs undercounts. This settles the spec's §5.6
conflict (28 vs 67): neither figure is right for 2026 — count from the ICS.

Two details worth keeping:

- `SUMMARY` is the constant string "Alternate Side Parking Suspended". The
  reason and the meter status are in `DESCRIPTION`
  ("…suspended for Christmas Day. Parking meters will not be in effect.").
- The meters-off days are exactly the six Major Legal Holidays — New Year's
  Day, Memorial Day, Independence Day (7/3 and 7/4), Labor Day, Thanksgiving,
  Christmas. Every other suspension keeps meters running. That is a
  machine-readable source for the rule the spec quotes in prose.

The 311 API (`api.nyc.gov/public/api/GetCalendar`) answers 401 without a key,
as expected; it stays an opt-in extra.

---

## 5. Basemap

The spec's build URL (`build.protomaps.com/builds.json`) 404s. The working path,
verified 2026-09-15:

1. The builds manifest the Protomaps builds page itself loads:
   `https://build-metadata.protomaps.dev/builds.json` — a JSON array of daily
   builds with `key`, `size`, `b3sum`, `uploaded`, `version`. It rejects the
   default urllib `User-Agent` with 403; send any other.
2. Archives live at `https://build.protomaps.com/<key>`, e.g.
   `20260914.pmtiles`, 138 GB, basemap version 4.15.2.
3. `pmtiles extract` pulls only the tiles in the bbox over HTTP range requests.

```
curl -L https://github.com/protomaps/go-pmtiles/releases/download/v1.31.2/go-pmtiles_1.31.2_Linux_x86_64.tar.gz | tar xz
./pmtiles extract https://build.protomaps.com/20260914.pmtiles data/basemap/manhattan.pmtiles \
    --bbox=-74.03,40.68,-73.90,40.88 --maxzoom=15
```

Measured result — **no planet download needed**:

| Measure | Value |
|---|---|
| Transferred | 24 MB (49 HTTP requests, overfetch 0.05) |
| Wall time | 7 s |
| Output | **23 MB**, `data/basemap/manhattan.pmtiles` |
| Tiles | 474 entries, zoom 0–15, MVT, gzip |
| OSM replication time | 2026-09-14T04:00:00Z |
| SHA-256 | `881ccd17a4ae3498b1554dbc3400b09808297bd44f680d6efba38cc1c4f1e360` |

`scripts/explore_basemap.py` resolves the current build and prints the command;
`--extract` runs it. The archive's own metadata carries the required
attribution string, `© OpenStreetMap`.

Fonts and sprites are in `github.com/protomaps/basemaps-assets` (no releases;
fetch from `raw.githubusercontent.com/protomaps/basemaps-assets/main/…`):

- `fonts/Noto Sans Regular/*.pbf` — 256 glyph ranges, 6.2 MB total. Also
  `Noto Sans Medium`, `Noto Sans Italic`, `Noto Sans Devanagari Regular v1`.
- `sprites/v4/{light,dark,black,white,grayscale}[@2x].{json,png}` — ≤ 29 KB each.

Style JSON comes from the `@protomaps/basemaps` npm package (CC0); vendor it
rather than fetching `npm-style.protomaps.dev` at runtime.

---

## 6. Scripts

The `explore_*` profilers that produced every number above:

| Script | Produces |
|---|---|
| `explore_fetch.py` | `data/raw/*.json` + `.meta.json` |
| `explore_names.py` | Shared street-name normalizer (no output) |
| `explore_signs.py` | `descriptions.tsv`, `arrows.txt`, `fields.txt`, `grouping.txt`, `sign_codes.txt`, `description_classes.txt`, `street_names.txt` |
| `explore_streets.py` | `centerline.txt`, `block_3ave_e85_e86.txt` |
| `explore_snap_feasibility.py` | `snap_feasibility.txt` |
| `explore_meters.py` | `meters_parknyc.txt`, `meters_points.txt`, `meter_rate_zones.txt`, `meters_join.txt` |
| `explore_calendar.py` | `data/raw/calendar/*`, `asp_calendar_2026.txt` |
| `explore_basemap.py` | `data/basemap/manhattan.pmtiles` |

They are stdlib-only (shapely and pyproj are pinned for the package but were
not importable while this profile was made) and read-only against `data/raw/`.

`scripts/` also holds four scripts that are not profilers and do write:
`fetch_basemap_assets.py` vendors the style, glyphs and sprites into
`web/basemap/` (§5, `--check` verifies instead of writing), `parse_report.py`
and `eval_gold.py` score the grammar against the corpus and the gold set, and
`smoke_search.py` runs a query against the built database. `dev_mock_server.py`
serves canned API responses for working on the frontend alone.
