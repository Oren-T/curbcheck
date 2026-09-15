"""Prototype for offline address autocomplete over NYC AddressPoint + our centerline.

Exploration-phase tool. The index build and the whole query path are stdlib
plus `sqlite3`; the only import from the package is `normalize_street_name`, so
that a name typed by a user is folded exactly the way the ETL folded the data.
It answers the question in docs/ux/AUTOCOMPLETE_RESEARCH.md: can a
Google-Maps-feel suggestion list be served from local data, in under 20 ms, with
zero network per keystroke?

The index it builds is a *vocabulary* index, not a text index over whole
address strings. In Manhattan the only hard part of a typed query is the street
name — the house number is just an integer, and the query grammar ("<n> <st>",
"<st> & <st>", zip, place) is tiny and unambiguous. So the build folds every
street into a handful of spelling variants with `curbcheck.etl.streets`'s own
normalizer, and the query path parses first and looks up second. That is what
makes "lex & 86", "86 & 3" and "1519 third avenue" all resolve with one
prefix-range scan each instead of a fuzzy search over 63k strings.

Usage:
    python scripts/explore_addresses.py fetch     # -> data/raw/*.json + .meta.json
    python scripts/explore_addresses.py profile   # dataset facts for the doc
    python scripts/explore_addresses.py build     # -> data/explore/autocomplete.sqlite
    python scripts/explore_addresses.py demo      # 15 example queries, top 3 each
    python scripts/explore_addresses.py bench     # latency over 20 queries
    python scripts/explore_addresses.py accuracy  # interpolation error vs surveyed points
    python scripts/explore_addresses.py query "1519 3rd ave"
"""

from __future__ import annotations

import collections
import hashlib
import json
import re
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "data" / "raw"
EXPLORE_DIR = REPO_ROOT / "data" / "explore"
CURBCHECK_DB = REPO_ROOT / "data" / "curbcheck.sqlite"
INDEX_DB = EXPLORE_DIR / "autocomplete.sqlite"

SOCRATA_HOST = "https://data.cityofnewyork.us"
PAGE_SIZE = 50_000

# dataset id -> (filename stem, SoQL $where). boroughcode 1 is Manhattan in
# both OTI datasets; the centerline uses the same code (docs/DATA.md §2.1).
DATASETS: dict[str, tuple[str, str]] = {
    "uf93-f8nk": ("address_points_manhattan", "boroughcode='1'"),
    "t95h-5fsr": ("common_places_manhattan", "boroughcode='1'"),
}

# Longest query the parser will look at. Same cap as curbcheck.geocode, so the
# prototype and the shipped endpoint agree on what "too long" means.
MAX_QUERY_CHARS = 200

# Suggestions returned to a UI. Eight is what fits a dropdown without scrolling.
DEFAULT_LIMIT = 8

# Confidence by how the candidate was found. A surveyed address point is the
# only thing here worth calling exact; everything under 0.6 is "roughly there".
CONFIDENCE_ADDRESS_POINT = 0.98
CONFIDENCE_INTERSECTION = 0.95
CONFIDENCE_PLACE = 0.85
CONFIDENCE_ADDRESS_INTERPOLATED = 0.75
CONFIDENCE_NEAR_ADDRESS = 0.60
# A street the whole query resolved to ("broadwa", "86th st") is what the user
# is naming; a street offered because half of an unmatched intersection hit it
# is a consolation prize, and ranks below a partial place-name match.
CONFIDENCE_STREET_WHOLE_QUERY = 0.45
CONFIDENCE_STREET = 0.30
CONFIDENCE_ZIP = 0.25

# A place name matching only some of its own words is a weak hit ("86 ST"
# matches "CTL PK W DR OV 86 ST TRNVS RD"), so the score scales with how much
# of the place's name the query accounted for, and weak hits are dropped.
MIN_PLACE_COVERAGE = 0.5

# A fuzzy street match is a guess about what the user meant, so it costs a
# fifth of the candidate's confidence rather than being silently as good.
FUZZY_CONFIDENCE_PENALTY = 0.8

# Feet per degree of latitude at 40.7°N. Only used for "how far is the nearest
# address point", where a flat-earth approximation is good to a fraction of a
# percent over Manhattan.
FT_PER_DEG_LAT = 364_000.0

sys.path.insert(0, str(REPO_ROOT))
from curbcheck.etl.streets import WORD_FORMS, normalize_street_name  # noqa: E402

# "&", "and", "at", "@" or a slash between two street names.
_INTERSECTION_SPLIT = re.compile(r"\s+(?:&|AND|AT)\s+|\s*[&@/]\s*")
_HOUSE_NUMBER = re.compile(r"^(\d{1,6})(?:-\d{1,6})?[A-Z]?\s+(.+)$")
_DIGIT_ORDINAL = re.compile(r"\b(\d+)(?:ST|ND|RD|TH)\b")
_ZIP = re.compile(r"^\d{5}$")
_PUNCTUATION = re.compile(r"[^A-Z0-9 ]")
_WHITESPACE = re.compile(r"\s+")
_NUMBERED_STREET = re.compile(r"^(?:([EW]) )?(\d{1,3})(?: (ST|AVE|PL|DR|WALK))?$")

# Buildings are written "1 WORLD TRADE CENTER" in CommonPlace and said "One
# World Trade Center" by everyone else. Only applied to place names, never to
# street names, where these words do not appear as numbers.
CARDINALS: dict[str, str] = {
    "ONE": "1",
    "TWO": "2",
    "THREE": "3",
    "FOUR": "4",
    "FIVE": "5",
    "SIX": "6",
    "SEVEN": "7",
    "EIGHT": "8",
    "NINE": "9",
    "TEN": "10",
}

# What New Yorkers say instead of the official name. Values are already in
# `normalize_street_name` form, so they are looked up, not re-folded.
NICKNAMES: dict[str, str] = {
    "LEX": "LEXINGTON AVE",
    "MAD": "MADISON AVE",
    "BWAY": "BROADWAY",
    "B WAY": "BROADWAY",
    "6 AVE": "AVE OF THE AMERICAS",
    "SIXTH AVE": "AVE OF THE AMERICAS",
    "AVE OF AMERICAS": "AVE OF THE AMERICAS",
    "FDR": "FRANKLIN D ROOSEVELT DR",
    "FDR DR": "FRANKLIN D ROOSEVELT DR",
    "WEST SIDE HWY": "JOE DIMAGGIO HWY",
    "WEST ST": "WEST ST",
    "MLK BLVD": "W 125 ST",
    "DR M L KING JR BLVD": "W 125 ST",
    "CPW": "CENTRAL PARK W",
    "ACP BLVD": "ADAM CLAYTON POWELL JR BLVD",
    "AMSTERDAM": "AMSTERDAM AVE",
    "COLUMBUS": "COLUMBUS AVE",
    "PARK": "PARK AVE",
    "PAS": "PARK AVE S",
    "RSD": "RIVERSIDE DR",
}

_SCHEMA = """
CREATE TABLE street (
    street_norm TEXT PRIMARY KEY,
    display     TEXT NOT NULL,
    lon         REAL NOT NULL,
    lat         REAL NOT NULL
);
-- One row per spelling a user might type. `variant` is the lookup key; the
-- prefix scan is a range query on this table's primary key.
CREATE TABLE street_variant (
    variant     TEXT NOT NULL,
    street_norm TEXT NOT NULL,
    PRIMARY KEY (variant, street_norm)
) WITHOUT ROWID;
CREATE TABLE address (
    street_norm TEXT NOT NULL,
    house       INTEGER NOT NULL,
    display     TEXT NOT NULL,
    zipcode     TEXT,
    lon         REAL NOT NULL,
    lat         REAL NOT NULL
);
CREATE INDEX address_by_street ON address (street_norm, house, lon, lat, display, zipcode);
-- Covers the bounding-box prefilter the reverse lookup does on a dropped pin.
CREATE INDEX address_by_lon ON address (lon, lat, display);
-- Both orders are stored so the lookup never has to try the pair twice.
CREATE TABLE intersection (
    a_norm  TEXT NOT NULL,
    b_norm  TEXT NOT NULL,
    display TEXT NOT NULL,
    lon     REAL NOT NULL,
    lat     REAL NOT NULL
);
CREATE INDEX intersection_by_pair ON intersection (a_norm, b_norm, lon, lat, display);
CREATE TABLE zipcode (
    zipcode TEXT PRIMARY KEY,
    lon     REAL NOT NULL,
    lat     REAL NOT NULL,
    points  INTEGER NOT NULL
);
CREATE TABLE place (
    place_id INTEGER PRIMARY KEY,
    display  TEXT NOT NULL,
    lon      REAL NOT NULL,
    lat      REAL NOT NULL,
    tokens   INTEGER NOT NULL
);
-- Token-level prefix index over place names, so "world trade" reaches
-- "ONE WORLD TRADE CENTER" without a full scan.
CREATE TABLE place_token (
    token    TEXT NOT NULL,
    place_id INTEGER NOT NULL,
    PRIMARY KEY (token, place_id)
) WITHOUT ROWID;
"""


@dataclass(frozen=True)
class Suggestion:
    """One autocomplete row: what to show, where it is, and how sure we are."""

    kind: str
    label: str
    lat: float
    lon: float
    confidence: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "lat": round(self.lat, 6),
            "lon": round(self.lon, 6),
            "confidence": round(self.confidence, 2),
        }


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------


def fetch_json(url: str, timeout: int = 300) -> list[dict[str, Any]]:
    if not url.startswith(SOCRATA_HOST + "/"):
        raise ValueError(f"refusing URL outside the Open Data host: {url}")
    request = urllib.request.Request(url, headers={"Accept": "application/json"})  # noqa: S310
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        payload: list[dict[str, Any]] = json.load(response)
    return payload


def fetch_dataset(dataset_id: str, where: str) -> None:
    """Page a Socrata resource into data/raw with the same sidecar the ETL writes."""
    stem, _ = DATASETS[dataset_id]
    rows: list[dict[str, Any]] = []
    first_url = ""
    offset = 0
    while True:
        params = {
            "$limit": str(PAGE_SIZE),
            "$offset": str(offset),
            "$order": ":id",
            "$where": where,
        }
        url = f"{SOCRATA_HOST}/resource/{dataset_id}.json?{urllib.parse.urlencode(params)}"
        first_url = first_url or url
        page = fetch_json(url)
        rows.extend(page)
        print(f"  {dataset_id}: +{len(page)} rows (total {len(rows)})", file=sys.stderr)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    (RAW_DIR / f"{stem}.json").write_bytes(payload)
    meta = {
        "dataset_id": dataset_id,
        "url": first_url,
        "where": where,
        "fetched_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "row_count": len(rows),
        "byte_size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    (RAW_DIR / f"{stem}.meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(json.dumps(meta, indent=2))


def load_raw(stem: str) -> list[dict[str, Any]]:
    path = RAW_DIR / f"{stem}.json"
    if not path.exists():
        raise SystemExit(f"{path} is missing; run `explore_addresses.py fetch` first")
    rows: list[dict[str, Any]] = json.loads(path.read_text())
    return rows


# --------------------------------------------------------------------------
# normalization shared by build and query
# --------------------------------------------------------------------------


def fold(raw: str) -> str:
    """User text or source text -> the one canonical spelling both sides use.

    Adds two folds on top of the ETL normalizer, both of which exist only
    because people type differently than DOT writes: digit ordinals ("3RD" ->
    "3", which the ETL never sees) and punctuation ("W. 86th St.").
    """
    text = _PUNCTUATION.sub(" ", str(raw).upper())
    text = _DIGIT_ORDINAL.sub(r"\1", text)
    text = _WHITESPACE.sub(" ", text).strip()
    if not text:
        return ""
    return normalize_street_name(text)


def street_variants(street_norm: str) -> set[str]:
    """Every spelling of one street that should match it.

    Four families: the canonical name; the name without its suffix type
    ("3 AVE" -> "3"); numbered cross streets without the E/W the user did not
    type ("E 86 ST" -> "86 ST", "86"); and the spelled-out ordinal forms, which
    `fold` already collapses, so they cost nothing here.
    """
    variants = {street_norm}
    tokens = street_norm.split(" ")
    suffixes = set(WORD_FORMS.values()) | {"ST", "AVE", "PL", "DR", "BLVD", "RD", "LN", "CT"}
    if len(tokens) > 1 and tokens[-1] in suffixes:
        variants.add(" ".join(tokens[:-1]))
    # "HOUSTON" for "E HOUSTON ST": the directional is the part New Yorkers
    # leave off, on named streets as much as on numbered ones.
    if len(tokens) > 2 and tokens[0] in {"E", "W", "N", "S"}:
        variants.add(" ".join(tokens[1:]))
        variants.add(" ".join(tokens[1:-1]) if tokens[-1] in suffixes else " ".join(tokens[1:]))
    numbered = _NUMBERED_STREET.match(street_norm)
    if numbered:
        directional, number, suffix = numbered.groups()
        variants.add(number)
        if suffix:
            variants.add(f"{number} {suffix}")
        if directional:
            variants.add(f"{directional} {number}")
    return {variant for variant in variants if variant}


def place_tokens(name: str) -> set[str]:
    """Words of a place name, minus the ones too common to narrow anything."""
    stop = {"THE", "OF", "AND", "AT", "A"}
    return {
        CARDINALS.get(token, token)
        for token in fold(name).split(" ")
        if token and token not in stop
    }


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------


def build_index() -> None:
    """Build data/explore/autocomplete.sqlite from the raw JSON and the app DB."""
    EXPLORE_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_DB.unlink(missing_ok=True)
    conn = sqlite3.connect(INDEX_DB)
    conn.executescript(_SCHEMA)

    streets = _build_streets(conn)
    _build_addresses(conn, streets)
    _build_intersections(conn, streets)
    _build_zipcodes(conn)
    _build_places(conn)
    _build_street_variants(conn, streets)

    conn.commit()
    conn.execute("ANALYZE")
    conn.execute("VACUUM")
    conn.close()
    print(f"index: {INDEX_DB} {INDEX_DB.stat().st_size / 1e6:.1f} MB")


def _centerline_streets() -> dict[str, tuple[str, float, float]]:
    """street_norm -> (display name, lon, lat of a mid-ish vertex) from our own DB."""
    if not CURBCHECK_DB.exists():
        raise SystemExit(f"{CURBCHECK_DB} is missing; run `make sync` first")
    conn = sqlite3.connect(f"file:{CURBCHECK_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    midpoints: dict[str, list[tuple[float, float]]] = {}
    display: dict[str, str] = {}
    for row in conn.execute("SELECT street_norm, street_name, geom FROM street_segment"):
        point = _middle_vertex(row["geom"])
        if point is None:
            continue
        display.setdefault(row["street_norm"], row["street_name"])
        midpoints.setdefault(row["street_norm"], []).append(point)
    conn.close()
    streets: dict[str, tuple[str, float, float]] = {}
    for street_norm, points in midpoints.items():
        # The mean of a curving street can land in the river, so the pin is the
        # real segment midpoint nearest the mean, as curbcheck.geocode does.
        mean_lon = sum(lon for lon, _ in points) / len(points)
        mean_lat = sum(lat for _, lat in points) / len(points)
        lon, lat = min(points, key=lambda p: (p[0] - mean_lon) ** 2 + (p[1] - mean_lat) ** 2)
        streets[street_norm] = (display[street_norm], lon, lat)
    return streets


def _middle_vertex(geom_json: str) -> tuple[float, float] | None:
    """A vertex near the middle of a GeoJSON LineString/MultiLineString.

    A vertex rather than an interpolated point: without shapely this is the
    cheapest point guaranteed to lie on the street rather than across a bend.
    """
    try:
        geometry = json.loads(geom_json)
    except (TypeError, ValueError):
        return None
    coords = geometry.get("coordinates")
    if not coords:
        return None
    if geometry.get("type") == "MultiLineString":
        coords = max(coords, key=len)
    if not coords:
        return None
    lon, lat = coords[len(coords) // 2][:2]
    return (float(lon), float(lat))


def _build_streets(conn: sqlite3.Connection) -> dict[str, tuple[str, float, float]]:
    streets = _centerline_streets()
    conn.executemany(
        "INSERT INTO street (street_norm, display, lon, lat) VALUES (?, ?, ?, ?)",
        [(norm, display, lon, lat) for norm, (display, lon, lat) in streets.items()],
    )
    print(f"streets: {len(streets)}")
    return streets


def _build_street_variants(
    conn: sqlite3.Connection, streets: dict[str, tuple[str, float, float]]
) -> None:
    """Variants for every street we know, from either source, plus the nicknames."""
    known = {row[0] for row in conn.execute("SELECT street_norm FROM street")}
    known |= {row[0] for row in conn.execute("SELECT DISTINCT street_norm FROM address")}
    pairs = {
        (variant, street_norm) for street_norm in known for variant in street_variants(street_norm)
    }
    pairs |= {(fold(nick), target) for nick, target in NICKNAMES.items() if target in known}
    conn.executemany("INSERT OR IGNORE INTO street_variant VALUES (?, ?)", sorted(pairs))
    print(f"street variants: {len(pairs)} over {len(known)} streets")
    _ = streets


def _build_addresses(
    conn: sqlite3.Connection, streets: dict[str, tuple[str, float, float]]
) -> None:
    """One row per AddressPoint, keyed by the normalized street the user will type."""
    rows = load_raw("address_points_manhattan")
    records = []
    for row in rows:
        house = _house_int(row.get("house_number"))
        geometry = row.get("the_geom") or {}
        coords = geometry.get("coordinates")
        full_name = row.get("full_street_name") or ""
        if house is None or not coords or not full_name:
            continue
        street_norm = fold(full_name)
        # Prefer the centerline's own spelling so a suggestion reads the same
        # way the results header will.
        display_street = streets.get(street_norm, (_display_street(full_name), 0.0, 0.0))[0]
        suffix = row.get("house_number_suffix") or ""
        records.append(
            (
                street_norm,
                house,
                f"{row['house_number']}{suffix} {display_street}",
                row.get("zipcode"),
                float(coords[0]),
                float(coords[1]),
            )
        )
    conn.executemany("INSERT INTO address VALUES (?, ?, ?, ?, ?, ?)", records)
    print(f"addresses: {len(records)} of {len(rows)} raw rows")


def _display_street(raw: str) -> str:
    return _WHITESPACE.sub(" ", str(raw)).strip()


def _build_intersections(
    conn: sqlite3.Connection, streets: dict[str, tuple[str, float, float]]
) -> None:
    """Every ordered pair of street names meeting at a `street_node`."""
    conn_app = sqlite3.connect(f"file:{CURBCHECK_DB}?mode=ro", uri=True)
    conn_app.row_factory = sqlite3.Row
    records = []
    nodes = 0
    for row in conn_app.execute("SELECT lon, lat, street_names FROM street_node"):
        try:
            names = json.loads(row["street_names"])
        except (TypeError, ValueError):
            continue
        names = [str(name) for name in names if isinstance(name, str)]
        if len(names) < 2:
            continue
        nodes += 1
        for i, first in enumerate(names):
            for second in names[i + 1 :]:
                a_norm, b_norm = fold(first), fold(second)
                if not a_norm or not b_norm or a_norm == b_norm:
                    continue
                label = f"{streets.get(a_norm, (first,))[0]} & {streets.get(b_norm, (second,))[0]}"
                records.append((a_norm, b_norm, label, float(row["lon"]), float(row["lat"])))
                records.append((b_norm, a_norm, label, float(row["lon"]), float(row["lat"])))
    conn_app.close()
    conn.executemany("INSERT INTO intersection VALUES (?, ?, ?, ?, ?)", records)
    print(f"intersections: {len(records) // 2} pairs over {nodes} multi-name nodes")


def _build_zipcodes(conn: sqlite3.Connection) -> None:
    """Mean of each zip's address points. Good enough to centre a map on."""
    conn.execute(
        "INSERT INTO zipcode SELECT zipcode, avg(lon), avg(lat), count(*) FROM address"
        " WHERE zipcode IS NOT NULL AND length(zipcode) = 5 GROUP BY zipcode"
    )
    count = conn.execute("SELECT count(*) FROM zipcode").fetchone()[0]
    print(f"zipcodes: {count}")


def _build_places(conn: sqlite3.Connection) -> None:
    """CommonPlace names, minus the ones that only repeat a street we already index.

    CommonPlace carries a bare "BROADWAY" and a "BLEECKER ST"; keeping them
    would push the street entry, which has a real centerline behind it, below a
    point with no geometry.
    """
    street_names = {row[0] for row in conn.execute("SELECT street_norm FROM street")}
    rows = load_raw("common_places_manhattan")
    records = []
    tokens = []
    dropped = 0
    for index, row in enumerate(rows):
        name = (row.get("feature_name") or "").strip()
        geometry = row.get("the_geom") or {}
        coords = geometry.get("coordinates")
        if not name or not coords:
            continue
        if fold(name) in street_names:
            dropped += 1
            continue
        name_tokens = place_tokens(name)
        records.append((index, name, float(coords[0]), float(coords[1]), len(name_tokens)))
        tokens.extend((token, index) for token in name_tokens)
    conn.executemany("INSERT INTO place VALUES (?, ?, ?, ?, ?)", records)
    conn.executemany("INSERT OR IGNORE INTO place_token VALUES (?, ?)", tokens)
    print(
        f"places: {len(records)} names, {len(tokens)} tokens, {dropped} street duplicates dropped"
    )


def _house_int(value: Any) -> int | None:
    if value is None:
        return None
    match = re.match(r"^\s*(\d+)", str(value))
    return int(match.group(1)) if match else None


# --------------------------------------------------------------------------
# query
# --------------------------------------------------------------------------


def _prefix_bound(prefix: str) -> str:
    """The exclusive upper end of a `LIKE prefix%` range, done as a range scan."""
    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


def resolve_street(conn: sqlite3.Connection, text: str) -> list[tuple[str, bool]]:
    """Street norms `text` could mean, as (street_norm, is_fuzzy), best first.

    Exact variant match first, then prefix, then — only if both came back empty
    — an edit-distance-1 scan over the variant vocabulary. Keeping the fuzzy
    pass last is what holds the common case inside the latency budget: a typo
    is rare, and a correct prefix must never pay for one.
    """
    folded = fold(text)
    if not folded:
        return []
    exact = [
        (row[0], False)
        for row in conn.execute(
            "SELECT street_norm FROM street_variant WHERE variant = ? ORDER BY street_norm",
            (folded,),
        )
    ]
    prefixed = [
        (row[0], False)
        for row in conn.execute(
            "SELECT DISTINCT street_norm FROM street_variant"
            " WHERE variant >= ? AND variant < ? LIMIT 40",
            (folded, _prefix_bound(folded)),
        )
    ]
    seen = {norm for norm, _ in exact}
    ordered = exact + [(norm, False) for norm, _ in prefixed if norm not in seen]
    if ordered:
        return ordered
    return [(norm, True) for norm in _fuzzy_streets(conn, folded)]


def _fuzzy_streets(conn: sqlite3.Connection, folded: str) -> list[str]:
    """Street norms whose variant is within one edit of `folded`.

    A linear scan over ~4k variants, filtered on length first. Measured at
    ~3 ms, and it only runs when the exact and prefix passes found nothing.
    """
    matches: list[str] = []
    for variant, street_norm in conn.execute("SELECT variant, street_norm FROM street_variant"):
        if abs(len(variant) - len(folded)) > 1:
            continue
        if _within_one_edit(variant, folded):
            matches.append(street_norm)
    return sorted(set(matches))[:10]


def _within_one_edit(a: str, b: str) -> bool:
    """True when `a` and `b` differ by at most one insert, delete or substitute."""
    if a == b:
        return True
    if len(a) == len(b):
        diffs = [i for i, (x, y) in enumerate(zip(a, b, strict=True)) if x != y]
        return len(diffs) <= 1
    longer, shorter = (a, b) if len(a) > len(b) else (b, a)
    return any(longer[:i] + longer[i + 1 :] == shorter for i in range(len(longer)))


def suggest(conn: sqlite3.Connection, text: str, *, limit: int = DEFAULT_LIMIT) -> list[Suggestion]:
    """Ranked suggestions for a partly-typed query. Never raises on hostile input."""
    cleaned = _WHITESPACE.sub(" ", str(text)).strip().upper()
    if not cleaned or len(cleaned) > MAX_QUERY_CHARS:
        return []

    out: list[Suggestion] = []
    if _ZIP.match(cleaned):
        out.extend(_zip_suggestions(conn, cleaned))

    parts = [part.strip() for part in _INTERSECTION_SPLIT.split(cleaned) if part.strip()]
    if len(parts) >= 2:
        crossings = _intersection_suggestions(conn, parts[0], parts[1], limit)
        out.extend(crossings)
        if not crossings:
            # Two streets that never share a centerline node. Offering each one
            # separately is honest; claiming a corner that is not in the data
            # would not be.
            out.extend(_street_suggestions(conn, parts[0], limit, base=CONFIDENCE_STREET))
            out.extend(_street_suggestions(conn, parts[1], limit, base=CONFIDENCE_STREET))
    else:
        house_match = _HOUSE_NUMBER.match(cleaned)
        if house_match:
            out.extend(
                _address_suggestions(conn, int(house_match.group(1)), house_match.group(2), limit)
            )
        # "1519 3rd ave" is not a street name and not a place name. Running
        # those two passes anyway is what pushed the median query over 60 ms,
        # because the street pass falls through to its fuzzy scan every time.
        if not out:
            out.extend(_street_suggestions(conn, cleaned, limit))
            out.extend(_place_suggestions(conn, cleaned, limit))

    return _rank(out, limit)


def _rank(suggestions: list[Suggestion], limit: int) -> list[Suggestion]:
    """Best first, one row per (kind, label), capped at `limit`."""
    best: dict[tuple[str, str], Suggestion] = {}
    for suggestion in suggestions:
        key = (suggestion.kind, suggestion.label)
        if key not in best or suggestion.confidence > best[key].confidence:
            best[key] = suggestion
    ordered = sorted(best.values(), key=lambda s: (-s.confidence, len(s.label), s.label))
    return ordered[:limit]


def _zip_suggestions(conn: sqlite3.Connection, zipcode: str) -> list[Suggestion]:
    row = conn.execute(
        "SELECT lon, lat, points FROM zipcode WHERE zipcode = ?", (zipcode,)
    ).fetchone()
    if row is None:
        return []
    return [
        Suggestion(
            "zip", f"{zipcode} (ZIP centre, {row[2]} addresses)", row[1], row[0], CONFIDENCE_ZIP
        )
    ]


def _address_suggestions(
    conn: sqlite3.Connection, house: int, street_text: str, limit: int
) -> list[Suggestion]:
    out: list[Suggestion] = []
    for street_norm, fuzzy in resolve_street(conn, street_text)[:6]:
        penalty = FUZZY_CONFIDENCE_PENALTY if fuzzy else 1.0
        exact = conn.execute(
            "SELECT display, lon, lat FROM address WHERE street_norm = ? AND house = ?"
            " ORDER BY display LIMIT ?",
            (street_norm, house, limit),
        ).fetchall()
        for display, lon, lat in exact:
            out.append(Suggestion("address", display, lat, lon, CONFIDENCE_ADDRESS_POINT * penalty))
        if exact:
            continue
        street_display = _street_display(conn, street_norm)
        between = _interpolate_between(conn, street_norm, house)
        if between is not None:
            lon, lat = between
            out.append(
                Suggestion(
                    "address",
                    f"{house} {street_display}",
                    lat,
                    lon,
                    CONFIDENCE_ADDRESS_INTERPOLATED * penalty,
                )
            )
            continue
        near = _nearest_house(conn, street_norm, house)
        if near is not None:
            display, lon, lat = near
            out.append(
                Suggestion(
                    "address", f"near {display}", lat, lon, CONFIDENCE_NEAR_ADDRESS * penalty
                )
            )
    return out


def _street_display(conn: sqlite3.Connection, street_norm: str) -> str:
    row = conn.execute(
        "SELECT display FROM street WHERE street_norm = ?", (street_norm,)
    ).fetchone()
    return str(row[0]) if row else street_norm


def _interpolate_between(
    conn: sqlite3.Connection, street_norm: str, house: int
) -> tuple[float, float] | None:
    """Position `house` between its two nearest same-parity neighbours on the street.

    NYC puts odd numbers on one side and even on the other, so interpolating
    between two surveyed points of the same parity stays on the correct side of
    the street. Only used when the number is missing from AddressPoint but is
    bracketed by numbers that are present, which is the common gap: 1519 3 AVE
    is absent while 1517 and 1529 are there.
    """
    parity = house % 2
    below = conn.execute(
        "SELECT house, lon, lat FROM address WHERE street_norm = ? AND house < ?"
        " AND house % 2 = ? ORDER BY house DESC LIMIT 1",
        (street_norm, house, parity),
    ).fetchone()
    above = conn.execute(
        "SELECT house, lon, lat FROM address WHERE street_norm = ? AND house > ?"
        " AND house % 2 = ? ORDER BY house ASC LIMIT 1",
        (street_norm, house, parity),
    ).fetchone()
    if below is None or above is None:
        return None
    # A bracket wider than one hundred-block is two different blocks with a gap
    # between them, not a run of missing doors; interpolating across it would
    # invent a location.
    if above[0] - below[0] > 100:
        return None
    span = above[0] - below[0]
    position = (house - below[0]) / span if span else 0.5
    return (
        below[1] + (above[1] - below[1]) * position,
        below[2] + (above[2] - below[2]) * position,
    )


def _nearest_house(
    conn: sqlite3.Connection, street_norm: str, house: int
) -> tuple[str, float, float] | None:
    """The closest house number on that street, either side. Two index seeks.

    This is the fallback the centerline geocoder reaches by interpolating into
    a hundred-block corner; against real address points it is both simpler and
    closer, because the neighbouring number is a surveyed location.
    """
    rows = []
    for order, comparison in (("DESC", "<="), ("ASC", ">=")):
        row = conn.execute(
            f"SELECT display, lon, lat, house FROM address WHERE street_norm = ?"  # noqa: S608
            f" AND house {comparison} ? ORDER BY house {order} LIMIT 1",
            (street_norm, house),
        ).fetchone()
        if row is not None:
            rows.append(row)
    if not rows:
        return None
    display, lon, lat, _ = min(rows, key=lambda row: abs(row[3] - house))
    return (display, lon, lat)


def _intersection_suggestions(
    conn: sqlite3.Connection, first_text: str, second_text: str, limit: int
) -> list[Suggestion]:
    out: list[Suggestion] = []
    firsts = resolve_street(conn, first_text)[:8]
    seconds = resolve_street(conn, second_text)[:8]
    for a_norm, a_fuzzy in firsts:
        for b_norm, b_fuzzy in seconds:
            penalty = FUZZY_CONFIDENCE_PENALTY if (a_fuzzy or b_fuzzy) else 1.0
            for display, lon, lat in conn.execute(
                "SELECT display, lon, lat FROM intersection WHERE a_norm = ? AND b_norm = ?"
                " LIMIT ?",
                (a_norm, b_norm, limit),
            ):
                out.append(
                    Suggestion("intersection", display, lat, lon, CONFIDENCE_INTERSECTION * penalty)
                )
    return out


def _street_suggestions(
    conn: sqlite3.Connection, text: str, limit: int, *, base: float = CONFIDENCE_STREET_WHOLE_QUERY
) -> list[Suggestion]:
    """The street itself. `base` drops when the street is only half of what was typed."""
    out: list[Suggestion] = []
    for street_norm, fuzzy in resolve_street(conn, text)[:limit]:
        row = conn.execute(
            "SELECT display, lon, lat FROM street WHERE street_norm = ?", (street_norm,)
        ).fetchone()
        if row is None:
            continue
        penalty = FUZZY_CONFIDENCE_PENALTY if fuzzy else 1.0
        out.append(Suggestion("street", row[0], row[2], row[1], base * penalty))
    return out


def _place_suggestions(conn: sqlite3.Connection, text: str, limit: int) -> list[Suggestion]:
    """Places every one of whose typed tokens prefix-matches a word of the name.

    Scored by how much of the place's own name the query accounted for, so
    "bryant park" (2 of 2 words) outranks the same two words buried inside
    "CTL PK W DR OV 86 ST TRNVS RD".
    """
    tokens = [CARDINALS.get(token, token) for token in fold(text).split(" ") if token]
    if not tokens:
        return []
    matched: collections.Counter[int] = collections.Counter()
    for position, token in enumerate(tokens):
        # Only the last token is a prefix: it is the one still being typed.
        # Treating every token as a prefix made "3" alone pull thousands of
        # place ids out of the token table on each keystroke.
        if position == len(tokens) - 1:
            rows = conn.execute(
                "SELECT place_id FROM place_token WHERE token >= ? AND token < ?",
                (token, _prefix_bound(token)),
            )
        else:
            rows = conn.execute("SELECT place_id FROM place_token WHERE token = ?", (token,))
        matched.update({row[0] for row in rows})
    keep = [place_id for place_id, hits in matched.items() if hits == len(tokens)]
    if not keep:
        return []
    placeholders = ",".join("?" * min(len(keep), 60))
    rows = conn.execute(
        f"SELECT display, lon, lat, tokens FROM place WHERE place_id IN ({placeholders})",  # noqa: S608
        tuple(keep[:60]),
    ).fetchall()
    scored = []
    for display, lon, lat, name_tokens in rows:
        coverage = min(len(tokens) / max(name_tokens, 1), 1.0)
        if coverage < MIN_PLACE_COVERAGE:
            continue
        scored.append(Suggestion("place", display, lat, lon, CONFIDENCE_PLACE * coverage))
    scored.sort(key=lambda suggestion: -suggestion.confidence)
    return scored[:limit]


def reverse(conn: sqlite3.Connection, lon: float, lat: float) -> Suggestion | None:
    """Nearest address point to a dropped pin, as a "near <address>" label.

    Prefilters on a bounding box so the scan touches a few hundred rows rather
    than 63k. The box is ~0.003° (about 1,100 ft), widened once if it is empty,
    which only happens over the rivers and the parks.
    """
    for span in (0.003, 0.012, 0.05):
        rows = conn.execute(
            "SELECT display, lon, lat FROM address WHERE lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?",
            (lon - span, lon + span, lat - span, lat + span),
        ).fetchall()
        if rows:
            display, best_lon, best_lat = min(
                rows, key=lambda row: _distance_ft(lon, lat, row[1], row[2])
            )
            feet = _distance_ft(lon, lat, best_lon, best_lat)
            confidence = CONFIDENCE_ADDRESS_POINT if feet <= 150 else CONFIDENCE_NEAR_ADDRESS
            return Suggestion("address", f"near {display}", best_lat, best_lon, confidence)
    return None


def _distance_ft(lon_a: float, lat_a: float, lon_b: float, lat_b: float) -> float:
    import math

    dy = (lat_a - lat_b) * FT_PER_DEG_LAT
    dx = (lon_a - lon_b) * FT_PER_DEG_LAT * math.cos(math.radians(lat_a))
    return math.hypot(dx, dy)


# --------------------------------------------------------------------------
# reporting commands
# --------------------------------------------------------------------------

DEMO_QUERIES = [
    "1519 3rd ave",
    "1519 third avenue",
    "1519 3rd av",
    "e 86th st and 3rd",
    "86 & 3",
    "lex & 86",
    "86th st",
    "10021",
    "w 4 st and bleeker",
    "350 5th",
    "one world trade",
    "bryant park",
    "broadwa",
    "fdr dr & 96",
    "1 police plaza",
]

BENCH_QUERIES = [
    *DEMO_QUERIES,
    "123 w 45",
    "park ave @ 57",
    "columbus circle",
    "amsterdam ave",
    "2 ave & houston",
]


def open_index() -> sqlite3.Connection:
    if not INDEX_DB.exists():
        raise SystemExit(f"{INDEX_DB} is missing; run `explore_addresses.py build` first")
    return sqlite3.connect(f"file:{INDEX_DB}?mode=ro", uri=True)


def run_demo() -> None:
    conn = open_index()
    for query in DEMO_QUERIES:
        print(f"\n{query!r}")
        results = suggest(conn, query)
        if not results:
            print("    (no match -> drop a pin)")
        for suggestion in results[:3]:
            data = suggestion.as_dict()
            print(
                f"    {data['kind']:<12} {data['confidence']:.2f}  {data['label']}"
                f"  ({data['lat']}, {data['lon']})"
            )
    conn.close()


def run_bench() -> None:
    """Latency on local disk and on the data mount, because they differ 200-fold.

    `data/` is a 9p bind mount in this dev container, where one SQLite page read
    costs ~2.5 ms instead of ~12 us. That is an artifact of the container, not
    of the index, so the headline number is measured from a copy on local disk
    and the mounted number is printed beside it.
    """
    print(f"index size: {INDEX_DB.stat().st_size / 1e6:.1f} MB")
    with tempfile.TemporaryDirectory() as tmp:
        local_copy = Path(tmp) / "autocomplete.sqlite"
        shutil.copy2(INDEX_DB, local_copy)
        local = _bench_against(local_copy, verbose=True, label="local disk")
        mounted = _bench_against(INDEX_DB, verbose=False, label="data/ mount (9p)")
    print(
        f"\n  local disk       median {statistics.median(local):6.2f} ms"
        f"  mean {statistics.mean(local):6.2f} ms  max {max(local):6.2f} ms"
    )
    print(
        f"  data/ mount (9p) median {statistics.median(mounted):6.2f} ms"
        f"  mean {statistics.mean(mounted):6.2f} ms  max {max(mounted):6.2f} ms"
    )


def _bench_against(path: Path, *, verbose: bool, label: str) -> list[float]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    timings = []
    if verbose:
        print(f"\n{label}:")
    for query in BENCH_QUERIES:
        samples = []
        for _ in range(50):
            start = time.perf_counter()
            results = suggest(conn, query)
            samples.append((time.perf_counter() - start) * 1000)
        median = statistics.median(samples)
        timings.append(median)
        if verbose:
            print(f"  {median:7.3f} ms  {len(results)} hits  {query!r}")
    start = time.perf_counter()
    near = reverse(conn, -73.9560, 40.7790)
    if verbose:
        print(
            f"  {(time.perf_counter() - start) * 1000:7.3f} ms  reverse(-73.9560, 40.7790)"
            f" -> {near.label if near else None}"
        )
    conn.close()
    return timings


def run_accuracy(sample_size: int = 800) -> None:
    """How far the shipped centerline geocoder lands from the surveyed point.

    Every AddressPoint row is a ground-truth location for an address string, so
    feeding the string to `curbcheck.geocode.geocode` and measuring the offset
    is a direct read on what the interpolation ladder costs. Imports the package
    geocoder (and therefore shapely), unlike the rest of this script.
    """
    import random

    from curbcheck.geocode import GeocodeKind, geocode

    rows = load_raw("address_points_manhattan")
    random.seed(7)
    sample = random.sample(rows, min(sample_size, len(rows)))
    app = sqlite3.connect(f"file:{CURBCHECK_DB}?mode=ro", uri=True)
    app.row_factory = sqlite3.Row
    index = open_index()

    errors: list[float] = []
    prototype_errors: list[float] = []
    kinds: collections.Counter[str] = collections.Counter()
    for row in sample:
        query = f"{row['house_number']} {row['full_street_name']}"
        lon, lat = row["the_geom"]["coordinates"]
        candidates = geocode(app, query, limit=1)
        kinds[str(candidates[0].kind) if candidates else "none"] += 1
        if candidates and candidates[0].kind == GeocodeKind.ADDRESS:
            errors.append(_distance_ft(lon, lat, candidates[0].lon, candidates[0].lat))
        mine = suggest(index, query, limit=1)
        if mine:
            prototype_errors.append(_distance_ft(lon, lat, mine[0].lon, mine[0].lat))
    app.close()
    index.close()

    print(f"sample {len(sample)} AddressPoint rows; result kinds {dict(kinds)}")
    _report_errors("curbcheck.geocode (centerline interpolation)", errors)
    _report_errors("prototype (address points)", prototype_errors)


def _report_errors(label: str, errors: list[float]) -> None:
    if not errors:
        print(f"{label}: no results")
        return
    errors.sort()

    def at(fraction: float) -> float:
        return errors[min(int(len(errors) * fraction), len(errors) - 1)]

    within = sum(1 for error in errors if error <= 100) / len(errors) * 100
    print(
        f"{label}: n={len(errors)} median {at(0.5):.0f} ft  p90 {at(0.9):.0f} ft"
        f"  p95 {at(0.95):.0f} ft  max {errors[-1]:.0f} ft  within 100 ft {within:.1f}%"
    )


def run_profile() -> None:
    """Dataset facts quoted in docs/ux/AUTOCOMPLETE_RESEARCH.md."""
    rows = load_raw("address_points_manhattan")
    print(f"AddressPoint Manhattan rows: {len(rows)}")
    fields: collections.Counter[str] = collections.Counter()
    for row in rows:
        fields.update(row.keys())
    for field, count in fields.most_common():
        print(f"  {field:<28} {count:>6} ({count / len(rows) * 100:5.1f}%)")

    names = collections.Counter(fold(row["full_street_name"]) for row in rows)
    print(f"\ndistinct normalized street names: {len(names)}")
    centerline = sqlite3.connect(f"file:{CURBCHECK_DB}?mode=ro", uri=True)
    known = {
        row[0] for row in centerline.execute("SELECT DISTINCT street_norm FROM street_segment")
    }
    centerline.close()
    unmatched = sorted(set(names) - known, key=lambda name: -names[name])
    print(f"  matching a centerline street_norm: {len(set(names) & known)}")
    print(f"  unmatched names: {len(unmatched)} covering {sum(names[n] for n in unmatched)} rows")
    for name in unmatched[:15]:
        print(f"    {name} ({names[name]})")

    places = load_raw("common_places_manhattan")
    named = [row for row in places if (row.get("feature_name") or "").strip()]
    print(f"\nCommonPlace Manhattan rows: {len(places)}, with a feature_name: {len(named)}")
    for row in named[:8]:
        print(f"    {row['feature_name']}")


def main(argv: list[str]) -> int:
    command = argv[0] if argv else "demo"
    if command == "fetch":
        for dataset_id, (_, where) in DATASETS.items():
            fetch_dataset(dataset_id, where)
    elif command == "profile":
        run_profile()
    elif command == "build":
        build_index()
    elif command == "demo":
        run_demo()
    elif command == "bench":
        run_bench()
    elif command == "accuracy":
        run_accuracy()
    elif command == "query":
        conn = open_index()
        print(json.dumps([s.as_dict() for s in suggest(conn, " ".join(argv[1:]))], indent=2))
        conn.close()
    else:
        raise SystemExit(f"unknown command {command!r}; see the module docstring")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
