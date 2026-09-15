"""Download the CurbCheck source datasets from NYC Open Data into data/raw/.

Exploration-phase tool: it writes plain JSON arrays plus a `.meta.json` sidecar
(URL, fetch time, row count, SHA-256) so later profiling can prove which
snapshot it read. The production fetcher lives in the package and goes through
`curbcheck.net`; this script is deliberately standalone (stdlib only) so it can
run before the package exists.

Usage: python scripts/explore_fetch.py [dataset ...]
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"
SOCRATA_HOST = "https://data.cityofnewyork.us"

# Socrata caps a single response well below the Manhattan row count, so every
# pull pages on :id, which is the only column guaranteed to be a stable total
# order across requests.
PAGE_SIZE = 50_000

# dataset id -> (filename stem, SoQL $where or None)
DATASETS: dict[str, tuple[str, str | None]] = {
    "nfid-uabd": ("signs_manhattan", "borough='Manhattan'"),
    "inkn-q76z": ("centerline_manhattan", "boroughcode='1'"),
    "e7yp-wx55": ("parknyc_blockfaces", None),
    "f72k-2u3b": ("meter_rate_zones", None),
    "693u-uax6": ("meters_manhattan", "borough='Manhattan'"),
}


def fetch_json(url: str, timeout: int = 300) -> list[dict[str, object]]:
    if not url.startswith("https://data.cityofnewyork.us/"):
        raise ValueError(f"refusing URL outside the Open Data host: {url}")
    request = urllib.request.Request(url, headers={"Accept": "application/json"})  # noqa: S310 - host checked above
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return json.load(response)


def fetch_dataset(dataset_id: str, where: str | None) -> tuple[list[dict[str, object]], str]:
    """Page through a Socrata resource and return (rows, first page URL)."""
    rows: list[dict[str, object]] = []
    first_url = ""
    offset = 0
    while True:
        params = {"$limit": str(PAGE_SIZE), "$offset": str(offset), "$order": ":id"}
        if where:
            params["$where"] = where
        url = f"{SOCRATA_HOST}/resource/{dataset_id}.json?{urllib.parse.urlencode(params)}"
        first_url = first_url or url
        page = fetch_json(url)
        rows.extend(page)
        print(f"  {dataset_id}: +{len(page)} rows (total {len(rows)})", file=sys.stderr)
        if len(page) < PAGE_SIZE:
            return rows, first_url
        offset += PAGE_SIZE
        time.sleep(0.5)


def write_dataset(dataset_id: str, stem: str, where: str | None) -> None:
    rows, url = fetch_dataset(dataset_id, where)
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()
    out = RAW_DIR / f"{stem}.json"
    out.write_bytes(payload)
    meta = {
        "dataset_id": dataset_id,
        "url": url,
        "where": where,
        "fetched_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "row_count": len(rows),
        "byte_size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    (RAW_DIR / f"{stem}.meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(json.dumps(meta, indent=2))


def main(argv: list[str]) -> int:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    wanted = argv or list(DATASETS)
    for dataset_id in wanted:
        stem, where = DATASETS[dataset_id]
        write_dataset(dataset_id, stem, where)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
