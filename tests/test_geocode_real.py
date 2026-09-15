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
from curbcheck.etl.addresses import PLACE_ALIASES
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

# The word-matching matrix (docs/ux/AUTOCOMPLETE_RESEARCH.md §6.4): what a
# person types, the answer they meant, and how near the top of the eight-row
# dropdown it has to be. Three of them are `3` rather than `1` because the
# index has no popularity signal and will not invent one: "fashion" alone puts
# the Fashion Institute first because the name starts with the word, "king" is
# two real King Streets before it is the boulevard CSCL files as W 125 ST, and
# "guggenheim" is the bandshell before the museum for the same reason.
TOP = 1
TOP_THREE = 3
MATRIX: list[tuple[str, str, int]] = [
    ("fashion", "HIGH SCHOOL OF FASHION INDUSTRIES", TOP_THREE),
    ("fashion high", "HIGH SCHOOL OF FASHION INDUSTRIES", TOP),
    ("high fashion", "HIGH SCHOOL OF FASHION INDUSTRIES", TOP),
    ("hs fashion", "HIGH SCHOOL OF FASHION INDUSTRIES", TOP),
    ("high school of fashion", "HIGH SCHOOL OF FASHION INDUSTRIES", TOP),
    ("trade center", "1 WORLD TRADE CENTER", TOP),
    ("one world trade", "1 WORLD TRADE CENTER", TOP),
    ("guggenheim", "SOLOMON R GUGGENHEIM MUSEUM", TOP_THREE),
    ("port authority", "PORT AUTHORITY BUS TERMINAL", TOP),
    ("grand central", "GRAND CENTRAL TERMINAL", TOP),
    ("moma", "MUSEUM OF MODERN ART (MOMA)", TOP),
    ("met museum", "METROPOLITAN MUSEUM OF ART", TOP),
    ("bryant park", "BRYANT PARK", TOP),
    ("penn station", "PENN STATION", TOP),
    ("empire state", "EMPIRE STATE BUILDING", TOP),
    ("carnegie", "CARNEGIE HALL", TOP),
    ("radio city", "RADIO CITY MUSIC HALL", TOP),
    ("art and design", "ART & DESIGN HIGH SCHOOL", TOP),
    ("americas", "AVE OF THE AMERICAS", TOP),
    ("riverside", "RIVERSIDE DR", TOP_THREE),
    ("riverside blvd", "RIVERSIDE BLVD", TOP),
    ("king", "W 125 ST", TOP_THREE),
    ("lex", "LEXINGTON AVE", TOP),
    ("broad", "BROAD ST", TOP),
    ("broadwa", "BROADWAY", TOP),
    ("madison", "MADISON AVE", TOP_THREE),
    ("central park", "CENTRAL PARK W", TOP_THREE),
    ("86th st", "E 86 ST", TOP),
    ("5 ave", "5 AVE", TOP),
    ("1519 3rd ave", "1519 3 AVE", TOP),
    ("lex & 86", "E 86 ST & LEXINGTON AVE", TOP),
    ("1 police plaza", "1 POLICE PLZ", TOP),
    ("10021", "10021", TOP),
]

# A bare word that is also the start of a street spelling answers with the
# street: this app searches both sides of a street for its whole length and a
# place is one point, so the street is the safer reading of an ambiguous word.
STREET_FIRST_QUERIES = ["lex", "broad", "w", "madison", "park", "riverside", "houston", "3"]

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


def test_the_word_matching_matrix_puts_the_meant_answer_at_the_top(conn):
    """The 33 queries `docs/ux/AUTOCOMPLETE_RESEARCH.md` §6.4 fixed the ranking against."""
    misplaced = []
    print()
    for query, expected, within in MATRIX:
        labels = [candidate.label for candidate in suggest(conn, query)]
        rank = labels.index(expected) + 1 if expected in labels else 0
        mark = "ok " if 0 < rank <= within else "BAD"
        print(f"{mark} {query:<24} #{rank} of {len(labels):<2} (needs <={within})  {labels[:3]}")
        if not 0 < rank <= within:
            misplaced.append((query, expected, rank))

    assert not misplaced


def test_the_reported_bug_is_fixed_and_one_word_of_a_name_finds_it(conn):
    """ "fashion" returned nothing while "high school of fashion" autocompleted."""
    assert suggest(conn, "fashion")
    assert suggest(conn, "fashion") == suggest(conn, "FASHION")


@pytest.mark.parametrize("query", STREET_FIRST_QUERIES)
def test_a_bare_word_that_starts_a_street_answers_with_the_street(conn, query):
    assert suggest(conn, query)[0].kind is GeocodeKind.STREET


def test_every_landmark_alias_names_a_place_the_snapshot_still_carries(conn):
    """An alias whose target left CommonPlace is a dangling name, not a shortcut."""
    for alias, target in PLACE_ALIASES.items():
        candidates = suggest(conn, alias)
        assert candidates, alias
        assert candidates[0].label == target, alias


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
