"""`site/build.py` over the real `web/` tree, a stub pack and a stub tile tree.

The build is the only thing between a reviewed frontend and a public URL, so
what is checked here is what would be wrong on that URL: the worker's `api.js`
in place of the server's, every basemap path under the deployment's base path,
the CSP that replaces the header the server sends, no PMTiles shim, and the
vendored bytes still matching the SHA-256s the repository vouches for.

The remote-URL and markup checks are the ones `tests/test_web_static.py`
already runs over `web/`, imported rather than restated.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from test_slice_basemap import load_site_module
from test_web_static import FORBIDDEN_JS, REMOTE_SCHEME, URL_ATTRIBUTES, _Links

build_module = load_site_module("build")

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = REPO_ROOT / "web"
STATIC_DIR = REPO_ROOT / "site" / "static"

PROJECT_BASE_PATH = "/curbcheck/"

# Bounds and zooms the slicer records for the Manhattan cut.
TILE_METADATA = {
    "bounds": [-74.03, 40.68, -73.90, 40.88],
    "center": [-73.965, 40.78, 0],
    "minzoom": 0,
    "maxzoom": 15,
    "tile_count": 3,
}
SAMPLE_TILES = [(0, 0, 0), (14, 4822, 6157), (15, 9647, 12313)]

STUB_API_JS = """// Stand-in for site/static/api.js while the worker lands.
export class ApiError extends Error {}
export function health() {
  return Promise.resolve({ status: "ok" });
}
"""


@pytest.fixture(scope="module")
def overlay(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """A snapshot of `site/static/`, given a stand-in `api.js` if it has none yet.

    A copy rather than the directory itself, so the build and the assertions
    about it read the same bytes. The stand-in is for the window in which the
    worker modules have not all landed: `build.py` refuses to run without
    `api.js`, and the rest of the build should be checkable before then.
    """
    snapshot = tmp_path_factory.mktemp("static")
    shutil.copytree(STATIC_DIR, snapshot, dirs_exist_ok=True)
    if not (snapshot / "api.js").is_file():
        (snapshot / "api.js").write_text(STUB_API_JS, encoding="utf-8")

    original = build_module.STATIC_DIR
    build_module.STATIC_DIR = snapshot
    yield snapshot
    build_module.STATIC_DIR = original


@pytest.fixture(scope="module")
def fake_pack(tmp_path_factory: pytest.TempPathFactory) -> Path:
    pack = tmp_path_factory.mktemp("pack")
    (pack / "meta.json").write_text(
        json.dumps({"format": 1, "built_at": "2026-09-15T18:02:11+00:00", "files": {}}),
        encoding="utf-8",
    )
    # Over the 100 KB line, so the size report has something to print.
    (pack / "rules.3fa9c1e2b0d4.json.gz").write_bytes(b"\x1f\x8b" + b"packed" * 30_000)
    return pack


def _write_tiles(root: Path, *, with_metadata: bool) -> Path:
    for zoom, x, y in SAMPLE_TILES:
        tile = root / str(zoom) / str(x) / f"{y}.pbf"
        tile.parent.mkdir(parents=True, exist_ok=True)
        tile.write_bytes(f"tile {zoom}/{x}/{y}".encode())
    if with_metadata:
        (root / "metadata.json").write_text(json.dumps(TILE_METADATA), encoding="utf-8")
    return root


@pytest.fixture(scope="module")
def fake_tiles(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _write_tiles(tmp_path_factory.mktemp("tiles"), with_metadata=True)


@pytest.fixture(scope="module")
def dist(
    tmp_path_factory: pytest.TempPathFactory, overlay: Path, fake_pack: Path, fake_tiles: Path
) -> Path:
    out = tmp_path_factory.mktemp("root") / "dist"
    build_module.build(out, fake_pack, fake_tiles, "/")
    return out


@pytest.fixture(scope="module")
def project_dist(
    tmp_path_factory: pytest.TempPathFactory, overlay: Path, fake_pack: Path, fake_tiles: Path
) -> Path:
    """The same build under a project URL, which is what `oren-t.github.io/curbcheck` is."""
    out = tmp_path_factory.mktemp("project") / "dist"
    build_module.build(out, fake_pack, fake_tiles, PROJECT_BASE_PATH)
    return out


# --- The overlay -----------------------------------------------------------


def test_the_overlay_replaces_the_servers_api_module(dist: Path, overlay: Path) -> None:
    shipped = (dist / "api.js").read_text(encoding="utf-8")
    assert shipped == (overlay / "api.js").read_text(encoding="utf-8")
    assert shipped != (WEB_DIR / "api.js").read_text(encoding="utf-8")


def test_every_overlay_file_reaches_dist(dist: Path, overlay: Path) -> None:
    for source in overlay.rglob("*.js"):
        relative = source.relative_to(overlay)
        assert (dist / relative).read_bytes() == source.read_bytes(), relative


def test_the_pack_is_copied_whole(dist: Path, fake_pack: Path) -> None:
    for source in fake_pack.iterdir():
        assert (dist / "pack" / source.name).read_bytes() == source.read_bytes()


def test_the_tiles_are_copied_as_files(dist: Path) -> None:
    for zoom, x, y in SAMPLE_TILES:
        tile = dist / "basemap" / "tiles" / str(zoom) / str(x) / f"{y}.pbf"
        assert tile.read_bytes() == f"tile {zoom}/{x}/{y}".encode()


# --- Step 4: the basemap paths --------------------------------------------


def test_the_style_url_and_the_style_paths_follow_the_base_path(project_dist: Path) -> None:
    map_js = (project_dist / "map.js").read_text(encoding="utf-8")
    assert f'const STYLE_URL = "{PROJECT_BASE_PATH}basemap/style.json";' in map_js
    assert '"/basemap/style.json"' not in map_js

    style = json.loads((project_dist / "basemap" / "style.json").read_text(encoding="utf-8"))
    assert style["glyphs"] == f"{PROJECT_BASE_PATH}basemap/fonts/{{fontstack}}/{{range}}.pbf"
    assert style["sprite"] == f"{PROJECT_BASE_PATH}basemap/sprites/v4/light"
    source = next(iter(style["sources"].values()))
    assert source["tiles"] == [f"{PROJECT_BASE_PATH}basemap/tiles/{{z}}/{{x}}/{{y}}.pbf"]


def test_the_vector_source_replaces_the_pmtiles_url_and_keeps_the_attribution(dist: Path) -> None:
    """The ODbL requires the credit; it is carried over, not rewritten."""
    original = json.loads((WEB_DIR / "basemap" / "style.json").read_text(encoding="utf-8"))
    style = json.loads((dist / "basemap" / "style.json").read_text(encoding="utf-8"))

    assert set(style["sources"]) == set(original["sources"])
    name = next(iter(style["sources"]))
    source = style["sources"][name]
    assert source == {
        "type": "vector",
        "tiles": ["/basemap/tiles/{z}/{x}/{y}.pbf"],
        "minzoom": 0,
        "maxzoom": 15,
        "bounds": TILE_METADATA["bounds"],
        "attribution": original["sources"][name]["attribution"],
    }
    assert "pmtiles" not in json.dumps(style)


def test_the_layers_are_untouched(dist: Path) -> None:
    original = json.loads((WEB_DIR / "basemap" / "style.json").read_text(encoding="utf-8"))
    style = json.loads((dist / "basemap" / "style.json").read_text(encoding="utf-8"))
    assert style["layers"] == original["layers"]


def test_tiles_without_metadata_are_bounded_from_the_tree(tmp_path: Path) -> None:
    """A tile tree alone still gives a usable source, rounded out to whole tiles."""
    tiles = _write_tiles(tmp_path / "tiles", with_metadata=False)
    source = build_module._tile_source(tiles, 3)

    west, south, east, north = source.bounds
    assert (source.min_zoom, source.max_zoom) == (0, 15)
    # The deepest zoom decides the box, so it lands inside the cut's own bbox.
    assert -74.03 <= west < east <= -73.90
    assert 40.68 <= south < north <= 40.88


# --- Step 5: the CSP -------------------------------------------------------


def test_the_csp_and_referrer_tags_are_the_first_children_of_head(dist: Path) -> None:
    html = (dist / "index.html").read_text(encoding="utf-8")
    head = html.index("<head>") + len("<head>")
    injected = html[head : html.index("<meta charset")]

    assert build_module.CSP_META in injected
    assert build_module.REFERRER_META in injected
    # Exactly the string docs/STATIC_SITE.md step 5 specifies.
    assert (
        "content=\"default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data: blob:; connect-src 'self'; worker-src 'self' blob:; "
        "object-src 'none'; base-uri 'none'\"" in html
    )
    # Moved, not duplicated: two referrer tags would leave the reader guessing.
    assert html.count(build_module.REFERRER_META) == 1
    assert "frame-ancestors" not in html


# --- Step 6: pmtiles -------------------------------------------------------


def test_no_pmtiles_script_tag_or_file_survives(dist: Path) -> None:
    html = (dist / "index.html").read_text(encoding="utf-8")
    assert "pmtiles" not in html.lower()
    assert not (dist / "vendor" / "pmtiles").exists()
    assert not any(path.name.startswith("pmtiles") for path in dist.rglob("*"))
    # MapLibre still ships: it is what draws the map.
    assert (dist / "vendor" / "maplibre-gl" / "maplibre-gl.mjs").is_file()


def test_nojekyll_is_written(dist: Path) -> None:
    assert (dist / ".nojekyll").is_file()


# --- What the manifests still vouch for ------------------------------------

# Two rows cannot match after the build, both on purpose: step 4 rewrites the
# style and step 6 deletes the shim. Step 1 verified both before it touched
# them, so these are the only bytes in dist/ the manifests no longer cover.
REWRITTEN_OR_REMOVED = {"basemap/style.json", "vendor/pmtiles/pmtiles.js"}


def test_the_copied_vendor_fonts_and_basemap_bytes_still_match_their_manifests(
    dist: Path,
) -> None:
    import hashlib

    checked = 0
    for directory in build_module.MANIFEST_DIRS:
        for name, size, digest in build_module.manifest_rows(dist / directory / "MANIFEST.md"):
            if f"{directory}/{name}" in REWRITTEN_OR_REMOVED:
                continue
            data = (dist / directory / name).read_bytes()
            assert len(data) == size and hashlib.sha256(data).hexdigest() == digest, name
            checked += 1
    assert checked > 20


def test_a_corrupted_vendored_file_fails_the_verification(tmp_path: Path) -> None:
    base = tmp_path / "fonts"
    shutil.copytree(WEB_DIR / "fonts", base)
    row = build_module.manifest_rows(base / "MANIFEST.md")[0]
    (base / row[0]).write_bytes(b"not the vendored bytes")

    with pytest.raises(build_module.BuildError, match="does not match"):
        build_module.verify_manifest(base / "MANIFEST.md", base)


# --- The checks web/ already gets -------------------------------------------


def test_dist_index_html_loads_nothing_from_the_network(dist: Path) -> None:
    parser = _Links()
    parser.feed((dist / "index.html").read_text(encoding="utf-8"))
    assert parser.elements

    for tag, attributes in parser.elements:
        for name in URL_ATTRIBUTES:
            url = attributes.get(name)
            if url is None or not REMOTE_SCHEME.match(url):
                continue
            rel = attributes.get("rel", "").lower().split()
            assert tag == "a", f"<{tag} {name}={url!r}> loads a remote resource"
            assert "noopener" in rel and "noreferrer" in rel


def test_no_shipped_script_builds_markup_or_code_from_data(dist: Path) -> None:
    for path in sorted(dist.rglob("*.js")):
        if "vendor" in path.relative_to(dist).parts:
            continue
        source = path.read_text(encoding="utf-8")
        for pattern in FORBIDDEN_JS:
            assert pattern not in source, f"{path.relative_to(dist)} contains {pattern!r}"


# --- Refusals and the report -----------------------------------------------


def test_a_base_path_without_both_slashes_is_refused() -> None:
    for value in ("curbcheck/", "/curbcheck", ""):
        with pytest.raises(SystemExit):
            build_module.main(["--pack", "p", "--tiles", "t", "--base-path", value])


def test_a_pack_without_meta_json_is_refused(tmp_path: Path) -> None:
    with pytest.raises(build_module.BuildError, match=r"meta\.json"):
        build_module.copy_pack(tmp_path, tmp_path / "out")


def test_a_tile_directory_with_no_tiles_is_refused(tmp_path: Path) -> None:
    empty = tmp_path / "tiles"
    empty.mkdir()
    with pytest.raises(build_module.BuildError, match=r"no \.pbf tiles"):
        build_module.copy_tiles(empty, tmp_path / "out")


def test_a_moved_style_url_fails_the_build_loudly(tmp_path: Path) -> None:
    """Step 4 asserts its occurrence count so a renamed constant is not a silent 404."""
    (tmp_path / "map.js").write_text('const STYLE_URL = "./basemap/style.json";', encoding="utf-8")
    with pytest.raises(build_module.BuildError, match="expected 1"):
        build_module.rewrite_style_url(tmp_path, "/")


def test_a_second_source_in_the_style_fails_the_build(tmp_path: Path) -> None:
    basemap = tmp_path / "basemap"
    basemap.mkdir()
    (basemap / "style.json").write_text(
        json.dumps(
            {
                "glyphs": "/basemap/fonts/{fontstack}/{range}.pbf",
                "sprite": "/basemap/sprites/v4/light",
                "sources": {"a": {"url": "pmtiles:///basemap/x.pmtiles"}, "b": {}},
            }
        ),
        encoding="utf-8",
    )
    source = build_module.TileSource(bounds=[0, 0, 1, 1], min_zoom=0, max_zoom=15, tiles=1)
    with pytest.raises(build_module.BuildError, match="2 sources"):
        build_module.rewrite_style(tmp_path, "/", source)


def test_the_report_prints_the_large_files_and_the_total(
    tmp_path: Path, overlay: Path, fake_pack: Path, fake_tiles: Path, capsys: pytest.CaptureFixture
) -> None:
    build_module.build(tmp_path / "dist", fake_pack, fake_tiles, "/")
    printed = capsys.readouterr().out

    assert "files over 100 KB" in printed
    assert "rules.3fa9c1e2b0d4.json.gz" in printed
    assert "basemap/tiles/ (3 tiles, collapsed)" in printed
    assert "total" in printed
