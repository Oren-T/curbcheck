"""p50 latency of the two endpoints a keystroke and a search hit, over the real DB.

Run before and after a change that claims to make the server faster:

    python scripts/bench_api.py                 # data/curbcheck.sqlite
    python scripts/bench_api.py --db other.sqlite --repeats 40

The first call of each endpoint is reported separately as `cold`, because the
question this script was written for is what a *fresh* connection costs: on the
9p data mount an unwarmed page read is ~2.5 ms and a cold geocode paid for
hundreds of them. For /api/geocode the cold number is the one that matters — a
keystroke asks a question the connection has not been asked before — while for
/api/search it is the warm p50, because a user re-runs one destination with
different windows. Everything runs through the real ASGI stack (TestClient), so
the numbers include the middleware and the JSON encoding the user waits for.
"""

from __future__ import annotations

import argparse
import statistics
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path

from fastapi.testclient import TestClient
from httpx import Response

from curbcheck.api.app import create_app
from curbcheck.config import REPO_ROOT

# 1519 3 AVE is the worked example in docs/DATA.md §2.3 and the address the UX
# audit's screenshots use. "1519 3" is the house-number path mid-typing, which
# is the call the autocomplete actually makes and the one the UX pass measured
# at 339-498 ms against 20-44 ms for everything else; "86th st" is that
# everything else, kept as the contrast the number only means something against.
# The rest are the word-matching path (docs/ux/AUTOCOMPLETE_RESEARCH.md §6):
# a rare word, a long multi-word query, the one-letter prefix that matches the
# most rows in the index, and a word that is a street and a place at once.
GEOCODE_QUERIES = (
    "1519 3",
    "1519 3 av",
    "100 w",
    "86th st",
    "fashion",
    "high school of f",
    "w",
    "lex",
)

SEARCH_BODY = {
    "address": "1519 3 ave",
    "t1": "2026-09-16T10:00:00",
    "t2": "2026-09-16T12:00:00",
}
# The radii the search card offers. 30 is the widest the custom field reaches
# and the one the client's 20 s timeout has to survive.
SEARCH_RADII = (5, 10, 20, 30)


def timed(call: Callable[[], Response], repeats: int) -> tuple[float, list[float], Response]:
    """(cold call ms, warm call ms each, the cold response). The cold call is not in the list.

    The very first call is the cold one and it is the interesting number for
    /api/geocode: the autocomplete asks a different question on every keystroke,
    so a street the connection has not read yet is the normal case rather than
    the exception. Nothing may run ahead of it — checking the status code with a
    throwaway request first is what hid a 267 ms first touch behind an 9 ms
    second one.
    """
    started = time.perf_counter()
    first = call()
    cold_ms = (time.perf_counter() - started) * 1000
    warm = []
    for _ in range(repeats):
        started = time.perf_counter()
        call()
        warm.append((time.perf_counter() - started) * 1000)
    return cold_ms, warm, first


def report(name: str, cold_ms: float, warm: list[float]) -> None:
    print(
        f"{name:<28} cold {cold_ms:7.1f} ms   "
        f"p50 {statistics.median(warm):6.1f} ms   "
        f"min {min(warm):6.1f} ms   max {max(warm):6.1f} ms   n={len(warm)}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=REPO_ROOT / "data" / "curbcheck.sqlite")
    parser.add_argument("--repeats", type=int, default=25)
    args = parser.parse_args()

    app = create_app(args.db, web_dir=REPO_ROOT / "web", basemap_path=None)
    print(f"{args.db} ({args.db.stat().st_size / 1e6:.1f} MB)")

    # Entered as a context manager, so one portal and one worker-thread pool
    # serve every request the way uvicorn does. A TestClient used without
    # `with` builds a portal per request, and anything the server keeps between
    # requests — the read-only connection, its page cache — would be thrown
    # away each time and the numbers would describe a server nobody runs.
    with TestClient(app) as client:
        def geocode(query: str) -> Callable[[], Response]:
            return partial(client.get, "/api/geocode", params={"q": query})

        def search(minutes: int) -> Callable[[], Response]:
            body = {**SEARCH_BODY, "walk_minutes": minutes}
            return partial(client.post, "/api/search", json=body)

        checks: list[tuple[str, Callable[[], Response], int]] = [
            (f"GET /api/geocode?q={query}", geocode(query), args.repeats)
            for query in GEOCODE_QUERIES
        ]
        checks.append(("GET /api/health", partial(client.get, "/api/health"), args.repeats))
        # A search is seconds, not milliseconds, so it gets fewer rounds.
        checks += [
            (f"POST /api/search {minutes:>2} min", search(minutes), max(3, args.repeats // 5))
            for minutes in SEARCH_RADII
        ]
        # The page calls /api/health on load, before the user can type, so the
        # first geocode below is a first *keystroke* and not the first request
        # of the process: without this it also paid for FastAPI's first
        # response, which is ~100 ms of one-off work no user ever waits for
        # twice and which buried the number this script exists to show.
        client.get("/api/health")

        for name, call, repeats in checks:
            cold_ms, warm, response = timed(call, repeats)
            if response.status_code != 200:
                raise SystemExit(f"{name} answered {response.status_code}: {response.text[:200]}")
            report(name, cold_ms, warm)


if __name__ == "__main__":
    main()
