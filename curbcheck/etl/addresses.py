"""OTI AddressPoint and CommonPlace folded into the vocabulary index the geocoder reads.

The hard part of a typed Manhattan query is the street name. The house number
is an integer and the grammar ("<n> <street>", "<street> & <street>", a ZIP, a
place name) is tiny, so this builds a *vocabulary* index rather than a text
index over whole address strings: every street is expanded at build time into
the handful of spellings a person might type, and the query path parses first
and looks up second. One prefix range-scan over `street_variant` resolves the
street and an index seek does the rest, which is what holds a keystroke under
a millisecond (docs/ux/AUTOCOMPLETE_RESEARCH.md §2).

A name is also indexed word by word, because a person types the word they
remember and not the start of the legal name: `street_token` and `place_token`
hold one row per (word, position, spelling), and a spelling is any way the name
can be written — the canonical one, the variants, the aliases and nicknames
(docs/ux/AUTOCOMPLETE_RESEARCH.md §6, docs/DECISIONS.md D31).

`fold` is the one normalizer both sides share: the ETL folds the source text
with it here and `curbcheck.geocode` folds the user's text with it there, so
the two can only ever meet on the same key.

Display strings are stored exactly as the city writes them, runs of spaces and
all (`E  86 ST`); `engine.labels.single_spaced` collapses them at the moment
they reach the user, which keeps the stored name byte-identical to the source.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from typing import Any

from pydantic import BaseModel, ConfigDict

from curbcheck.db import json_string_list
from curbcheck.etl.stage import neutralize_formula
from curbcheck.etl.streets import NAME_ALIASES, WORD_FORMS, normalize_street_name

LOGGER = logging.getLogger(__name__)

# What New Yorkers say instead of the official name, beyond the aliases the two
# city datasets already disagree over (`streets.NAME_ALIASES`, which `fold`
# applies). Values are in `normalize_street_name` form and are looked up in the
# centerline, not re-folded: a nickname whose target has no segment is dropped.
NICKNAMES: dict[str, str] = {
    "LEX": "LEXINGTON AVE",
    "MAD": "MADISON AVE",
    "BWAY": "BROADWAY",
    "B WAY": "BROADWAY",
    "FDR": "FRANKLIN D ROOSEVELT DR",
    "WEST SIDE HWY": "W ST",
    "MLK BLVD": "W 125 ST",
    "CPW": "CENTRAL PARK W",
    "ACP BLVD": "ADAM CLAYTON POWELL JR BLVD",
    "AMSTERDAM": "AMSTERDAM AVE",
    "COLUMBUS": "COLUMBUS AVE",
    "PARK": "PARK AVE",
    "PAS": "PARK AVE S",
    "RSD": "RIVERSIDE DR",
    # `streets.NAME_ALIASES` already carries the city's own shorthand for this
    # honorific, `DR M L KING JR BLVD`; nobody types the initials.
    "MARTIN LUTHER KING JR BLVD": "W 125 ST",
}

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

# Tokens that end a street name rather than name it, so a variant with the last
# one dropped still points at the same street ("3 AVE" -> "3"). `WORD_FORMS`'s
# own values cover the spelled-out suffixes and the directionals; the literals
# are the abbreviations the source writes directly.
STREET_SUFFIXES = frozenset(WORD_FORMS.values()) | frozenset(
    {"ST", "AVE", "PL", "DR", "BLVD", "RD", "LN", "CT"}
)

# The part of a name New Yorkers leave off, on named streets ("HOUSTON") as
# much as on numbered ones ("86 ST").
DIRECTIONALS = frozenset({"E", "W", "N", "S"})

# Words too common in a name to narrow anything down. Dropped from the index
# and from the query alike, so they can neither be required nor get in the way:
# "high school of fashion" and "fashion high school" ask the same question.
NAME_STOPWORDS = frozenset({"THE", "OF", "AND", "AT", "A"})

# What a New Yorker types for a landmark CommonPlace files under its legal
# name. Every target was read out of the 2026-09-15 `feature_name` snapshot and
# an alias whose target is not in the data is dropped rather than guessed at
# (`tests/test_geocode_real.py` checks each one against the live index). The
# list is deliberately tiny: these are the names whose colloquial form shares
# no word with the legal one, or where the legal form belongs to a different
# building on the same block (MoMA's garden, the Port Authority's post office).
PLACE_ALIASES: dict[str, str] = {
    "MOMA": "MUSEUM OF MODERN ART (MOMA)",
    "THE MET": "METROPOLITAN MUSEUM OF ART",
    "MET MUSEUM": "METROPOLITAN MUSEUM OF ART",
    "PORT AUTHORITY": "PORT AUTHORITY BUS TERMINAL",
    "GRAND CENTRAL": "GRAND CENTRAL TERMINAL",
    "MSG": "MADISON SQUARE GARDEN",
}

# CommonPlace writes a school both ways — 121 names begin "PS", 91 spell out
# "HIGH SCHOOL" — so each name is also indexed under the other spelling and it
# does not matter which one the user learned.
PLACE_SHORTHAND: dict[tuple[str, ...], str] = {
    ("PUBLIC", "SCHOOL"): "PS",
    ("HIGH", "SCHOOL"): "HS",
    ("MIDDLE", "SCHOOL"): "MS",
}

_PUNCTUATION = re.compile(r"[^A-Z0-9 ]")
_WHITESPACE = re.compile(r"\s+")
_LEADING_DIGITS = re.compile(r"^(\d{1,6})")
# "85TH" -> "85". The ETL normalizer folds the spelled-out ordinals (FIFTH -> 5)
# but not the digit ones, because the source datasets never write those; people
# typing into the address box do.
_DIGIT_ORDINAL = re.compile(r"\b(\d+)(?:ST|ND|RD|TH)\b")
_NUMBERED_STREET = re.compile(r"^(?:([EW]) )?(\d{1,3})(?: (ST|AVE|PL|DR|WALK))?$")

_INSERT_STREET = "INSERT OR REPLACE INTO street (street_norm, display, lon, lat) VALUES (?,?,?,?)"
_INSERT_VARIANT = "INSERT OR IGNORE INTO street_variant (variant, street_norm) VALUES (?, ?)"
_INSERT_ADDRESS = (
    "INSERT INTO address_point (street_norm, house_number, display, zipcode, lon, lat)"
    " VALUES (?, ?, ?, ?, ?, ?)"
)
_INSERT_INTERSECTION = (
    "INSERT INTO intersection (a_norm, b_norm, display, lon, lat) VALUES (?, ?, ?, ?, ?)"
)
_INSERT_ZIP = (
    "INSERT INTO zip_centroid (zipcode, lon, lat, address_points)"
    " SELECT zipcode, avg(lon), avg(lat), count(*) FROM address_point"
    " WHERE zipcode IS NOT NULL GROUP BY zipcode"
)
_INSERT_PLACE = "INSERT INTO place (place_id, display, lon, lat) VALUES (?, ?, ?, ?)"
# S105 reads "token" in these table names as a credential; it is a word of a name.
_INSERT_PLACE_TOKEN = (
    "INSERT OR IGNORE INTO place_token (token, position, place_id, search_name)"  # noqa: S105
    " VALUES (?, ?, ?, ?)"
)
_INSERT_STREET_TOKEN = (
    "INSERT OR IGNORE INTO street_token (token, position, street_norm, search_name)"  # noqa: S105
    " VALUES (?, ?, ?, ?)"
)


class RawAddressPointRow(BaseModel):
    """AddressPoint row exactly as Socrata sends it (`uf93-f8nk`).

    Every field is optional because Socrata omits nulls from resource JSON.
    `house_number` stays text here — it carries hyphens ("159-48") and the
    integer is derived — and `the_geom` is a GeoJSON Point in WGS-84, so no
    reprojection is needed. Unknown columns are kept so a new one is never
    silently dropped.
    """

    model_config = ConfigDict(extra="allow")

    house_number: str | None = None
    house_number_suffix: str | None = None
    full_street_name: str | None = None
    zipcode: str | None = None
    boroughcode: str | None = None
    the_geom: dict[str, Any] | None = None


class RawCommonPlaceRow(BaseModel):
    """CommonPlace row as Socrata sends it (`t95h-5fsr`). `feature_name` is the label."""

    model_config = ConfigDict(extra="allow")

    feature_name: str | None = None
    boroughcode: str | None = None
    the_geom: dict[str, Any] | None = None


@dataclass(frozen=True)
class StagedAddressPoint:
    """One surveyed door: the key it is looked up by, and where it is."""

    street_norm: str
    house_number: int
    display: str
    zipcode: str | None
    lon: float
    lat: float


@dataclass(frozen=True)
class StagedPlace:
    """One named place, with every word sequence it can be searched by."""

    display: str
    lon: float
    lat: float
    spellings: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class AddressReport:
    """What the index holds, so a snapshot that lost half its doors is visible."""

    source_address_rows: int
    address_points: int
    dropped_address_rows: int
    streets: int
    street_variants: int
    intersections: int
    zipcodes: int
    source_place_rows: int
    places: int
    place_tokens: int
    street_tokens: int
    places_dropped_as_streets: int


def fold(raw: str) -> str:
    """Source text or user text -> the one canonical spelling both sides look up by.

    Two folds on top of `normalize_street_name`, both there only because people
    type differently than the city writes: digit ordinals ("3RD" -> "3", which
    the ETL never sees) and punctuation ("W. 86th St.").
    """
    text = _PUNCTUATION.sub(" ", str(raw).upper())
    text = _DIGIT_ORDINAL.sub(r"\1", text)
    text = _WHITESPACE.sub(" ", text).strip()
    if not text:
        return ""
    return normalize_street_name(text)


def street_variants(street_norm: str) -> set[str]:
    """Every spelling of one street that should resolve to it.

    Three families beyond the canonical name: the name with its suffix type
    dropped ("3 AVE" -> "3"); the name without the leading directional the user
    did not type ("E HOUSTON ST" -> "HOUSTON ST", "HOUSTON"); and, for a
    numbered cross street, the bare number and the number with either half of
    its qualifiers ("E 86 ST" -> "86", "86 ST", "E 86"). The spelled-out
    ordinals cost nothing here because `fold` already collapses them.
    """
    variants = {street_norm}
    tokens = street_norm.split(" ")
    if len(tokens) > 1 and tokens[-1] in STREET_SUFFIXES:
        variants.add(" ".join(tokens[:-1]))
    if len(tokens) > 2 and tokens[0] in DIRECTIONALS:
        variants.add(" ".join(tokens[1:]))
        if tokens[-1] in STREET_SUFFIXES:
            variants.add(" ".join(tokens[1:-1]))
    numbered = _NUMBERED_STREET.match(street_norm)
    if numbered:
        directional, number, suffix = numbered.groups()
        variants.add(number)
        if suffix:
            variants.add(f"{number} {suffix}")
        if directional:
            variants.add(f"{directional} {number}")
    return {variant for variant in variants if variant}


def name_words(name: str) -> list[str]:
    """Source text or user text -> the words it is searched by, in the order written.

    The one word list both sides share, so a typed word and an indexed word can
    only ever meet in the same form. Order is kept because it is what separates
    a name the query starts ("bryant park") from one it merely appears in
    ("five bryant park").
    """
    return spelling_words(fold(name))


def spelling_words(folded: str) -> list[str]:
    """The same, for text `fold` has already been over.

    Street variants and `streets.NAME_ALIASES` keys are stored in folded form
    already, and folding them again would apply the alias map a second time and
    turn the spelling back into the name it is an alternative to.
    """
    return [
        CARDINALS.get(word, word)
        for word in folded.split(" ")
        if word and word not in NAME_STOPWORDS
    ]


def search_name(words: Sequence[str]) -> str:
    """The stored form of one spelling: its words, in order, single-spaced."""
    return " ".join(words)


def place_spellings(name: str) -> tuple[tuple[str, ...], ...]:
    """Every word sequence a place answers to: its own name, its shorthand, its aliases."""
    words = tuple(name_words(name))
    spellings = {words} | _shorthand_spellings(words) | set(_aliases_by_name().get(words, ()))
    return tuple(sorted(spelling for spelling in spellings if spelling))


@cache
def _aliases_by_name() -> dict[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    """`PLACE_ALIASES` inverted onto the word form its target is indexed under."""
    inverted: dict[tuple[str, ...], list[tuple[str, ...]]] = {}
    for alias, target in PLACE_ALIASES.items():
        inverted.setdefault(tuple(name_words(target)), []).append(tuple(name_words(alias)))
    return {target: tuple(aliases) for target, aliases in inverted.items()}


def _shorthand_spellings(words: tuple[str, ...]) -> set[tuple[str, ...]]:
    """The name again with "HIGH SCHOOL" written "HS", for every phrase it contains."""
    found = set()
    for phrase, short in PLACE_SHORTHAND.items():
        for start in range(len(words) - len(phrase) + 1):
            if words[start : start + len(phrase)] == phrase:
                found.add((*words[:start], short, *words[start + len(phrase) :]))
    return found


def stage_address_points(
    raw_rows: Sequence[dict[str, Any]], street_displays: dict[str, str]
) -> tuple[list[StagedAddressPoint], int]:
    """Validate and fold AddressPoint rows, returning the kept rows and the dropped count.

    A row with no geometry, no street name or no leading digits in its house
    number carries no address we could offer, so it is dropped rather than
    stored with a hole in it. `street_displays` supplies the centerline's own
    spelling where it has one, so a suggestion reads the same way the results
    header will.
    """
    staged: list[StagedAddressPoint] = []
    dropped = 0
    for raw_row in raw_rows:
        row = RawAddressPointRow.model_validate(raw_row)
        point = _point(row.the_geom)
        house_number = _house_int(row.house_number)
        street_norm = fold(row.full_street_name or "")
        if point is None or house_number is None or not street_norm:
            dropped += 1
            continue
        house_text = (
            str(row.house_number or "").strip() + str(row.house_number_suffix or "").strip()
        )
        display_street = street_displays.get(street_norm) or str(row.full_street_name or "").strip()
        staged.append(
            StagedAddressPoint(
                street_norm=street_norm,
                house_number=house_number,
                display=neutralize_formula(f"{house_text} {display_street}"),
                zipcode=_zipcode(row.zipcode),
                lon=point[0],
                lat=point[1],
            )
        )
    return staged, dropped


def stage_places(
    raw_rows: Sequence[dict[str, Any]], street_spellings: frozenset[str]
) -> tuple[list[StagedPlace], int]:
    """Validate CommonPlace rows, dropping the ones that only repeat a street we index.

    CommonPlace carries a bare "BROADWAY", a "BLEECKER ST" and a "MADISON".
    Keeping them would push the street entry, which has a real centerline
    behind it, below a point with no geometry — and it is what lets a place
    outrank the street a one-word query names, which the confidence ladder
    otherwise guarantees it cannot (`geocode.candidates`). The comparison is
    against every *spelling* of every street, not just the canonical name,
    because "MADISON" is how a user reaches MADISON AVE.
    """
    staged: list[StagedPlace] = []
    dropped = 0
    for raw_row in raw_rows:
        row = RawCommonPlaceRow.model_validate(raw_row)
        point = _point(row.the_geom)
        name = str(row.feature_name or "").strip()
        if point is None or not name:
            continue
        spellings = tuple(
            words for words in place_spellings(name) if search_name(words) not in street_spellings
        )
        if not spellings or search_name(name_words(name)) in street_spellings:
            dropped += 1
            continue
        staged.append(
            StagedPlace(
                display=neutralize_formula(name),
                lon=point[0],
                lat=point[1],
                spellings=spellings,
            )
        )
    return staged, dropped


def build_address_index(
    conn: sqlite3.Connection,
    *,
    address_rows: Sequence[dict[str, Any]],
    place_rows: Sequence[dict[str, Any]],
) -> AddressReport:
    """Write every suggester table from the two snapshots and the centerline already in `conn`.

    Must run after the geometry step: streets, corners and the display spelling
    of every address all come from `street_segment` and `street_node`.
    """
    street_points = _street_points(conn)
    displays = {norm: display for norm, (display, _, _) in street_points.items()}
    _write_streets(conn, street_points)

    addresses, dropped_addresses = stage_address_points(address_rows, displays)
    _write_address_points(conn, addresses)
    conn.execute(_INSERT_ZIP)

    intersections = _write_intersections(conn, displays)
    # The variants come before the places because a place whose name is one of
    # them is dropped, and before the street tokens because they are what the
    # word index is built from.
    variants = _street_variant_pairs(conn, frozenset(displays))
    conn.executemany(_INSERT_VARIANT, sorted(variants))
    street_tokens_written = _write_street_tokens(conn, variants, frozenset(displays))
    places, dropped_places = stage_places(
        place_rows, _spelling_texts(variants, frozenset(displays))
    )
    place_tokens_written = _write_places(conn, places)

    report = AddressReport(
        source_address_rows=len(address_rows),
        address_points=len(addresses),
        dropped_address_rows=dropped_addresses,
        streets=len(street_points),
        street_variants=len(variants),
        intersections=intersections,
        zipcodes=int(conn.execute("SELECT count(*) FROM zip_centroid").fetchone()[0]),
        source_place_rows=len(place_rows),
        places=len(places),
        place_tokens=place_tokens_written,
        street_tokens=street_tokens_written,
        places_dropped_as_streets=dropped_places,
    )
    LOGGER.info(
        "addresses.index points=%d dropped=%d streets=%d variants=%d corners=%d places=%d"
        " zips=%d place_tokens=%d street_tokens=%d",
        report.address_points,
        report.dropped_address_rows,
        report.streets,
        report.street_variants,
        report.intersections,
        report.places,
        report.zipcodes,
        report.place_tokens,
        report.street_tokens,
    )
    return report


def _street_points(conn: sqlite3.Connection) -> dict[str, tuple[str, float, float]]:
    """street_norm -> (display name, lon, lat) for every street in the centerline.

    The pin is the middle vertex of the segment nearest the mean of all of
    them: the mean of a curving street can land in the river, so it has to snap
    back onto a real segment, the same way `geocode` picks a street's midpoint.
    """
    midpoints: dict[str, list[tuple[float, float]]] = {}
    displays: dict[str, str] = {}
    for row in conn.execute("SELECT street_norm, street_name, geom FROM street_segment"):
        point = _middle_vertex(str(row["geom"]))
        if point is None:
            continue
        street_norm = str(row["street_norm"])
        displays.setdefault(street_norm, str(row["street_name"]))
        midpoints.setdefault(street_norm, []).append(point)

    streets: dict[str, tuple[str, float, float]] = {}
    for street_norm, points in midpoints.items():
        mean_lon = sum(lon for lon, _ in points) / len(points)
        mean_lat = sum(lat for _, lat in points) / len(points)
        lon, lat = min(points, key=lambda p: (p[0] - mean_lon) ** 2 + (p[1] - mean_lat) ** 2)
        streets[street_norm] = (neutralize_formula(displays[street_norm]), lon, lat)
    return streets


def _write_streets(conn: sqlite3.Connection, streets: dict[str, tuple[str, float, float]]) -> None:
    conn.executemany(
        _INSERT_STREET,
        [(norm, display, lon, lat) for norm, (display, lon, lat) in sorted(streets.items())],
    )


def _write_address_points(conn: sqlite3.Connection, staged: Sequence[StagedAddressPoint]) -> None:
    conn.executemany(
        _INSERT_ADDRESS,
        [
            (row.street_norm, row.house_number, row.display, row.zipcode, row.lon, row.lat)
            for row in staged
        ],
    )


def _write_intersections(conn: sqlite3.Connection, displays: dict[str, str]) -> int:
    """Every ordered pair of street names meeting at a `street_node`.

    Stored both ways round so a lookup is one seek rather than two. A node
    naming one street is not a corner and is skipped.
    """
    records: list[tuple[str, str, str, float, float]] = []
    for row in conn.execute("SELECT lon, lat, street_names FROM street_node"):
        names = json_string_list(row["street_names"])
        if len(names) < 2:
            continue
        lon, lat = float(row["lon"]), float(row["lat"])
        for index, first in enumerate(names):
            for second in names[index + 1 :]:
                a_norm, b_norm = fold(first), fold(second)
                if not a_norm or not b_norm or a_norm == b_norm:
                    continue
                label = neutralize_formula(
                    f"{displays.get(a_norm, first)} & {displays.get(b_norm, second)}"
                )
                records.append((a_norm, b_norm, label, lon, lat))
                records.append((b_norm, a_norm, label, lon, lat))
    conn.executemany(_INSERT_INTERSECTION, records)
    return len(records) // 2


def _write_places(conn: sqlite3.Connection, staged: Sequence[StagedPlace]) -> int:
    """The places and their word index. Returns the number of token rows written."""
    conn.executemany(
        _INSERT_PLACE,
        [(index, place.display, place.lon, place.lat) for index, place in enumerate(staged)],
    )
    tokens = [
        (word, position, index, search_name(words))
        for index, place in enumerate(staged)
        for words in place.spellings
        for position, word in enumerate(words)
    ]
    conn.executemany(_INSERT_PLACE_TOKEN, tokens)
    return len(tokens)


def _write_street_tokens(
    conn: sqlite3.Connection, variants: set[tuple[str, str]], centerline: frozenset[str]
) -> int:
    """One row per word of every spelling of every street that has a point to offer.

    Built from the variants rather than from the canonical name alone, so the
    directional and the suffix a person leaves off do not push the word they
    did type off the front of the name: "HOUSTON" is the first word of a
    spelling of E HOUSTON ST, not the second word of its only one. The
    `streets.NAME_ALIASES` keys are added unfolded — folding one applies the
    alias map again and turns it back into its target — which is what puts
    "KING" and "MALCOLM" in the index at all.
    """
    spellings = {(variant, street_norm) for variant, street_norm in variants}
    spellings |= {(alias, target) for alias, target in NAME_ALIASES.items() if target in centerline}
    tokens = {
        (word, position, street_norm, search_name(words))
        for spelling, street_norm in spellings
        if street_norm in centerline
        for words in [spelling_words(spelling)]
        for position, word in enumerate(words)
    }
    conn.executemany(_INSERT_STREET_TOKEN, sorted(tokens))
    return len(tokens)


def _street_variant_pairs(
    conn: sqlite3.Connection, centerline: frozenset[str]
) -> set[tuple[str, str]]:
    """(spelling, street) for every street either source knows, plus the nicknames.

    AddressPoint files doors on names the centerline has no segment for
    (GOVERNORS ISLAND, POMANDER WALK), so the variant table is built from both
    tables rather than from the centerline alone.
    """
    known = set(centerline) | {
        str(row[0]) for row in conn.execute("SELECT DISTINCT street_norm FROM address_point")
    }
    pairs = {
        (variant, street_norm) for street_norm in known for variant in street_variants(street_norm)
    }
    pairs |= {(fold(nick), target) for nick, target in NICKNAMES.items() if target in known}
    return pairs


def _spelling_texts(variants: set[tuple[str, str]], centerline: frozenset[str]) -> frozenset[str]:
    """The word form of every street spelling, which is what a place may not repeat.

    Only the streets with a centerline behind them. AddressPoint files doors on
    BRYANT PARK and LINCOLN CENTER PLZ, which CSCL has no segment for and the
    street rung therefore has no point to offer: dropping the CommonPlace
    entries that name them would lose the name altogether.
    """
    return frozenset(
        search_name(spelling_words(variant))
        for variant, street_norm in variants
        if street_norm in centerline
    )


def _middle_vertex(geom_json: str) -> tuple[float, float] | None:
    """A vertex near the middle of a GeoJSON LineString or MultiLineString.

    A vertex rather than an interpolated point: it is the cheapest point
    guaranteed to lie on the street rather than across a bend, and the index
    only needs somewhere to centre the map when a bare street name is picked.
    """
    try:
        geometry = json.loads(geom_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(geometry, dict):
        return None
    coords = geometry.get("coordinates")
    if not isinstance(coords, list) or not coords:
        return None
    if geometry.get("type") == "MultiLineString":
        parts = [part for part in coords if isinstance(part, list) and part]
        if not parts:
            return None
        coords = max(parts, key=len)
    return _coordinate(coords[len(coords) // 2])


def _point(geometry: dict[str, Any] | None) -> tuple[float, float] | None:
    """The (lon, lat) of a GeoJSON Point, or None when it is missing or unreadable."""
    if not isinstance(geometry, dict):
        return None
    return _coordinate(geometry.get("coordinates"))


def _coordinate(position: Any) -> tuple[float, float] | None:
    """A GeoJSON position as finite floats. Downloaded numbers are untrusted (CLAUDE.md)."""
    if not isinstance(position, (list, tuple)) or len(position) < 2:
        return None
    try:
        lon, lat = float(position[0]), float(position[1])
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lon) and math.isfinite(lat)):
        return None
    return (lon, lat)


def _house_int(value: str | None) -> int | None:
    """Leading digits of a house number: `'450'`, `'159-48'` -> 159, `'REAR'` -> None."""
    match = _LEADING_DIGITS.match(str(value or "").strip())
    return int(match.group(1)) if match else None


def _zipcode(value: str | None) -> str | None:
    """A five-digit ZIP, or None. AddressPoint leaves it null on two Manhattan rows."""
    text = str(value or "").strip()
    return text if len(text) == 5 and text.isdigit() else None
