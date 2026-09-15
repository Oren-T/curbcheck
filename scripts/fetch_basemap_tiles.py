"""Cut a Manhattan-only Protomaps basemap out of the daily planet build.

`pmtiles extract` reads the 138 GB planet archive over HTTP range requests and
writes only the tiles inside the bounding box, so the transfer is ~24 MB rather
than the whole planet. The build bucket has no directory listing; the manifest
the Protomaps builds page itself fetches is the only way to learn the current
key, and it is not the `build.protomaps.com/builds.json` URL the spec cites
(that one 404s).

This is the supported way to get `data/basemap/manhattan.pmtiles`, the one
basemap artifact that is not vendored (23 MB, gitignored). Its companion,
`scripts/fetch_basemap_assets.py`, vendors the style, glyphs and sprites.

Run: `python scripts/fetch_basemap_tiles.py [--dry-run]`. Needs the `pmtiles`
CLI on PATH; see docs/DATA.md §6 for the release URL.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

BUILDS_MANIFEST = "https://build-metadata.protomaps.dev/builds.json"
BUILD_BUCKET = "https://build.protomaps.com"

# Same rule as scripts/fetch_basemap_assets.py, narrower list: this script runs
# outside `curbcheck.net` and its allowlist, so it carries its own.
ALLOWED_HOSTS = frozenset({"build-metadata.protomaps.dev"})

# A build key is a date and nothing else: `20260914.pmtiles`. The value comes
# out of a remote JSON document and goes into a subprocess argv, so it is
# matched against this before it is used at all — an argv is not a shell, but a
# key of `--config=/etc/passwd` would still be an argument we did not mean to
# pass (docs/SECURITY.md, threat T3).
BUILD_KEY_PATTERN = re.compile(r"\d{8}\.pmtiles")

# Manhattan plus a margin for Roosevelt Island and the approaches.
BBOX = "-74.03,40.68,-73.90,40.88"
MAX_ZOOM = 15

MAX_MANIFEST_BYTES = 4_000_000

OUTPUT = Path(__file__).resolve().parent.parent / "data" / "basemap" / "manhattan.pmtiles"


def latest_build() -> dict[str, object]:
    """The newest entry in the builds manifest, fetched over https from the one host."""
    parts = urllib.parse.urlsplit(BUILDS_MANIFEST)
    if parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS:
        raise ValueError(f"refusing to fetch {BUILDS_MANIFEST}")

    # The bucket's CDN rejects the default urllib User-Agent with 403.
    headers = {"Accept": "application/json", "User-Agent": "curbcheck-basemap/1"}
    request = urllib.request.Request(BUILDS_MANIFEST, headers=headers)  # noqa: S310
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        builds = json.loads(response.read(MAX_MANIFEST_BYTES + 1).decode("utf-8"))
    if not isinstance(builds, list) or not builds:
        raise ValueError("builds manifest is not a non-empty list")
    return max(builds, key=lambda build: str(build["uploaded"]))


def checked_key(build: dict[str, object]) -> str:
    key = str(build.get("key", ""))
    if not BUILD_KEY_PATTERN.fullmatch(key):
        raise ValueError(f"build key is not a dated pmtiles name: {key!r}")
    return key


def extract_command(key: str) -> list[str]:
    return [
        "pmtiles",
        "extract",
        f"{BUILD_BUCKET}/{key}",
        str(OUTPUT),
        f"--bbox={BBOX}",
        f"--maxzoom={MAX_ZOOM}",
    ]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run", action="store_true", help="print the command instead of running it"
    )
    args = parser.parse_args(argv)

    build = latest_build()
    command = extract_command(checked_key(build))
    print(
        f"latest build   {build['key']}  version {build['version']}  "
        f"{int(build['size']) / 1e9:.0f} GB  uploaded {build['uploaded']}"
    )
    print(f"blake3         {build['b3sum']}")
    print("command        " + " ".join(command))
    if args.dry_run:
        return 0
    if shutil.which("pmtiles") is None:
        print("pmtiles CLI not on PATH; see docs/DATA.md for the release URL", file=sys.stderr)
        return 1
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    # argv, not a shell, and every element is a literal or a validated key.
    return subprocess.run(command, check=False).returncode  # noqa: S603


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
