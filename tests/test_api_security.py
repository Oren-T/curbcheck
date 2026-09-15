"""Adversarial tests against the local server (threats T3 and T4).

`tests/test_api.py` proves the contract. This file attacks it: path traversal
and symlink escapes out of `web/`, hostile text carried all the way from a
`sign_description` column to the JSON body, and the security headers on the
response shapes that are easy to miss — 400, 404, 405, 416, 422 and 206.

The database is built with `db.create_schema` and filled with payloads no real
sign carries, so a regression shows up as a changed response rather than as a
crash somewhere in the ETL.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import warnings
from pathlib import Path
from urllib.parse import quote

import pytest
from starlette.exceptions import StarletteDeprecationWarning

with warnings.catch_warnings():
    # Same two pinned-version warnings tests/test_api.py silences at import.
    warnings.simplefilter("ignore", StarletteDeprecationWarning)
    warnings.simplefilter("ignore", DeprecationWarning)
    from fastapi.testclient import TestClient

from curbcheck.api.app import CONTENT_SECURITY_POLICY, create_app
from curbcheck.db import create_schema, days_to_mask
from curbcheck.model import ALL_DAYS

NODE_85 = (-73.9544835, 40.7781469)
NODE_86 = (-73.9539850, 40.7788304)
DESTINATION = {"lat": 40.7784, "lon": -73.9542}
WINDOW = {"t1": "2026-09-15T09:00:00", "t2": "2026-09-15T11:00:00"}

SECRET = "TOPSECRET-OUTSIDE-WEB-ROOT"

# One payload per class of attack the threat model names for ingested text
# (SPEC §3.1 T3). Every one of these is stored verbatim and must come back
# verbatim: the API is not the escaping boundary, the frontend is (docs/API.md).
PAYLOADS: dict[str, str] = {
    "script_tag": "2 HOUR PARKING <script>alert('x')</script>",
    "img_onerror": "<img src=x onerror=alert(1)>",
    "formula": "=1+1",
    "formula_neutralized": "'=cmd|'/c calc'!A1",
    "sql_comment": "'; DROP TABLE sign; --",
    "sql_union": "' UNION SELECT value FROM sync_meta --",
    "long": "A" * 100_000,
    "nul_byte": "NO\x00PARKING",
    "rtl_override": "NO PARKING \u202ednekeew\u202c",
    "bidi_isolate": "NO PARKING \u2066\u200f EVIL \u2069",
    "json_break": '{"a": 1}</script><script>alert(1)</script>',
    "header_break": "NO PARKING\r\nX-Injected: yes",
    "astral": "\U0001f6a7 NO PARKING \u00e9\u0301",
    "backslashes": "C:\\\\Windows\\\\ \\u0041 \\",
}


def _geojson(start: tuple[float, float], end: tuple[float, float]) -> str:
    return json.dumps({"type": "LineString", "coordinates": [list(start), list(end)]})


def _build_hostile_database(path: Path) -> None:
    """One blockface whose every text column holds a payload from `PAYLOADS`."""
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
        " min_lon, min_lat, max_lon, max_lat) VALUES (?,?,?,?,?,?,?,?)",
        ("3681", PAYLOADS["img_onerror"], "3 AVE", geom, *bbox),
    )
    for name, text in PAYLOADS.items():
        conn.execute(
            "INSERT INTO sign (sign_id, order_number, on_street, from_street, to_street,"
            " side_of_street, distance_from_intersection, sign_code, sign_description,"
            " segment_id, snap_confidence, snap_notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"sign-{name}",
                text[:60],
                "3 AVENUE",
                "EAST 85 STREET",
                "EAST 86 STREET",
                "W",
                44.0,
                text[:30],
                text,
                "3681",
                0.94,
                text[:40],
            ),
        )
    conn.execute(
        "INSERT INTO regulation_segment (reg_seg_id, segment_id, side, start_ft, end_ft, geom,"
        " min_lon, min_lat, max_lon, max_lat, length_ft, capacity_cars, confidence, derived_from)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "3681:W:0",
            "3681",
            "W",
            0.0,
            284.8,
            geom,
            *bbox,
            284.8,
            12,
            0.94,
            json.dumps([f"sign-{name}" for name in PAYLOADS]),
        ),
    )
    conn.execute(
        "INSERT INTO asp_suspension (date, is_major_legal_holiday, meters_suspended, label)"
        " VALUES (?,?,?,?)",
        ("2026-12-25", 1, 1, PAYLOADS["sql_comment"]),
    )
    for index, text in enumerate(PAYLOADS.values()):
        conn.execute(
            "INSERT INTO regulation (reg_id, reg_seg_id, action, permitted, vehicle_class,"
            " exclusive, days_mask, time_from, time_to, metered, max_duration_min, flags, arrow,"
            " raw_sign_description, parse_method, parse_confidence)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"3681:W:0:{index}",
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
                text,
                "grammar",
                0.98,
            ),
        )
    conn.execute(
        "INSERT INTO meter_rate (blockface_id, segment_id, side, rate_label, hour_rates,"
        " max_session_min) VALUES (?,?,?,?,?,?)",
        ("bf-1", "3681", "W", PAYLOADS["formula"], json.dumps(["4.50", "5.50"]), 120),
    )
    conn.execute(
        "INSERT INTO sync_meta (key, value) VALUES (?,?)",
        (PAYLOADS["img_onerror"], PAYLOADS["sql_comment"]),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def secret_file(tmp_path: Path) -> Path:
    """A file next to, but not inside, the served directory."""
    path = tmp_path / "secret.txt"
    path.write_text(SECRET)
    return path


@pytest.fixture
def web_dir(tmp_path: Path, secret_file: Path) -> Path:
    root = tmp_path / "web"
    (root / "vendor").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>CurbCheck</title>")
    (root / "vendor" / "pmtiles.js").write_text("export const x = 1;\n")
    # Symlinks pointing out of the root, to prove StaticFiles resolves before
    # it serves rather than trusting the path it was handed (threat T4).
    (root / "escape.txt").symlink_to(secret_file)
    (root / "escape-dir").symlink_to(tmp_path)
    return root


@pytest.fixture
def client(tmp_path: Path, web_dir: Path) -> TestClient:
    db_path = tmp_path / "curbcheck.sqlite"
    _build_hostile_database(db_path)
    basemap = tmp_path / "manhattan.pmtiles"
    basemap.write_bytes(bytes(range(256)) * 8)
    return TestClient(create_app(db_path, web_dir=web_dir, basemap_path=basemap))


# --- the static mount cannot leave web/ -----------------------------------


TRAVERSALS = (
    "/../secret.txt",
    "/..%2fsecret.txt",
    "/%2e%2e/secret.txt",
    "/%2e%2e%2fsecret.txt",
    "/%252e%252e/secret.txt",
    "/./../secret.txt",
    "/....//secret.txt",
    "/..\\secret.txt",
    "/vendor/../../secret.txt",
    "/vendor/..%2f..%2fsecret.txt",
    "/vendor/%2e%2e/%2e%2e/secret.txt",
    "/vendor/../../../../../../etc/passwd",
    "/vendor/../../data/curbcheck.sqlite",
    "//etc/passwd",
    "/escape.txt",
    "/escape-dir/secret.txt",
)


@pytest.mark.parametrize("path", TRAVERSALS)
def test_static_mount_never_serves_outside_the_web_directory(client, path):
    response = client.get(path, follow_redirects=False)
    assert response.status_code in {400, 404}, f"{path} was served"
    assert SECRET not in response.text
    assert "root:" not in response.text


@pytest.mark.parametrize(
    "path",
    ["/basemap/", "/basemap/style.json", "/basemap/manhattan.pmtiles/../../secret.txt"],
)
def test_the_basemap_route_serves_only_its_one_file(client, path):
    response = client.get(path, follow_redirects=False)
    assert response.status_code in {400, 404}
    assert SECRET not in response.text


# --- headers on the response shapes that are easy to miss ------------------


def _assert_headers(response, *, no_store: bool) -> None:
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "access-control-allow-origin" not in response.headers
    assert response.headers.get("cache-control") == ("no-store" if no_store else None)


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("GET", "/api/segment/not a valid id!", 400),
        ("GET", "/api/segment/missing", 404),
        ("GET", "/api/nope", 404),
        ("PATCH", "/api/nope", 405),
        ("OPTIONS", "/api/search", 405),
        ("GET", "/api/geocode", 422),
    ],
)
def test_api_error_responses_carry_the_headers_and_no_store(client, method, path, expected):
    response = client.request(method, path, headers={"Origin": "https://evil.example"})
    assert response.status_code == expected
    _assert_headers(response, no_store=True)


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({}, 200),
        ({"Range": "bytes=0-9"}, 206),
        ({"Range": "bytes=999999-"}, 416),
    ],
)
def test_the_basemap_carries_the_headers_on_every_range_outcome(client, headers, expected):
    response = client.get("/basemap/manhattan.pmtiles", headers=headers)
    assert response.status_code == expected
    _assert_headers(response, no_store=False)


def test_a_static_404_still_carries_the_headers(client):
    response = client.get("/does-not-exist.html")
    assert response.status_code == 404
    _assert_headers(response, no_store=False)


# --- hostile text from the database to the JSON body ----------------------


def test_every_hostile_description_survives_the_round_trip_unchanged(client):
    response = client.post("/api/search", json={**DESTINATION, **WINDOW, "walk_minutes": 10})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")

    returned = {
        sign["sign_description"]
        for result in response.json()["results"]
        for sign in result["signs"]
    }
    for name, text in PAYLOADS.items():
        assert text in returned, f"{name} did not come back byte for byte"


def test_the_json_body_is_valid_json_with_no_raw_control_bytes(client):
    response = client.post("/api/search", json={**DESTINATION, **WINDOW})
    assert b"\x00" not in response.content
    assert b"\r\n\r\n" not in response.content.strip()
    json.loads(response.content)  # raises if the encoder produced anything malformed


def test_hostile_text_cannot_inject_a_response_header(client):
    response = client.post("/api/search", json={**DESTINATION, **WINDOW})
    assert "x-injected" not in {name.lower() for name in response.headers}


def test_a_hundred_kilobyte_description_is_returned_whole_not_truncated(client):
    response = client.get("/api/segment/3681:W:0")
    assert response.status_code == 200
    body = response.json()
    descriptions = [
        sign["sign_description"] for sign in [*body["governing"], *body["other_on_block"]]
    ]
    assert PAYLOADS["long"] in descriptions


def test_every_hostile_sign_reaches_one_of_the_two_groups(client):
    """Grouping the signs is allowed; losing one is not (UX audit invariant 4)."""
    body = client.get("/api/segment/3681:W:0").json()
    grouped = {sign["sign_description"] for sign in [*body["governing"], *body["other_on_block"]]}

    assert grouped == set(PAYLOADS.values())


def test_sql_metacharacters_in_the_data_do_not_execute(client):
    """The payloads name tables. If any were interpolated, the schema would be gone."""
    assert client.post("/api/search", json={**DESTINATION, **WINDOW}).status_code == 200
    health = client.get("/api/health").json()
    assert health["status"] == "ok"
    assert health["sign_count"] == len(PAYLOADS)


def test_sync_status_returns_hostile_keys_and_values_as_stored(client):
    body = client.get("/api/sync-status").json()
    assert body == {PAYLOADS["img_onerror"]: PAYLOADS["sql_comment"]}


# --- hostile input on the way in ------------------------------------------


@pytest.mark.parametrize("payload", list(PAYLOADS.values()))
def test_hostile_geocode_queries_answer_without_raising(client, payload):
    response = client.get("/api/geocode", params={"q": payload[:200]})
    assert response.status_code == 200
    assert response.json()["candidates"] == []


@pytest.mark.parametrize("payload", list(PAYLOADS.values()))
def test_hostile_segment_ids_are_rejected_before_any_query(client, payload):
    # Percent-encoded, because that is how a browser sends these bytes and
    # because the test client refuses to build a URL holding a raw CR or NUL.
    response = client.get("/api/segment/" + quote(payload[:80], safe=""))
    assert response.status_code in {400, 404}
    assert response.json()["error"]["code"] in {"invalid_request", "not_found"}


def test_an_oversized_address_is_a_422_not_a_truncated_search(client):
    response = client.post("/api/search", json={"address": "x" * 5000, **WINDOW})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_unknown_body_fields_are_rejected(client):
    response = client.post(
        "/api/search", json={**DESTINATION, **WINDOW, "host": "evil.example", "limit": 1}
    )
    assert response.status_code == 422


def test_error_messages_never_carry_a_path_a_traceback_or_sql(client):
    responses = [
        client.post("/api/search", json={"address": "x" * 5000, **WINDOW}),
        client.get("/api/segment/not valid!"),
        client.get("/api/nope"),
        client.post("/api/search", json={**DESTINATION, "t1": "2026-09-15T09:00", "t2": "bad"}),
    ]
    for response in responses:
        message = response.json()["error"]["message"]
        assert "Traceback" not in message
        assert "/workspace" not in message and "\\" not in message
        assert "SELECT" not in message.upper()
        assert ".py" not in message


# --- the request log never carries a destination (T6) ----------------------


def test_the_typed_address_never_reaches_the_request_log(client, caplog):
    """docs/SECURITY.md residual 2: the autocomplete is a GET, so its `q` was logged.

    uvicorn's access log writes the whole request line. `curbcheck serve` turns
    it off and `AccessLogMiddleware` logs method, path, status and duration
    instead; nothing in the chain ever holds the query string.
    """
    address = "350 W UNIQUESTREETNAME ST"
    # The test client is httpx, and httpx logs the URL it just requested at
    # INFO. That is this test's own plumbing, not the server, so it is quieted
    # rather than filtered out of the assertion afterwards.
    caplog.set_level(logging.WARNING, logger="httpx")

    with caplog.at_level(logging.INFO):
        response = client.get("/api/geocode", params={"q": address})

    assert response.status_code == 200
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "UNIQUESTREETNAME" not in logged
    assert "q=" not in logged
    assert "GET /api/geocode 200" in logged


def test_the_request_log_records_method_path_status_and_duration(client, caplog):
    with caplog.at_level(logging.INFO):
        client.post("/api/search", json={**DESTINATION, **WINDOW})

    lines = [record.getMessage() for record in caplog.records if record.name == "curbcheck.access"]
    assert len(lines) == 1
    assert re.fullmatch(r"POST /api/search 200 \d+\.\dms", lines[0]), lines[0]
