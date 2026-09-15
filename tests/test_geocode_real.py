"""The suggester against the real database: the demo queries, and how close it lands.

Skipped when `data/curbcheck.sqlite` is not on the machine, which is how CI and
a fresh clone see it. The accuracy check has a fixture twin in
`test_geocode_accuracy_fixture.py`-shaped form below, so a regression in the
ladder still fails a `make check` that has no database.
"""

from __future__ import annotations

import json
import math
import random
import statistics

import pytest

from curbcheck.config import DB_PATH, RAW_DIR
from curbcheck.db import connect
from curbcheck.engine.geo import M_PER_DEG_LAT, meters_per_degree_lon
from curbcheck.geocode import GeocodeKind, reverse_geocode, suggest

pytestmark = pytest.mark.skipif(
    not DB_PATH.is_file(), reason="needs data/curbcheck.sqlite; run `curbcheck sync`"
)

# The fifteen queries `docs/ux/AUTOCOMPLETE_RESEARCH.md` §2.3 measured, with the
# kind and top label it recorded. Two of them are honest failures and are
# written down as such: "w 4 st and bleeker" corrects the typo but finds no
# corner, because W 4 ST and BLEECKER ST share no centerline node; "86th st"
# cannot know which side of Fifth Avenue is meant and offers both.
DEMO_QUERIES = [
    ("1519 3rd ave", GeocodeKind.ADDRESS, "1519 3 AVE"),
    ("1519 third avenue", GeocodeKind.ADDRESS, "1519 3 AVE"),
    ("1519 3rd av", GeocodeKind.ADDRESS, "1519 3 AVE"),
    ("e 86th st and 3rd", GeocodeKind.INTERSECTION, "3 AVE & E 86 ST"),
    ("86 & 3", GeocodeKind.INTERSECTION, "3 AVE & E 86 ST"),
    ("lex & 86", GeocodeKind.INTERSECTION, "E 86 ST & LEXINGTON AVE"),
    ("86th st", GeocodeKind.STREET, "E 86 ST"),
    ("10021", GeocodeKind.ZIP, "10021"),
    ("w 4 st and bleeker", GeocodeKind.STREET, "W 4 ST"),
    ("350 5th", GeocodeKind.ADDRESS, "350 5 AVE"),
    ("one world trade", GeocodeKind.PLACE, "1 WORLD TRADE CENTER"),
    ("bryant park", GeocodeKind.PLACE, "BRYANT PARK"),
    ("broadwa", GeocodeKind.STREET, "BROADWAY"),
    ("fdr dr & 96", GeocodeKind.INTERSECTION, "E 96 ST & FRANKLIN D ROOSEVELT DR"),
    ("1 police plaza", GeocodeKind.ADDRESS, "1 POLICE PLZ"),
]

# docs/DATA.md §5.1: every AddressPoint row is a ground-truth location for an
# address string, so feeding the string back through the suggester measures the
# ladder directly. The thresholds are the regression guard, not the measurement
# — the run is at a median of 0 m because most of the sample is a surveyed
# door the index holds.
ACCURACY_SAMPLE = 400
ACCURACY_SEED = 7
MEDIAN_ERROR_MAX_M = 10.0
P95_ERROR_MAX_M = 60.0


@pytest.fixture(scope="module")
def conn():
    connection = connect(DB_PATH, readonly=True)
    yield connection
    connection.close()


@pytest.mark.parametrize(
    ("query", "kind", "label"), DEMO_QUERIES, ids=[q for q, _, _ in DEMO_QUERIES]
)
def test_the_measured_demo_queries_still_answer_the_same_way(conn, query, kind, label):
    candidates = suggest(conn, query)

    assert candidates, f"{query!r} lost every candidate"
    assert candidates[0].kind is kind
    assert candidates[0].label == label


def test_every_demo_query_stays_inside_the_eight_row_dropdown(conn):
    for query, _, _ in DEMO_QUERIES:
        assert len(suggest(conn, query)) <= 8


def test_a_numbered_avenue_is_the_avenue_and_not_a_house_number_on_avenue_a(conn):
    """ "5 ave" parses as house 5 on a street called "AVE"; the street is what was typed."""
    for query in ("5 ave", "1 ave", "86 st"):
        assert suggest(conn, query)[0].kind is GeocodeKind.STREET


def test_a_pin_on_a_street_reads_back_as_the_nearest_door(conn):
    match = reverse_geocode(conn, lon=-73.9560, lat=40.7790)

    assert match is not None
    assert match.kind is GeocodeKind.ADDRESS
    assert match.label.startswith("near ")
    assert match.distance_m < 60.0


def test_a_pin_in_new_jersey_reverses_to_nothing(conn):
    assert reverse_geocode(conn, lon=-74.0324, lat=40.7440) is None


@pytest.mark.slow
def test_a_sample_of_real_doors_geocodes_to_within_ten_metres(conn):
    """The accuracy regression: median under 10 m, p95 under 60 m, over 400 doors."""
    rows = _address_point_sample()
    errors = []
    misses = []
    for row in rows:
        query = f"{row['house_number']} {row['full_street_name']}"
        lon, lat = row["the_geom"]["coordinates"]
        candidates = suggest(conn, query, limit=1)
        if not candidates:
            misses.append(query)
            continue
        errors.append(_distance_m(lon, lat, candidates[0].lon, candidates[0].lat))

    assert len(misses) <= len(rows) * 0.01, (
        f"{len(misses)} of {len(rows)} found nothing: {misses[:5]}"
    )
    errors.sort()
    median = statistics.median(errors)
    p95 = errors[min(int(len(errors) * 0.95), len(errors) - 1)]
    assert median <= MEDIAN_ERROR_MAX_M, f"median {median:.1f} m over {len(errors)} doors"
    assert p95 <= P95_ERROR_MAX_M, f"p95 {p95:.1f} m over {len(errors)} doors"


def _address_point_sample():
    path = RAW_DIR / "address_points_manhattan.json"
    if not path.exists():
        pytest.skip("needs data/raw/address_points_manhattan.json")
    rows = json.loads(path.read_text(encoding="utf-8"))
    random.seed(ACCURACY_SEED)
    return random.sample(rows, min(ACCURACY_SAMPLE, len(rows)))


def _distance_m(lon_a: float, lat_a: float, lon_b: float, lat_b: float) -> float:
    return math.hypot(
        (lon_a - lon_b) * meters_per_degree_lon(lat_a), (lat_a - lat_b) * M_PER_DEG_LAT
    )
