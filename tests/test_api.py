"""The local API: contract, validation, security headers, and Range support.

Every test runs against a temporary SQLite file built with `create_schema` and
a handful of synthetic rows, so nothing here needs a real sync.
"""

from __future__ import annotations

import json
import sqlite3
import warnings
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

from curbcheck.api.app import CONTENT_SECURITY_POLICY, create_app
from curbcheck.api.routes import ASP_SUSPENSION_CAVEAT, TEMPORARY_SIGNAGE_CAVEAT
from curbcheck.db import create_schema, days_to_mask
from curbcheck.model import ALL_DAYS

# The block from docs/DATA.md §2.3: 3 AVE between E 85 ST and E 86 ST.
NODE_85 = (-73.9544835, 40.7781469)
NODE_86 = (-73.9539850, 40.7788304)
DESTINATION = {"lat": 40.7784, "lon": -73.9542}

# A sign description carrying markup, to prove the API neither sanitizes nor
# mangles it: the frontend is the escaping boundary (docs/API.md).
XSS_SIGN_TEXT = "2 HOUR PARKING <script>alert('x')</script> 9AM-7PM"

WINDOW = {"t1": "2026-09-15T09:00:00", "t2": "2026-09-15T11:00:00"}


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
def client(tmp_path: Path, web_dir: Path, basemap: Path) -> TestClient:
    db_path = tmp_path / "curbcheck.sqlite"
    _build_database(db_path)
    return TestClient(create_app(db_path, web_dir=web_dir, basemap_path=basemap))


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
        "rate_label",
    }
    assert set(result["signs"][0]) == {
        "sign_id",
        "order_number",
        "sign_code",
        "sign_description",
        "parse_method",
        "parse_confidence",
    }


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

    assert {sign["sign_description"] for sign in body["signs"]} == {
        XSS_SIGN_TEXT,
        "NO STANDING ANYTIME",
    }
    assert body["meter_rates"][0]["hour_rates"] == ["4.50", "5.50"]


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
    assert set(candidate) == {"label", "lat", "lon", "kind", "confidence"}


@pytest.mark.parametrize("q", ["", "x" * 201])
def test_out_of_range_geocode_queries_are_422(client, q):
    assert client.get("/api/geocode", params={"q": q}).status_code == 422


# --- health and sync status ----------------------------------------------


def test_health_reports_a_present_database(client):
    assert client.get("/api/health").json() == {
        "status": "ok",
        "db_present": True,
        "db_readonly": True,
        "sign_count": 2,
    }


def test_health_answers_without_a_database(empty_client):
    assert empty_client.get("/api/health").json() == {
        "status": "degraded",
        "db_present": False,
        "db_readonly": False,
        "sign_count": 0,
    }


def test_sync_status_returns_the_meta_table_as_strings(client):
    body = client.get("/api/sync-status").json()
    assert body == {
        "last_sync_at": "2026-09-14T22:05:11-04:00",
        "signs_loaded": "2",
        "blockface_sides_matched_share": "0.9378",
    }


@pytest.mark.parametrize(
    ("method", "path"),
    [("post", "/api/search"), ("get", "/api/segment/3681:W:0"), ("get", "/api/sync-status")],
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
    "path", ["/", "/index.html", "/vendor/pmtiles.js", "/api/health", "/api/nope", "/nope.html"]
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
