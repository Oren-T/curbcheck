# The static site

CurbCheck with no server: the `web/` frontend, a JavaScript port of the engine
and the geocoder running in a Web Worker, and a data pack compiled from the
SQLite file. Deployed to GitHub Pages by `.github/workflows/pages.yml`, which
re-runs the ETL every Monday. `docs/STATIC_SITE.md` is the design and the
contract; `docs/DECISIONS.md` D32 is why.

## Build it locally

```bash
make site          # pack -> fixtures -> unit + differential tests -> tiles -> build/dist
python -m http.server --directory build/dist 8080   # then open http://127.0.0.1:8080/
```

The steps, one at a time:

```bash
curbcheck pack --out build/pack                                  # 28 s
python site/tests/harness/make_fixtures.py                       # 2 min; writes site/tests/harness/expected.json
node --test site/tests/*.test.js site/tests/harness/*.test.js    # 302 tests incl. 1,975 replayed queries
python site/slice_basemap.py data/basemap/manhattan.pmtiles build/tiles
python site/build.py --out build/dist --pack build/pack --tiles build/tiles [--base-path /curbcheck/]
```

Needs the database (`make sync`) and the basemap archive
(`scripts/fetch_basemap_tiles.py`), Python 3.12 with the project installed, and
Node 20 or later. Nothing is installed from npm.

## What ships, measured on the 2026-09-15 database

|                          | bytes       | notes                                                                   |
| ------------------------ | ----------- | ----------------------------------------------------------------------- |
| `pack/rules.*.json.gz`   | 5.4 MB      | 18.7 MB inflated: spans, rules, all 74,389 signs, meter rates, calendar |
| `pack/geocode.*.json.gz` | 1.9 MB      | 6.6 MB inflated: the eight address tables                               |
| `pack/streets.*.json.gz` | 0.9 MB      | 2.8 MB inflated: centerline and nodes                                   |
| `basemap/tiles/`         | 41.6 MB     | 474 tile files, zoom 0–15; a view fetches about six                     |
| `vendor/maplibre-gl/`    | 1.2 MB      | vendored, hashes in `web/vendor/MANIFEST.md`                            |
| everything else          | 0.6 MB      | the frontend, the engine, the glyphs, the font                          |
| **total**                | **53.5 MB** | in 561 files; GitHub Pages allows 1 GB                                  |

A first visit downloads about 9.5 MB: the three pack files (8.3 MB), MapLibre,
the style, a few glyph ranges and the tiles in view. The pack is fetched once
per page load and held in the worker's memory: about 88 MB of retained heap
in Node 20, with `loadPack` taking 0.6 s warm and 1.2 s cold on a laptop. A
search over a 30-minute radius runs in tens of milliseconds; a keystroke in
the address box in under a millisecond.

## What the harness proves

`site/tests/harness/compare.test.js` replays the Python engine's answers to
600 searches, 1,075 detail panels, 200 typed strings and 100 dropped pins
through the browser engine and compares every field exactly (floats to a
relative 1e-9). It runs in `make check` when `build/pack` and the fixture are
present, and it is a gate in `pages.yml`: the site does not deploy on a
mismatch. `site/tests/harness/README.md` says how to read a failure.

The unit tests under `site/tests/` replay two smaller fixtures generated from
Python — `time_cases.json` (both daylight-saving Sundays of 2026 and 2027) and
`money_cases.json` (every meter rate list and every confidence pair in the
database) — and port the behaviour tests of the Python modules they mirror.

## Go-live checklist

1. Merge to `main`. Make the repository public (Pages on a free account needs it).
2. GitHub → Settings → Pages → Build and deployment → Source: **GitHub Actions**.
   Actions → **pages** → Run workflow. Open `https://oren-t.github.io/curbcheck/`.
3. Squarespace Domains → `orentirschwell.com` → DNS → DNS Settings → Custom
   Records → Add Record: Type `CNAME`, Host `curbcheck`, Data `oren-t.github.io`.
   Never point it at the apex domain.
4. GitHub → Settings → Pages → Custom domain `curbcheck.orentirschwell.com` →
   Save. When the DNS check passes, tick **Enforce HTTPS** (may take up to a day
   to become available).
5. Run the workflow again; the base path becomes `/`. Check with
   `curl -sI https://curbcheck.orentirschwell.com/`.

Before step 1, three things are the owner's: the repository licence, the NYC
Open Data terms review, and the §17 notice on first visit
(`docs/STATIC_SITE.md`, "What stays the owner's call").

## Privacy

`site/PRIVACY.md`. The short version is on the About sheet of the site itself.
