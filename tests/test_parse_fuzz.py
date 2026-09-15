"""Mutation fuzzing of the three functions that read untrusted text.

`parse_description`, `normalize_street_name`, `geocode.parse_query` and
`geocode.suggest` are the places hostile bytes from NYC Open Data and from the
address box first meet code. SPEC §12 phase 1 asks for exactly this: fuzz the sign-text parser with
adversarial strings before trusting it.

The contract each function is held to here is narrow and absolute:

- it returns rather than raising, for any input at all;
- it returns within `MAX_CALL_S`, so a pathological string cannot stall a sync
  or an API request (catastrophic regex backtracking is the usual way that
  happens);
- `parse_description` never claims to have read a rule it did not (a raising
  parser and a guessing parser are both failures, per SPEC §11);
- `suggest` returns candidates and never mutates the database, however the
  query is spelled: it reaches SQLite on every call, and every value it sends
  is a bound parameter.

Marked `slow` and deselected by default: `make test` runs in seconds and this
takes about a minute. Run it with `pytest -m slow`.
"""

from __future__ import annotations

import random
import sqlite3
import string
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from curbcheck.config import REPO_ROOT
from curbcheck.db import create_schema
from curbcheck.etl.addresses import build_address_index
from curbcheck.etl.parse import parse_description
from curbcheck.etl.streets import normalize_street_name
from curbcheck.geocode import (
    AddressQuery,
    GeocodeCandidate,
    IntersectionQuery,
    StreetQuery,
    ZipQuery,
    parse_query,
    suggest,
)
from curbcheck.model import ParseMethod

ITERATIONS = 20_000

# Measured worst case over 20k inputs on the real corpus is about 15 ms for
# `parse_description` and under 1 ms for the other two. 50 ms leaves room for a
# slow CI runner while still failing on a quadratic blowup.
MAX_CALL_S = 0.050

SEED = 20260915

DESCRIPTIONS_TSV = REPO_ROOT / "data" / "explore" / "descriptions.tsv"
STREET_NAMES_TXT = REPO_ROOT / "data" / "explore" / "street_names.txt"

# Fallback seeds, used when the explore corpus is not on the machine. They are
# the templates SPEC §8.4 lists, which is what the grammar is built around.
FALLBACK_DESCRIPTIONS = (
    "2 HMP SATURDAY 8AM-7PM ·",
    "NO PARKING (SANITATION BROOM SYMBOL) MONDAY THURSDAY 9AM-10:30AM",
    "NO STANDING ANYTIME",
    "1 HOUR METERED PARKING 9AM-7PM INCLUDING SUNDAY",
    "2 HOUR PARKING 8AM-6PM EXCEPT SUNDAY",
    "NIGHT REGULATION NO STANDING 8PM-6AM ALL DAYS",
    "3 HOUR PARKING 9AM-6PM MON-FRI (SYMBOL) COMMERCIAL VEHICLES ONLY",
)
FALLBACK_STREETS = ("EAST 85 STREET", "3 AVENUE", "BROADWAY", "W 125 ST", "FDR DRIVE")
QUERY_SEEDS = ("123 E 85 St", "1500 3rd Ave", "Lexington Ave & 86th St", "E 86 St and 3 Ave")

# Fragments chosen to hit the parser's own delimiters and the classes of
# character the threat model names: markup, formula leads, SQL metacharacters,
# bidi controls, combining marks, and unbalanced grouping.
SPLICES = (
    "\x00",
    "﻿",
    "‮",
    "⁦",
    "́",
    "<script>",
    "</script>",
    "=1+1",
    "'; DROP TABLE sign; --",
    "(" * 40,
    ")" * 40,
    "-" * 120,
    ":" * 60,
    "\\",
    "\n",
    "\r\n",
    "\t",
    "%s",
    "{}",
    "AM",
    "PM",
    "12:",
    "MIDNIGHT",
    "EXCEPT",
    "-->",
    "<->",
    "\U0001f6a7",
    "퟿",
    "é",
)


def _read_lines(path: Path, *, column: int | None = None) -> list[str]:
    if not path.exists():
        return []
    values = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[1:]:
        field = line.split("\t")[column] if column is not None else line
        if field.strip():
            values.append(field)
    return values


def description_seeds() -> list[str]:
    return _read_lines(DESCRIPTIONS_TSV, column=-1) or list(FALLBACK_DESCRIPTIONS)


def street_seeds() -> list[str]:
    return _read_lines(STREET_NAMES_TXT) or list(FALLBACK_STREETS)


def mutate(text: str, rng: random.Random) -> str:
    """Apply one to four splice/delete/duplicate/rotate edits to `text`."""
    for _ in range(rng.randint(1, 4)):
        if not text:
            text = "X"
        cut = rng.randrange(len(text))
        operation = rng.randrange(7)
        if operation == 0:
            text = text[:cut] + rng.choice(SPLICES) + text[cut:]
        elif operation == 1:
            text = text[:cut] + text[cut + 1 :]
        elif operation == 2:
            text = text[:cut] + rng.choice(string.printable) + text[cut:]
        elif operation == 3 and len(text) < 4000:
            text = text * 2
        elif operation == 4:
            text = text[:cut]
        elif operation == 5:
            text = text[cut:] + text[:cut]
        else:
            text = text.upper() if rng.random() < 0.5 else text.lower()
    return text


def generate(seeds: list[str], rng: random.Random) -> str:
    """A seed, a mutated seed, random code points, or arbitrary bytes as latin-1."""
    roll = rng.random()
    if roll < 0.45:
        return mutate(rng.choice(seeds), rng)
    if roll < 0.70:
        return rng.choice(seeds)
    if roll < 0.85:
        return "".join(chr(rng.randrange(0, 0x2FFF)) for _ in range(rng.randint(0, 120)))
    return bytes(rng.randrange(256) for _ in range(rng.randint(0, 200))).decode("latin-1")


def _fuzz(
    function: Callable[[str], object], seeds: list[str], *, check: Callable[[str, object], None]
) -> None:
    rng = random.Random(SEED)
    slowest = 0.0
    slowest_input = ""
    for _ in range(ITERATIONS):
        text = generate(seeds, rng)
        started = time.perf_counter()
        try:
            result = function(text)
        except Exception as error:
            pytest.fail(f"{function.__name__} raised {type(error).__name__} on {text!r}: {error}")
        elapsed = time.perf_counter() - started
        if elapsed > slowest:
            slowest, slowest_input = elapsed, text
        check(text, result)
    assert slowest < MAX_CALL_S, (
        f"{function.__name__} took {slowest * 1000:.1f} ms on {slowest_input[:120]!r}"
    )


@pytest.mark.slow
def test_parse_description_never_raises_and_never_guesses() -> None:
    def check(text: str, result: object) -> None:
        parsed = result
        assert parsed.raw == text  # type: ignore[attr-defined]
        if parsed.parse_method is ParseMethod.UNPARSED:  # type: ignore[attr-defined]
            assert parsed.regulations == []  # type: ignore[attr-defined]
            assert parsed.confidence == 0.0  # type: ignore[attr-defined]

    _fuzz(parse_description, description_seeds(), check=check)


@pytest.mark.slow
def test_normalize_street_name_never_raises_on_arbitrary_text() -> None:
    def check(text: str, result: object) -> None:
        assert isinstance(result, str)

    _fuzz(normalize_street_name, street_seeds() + description_seeds(), check=check)


@pytest.mark.slow
def test_parse_query_never_raises_on_arbitrary_text() -> None:
    def check(text: str, result: object) -> None:
        assert result is None or isinstance(
            result, (AddressQuery, IntersectionQuery, StreetQuery, ZipQuery)
        )

    _fuzz(parse_query, [*QUERY_SEEDS, *street_seeds(), *description_seeds()], check=check)


def fuzz_index() -> sqlite3.Connection:
    """A one-block index, in memory: the fuzzer is about the code path, not the data."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_schema(conn)
    conn.execute(
        "INSERT INTO street_segment (segment_id, street_name, street_norm, geom,"
        " min_lon, min_lat, max_lon, max_lat, left_low_address, left_high_address)"
        " VALUES ('1', '3 AVE', '3 AVE', ?, -73.955, 40.778, -73.953, 40.779, '1510', '1528')",
        ('{"type": "LineString", "coordinates": [[-73.9545, 40.7781], [-73.9540, 40.7788]]}',),
    )
    conn.execute(
        "INSERT INTO street_node (node_id, lon, lat, street_names)"
        " VALUES ('n1', -73.9545, 40.7781, '[\"3 AVE\", \"E 85 ST\"]')"
    )
    build_address_index(
        conn,
        address_rows=[
            {
                "house_number": "1517",
                "full_street_name": "3 AVE",
                "zipcode": "10028",
                "the_geom": {"type": "Point", "coordinates": [-73.9542, 40.7785]},
            }
        ],
        place_rows=[
            {
                "feature_name": "GRACIE MANSION",
                "the_geom": {"type": "Point", "coordinates": [-73.9432, 40.7760]},
            }
        ],
    )
    conn.commit()
    return conn


@pytest.mark.slow
def test_suggest_never_raises_and_never_writes() -> None:
    conn = fuzz_index()
    rows_before = conn.execute("SELECT count(*) FROM address_point").fetchone()[0]

    def check(text: str, result: object) -> None:
        assert isinstance(result, list)
        assert all(isinstance(candidate, GeocodeCandidate) for candidate in result)
        assert len(result) <= 8

    try:
        _fuzz(
            lambda text: suggest(conn, text),
            [*QUERY_SEEDS, *street_seeds(), *description_seeds()],
            check=check,
        )
        assert conn.execute("SELECT count(*) FROM address_point").fetchone()[0] == rows_before
    finally:
        conn.close()
