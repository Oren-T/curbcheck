"""Assemble `dist/`: the same frontend, served by GitHub Pages instead of uvicorn.

Seven steps, in the order `docs/STATIC_SITE.md` ("The build") lists them: copy
`web/` and re-verify its manifests, overlay `site/static/`, copy the pack and
the sliced tiles, point the basemap at the base path, inject the CSP that the
server sends as a header, drop the PMTiles shim, and print what it all weighs.

Standard library only, and every rewrite asserts how many occurrences it
expected: this file edits the frontend from the outside, so it has to fail
loudly the day the frontend changes underneath it rather than ship a page whose
basemap silently 404s.

Run: `python site/build.py --out dist --pack build/pack --tiles build/tiles --base-path /`
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = REPO_ROOT / "web"
STATIC_DIR = REPO_ROOT / "site" / "static"

# The three vendored directories that carry a MANIFEST.md of SHA-256s
# (docs/SECURITY.md, T1). Verified over the copy, so a build cannot ship bytes
# the repository does not vouch for.
MANIFEST_DIRS = ("vendor", "basemap", "fonts")
MANIFEST_ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*(\d+)\s*\|\s*`([0-9a-f]{64})`", re.MULTILINE)

# What `curbcheck/api/app.py` sends as a header and Pages cannot. `style-src`
# drops 'unsafe-inline' because MapLibre styles through CSSOM property
# assignment, which CSP does not govern; `worker-src` keeps `blob:` because
# MapLibre's fallback worker is one. `frame-ancestors` is not expressible in a
# meta tag — docs/STATIC_SITE.md, "Security, restated", accepts that.
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data: blob:; connect-src 'self'; worker-src 'self' blob:; "
    "object-src 'none'; base-uri 'none'"
)
CSP_META = f'<meta http-equiv="Content-Security-Policy" content="{CSP}" />'
REFERRER_META = '<meta name="referrer" content="no-referrer" />'

STYLE_URL_LINE = 'const STYLE_URL = "/basemap/style.json";'

# The whole block, comment included: the comment explains a script that is
# about to stop existing.
PMTILES_SCRIPT_BLOCK = """    <!-- pmtiles.js is the browser IIFE build and must define the `pmtiles`
         global before map.js registers the protocol (web/vendor/MANIFEST.md). -->
    <script src="./vendor/pmtiles/pmtiles.js"></script>
"""
PMTILES_REPLACEMENT = (
    "    <!-- The basemap protocol shim is not shipped in this build; the tiles\n"
    '         are files. See docs/STATIC_SITE.md, "The build". -->\n'
)

# Printed individually in step 7. Below this a file is only interesting in its
# directory's total.
LARGE_FILE_BYTES = 100 * 1024


class BuildError(RuntimeError):
    """The build cannot produce a correct `dist/` and refuses to produce a wrong one."""


@dataclass(frozen=True)
class TileSource:
    """What the vector source in the rewritten style needs to say."""

    bounds: list[float]
    min_zoom: int
    max_zoom: int
    tiles: int


# --- Step 1: web/ ----------------------------------------------------------


def manifest_rows(manifest: Path) -> list[tuple[str, int, str]]:
    rows = [
        (name, int(size), digest)
        for name, size, digest in MANIFEST_ROW.findall(manifest.read_text(encoding="utf-8"))
    ]
    if not rows:
        raise BuildError(f"{manifest} lists no files; did its table format change?")
    return rows


def verify_manifest(manifest: Path, base: Path) -> int:
    """Check every row of a MANIFEST.md against the bytes under `base`.

    The snippet in docs/SECURITY.md, run over the copy rather than the source.
    """
    rows = manifest_rows(manifest)
    for name, size, digest in rows:
        path = base / name
        if not path.is_file():
            raise BuildError(f"{manifest.name} lists {name}, which is not in {base}")
        data = path.read_bytes()
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            raise BuildError(f"{base / name} does not match {manifest.name}")
    return len(rows)


def copy_web(out: Path) -> dict[str, int]:
    if out.exists():
        if out.is_file() or (any(out.iterdir()) and not (out / "index.html").is_file()):
            raise BuildError(f"{out} exists and is not a previous dist/; remove it yourself")
        shutil.rmtree(out)
    shutil.copytree(WEB_DIR, out)
    return {name: verify_manifest(out / name / "MANIFEST.md", out / name) for name in MANIFEST_DIRS}


# --- Step 2: site/static/ --------------------------------------------------


def overlay_static(out: Path) -> tuple[list[str], list[str]]:
    """Copy `site/static/` over the copy of `web/`. Returns (copied, replaced)."""
    if not (STATIC_DIR / "api.js").is_file():
        raise BuildError(f"{STATIC_DIR / 'api.js'} is missing; dist/ would talk to no server")

    copied: list[str] = []
    replaced: list[str] = []
    for source in sorted(STATIC_DIR.rglob("*")):
        if not source.is_file():
            continue
        relative = source.relative_to(STATIC_DIR)
        target = out / relative
        if target.exists():
            replaced.append(relative.as_posix())
        copied.append(relative.as_posix())
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

    if "api.js" not in replaced:
        raise BuildError("site/static/api.js did not replace web/api.js")
    return copied, replaced


# --- Step 3: the pack and the tiles ---------------------------------------


def copy_pack(pack: Path, out: Path) -> list[str]:
    """Copy the data pack into `dist/pack/`.

    That directory already holds `site/static/pack/` — the loader modules share
    the name with the data they load (docs/STATIC_SITE.md, "Layout"), so the
    copy merges rather than replaces, and a pack file that would land on top of
    a module is refused instead of shipped.
    """
    if not (pack / "meta.json").is_file():
        raise BuildError(f"{pack} has no meta.json; run `curbcheck pack --out {pack}` first")
    destination = out / "pack"
    clashes = [
        source.name
        for source in pack.iterdir()
        if source.is_file() and (destination / source.name).exists()
    ]
    if clashes:
        raise BuildError(f"pack files would overwrite site/static/pack/: {', '.join(clashes)}")
    shutil.copytree(pack, destination, dirs_exist_ok=True)
    return sorted(path.name for path in pack.iterdir() if path.is_file())


def copy_tiles(tiles: Path, out: Path) -> TileSource:
    destination = out / "basemap" / "tiles"
    shutil.copytree(tiles, destination)
    count = sum(1 for _ in destination.rglob("*.pbf"))
    if count == 0:
        raise BuildError(f"{tiles} holds no .pbf tiles; run site/slice_basemap.py first")
    return _tile_source(destination, count)


def _tile_source(tiles: Path, count: int) -> TileSource:
    """The source's bounds and zoom range, from the slicer's metadata or the tree.

    `slice_basemap.py` writes the bounds the archive was cut to; deriving them
    from the tile tree instead only gives them rounded out to whole tiles, which
    is correct but wider.
    """
    metadata = tiles / "metadata.json"
    if metadata.is_file():
        data = json.loads(metadata.read_text(encoding="utf-8"))
        return TileSource(
            bounds=[float(value) for value in data["bounds"]],
            min_zoom=int(data["minzoom"]),
            max_zoom=int(data["maxzoom"]),
            tiles=count,
        )

    zooms = sorted(int(d.name) for d in tiles.iterdir() if d.is_dir() and d.name.isdigit())
    if not zooms:
        raise BuildError(f"{tiles} has no {{z}} directories")
    deepest = zooms[-1]
    xs = [int(d.name) for d in (tiles / str(deepest)).iterdir() if d.is_dir()]
    ys = [int(path.stem) for x in xs for path in (tiles / str(deepest) / str(x)).glob("*.pbf")]
    if not xs or not ys:
        raise BuildError(f"{tiles} has no tiles at zoom {deepest}")
    return TileSource(
        bounds=[
            _tile_to_lon(min(xs), deepest),
            _tile_to_lat(max(ys) + 1, deepest),
            _tile_to_lon(max(xs) + 1, deepest),
            _tile_to_lat(min(ys), deepest),
        ],
        min_zoom=zooms[0],
        max_zoom=deepest,
        tiles=count,
    )


def _tile_to_lon(x: int, zoom: int) -> float:
    return x / (1 << zoom) * 360.0 - 180.0


def _tile_to_lat(y: int, zoom: int) -> float:
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / (1 << zoom)))))


# --- Step 4: the basemap paths --------------------------------------------


def rewrite_style_url(out: Path, base_path: str) -> None:
    path = out / "map.js"
    text = path.read_text(encoding="utf-8")
    found = text.count(STYLE_URL_LINE)
    if found != 1:
        raise BuildError(f"map.js holds {found} copies of {STYLE_URL_LINE!r}, expected 1")
    rewritten = f'const STYLE_URL = "{base_path}basemap/style.json";'
    path.write_text(text.replace(STYLE_URL_LINE, rewritten), encoding="utf-8")


def rewrite_style(out: Path, base_path: str, source: TileSource) -> None:
    """Point glyphs, sprite and the vector source at this deployment's paths.

    The `pmtiles://` URL cannot survive: Pages answers range requests in a way
    Firefox miscaches (protomaps/PMTiles#584), so the tiles are files and the
    source lists them. The attribution string is carried over unchanged — the
    ODbL requires it (web/basemap/MANIFEST.md).
    """
    path = out / "basemap" / "style.json"
    style = json.loads(path.read_text(encoding="utf-8"))

    for key in ("glyphs", "sprite"):
        value = style.get(key)
        if not isinstance(value, str) or not value.startswith("/basemap/"):
            raise BuildError(f"style.json {key} is {value!r}, expected a /basemap/ path")
        style[key] = base_path + value[len("/") :]

    sources = style.get("sources", {})
    if len(sources) != 1:
        raise BuildError(f"style.json has {len(sources)} sources, expected 1")
    name, old = next(iter(sources.items()))
    if not str(old.get("url", "")).startswith("pmtiles:///basemap/"):
        raise BuildError(f"style.json source {name} is {old.get('url')!r}, expected pmtiles:///")
    attribution = old.get("attribution")
    if not attribution:
        raise BuildError(f"style.json source {name} carries no attribution")

    style["sources"][name] = {
        "type": "vector",
        "tiles": [base_path + "basemap/tiles/{z}/{x}/{y}.pbf"],
        "minzoom": source.min_zoom,
        "maxzoom": source.max_zoom,
        "bounds": source.bounds,
        "attribution": attribution,
    }
    path.write_text(
        json.dumps(style, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# --- Steps 5 and 6: index.html --------------------------------------------


def rewrite_index(out: Path) -> None:
    """Inject the meta CSP and referrer tags, and drop the PMTiles script."""
    path = out / "index.html"
    html = path.read_text(encoding="utf-8")

    if html.count("<head>") != 1:
        raise BuildError("index.html does not have exactly one <head>")
    if html.count(REFERRER_META) != 1:
        raise BuildError("index.html does not carry exactly one referrer meta tag")
    if html.count(PMTILES_SCRIPT_BLOCK) != 1:
        raise BuildError("index.html does not carry exactly one pmtiles.js script block")

    # The referrer tag is moved rather than duplicated: two of them would leave
    # a reader wondering which one the browser honoured.
    html = html.replace(f"    {REFERRER_META}\n", "", 1)
    html = html.replace("<head>", f"<head>\n    {CSP_META}\n    {REFERRER_META}", 1)
    html = html.replace(PMTILES_SCRIPT_BLOCK, PMTILES_REPLACEMENT, 1)

    path.write_text(html, encoding="utf-8")


def drop_pmtiles(out: Path) -> None:
    shutil.rmtree(out / "vendor" / "pmtiles")
    # Harmless under an Actions deploy, load-bearing under a branch deploy:
    # Jekyll would otherwise drop every path beginning with an underscore.
    (out / ".nojekyll").write_text("", encoding="utf-8")


# --- Step 7: sizes ---------------------------------------------------------


def print_sizes(out: Path) -> None:
    sizes = {path: path.stat().st_size for path in out.rglob("*") if path.is_file()}
    total = sum(sizes.values())

    tiles_root = out / "basemap" / "tiles"
    large = sorted(
        (
            (size, path.relative_to(out).as_posix())
            for path, size in sizes.items()
            if size >= LARGE_FILE_BYTES and tiles_root not in path.parents
        ),
        reverse=True,
    )
    print("\nfiles over 100 KB")
    for size, name in large:
        print(f"  {size:>12,}  {name}")
    # The tile tree is 474 files of one kind; listing each is noise, and its
    # total is the number that decides whether the site is too big.
    tile_bytes = sum(size for path, size in sizes.items() if tiles_root in path.parents)
    tile_count = sum(1 for path in sizes if tiles_root in path.parents and path.suffix == ".pbf")
    print(f"  {tile_bytes:>12,}  basemap/tiles/ ({tile_count:,} tiles, collapsed)")

    print("\nby directory")
    by_directory: dict[str, int] = {}
    for path, size in sizes.items():
        relative = path.relative_to(out)
        top = relative.parts[0] if len(relative.parts) > 1 else "."
        by_directory[top] = by_directory.get(top, 0) + size
    for name, size in sorted(by_directory.items(), key=lambda item: -item[1]):
        print(f"  {size:>12,}  {name}")

    print(f"\ntotal       {total:,} bytes in {len(sizes):,} files")


# --- The build -------------------------------------------------------------


def build(out: Path, pack: Path, tiles: Path, base_path: str) -> None:
    verified = copy_web(out)
    print(f"web         copied, {sum(verified.values())} manifest files verified")

    copied, replaced = overlay_static(out)
    print(
        f"static      {len(copied)} files overlaid, {len(replaced)} replaced ({', '.join(replaced)})"
    )

    pack_files = copy_pack(pack, out)
    print(f"pack        {len(pack_files)} files: {', '.join(pack_files)}")

    source = copy_tiles(tiles, out)
    print(f"tiles       {source.tiles:,} tiles, zoom {source.min_zoom}-{source.max_zoom}")

    rewrite_style_url(out, base_path)
    rewrite_style(out, base_path, source)
    print(f"basemap     rewritten to {base_path}basemap/")

    rewrite_index(out)
    drop_pmtiles(out)
    print("index.html  CSP and referrer injected, pmtiles.js removed")

    print_sizes(out)


def _base_path(value: str) -> str:
    """`/curbcheck/` from `/curbcheck`, `curbcheck/` or `//curbcheck//`; `/` from `` or `/`.

    Normalised rather than validated because the value comes from
    `actions/configure-pages`, which reports `/repo` for a project site and
    `/` for a custom domain; a caller appending its own slash would otherwise
    produce `//`, and `//basemap/...` is a protocol-relative URL to a host
    called basemap.
    """
    inner = value.strip("/")
    if not inner:
        return "/"
    if "//" in inner or any(ch.isspace() for ch in inner):
        raise argparse.ArgumentTypeError(f"base path {value!r} is not one path prefix")
    return f"/{inner}/"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path("dist"), help="directory to assemble")
    parser.add_argument("--pack", type=Path, required=True, help="`curbcheck pack --out` directory")
    parser.add_argument(
        "--tiles", type=Path, required=True, help="`site/slice_basemap.py` output directory"
    )
    parser.add_argument(
        "--base-path",
        type=_base_path,
        default="/",
        help="URL prefix the site is served under, e.g. /curbcheck/",
    )
    args = parser.parse_args(argv)

    try:
        build(args.out, args.pack, args.tiles, args.base_path)
    except BuildError as error:
        print(f"build failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
