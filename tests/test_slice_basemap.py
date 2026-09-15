"""`site/slice_basemap.py` against archives this module writes itself.

The reader is a hand-rolled parser of a binary format produced by somebody
else's tool, so the only honest test is a round trip: build a valid PMTiles v3
archive here — gzip directories, gzip tiles, a leaf directory, a run — slice it,
and check the files. The Hilbert encoder below is written from the spec's own
description rather than from the reader, so the two have to agree independently.

A byte-for-byte comparison against the `pmtiles` CLI on the real 23 MB
Manhattan archive is in the build log, not here: the CLI is not a dependency
and `data/` is not committed.
"""

from __future__ import annotations

import gzip
import importlib.util
import json
import struct
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_site_module(name: str) -> ModuleType:
    """Import `site/<name>.py` by path.

    `site` is a standard-library module name, so `import site.build` would find
    the wrong thing. Registering the module in `sys.modules` first is what lets
    `@dataclass` resolve its own annotations.
    """
    spec = importlib.util.spec_from_file_location(
        f"curbcheck_site_{name}", REPO_ROOT / "site" / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


slicer = load_site_module("slice_basemap")

COMPRESSION_NONE = 1
COMPRESSION_GZIP = 2
COMPRESSION_BROTLI = 3
COMPRESSION_ZSTD = 4
TILE_TYPE_MVT = 1
TILE_TYPE_PNG = 2

BOUNDS = (-74.03, 40.68, -73.90, 40.88)


# --- A minimal PMTiles v3 writer ------------------------------------------


def _varint(value: int) -> bytes:
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _rotate(side: int, x: int, y: int, rx: int, ry: int) -> tuple[int, int]:
    """The Hilbert rotation, in the encoding direction (Wikipedia's `rot`)."""
    if ry == 0:
        if rx == 1:
            x = side - 1 - x
            y = side - 1 - y
        return y, x
    return x, y


def zxy_to_tile_id(zoom: int, x: int, y: int) -> int:
    """`{z}/{x}/{y}` to the spec's tile id: zoom levels stacked, Hilbert within one."""
    tile_id = sum(1 << (2 * level) for level in range(zoom))
    side = 1 << zoom
    step = side // 2
    while step > 0:
        rx = 1 if (x & step) > 0 else 0
        ry = 1 if (y & step) > 0 else 0
        tile_id += step * step * ((3 * rx) ^ ry)
        x, y = _rotate(side, x, y, rx, ry)
        step //= 2
    return tile_id


def _compress(data: bytes, compression: int) -> bytes:
    if compression == COMPRESSION_NONE:
        return data
    # mtime=0 so an archive written twice is byte-identical.
    return gzip.compress(data, mtime=0)


def _serialize_directory(entries: list[tuple[int, int, int, int]]) -> bytes:
    """Entries are `(tile_id, offset, length, run_length)` in ascending tile-id order."""
    out = bytearray(_varint(len(entries)))
    previous_id = 0
    for tile_id, _offset, _length, _run in entries:
        out += _varint(tile_id - previous_id)
        previous_id = tile_id
    for _tile_id, _offset, _length, run in entries:
        out += _varint(run)
    for _tile_id, _offset, length, _run in entries:
        out += _varint(length)
    for index, (_tile_id, offset, _length, _run) in enumerate(entries):
        if index > 0:
            previous = entries[index - 1]
            if offset == previous[1] + previous[2]:
                out += _varint(0)
                continue
        out += _varint(offset + 1)
    return bytes(out)


def write_archive(
    path: Path,
    blobs: list[tuple[int, int, bytes]],
    *,
    internal_compression: int = COMPRESSION_GZIP,
    tile_compression: int = COMPRESSION_GZIP,
    tile_type: int = TILE_TYPE_MVT,
    leaf_after: int | None = None,
    magic: bytes = b"PMTiles",
    version: int = 3,
    truncate_tile_data: int = 0,
) -> Path:
    """Write a PMTiles v3 archive. `blobs` are `(tile_id, run_length, tile bytes)`.

    `leaf_after` moves every entry from that index on into a leaf directory, so
    the reader's leaf path gets exercised.
    """
    tile_section = bytearray()
    entries: list[tuple[int, int, int, int]] = []
    for tile_id, run_length, payload in blobs:
        body = _compress(payload, tile_compression)
        entries.append((tile_id, len(tile_section), len(body), run_length))
        tile_section += body

    if leaf_after is None:
        root_entries = entries
        leaf_section = b""
    else:
        leaf_entries = entries[leaf_after:]
        leaf_body = _compress(_serialize_directory(leaf_entries), internal_compression)
        leaf_section = leaf_body
        root_entries = [*entries[:leaf_after], (leaf_entries[0][0], 0, len(leaf_body), 0)]

    root = _compress(_serialize_directory(root_entries), internal_compression)
    metadata = _compress(
        json.dumps({"name": "test basemap", "attribution": "test attribution"}).encode(),
        internal_compression,
    )

    root_offset = slicer.HEADER_SIZE
    metadata_offset = root_offset + len(root)
    leaf_offset = metadata_offset + len(metadata)
    tile_data_offset = leaf_offset + len(leaf_section)

    header = bytearray(slicer.HEADER_SIZE)
    header[0:7] = magic
    header[7] = version
    struct.pack_into(
        "<8Q",
        header,
        8,
        root_offset,
        len(root),
        metadata_offset,
        len(metadata),
        leaf_offset,
        len(leaf_section),
        tile_data_offset,
        len(tile_section),
    )
    struct.pack_into("<3Q", header, 72, len(blobs), len(entries), len(entries))
    header[96] = 1  # clustered
    header[97] = internal_compression
    header[98] = tile_compression
    header[99] = tile_type
    header[100] = 0
    header[101] = 2
    struct.pack_into("<4i", header, 102, *(round(value * 10_000_000) for value in BOUNDS))
    header[118] = 0
    struct.pack_into("<2i", header, 119, round(-73.965 * 10_000_000), round(40.78 * 10_000_000))

    body = bytes(tile_section)
    if truncate_tile_data:
        body = body[:-truncate_tile_data]
    path.write_bytes(bytes(header) + root + metadata + leaf_section + body)
    return path


def _tile(zoom: int, x: int, y: int, marker: bytes) -> tuple[int, int, bytes]:
    # Not real MVT: the slicer copies tile bytes through without parsing them,
    # and a recognisable payload is what makes a mismatch readable.
    return (zxy_to_tile_id(zoom, x, y), 1, marker * 40)


SMALL_TILES = [
    _tile(0, 0, 0, b"z0-0-0 "),
    _tile(1, 0, 1, b"z1-0-1 "),
    _tile(1, 1, 0, b"z1-1-0 "),
    _tile(2, 1, 2, b"z2-1-2 "),
    _tile(2, 3, 3, b"z2-3-3 "),
]


# --- The Hilbert mapping ---------------------------------------------------


def test_tile_ids_follow_the_specs_hilbert_order() -> None:
    """The spec's own worked example: id 0 is 0/0/0, then z1 runs 0,0 → 0,1 → 1,1 → 1,0."""
    assert slicer.tile_id_to_zxy(0) == (0, 0, 0)
    assert slicer.tile_id_to_zxy(1) == (1, 0, 0)
    assert slicer.tile_id_to_zxy(2) == (1, 0, 1)
    assert slicer.tile_id_to_zxy(3) == (1, 1, 1)
    assert slicer.tile_id_to_zxy(4) == (1, 1, 0)
    assert slicer.tile_id_to_zxy(5) == (2, 0, 0)


@pytest.mark.parametrize("zoom", [0, 1, 2, 3, 4, 8, 15])
def test_the_reader_inverts_an_independent_hilbert_encoder(zoom: int) -> None:
    side = 1 << zoom
    step = max(1, side // 7)
    for x in range(0, side, step):
        for y in range(0, side, step):
            assert slicer.tile_id_to_zxy(zxy_to_tile_id(zoom, x, y)) == (zoom, x, y)


# --- Slicing ---------------------------------------------------------------


def test_every_tile_is_written_at_its_path_with_the_mvt_bytes_inflated(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a.pmtiles", SMALL_TILES)
    out = tmp_path / "tiles"
    result = slicer.slice_archive(archive, out)

    written = {str(path.relative_to(out)): path.read_bytes() for path in sorted(out.rglob("*.pbf"))}
    assert set(written) == {"0/0/0.pbf", "1/0/1.pbf", "1/1/0.pbf", "2/1/2.pbf", "2/3/3.pbf"}
    assert written["0/0/0.pbf"] == b"z0-0-0 " * 40
    assert written["2/3/3.pbf"] == b"z2-3-3 " * 40
    # Written inflated: the host serves these with no Content-Encoding.
    assert not written["1/0/1.pbf"].startswith(b"\x1f\x8b")

    assert result.tiles == 5
    assert result.bytes_written == sum(len(data) for data in written.values())
    assert (result.min_zoom, result.max_zoom) == (0, 2)
    assert result.bounds == pytest.approx(BOUNDS)


def test_the_tile_metadata_records_the_bounds_and_zooms_the_build_needs(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a.pmtiles", SMALL_TILES)
    out = tmp_path / "tiles"
    slicer.slice_archive(archive, out)

    metadata = json.loads((out / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["bounds"] == pytest.approx(list(BOUNDS))
    assert (metadata["minzoom"], metadata["maxzoom"]) == (0, 2)
    assert metadata["tile_count"] == 5
    assert metadata["name"] == "test basemap"
    # The archive's attribution is an HTML anchor at a remote URL; nothing in
    # this repository ships one, and the style carries the credit that is shown.
    assert "attribution" not in metadata


def test_a_leaf_directory_is_followed(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a.pmtiles", SMALL_TILES, leaf_after=2)
    out = tmp_path / "tiles"
    result = slicer.slice_archive(archive, out)

    assert result.tiles == 5
    assert (out / "2/3/3.pbf").read_bytes() == b"z2-3-3 " * 40


def test_a_run_writes_every_tile_id_it_addresses(tmp_path: Path) -> None:
    """One stored tile can answer several consecutive ids — deduplicated ocean, mostly."""
    first = zxy_to_tile_id(1, 0, 0)
    archive = write_archive(
        tmp_path / "a.pmtiles",
        [(zxy_to_tile_id(0, 0, 0), 1, b"root"), (first, 4, b"same")],
    )
    out = tmp_path / "tiles"
    result = slicer.slice_archive(archive, out)

    assert result.tiles == 5
    for zoom, x, y in [(1, 0, 0), (1, 0, 1), (1, 1, 1), (1, 1, 0)]:
        assert (out / str(zoom) / str(x) / f"{y}.pbf").read_bytes() == b"same"


def test_an_uncompressed_archive_is_read_as_is(tmp_path: Path) -> None:
    archive = write_archive(
        tmp_path / "a.pmtiles",
        SMALL_TILES,
        internal_compression=COMPRESSION_NONE,
        tile_compression=COMPRESSION_NONE,
    )
    out = tmp_path / "tiles"
    assert slicer.slice_archive(archive, out).tiles == 5
    assert (out / "0/0/0.pbf").read_bytes() == b"z0-0-0 " * 40


def test_slicing_twice_leaves_the_same_bytes(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a.pmtiles", SMALL_TILES)
    out = tmp_path / "tiles"
    slicer.slice_archive(archive, out)
    before = (out / "2/1/2.pbf").read_bytes()
    slicer.slice_archive(archive, out)
    assert (out / "2/1/2.pbf").read_bytes() == before


# --- Refusals --------------------------------------------------------------


def test_a_png_archive_is_refused_rather_than_written_as_pbf(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a.pmtiles", SMALL_TILES, tile_type=TILE_TYPE_PNG)
    with pytest.raises(slicer.PMTilesError, match="tile type is png"):
        slicer.slice_archive(archive, tmp_path / "tiles")


@pytest.mark.parametrize(
    ("field", "compression", "message"),
    [
        ("internal_compression", COMPRESSION_BROTLI, "internal compression is brotli"),
        ("tile_compression", COMPRESSION_ZSTD, "tile compression is zstd"),
    ],
)
def test_a_compression_this_reader_cannot_do_is_refused(
    tmp_path: Path, field: str, compression: int, message: str
) -> None:
    # Written with gzip and then relabelled: the refusal has to happen on the
    # header byte, before anything tries to decompress.
    archive = write_archive(tmp_path / "a.pmtiles", SMALL_TILES)
    raw = bytearray(archive.read_bytes())
    raw[97 if field == "internal_compression" else 98] = compression
    archive.write_bytes(bytes(raw))

    with pytest.raises(slicer.PMTilesError, match=message):
        slicer.slice_archive(archive, tmp_path / "tiles")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"magic": b"NOTPMTx"}, "magic number"),
        ({"version": 2}, "spec version 2"),
    ],
)
def test_a_file_that_is_not_pmtiles_v3_is_refused(
    tmp_path: Path, kwargs: dict[str, object], message: str
) -> None:
    archive = write_archive(tmp_path / "a.pmtiles", SMALL_TILES, **kwargs)
    with pytest.raises(slicer.PMTilesError, match=message):
        slicer.slice_archive(archive, tmp_path / "tiles")


def test_a_truncated_archive_is_refused_rather_than_half_written(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a.pmtiles", SMALL_TILES, truncate_tile_data=30)
    with pytest.raises(slicer.PMTilesError, match="truncated"):
        slicer.slice_archive(archive, tmp_path / "tiles")


def test_a_short_file_is_refused_before_the_header_is_read(tmp_path: Path) -> None:
    short = tmp_path / "short.pmtiles"
    short.write_bytes(b"PMTiles\x03")
    with pytest.raises(slicer.PMTilesError, match="shorter than"):
        slicer.slice_archive(short, tmp_path / "tiles")


def test_a_directory_that_inflates_past_the_cap_is_refused() -> None:
    bomb = gzip.compress(b"\x00" * (slicer.MAX_TILE_BYTES + 1), mtime=0)
    with pytest.raises(slicer.PMTilesError, match="past the"):
        slicer.inflate(bomb, COMPRESSION_GZIP, 1024)


def test_a_directory_offset_of_zero_in_the_first_entry_is_refused() -> None:
    """Offset 0 means "after the previous entry"; there is no previous entry at index 0."""
    data = _varint(1) + _varint(0) + _varint(1) + _varint(10) + _varint(0)
    with pytest.raises(slicer.PMTilesError, match="no offset"):
        slicer.deserialize_directory(data)


def test_the_cli_prints_the_count_the_zooms_and_the_bounds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    archive = write_archive(tmp_path / "a.pmtiles", SMALL_TILES)
    assert slicer.main([str(archive), str(tmp_path / "tiles")]) == 0

    printed = capsys.readouterr().out
    assert "tiles       5 written" in printed
    assert "zooms       0-2" in printed
    assert "-74.030000,40.680000,-73.900000,40.880000" in printed
