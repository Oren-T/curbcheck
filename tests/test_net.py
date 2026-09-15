"""Policy tests for the allowlisted HTTP client. No test here touches the network."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from curbcheck.net import (
    AllowlistedClient,
    DisallowedHostError,
    Manifest,
    ResponseTooLargeError,
    check_url,
    socrata_fetch_all,
)

ALLOWED_URL = "https://data.cityofnewyork.us/resource/nfid-uabd.json"


Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: Handler) -> AllowlistedClient:
    return AllowlistedClient(transport=httpx.MockTransport(handler))


def test_request_to_host_outside_allowlist_is_refused() -> None:
    with pytest.raises(DisallowedHostError, match="allowlist"):
        check_url("https://evil.example.com/data.json")


def test_plain_http_url_is_refused_even_for_an_allowed_host() -> None:
    with pytest.raises(DisallowedHostError, match="https"):
        check_url("http://data.cityofnewyork.us/resource/nfid-uabd.json")


def test_download_to_disallowed_host_never_opens_a_connection(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("the client must refuse before sending the request")

    with _client(handler) as client, pytest.raises(DisallowedHostError):
        client.download("https://evil.example.com/x.json", tmp_path / "x.json")


def test_redirect_to_a_disallowed_host_is_refused(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example.com/x.json"})

    with (
        _client(handler) as client,
        pytest.raises(DisallowedHostError, match=re.escape("evil.example.com")),
    ):
        client.download(ALLOWED_URL, tmp_path / "x.json")


def test_redirect_within_the_allowlist_is_followed(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "data.cityofnewyork.us":
            return httpx.Response(302, headers={"location": "https://www.nyc.gov/x.json"})
        return httpx.Response(200, content=b"ok", headers={"content-type": "application/json"})

    dest = tmp_path / "x.json"
    with _client(handler) as client:
        record = client.download(ALLOWED_URL, dest)

    assert dest.read_bytes() == b"ok"
    assert record.size_bytes == 2


def test_download_past_the_byte_cap_aborts_and_leaves_no_file(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"x" * 5000, headers={"content-type": "application/json"}
        )

    dest = tmp_path / "big.json"
    with _client(handler) as client, pytest.raises(ResponseTooLargeError):
        client.download(ALLOWED_URL, dest, max_bytes=100)

    assert not dest.exists()
    assert list(tmp_path.iterdir()) == []


def test_download_records_hash_size_and_content_type(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"hello", headers={"content-type": "application/json; charset=utf-8"}
        )

    with _client(handler) as client:
        record = client.download(ALLOWED_URL, tmp_path / "hello.json")

    assert record.content_type == "application/json"
    assert record.size_bytes == 5
    assert record.sha256 == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"


def test_manifest_returns_previous_records_for_the_same_url(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"a", headers={"content-type": "application/json"})

    manifest = Manifest(tmp_path / "manifest.jsonl")
    assert manifest.latest_for(ALLOWED_URL) is None

    with _client(handler) as client:
        manifest.append(client.download(ALLOWED_URL, tmp_path / "a.json"))
        manifest.append(client.download("https://www.nyc.gov/b.json", tmp_path / "b.json"))
        manifest.append(client.download(ALLOWED_URL, tmp_path / "a.json"))

    assert len(manifest.records_for(ALLOWED_URL)) == 2
    assert manifest.latest_for(ALLOWED_URL) is not None


def test_socrata_paging_stops_on_a_short_page() -> None:
    pages = {0: [{"a": 1}, {"a": 2}], 2: [{"a": 3}]}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["$order"] == ":id"
        offset = int(request.url.params["$offset"])
        return httpx.Response(200, json=pages[offset])

    with _client(handler) as client:
        rows = socrata_fetch_all(client, "nfid-uabd", where="boro='M'", page_size=2)

    assert rows == [{"a": 1}, {"a": 2}, {"a": 3}]


def test_socrata_rejects_an_id_that_is_not_a_dataset_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not be reached")

    with _client(handler) as client, pytest.raises(ValueError, match="dataset id"):
        socrata_fetch_all(client, "../../etc/passwd", where="1=1")
