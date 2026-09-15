# Dependencies

Every dependency is pinned with a hash. `requirements.in` and
`requirements-dev.in` hold the human-edited list; `requirements.txt` and
`requirements-dev.txt` are compiled from them with `uv pip compile
--generate-hashes` and are the only thing ever installed. Adding a package
means adding a row here (CLAUDE.md, "Non-negotiables").

Versions below are the latest stable releases as of 2026-09-14. Pre-releases
are never used: httpx 1.0.devN and pydantic 2.14 alphas exist and are excluded.

## Runtime (`requirements.txt`)

| Package | Version | License | Why |
|---|---|---|---|
| shapely | 2.1.2 | BSD-3-Clause | Geometry: buffers, `project`/`interpolate` for linear referencing along a blockface, bbox refinement for the SQLite radius query (D4). Wheels bundle GEOS, so no system library is needed. |
| pyproj | 3.8.0 | MIT | EPSG:2263 (NY State Plane, feet) ↔ EPSG:4326 transforms. Sign offsets are in feet; the data and the map are in degrees. PROJ is bundled in the wheel. |
| fastapi | 0.141.1 | MIT | The local read-only API. Chosen for Pydantic-native request/response validation, which is the same schema layer `curbcheck/model.py` already uses. |
| uvicorn | 0.53.0 | BSD-3-Clause | ASGI server, bound to 127.0.0.1 only (SPEC §3.4). |
| h11 | 0.16.0 | MIT | HTTP/1.1 state machine under uvicorn and httpx. Pinned directly, not just transitively, because GHSA-vqfr-h8mv-ghfj (lenient `Chunked-Encoding` parsing, request smuggling) is fixed in 0.16.0. |
| pydantic | 2.13.5 | MIT | Validates every external input before the rest of the code sees it (STYLE_GUIDE §2, threat T3). Already the type system of `curbcheck/model.py`. |
| httpx | 0.28.1 | BSD-3-Clause | The single outbound HTTP client, wrapped by `curbcheck/net.py` to enforce the allowlist, TLS, redirect re-checking, and byte caps. Chosen over stdlib `urllib` for streaming plus per-phase timeouts. |

`numpy` 2.5.3 is pulled in transitively by shapely and pyproj and is not used
directly.

Nothing is installed outside these two files. `pip install -e .` runs with
`--no-build-isolation` in both `make setup` and the Dockerfile, so the build
backend is the hashed `hatchling` from `requirements-dev.txt` rather than
whatever PyPI serves at build time.

## Development (`requirements-dev.txt`)

| Package | Version | License | Why |
|---|---|---|---|
| ruff | 0.16.7 | MIT | Lint and format. `make lint` runs both; CI fails on either (STYLE_GUIDE §3). |
| mypy | 2.3.1 | MIT | `mypy --strict` over the `curbcheck` package. |
| pytest | 9.1.1 | MIT | Test runner. |
| pytest-cov | 7.1.0 | MIT | Coverage over `curbcheck`, for finding untested parser branches. |
| pip-audit | 2.10.1 | Apache-2.0 | Vulnerability scan of the pinned tree against PyPI's advisory database (threat T1). |
| uv | 0.12.14 | MIT OR Apache-2.0 | Compiles the `.in` files into the hashed `.txt` lockfiles. |
| types-shapely | 2.1.0.20260728 | Apache-2.0 | Shapely ships no inline type hints, so `mypy --strict` needs these stubs. |
| hatchling | 1.32.0 | MIT | The build backend `pyproject.toml` declares. Pinned and hashed here so `make setup` and the Dockerfile can install the package with `--no-build-isolation`: PEP 517 build isolation otherwise fetches hatchling from PyPI with no hash, which was the last install `--require-hashes` did not cover (threat T1). Brings `tomlkit` and `trove-classifiers`. |
| editables | 0.6 | MIT | Hatchling asks for it when it builds an *editable* wheel, which is what `make setup` installs; it is not a dependency of hatchling itself, so it is listed on its own. |

## Audit result

```
$ pip-audit -r requirements.txt
No known vulnerabilities found

$ pip-audit -r requirements-dev.txt
No known vulnerabilities found
```

Run on 2026-09-15 with pip-audit 2.10.1, after adding hatchling and editables.
`make audit` reruns the first of these and is part of `make check`.

## Packages the spec listed that are deliberately absent

- **duckdb** and its `spatial` extension — replaced by stdlib `sqlite3`; see
  `docs/DECISIONS.md` D4. Also removes an install-time network fetch of the
  extension binary, which the spec itself flagged (§15.8).
- **fiona / pyogrio** — only needed to read the LION file geodatabase, which
  CSCL over Socrata replaces; see D1. Dropping it removes a bundled GDAL and
  the shapefile parsers the spec's own §3.3 warns about.
- **geopandas** — the spec (§4.1) already flagged it as a rejection candidate.
  With DuckDB gone, its only role would be ETL joins over ~76k rows, which
  plain dicts and shapely do without pulling in pandas and pyarrow. See D8.
- **nyc311calendar** — rejected by the spec (§4.2); unmaintained since 2022.

## How to upgrade

1. Edit `requirements.in` or `requirements-dev.in`. Only floors go there; never
   put a hash or an exact pin in a `.in` file.
2. Recompile both lockfiles: `make compile`. Both must be recompiled together
   because `requirements-dev.in` includes `requirements.in`, and a version that
   differs between the two files makes `pip install -r` of both fail.
3. Review the diff. A new transitive package appearing is the signal to stop
   and look at what pulled it in.
4. Install: `make setup`. It uses `--require-hashes`, so any artifact that does
   not match the lockfile aborts the install.
5. Re-audit: `make audit`, and paste the result into the section above.
6. Update the tables above with the new versions, and add a row for any new
   direct dependency with its license and its reason.

Upgrades are never automatic. There is no Dependabot and no unpinned range.
