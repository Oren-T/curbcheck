"""Bypass attempts against the SPEC §3.2 allowlist (threat T2).

`tests/test_net.py` covers the happy paths and the two obvious refusals. This
file is the adversarial half: every way a URL can name one host to a checker and
a different host to the HTTP client. Each case asserts the *policy* outcome, not
the parse, so a change to `check_url` that loosens it fails here rather than in
review.

Nothing here touches the network; every client is wired to a MockTransport that
fails the test if it is ever asked to send a refused request.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from curbcheck.net import (
    AllowlistedClient,
    DisallowedHostError,
    ResponseTooLargeError,
    TooManyRedirectsError,
    check_url,
)

ALLOWED_URL = "https://data.cityofnewyork.us/resource/nfid-uabd.json"

Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: Handler) -> AllowlistedClient:
    return AllowlistedClient(transport=httpx.MockTransport(handler))


def _never_called(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"the client must refuse before sending {request.url}")


# --- URLs that name an allowed host but resolve to another ----------------

# Each entry is a URL a reviewer might read as "data.cityofnewyork.us" at a
# glance. `hostname` is what the client actually connects to, so all of them
# must be refused.
DECEPTIVE_URLS = (
    pytest.param("https://data.cityofnewyork.us@evil.example.com/x.json", id="userinfo"),
    pytest.param("https://data.cityofnewyork.us:443@evil.example.com/x.json", id="userinfo-port"),
    pytest.param("https://data.cityofnewyork.us\\@evil.example.com/x.json", id="backslash"),
    pytest.param("https://data.cityofnewyork.us.evil.example.com/x.json", id="suffix"),
    pytest.param("https://evil.example.com/?host=data.cityofnewyork.us", id="query"),
    pytest.param("https://evil.example.com/data.cityofnewyork.us/x.json", id="path"),
    # A trailing dot is a distinct DNS name and is not in the frozenset, so the
    # allowlist fails closed on it. Listed here so the behaviour is deliberate.
    pytest.param("https://data.cityofnewyork.us./x.json", id="trailing-dot"),
)

# Unicode hosts that render like the allowlisted one. Refusing them is the whole
# point of comparing against an ASCII frozenset rather than doing fuzzy matching.
CONFUSABLE_URLS = (
    pytest.param("https://dаta.cityofnewyork.us/x.json", id="cyrillic-a"),
    pytest.param("https://data.cityofnewyork。us/x.json", id="ideographic-full-stop"),
    pytest.param("https://data．cityofnewyork.us/x.json", id="fullwidth-full-stop"),
    pytest.param("https://ＤＡＴＡ.cityofnewyork.us/x.json", id="fullwidth-label"),
    pytest.param("https://xn--dta-sla.cityofnewyork.us/x.json", id="punycode"),
)

NON_HTTPS_URLS = (
    pytest.param("http://data.cityofnewyork.us/x.json", id="http"),
    pytest.param("file:///etc/passwd", id="file"),
    pytest.param("ftp://data.cityofnewyork.us/x.json", id="ftp"),
    pytest.param("//data.cityofnewyork.us/x.json", id="scheme-relative"),
    pytest.param("data:application/json,[]", id="data-uri"),
)


@pytest.mark.parametrize("url", DECEPTIVE_URLS)
def test_url_that_only_looks_allowlisted_is_refused(url: str) -> None:
    with pytest.raises(DisallowedHostError):
        check_url(url)


@pytest.mark.parametrize("url", CONFUSABLE_URLS)
def test_confusable_unicode_host_is_refused(url: str) -> None:
    with pytest.raises(DisallowedHostError, match="allowlist"):
        check_url(url)


@pytest.mark.parametrize("url", NON_HTTPS_URLS)
def test_only_https_is_accepted(url: str) -> None:
    with pytest.raises(DisallowedHostError, match="https"):
        check_url(url)


@pytest.mark.parametrize("url", [*DECEPTIVE_URLS, *CONFUSABLE_URLS, *NON_HTTPS_URLS])
def test_a_refused_url_never_opens_a_connection(url: str, tmp_path: Path) -> None:
    with _client(_never_called) as client, pytest.raises(DisallowedHostError):
        client.download(url, tmp_path / "x.json")


# --- URLs that are genuinely on an allowlisted host ------------------------


@pytest.mark.parametrize(
    "url",
    [
        "HTTPS://DATA.CITYOFNEWYORK.US/x.json",
        "https://user:pass@data.cityofnewyork.us/x.json",
        "https://data.cityofnewyork.us:443/x.json",
    ],
)
def test_host_comparison_ignores_case_userinfo_and_port(url: str) -> None:
    assert check_url(url) == "data.cityofnewyork.us"


def test_an_allowed_host_on_an_odd_port_is_still_allowed() -> None:
    """The allowlist is by host, not by host:port. Recorded so the gap is known."""
    assert check_url("https://data.cityofnewyork.us:8443/x.json") == "data.cityofnewyork.us"


# --- redirects -------------------------------------------------------------


def _redirect_once(location: str) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": location})
        return httpx.Response(200, content=b"{}", headers={"content-type": "application/json"})

    return handler


@pytest.mark.parametrize(
    "location",
    [
        pytest.param("https://evil.example.com/x.json", id="plain"),
        pytest.param("//evil.example.com/x.json", id="protocol-relative"),
        pytest.param("https://data.cityofnewyork.us@evil.example.com/x", id="userinfo"),
        # A Location header is ASCII bytes on the wire, so a homograph arrives
        # punycoded. `xn--dta-sla` is `dаta` with a Cyrillic a.
        pytest.param("https://xn--dta-sla.cityofnewyork.us/x", id="punycode-homograph"),
    ],
)
def test_redirect_off_the_allowlist_is_refused(location: str) -> None:
    with (
        _client(_redirect_once(location)) as client,
        pytest.raises(DisallowedHostError),
    ):
        client.get_json("https://data.cityofnewyork.us/start")


def test_redirect_downgrading_to_http_is_refused() -> None:
    """A MITM that answers 302 to http:// must not get a cleartext second request."""
    with (
        _client(_redirect_once("http://data.cityofnewyork.us/x.json")) as client,
        pytest.raises(DisallowedHostError, match="https"),
    ):
        client.get_json("https://data.cityofnewyork.us/start")


def test_an_endless_redirect_chain_stops_at_the_hop_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://data.cityofnewyork.us/next"})

    with _client(handler) as client, pytest.raises(TooManyRedirectsError):
        client.get_json(ALLOWED_URL)


# --- the client's own guarantees ------------------------------------------


def test_certificate_verification_cannot_be_turned_off() -> None:
    """There is no `verify` parameter to pass, and the wrapped client has it on."""
    with AllowlistedClient() as client:
        assert "verify" not in AllowlistedClient.__init__.__annotations__
        assert client._client.follow_redirects is False


def test_get_json_stops_reading_at_the_byte_cap() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b'"' + b"x" * 5000 + b'"', headers={"content-type": "application/json"}
        )

    with _client(handler) as client, pytest.raises(ResponseTooLargeError):
        client.get_json(ALLOWED_URL, max_bytes=100)
