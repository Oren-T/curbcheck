"""The local API: contract, validation, security headers, and Range support.

Every test runs against a temporary SQLite file built with `create_schema` and
a handful of synthetic rows, so nothing here needs a real sync.
"""

from __future__ import annotations

import json
import sqlite3
import warnings
from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.exceptions import StarletteDeprecationWarning

with warnings.catch_warnings():
    # Importing Starlette's test client warns twice with the pinned versions:
    # it prefers the not-yet-pinned `httpx2` over `httpx`, and it touches an
    # anyio alias anyio has deprecated. pytest turns warnings into errors, so
    # both are silenced at this import rather than by adding a dependency or
    # loosening the project-wide warning filter. Neither affects the server.
    warnings.simplefilter("ignore", StarletteDeprecationWarning)
    warnings.simplefilter("ignore", DeprecationWarning)
    from fastapi.testclient import TestClient

from curbcheck.api import routes
from curbcheck.api.app import CONTENT_SECURITY_POLICY, create_app
from curbcheck.api.routes import ASP_SUSPENSION_CAVEAT, TEMPORARY_SIGNAGE_CAVEAT
from curbcheck.db import create_schema, days_to_mask
from curbcheck.engine.resolve import CALENDAR_MISSING_CAVEAT, NO_DATA_CAVEAT
from curbcheck.etl.addresses import build_address_index
from curbcheck.geocode import MAX_QUERY_CHARS
from curbcheck.model import ALL_DAYS

# The block from docs/DATA.md §2.3: 3 AVE between E 85 ST and E 86 ST.
NODE_85 = (-73.9544835, 40.7781469)
NODE_86 = (-73.9539850, 40.7788304)
DESTINATION = {"lat": 40.7784, "lon": -73.9542}

# A sign description carrying markup, to prove the API neither sanitizes nor
# mangles it: the frontend is the escaping boundary (docs/API.md).
XSS_SIGN_TEXT = "2 HOUR PARKING <script>alert('x')</script> 9AM-7PM"

WINDOW = {"t1": "2026-09-15T09:00:00", "t2": "2026-09-15T11:00:00"}

# The one surveyed door on the fixture block, halfway along it.
ADDRESS_1519 = (
    (NODE_85[0] + NODE_86[0]) / 2,
    (NODE_85[1] + NODE_86[1]) / 2,
)


def _geojson(start: tuple[float, float], end: tuple[float, float]) -> str:
    return json.dumps({"type": "LineString", "coordinates": [list(start), list(end)]})


def _build_database(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    create_schema(conn)

    geom = _geojson(NODE_85, NODE_86)
    bbox = (
        min(NODE_85[0], NODE_86[0]),
        min(NODE_85[1], NODE_86[1]),
        max(NODE_85[0], NODE_86[0]),
        max(NODE_85[1], NODE_86[1]),
    )
    conn.execute(
        "INSERT INTO street_segment (segment_id, street_name, street_norm, geom,"
        " min_lon, min_lat, max_lon, max_lat, left_low_address, left_high_address,"
        " right_low_address, right_high_address) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("3681", "3 AVE", "3 AVE", geom, *bbox, "1510", "1528", "1509", "1525"),
    )
    conn.execute(
        "INSERT INTO street_node (node_id, lon, lat, street_names) VALUES (?,?,?,?)",
        ("n85", NODE_85[0], NODE_85[1], json.dumps(["3 AVE", "E 85 ST"])),
    )
    # The suggester reads its own tables, so the fixture builds them the way a
    # sync would rather than hand-writing rows into them.
    build_address_index(
        conn,
        address_rows=[
            {
                "house_number": "1519",
                "full_street_name": "3 AVE",
                "zipcode": "10028",
                "the_geom": {"type": "Point", "coordinates": list(ADDRESS_1519)},
            }
        ],
        place_rows=[],
    )
    for sign_id, description in (("sign-1", XSS_SIGN_TEXT), ("sign-2", "NO STANDING ANYTIME")):
        conn.execute(
            "INSERT INTO sign (sign_id, order_number, on_street, from_street, to_street,"
            " side_of_street, distance_from_intersection, sign_code, sign_description,"
            " segment_id, snap_confidence, snap_notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                sign_id,
                "1-11111",
                "3 AVENUE",
                "EAST 85 STREET",
                "EAST 86 STREET",
                "W",
                44.0,
                "PRK-9",
                description,
                "3681",
                0.94,
                "",
            ),
        )
    conn.execute(
        "INSERT INTO regulation_segment (reg_seg_id, segment_id, side, start_ft, end_ft, geom,"
        " min_lon, min_lat, max_lon, max_lat, length_ft, capacity_cars, confidence, derived_from)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("3681:W:0", "3681", "W", 0.0, 284.8, geom, *bbox, 284.8, 12, 0.94, json.dumps(["sign-1"])),
    )
    conn.execute(
        "INSERT INTO regulation (reg_id, reg_seg_id, action, permitted, vehicle_class, exclusive,"
        " days_mask, time_from, time_to, metered, max_duration_min, flags, arrow,"
        " raw_sign_description, parse_method, parse_confidence)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "3681:W:0:1",
            "3681:W:0",
            "park",
            1,
            "all",
            0,
            days_to_mask(ALL_DAYS),
            "08:00",
            "19:00",
            0,
            240,
            "{}",
            "none",
            XSS_SIGN_TEXT,
            "grammar",
            0.98,
        ),
    )
    conn.execute(
        "INSERT INTO meter_rate (blockface_id, segment_id, side, rate_label, hour_rates,"
        " max_session_min) VALUES (?,?,?,?,?,?)",
        ("bf-1", "3681", "W", "Area 1", json.dumps(["4.50", "5.50"]), 120),
    )
    # One suspension date, so the database has a calendar: with none at all
    # /api/health is `degraded` and every result carries the missing-calendar
    # caveat, which is its own test below.
    conn.execute(
        "INSERT INTO asp_suspension (date, is_major_legal_holiday, meters_suspended, label)"
        " VALUES (?,?,?,?)",
        ("2026-12-25", 1, 1, "Christmas Day"),
    )
    for key, value in (
        ("last_sync_at", "2026-09-14T22:05:11-04:00"),
        ("signs_loaded", "2"),
        ("blockface_sides_matched_share", "0.9378"),
    ):
        conn.execute("INSERT INTO sync_meta (key, value) VALUES (?,?)", (key, value))
    conn.commit()
    conn.close()


@pytest.fixture
def web_dir(tmp_path: Path) -> Path:
    root = tmp_path / "web"
    (root / "vendor").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>CurbCheck</title>")
    (root / "vendor" / "pmtiles.js").write_text("export const x = 1;\n")
    return root


@pytest.fixture
def basemap(tmp_path: Path) -> Path:
    path = tmp_path / "manhattan.pmtiles"
    path.write_bytes(bytes(range(256)) * 8)
    return path


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "curbcheck.sqlite"
    _build_database(path)
    return path


@pytest.fixture
def client(db_path: Path, web_dir: Path, basemap: Path) -> TestClient:
    return TestClient(create_app(db_path, web_dir=web_dir, basemap_path=basemap))


@pytest.fixture
def served_client(db_path: Path, web_dir: Path, basemap: Path) -> Iterator[TestClient]:
    """`client`, entered as a context manager, which is what a running server is.

    Entered, the client holds one portal and one worker-thread pool for every
    request, the way uvicorn does. The plain fixture builds a portal per
    request, so anything about state kept *between* requests has to be asked
    of this one.
    """
    with TestClient(create_app(db_path, web_dir=web_dir, basemap_path=basemap)) as entered:
        yield entered


@pytest.fixture
def empty_client(tmp_path: Path, web_dir: Path) -> TestClient:
    """A server whose database was never built, i.e. before the first sync."""
    return TestClient(create_app(tmp_path / "missing.sqlite", web_dir=web_dir, basemap_path=None))


# --- search --------------------------------------------------------------


def test_search_returns_results_with_the_disclaimer_and_caveats(client):
    response = client.post("/api/search", json={**DESTINATION, **WINDOW, "walk_minutes": 10})
    assert response.status_code == 200
    body = response.json()

    assert body["destination"]["lat"] == DESTINATION["lat"]
    assert "advisory only" in body["disclaimer"]
    assert "Always read the posted sign" in body["disclaimer"]
    assert body["caveats"] == [TEMPORARY_SIGNAGE_CAVEAT, ASP_SUSPENSION_CAVEAT]
    assert body["sync"] == {
        "last_sync": "2026-09-14T22:05:11-04:00",
        "sign_count": 2,
        "coverage_pct": pytest.approx(93.78),
    }

    [result] = body["results"]
    assert result["reg_seg_id"] == "3681:W:0"
    assert result["verdict"] == "legal"
    assert result["geometry"]["type"] == "LineString"
    assert TEMPORARY_SIGNAGE_CAVEAT in result["caveats"]


def test_search_result_mirrors_the_search_result_fields(client):
    [result] = client.post("/api/search", json={**DESTINATION, **WINDOW}).json()["results"]
    assert set(result) == {
        "reg_seg_id",
        "geometry",
        "verdict",
        "reason",
        "caveats",
        "walk_min",
        "money",
        "price_known",
        "capacity_cars",
        "confidence",
        "signs",
        "charged_minutes",
        "metered",
        "score",
        "money_value",
        "risk",
        "basis",
        "confidence_shown",
        "rate_label",
        "street_name",
        "gap_kind",
    }
    assert set(result["signs"][0]) == {
        "sign_id",
        "order_number",
        "sign_code",
        "sign_description",
        "parse_method",
        "parse_confidence",
    }


def test_search_reports_counts_taken_before_the_caps(client):
    """docs/VALIDATION.md U1: the status line must describe the query, not the response."""
    body = client.post("/api/search", json={**DESTINATION, **WINDOW, "limit": 1}).json()

    assert body["counts"] == {
        "legal": 1,
        "illegal": 0,
        "ambiguous": 0,
        "no_data": 0,
        "total": 1,
    }


def test_a_small_limit_keeps_the_other_verdicts_in_the_response(client, tmp_path):
    """`limit` caps the ranked legal list; every other verdict still reaches the map."""
    conn = sqlite3.connect(tmp_path / "curbcheck.sqlite")
    conn.execute(
        "INSERT INTO regulation_segment (reg_seg_id, segment_id, side, start_ft, end_ft, geom,"
        " min_lon, min_lat, max_lon, max_lat, length_ft, capacity_cars, confidence, derived_from)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "3681:E:0",
            "3681",
            "E",
            0.0,
            284.8,
            _geojson(NODE_85, NODE_86),
            min(NODE_85[0], NODE_86[0]),
            min(NODE_85[1], NODE_86[1]),
            max(NODE_85[0], NODE_86[0]),
            max(NODE_85[1], NODE_86[1]),
            284.8,
            12,
            0.94,
            json.dumps(["sign-2"]),
        ),
    )
    conn.execute(
        "INSERT INTO regulation (reg_id, reg_seg_id, action, permitted, vehicle_class, exclusive,"
        " days_mask, metered, flags, arrow, raw_sign_description, parse_method, parse_confidence)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "3681:E:0:1",
            "3681:E:0",
            "stand",
            0,
            "all",
            0,
            days_to_mask(ALL_DAYS),
            0,
            "{}",
            "none",
            "NO STANDING ANYTIME",
            "grammar",
            0.98,
        ),
    )
    conn.commit()
    conn.close()

    body = client.post("/api/search", json={**DESTINATION, **WINDOW, "limit": 1}).json()

    assert [result["verdict"] for result in body["results"]] == ["legal", "illegal"]
    assert body["counts"] == {
        "legal": 1,
        "illegal": 1,
        "ambiguous": 0,
        "no_data": 0,
        "total": 2,
    }


def test_the_dev_mock_data_answers_the_same_shape_as_the_server(client):
    """`scripts/dev_mock_data.json` is the frontend's other statement of the contract.

    A stale one teaches the page to expect fields the server no longer sends,
    which is invisible until someone opens the mock server.
    """
    mock = json.loads(
        (Path(__file__).resolve().parents[1] / "scripts" / "dev_mock_data.json").read_text(
            encoding="utf-8"
        )
    )
    live = client.post("/api/search", json={**DESTINATION, **WINDOW}).json()

    assert set(mock["search"]) == set(live)
    assert set(mock["search"]["counts"]) == set(live["counts"])
    for result in mock["search"]["results"]:
        assert set(result) == set(live["results"][0])
    assert set(mock["health"]) == set(client.get("/api/health").json())


def test_money_is_a_string_not_a_float(client):
    [result] = client.post("/api/search", json={**DESTINATION, **WINDOW}).json()["results"]
    assert isinstance(result["money"], str)


def test_search_by_address_geocodes_locally(client):
    body = client.post("/api/search", json={"address": "1519 3rd Ave", **WINDOW}).json()
    assert body["destination"]["label"] == "1519 3 AVE"
    assert body["results"]


def test_unknown_address_is_a_404_not_a_crash(client):
    response = client.post("/api/search", json={"address": "999 NOWHERE BLVD", **WINDOW})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "address_not_found"


def test_naive_times_are_read_as_new_york_local_time(client):
    """A naive 09:00 and an explicit 09:00-04:00 must produce the same answer."""
    naive = client.post("/api/search", json={**DESTINATION, **WINDOW}).json()
    aware = client.post(
        "/api/search",
        json={**DESTINATION, "t1": "2026-09-15T09:00:00-04:00", "t2": "2026-09-15T11:00:00-04:00"},
    ).json()
    assert naive["results"] == aware["results"]


def test_the_search_result_says_how_its_legal_verdict_was_reached(client):
    [result] = client.post("/api/search", json={**DESTINATION, **WINDOW}).json()["results"]

    assert result["basis"] == "posted"
    assert result["confidence_shown"] is True
    assert result["money_value"] == pytest.approx(float(result["money"]))
    # snap confidence 0.94 is the weaker of the two: 0.06 doubt * 0.5 * $65.
    assert result["risk"] == pytest.approx(1.95)


def test_a_verdict_legal_only_by_absence_hides_its_confidence(client):
    """Absence of a rule is not a confident permission (UX audit P0-1)."""
    body = client.post(
        "/api/search",
        json={**DESTINATION, "t1": "2026-09-15T20:00:00", "t2": "2026-09-15T22:00:00"},
    ).json()

    [result] = body["results"]
    assert result["verdict"] == "legal"
    assert result["basis"] == "absence"
    assert result["reason"] == "No posted rule is in effect during this window"
    assert result["confidence_shown"] is False


def test_a_grey_span_says_which_kind_of_nothing_it_is(client, tmp_path):
    """The engine, not the client, decides the sentence: UX verification open item 10."""
    conn = sqlite3.connect(tmp_path / "curbcheck.sqlite")
    for reg_seg_id, side, gap_kind in (
        ("3681:E:gap", "E", "no_signs"),
        ("3681:W:gap", "W", "unmatched_signs"),
    ):
        conn.execute(
            "INSERT INTO regulation_segment (reg_seg_id, segment_id, side, geom, min_lon, min_lat,"
            " max_lon, max_lat, confidence, derived_from, gap_kind) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                reg_seg_id,
                "3681",
                side,
                _geojson(NODE_85, NODE_86),
                min(NODE_85[0], NODE_86[0]),
                min(NODE_85[1], NODE_86[1]),
                max(NODE_85[0], NODE_86[0]),
                max(NODE_85[1], NODE_86[1]),
                0.0,
                "[]",
                gap_kind,
            ),
        )
    conn.commit()
    conn.close()

    body = client.post("/api/search", json={**DESTINATION, **WINDOW}).json()
    grey = {result["gap_kind"]: result for result in body["results"] if result["gap_kind"]}

    assert grey["no_signs"]["reason"] == "NYC DOT lists no signs on this stretch"
    assert grey["no_signs"]["caveats"][0] == NO_DATA_CAVEAT["no_signs"]
    assert grey["unmatched_signs"]["reason"] == "Signs exist here that CurbCheck could not place"
    assert grey["unmatched_signs"]["caveats"][0] == NO_DATA_CAVEAT["unmatched_signs"]
    # The sentence the frontend had to override because it was false here.
    assert "no sign data" not in grey["unmatched_signs"]["reason"].lower()


def test_every_caveat_is_a_sentence(client):
    """They are printed verbatim under a sentence-case heading (UX verification item 13)."""
    # The 20:00 window is legal by absence, which is the caveat the audit caught
    # lower-cased at the head of "Before you park".
    body = client.post(
        "/api/search",
        json={**DESTINATION, "t1": "2026-09-15T20:00:00", "t2": "2026-09-15T22:00:00"},
    ).json()
    caveats = [caveat for result in body["results"] for caveat in result["caveats"]]

    assert "No posted rule is in effect during this window; read the curb." in caveats
    for caveat in [*caveats, *body["caveats"]]:
        assert caveat[0].isupper(), caveat
        assert caveat.endswith("."), caveat


def test_a_no_data_span_reports_no_price_and_no_confidence(client, tmp_path):
    """Never "$0.00" and never "no meter" on curb the app knows nothing about (P0-5)."""
    conn = sqlite3.connect(tmp_path / "curbcheck.sqlite")
    conn.execute(
        "INSERT INTO regulation_segment (reg_seg_id, segment_id, side, geom, min_lon, min_lat,"
        " max_lon, max_lat, confidence, derived_from, gap_kind) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "3681:E:gap",
            "3681",
            "E",
            _geojson(NODE_85, NODE_86),
            min(NODE_85[0], NODE_86[0]),
            min(NODE_85[1], NODE_86[1]),
            max(NODE_85[0], NODE_86[0]),
            max(NODE_85[1], NODE_86[1]),
            0.0,
            "[]",
            "no_signs",
        ),
    )
    conn.commit()
    conn.close()

    body = client.post("/api/search", json={**DESTINATION, **WINDOW}).json()
    grey = next(result for result in body["results"] if result["verdict"] == "no_data")

    assert grey["money"] is None
    assert grey["money_value"] is None
    assert grey["price_known"] is False
    assert grey["rate_label"] is None
    assert grey["basis"] is None
    assert grey["confidence_shown"] is False


# --- coverage ------------------------------------------------------------


def test_a_destination_outside_coverage_is_refused_not_answered_empty(client):
    """A pin in New Jersey is a question this build cannot answer (UX audit P0-3)."""
    response = client.post(
        "/api/search", json={"lat": 40.8600, "lon": -73.9000, **WINDOW, "walk_minutes": 30}
    )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "outside_coverage"
    assert "Manhattan" in error["message"]


def test_a_destination_inside_coverage_still_answers(client):
    assert client.post("/api/search", json={**DESTINATION, **WINDOW}).status_code == 200


def test_health_reports_the_coverage_area_and_its_box(client):
    coverage = client.get("/api/health").json()["coverage"]

    assert coverage["area"] == "Manhattan"
    min_lon, min_lat, max_lon, max_lat = coverage["bbox"]
    assert min_lon < max_lon and min_lat < max_lat
    assert min_lon <= DESTINATION["lon"] <= max_lon


# --- reverse -------------------------------------------------------------


def test_reverse_names_the_place_a_pin_landed_on(client):
    body = client.get("/api/reverse", params={"lat": 40.77849, "lon": -73.95424}).json()

    assert set(body) == {"label", "secondary", "kind", "lat", "lon", "distance_m"}
    assert body["kind"] in ("address", "intersection", "street")
    assert body["label"]
    assert body["distance_m"] >= 0


def test_reverse_outside_coverage_is_422(client):
    response = client.get("/api/reverse", params={"lat": 40.8600, "lon": -73.9000})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "outside_coverage"


@pytest.mark.parametrize(
    ("params", "reason"),
    [
        ({"lat": 40.7784}, "lon missing"),
        ({"lon": -73.9542}, "lat missing"),
        ({"lat": 51.5, "lon": -73.9542}, "lat outside the bbox"),
        ({"lat": 40.7784, "lon": -80.0}, "lon outside the bbox"),
        ({"lat": "nope", "lon": -73.9542}, "lat not a number"),
        ({"lat": "nan", "lon": -73.9542}, "lat is NaN"),
        ({"lat": "inf", "lon": -73.9542}, "lat is infinite"),
    ],
)
def test_bad_reverse_parameters_are_422_with_an_error_envelope(client, params, reason):
    response = client.get("/api/reverse", params=params)

    assert response.status_code == 422, reason
    assert response.json()["error"]["code"] == "validation_error"


# --- validation ----------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ({**DESTINATION, **WINDOW, "surprise": 1}, "unknown field"),
        ({**DESTINATION, "t1": "2026-09-15T11:00:00", "t2": "2026-09-15T09:00:00"}, "t2 before t1"),
        ({**DESTINATION, "t1": "2026-09-15T09:00:00", "t2": "2026-09-15T09:00:00"}, "zero length"),
        ({**DESTINATION, "t1": "2026-09-15T09:00:00", "t2": "2026-09-17T09:00:00"}, "over 24 h"),
        ({"lat": 40.7784, "lon": -80.0, **WINDOW}, "lon outside the bbox"),
        ({"lat": 51.5, "lon": -73.95, **WINDOW}, "lat outside the bbox"),
        ({"lat": 40.7784, **WINDOW}, "lat without lon"),
        ({**DESTINATION, "address": "1519 3 Ave", **WINDOW}, "both point and address"),
        ({**WINDOW}, "no destination"),
        ({**DESTINATION, **WINDOW, "walk_minutes": 0}, "walk_minutes under 1"),
        ({**DESTINATION, **WINDOW, "walk_minutes": 31}, "walk_minutes over 30"),
        ({**DESTINATION, **WINDOW, "weights": {"walk": 11}}, "weight over 10"),
        ({**DESTINATION, **WINDOW, "weights": {"walk": -1}}, "negative weight"),
        ({**DESTINATION, **WINDOW, "weights": {"speed": 1}}, "unknown weight"),
        ({**DESTINATION, **WINDOW, "limit": 0}, "limit under 1"),
        ({**DESTINATION, "t1": "not a time", "t2": "2026-09-15T11:00:00"}, "unparseable time"),
    ],
)
def test_bad_search_bodies_are_422_with_an_error_envelope(client, body, reason):
    response = client.post("/api/search", json=body)
    assert response.status_code == 422, reason
    assert response.json()["error"]["code"] == "validation_error"
    assert isinstance(response.json()["error"]["message"], str)


def test_validation_errors_do_not_leak_paths_or_tracebacks(client):
    message = client.post("/api/search", json={"surprise": 1}).json()["error"]["message"]
    assert "Traceback" not in message
    assert "/workspace" not in message


# --- segment detail ------------------------------------------------------


def test_segment_returns_the_stack_the_signs_and_the_rates(client):
    body = client.get("/api/segment/3681:W:0").json()
    assert body["segment"]["street_name"] == "3 AVE"
    assert body["segment"]["capacity_cars"] == 12
    assert body["geometry"]["type"] == "LineString"

    [regulation] = body["regulations"]
    assert regulation["parse_method"] == "grammar"
    assert regulation["parse_confidence"] == 0.98
    assert regulation["raw_sign_description"] == XSS_SIGN_TEXT
    assert regulation["regulation"]["max_duration_min"] == 240
    # The rule names the post it was read from, so the UI can file it under it.
    assert regulation["sign_id"] == "sign-1"

    assert [sign["sign_description"] for sign in body["governing"]] == [XSS_SIGN_TEXT]
    assert [sign["sign_description"] for sign in body["other_on_block"]] == ["NO STANDING ANYTIME"]
    assert body["meter_rates"][0]["hour_rates"] == ["4.50", "5.50"]


def test_segment_signs_carry_the_distance_and_the_arrow(client):
    [sign] = client.get("/api/segment/3681:W:0").json()["governing"]

    assert sign["distance_ft"] == 44.0
    assert sign["distance_ft"] == sign["distance_from_intersection"]
    assert sign["arrow"] is None
    assert sign["side_of_street"] == "W"
    assert sign["panel_class"] == "regulation"


def test_governing_signs_come_back_in_curb_order(client, tmp_path):
    """SPEC §10's sign list is read standing on the pavement, not sorted by id."""
    conn = sqlite3.connect(tmp_path / "curbcheck.sqlite")
    conn.execute(
        "INSERT INTO sign (sign_id, on_street, from_street, to_street, side_of_street,"
        " distance_from_intersection, sign_description, arrow_direction, segment_id)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "aaa-first-by-id",
            "3 AVENUE",
            "EAST 85 STREET",
            "EAST 86 STREET",
            "W",
            120.0,
            "2 HOUR PARKING 9AM-7PM",
            "N",
            "3681",
        ),
    )
    conn.execute(
        "UPDATE regulation_segment SET derived_from = ? WHERE reg_seg_id = ?",
        (json.dumps(["sign-1", "aaa-first-by-id"]), "3681:W:0"),
    )
    conn.commit()
    conn.close()

    governing = client.get("/api/segment/3681:W:0").json()["governing"]

    assert [sign["distance_ft"] for sign in governing] == [44.0, 120.0]
    assert governing[1]["arrow"] == "N"


def test_a_placeholder_span_has_no_governing_signs(client, tmp_path):
    """A grey span asserts nothing: no sign of its own, and the block's signs beside it."""
    conn = sqlite3.connect(tmp_path / "curbcheck.sqlite")
    conn.execute(
        "INSERT INTO regulation_segment (reg_seg_id, segment_id, side, geom, min_lon, min_lat,"
        " max_lon, max_lat, confidence, derived_from, gap_kind) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "3681:W:gap",
            "3681",
            "W",
            _geojson(NODE_85, NODE_86),
            min(NODE_85[0], NODE_86[0]),
            min(NODE_85[1], NODE_86[1]),
            max(NODE_85[0], NODE_86[0]),
            max(NODE_85[1], NODE_86[1]),
            0.0,
            json.dumps(["sign-1"]),
            "no_signs",
        ),
    )
    conn.commit()
    conn.close()

    body = client.get("/api/segment/3681:W:gap").json()

    assert body["segment"]["gap_kind"] == "no_signs"
    assert body["governing"] == []
    assert {sign["sign_description"] for sign in body["other_on_block"]} == {
        XSS_SIGN_TEXT,
        "NO STANDING ANYTIME",
    }


def test_an_unmatched_placeholder_lists_the_signs_that_never_snapped(client, tmp_path):
    """332 of 499 unmatched blockface-sides carry a NO STANDING sign (VALIDATION §5)."""
    conn = sqlite3.connect(tmp_path / "curbcheck.sqlite")
    conn.execute(
        "INSERT INTO street_node (node_id, lon, lat, street_names) VALUES (?,?,?,?)",
        ("n86", NODE_86[0], NODE_86[1], json.dumps(["3 AVE", "E 86 ST"])),
    )
    conn.execute(
        "UPDATE street_segment SET from_node = 'n85', to_node = 'n86' WHERE segment_id = '3681'"
    )
    conn.execute(
        "INSERT INTO sign (sign_id, on_street, from_street, to_street, side_of_street,"
        " distance_from_intersection, sign_description, segment_id, snap_notes)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "sign-unplaced",
            "3 AVENUE",
            "EAST 85 STREET",
            "EAST 86 STREET",
            "E",
            10.0,
            "NO STANDING ANYTIME",
            None,
            "unmatched: no_geometry",
        ),
    )
    conn.execute(
        "INSERT INTO regulation_segment (reg_seg_id, segment_id, side, geom, min_lon, min_lat,"
        " max_lon, max_lat, confidence, derived_from, gap_kind) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "3681:E:gap",
            "3681",
            "E",
            _geojson(NODE_85, NODE_86),
            min(NODE_85[0], NODE_86[0]),
            min(NODE_85[1], NODE_86[1]),
            max(NODE_85[0], NODE_86[0]),
            max(NODE_85[1], NODE_86[1]),
            0.0,
            "[]",
            "unmatched_signs",
        ),
    )
    conn.commit()
    conn.close()

    body = client.get("/api/segment/3681:E:gap").json()

    assert body["governing"] == []
    assert [sign["sign_id"] for sign in body["other_on_block"]] == ["sign-unplaced"]


def test_unknown_segment_id_is_404(client):
    response = client.get("/api/segment/3681:W:999")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize(
    "garbage",
    [
        "'%20OR%201=1",
        "a'b",
        "3681:W:0;DROP%20TABLE%20sign",
        "%3Cscript%3E",
        "x" * 200,
        "..%2F..%2Fetc%2Fpasswd",
    ],
)
def test_garbage_segment_ids_are_rejected_without_touching_the_database(tmp_path, web_dir, garbage):
    """The id is validated before a connection is opened, so a missing DB still 400s."""
    client = TestClient(create_app(tmp_path / "missing.sqlite", web_dir=web_dir, basemap_path=None))
    response = client.get(f"/api/segment/{garbage}")
    assert response.status_code in (400, 404)
    assert response.json()["error"]["code"] in ("invalid_request", "not_found")


# --- untrusted text ------------------------------------------------------


def test_sign_text_with_markup_comes_back_json_escaped_and_untouched(client):
    [result] = client.post("/api/search", json={**DESTINATION, **WINDOW}).json()["results"]
    assert result["signs"][0]["sign_description"] == XSS_SIGN_TEXT

    # JSON-escaped on the wire, never HTML-escaped: the API does not sanitize.
    raw = client.get("/api/segment/3681:W:0").text
    assert "<script>alert('x')<\\/script>" in raw or "<script>alert('x')</script>" in raw
    assert "&lt;script&gt;" not in raw


@pytest.mark.parametrize(
    "query",
    [
        "<script>alert(1)</script>",
        "=1+1",
        "+SUM(A1:A9)",
        "@import",
        "-2+3",
        "'; DROP TABLE sign; --",
        "%",
        "_",
        "\\",
        "\x00",
        "1519 3rd Ave' OR '1'='1",
    ],
)
def test_hostile_geocode_queries_never_raise(client, query):
    response = client.get("/api/geocode", params={"q": query})
    assert response.status_code == 200
    assert isinstance(response.json()["candidates"], list)


def test_geocode_returns_candidates_for_a_real_address(client):
    body = client.get("/api/geocode", params={"q": "1519 3rd Ave"}).json()
    assert body["query"] == "1519 3rd Ave"
    [candidate] = body["candidates"]
    assert candidate["kind"] == "address"
    assert set(candidate) == {"label", "lat", "lon", "kind", "confidence", "secondary"}
    assert candidate["secondary"] == "Manhattan 10028"


@pytest.mark.parametrize("q", ["", "x" * (MAX_QUERY_CHARS + 1)])
def test_out_of_range_geocode_queries_are_422(client, q):
    assert client.get("/api/geocode", params={"q": q}).status_code == 422


def test_geocode_offers_a_street_when_the_house_number_cannot_be_placed(client):
    body = client.get("/api/geocode", params={"q": "9999 3 Ave"}).json()

    assert body["candidates"][0]["kind"] == "address"
    assert body["candidates"][0]["label"] == "near 1519 3 AVE"


def test_geocode_never_returns_more_than_eight_candidates(client):
    for query in ("3 Ave", "1519 3rd Ave", "3 Ave & E 85 St", "10028"):
        assert len(client.get("/api/geocode", params={"q": query}).json()["candidates"]) <= 8


def test_geocode_is_never_cached(client):
    """A typed address must not sit in a proxy or a disk cache (threat T6)."""
    response = client.get("/api/geocode", params={"q": "1519 3rd Ave"})

    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Content-Type"].startswith("application/json")


def test_reverse_reads_a_pin_back_as_the_nearest_door(client):
    body = client.get(
        "/api/reverse", params={"lat": ADDRESS_1519[1], "lon": ADDRESS_1519[0]}
    ).json()

    assert body["kind"] == "address"
    assert body["label"] == "near 1519 3 AVE"
    assert body["distance_m"] == 0.0


# --- health and sync status ----------------------------------------------


def test_health_reports_a_present_database(client):
    assert client.get("/api/health").json() == {
        "status": "ok",
        "db_present": True,
        "db_readonly": True,
        "sign_count": 2,
        "calendar_missing": False,
        "coverage": {
            "area": "Manhattan",
            "bbox": [
                min(NODE_85[0], NODE_86[0]),
                min(NODE_85[1], NODE_86[1]),
                max(NODE_85[0], NODE_86[0]),
                max(NODE_85[1], NODE_86[1]),
            ],
        },
    }


def test_health_answers_without_a_database(empty_client):
    assert empty_client.get("/api/health").json() == {
        "status": "degraded",
        "db_present": False,
        "db_readonly": False,
        "sign_count": 0,
        "calendar_missing": True,
        "coverage": None,
    }


def test_a_database_with_no_calendar_is_degraded(client, tmp_path):
    """A sync that dropped the calendar answers every query and gets holidays wrong."""
    conn = sqlite3.connect(tmp_path / "curbcheck.sqlite")
    conn.execute("DELETE FROM asp_suspension")
    conn.commit()
    conn.close()

    health = client.get("/api/health").json()

    assert health["status"] == "degraded"
    assert health["calendar_missing"] is True
    assert health["sign_count"] == 2


def test_every_result_carries_the_caveat_when_the_calendar_is_missing(client, tmp_path):
    conn = sqlite3.connect(tmp_path / "curbcheck.sqlite")
    conn.execute("DELETE FROM asp_suspension")
    conn.commit()
    conn.close()

    body = client.post("/api/search", json={**DESTINATION, **WINDOW}).json()

    assert body["results"]
    for result in body["results"]:
        assert CALENDAR_MISSING_CAVEAT in result["caveats"]


def _rebuilt_database(source: Path, destination: Path, last_sync_at: str) -> None:
    """Build a second database and rename it over `destination`, as `db.swap_in` does."""
    _build_database(source)
    conn = sqlite3.connect(source)
    conn.execute("UPDATE sync_meta SET value = ? WHERE key = 'last_sync_at'", (last_sync_at,))
    conn.commit()
    conn.close()
    source.replace(destination)


def test_the_database_is_opened_once_and_the_connection_reused(served_client, monkeypatch):
    """Per-request opens start with an empty page cache, which is most of a geocode."""
    opened = []
    real_connect = routes.connect

    def counting_connect(path, *, readonly=False):
        opened.append(path)
        return real_connect(path, readonly=readonly)

    monkeypatch.setattr(routes, "connect", counting_connect)
    for _ in range(3):
        assert served_client.get("/api/sync-status").status_code == 200

    assert len(opened) == 1


def test_a_database_swapped_in_under_the_server_is_picked_up(served_client, db_path, tmp_path):
    """`curbcheck sync` renames a fresh file over the old one while the server runs."""
    assert (
        served_client.get("/api/sync-status").json()["last_sync_at"] == "2026-09-14T22:05:11-04:00"
    )

    _rebuilt_database(tmp_path / "rebuilt.sqlite", db_path, "2026-09-16T08:00:00-04:00")

    assert (
        served_client.get("/api/sync-status").json()["last_sync_at"] == "2026-09-16T08:00:00-04:00"
    )


def test_sync_status_returns_the_meta_table_as_strings(client):
    body = client.get("/api/sync-status").json()
    assert body == {
        "last_sync_at": "2026-09-14T22:05:11-04:00",
        "signs_loaded": "2",
        "blockface_sides_matched_share": "0.9378",
    }


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/api/search"),
        ("get", "/api/segment/3681:W:0"),
        ("get", "/api/sync-status"),
        ("get", "/api/reverse?lat=40.7784&lon=-73.9542"),
    ],
)
def test_a_missing_database_is_503_with_the_sync_instruction(empty_client, method, path):
    body = {**DESTINATION, **WINDOW} if method == "post" else None
    response = empty_client.request(method.upper(), path, json=body)
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "database_unavailable"
    assert "curbcheck sync" in error["message"]


def test_unknown_api_path_is_a_json_404(client):
    response = client.get("/api/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# --- security headers ----------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/index.html",
        "/vendor/pmtiles.js",
        "/api/health",
        "/api/nope",
        "/nope.html",
        "/api/reverse?lat=40.7784&lon=-73.9542",
        "/api/reverse?lat=999&lon=-73.9542",
    ],
)
def test_security_headers_are_on_every_response(client, path):
    response = client.get(path)
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_the_csp_is_the_spec_string_plus_worker_src(client):
    assert client.get("/api/health").headers["content-security-policy"] == (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; connect-src 'self'; worker-src 'self' blob:; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    )


def test_api_responses_are_never_cached(client):
    assert client.get("/api/health").headers["cache-control"] == "no-store"


def test_static_responses_are_not_forced_no_store(client):
    assert client.get("/vendor/pmtiles.js").headers.get("cache-control") != "no-store"


@pytest.mark.parametrize("path", ["/", "/api/health", "/api/search"])
def test_no_cors_header_is_ever_sent(client, path):
    response = client.get(path, headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in response.headers


def test_index_html_is_served_at_the_root(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "CurbCheck" in response.text


def test_the_404_page_still_carries_the_headers(client):
    response = client.get("/definitely-not-here")
    assert response.status_code == 404
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY


# --- basemap Range support -----------------------------------------------


def test_basemap_serves_the_whole_file_and_advertises_ranges(client, basemap):
    response = client.get("/basemap/manhattan.pmtiles")
    assert response.status_code == 200
    assert response.content == basemap.read_bytes()
    assert response.headers["accept-ranges"] == "bytes"


def test_basemap_range_request_returns_206_with_the_right_bytes(client, basemap):
    """pmtiles.js reads the archive header and each tile as a byte range."""
    expected = basemap.read_bytes()
    response = client.get("/basemap/manhattan.pmtiles", headers={"Range": "bytes=100-226"})
    assert response.status_code == 206
    assert response.content == expected[100:227]
    assert response.headers["content-range"] == f"bytes 100-226/{len(expected)}"
    assert response.headers["content-length"] == "127"


def test_basemap_range_beyond_the_file_is_416(client, basemap):
    response = client.get(
        "/basemap/manhattan.pmtiles", headers={"Range": f"bytes={len(basemap.read_bytes())}-"}
    )
    assert response.status_code == 416


def test_basemap_range_response_still_carries_the_headers(client):
    response = client.get("/basemap/manhattan.pmtiles", headers={"Range": "bytes=0-16"})
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert response.headers["x-content-type-options"] == "nosniff"


def test_missing_basemap_is_a_json_404(empty_client):
    response = empty_client.get("/basemap/manhattan.pmtiles")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
