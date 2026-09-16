# CurbCheck

Local, offline-capable web app that answers: "Where can I legally park a
passenger car near address X in Manhattan from T1 to T2, ranked by walk time
and money?" Built from NYC Open Data. Single user, server bound to 127.0.0.1.

A snapshot of the current state, kept short. Not a history; git holds that.
Update it when the state changes and delete anything no longer true.

## Read first

- `STYLE_GUIDE.md` — how to write code and comments here. Binding.
- `docs/SPEC.md` — the original specification. §3 (threat model) and §11
  (failure surfacing) are binding; the rest is guidance, much of it overridden
  by `docs/DECISIONS.md`, which records every departure with its evidence
  (D1–D30) and is what to read before trusting a spec detail.
- As needed: `docs/ARCHITECTURE.md` module map and schema, `docs/STATIC_SITE.md`
  the browser build and its contract, `docs/DATA.md` the
  sources measured, `docs/VALIDATION.md` the run against DOT's own viewer (§9
  supersedes §8), `docs/API.md` the contract, `docs/SECURITY.md` the controls,
  `docs/ux/` the UI audit, `README.md` the outside view.

## Layout

```
curbcheck/  config.py net.py model.py db.py cli.py
  geocode/  the local suggester: candidates, query, street, address,
            intersection, places, suggest, reverse
  etl/      fetch, stage, streets, snap, parse/, segments, meters, calendar,
            addresses, build
  engine/   window, resolve, cost, search, coverage, geo, labels, ranges, signs
  api/      app, routes, schemas, errors
  pack.py   the SQLite -> gzipped JSON compiler behind `curbcheck pack`
web/        index.html, tokens/components/styles.css, fonts/, vendor/, basemap/,
            and 16 ES modules: app, map, api, searchcard, autocomplete, results,
            detail, verdicts, rank, legend, drawer, about, states, dom,
            format, copy
site/       the static site: build.py, slice_basemap.py, static/ (the JS port of
            engine/ and geocode/, one file per Python module, plus the worker and
            the api.js shim), tests/ (node --test, incl. the differential harness)
tests/      pytest suite; tests/gold/ is the 520-line sign-text gold set
scripts/    Data exploration, basemap and font vendoring, gold eval, bench_api
docs/       Spec, architecture, decisions, data, API, security, validation, ux/
data/       Mounted drive: downloads, the 94 MB SQLite DB, basemap. Gitignored.
```

## Commands and environment

`make setup` hash-pinned install · `make sync` build `data/curbcheck.sqlite` ·
`make check` ruff + mypy + pytest + node --test + audit · `pytest -m slow` the
fuzzers · `make serve` the API and UI on http://127.0.0.1:8765 · `make docker-*`
the same in a container · `make site` the static site into `build/dist`. Conda env `curbcheck` (Python 3.12) is active in every shell and
application packages come from `requirements*.txt` via pip; data lives in
`data/`, or wherever `CURBCHECK_DATA_DIR` points.

## Where it stands (2026-09-15 snapshot)

74,590 active signs (74,389 after duplicates) → 96.4% snapped, 95.8% of
blockface-sides covered, 30,524 curb spans plus 5,648 grey placeholders for the
sides with no rule at all, 99.83% of rows parsed, 100% semantic / zero
false-permitted on the 520-description gold set, 0 of 6,558 metered segments
unpriced, 26 of 30 sampled sides agreeing with DOT's viewer — which renders the
same SIMS export we read, so that is a check on our source, not a survey.
Address search comes from a local index of 63,245 doors, 5,645 corners, 5,796
places and 2,815 street spellings, matched on any word (D29): median 0 m error on a 400-door sample,
nothing leaving the machine, answered from one read-only connection per worker
thread whose 48 MB page cache holds `/api/geocode` at a 6 ms p50 on this data
mount (32 ms on a street the connection has not read yet), where it was 489 ms;
the same cache is what holds a 30-minute search at 0.9 s, where it was 4.8 s.
The UI is the `docs/ux/` redesign: map-first, a 36 px §17 strip with the full
notice one click away (D30), local autocomplete.
The static site (D32, `site/`) is built and green but not yet live: the engine
and geocoder are ported to JavaScript and the differential harness matches the
Python engine on all 1,975 replayed queries; the 91 MB database packs to 8.3 MB
gzipped and the whole site is 53 MB. Going live needs the owner to make the
repo public, enable Pages, and add one CNAME at Squarespace (`site/README.md`).

## Non-negotiables

- Server binds 127.0.0.1 only. No telemetry, no remote assets, strict CSP. All
  outbound HTTP goes through `curbcheck/net.py` and its domain allowlist.
- Downloaded data is untrusted: parameterized SQL, safe parsers, escaped output.
- Never show a confident "legal" verdict where data is missing or ambiguous;
  the raw sign text is always exposed next to any verdict.
- The sign-text parser is a deterministic grammar. No LLM at runtime (D2).
  Dependencies are hash-pinned; adding one needs a `docs/DEPENDENCIES.md` row.
- `tests/test_boundaries.py` enforces the import boundaries: only `net.py`
  reaches the network, `etl/` and `api/` never import each other, no wildcard
  bind address anywhere.
