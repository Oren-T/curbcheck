"""Compile the database into the static site's data pack.

`curbcheck pack --out DIR` writes `meta.json` and three gzipped JSON files —
rules, streets and geocode — that the browser build of the engine and the
geocoder read in place of SQLite (docs/STATIC_SITE.md, "The pack"). Nothing
here runs in the server; this is a second reader of the same file.

Two properties the JS depends on, and the reasons for them:

- every table is written in SQLite `rowid` order, and every reference from one
  row to another is rewritten as a row index (-1 for "no row"), so the loader
  can hold a table as parallel typed arrays and a join is an array lookup;
- numbers go through `json.dumps`, which writes the shortest repr that reads
  back as the same double, so no coordinate and no confidence changes value on
  the way out. The differential harness compares distances and ranks exactly,
  which only means something if the two implementations see the same bits.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from curbcheck.db import connect, json_string_list, read_snapshot
from curbcheck.engine.coverage import COVERAGE_AREA, coverage_bbox
from curbcheck.engine.search import calendar_is_missing
from curbcheck.model import Flags

# Bumped when the loader would misread an older pack. `site/static/pack/loader.js`
# refuses anything else.
FORMAT_VERSION = 1

META_NAME = "meta.json"

# gzip level 9 with mtime 0: the name carries the sha256 of these bytes, so the
# same database has to compile to the same file or every deploy looks changed.
_GZIP_LEVEL = 9
_HASH_CHARS = 12

# Rates are money and are parsed as cents in the browser (docs/STATIC_SITE.md,
# "Money"). A third decimal has no cent to round to, so it is a build failure
# rather than something the JS has to guess at.
_MAX_RATE_DECIMALS = 2

# `model.Flags` in declaration order: the pack ships bit i for field i, and
# `site/static/engine/resolve.js` reads it back with the same list.
_FLAG_FIELDS: tuple[str, ...] = tuple(Flags.model_fields)

# `docs/SECURITY.md`: a message never carries a path from the build machine,
# and `sync_meta.calendar_source_path` is one.
_PATH_VALUE_PREFIX = "/"

_NODES_SQL = "SELECT node_id, lon, lat, street_names FROM street_node ORDER BY rowid"
_SEGMENTS_SQL = (
    "SELECT segment_id, street_name, street_norm, from_node, to_node, width_ft, length_ft,"
    " geom, left_low_address, left_high_address, right_low_address, right_high_address"
    " FROM street_segment ORDER BY rowid"
)
_SIGNS_SQL = (
    "SELECT sign_id, order_number, on_street, from_street, to_street, side_of_street,"
    " distance_from_intersection, arrow_direction, facing_direction, sign_code,"
    " sign_description, segment_id, snap_confidence, snap_notes, is_regulation, panel_class"
    " FROM sign ORDER BY rowid"
)
_SPANS_SQL = (
    "SELECT reg_seg_id, segment_id, side, start_ft, end_ft, geom, length_ft, capacity_cars,"
    " capacity_approximate, confidence, derived_from, gap_kind"
    " FROM regulation_segment ORDER BY rowid"
)
_REGULATIONS_SQL = (
    "SELECT reg_id, reg_seg_id, action, permitted, vehicle_class, exclusive, days_mask,"
    " time_from, time_to, metered, max_duration_min, flags, effective_from, effective_to,"
    " arrow, raw_sign_description, parse_method, parse_confidence"
    " FROM regulation ORDER BY rowid"
)
_METER_RATES_SQL = (
    "SELECT blockface_id, segment_id, side, rate_label, hour_rates, commercial_hour_rates,"
    " max_session_min, source, confidence FROM meter_rate ORDER BY rowid"
)
_CALENDAR_SQL = (
    "SELECT date, is_major_legal_holiday, meters_suspended, label"
    " FROM asp_suspension ORDER BY rowid"
)
_ADDRESS_POINTS_SQL = (
    "SELECT street_norm, house_number, display, zipcode, lon, lat FROM address_point ORDER BY rowid"
)
_INTERSECTIONS_SQL = "SELECT a_norm, b_norm, display, lon, lat FROM intersection ORDER BY rowid"
_PLACES_SQL = "SELECT place_id, display, lon, lat FROM place ORDER BY rowid"
# `place_token` and `street_token` are WITHOUT ROWID tables, so their primary
# key *is* the order a scan produces; that is the order the JS keeps.
_PLACE_TOKENS_SQL = (
    "SELECT token, position, place_id, search_name FROM place_token"
    " ORDER BY token, position, place_id, search_name"
)
_STREETS_SQL = "SELECT street_norm, display, lon, lat FROM street ORDER BY rowid"
_STREET_VARIANTS_SQL = (
    "SELECT variant, street_norm FROM street_variant ORDER BY variant, street_norm"
)
_STREET_TOKENS_SQL = (
    "SELECT token, position, street_norm, search_name FROM street_token"
    " ORDER BY token, position, street_norm, search_name"
)
_ZIP_CENTROIDS_SQL = "SELECT zipcode, lon, lat, address_points FROM zip_centroid ORDER BY rowid"
_SYNC_META_SQL = "SELECT key, value FROM sync_meta ORDER BY rowid"


class PackError(Exception):
    """The database holds something the pack format cannot carry."""


@dataclass(frozen=True)
class PackFile:
    """One file written into the pack directory."""

    name: str
    raw_bytes: int
    stored_bytes: int


@dataclass(frozen=True)
class PackReport:
    """What `compile_pack` wrote, for the CLI to print and the tests to check."""

    directory: Path
    meta: dict[str, Any]
    meta_bytes: int
    files: tuple[PackFile, ...]

    @property
    def raw_total(self) -> int:
        return sum(item.raw_bytes for item in self.files)

    @property
    def stored_total(self) -> int:
        return sum(item.stored_bytes for item in self.files)


def compile_pack(db_path: Path, out_dir: Path) -> PackReport:
    """Read `db_path` and write a complete pack into `out_dir`.

    Any `.json.gz` already in the directory from an earlier build is removed,
    so a stale file cannot be picked up by `site/build.py` or served next to
    the `meta.json` that no longer names it.
    """
    conn = connect(db_path, readonly=True)
    try:
        # One read transaction: `curbcheck sync` can swap a new database in
        # under a long compile, and half of each is not a pack.
        with read_snapshot(conn):
            return _compile(conn, out_dir)
    finally:
        conn.close()


def report_lines(report: PackReport) -> list[str]:
    """The size table `curbcheck pack` prints. Both sizes, because both are paid."""
    lines = [
        f"{item.name}: {_mib(item.raw_bytes)} MiB raw, {_mib(item.stored_bytes)} MiB gzipped"
        for item in report.files
    ]
    lines.append(f"{META_NAME}: {report.meta_bytes} bytes, uncompressed")
    lines.append(
        f"total: {_mib(report.raw_total)} MiB raw, {_mib(report.stored_total)} MiB gzipped"
        f" in {report.directory}"
    )
    return lines


def _compile(conn: sqlite3.Connection, out_dir: Path) -> PackReport:
    nodes = _fetch(conn, _NODES_SQL)
    segments = _fetch(conn, _SEGMENTS_SQL)
    signs = _fetch(conn, _SIGNS_SQL)
    spans = _fetch(conn, _SPANS_SQL)
    regulations = _fetch(conn, _REGULATIONS_SQL)

    node_rows = _row_index(nodes, "node_id")
    segment_rows = _row_index(segments, "segment_id")
    sign_rows = _row_index(signs, "sign_id")
    span_rows = _row_index(spans, "reg_seg_id")

    dropped_sign_refs = _Counter()
    rules = _rules_payload(
        conn,
        spans=spans,
        regulations=regulations,
        signs=signs,
        segment_rows=segment_rows,
        sign_rows=sign_rows,
        span_rows=span_rows,
        dropped=dropped_sign_refs,
    )
    streets = _streets_payload(segments=segments, nodes=nodes, node_rows=node_rows)
    geocode = _geocode_payload(conn)

    out_dir.mkdir(parents=True, exist_ok=True)
    files = tuple(
        _write_table_file(out_dir, label, payload)
        for label, payload in (("rules", rules), ("streets", streets), ("geocode", geocode))
    )
    meta = _meta(
        conn,
        files=files,
        counts={
            "spans": len(spans),
            "regulations": len(regulations),
            "signs": len(signs),
            "segments": len(segments),
        },
        dropped_sign_refs=dropped_sign_refs.value,
    )
    meta_bytes = _write_meta(out_dir, meta)
    _remove_stale_files(out_dir, keep={item.name for item in files})
    return PackReport(directory=out_dir, meta=meta, meta_bytes=meta_bytes, files=files)


def _rules_payload(
    conn: sqlite3.Connection,
    *,
    spans: Sequence[sqlite3.Row],
    regulations: Sequence[sqlite3.Row],
    signs: Sequence[sqlite3.Row],
    segment_rows: Mapping[str, int],
    sign_rows: Mapping[str, int],
    span_rows: Mapping[str, int],
    dropped: _Counter,
) -> dict[str, Any]:
    dicts = _Dictionaries()
    tables = {
        "spans": _spans_table(spans, dicts, segment_rows, sign_rows, dropped),
        "regulations": _regulations_table(regulations, dicts, span_rows),
        "signs": _signs_table(signs, dicts, segment_rows),
        "meter_rates": _meter_rates_table(_fetch(conn, _METER_RATES_SQL), dicts, segment_rows),
        "calendar": _calendar_table(_fetch(conn, _CALENDAR_SQL)),
    }
    return {"tables": tables, "dicts": dicts.as_json()}


def _streets_payload(
    *,
    segments: Sequence[sqlite3.Row],
    nodes: Sequence[sqlite3.Row],
    node_rows: Mapping[str, int],
) -> dict[str, Any]:
    # No dictionary columns: `street_name` repeats, but the streets file is the
    # smallest of the three and the loader would pay a decode pass for it.
    tables = {
        "segments": _segments_table(segments, node_rows),
        "nodes": _nodes_table(nodes),
    }
    return {"tables": tables, "dicts": {}}


def _geocode_payload(conn: sqlite3.Connection) -> dict[str, Any]:
    dicts = _Dictionaries()
    address_points = _fetch(conn, _ADDRESS_POINTS_SQL)
    tables = {
        "address_points": _table(
            len(address_points),
            {
                # 63,245 doors over ~1,000 streets, so the code is a tenth of
                # the string it replaces.
                "streetNorm": dicts.encode("street_norms", _column(address_points, "street_norm")),
                "houseNumber": _column(address_points, "house_number"),
                "display": _column(address_points, "display"),
                "zipcode": _column(address_points, "zipcode"),
                "lon": _column(address_points, "lon"),
                "lat": _column(address_points, "lat"),
            },
        ),
        "intersections": _plain_table(
            _fetch(conn, _INTERSECTIONS_SQL),
            {
                "aNorm": "a_norm",
                "bNorm": "b_norm",
                "display": "display",
                "lon": "lon",
                "lat": "lat",
            },
        ),
        "places": _plain_table(
            _fetch(conn, _PLACES_SQL),
            {"placeId": "place_id", "display": "display", "lon": "lon", "lat": "lat"},
        ),
        "place_tokens": _plain_table(
            _fetch(conn, _PLACE_TOKENS_SQL),
            {
                "token": "token",
                "position": "position",
                "placeId": "place_id",
                "searchName": "search_name",
            },
        ),
        "streets": _plain_table(
            _fetch(conn, _STREETS_SQL),
            {"streetNorm": "street_norm", "display": "display", "lon": "lon", "lat": "lat"},
        ),
        "street_variants": _plain_table(
            _fetch(conn, _STREET_VARIANTS_SQL), {"variant": "variant", "streetNorm": "street_norm"}
        ),
        "street_tokens": _plain_table(
            _fetch(conn, _STREET_TOKENS_SQL),
            {
                "token": "token",
                "position": "position",
                "streetNorm": "street_norm",
                "searchName": "search_name",
            },
        ),
        "zip_centroids": _plain_table(
            _fetch(conn, _ZIP_CENTROIDS_SQL),
            {
                "zipcode": "zipcode",
                "lon": "lon",
                "lat": "lat",
                "addressPoints": "address_points",
            },
        ),
    }
    return {"tables": tables, "dicts": dicts.as_json()}


def _spans_table(
    rows: Sequence[sqlite3.Row],
    dicts: _Dictionaries,
    segment_rows: Mapping[str, int],
    sign_rows: Mapping[str, int],
    dropped: _Counter,
) -> dict[str, Any]:
    return _table(
        len(rows),
        {
            "regSegId": _column(rows, "reg_seg_id"),
            "segment": _references(rows, "segment_id", segment_rows),
            "segmentId": _column(rows, "segment_id"),
            "side": dicts.encode("sides", _column(rows, "side")),
            "startFt": _column(rows, "start_ft"),
            "endFt": _column(rows, "end_ft"),
            "geom": _coords_column(rows, "geom", "reg_seg_id"),
            "lengthFt": _column(rows, "length_ft"),
            "capacityCars": _column(rows, "capacity_cars"),
            "capacityApproximate": _column(rows, "capacity_approximate"),
            "confidence": _column(rows, "confidence"),
            "derivedFrom": _lists_column(
                [_sign_references(row, sign_rows, dropped) for row in rows]
            ),
            "gapKind": dicts.encode("gap_kinds", _column(rows, "gap_kind")),
        },
    )


def _regulations_table(
    rows: Sequence[sqlite3.Row], dicts: _Dictionaries, span_rows: Mapping[str, int]
) -> dict[str, Any]:
    return _table(
        len(rows),
        {
            "regId": _column(rows, "reg_id"),
            "span": _references(rows, "reg_seg_id", span_rows),
            "action": dicts.encode("actions", _column(rows, "action")),
            "permitted": _column(rows, "permitted"),
            "vehicleClass": dicts.encode("vehicle_classes", _column(rows, "vehicle_class")),
            "exclusive": _column(rows, "exclusive"),
            "daysMask": _column(rows, "days_mask"),
            "timeFrom": _column(rows, "time_from"),
            "timeTo": _column(rows, "time_to"),
            "metered": _column(rows, "metered"),
            "maxDurationMin": _column(rows, "max_duration_min"),
            "flags": [_flag_bits(row) for row in rows],
            "effectiveFrom": _column(rows, "effective_from"),
            "effectiveTo": _column(rows, "effective_to"),
            "arrow": dicts.encode("arrows", _column(rows, "arrow")),
            "rawSignDescription": dicts.encode(
                "descriptions", _column(rows, "raw_sign_description")
            ),
            "parseMethod": dicts.encode("parse_methods", _column(rows, "parse_method")),
            "parseConfidence": _column(rows, "parse_confidence"),
        },
    )


def _signs_table(
    rows: Sequence[sqlite3.Row], dicts: _Dictionaries, segment_rows: Mapping[str, int]
) -> dict[str, Any]:
    return _table(
        len(rows),
        {
            "signId": _column(rows, "sign_id"),
            "orderNumber": _column(rows, "order_number"),
            # The three blockface names share one dictionary: they are drawn
            # from the same ~2,000 street spellings.
            "onStreet": dicts.encode("street_names", _column(rows, "on_street")),
            "fromStreet": dicts.encode("street_names", _column(rows, "from_street")),
            "toStreet": dicts.encode("street_names", _column(rows, "to_street")),
            "sideOfStreet": _column(rows, "side_of_street"),
            "distanceFromIntersection": _column(rows, "distance_from_intersection"),
            "arrowDirection": dicts.encode("arrow_directions", _column(rows, "arrow_direction")),
            "facingDirection": dicts.encode("facing_directions", _column(rows, "facing_direction")),
            "signCode": dicts.encode("sign_codes", _column(rows, "sign_code")),
            "signDescription": dicts.encode("descriptions", _column(rows, "sign_description")),
            "segment": _references(rows, "segment_id", segment_rows),
            "segmentId": _column(rows, "segment_id"),
            "snapConfidence": _column(rows, "snap_confidence"),
            "snapNotes": dicts.encode("snap_notes", _column(rows, "snap_notes")),
            "isRegulation": _column(rows, "is_regulation"),
            "panelClass": dicts.encode("panel_classes", _column(rows, "panel_class")),
        },
    )


def _meter_rates_table(
    rows: Sequence[sqlite3.Row], dicts: _Dictionaries, segment_rows: Mapping[str, int]
) -> dict[str, Any]:
    for row in rows:
        _check_rates(row, "hour_rates")
        _check_rates(row, "commercial_hour_rates")
    return _table(
        len(rows),
        {
            "blockfaceId": _column(rows, "blockface_id"),
            "segment": _references(rows, "segment_id", segment_rows),
            "segmentId": _column(rows, "segment_id"),
            "side": dicts.encode("sides", _column(rows, "side")),
            "rateLabel": dicts.encode("rate_labels", _column(rows, "rate_label")),
            # Left as the stored JSON string: money never round-trips through a
            # float, here or in `engine.search._decimal_list`.
            "hourRates": _column(rows, "hour_rates"),
            "commercialHourRates": _column(rows, "commercial_hour_rates"),
            "maxSessionMin": _column(rows, "max_session_min"),
            "source": _column(rows, "source"),
            "confidence": _column(rows, "confidence"),
        },
    )


def _calendar_table(rows: Sequence[sqlite3.Row]) -> dict[str, Any]:
    return _plain_table(
        rows,
        {
            "date": "date",
            "isMajorLegalHoliday": "is_major_legal_holiday",
            "metersSuspended": "meters_suspended",
            "label": "label",
        },
    )


def _segments_table(rows: Sequence[sqlite3.Row], node_rows: Mapping[str, int]) -> dict[str, Any]:
    return _table(
        len(rows),
        {
            "segmentId": _column(rows, "segment_id"),
            "streetName": _column(rows, "street_name"),
            "streetNorm": _column(rows, "street_norm"),
            "fromNode": _references(rows, "from_node", node_rows),
            "toNode": _references(rows, "to_node", node_rows),
            "widthFt": _column(rows, "width_ft"),
            "lengthFt": _column(rows, "length_ft"),
            "geom": _coords_column(rows, "geom", "segment_id"),
            "leftLowAddress": _column(rows, "left_low_address"),
            "leftHighAddress": _column(rows, "left_high_address"),
            "rightLowAddress": _column(rows, "right_low_address"),
            "rightHighAddress": _column(rows, "right_high_address"),
        },
    )


def _nodes_table(rows: Sequence[sqlite3.Row]) -> dict[str, Any]:
    return _plain_table(
        rows, {"nodeId": "node_id", "lon": "lon", "lat": "lat", "streetNames": "street_names"}
    )


class _Counter:
    """A count the table builders can add to without threading a return value back."""

    def __init__(self) -> None:
        self.value = 0

    def add(self, amount: int = 1) -> None:
        self.value += amount


class _Dictionaries:
    """The string dictionaries of one pack file, built as the columns are encoded."""

    def __init__(self) -> None:
        self._codes: dict[str, dict[str, int]] = {}
        self._strings: dict[str, list[str]] = {}

    def encode(self, name: str, values: Sequence[Any]) -> dict[str, Any]:
        """`values` as codes into the named dictionary; -1 for null."""
        codes = self._codes.setdefault(name, {})
        strings = self._strings.setdefault(name, [])
        encoded: list[int] = []
        for value in values:
            if value is None:
                encoded.append(-1)
                continue
            text = str(value)
            code = codes.get(text)
            if code is None:
                code = len(strings)
                codes[text] = code
                strings.append(text)
            encoded.append(code)
        return {"dict": name, "codes": encoded}

    def as_json(self) -> dict[str, list[str]]:
        return dict(self._strings)


def _fetch(conn: sqlite3.Connection, query: str) -> list[sqlite3.Row]:
    return list(conn.execute(query))


def _table(rows: int, columns: dict[str, Any]) -> dict[str, Any]:
    return {"rows": rows, "columns": columns}


def _plain_table(rows: Sequence[sqlite3.Row], columns: Mapping[str, str]) -> dict[str, Any]:
    """A table whose every column is stored as it comes out of SQLite."""
    return _table(len(rows), {name: _column(rows, source) for name, source in columns.items()})


def _column(rows: Sequence[sqlite3.Row], name: str) -> list[Any]:
    return [row[name] for row in rows]


def _row_index(rows: Sequence[sqlite3.Row], key: str) -> dict[str, int]:
    return {str(row[key]): index for index, row in enumerate(rows)}


def _references(rows: Sequence[sqlite3.Row], name: str, target: Mapping[str, int]) -> list[int]:
    """A foreign-key column as row indexes, -1 where it is null or names no row."""
    return [-1 if row[name] is None else target.get(str(row[name]), -1) for row in rows]


def _sign_references(
    row: sqlite3.Row, sign_rows: Mapping[str, int], dropped: _Counter
) -> list[int]:
    """`derived_from` as row indexes into `signs`, minus the ids that name no sign.

    A dangling id means the sign was dropped as a duplicate after the span was
    built; the JS has no row to show for it, so it is counted in `meta.json`
    rather than shipped as a hole.
    """
    references = []
    for sign_id in json_string_list(row["derived_from"]):
        index = sign_rows.get(sign_id)
        if index is None:
            dropped.add()
            continue
        references.append(index)
    return references


def _coords_column(rows: Sequence[sqlite3.Row], name: str, id_name: str) -> dict[str, Any]:
    """A GeoJSON LineString column as flat `[lon, lat, …]` with per-row offsets."""
    coordinates: list[float] = []
    offsets: list[int] = [0]
    for row in rows:
        for lon, lat in _line_string(row[name], str(row[id_name])):
            coordinates.append(lon)
            coordinates.append(lat)
        offsets.append(len(coordinates) // 2)
    return {"coords": coordinates, "offsets": offsets}


def _line_string(value: Any, row_id: str) -> list[tuple[float, float]]:
    """The vertices of a stored geometry.

    Only LineStrings: all 47,274 geometries in the database are one
    (docs/STATIC_SITE.md, "Porting rules"), and a MultiLineString would need a
    second offsets level that nothing would read. A build is the right place to
    fail on it, so the browser never has to.
    """
    try:
        parsed = json.loads(str(value))
        kind = parsed["type"]
        coordinates = parsed["coordinates"]
    except (ValueError, TypeError, KeyError) as error:
        raise PackError(f"{row_id}: geometry is not readable GeoJSON") from error
    if kind != "LineString":
        raise PackError(f"{row_id}: geometry is a {kind}, and the pack carries only LineStrings")
    return [(float(point[0]), float(point[1])) for point in coordinates]


def _lists_column(lists: Sequence[Sequence[int]]) -> dict[str, Any]:
    flat: list[int] = []
    offsets: list[int] = [0]
    for values in lists:
        flat.extend(values)
        offsets.append(len(flat))
    return {"lists": flat, "offsets": offsets}


def _flag_bits(row: sqlite3.Row) -> int:
    """`regulation.flags` as a bitmask over `model.Flags` in declaration order.

    The stored JSON is untrusted like every other cell (CLAUDE.md), so it goes
    back through the model rather than being read as a dict.
    """
    try:
        flags = Flags.model_validate_json(str(row["flags"]))
    except ValueError as error:
        raise PackError(f"{row['reg_id']}: flags are not a readable Flags object") from error
    bits = 0
    for index, field in enumerate(_FLAG_FIELDS):
        if getattr(flags, field):
            bits |= 1 << index
    return bits


def _check_rates(row: sqlite3.Row, name: str) -> None:
    """Refuse a rate the browser's cent arithmetic could not reproduce exactly.

    A string that is not a number at all is left alone: `engine.search.
    _decimal_list` drops the whole list when it meets one, and `money.js` does
    the same, so the two agree without the build failing.
    """
    for item in json_string_list(row[name]):
        try:
            rate = Decimal(item)
        except InvalidOperation:
            continue
        if not rate.is_finite():
            continue
        decimals = -int(rate.as_tuple().exponent)
        if decimals > _MAX_RATE_DECIMALS:
            raise PackError(
                f"{row['blockface_id']}: {name} holds {item!r}, which is not a whole number"
                " of cents"
            )


def _meta(
    conn: sqlite3.Connection,
    *,
    files: Sequence[PackFile],
    counts: Mapping[str, int],
    dropped_sign_refs: int,
) -> dict[str, Any]:
    bbox = coverage_bbox(conn)
    return {
        "format": FORMAT_VERSION,
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "sync": _sync_meta(conn),
        "coverage": None if bbox is None else {"area": COVERAGE_AREA, "bbox": list(bbox)},
        "calendar_missing": calendar_is_missing(conn),
        "counts": dict(counts),
        "dropped_sign_refs": dropped_sign_refs,
        "files": {
            label: item.name
            for label, item in zip(("rules", "streets", "geocode"), files, strict=True)
        },
    }


def _sync_meta(conn: sqlite3.Connection) -> dict[str, str]:
    """`sync_meta` as strings, without the keys whose value is a build-machine path."""
    return {
        str(row["key"]): str(row["value"])
        for row in conn.execute(_SYNC_META_SQL)
        if not str(row["value"]).startswith(_PATH_VALUE_PREFIX)
    }


def _write_table_file(out_dir: Path, label: str, payload: Mapping[str, Any]) -> PackFile:
    """Write one gzipped table file, named by the sha256 of the bytes it holds."""
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    compressed = gzip.compress(raw, compresslevel=_GZIP_LEVEL, mtime=0)
    digest = hashlib.sha256(compressed).hexdigest()[:_HASH_CHARS]
    name = label + "." + digest + ".json.gz"
    (out_dir / name).write_bytes(compressed)
    return PackFile(name=name, raw_bytes=len(raw), stored_bytes=len(compressed))


def _write_meta(out_dir: Path, meta: Mapping[str, Any]) -> int:
    """Write `meta.json`, indented: it is small, fetched fresh every time, and read by people."""
    raw = (json.dumps(meta, indent=2) + "\n").encode("utf-8")
    (out_dir / META_NAME).write_bytes(raw)
    return len(raw)


def _remove_stale_files(out_dir: Path, *, keep: Iterable[str]) -> None:
    kept = set(keep)
    for path in out_dir.glob("*.json.gz"):
        if path.name not in kept:
            path.unlink()


def _mib(size: int) -> str:
    return f"{size / (1024 * 1024):.1f}"
