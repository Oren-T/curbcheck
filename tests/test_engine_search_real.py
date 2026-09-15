"""`engine.search` against the real database: the answer must not depend on the caps.

The street label and the raw sign text are fetched after `limit` and
`map_limit` have chosen what to return, because they are the expensive half of
a wide search and nothing before the cap reads them (`_attach_details`). That is
only safe if a capped answer is byte-for-byte the answer an uncapped one would
have given for the same spans, which is what these two fixed queries check.

Slow and skipped without `data/curbcheck.sqlite`: each query is a real
30-minute-class radius over a 94 MB file. Run with `pytest -m slow`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

import pytest

from curbcheck.config import DB_PATH, NYC_TZ
from curbcheck.db import connect
from curbcheck.engine.search import (
    DEFAULT_LIMIT,
    DEFAULT_MAP_LIMIT,
    MAX_MAP_LIMIT,
    SearchResults,
    search,
)

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not DB_PATH.is_file(), reason="needs data/curbcheck.sqlite; run `curbcheck sync`"
    ),
]


@dataclass(frozen=True)
class Query:
    """One fixed search, named so a failure says which one moved."""

    name: str
    lon: float
    lat: float
    walk_minutes: float
    limit: int
    map_limit: int


# 1519 3 AVE is the worked example in docs/DATA.md §2.3; the second point is the
# E 46 ST block whose `unmatched_signs` placeholder the UX pass reproduced.
# The caps are the ones the API defaults to for the first query and tighter for
# the second, whose 10-minute radius holds one legal span and no 2,000th row.
QUERIES = (
    Query(
        name="1519 3 ave, 20 min",
        lon=-73.95393367616,
        lat=40.778575223369,
        walk_minutes=20,
        limit=DEFAULT_LIMIT,
        map_limit=DEFAULT_MAP_LIMIT,
    ),
    Query(
        name="e 46 st, 10 min",
        lon=-73.975830,
        lat=40.754640,
        walk_minutes=10,
        limit=1,
        map_limit=200,
    ),
)

WINDOW = (
    datetime.fromisoformat("2026-09-16T10:00").replace(tzinfo=NYC_TZ),
    datetime.fromisoformat("2026-09-16T12:00").replace(tzinfo=NYC_TZ),
)


@pytest.fixture(scope="module")
def conn() -> sqlite3.Connection:
    return connect(DB_PATH, readonly=True)


def run(conn: sqlite3.Connection, query: Query, *, capped: bool) -> SearchResults:
    t1, t2 = WINDOW
    return search(
        conn,
        lon=query.lon,
        lat=query.lat,
        t1=t1,
        t2=t2,
        walk_minutes_max=query.walk_minutes,
        limit=query.limit if capped else MAX_MAP_LIMIT,
        map_limit=query.map_limit if capped else MAX_MAP_LIMIT,
    )


@pytest.mark.parametrize("query", QUERIES, ids=lambda query: query.name)
def test_a_capped_answer_matches_the_uncapped_one_span_for_span(
    conn: sqlite3.Connection, query: Query
) -> None:
    capped = run(conn, query, capped=True)
    uncapped = run(conn, query, capped=False)

    assert capped.counts == uncapped.counts
    assert uncapped.counts.total > len(capped.all), (
        "the caps have to bite for this to mean anything"
    )

    by_id = {result.reg_seg_id: result for result in uncapped.all}
    for result in capped.all:
        assert result == by_id[result.reg_seg_id], result.reg_seg_id

    kept = {result.reg_seg_id for result in capped.all}
    assert [result.reg_seg_id for result in capped.all] == [
        result.reg_seg_id for result in uncapped.all if result.reg_seg_id in kept
    ]


@pytest.mark.parametrize("query", QUERIES, ids=lambda query: query.name)
def test_every_returned_span_carries_its_label_and_its_signs(
    conn: sqlite3.Connection, query: Query
) -> None:
    """The deferral's failure mode is a span that came back stripped of both."""
    results = run(conn, query, capped=True).all

    assert all(result.street_name for result in results)
    signed = [result for result in results if result.signs]
    assert len(signed) > len(results) // 2
    for result in signed:
        assert all(sign.sign_description for sign in result.signs)


@pytest.mark.parametrize("query", QUERIES, ids=lambda query: query.name)
def test_the_same_query_twice_on_one_connection_answers_the_same(
    conn: sqlite3.Connection, query: Query
) -> None:
    """Nothing the connection keeps between searches may leak into the next one."""
    assert run(conn, query, capped=True) == run(conn, query, capped=True)
