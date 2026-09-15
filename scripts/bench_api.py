"""p50 latency of the two endpoints a keystroke and a search hit, over the real DB.

Run before and after a change that claims to make the server faster:

    python scripts/bench_api.py                 # data/curbcheck.sqlite
    python scripts/bench_api.py --db other.sqlite --repeats 40

The first call of each endpoint is reported separately as `cold`, because the
question this script was written for is what a *fresh* connection costs: on the
9p data mount an unwarmed page read is ~2.5 ms and a cold geocode paid for
hundreds of them. Everything runs through the real ASGI stack (TestClient), so
the numbers include the middleware and the JSON encoding the user waits for.
"""

from __future__ import annotations

import argparse
import statistics
import time
from pathlib import Path

from fastapi.testclient import TestClient

from curbcheck.api.app import create_app
from curbcheck.config import REPO_ROOT

# 1519 3 AVE is the worked example in docs/DATA.md §2.3 and the address the UX
# audit's screenshots use; "1519 3" is the prefix mid-typing, which is the call
# the autocomplete actually makes.
GEOCODE_QUERY = "1519 3"
SEARCH_BODY = {
    "address": "1519 3 ave",
    "t1": "2026-09-16T10:00:00",
    "t2": "2026-09-16T12:00:00",
    "walk_minutes": 10,
}


def timed(call: object, repeats: int) -> tuple[float, list[float]]:
    """(cold call ms, warm call ms each). The cold call is not in the list."""
    started = time.perf_counter()
    call()  # type: ignore[operator]
    cold_ms = (time.perf_counter() - started) * 1000
    warm = []
    for _ in range(repeats):
        started = time.perf_counter()
        call()  # type: ignore[operator]
        warm.append((time.perf_counter() - started) * 1000)
    return cold_ms, warm


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
        checks = (
            (
                f"GET /api/geocode?q={GEOCODE_QUERY}",
                lambda: client.get("/api/geocode", params={"q": GEOCODE_QUERY}),
                args.repeats,
            ),
            ("GET /api/health", lambda: client.get("/api/health"), args.repeats),
            # A search is seconds, not milliseconds, so it gets fewer rounds.
            (
                "POST /api/search",
                lambda: client.post("/api/search", json=SEARCH_BODY),
                max(3, args.repeats // 4),
            ),
        )
        for name, call, repeats in checks:
            response = call()
            if response.status_code != 200:
                raise SystemExit(f"{name} answered {response.status_code}: {response.text[:200]}")
            cold_ms, warm = timed(call, repeats)
            report(name, cold_ms, warm)


if __name__ == "__main__":
    main()
