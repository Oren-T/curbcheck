"""The differential harness's query set, built from the database and fixed by a seed.

`docs/STATIC_SITE.md` "The differential harness" defines what has to be in it:
destinations spread over the coverage box, the windows that make the calendar
and the DST rules matter, both weight sets, both cap settings, then the typed
strings, the dropped pins, and the spans each search produced.

Nothing here runs the engine. `make_fixtures.py` turns these queries into the
reference answers and `compare.test.js` replays the same `args` through the
JavaScript engine, so the only thing that must stay stable between the two runs
is this file's output: same database, same seed, same queries, byte for byte.

Two numbers shape the size of the set, both measured on the 2026-09-15
database over the `data/` mount with a warm page cache:

| walk_minutes | seconds per search | JSON per search |
|---|---|---|
| 3  | 0.42 | 0.18 MB |
| 10 | 0.74 | 1.44 MB |
| 30 | 2.82 | 6.04 MB at `map_limit` 5000 |

Six hundred searches drawn uniformly from those three radii would be a
twenty-minute run and a 1.5 GB `expected.json`, which V8 cannot even read: its
maximum string length is ~512 MB. `WALK_MINUTES_MIX` therefore keeps all three
radii but weights the cheap one, and every 30-minute search runs at the default
caps. The result is the run and the file `README.md` records.
"""

from __future__ import annotations

import ast
import random
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from curbcheck.config import NYC_TZ
from curbcheck.engine.coverage import coverage_bbox, within_coverage

SEED = 20260915

# The query set is defined by this file's output, so every count below is part
# of that definition. Changing one changes the fixture.
SEARCH_QUERIES = 600
GEOCODE_QUERIES = 200
REVERSE_QUERIES = 100
# Six spans per search, deduplicated, would be ~7,000 segment calls at 43 ms
# each. The cap keeps the third of the run that checks the detail panel to
# about a minute, and the stride that applies it spreads the survivors over the
# whole search set rather than keeping the first searches' spans only.
MAX_SEGMENT_QUERIES_PER_KIND = 600

# Drawn over the coverage box, filtered by `within_coverage`: 96 of these 300
# land within 250 m of a centerline on the 2026-09-15 database and 204 do not,
# which is where the `outside_coverage` cases come from.
DESTINATION_DRAWS = 300
OUTSIDE_COVERAGE_SEARCHES = 8
ADDRESS_NOT_FOUND_SEARCHES = 2

# Cycled, then shuffled, over the searches. See the table in the module
# docstring for what each radius costs.
WALK_MINUTES_MIX: tuple[float, ...] = (3.0,) * 40 + (10.0,) * 3 + (30.0,)
SKEWED_WEIGHTS = {"walk": 2.0, "money": 0.5, "risk": 3.0}
# "nothing cut": on this database a 30-minute radius holds 4,694 spans, so 500
# and 5,000 are above every count the set produces.
WIDE_LIMIT = 500
WIDE_MAP_LIMIT = 5000
DEFAULT_CAP_SEARCHES = 30

# A fifth of the searches send the window the way a client with a time zone
# does, half as `-04:00`/`-05:00` and half as `Z`. Both forms are here because
# they are read differently: the offset names the same wall clock the naive
# string would have meant, `Z` names an instant the reader has to convert.

# 2026-03-08 02:00 EST -> 03:00 EDT and 2026-11-01 02:00 EDT -> 01:00 EST.
SPRING_FORWARD = date(2026, 3, 8)
FALL_BACK = date(2026, 11, 1)
# A Wednesday, the Saturday and the Sunday of the same week.
WEEKDAY = date(2026, 9, 16)
SATURDAY = date(2026, 9, 19)
SUNDAY = date(2026, 9, 20)
# 42 suspension dates is more window than the set needs; every other one keeps
# the spread across the year.
ASP_WINDOWS = 20

_REPO_ROOT = Path(__file__).resolve().parents[3]
_GEOCODE_TEST = _REPO_ROOT / "tests" / "test_geocode_real.py"


@dataclass(frozen=True)
class Query:
    """One call to make: `op` and the `args` the worker's envelope carries."""

    id: str
    op: str
    args: dict[str, Any]


@dataclass(frozen=True)
class Window:
    """A parking window as wall-clock New York time, before it is rendered to a string."""

    name: str
    t1: datetime
    t2: datetime


def build_queries(conn: sqlite3.Connection) -> list[Query]:
    """The search, geocode and reverse queries, in that order.

    The segment queries are not here: they name the spans each search returned,
    so `make_fixtures` builds them with `segment_queries` once the searches
    have run. One `random.Random(SEED)` is threaded through all three sections,
    which makes the order of the calls below part of the definition.
    """
    # A fixed-seed PRNG is the whole point here, as in tests/test_parse_fuzz.py:
    # nothing about this set is a secret, and it has to be the same set twice.
    rng = random.Random(SEED)  # noqa: S311
    return [
        *_search_queries(conn, rng),
        *_geocode_queries(conn, rng),
        *_reverse_queries(conn, rng),
    ]


def result_span_ids(result: dict[str, Any]) -> list[str]:
    """The first three legal and first three non-legal spans of one search answer.

    `docs/STATIC_SITE.md`: the detail panel is checked on the spans a search
    actually produced, which is what makes the segment set follow the search
    set rather than being a second sample of the span table.
    """
    legal = [row["reg_seg_id"] for row in result["results"] if row["verdict"] == "legal"]
    other = [row["reg_seg_id"] for row in result["results"] if row["verdict"] != "legal"]
    return [*legal[:3], *other[:3]]


def segment_queries(sources: list[tuple[dict[str, Any], list[str]]]) -> list[Query]:
    """Segment queries for every span a search returned, with its window and without.

    `sources` is one entry per successful search: the search's `args` (for the
    `t1`/`t2` strings exactly as they were sent) and the span ids from
    `result_span_ids`. Deduplicated, because 600 searches over 146 destinations
    revisit the same curb, and then thinned by a stride to the cap.
    """
    windowed: dict[tuple[str, str, str], dict[str, Any]] = {}
    bare: dict[str, dict[str, Any]] = {}
    for args, span_ids in sources:
        for reg_seg_id in span_ids:
            windowed.setdefault(
                (reg_seg_id, args["t1"], args["t2"]),
                {"reg_seg_id": reg_seg_id, "t1": args["t1"], "t2": args["t2"]},
            )
            bare.setdefault(reg_seg_id, {"reg_seg_id": reg_seg_id})

    kept = [
        *_stride(list(windowed.values()), MAX_SEGMENT_QUERIES_PER_KIND),
        *_stride(list(bare.values()), MAX_SEGMENT_QUERIES_PER_KIND),
        # The four ways the endpoint refuses: the charset check, a well-formed
        # id nothing carries, half a window, and a window under five minutes.
        {"reg_seg_id": "not an id!"},
        {"reg_seg_id": "0123456789abcdef"},
        {"reg_seg_id": _any_id(windowed), "t1": "2026-09-16T09:00:00"},
        {
            "reg_seg_id": _any_id(windowed),
            "t1": "2026-09-16T09:00:00",
            "t2": "2026-09-16T09:03:00",
        },
    ]
    return [
        Query(id=f"segment/{index:04d}", op="segment", args=args) for index, args in enumerate(kept)
    ]


def _any_id(windowed: dict[tuple[str, str, str], dict[str, Any]]) -> str:
    """A real span id for the malformed-window cases, so only the window is wrong."""
    for reg_seg_id, _, _ in windowed:
        return reg_seg_id
    return "0123456789abcdef"


def _stride(values: list[dict[str, Any]], cap: int) -> list[dict[str, Any]]:
    if len(values) <= cap:
        return values
    step = (len(values) + cap - 1) // cap
    return values[::step][:cap]


def _search_queries(conn: sqlite3.Connection, rng: random.Random) -> list[Query]:
    inside, outside = _destinations(conn, rng)
    addresses = [{"address": text} for text in _test_module_queries()]
    destinations = [*({"lat": lat, "lon": lon} for lon, lat in inside), *addresses]
    windows = _windows(conn)

    failures = [
        *({"lat": lat, "lon": lon} for lon, lat in outside[:OUTSIDE_COVERAGE_SEARCHES]),
        {"address": "zzqq nowhere avenue"},
        {"address": "90210 boulevard of broken data"},
    ]
    main = SEARCH_QUERIES - len(failures) - len(_INVALID_SEARCH_ARGS)

    walks = _cycle(list(WALK_MINUTES_MIX), main)
    aware = main // 10
    styles = ["offset"] * aware + ["utc"] * aware + ["naive"] * (main - 2 * aware)
    skewed = _cycle([True] + [False] * 3, main)
    rng.shuffle(walks)
    rng.shuffle(styles)
    rng.shuffle(skewed)
    default_caps = _default_cap_indexes(walks)

    queries = []
    for index in range(main):
        window = windows[index % len(windows)]
        args: dict[str, Any] = dict(destinations[index % len(destinations)])
        args["t1"] = _render(window.t1, styles[index])
        args["t2"] = _render(window.t2, styles[index])
        args["walk_minutes"] = walks[index]
        if skewed[index]:
            args["weights"] = dict(SKEWED_WEIGHTS)
        if index not in default_caps:
            args["limit"] = WIDE_LIMIT
            args["map_limit"] = WIDE_MAP_LIMIT
        queries.append(args)

    # The refusals go last so that adding one does not renumber the rest.
    for args in failures:
        queries.append({**args, "t1": "2026-09-16T09:00:00", "t2": "2026-09-16T11:00:00"})
    queries.extend(dict(args) for args in _INVALID_SEARCH_ARGS)
    return [
        Query(id=f"search/{index:04d}", op="search", args=args)
        for index, args in enumerate(queries)
    ]


# Every one of these is a 422 `validation_error` from `SearchRequest`: unknown
# field, half a destination, two destinations, no destination, an unparseable
# time, a window that ends before it starts, one under five minutes, and a walk
# radius outside 1-30.
_INVALID_SEARCH_ARGS: tuple[dict[str, Any], ...] = (
    {
        "lat": 40.75,
        "lon": -73.98,
        "t1": "2026-09-16T09:00:00",
        "t2": "2026-09-16T11:00:00",
        "zoom": 12,
    },
    {"lat": 40.75, "t1": "2026-09-16T09:00:00", "t2": "2026-09-16T11:00:00"},
    {
        "lat": 40.75,
        "lon": -73.98,
        "address": "350 5th",
        "t1": "2026-09-16T09:00:00",
        "t2": "2026-09-16T11:00:00",
    },
    {"t1": "2026-09-16T09:00:00", "t2": "2026-09-16T11:00:00"},
    {"lat": 40.75, "lon": -73.98, "t1": "not a time", "t2": "2026-09-16T11:00:00"},
    {"lat": 40.75, "lon": -73.98, "t1": "2026-09-16T11:00:00", "t2": "2026-09-16T09:00:00"},
    {"lat": 40.75, "lon": -73.98, "t1": "2026-09-16T09:00:00", "t2": "2026-09-16T09:03:00"},
    {
        "lat": 40.75,
        "lon": -73.98,
        "t1": "2026-09-16T09:00:00",
        "t2": "2026-09-16T11:00:00",
        "walk_minutes": 45.0,
    },
)


def _destinations(
    conn: sqlite3.Connection, rng: random.Random
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """`DESTINATION_DRAWS` points over the coverage box, split by `within_coverage`.

    The rejected points are not waste: they are the only honest source of
    `outside_coverage` destinations, since a hand-picked one would be a guess
    about where the 250 m rule falls.
    """
    bbox = coverage_bbox(conn)
    if bbox is None:
        raise RuntimeError("the database has no street_segment rows to take a coverage box from")
    min_lon, min_lat, max_lon, max_lat = bbox
    inside: list[tuple[float, float]] = []
    outside: list[tuple[float, float]] = []
    for _ in range(DESTINATION_DRAWS):
        lon = rng.uniform(min_lon, max_lon)
        lat = rng.uniform(min_lat, max_lat)
        target = inside if within_coverage(conn, lon=lon, lat=lat) else outside
        target.append((lon, lat))
    if len(inside) < 20 or len(outside) < OUTSIDE_COVERAGE_SEARCHES:
        raise RuntimeError(f"the coverage box gave {len(inside)} in and {len(outside)} out")
    return inside, outside


def _windows(conn: sqlite3.Connection) -> list[Window]:
    """The windows the harness rotates through, in a fixed order.

    Thirty-two of them, and the six on the DST Sundays are the reason this list
    is not three entries long.
    """
    windows = [
        _window("weekday", WEEKDAY, "09:00", 120),
        _window("saturday", SATURDAY, "08:00", 120),
        _window("sunday", SUNDAY, "10:00", 120),
    ]
    for day in _asp_dates(conn):
        windows.append(_window(f"asp-{day.isoformat()}", day, "09:00", 120))
    for day in (SPRING_FORWARD, FALL_BACK):
        label = "spring" if day == SPRING_FORWARD else "fall"
        windows.append(_window(f"dst-{label}-around", day, "00:30", 240))
        windows.append(_window(f"dst-{label}-inside", day, "01:30", 120))
        # Wall-clock midnight to wall-clock midnight: 23 real hours in spring
        # and 25 in autumn, and both are accepted, because Python subtracts two
        # datetimes that share a tzinfo as wall clocks and gets 24 hours either
        # way. An engine that measures the window in epoch milliseconds refuses
        # the autumn one, which is the difference this window exists to find.
        windows.append(
            Window(
                name=f"dst-{label}-day",
                t1=datetime.combine(day, datetime.min.time()),
                t2=datetime.combine(day + timedelta(days=1), datetime.min.time()),
            )
        )
    windows.append(_window("midnight-crossing", WEEKDAY, "23:00", 150))
    windows.append(_window("five-minutes", WEEKDAY, "14:00", 5))
    windows.append(_window("twenty-four-hours", WEEKDAY, "00:00", 24 * 60))
    return windows


def _window(name: str, day: date, start: str, minutes: int) -> Window:
    t1 = datetime.combine(day, datetime.strptime(start, "%H:%M").time())
    return Window(name=name, t1=t1, t2=t1 + timedelta(minutes=minutes))


def _asp_dates(conn: sqlite3.Connection) -> list[date]:
    rows = conn.execute("SELECT date FROM asp_suspension ORDER BY date").fetchall()
    days = [date.fromisoformat(str(row[0])) for row in rows]
    if len(days) <= ASP_WINDOWS:
        return days
    return days[:: max(1, len(days) // ASP_WINDOWS)][:ASP_WINDOWS]


def _render(value: datetime, style: str) -> str:
    """A wall clock as the string a client sends.

    `naive` is what the UI sends and the server reads as New York. `offset`
    names the same wall clock with the zone's own offset. `utc` is the same
    instant written as `Z`, which is the one form whose wall clock the reader
    has to compute rather than copy.
    """
    if style == "naive":
        return value.isoformat()
    aware = value.replace(tzinfo=NYC_TZ)
    if style == "offset":
        return aware.isoformat()
    return aware.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _default_cap_indexes(walks: list[float]) -> set[int]:
    """Which searches send no `limit` or `map_limit`.

    Every 30-minute search, because `map_limit` 5000 writes 6 MB of JSON for
    one of those and 2,000 is what the frontend actually sends, plus a spread
    of the rest up to `DEFAULT_CAP_SEARCHES`.
    """
    chosen = {index for index, walk in enumerate(walks) if walk == 30.0}
    stride = max(1, len(walks) // (DEFAULT_CAP_SEARCHES * 2))
    for index in range(0, len(walks), stride):
        if len(chosen) >= DEFAULT_CAP_SEARCHES:
            break
        chosen.add(index)
    return chosen


def _cycle(values: list[Any], count: int) -> list[Any]:
    return [values[index % len(values)] for index in range(count)]


def _test_module_queries() -> list[str]:
    """The address strings `tests/test_geocode_real.py` measures the suggester with.

    Read out of the file with `ast` rather than imported: importing a pytest
    module to get three lists would pull pytest into a script that otherwise
    needs nothing but the standard library and `curbcheck`.
    """
    tree = ast.parse(_GEOCODE_TEST.read_text(encoding="utf-8"))
    wanted = ("DEMO_QUERIES", "MATRIX", "STREET_FIRST_QUERIES")
    found: dict[str, list[str]] = {}
    for node in tree.body:
        name, value = _assigned(node)
        if name in wanted and isinstance(value, ast.List | ast.Tuple):
            found[name] = [text for element in value.elts if (text := _first_string(element))]
    missing = [name for name in wanted if not found.get(name)]
    if missing:
        raise RuntimeError(f"{_GEOCODE_TEST} no longer defines {missing}")
    return _unique([text for name in wanted for text in found[name]])


def _assigned(node: ast.stmt) -> tuple[str, ast.expr | None]:
    if (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    ):
        return node.targets[0].id, node.value
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id, node.value
    return "", None


def _first_string(element: ast.expr) -> str | None:
    """The string a list element is, or the first field of the tuple it is."""
    if isinstance(element, ast.Tuple) and element.elts:
        element = element.elts[0]
    return (
        element.value
        if isinstance(element, ast.Constant) and isinstance(element.value, str)
        else None
    )


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


# What a person types that no table can be sampled for: trailing punctuation,
# an abbreviation with a full stop, doubled spaces, a leading hash. Each one is
# here because `geocode.query.clean_query` has a rule about it.
_PUNCTUATION_QUERIES: tuple[str, ...] = (
    "350 5th ave.",
    "w. 42nd st",
    "broadway & 42nd",
    "moma!",
    "#1 police plaza",
    "st. nicholas ave",
    "1519  3rd   ave",
    "  bryant park  ",
    "e 86 st & 3 ave",
    "washington sq. park",
)

_SINGLE_CHARACTER_QUERIES: tuple[str, ...] = ("a", "b", "e", "w", "q", "z", "1", "3", "5", "9")

# 120 is `geocode.MAX_QUERY_CHARS`: the first of these is the longest string
# the endpoint accepts, the second the shortest one it refuses.
_LONG_QUERY = ("350 5th ave " * 20)[:120]

_GEOCODE_GROUP_SIZES = {
    "corners": 20,
    "places": 20,
    "streets": 20,
    "zips": 10,
    "typos": 15,
    "prefixes": 12,
}


def _geocode_queries(conn: sqlite3.Connection, rng: random.Random) -> list[Query]:
    """`GEOCODE_QUERIES` typed strings: what the tables hold, and what fingers do to it."""
    doors = _rows(conn, "SELECT house_number, street_norm, display FROM address_point")
    corners = _rows(conn, "SELECT a_norm, b_norm, display FROM intersection")
    places = _rows(conn, "SELECT display FROM place")
    streets = _rows(conn, "SELECT display FROM street")
    zips = _rows(conn, "SELECT zipcode FROM zip_centroid")

    sizes = _GEOCODE_GROUP_SIZES
    groups: list[list[str]] = [
        # First, so that the four boundary cases survive the truncation below.
        ["", _LONG_QUERY, _LONG_QUERY + "x", "b" * 600],
        _test_module_queries(),
        [
            _corner_query(row, index)
            for index, row in enumerate(_sample(rng, corners, sizes["corners"]))
        ],
        [
            _place_query(row, index)
            for index, row in enumerate(_sample(rng, places, sizes["places"]))
        ],
        [
            _street_query(row, index)
            for index, row in enumerate(_sample(rng, streets, sizes["streets"]))
        ],
        [str(row[0]) for row in _sample(rng, zips, sizes["zips"] - 1)] + ["09999"],
        [_typo(f"{row[0]} {row[1]}".lower(), rng) for row in _sample(rng, doors, sizes["typos"])],
        list(_SINGLE_CHARACTER_QUERIES),
        [str(row[0]).lower()[:5].strip() for row in _sample(rng, places, sizes["prefixes"])],
        list(_PUNCTUATION_QUERIES),
    ]
    spent = sum(len(group) for group in groups)
    if spent >= GEOCODE_QUERIES - 20:
        raise RuntimeError(f"{spent} fixed geocode strings leaves no room for doors")

    door_rows = _sample(rng, doors, GEOCODE_QUERIES - spent + 20)
    groups.append([_door_query(row, index) for index, row in enumerate(door_rows)])
    strings = _unique([text for group in groups for text in group])[:GEOCODE_QUERIES]
    if len(strings) != GEOCODE_QUERIES:
        raise RuntimeError(f"{len(strings)} unique geocode strings, wanted {GEOCODE_QUERIES}")
    return [
        Query(id=f"geocode/{index:04d}", op="geocode", args={"q": text})
        for index, text in enumerate(strings)
    ]


def _door_query(row: sqlite3.Row, index: int) -> str:
    house, street, display = str(row[0]), str(row[1]), str(row[2])
    forms = (
        f"{house} {street}",
        f"{house} {street}".lower(),
        f"{house} {_ordinalize(street)}".lower(),
        f"{house} {street.lower()}.",
        display,
    )
    return forms[index % len(forms)]


def _corner_query(row: sqlite3.Row, index: int) -> str:
    first, second, display = str(row[0]), str(row[1]), str(row[2])
    forms = (
        display,
        f"{first} and {second}".lower(),
        f"{first} & {second}".lower(),
        f"{first.split()[0]} & {second.split()[0]}".lower(),
    )
    return forms[index % len(forms)]


def _place_query(row: sqlite3.Row, index: int) -> str:
    display = str(row[0])
    words = display.split()
    forms = (
        display,
        display.lower(),
        words[0].lower(),
        " ".join(words[-2:]).lower(),
        " ".join(reversed(words)).lower(),
    )
    return forms[index % len(forms)]


def _street_query(row: sqlite3.Row, index: int) -> str:
    display = " ".join(str(row[0]).split())
    forms = (display, display.lower(), display.lower()[:5].strip(), _ordinalize(display).lower())
    return forms[index % len(forms)]


def _ordinalize(name: str) -> str:
    """ "E 118 ST" -> "E 118th ST", the way a person types a numbered street."""
    return " ".join(_ordinal(word) if word.isdigit() else word for word in name.split())


_ORDINAL_SUFFIXES = {1: "st", 2: "nd", 3: "rd"}


def _ordinal(digits: str) -> str:
    value = int(digits)
    if value % 100 in (11, 12, 13):
        return f"{value}th"
    return f"{value}{_ORDINAL_SUFFIXES.get(value % 10, 'th')}"


def _typo(text: str, rng: random.Random) -> str:
    """One transposition, which is the typo the fuzzy rung exists for."""
    if len(text) < 4:
        return text
    cut = rng.randrange(1, len(text) - 2)
    return text[:cut] + text[cut + 1] + text[cut] + text[cut + 2 :]


# Hand-picked interiors of Central Park, where the nearest centerline is far
# enough away to matter: `engine.coverage` measured the Great Lawn at 184 m
# (inside) and the reservoir at 287 m (refused), which are the two sides of the
# 250 m rule that no sampled door can reach.
_PARK_PINS: tuple[tuple[float, float], ...] = (
    (-73.9665, 40.7812),
    (-73.9625, 40.7855),
    (-73.9750, 40.7718),
)
# Water and the other boroughs, inside the input box and outside coverage.
_OUTSIDE_COVERAGE_PINS: tuple[tuple[float, float], ...] = (
    (-74.0324, 40.7440),
    (-74.0400, 40.7000),
    (-73.8900, 40.8600),
    (-73.8850, 40.7400),
    (-73.9000, 40.6900),
)
# Outside `schemas.MIN_LAT`/`MAX_LON`, so the request never reaches the engine.
_OFF_MAP_PINS: tuple[tuple[float, float], ...] = ((-73.9000, 41.5000), (-75.0000, 40.7500))
_PARK_PLACE_PINS = 15
# ~20 m, the distance between a dropped pin and the door it is meant for.
_PIN_JITTER_DEG = 2e-4


def _reverse_queries(conn: sqlite3.Connection, rng: random.Random) -> list[Query]:
    """`REVERSE_QUERIES` dropped pins: near doors, inside parks, and off the map."""
    parks = _rows(
        conn,
        "SELECT lon, lat FROM place WHERE display LIKE '% PARK' OR display LIKE '% PARK %'",
    )
    fixed = [*_PARK_PINS, *_OUTSIDE_COVERAGE_PINS, *_OFF_MAP_PINS]
    park_pins = [(float(row[0]), float(row[1])) for row in _sample(rng, parks, _PARK_PLACE_PINS)]
    door_count = REVERSE_QUERIES - len(fixed) - len(park_pins)
    doors = _rows(conn, "SELECT lon, lat FROM address_point")
    door_pins = [
        (
            float(row[0]) + rng.uniform(-_PIN_JITTER_DEG, _PIN_JITTER_DEG),
            float(row[1]) + rng.uniform(-_PIN_JITTER_DEG, _PIN_JITTER_DEG),
        )
        for row in _sample(rng, doors, door_count)
    ]
    pins = [*door_pins, *park_pins, *fixed]
    return [
        Query(id=f"reverse/{index:04d}", op="reverse", args={"lat": lat, "lon": lon})
        for index, (lon, lat) in enumerate(pins)
    ]


def _rows(conn: sqlite3.Connection, sql: str) -> list[sqlite3.Row]:
    """Every row of a table, in rowid order, so `_sample` draws from a fixed list."""
    return conn.execute(sql).fetchall()


def _sample(rng: random.Random, rows: list[sqlite3.Row], count: int) -> list[sqlite3.Row]:
    if count >= len(rows):
        return rows
    return [rows[index] for index in sorted(rng.sample(range(len(rows)), count))]
