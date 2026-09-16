"""Cut a PMTiles v3 archive into one file per tile: `{z}/{x}/{y}.pbf`.

Serving the archive itself needs HTTP range requests, and GitHub Pages answers
a range request with `206` plus `Cache-Control: max-age=600`; Firefox then
reuses that cached partial response for a different range and decodes garbage
(protomaps/PMTiles#584). So the static site ships the tiles as files and the
style points at `{z}/{x}/{y}.pbf` instead of `pmtiles://` — see
`docs/STATIC_SITE.md`, "The build".

Standard library only, and the archive is read one tile at a time: only the
directories are held in memory. The archive is the one shipped artifact with no
recorded hash (`docs/STATIC_SITE.md`, T1), so every length it declares is
checked against a cap before anything is allocated.

Format: https://github.com/protomaps/PMTiles/blob/main/spec/v3/spec.md

Run: `python site/slice_basemap.py data/basemap/manhattan.pmtiles dist/basemap/tiles`
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

HEADER_SIZE = 127
MAGIC = b"PMTiles"
SPEC_VERSION = 3

# Spec §"Compression" and §"Tile Type". Only the two this reader supports are
# named by constant; the rest exist to make the refusal message readable.
COMPRESSION_NONE = 1
COMPRESSION_GZIP = 2
COMPRESSION_NAMES = {0: "unknown", 1: "none", 2: "gzip", 3: "brotli", 4: "zstd"}
TILE_TYPE_MVT = 1
TILE_TYPE_NAMES = {
    0: "unknown",
    1: "mvt",
    2: "png",
    3: "jpeg",
    4: "webp",
    5: "avif",
    6: "maplibre vector",
}

# Latitudes and longitudes are stored as signed 32-bit degrees times 10^7.
COORDINATE_SCALE = 10_000_000.0

# Caps on anything the file itself asks us to allocate. Measured on the
# Manhattan cut: the largest inflated tile is 361 KB and the root directory is
# 1,404 compressed bytes. Both caps are far above what a city-sized extract
# produces and far below what would hurt.
MAX_TILE_BYTES = 32 * 1024 * 1024
MAX_DIRECTORY_BYTES = 64 * 1024 * 1024
MAX_METADATA_BYTES = 8 * 1024 * 1024

# The spec allows leaf directories to nest; every writer in practice uses one
# level. This bounds the recursion rather than trusting the file not to loop.
MAX_DIRECTORY_DEPTH = 4


class PMTilesError(ValueError):
    """The archive is malformed, truncated, or in a form this reader refuses."""


@dataclass(frozen=True)
class Header:
    """The 127-byte header, with the fields this reader uses."""

    root_offset: int
    root_length: int
    metadata_offset: int
    metadata_length: int
    leaf_offset: int
    leaf_length: int
    tile_data_offset: int
    tile_data_length: int
    addressed_tiles: int
    tile_entries: int
    tile_contents: int
    clustered: bool
    internal_compression: int
    tile_compression: int
    tile_type: int
    min_zoom: int
    max_zoom: int
    bounds: tuple[float, float, float, float]
    center_zoom: int
    center: tuple[float, float]


@dataclass(frozen=True)
class Entry:
    """One directory entry. `run_length == 0` means it points at a leaf directory."""

    tile_id: int
    offset: int
    length: int
    run_length: int


@dataclass(frozen=True)
class SliceResult:
    """What a run wrote, for the caller to print or assert on."""

    tiles: int
    bytes_written: int
    min_zoom: int
    max_zoom: int
    bounds: tuple[float, float, float, float]


class _Varints:
    """A cursor over a decompressed directory."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def read(self) -> int:
        result = 0
        shift = 0
        while True:
            if self.pos >= len(self.data):
                raise PMTilesError("directory ends in the middle of a varint")
            byte = self.data[self.pos]
            self.pos += 1
            result |= (byte & 0x7F) << shift
            if byte < 0x80:
                return result
            shift += 7
            if shift > 63:
                raise PMTilesError("directory holds a varint wider than 64 bits")


def read_header(raw: bytes) -> Header:
    """Parse the fixed header. Raises `PMTilesError` for anything that is not PMTiles v3."""
    if len(raw) < HEADER_SIZE:
        raise PMTilesError(f"file is shorter than the {HEADER_SIZE}-byte header")
    if raw[:7] != MAGIC:
        raise PMTilesError("file does not start with the PMTiles magic number")
    if raw[7] != SPEC_VERSION:
        raise PMTilesError(f"spec version {raw[7]} is not 3")

    (
        root_offset,
        root_length,
        metadata_offset,
        metadata_length,
        leaf_offset,
        leaf_length,
        tile_data_offset,
        tile_data_length,
    ) = struct.unpack_from("<8Q", raw, 8)
    addressed_tiles, tile_entries, tile_contents = struct.unpack_from("<3Q", raw, 72)
    clustered, internal_compression, tile_compression, tile_type, min_zoom, max_zoom = raw[96:102]
    min_lon, min_lat, max_lon, max_lat = struct.unpack_from("<4i", raw, 102)
    center_zoom = raw[118]
    center_lon, center_lat = struct.unpack_from("<2i", raw, 119)

    return Header(
        root_offset=root_offset,
        root_length=root_length,
        metadata_offset=metadata_offset,
        metadata_length=metadata_length,
        leaf_offset=leaf_offset,
        leaf_length=leaf_length,
        tile_data_offset=tile_data_offset,
        tile_data_length=tile_data_length,
        addressed_tiles=addressed_tiles,
        tile_entries=tile_entries,
        tile_contents=tile_contents,
        clustered=bool(clustered),
        internal_compression=internal_compression,
        tile_compression=tile_compression,
        tile_type=tile_type,
        min_zoom=min_zoom,
        max_zoom=max_zoom,
        bounds=(
            min_lon / COORDINATE_SCALE,
            min_lat / COORDINATE_SCALE,
            max_lon / COORDINATE_SCALE,
            max_lat / COORDINATE_SCALE,
        ),
        center_zoom=center_zoom,
        center=(center_lon / COORDINATE_SCALE, center_lat / COORDINATE_SCALE),
    )


def check_supported(header: Header) -> None:
    """Refuse an archive this slicer would silently mangle.

    A PNG archive would be written out as `.pbf`, and brotli or zstd would need
    a dependency (`docs/DEPENDENCIES.md`) for a case no Protomaps build emits.
    """
    if header.tile_type != TILE_TYPE_MVT:
        name = TILE_TYPE_NAMES.get(header.tile_type, str(header.tile_type))
        raise PMTilesError(f"tile type is {name}, not mvt")
    for label, compression in (
        ("internal", header.internal_compression),
        ("tile", header.tile_compression),
    ):
        if compression not in (COMPRESSION_NONE, COMPRESSION_GZIP):
            name = COMPRESSION_NAMES.get(compression, str(compression))
            raise PMTilesError(f"{label} compression is {name}, not none or gzip")


def inflate(data: bytes, compression: int, limit: int) -> bytes:
    """Decompress per the header's compression enum, refusing output past `limit`."""
    if compression == COMPRESSION_NONE:
        return data
    if compression != COMPRESSION_GZIP:
        raise PMTilesError(f"cannot decompress {COMPRESSION_NAMES.get(compression, compression)}")
    # wbits=31 is zlib's "gzip wrapper"; `max_length` is what bounds a bomb.
    decompressor = zlib.decompressobj(wbits=31)
    try:
        out = decompressor.decompress(data, limit)
    except zlib.error as error:
        raise PMTilesError(f"gzip member is corrupt: {error}") from error
    if decompressor.unconsumed_tail:
        raise PMTilesError(f"gzip member inflates past the {limit}-byte cap")
    return out


def deserialize_directory(data: bytes) -> list[Entry]:
    """Decode the five varint sections of a directory into entries, in stored order."""
    cursor = _Varints(data)
    count = cursor.read()
    # Every entry is at least four one-byte varints, so a count the bytes in
    # hand cannot hold is a lie, and one to refuse before the lists below are
    # allocated for it.
    if count > len(data) // 4:
        raise PMTilesError(f"directory claims {count} entries in {len(data)} bytes")

    tile_ids: list[int] = []
    tile_id = 0
    for _ in range(count):
        # TileIDs are delta-encoded against the previous entry.
        tile_id += cursor.read()
        tile_ids.append(tile_id)
    run_lengths = [cursor.read() for _ in range(count)]
    lengths = [cursor.read() for _ in range(count)]

    offsets: list[int] = []
    for index in range(count):
        value = cursor.read()
        if value == 0:
            # 0 means "directly after the previous entry"; anything else is the
            # offset plus one, so that 0 can carry that meaning.
            if index == 0:
                raise PMTilesError("first directory entry has no offset")
            offsets.append(offsets[index - 1] + lengths[index - 1])
        else:
            offsets.append(value - 1)

    return [
        Entry(tile_id=tile_ids[i], offset=offsets[i], length=lengths[i], run_length=run_lengths[i])
        for i in range(count)
    ]


def tile_id_to_zxy(tile_id: int) -> tuple[int, int, int]:
    """Undo the spec's tile id: zoom levels stacked, each one a Hilbert curve."""
    if tile_id < 0:
        raise PMTilesError(f"negative tile id {tile_id}")
    zoom = 0
    first_id = 0
    while True:
        tiles_at_zoom = 1 << (2 * zoom)
        if tile_id - first_id < tiles_at_zoom:
            break
        first_id += tiles_at_zoom
        zoom += 1
        if zoom > 31:
            raise PMTilesError(f"tile id {tile_id} is past zoom 31")
    x, y = _hilbert_to_xy(zoom, tile_id - first_id)
    return zoom, x, y


def _hilbert_to_xy(zoom: int, position: int) -> tuple[int, int]:
    side = 1 << zoom
    x = 0
    y = 0
    remaining = position
    step = 1
    while step < side:
        rx = 1 & (remaining >> 1)
        ry = 1 & (remaining ^ rx)
        x, y = _rotate(step, x, y, rx, ry)
        x += step * rx
        y += step * ry
        remaining >>= 2
        step <<= 1
    return x, y


def _rotate(side: int, x: int, y: int, rx: int, ry: int) -> tuple[int, int]:
    if ry != 0:
        return x, y
    if rx == 1:
        return side - 1 - y, side - 1 - x
    return y, x


def _read_at(stream: BinaryIO, offset: int, length: int, what: str, cap: int) -> bytes:
    if length > cap:
        raise PMTilesError(f"{what} claims {length} bytes, over the {cap}-byte cap")
    stream.seek(offset)
    data = stream.read(length)
    if len(data) != length:
        raise PMTilesError(f"{what} is truncated: wanted {length} bytes, got {len(data)}")
    return data


def read_directory(stream: BinaryIO, header: Header, offset: int, length: int) -> list[Entry]:
    raw = _read_at(stream, offset, length, "directory", MAX_DIRECTORY_BYTES)
    return deserialize_directory(inflate(raw, header.internal_compression, MAX_DIRECTORY_BYTES))


def read_metadata(stream: BinaryIO, header: Header) -> dict[str, object]:
    """The archive's JSON metadata, or `{}` when it is absent or not an object."""
    if header.metadata_length == 0:
        return {}
    raw = _read_at(
        stream, header.metadata_offset, header.metadata_length, "metadata", MAX_METADATA_BYTES
    )
    text = inflate(raw, header.internal_compression, MAX_METADATA_BYTES).decode("utf-8")
    parsed = json.loads(text)
    return parsed if isinstance(parsed, dict) else {}


def _tile_entries(
    stream: BinaryIO, header: Header, directory: list[Entry], depth: int = 0
) -> Iterator[Entry]:
    """Every tile entry under `directory`, resolving leaves one at a time."""
    for entry in directory:
        if entry.run_length != 0:
            yield entry
            continue
        if depth >= MAX_DIRECTORY_DEPTH:
            raise PMTilesError(f"leaf directories nest more than {MAX_DIRECTORY_DEPTH} deep")
        leaf = read_directory(stream, header, header.leaf_offset + entry.offset, entry.length)
        yield from _tile_entries(stream, header, leaf, depth + 1)


def slice_archive(archive: Path, out_dir: Path) -> SliceResult:
    """Write every tile in `archive` as `out_dir/{z}/{x}/{y}.pbf`, MVT bytes inflated.

    Existing files are overwritten; delete `out_dir` first to be sure nothing
    from an older archive is left behind.
    """
    tiles = 0
    bytes_written = 0
    zooms: set[int] = set()

    with archive.open("rb") as stream:
        header = read_header(stream.read(HEADER_SIZE))
        check_supported(header)
        root = read_directory(stream, header, header.root_offset, header.root_length)
        metadata = read_metadata(stream, header)

        for entry in _tile_entries(stream, header, root):
            # A run addresses consecutive ids and writes a file for each, so
            # its length is a file count the header has already bounded: no
            # entry can address more tiles than the whole archive says it has.
            if (
                entry.run_length > header.addressed_tiles
                or tiles + entry.run_length > header.addressed_tiles
            ):
                raise PMTilesError(
                    f"entry at tile {entry.tile_id} runs {entry.run_length} tiles,"
                    f" past the {header.addressed_tiles} the header addresses"
                )
            raw = _read_at(
                stream,
                header.tile_data_offset + entry.offset,
                entry.length,
                f"tile {entry.tile_id}",
                MAX_TILE_BYTES,
            )
            data = inflate(raw, header.tile_compression, MAX_TILE_BYTES)
            # A run is one stored tile addressed by several consecutive ids —
            # ocean at low zoom, mostly. Read once, write each id.
            for tile_id in range(entry.tile_id, entry.tile_id + entry.run_length):
                zoom, x, y = tile_id_to_zxy(tile_id)
                path = out_dir / str(zoom) / str(x) / f"{y}.pbf"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                zooms.add(zoom)
                tiles += 1
                bytes_written += len(data)

    if tiles == 0:
        raise PMTilesError("archive holds no tiles")

    _write_tile_metadata(out_dir, header, metadata, tiles, min(zooms), max(zooms))
    return SliceResult(
        tiles=tiles,
        bytes_written=bytes_written,
        min_zoom=min(zooms),
        max_zoom=max(zooms),
        bounds=header.bounds,
    )


def _write_tile_metadata(
    out_dir: Path,
    header: Header,
    metadata: dict[str, object],
    tiles: int,
    min_zoom: int,
    max_zoom: int,
) -> None:
    """Record what the archive said, so `site/build.py` does not have to re-open it.

    The tile tree alone only gives bounds rounded out to whole tiles; these are
    the bounds the extract was cut to.

    The archive's own `attribution` is deliberately not carried over: it is an
    HTML anchor pointing at openstreetmap.org, and nothing in this repository
    ships a remote URL. `web/basemap/style.json` holds the plain-text credit the
    page actually renders, and `site/build.py` keeps that one.
    """
    west, south, east, north = header.bounds
    summary = {
        "name": metadata.get("name"),
        "format": "pbf",
        "bounds": [west, south, east, north],
        "center": [header.center[0], header.center[1], header.center_zoom],
        "minzoom": min_zoom,
        "maxzoom": max_zoom,
        "header_minzoom": header.min_zoom,
        "header_maxzoom": header.max_zoom,
        "tile_count": tiles,
    }
    (out_dir / "metadata.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("archive", type=Path, help="the PMTiles v3 file to read")
    parser.add_argument("out_dir", type=Path, help="directory to write {z}/{x}/{y}.pbf into")
    args = parser.parse_args(argv)

    with args.archive.open("rb") as stream:
        header = read_header(stream.read(HEADER_SIZE))
    check_supported(header)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    result = slice_archive(args.archive, args.out_dir)

    west, south, east, north = result.bounds
    print(f"archive     {args.archive} ({args.archive.stat().st_size:,} bytes)")
    print(
        f"encoding    tiles {TILE_TYPE_NAMES[header.tile_type]}/"
        f"{COMPRESSION_NAMES.get(header.tile_compression)}, "
        f"directories {COMPRESSION_NAMES.get(header.internal_compression)}"
    )
    print(
        f"zooms       {result.min_zoom}-{result.max_zoom} (header {header.min_zoom}-{header.max_zoom})"
    )
    print(f"bounds      {west:.6f},{south:.6f},{east:.6f},{north:.6f}")
    print(f"tiles       {result.tiles:,} written to {args.out_dir}")
    print(f"bytes       {result.bytes_written:,} inflated MVT")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
