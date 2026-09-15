# CurbCheck

Local, offline-capable web app that answers: "Where can I legally park a
passenger car near address X in Manhattan from T1 to T2, ranked by walk time
and money?" Built from NYC Open Data. Single user, server bound to 127.0.0.1.

This file is a snapshot of the current state of the project, kept as short as
possible. It is not a history; git holds the history. Update it when the state
changes, and delete anything that is no longer true.

## Read first

- `STYLE_GUIDE.md` — how to write code and comments here. Binding.
- `docs/SPEC.md` — the original specification. Sections 3 (threat model) and
  11 (failure surfacing) are binding. Other sections are guidance and several
  have been overridden; see `docs/DECISIONS.md` before trusting a detail.
- `docs/DECISIONS.md` — every departure from the spec, with evidence (D1–D19).
- `docs/ARCHITECTURE.md` — module map, schema, data flow.
- `docs/DATA.md` — measured facts about the source datasets.
- `docs/API.md` — the local HTTP contract. `docs/SECURITY.md` — where each
  control lives. `docs/VALIDATION.md` — ground-truth accuracy.
- `README.md` — the outside view: quick start, limitations, data sources.

## Layout

```
curbcheck/        Python package
  config.py net.py model.py db.py geocode.py cli.py
  etl/            fetch, stage, streets, snap, parse/, segments, meters,
                  calendar, build
  engine/         window, resolve, cost, search
  api/            app, routes, schemas, errors
web/              Static frontend (plain ES modules), vendor/ and basemap/ assets
tests/            pytest suite; tests/gold/ is the 520-line sign-text gold set
scripts/          Data-exploration scripts, basemap vendoring, gold eval
docs/             Spec, architecture, decisions, data, API, security, validation
data/             Mounted drive: downloads, SQLite DB, basemap. Gitignored.
```

## Commands

```
make setup        Install hash-pinned deps into the active conda env
make check        ruff + mypy + pytest + audit
make sync         Fetch source data and build data/curbcheck.sqlite
make serve        Run the API and UI on http://127.0.0.1:8765
make docker-*     build / sync / serve in a container (see docker-compose.yml)
curbcheck parse-report   Grammar coverage over the description corpus
```

## Where it stands (2026-09-15 snapshot)

74,590 active Manhattan signs → 95.0% snapped, 93.8% of blockface-sides
covered, 36,518 curb spans, 99.83% of regulation rows parsed by the grammar,
100% semantic / zero false-permitted on the 520-description gold set, 0 of
6,539 metered segments unpriced.

## Non-negotiables

- Server binds 127.0.0.1 only. No telemetry, no remote assets, strict CSP.
- All outbound HTTP goes through `curbcheck/net.py` and its domain allowlist.
- Downloaded data is untrusted: parameterized SQL, safe parsers, escaped output.
- Never show a confident "legal" verdict where data is missing or ambiguous.
  The raw sign text is always exposed next to any verdict.
- The sign-text parser is a deterministic grammar. No LLM at runtime (D2).
- Dependencies are hash-pinned. Adding one requires an entry in
  `docs/DEPENDENCIES.md`.
- `tests/test_boundaries.py` enforces the import boundaries: only `net.py`
  reaches the network, `etl/` and `api/` never import each other, no module
  holds a wildcard bind address.

## Environment

Conda env `curbcheck` (Python 3.12) is active in every shell. Application
packages are installed with pip from `requirements*.txt`. Data lives on the
mounted drive at `data/`; `CURBCHECK_DATA_DIR` points it elsewhere.
