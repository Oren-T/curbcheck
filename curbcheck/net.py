"""The only outbound HTTP client in the codebase.

Every fetch goes through AllowlistedClient so the controls in SPEC §3.2 and
threat T2 hold in one place: HTTPS only, certificate verification always on,
an enumerated host allowlist that also applies to each redirect hop, and a
byte cap so a hostile or broken endpoint cannot fill the disk. Direct use of
httpx or urllib anywhere else is a review blocker (STYLE_GUIDE §5).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field

from curbcheck.config import ALLOWED_HOSTS, RAW_DIR

DEFAULT_TIMEOUT_S = 60.0
DEFAULT_MAX_BYTES = 500 * 1024 * 1024
DEFAULT_MAX_REDIRECTS = 5
SOCRATA_HOST = "data.cityofnewyork.us"
_DATASET_ID = re.compile(r"^[a-z0-9]{4}-[a-z0-9]{4}$")


class NetworkPolicyError(Exception):
    """A request was refused by this module's own rules, not by the remote host."""


class DisallowedHostError(NetworkPolicyError):
    """The URL's scheme is not https or its host is not in ALLOWED_HOSTS."""


class ResponseTooLargeError(NetworkPolicyError):
    """The response exceeded the caller's byte cap and was aborted."""


class TooManyRedirectsError(NetworkPolicyError):
    """The redirect chain exceeded the allowed number of hops."""


class UnexpectedContentTypeError(NetworkPolicyError):
    """The response Content-Type was not one the caller said it would accept."""


class DownloadRecord(BaseModel):
    """Provenance for one fetched artifact, appended to the raw-data manifest."""

    url: str
    path: str
    sha256: str
    size_bytes: int
    status_code: int
    content_type: str
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    row_count: int | None = Field(
        default=None, description="Rows the ETL read from this artifact, for drift checks"
    )


def check_url(url: str) -> str:
    """Return the host of url, or raise DisallowedHostError if policy forbids the URL."""
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise DisallowedHostError(f"only https is allowed, got {parts.scheme!r} in {url!r}")
    host = parts.hostname or ""
    if host not in ALLOWED_HOSTS:
        raise DisallowedHostError(f"host {host!r} is not in the allowlist")
    return host


class AllowlistedClient:
    """httpx wrapper that can only reach the hosts in ALLOWED_HOSTS, over TLS.

    There is deliberately no way to disable certificate verification. Redirects
    are followed manually so each hop is re-checked against the allowlist.
    """

    def __init__(
        self,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._max_redirects = max_redirects
        self._client = httpx.Client(
            timeout=timeout_s,
            follow_redirects=False,
            verify=True,
            transport=transport,
            headers={"User-Agent": "curbcheck/0.1 (local single-user tool)"},
        )

    def __enter__(self) -> AllowlistedClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    @contextmanager
    def stream(
        self, method: str, url: str, *, params: dict[str, Any] | None = None
    ) -> Iterator[httpx.Response]:
        """Open a streamed response, re-checking the allowlist on every redirect hop."""
        for _ in range(self._max_redirects + 1):
            check_url(url)
            with self._client.stream(method, url, params=params) as response:
                redirect = response.next_request
                if redirect is None:
                    yield response
                    return
                url = str(redirect.url)
                params = None  # the Location URL carries its own query string
        raise TooManyRedirectsError(f"more than {self._max_redirects} redirects from {url!r}")

    def get_json(
        self, url: str, *, params: dict[str, Any] | None = None, max_bytes: int = 64 * 1024 * 1024
    ) -> Any:
        """Fetch and decode a JSON body, aborting if it grows past max_bytes."""
        with self.stream("GET", url, params=params) as response:
            response.raise_for_status()
            body = _read_capped(response, max_bytes)
        return json.loads(body)

    def download(
        self,
        url: str,
        dest_path: Path,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        expected_content_types: frozenset[str] | None = None,
    ) -> DownloadRecord:
        """Stream url to dest_path via a temp file, returning its hash, size, and type.

        The rename is atomic, so a partial or oversized download never replaces
        the previous good snapshot (threat T2).
        """
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size_bytes = 0
        with self.stream("GET", url) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if expected_content_types is not None and content_type not in expected_content_types:
                raise UnexpectedContentTypeError(
                    f"{url!r} returned {content_type!r}, "
                    f"expected one of {sorted(expected_content_types)}"
                )
            handle, temp_name = tempfile.mkstemp(dir=dest_path.parent, suffix=".part")
            temp_path = Path(temp_name)
            try:
                with os.fdopen(handle, "wb") as sink:
                    for chunk in response.iter_bytes():
                        size_bytes += len(chunk)
                        if size_bytes > max_bytes:
                            raise ResponseTooLargeError(
                                f"{url!r} exceeded the {max_bytes} byte cap"
                            )
                        digest.update(chunk)
                        sink.write(chunk)
                temp_path.replace(dest_path)
            except BaseException:
                temp_path.unlink(missing_ok=True)
                raise
            status_code = response.status_code
        return DownloadRecord(
            url=url,
            path=str(dest_path),
            sha256=digest.hexdigest(),
            size_bytes=size_bytes,
            status_code=status_code,
            content_type=content_type,
        )


def _read_capped(response: httpx.Response, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > max_bytes:
            raise ResponseTooLargeError(f"response exceeded the {max_bytes} byte cap")
        chunks.append(chunk)
    return b"".join(chunks)


class Manifest:
    """Append-only provenance log of every artifact fetched into the raw directory."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else RAW_DIR / "manifest.jsonl"

    def append(self, record: DownloadRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as sink:
            sink.write(record.model_dump_json() + "\n")

    def records_for(self, url: str) -> list[DownloadRecord]:
        """Past records for url, oldest first. Empty when nothing was ever fetched."""
        if not self.path.exists():
            return []
        matches = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = DownloadRecord.model_validate_json(line)
            if record.url == url:
                matches.append(record)
        return matches

    def latest_for(self, url: str) -> DownloadRecord | None:
        records = self.records_for(url)
        return records[-1] if records else None


def socrata_fetch_all(
    client: AllowlistedClient,
    dataset_id: str,
    where: str,
    select: str | None = None,
    page_size: int = 50_000,
    max_rows: int = 2_000_000,
) -> list[dict[str, Any]]:
    """Page a Socrata resource endpoint and return every matching row.

    Ordering by `:id` is what makes offset paging stable; without it Socrata may
    return a row twice or skip one. Socrata omits null fields from JSON rows
    entirely, so a missing key means null and callers must use `row.get(...)`.
    Raises ResponseTooLargeError if the result would exceed max_rows.
    """
    if not _DATASET_ID.match(dataset_id):
        raise ValueError(f"not a Socrata dataset id: {dataset_id!r}")
    url = f"https://{SOCRATA_HOST}/resource/{dataset_id}.json"
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        params: dict[str, Any] = {
            "$where": where,
            "$limit": page_size,
            "$offset": offset,
            "$order": ":id",
        }
        if select is not None:
            params["$select"] = select
        page: list[dict[str, Any]] = client.get_json(url, params=params)
        rows.extend(page)
        if len(rows) > max_rows:
            raise ResponseTooLargeError(f"{dataset_id} returned more than {max_rows} rows")
        if len(page) < page_size:
            return rows
        offset += page_size
