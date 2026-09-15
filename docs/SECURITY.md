# Security

CurbCheck ingests a few hundred thousand rows of public NYC data over the
internet and then serves them to a browser on `127.0.0.1`. Those two sentences
are the whole threat model: **everything downloaded is hostile until proven
otherwise, and the only party who should be able to reach the server is the
person sitting at the machine.**

`docs/SPEC.md` §3 is the binding version. This file says where each control
actually lives, how to check it yourself, and what is knowingly left open.

## The six threats, in one line each

| | Threat | What we do about it |
|---|---|---|
| **T1** | A dependency is hijacked upstream | Every package is hash-pinned and installed with `--require-hashes`; `pip-audit` runs in `make check` and in CI; the frontend libraries are checked in with SHA-256s, not fetched. |
| **T2** | A data endpoint is spoofed or MITM'd | One HTTP client, HTTPS only, certificate verification with no way to turn it off, a six-host allowlist re-checked on every redirect hop, byte caps, and a SHA-256 of every artifact in a manifest. The previous database survives as `.prev`. |
| **T3** | Hostile strings ride in on the data | Explicit Pydantic schemas at the boundary, stdlib `json` only, no `eval`/`exec`/`pickle`/`yaml`/`subprocess` anywhere near a downloaded value, every SQL statement parameterized, spreadsheet-formula leads neutralized, and a frontend with no markup-building code path at all. |
| **T4** | The local server is reached or abused | Binds `127.0.0.1` with no flag or environment variable that can widen it, one worker, no `--reload`, no `/docs`, a strict CSP on every response, no CORS, and a read-only SQLite connection. |
| **T5** | Prompt injection through sign text | Not applicable: there is no LLM. `docs/DECISIONS.md` D2 removed the fallback parser, which removed the threat. |
| **T6** | The user's destinations leak | No analytics, no telemetry, no crash reporting, no cookies, no `localStorage`, no remote asset of any kind. The geocoder and its autocomplete are local (D29), so not even the typing leaks; the basemap is a file on disk. After a sync the app never opens a socket. |

## Where each control lives

**Network egress (T2, T6)**

- `curbcheck/net.py:check_url` — https-only, host must be in the allowlist. It
  reads `urlsplit(...).hostname`, so userinfo (`https://data.cityofnewyork.us@evil/`),
  case, and ports cannot disguise a host.
- `curbcheck/net.py:AllowlistedClient` — `follow_redirects=False` and a manual
  hop loop, so `check_url` runs again on every `Location`. `verify=True` is
  hard-coded and there is no parameter to override it.
- `curbcheck/net.py:AllowlistedClient.download` / `_read_capped` — byte caps,
  content-type check, SHA-256, atomic rename via a temp file.
- `curbcheck/config.py:ALLOWED_HOSTS` — the six hosts, a module constant, not
  user-editable at runtime.
- `tests/test_boundaries.py` — proves `net.py` is the only module in the
  package that imports a network library.
- Frontend: `web/index.html` loads only same-directory files; the one remote
  URL in the repo is the visible OpenStreetMap attribution link, which carries
  `rel="noopener noreferrer"`. `web/basemap/style.json` points at `/basemap/…`
  and `pmtiles:///basemap/…`. The vendored MapLibre and PMTiles builds contain
  no telemetry endpoint, no `sendBeacon`, and no absolute URL that is fetched.

**Untrusted data (T3)**

- `curbcheck/etl/stage.py:stage_signs` — every row through `RawSignRow`
  (Pydantic) before anything else touches it.
- `curbcheck/etl/stage.py:neutralize_formula` — applied to `sign_description`,
  `order_number`, the three street names, `sign_code` and `sign_notes`.
- `curbcheck/etl/parse/__init__.py:parse_description` — never raises, never
  guesses; an unreadable string becomes `ParseMethod.UNPARSED` and the segment
  goes amber (`docs/DECISIONS.md` D13).
- `curbcheck/db.py:placeholders` — the one sanctioned way to size an
  `IN (...)` clause. `tests/test_engine_sql_safety.py` walks the AST of every
  module and fails on an f-string, a `%`, a `.format()`, or a `+` near SQL.
- `curbcheck/api/routes.py` — returns stored text byte for byte. This is
  deliberate (`docs/API.md`): escaping twice hides what the sign really says.
- `web/dom.js:el` — every string reaches the page through `textContent`.
  `tests/test_web_static.py` fails the build if `innerHTML`, `outerHTML`,
  `document.write`, `eval(` or `new Function` appears in any file we wrote.
  There are no MapLibre popups; GeoJSON goes into the map as a data source.

**Local surface (T4)**

- `curbcheck/config.py:BIND_HOST` and `curbcheck/cli.py:_serve` — `127.0.0.1`,
  `workers=1`, no reload flag, and deliberately no `--host` option.
- `curbcheck/api/app.py:SecurityHeadersMiddleware` / `security_headers` — CSP,
  `nosniff` and `Referrer-Policy` on *every* response, `Cache-Control: no-store`
  on `/api/*`. Written as raw ASGI so the streaming basemap response keeps them
  on 206 and 416 too.
- `curbcheck/api/app.py:create_app` — `docs_url=None`, `redoc_url=None`,
  `openapi_url=None`; no CORS middleware anywhere.
- `curbcheck/api/schemas.py` — `extra="forbid"`, bounded lat/lon, a 5-minute to
  24-hour window, `walk_minutes` 1–30, `limit` 1–500, `address` and `q` 1–120
  characters.
- `curbcheck/api/routes.py:get_segment` — `REG_SEG_ID_PATTERN` rejects a bad id
  with a 400 before any query is built.
- `curbcheck/api/errors.py` — one error shape; messages never carry a path, a
  traceback, or SQL. Tracebacks go to the log.
- `curbcheck/api/app.py:AccessLogMiddleware` — the only request log. Method,
  path, status, duration; never a query string, so the addresses typed into the
  autocomplete stay inside the process (T6).
- `curbcheck/db.py:connect` — the server opens `mode=ro`, so a bug in a handler
  cannot write to the database.

## How to verify it yourself

```sh
make check                  # ruff + mypy --strict + pytest + pip-audit
pytest -m slow              # 20k-input fuzz of the three text parsers
pip-audit -r requirements.txt -r requirements-dev.txt

# The vendored frontend bytes are what the manifests claim:
python - <<'EOF'
import hashlib, pathlib, re
for manifest, base in (("web/vendor/MANIFEST.md", "web/vendor"),
                       ("web/basemap/MANIFEST.md", "web/basemap")):
    rows = re.findall(r'^\|\s*`([^`]+)`\s*\|\s*(\d+)\s*\|\s*`([0-9a-f]{64})`',
                      pathlib.Path(manifest).read_text(), re.M)
    for rel, size, want in rows:
        data = (pathlib.Path(base) / rel).read_bytes()
        assert len(data) == int(size) and hashlib.sha256(data).hexdigest() == want, rel
    print(manifest, len(rows), "files verified")
EOF

# The running server's headers, and that nothing else is listening:
curl -sI http://127.0.0.1:8765/api/health | grep -i -e content-security -e nosniff -e no-store
ss -ltnp | grep 8765         # expect 127.0.0.1:8765, never 0.0.0.0:8765
```

In the browser, open devtools with the network panel filtered to third-party
and pan the map: there should be zero requests off `127.0.0.1`.

## Residual risks we accept

1. **The frontend is the only escaping boundary.** The API returns
   `<script>` as `<script>`. `X-Content-Type-Options: nosniff`, the
   `application/json` content type, the CSP, and `web/dom.js` each have to hold
   for that to be safe. Three of the four are tested; the trade buys the raw
   sign text being visible next to every verdict, which `CLAUDE.md` requires.
2. **Closed.** `GET /api/geocode?q=…` used to put the typed address in
   uvicorn's access log, which wrote the full request line at INFO.
   `curbcheck/cli.py:_serve` now passes `access_log=False`, and
   `curbcheck/api/app.py:AccessLogMiddleware` logs `method path status
   duration_ms` in its place. It reads `scope["path"]`, which by ASGI definition
   excludes the query string, so the query bytes are never in hand rather than
   being stripped. `tests/test_api_security.py::test_the_typed_address_never_reaches_the_request_log`
   pins it.
3. **No request-body size limit.** The fields are bounded (`extra="forbid"` plus
   `max_length`), so an oversized body is a 422, but Starlette buffers it first.
   A local process could make the server hold a very large body in memory.
4. **A maximal search costs about 5–7 seconds.** 30-minute walk radius, a
   24-hour window and `limit=500` against the real Manhattan database, on one
   worker. Bounded, but a handful of concurrent maximal queries will queue.
5. **Formula neutralization covers the sign columns only.** `street_name`,
   `rate_label` and the ASP `label` are stored as they arrive. Nothing in v1
   exports a CSV, so there is no formula to execute; add the neutralization at
   the same time as any export feature.
6. **`scripts/` does not go through `curbcheck/net.py`.** The exploration and
   basemap scripts use `urllib` with their own https-and-host checks. They are
   developer tools, are excluded from lint and from the package, and are not
   reachable from `curbcheck sync` or `curbcheck serve`. The two rough edges
   recorded here are fixed: `scripts/fetch_basemap_tiles.py` (was
   `explore_basemap.py`) checks scheme and host on its one constant URL, and
   matches the build key it takes from the remote manifest against
   `\d{8}\.pmtiles` before it reaches the `subprocess` argv
   (`tests/test_scripts_basemap.py`). What remains, by design: the hosts these
   scripts use — `registry.npmjs.org`, `raw.githubusercontent.com`,
   `build-metadata.protomaps.dev` — are **not** in `config.ALLOWED_HOSTS` and
   should not be, because nothing in the app fetches them; `docs/DECISIONS.md`
   D11 said they were added to the allowlist and carries the correction.
7. **`AllowlistedClient(transport=…)` accepts any transport.** It exists so
   tests can inject a `MockTransport`. Passing a real transport with
   `verify=False` would defeat the module; nothing does, and
   `tests/test_boundaries.py` keeps httpx out of every other module.
8. **The allowlist is by host, not host and port.** CI pins actions by major
   tag (`actions/checkout@v4`); pinning by commit SHA would be stronger. The
   unhashed `hatchling` that PEP 517 build isolation used to fetch is closed:
   `hatchling` and `editables` are pinned and hashed in `requirements-dev.txt`
   and both `make setup` and the Dockerfile build with `--no-build-isolation`,
   so every byte installed comes from a hashed lockfile.
9. **Closed.** An unreadable `geom` cell used to fail the whole search:
   `engine/search.py:_candidates_in_radius` called `json.loads` and `shape()` on
   `regulation_segment.geom` without a guard, so a truncated snapshot answered
   500. `_measure` now returns `None` for a row it cannot read; the row is
   skipped and the count goes to the log as **one** warning per query, not one
   per row. Dropping a span does hide curb the user asked about, so the count is
   logged rather than swallowed — but a search that answers about the rest of
   the neighbourhood is worth more than one that answers about none of it.
10. **The data itself is only as good as NYC's.** That is a correctness risk, not
   a security one, and `docs/SPEC.md` §11 and §17 are where it is handled.

## Reporting

This is a single-user tool with no deployment and no users but its owner. If you
find a problem, open an issue in this repository, or if it is sensitive, contact
the repository owner directly rather than filing publicly. There is no bounty
and no SLA.
