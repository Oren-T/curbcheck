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
- `docs/DECISIONS.md` — every departure from the spec, with evidence.
- `docs/ARCHITECTURE.md` — module map and data flow.
- `docs/DATA.md` — measured facts about the source datasets.

## Layout

```
curbcheck/        Python package (ETL, engine, API)
web/              Static frontend (plain ES modules, vendored libs)
tests/            pytest suite
scripts/          One-off developer scripts
docs/             Spec, architecture, decisions, data notes
data/             Mounted drive for downloads and the SQLite DB. Gitignored.
```

## Commands

```
make setup        Install hash-pinned deps into the active conda env
make check        ruff + mypy + pytest + audit
make sync         Fetch source data and build data/curbcheck.sqlite
make serve        Run the API and UI on http://127.0.0.1:8765
```

## Non-negotiables

- Server binds 127.0.0.1 only. No telemetry, no remote assets, strict CSP.
- All outbound HTTP goes through `curbcheck/net.py` and its domain allowlist.
- Downloaded data is untrusted: parameterized SQL, safe parsers, escaped output.
- Never show a confident "legal" verdict where data is missing or ambiguous.
  The raw sign text is always exposed next to any verdict.
- Dependencies are hash-pinned. Adding one requires an entry in
  `docs/DEPENDENCIES.md`.

## Environment

Conda env `curbcheck` (Python 3.12) is active in every shell. Application
packages are installed with pip from `requirements*.txt`. Data lives on the
mounted drive at `data/`.
