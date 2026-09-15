"""Cut a Manhattan-only Protomaps basemap out of the daily planet build.

`pmtiles extract` reads the 138 GB planet archive over HTTP range requests and
writes only the tiles inside the bounding box, so the transfer is ~24 MB rather
than the whole planet. The build bucket has no directory listing; the manifest
the Protomaps builds page itself fetches is the only way to learn the current
key, and it is not the `build.protomaps.com/builds.json` URL the spec cites
(that one 404s).

Run with --extract to perform the download; without it the script only prints
the commands so they can be reviewed first.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

BUILDS_MANIFEST = "https://build-metadata.protomaps.dev/builds.json"
BUILD_BUCKET = "https://build.protomaps.com"

# Manhattan plus a margin for Roosevelt Island and the approaches.
BBOX = "-74.03,40.68,-73.90,40.88"
MAX_ZOOM = 15

OUTPUT = Path(__file__).resolve().parent.parent / "data" / "basemap" / "manhattan.pmtiles"


def latest_build() -> dict[str, object]:
    # The bucket's CDN rejects the default urllib User-Agent with 403.
    headers = {"Accept": "application/json", "User-Agent": "curbcheck-explore/0"}
    request = urllib.request.Request(BUILDS_MANIFEST, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        builds = json.load(response)
    return max(builds, key=lambda b: str(b["uploaded"]))


def main(argv: list[str]) -> int:
    build = latest_build()
    source = f"{BUILD_BUCKET}/{build['key']}"
    command = [
        "pmtiles",
        "extract",
        source,
        str(OUTPUT),
        f"--bbox={BBOX}",
        f"--maxzoom={MAX_ZOOM}",
    ]
    print(
        f"latest build   {build['key']}  version {build['version']}  "
        f"{int(build['size']) / 1e9:.0f} GB  uploaded {build['uploaded']}"
    )
    print(f"blake3         {build['b3sum']}")
    print("command        " + " ".join(command))
    if "--extract" not in argv:
        print("\n(dry run; pass --extract to download)")
        return 0
    if shutil.which("pmtiles") is None:
        print("pmtiles CLI not on PATH; see docs/DATA.md for the release URL", file=sys.stderr)
        return 1
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    return subprocess.run(command, check=False).returncode  # noqa: S603 - argv built from literals


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
