"""Re-derive `web/fonts/InterVariable-latin.woff2`, or verify the bytes that ship.

The UI's one typeface is vendored: a 72 KB Latin subset of Inter 4.1, served
from `'self'` because a font CDN would tell a third party when the page was
opened (SPEC §3.4, threat T6). This script is the recipe that produced it, so
"how was this binary made?" has an answer that runs.

    python scripts/vendor_font.py --check          # hashes only, no network
    python scripts/vendor_font.py --zip Inter-4.1.zip

Deliberately does not download anything. `curbcheck/net.py` is the only
allowlisted HTTP client in the project and `github.com` is not on its
allowlist, nor should it be: nothing in the app fetches a font
(`docs/SECURITY.md`, residual risk 6). Fetch the release yourself — the URL and
its SHA-256 are below — and hand the file to `--zip`; every byte is checked
against the recorded hash before anything is unpacked.

`fontTools` and `brotli` are used opportunistically and are not pinned
dependencies: they exist in the conda env, nothing at runtime or in `make
check` imports them, and `docs/DEPENDENCIES.md` says so. Without them this
script still verifies hashes and prints the exact `pyftsubset` command to run
elsewhere.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from curbcheck.config import REPO_ROOT

FONT_DIR = REPO_ROOT / "web" / "fonts"
OUTPUT = FONT_DIR / "InterVariable-latin.woff2"
MANIFEST = FONT_DIR / "MANIFEST.md"

RELEASE_URL = "https://github.com/rsms/inter/releases/download/v4.1/Inter-4.1.zip"
RELEASE_SHA256 = "9883fdd4a49d4fb66bd8177ba6625ef9a64aa45899767dde3d36aa425756b11e"
RELEASE_BYTES = 33_707_794

# The variable font inside the zip. `Inter-4.1/extras/ttf/` also ships static
# instances; the variable file is what carries both axes the UI asks for.
MEMBER = "InterVariable.ttf"
MEMBER_SHA256 = "4989b125924991b90d05b2d16e0e388c48f7d5bb8b30539bbf9c755278d0ccaf"
MEMBER_BYTES = 879_708

# The Google Fonts `latin` block plus the six characters this UI draws outside
# it: the cross-street arrow (U+2192), the separators · and •, the en and em
# dashes, © and § in the notice, and ✓. `web/fonts/MANIFEST.md` says which is
# which.
UNICODES = (
    "U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,"
    "U+0304,U+0308,U+0329,U+2000-206F,U+2074,U+20AC,U+2122,U+2190-2193,U+2212,"
    "U+2215,U+2022,U+25B8,U+2713,U+FEFF,U+FFFD"
)


def subset_argv(source: Path, output: Path) -> list[str]:
    """The exact `pyftsubset` call that produced the shipped file.

    `tnum` is added to the default feature set because the price and walk-time
    columns are set with `font-variant-numeric: tabular-nums`, which is a no-op
    without the feature surviving into the subset.
    """
    return [
        "pyftsubset",
        str(source),
        f"--output-file={output}",
        "--flavor=woff2",
        "--layout-features+=tnum",
        f"--unicodes={UNICODES}",
        "--name-IDs=*",
        "--name-legacy",
        "--name-languages=*",
    ]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def check_shipped() -> int:
    """Verify every file `web/fonts/MANIFEST.md` lists. Returns a process exit code."""
    rows = re.findall(
        r"^\|\s*`([^`]+)`\s*\|\s*(\d+)\s*\|\s*`([0-9a-f]{64})`",
        MANIFEST.read_text(encoding="utf-8"),
        re.M,
    )
    if not rows:
        print(f"no file rows found in {MANIFEST}", file=sys.stderr)
        return 1
    failed = False
    for name, size, want in rows:
        path = FONT_DIR / name
        actual, actual_size = sha256(path), path.stat().st_size
        ok = actual == want and actual_size == int(size)
        failed = failed or not ok
        print(f"{'ok  ' if ok else 'FAIL'} {name} {actual_size} bytes {actual}")
    return 1 if failed else 0


def rebuild(zip_path: Path) -> int:
    if sha256(zip_path) != RELEASE_SHA256 or zip_path.stat().st_size != RELEASE_BYTES:
        print(f"{zip_path} is not the recorded Inter 4.1 release", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as workspace:
        source = Path(workspace) / MEMBER
        with zipfile.ZipFile(zip_path) as archive:
            member = next(
                (name for name in archive.namelist() if name.endswith("/" + MEMBER)), None
            )
            if member is None:
                print(f"{MEMBER} is not in {zip_path}", file=sys.stderr)
                return 1
            source.write_bytes(archive.read(member))
        if sha256(source) != MEMBER_SHA256 or source.stat().st_size != MEMBER_BYTES:
            print(f"{MEMBER} in {zip_path} is not the recorded build", file=sys.stderr)
            return 1

        argv = subset_argv(source, Path(workspace) / OUTPUT.name)
        if shutil.which(argv[0]) is None:
            print("fontTools is not installed; it is not a pinned dependency. Run:")
            print("  " + " ".join(argv))
            return 1
        # argv is built entirely from constants in this file and a temp path,
        # and runs without a shell.
        subprocess.run(argv, check=True)  # noqa: S603
        produced = Path(workspace) / OUTPUT.name
        print(f"{produced.name} {produced.stat().st_size} bytes {sha256(produced)}")
        shutil.copyfile(produced, OUTPUT)
    print(f"wrote {OUTPUT}; update the table in {MANIFEST} if the hash moved")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify the shipped bytes")
    parser.add_argument("--zip", type=Path, help=f"a local copy of {RELEASE_URL}")
    args = parser.parse_args()

    if args.zip is not None:
        return rebuild(args.zip)
    if args.check:
        return check_shipped()
    print(f"Fetch {RELEASE_URL}")
    print(f"  sha256 {RELEASE_SHA256}, {RELEASE_BYTES} bytes")
    print("then rerun with --zip <file>, or --check to verify what ships.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
