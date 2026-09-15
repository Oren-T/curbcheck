# The differential harness

The browser engine is a second implementation of `curbcheck/engine/` and
`curbcheck/geocode/`. This is what keeps it honest: a fixed set of queries is
answered by the Python engine, then replayed through the JavaScript one, and
any difference fails. `docs/STATIC_SITE.md` ("The differential harness") is the
contract; `.github/workflows/pages.yml` runs all three steps before it builds
`dist/`, so a mismatch stops the deploy and the previous one stays live.

## The three steps

```sh
curbcheck pack --out build/pack                                  # 1. the data that ships
python site/tests/harness/make_fixtures.py --pack build/pack     # 2. the reference answers
node --test site/tests                                           # 3. the comparison
```

1. **The pack.** The JS engine reads no SQLite; it reads the pack. Building it
   first is also what makes step 2's `--pack` check meaningful: it compares
   `meta.json`'s counts against the database's tables and refuses to write a
   fixture for a different snapshot than the one the pack was cut from.
2. **The fixture.** `make_fixtures.py` calls `curbcheck.api.routes`' own
   handler functions — not HTTP, not a copy of their bodies — and writes the
   API JSON for every query, or the error code for the ones that must fail.
   `--db` defaults to `data/curbcheck.sqlite` (`CURBCHECK_DATA_DIR` applies),
   `--out` to `site/tests/harness/expected.json`. The file is a build output:
   gitignored, regenerated per run, never committed (STYLE_GUIDE §6).
3. **The comparison.** `compare.test.js` loads the pack with
   `tests/helpers/read_pack.js` and `static/pack/loader.js`, builds the state
   `worker.js` builds, and calls `handlers.handle(op, args, pack, state)` for
   every query. `PACK_DIR` (default `build/pack`) and `EXPECTED` (default
   `site/tests/harness/expected.json`) override the two inputs. Both missing is
   the normal case on a clone with no database, and the test **skips** with a
   message naming what it could not find.

## What is compared

Every number is equal when `|a - b| <= 1e-9 * max(1, |a|, |b|)`: one ulp
between GEOS's `Distance::pointToSegment` and the ported one is allowed, and
that is the only latitude in the file. Strings, verdicts, ids, nulls, array
length and order, object keys **and their order** are exact; so is the error
`code` on a query that must fail (messages are not compared — the doc reserves
the right to word them differently). A failing op reports the query id, the
JSON path, both values, and a `op: N of M matched` summary line; the first 20
differences are printed, and one query stops contributing after 8.

`health` and `syncStatus` are deliberately not in the set. Both differ from the
server on purpose — `health` adds `notice` and `hosting`, `syncStatus` drops
the keys whose values are filesystem paths — so an exact comparison would be
asserting the wrong thing. The unit tests under `site/tests/` cover them.

## The query set

`queries.py` builds it from the database with `random.Random(20260915)`, so the
same database gives the same queries and `expected.json` is byte-identical
between runs (nothing in it is a clock or a path).

| op        | queries | what they cover                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| --------- | ------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `search`  | 600     | 96 points drawn over the coverage box and kept by `within_coverage`, plus every address string in `tests/test_geocode_real.py`; 32 windows — a weekday, a Saturday, a Sunday, 20 of the 42 ASP suspension dates, both DST Sundays (around the change, inside it, and a whole wall-clock day, which is 23 real hours in spring and 25 in autumn and 24 to the window-length check either way), a midnight crossing, five minutes, 24 hours; walk radii 3, 10 and 30; default and skewed weights; `limit` 500 / `map_limit` 5000 except for 30 at the defaults; a fifth of the windows sent as `-04:00`/`-05:00`/`Z` rather than naive. 18 of the 600 must fail. |
| `geocode` | 200     | doors, corners, places, streets, ZIPs, one transposition typo each over 15 doors, single letters, five-character prefixes, punctuation and lowercase, the 120-character maximum and the 121 that is refused.                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `reverse` | 100     | 75 pins jittered ~20 m off a surveyed door, 15 park label points, three Central Park interiors (the Great Lawn is 184 m from a centerline and inside, the reservoir 287 m and refused), five pins in water or another borough, two outside the input box.                                                                                                                                                                                                                                                                                                                                                                                                      |
| `segment` | 1,075   | the first three legal and first three non-legal spans of every search, with that search's window and without it, deduplicated and thinned by a stride; plus the four ways the endpoint refuses.                                                                                                                                                                                                                                                                                                                                                                                                                                                                |

The counts are constants at the top of `queries.py` and the mix of walk radii
is a measured trade-off, recorded there: a 30-minute search costs 2.8 s and
6 MB of JSON against a 3-minute one's 0.42 s and 0.18 MB, so six hundred
searches drawn uniformly would be a twenty-minute run and a 1.5 GB fixture —
which V8 could not read at all, its maximum string length being about 512 MB.
All three radii are in the set; the cheap one carries it.

## Running it, and what a failure means

Measured on the 2026-09-15 database (74,389 signs, 36,172 spans, 11,102
centreline segments) over the `data/` mount, with nothing else running:

|                        |                                                                                                             |
| ---------------------- | ----------------------------------------------------------------------------------------------------------- |
| queries                | 1,975: 600 search, 200 geocode, 100 reverse, 1,075 segment                                                  |
| refused on purpose     | 33: 14 `outside_coverage`, 13 `validation_error`, 3 `invalid_request`, 2 `address_not_found`, 1 `not_found` |
| step 2                 | 110 s inside the engine, 2 min 10 s of wall clock including the query build and the 143 MB write            |
| `expected.json`        | 143 MB                                                                                                      |
| two runs, one database | identical bytes (same sha256)                                                                               |

143 MB is one `JSON.parse` of one string in step 3, comfortably under V8's
~512 MB string ceiling; if the set is ever widened, raise the heap with
`NODE_OPTIONS=--max-old-space-size=8192` before widening it further.

A mismatch is one of three things, in the order worth checking:

1. **A bug in the JavaScript.** The usual one. The path in the message names
   the field; `docs/STATIC_SITE.md` "Porting rules" names the traps (SQL result
   order where there is no `ORDER BY`, UTF-16 versus `localeCompare`, the order
   floats are combined in, `zoneinfo`'s `fold`).
2. **A change to the Python engine.** The fixture is generated, so a deliberate
   change to a verdict or a label shows up here as a hundred mismatches at
   once. Regenerate it and check the diff is what you meant.
3. **A stale fixture.** `expected.json` and the pack must come from one
   database. Run step 2 with `--pack` and it will say so instead of failing
   obscurely in step 3.

Two things `make_fixtures.py` does that `routes.py` does not, because FastAPI
does them: it builds the `SearchRequest` (a `ValidationError` is recorded as
`validation_error`, which is what `api/app.py` turns it into) and it enforces
the `Query(...)` bounds on `q`, `lat` and `lon`. Everything else in the fixture
comes from the route functions themselves, private helpers included.
