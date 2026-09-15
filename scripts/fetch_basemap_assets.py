"""Vendor the Protomaps basemap style, glyphs, and sprites into `web/basemap/`.

The app must render a map with zero third-party requests (SPEC §16, threat T6),
so every asset MapLibre asks for is served from `'self'`. This script is the
reproducible record of where those bytes came from.

Three sources, all fetched once and then committed:

1. The style. `@protomaps/basemaps` (CC0 styles, BSD-3 code) exports `layers()`,
   which builds the MapLibre layer list for a flavor. We run it under node to
   emit a full style whose URLs point at our own paths. The npm tarball is
   executed, so it is pinned by version and checked against the sha512 integrity
   the registry publishes before node ever sees it (threat T1).
2. The glyphs, from `protomaps/basemaps-assets`. Only the ranges listed in
   `GLYPH_RANGES`: the full 256-range set is 6.2 MB per fontstack and the style
   uses three of them (see `web/basemap/MANIFEST.md` for the consequence).
3. The v4 light sprite sheet, same repository.

Run: `python scripts/fetch_basemap_assets.py [--check]`. `--check` re-downloads
and compares hashes against the manifest instead of writing files.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "web" / "basemap"

# This script runs before (and independently of) `curbcheck.net`, whose allowlist
# covers the data pipeline. Same rule, narrower list: these two hosts only.
ALLOWED_HOSTS = frozenset({"registry.npmjs.org", "raw.githubusercontent.com"})

PACKAGE_NAME = "@protomaps/basemaps"
PACKAGE_VERSION = "5.7.2"
# `npm view @protomaps/basemaps@5.7.2 dist.integrity`, 2026-09-15. The tarball's
# code is executed by node below, so this is checked before extraction.
PACKAGE_INTEGRITY = "sha512-K1Yk6bWdULulYg+R2QRVXx4NzJZan5YQhpejEG0c1/sXruJrfPIPZuakpf3jwAgVmjIRVQwAv+yRafDeN0aaUQ=="

ASSETS_REPO = "https://raw.githubusercontent.com/protomaps/basemaps-assets/main"
# basemaps-assets publishes no releases and no tags, so `main` is the only ref.
# Pinning the commit would be better; the per-file SHA-256s in the manifest are
# what actually make a re-run verifiable.
ASSETS_REF = "main"

FLAVOR = "light"
LANGUAGE = "en"
SPRITE_VERSION = "v4"

# Served by the API from data/basemap/ (gitignored, 23 MB); see docs/DATA.md §5.
TILES_URL = "pmtiles:///basemap/manhattan.pmtiles"
GLYPHS_URL = "/basemap/fonts/{fontstack}/{range}.pbf"
SPRITE_URL = f"/basemap/sprites/{SPRITE_VERSION}/{FLAVOR}"

# Basic Latin + Latin-1 Supplement, then Latin Extended-A/B and Greek/Cyrillic,
# then General Punctuation: MapLibre asks for 8192-8447 as soon as a label
# contains an en dash (U+2013), which Manhattan street labels do.
GLYPH_RANGES = ("0-255", "256-511", "8192-8447")

SPRITE_FILES = (
    f"{FLAVOR}.json",
    f"{FLAVOR}.png",
    f"{FLAVOR}@2x.json",
    f"{FLAVOR}@2x.png",
)

MAX_TARBALL_BYTES = 8_000_000
MAX_ASSET_BYTES = 2_000_000
MAX_TOTAL_BYTES = 15_000_000

LICENSES = {
    "style.json": "CC0-1.0 (Protomaps basemap styles); generator code BSD-3-Clause",
    "fonts": "SIL Open Font License 1.1 (fonts/OFL.txt)",
    "sprites": "MIT, derived from tangrams/icons",
}


def fetch(url: str, *, max_bytes: int) -> bytes:
    """GET `url` over HTTPS from an allowlisted host, refusing oversized bodies."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https":
        raise ValueError(f"refusing non-https URL: {url}")
    if parts.hostname not in ALLOWED_HOSTS:
        raise ValueError(f"host not on the allowlist: {parts.hostname}")

    request = urllib.request.Request(  # noqa: S310 - scheme and host checked above
        url, headers={"User-Agent": "curbcheck-basemap/1"}
    )
    with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
        declared = response.headers.get("Content-Length")
        if declared is not None and int(declared) > max_bytes:
            raise ValueError(f"{url} declares {declared} bytes, over the {max_bytes} cap")
        body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ValueError(f"{url} exceeded the {max_bytes} byte cap")
    return body


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def download_package_tarball() -> bytes:
    """Fetch the pinned npm tarball and verify the registry's sha512 integrity."""
    scope, _, name = PACKAGE_NAME.partition("/")
    url = f"https://registry.npmjs.org/{scope}/{name}/-/{name}-{PACKAGE_VERSION}.tgz"
    tarball = fetch(url, max_bytes=MAX_TARBALL_BYTES)

    algorithm, _, expected = PACKAGE_INTEGRITY.partition("-")
    if algorithm != "sha512":
        raise ValueError(f"unsupported integrity algorithm {algorithm}")
    actual = base64.b64encode(hashlib.sha512(tarball).digest()).decode()
    if actual != expected:
        raise ValueError(f"integrity mismatch for {url}: got sha512-{actual}")
    return tarball


def extract_package(tarball: bytes, into: Path) -> Path:
    """Unpack the npm tarball's `package/` root, rejecting paths that escape it."""
    archive_path = into / "package.tgz"
    archive_path.write_bytes(tarball)
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile() and not member.isdir():
                raise ValueError(f"unexpected tar member type: {member.name}")
            target = (into / member.name).resolve()
            if not target.is_relative_to(into.resolve()):
                raise ValueError(f"tar member escapes the extraction directory: {member.name}")
        archive.extractall(into, filter="data")
    return into / "package"


def generate_style(package_dir: Path) -> bytes:
    """Run the package's `layers()` under node to build the full MapLibre style.

    The package's own `generate-style` CLI needs tsx and points at remote asset
    URLs, so we call the same exported function with our local paths instead.
    """
    entry = package_dir / "dist" / "esm" / "index.js"
    if not entry.is_file():
        raise FileNotFoundError(f"package is missing {entry}")

    generator = package_dir.parent / "generate_style.mjs"
    generator.write_text(
        "\n".join(
            [
                f"import {{ layers, namedFlavor }} from {json.dumps(str(entry))};",
                "const style = {",
                "  version: 8,",
                f"  name: {json.dumps(f'Protomaps {FLAVOR} (CurbCheck, self-hosted)')},",
                # Plain text, not the upstream HTML-with-links attribution: the
                # page renders its own attribution (SPEC §16) and no style URL
                # may point off-host (tests/test_web_static.py).
                "  sources: { protomaps: { type: 'vector',",
                "    attribution: '\\u00a9 OpenStreetMap contributors, Protomaps',",
                f"    url: {json.dumps(TILES_URL)} }} }},",
                f"  glyphs: {json.dumps(GLYPHS_URL)},",
                f"  sprite: {json.dumps(SPRITE_URL)},",
                f"  layers: layers('protomaps', namedFlavor({json.dumps(FLAVOR)}),"
                f" {{ lang: {json.dumps(LANGUAGE)} }}),",
                "};",
                "process.stdout.write(JSON.stringify(style, null, 2) + '\\n');",
            ]
        ),
        encoding="utf-8",
    )
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("node is required to generate the style JSON")
    # argv is an absolute interpreter path plus a file this script just wrote.
    result = subprocess.run([node, str(generator)], capture_output=True, check=True)  # noqa: S603
    return result.stdout


# A `text-font` value is either a font array or an expression that chooses
# between font arrays; only `literal` sub-expressions hold real font names.
_EXPRESSION_OPERATORS = frozenset(
    {
        "case",
        "match",
        "step",
        "literal",
        "get",
        "zoom",
        "coalesce",
        "concat",
        "==",
        "!=",
        "<",
        "<=",
        ">",
        ">=",
        "all",
        "any",
        "!",
    }
)


def _fonts_in_text_font(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list) or not value:
        return []
    head = value[0]
    if isinstance(head, str) and head not in _EXPRESSION_OPERATORS:
        return [item for item in value if isinstance(item, str)]
    if head == "literal":
        return _fonts_in_text_font(value[1]) if len(value) > 1 else []
    fonts: list[str] = []
    for operand in value[1:]:
        fonts.extend(_fonts_in_text_font(operand) if isinstance(operand, list) else [])
    return fonts


def fontstacks_in(style: dict[str, object]) -> list[str]:
    """Every font name the style can ask for, including inside `case` expressions."""
    found: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for key, value in node.items():
                if key == "text-font":
                    found.update(_fonts_in_text_font(value))
                else:
                    walk(value)

    walk(style.get("layers", []))
    return sorted(found)


def asset_url(path: str) -> str:
    return f"{ASSETS_REPO}/{urllib.parse.quote(path)}"


def collect_assets(fontstacks: Iterable[str]) -> dict[str, bytes]:
    """Download glyph ranges and sprite files, keyed by their path under web/basemap/."""
    assets: dict[str, bytes] = {}
    for fontstack in fontstacks:
        for glyph_range in GLYPH_RANGES:
            path = f"fonts/{fontstack}/{glyph_range}.pbf"
            assets[path] = fetch(asset_url(path), max_bytes=MAX_ASSET_BYTES)
    assets["fonts/OFL.txt"] = fetch(asset_url("fonts/OFL.txt"), max_bytes=MAX_ASSET_BYTES)
    for name in SPRITE_FILES:
        path = f"sprites/{SPRITE_VERSION}/{name}"
        assets[path] = fetch(asset_url(path), max_bytes=MAX_ASSET_BYTES)
    return assets


def render_manifest(assets: dict[str, bytes], fontstacks: list[str], missing: list[str]) -> str:
    total = sum(len(data) for data in assets.values())
    rows = "\n".join(
        f"| `{path}` | {len(assets[path])} | `{sha256(assets[path])}` |" for path in sorted(assets)
    )
    missing_note = (
        "The style also names "
        + ", ".join(f"`{name}`" for name in missing)
        + ", which `basemaps-assets` does not publish; MapLibre falls back to the"
        " other stacks. "
        if missing
        else ""
    )
    return f"""# Vendored basemap assets

Produced by `scripts/fetch_basemap_assets.py` on {datetime.now(UTC).date().isoformat()}.
Re-run it with `--check` to verify these bytes against the upstream sources.

Everything MapLibre loads for the basemap is in this directory, so panning the
map makes zero third-party requests (SPEC §16, threat T6). The tiles themselves
are **not** here: `data/basemap/manhattan.pmtiles` is 23 MB, gitignored, and
served by the API at `/basemap/manhattan.pmtiles` (docs/DATA.md §5).

## Attribution (required, not optional)

The tiles are a Produced Work of OpenStreetMap under the **ODbL**, so the map
must carry visible **© OpenStreetMap contributors** attribution. `web/index.html`
renders it, together with Protomaps and the NYC Open Data credit, in the map
corner. Do not remove it.

## Provenance and licensing

| Asset | Source | Version | License |
|---|---|---|---|
| `style.json` | npm `{PACKAGE_NAME}` | {PACKAGE_VERSION} | {LICENSES["style.json"]} |
| `fonts/` | `github.com/protomaps/basemaps-assets` @ `{ASSETS_REF}` | undated (no releases) | {LICENSES["fonts"]} |
| `sprites/` | `github.com/protomaps/basemaps-assets` @ `{ASSETS_REF}` | `{SPRITE_VERSION}` | {LICENSES["sprites"]} |
| tiles (elsewhere) | `build.protomaps.com` daily build | basemap 4.15.2 | ODbL, © OpenStreetMap contributors |

npm tarball integrity: `{PACKAGE_INTEGRITY}`.

`style.json` was generated by calling the package's exported `layers()` with the
`{FLAVOR}` flavor and `lang={LANGUAGE}`, then pointing the source at
`{TILES_URL}`, glyphs at `{GLYPHS_URL}`, and the sprite at `{SPRITE_URL}`. The
style's `source-layer` names (boundaries, buildings, earth, landcover, landuse,
places, pois, roads, water) match the vector layers in the vendored PMTiles
archive exactly.

## Glyph coverage (a deliberate limitation)

Only the ranges {", ".join(f"`{r}`" for r in GLYPH_RANGES)} are vendored, for the
fontstacks the style actually uses ({", ".join(f"`{f}`" for f in fontstacks)}).
The full 256-range set is about 6.2 MB per fontstack, which would put this
directory over 18 MB for labels no Manhattan basemap draws. Consequence: a label
whose text falls outside Latin/Greek/Cyrillic — a CJK POI name in Chinatown, for
instance — renders blank and the browser logs a 404 for that glyph range. To fix
it, add the range to `GLYPH_RANGES` and re-run the script. {missing_note}

## Files

Total {total} bytes.

| File | Bytes | SHA-256 |
|---|---|---|
{rows}
"""


def write_outputs(assets: dict[str, bytes]) -> None:
    for path, data in assets.items():
        target = OUT_DIR / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def check_outputs(assets: dict[str, bytes]) -> int:
    problems = 0
    for path, data in sorted(assets.items()):
        target = OUT_DIR / path
        if not target.is_file():
            print(f"missing   {path}")
            problems += 1
        elif target.read_bytes() != data:
            print(f"differs   {path}")
            problems += 1
    print(f"checked {len(assets)} files, {problems} problem(s)")
    return 1 if problems else 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check", action="store_true", help="compare with the vendored files instead of writing"
    )
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory() as work:
        package_dir = extract_package(download_package_tarball(), Path(work))
        style_bytes = generate_style(package_dir)

    style = json.loads(style_bytes)
    wanted = fontstacks_in(style)
    available = json.loads(fetch(asset_url("fonts.json"), max_bytes=MAX_ASSET_BYTES))
    fontstacks = [name for name in wanted if name in available]
    missing = [name for name in wanted if name not in available]

    assets = {"style.json": style_bytes, **collect_assets(fontstacks)}
    assets["MANIFEST.md"] = render_manifest(assets, fontstacks, missing).encode("utf-8")

    total = sum(len(data) for data in assets.values())
    if total > MAX_TOTAL_BYTES:
        raise ValueError(f"assets total {total} bytes, over the {MAX_TOTAL_BYTES} cap")

    if args.check:
        return check_outputs(assets)
    write_outputs(assets)
    print(f"wrote {len(assets)} files ({total} bytes) to {OUT_DIR}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (urllib.error.URLError, ValueError, RuntimeError) as error:
        print(f"basemap asset fetch failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
