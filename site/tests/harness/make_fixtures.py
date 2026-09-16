"""Run the harness query set through the Python engine and write the reference answers.

The point of this script is that it is not a second implementation of anything.
It calls `curbcheck.api.routes`' own handler functions, so what lands in
`expected.json` is the JSON the local server would have returned for the same
request — including the private payload helpers, which are imported rather than
copied for exactly that reason (`docs/STATIC_SITE.md`, "The differential
harness"). Two things FastAPI does are not in those functions and are done here:
turning the request into a `SearchRequest`, and enforcing the `Query(...)`
bounds on the geocode and reverse parameters. Both are named below.

Nothing in the output is a clock or a path, so two runs over one database
produce the same bytes. `make check` does not run this; the pages workflow does,
between `curbcheck pack` and `node --test`.

    python site/tests/harness/make_fixtures.py [--db DB] [--out FILE] [--pack DIR]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pydantic import TypeAdapter, ValidationError

# Run as a script, so the harness package directory is already sys.path[0].
from queries import SEED, Query, build_queries, result_span_ids, segment_queries

from curbcheck.api import routes
from curbcheck.api.errors import ApiError
from curbcheck.api.schemas import MAX_LAT, MAX_LON, MIN_LAT, MIN_LON, SearchRequest
from curbcheck.config import DB_PATH
from curbcheck.db import connect
from curbcheck.geocode import MAX_QUERY_CHARS

DEFAULT_OUT = Path(__file__).resolve().parent / "expected.json"
FORMAT = 1

# What FastAPI does to `t1: datetime | None = None` before `get_segment` sees
# it. A string this rejects is a 422 `validation_error` on the server.
_OPTIONAL_DATETIME: TypeAdapter[datetime | None] = TypeAdapter(datetime | None)

# `meta.json` counts against the tables they are counted from, so a fixture
# built from one database and a pack built from another cannot pass the gate
# quietly (`docs/STATIC_SITE.md`: the harness runs "on the data that ships").
_PACK_COUNT_SQL = {
    "spans": "SELECT count(*) FROM regulation_segment",
    "regulations": "SELECT count(*) FROM regulation",
    "signs": "SELECT count(*) FROM sign",
    "segments": "SELECT count(*) FROM street_segment",
}
_PROGRESS_EVERY = 100


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    conn = connect(args.db, readonly=True)
    request = _request(args.db)
    if args.pack is not None:
        _check_pack_matches(conn, args.pack)

    started = time.perf_counter()
    records: list[dict[str, Any]] = []
    sources: list[tuple[dict[str, Any], list[str]]] = []
    queries = build_queries(conn)
    for index, query in enumerate(queries):
        record = _run(request, query)
        records.append(record)
        if query.op == "search" and "result" in record:
            sources.append((query.args, result_span_ids(record["result"])))
        _progress(index, len(queries))

    spans = segment_queries(sources)
    for index, query in enumerate(spans):
        records.append(_run(request, query))
        _progress(index, len(spans))
    elapsed = time.perf_counter() - started
    conn.close()

    _write(args.out, records, conn_counts=_database_summary(args.db))
    _report(records, args.out, elapsed)
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=DB_PATH, help="the SQLite file to read")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="where to write the fixture")
    parser.add_argument(
        "--pack",
        type=Path,
        default=None,
        help="a pack directory to check the database against before running",
    )
    return parser.parse_args(argv)


def _request(db_path: Path) -> Any:
    """Enough of a FastAPI `Request` for the handlers: they read one attribute.

    `routes.open_database` reaches for `request.app.state.db_path` and nothing
    else, so this is what lets the fixture come out of the real handlers
    instead of out of a copy of their bodies.
    """
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_path=db_path)))


def _run(request: Any, query: Query) -> dict[str, Any]:
    """One query's record: the args as sent, and either the API JSON or the error code.

    Only the two failures the API is allowed to have are caught. Anything else
    is a bug in the engine or in the query set, and crashing here is better
    than writing a wrong expectation into the gate.
    """
    record: dict[str, Any] = {"id": query.id, "op": query.op, "args": query.args}
    try:
        record["result"] = _RUNNERS[query.op](request, query.args)
    except ApiError as error:
        record["error"] = {"code": error.code}
    except ValidationError:
        # What FastAPI's RequestValidationError handler turns into (app.py).
        record["error"] = {"code": "validation_error"}
    return record


def _run_search(request: Any, args: dict[str, Any]) -> dict[str, Any]:
    return routes.post_search(request, SearchRequest.model_validate(args))


def _run_segment(request: Any, args: dict[str, Any]) -> dict[str, Any]:
    t1 = _OPTIONAL_DATETIME.validate_python(args.get("t1"))
    t2 = _OPTIONAL_DATETIME.validate_python(args.get("t2"))
    return routes.get_segment(request, str(args["reg_seg_id"]), t1, t2)


def _run_geocode(request: Any, args: dict[str, Any]) -> dict[str, Any]:
    query = str(args["q"])
    # `Query(min_length=1, max_length=MAX_QUERY_CHARS)` on the parameter, which
    # is FastAPI's job and therefore not inside `get_geocode`.
    if not 1 <= len(query) <= MAX_QUERY_CHARS:
        raise ApiError(422, "validation_error", "q: out of range")
    return routes.get_geocode(request, query)


def _run_reverse(request: Any, args: dict[str, Any]) -> dict[str, Any]:
    lat, lon = float(args["lat"]), float(args["lon"])
    # `Query(ge=MIN_LAT, le=MAX_LAT)` and the same for `lon`, as above.
    if not MIN_LAT <= lat <= MAX_LAT or not MIN_LON <= lon <= MAX_LON:
        raise ApiError(422, "validation_error", "lat/lon: out of range")
    return routes.get_reverse(request, lat=lat, lon=lon)


_RUNNERS = {
    "search": _run_search,
    "segment": _run_segment,
    "geocode": _run_geocode,
    "reverse": _run_reverse,
}


def _check_pack_matches(conn: sqlite3.Connection, pack_dir: Path) -> None:
    meta = json.loads((pack_dir / "meta.json").read_text(encoding="utf-8"))
    counts = meta.get("counts", {})
    for name, sql in _PACK_COUNT_SQL.items():
        if name not in counts:
            continue
        rows = int(conn.execute(sql).fetchone()[0])
        if int(counts[name]) != rows:
            raise SystemExit(
                f"pack and database disagree: {name} is {counts[name]} in meta.json and "
                f"{rows} in the database; the harness must run on the data that ships"
            )


def _database_summary(db_path: Path) -> dict[str, Any]:
    """What the fixture says about its own source. No path: `docs/SECURITY.md`."""
    conn = connect(db_path, readonly=True)
    try:
        counts = {
            name: int(conn.execute(sql).fetchone()[0]) for name, sql in _PACK_COUNT_SQL.items()
        }
        row = conn.execute("SELECT value FROM sync_meta WHERE key = 'last_sync_at'").fetchone()
    finally:
        conn.close()
    return {**counts, "last_sync_at": None if row is None else str(row[0])}


def _write(out: Path, records: list[dict[str, Any]], *, conn_counts: dict[str, Any]) -> None:
    payload = {
        "format": FORMAT,
        "seed": SEED,
        "database": conn_counts,
        "counts": _counts(records),
        "queries": records,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as handle:
        # `allow_nan=False`: a NaN would be written as a bare `NaN`, which is
        # not JSON and which `JSON.parse` refuses, so it fails here instead.
        json.dump(payload, handle, allow_nan=False)


def _counts(records: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    answered: Counter[str] = Counter()
    refused: Counter[str] = Counter()
    for record in records:
        (answered if "result" in record else refused)[record["op"]] += 1
    return {
        op: {"answered": answered[op], "refused": refused[op], "total": answered[op] + refused[op]}
        for op in sorted(set(answered) | set(refused))
    }


def _report(records: list[dict[str, Any]], out: Path, elapsed: float) -> None:
    codes: Counter[str] = Counter(
        record["error"]["code"] for record in records if "error" in record
    )
    for op, count in _counts(records).items():
        print(f"{op:<8} {count['total']:>5} queries, {count['refused']:>4} refused")
    print("error codes: " + ", ".join(f"{code}={n}" for code, n in sorted(codes.items())))
    print(f"{len(records)} queries in {elapsed:.1f}s -> {out} ({out.stat().st_size / 1e6:.1f} MB)")


def _progress(index: int, total: int) -> None:
    if index % _PROGRESS_EVERY == 0:
        print(f"  {index}/{total}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
