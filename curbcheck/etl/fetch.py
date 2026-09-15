"""Socrata pulls into `data/raw`, with provenance records and a freshness check.

The only module in the ETL that opens a socket, and it does so exclusively
through `curbcheck.net.AllowlistedClient`. Each dataset lands as a JSON array
plus a `.meta.json` sidecar naming the URL, row count and SHA-256 of the bytes
that were written, so every later step can prove which snapshot it read.
"""

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from curbcheck.config import RAW_DIR
from curbcheck.net import (
    BROWSER_USER_AGENT,
    SOCRATA_HOST,
    TRANSPORT_ERRORS,
    AllowlistedClient,
    DownloadRecord,
    Manifest,
    socrata_fetch_all,
)

LOGGER = logging.getLogger(__name__)

# SPEC §5.7 sets a weekly cadence; a snapshot younger than this is reused.
DEFAULT_MAX_AGE_DAYS = 7

# SPEC §5.7: a row count that moves more than this against the previous
# manifest record for the same URL means the export changed shape, not the city.
ROW_COUNT_DRIFT_TOLERANCE = 0.20

# SPEC §5.7 refreshes the ASP calendar annually, but DOT does correct the file
# after publishing it, so the snapshot is re-checked once a quarter.
CALENDAR_MAX_AGE_DAYS = 90

CALENDAR_DIRNAME = "calendar"
CALENDAR_ICS_URL = "https://www.nyc.gov/html/dot/downloads/misc/{year}-alternate-side.ics"

# nyc.gov sends the ICS as text/calendar; the octet-stream and text/plain
# spellings are accepted because a CDN in front of it may relabel the body.
CALENDAR_CONTENT_TYPES = frozenset({"text/calendar", "application/octet-stream", "text/plain"})

# 23 KB in 2026. The cap is three orders of magnitude of headroom, not a limit
# the real file is anywhere near.
CALENDAR_MAX_BYTES = 8 * 1024 * 1024


class FetchError(Exception):
    """A dataset could not be produced in the raw directory."""


class RawDataMissingError(FetchError):
    """Offline mode was requested but the snapshot is not on disk."""


class RowCountDriftError(FetchError):
    """The new snapshot's row count moved further than SPEC §5.7 allows."""


@dataclass(frozen=True)
class Dataset:
    """One Socrata resource and the filename its snapshot keeps in `data/raw`."""

    stem: str
    dataset_id: str
    where: str | None


# Filters and stems match the snapshot `scripts/explore_fetch.py` produced, so a
# re-fetch overwrites the files docs/DATA.md was measured from rather than
# adding a second naming scheme.
DATASETS: tuple[Dataset, ...] = (
    Dataset("signs_manhattan", "nfid-uabd", "borough='Manhattan'"),
    Dataset("centerline_manhattan", "inkn-q76z", "boroughcode='1'"),
    Dataset("parknyc_blockfaces", "e7yp-wx55", None),
    Dataset("meter_rate_zones", "f72k-2u3b", None),
    Dataset("meters_manhattan", "693u-uax6", "borough='Manhattan'"),
)

DATASETS_BY_STEM: dict[str, Dataset] = {dataset.stem: dataset for dataset in DATASETS}


@dataclass(frozen=True)
class FetchResult:
    """What one dataset cost this run: the file, its row count, and whether we refetched."""

    dataset: Dataset
    path: Path
    row_count: int
    refetched: bool


@dataclass(frozen=True)
class CalendarFetchResult:
    """The ASP calendar snapshot after a sync attempt.

    `path` is None only when nothing is on disk and the fetch did not succeed.
    `error` records why a refetch failed while an older snapshot survives, which
    is the case the build continues through with a warning.
    """

    path: Path | None
    refetched: bool
    error: str | None = None


def calendar_path(year: int, raw_dir: Path = RAW_DIR) -> Path:
    """Where the year's ICS lives. Matches the name `scripts/explore_calendar.py` wrote."""
    return raw_dir / CALENDAR_DIRNAME / f"{year}-alternate-side.ics"


def fetch_calendar(
    client: AllowlistedClient | None,
    year: int,
    *,
    raw_dir: Path = RAW_DIR,
    manifest: Manifest | None = None,
    max_age_days: float = CALENDAR_MAX_AGE_DAYS,
    offline: bool = False,
) -> CalendarFetchResult:
    """Ensure the DOT ASP suspension ICS for `year` is on disk (docs/DECISIONS.md D11).

    Never raises: a calendar we cannot refresh is a degraded build, not a failed
    one (SPEC §11). The caller gets the previous snapshot with `error` set, or
    `path=None` when there is nothing at all and the ETL should carry on with an
    empty `asp_suspension` table.
    """
    path = calendar_path(year, raw_dir)
    if offline or is_fresh(path, max_age_days):
        if not path.exists():
            return CalendarFetchResult(path=None, refetched=False, error="no snapshot on disk")
        return CalendarFetchResult(path=path, refetched=False)

    url = CALENDAR_ICS_URL.format(year=year)
    owned_client = client is None
    active = client or AllowlistedClient()
    try:
        record = active.download(
            url,
            path,
            max_bytes=CALENDAR_MAX_BYTES,
            expected_content_types=CALENDAR_CONTENT_TYPES,
            headers={"User-Agent": BROWSER_USER_AGENT},
        )
    except TRANSPORT_ERRORS as error:
        LOGGER.warning("fetch.calendar_failed year=%d url=%s error=%s", year, url, error)
        return CalendarFetchResult(
            path=path if path.exists() else None, refetched=False, error=str(error)
        )
    finally:
        if owned_client:
            active.close()

    (manifest or Manifest(raw_dir / "manifest.jsonl")).append(record)
    _write_calendar_sidecar(path, record)
    LOGGER.info("fetch.calendar year=%d bytes=%d", year, record.size_bytes)
    return CalendarFetchResult(path=path, refetched=True)


def raw_path(dataset: Dataset, raw_dir: Path = RAW_DIR) -> Path:
    return raw_dir / f"{dataset.stem}.json"


def meta_path(dataset: Dataset, raw_dir: Path = RAW_DIR) -> Path:
    return raw_dir / f"{dataset.stem}.meta.json"


def resource_url(dataset: Dataset) -> str:
    """Canonical URL for the dataset, used as the manifest key across runs.

    The paging parameters are deliberately left off: they differ per page, and
    the manifest needs one stable identity per dataset to compare against.
    """
    query = urlencode({"$where": dataset.where}) if dataset.where else ""
    suffix = f"?{query}" if query else ""
    return f"https://{SOCRATA_HOST}/resource/{dataset.dataset_id}.json{suffix}"


def load_rows(stem: str, raw_dir: Path = RAW_DIR) -> list[dict[str, Any]]:
    """Read a snapshot back as a list of raw Socrata rows.

    Socrata omits null fields from JSON rows, so a missing key means null and
    callers must use `row.get(...)`.
    """
    path = raw_dir / f"{stem}.json"
    if not path.exists():
        raise RawDataMissingError(f"no snapshot at {path}; run `curbcheck sync` without --offline")
    rows: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    return rows


def is_fresh(path: Path, max_age_days: float, *, now: datetime | None = None) -> bool:
    """Whether the snapshot exists and is younger than max_age_days."""
    if not path.exists():
        return False
    moment = now or datetime.now(UTC)
    age = moment - datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    return age < timedelta(days=max_age_days)


def check_row_count_drift(manifest: Manifest, url: str, row_count: int) -> None:
    """Raise when the row count moved more than SPEC §5.7's ±20% against the last run.

    On the first run there is no previous record and nothing to compare, so this
    is a no-op.
    """
    previous = manifest.latest_for(url)
    if previous is None or not previous.row_count:
        return
    drift = abs(row_count - previous.row_count) / previous.row_count
    if drift > ROW_COUNT_DRIFT_TOLERANCE:
        raise RowCountDriftError(
            f"{url} returned {row_count} rows, {drift:.1%} from the previous "
            f"{previous.row_count}; refusing to replace the snapshot"
        )


def fetch_dataset(
    dataset: Dataset,
    *,
    client: AllowlistedClient | None = None,
    raw_dir: Path = RAW_DIR,
    manifest: Manifest | None = None,
    max_age_days: float = DEFAULT_MAX_AGE_DAYS,
    offline: bool = False,
) -> FetchResult:
    """Ensure a fresh snapshot of one dataset is on disk, downloading only if needed.

    Returns without touching the network when the file is younger than
    max_age_days or when offline is set; raises RawDataMissingError if offline
    is set and there is no file at all.
    """
    path = raw_path(dataset, raw_dir)
    if offline or is_fresh(path, max_age_days):
        if not path.exists():
            raise RawDataMissingError(f"offline fetch requested but {path} does not exist")
        row_count = _row_count_of(path)
        LOGGER.info("fetch.skip stem=%s rows=%d reason=%s", dataset.stem, row_count, "fresh")
        return FetchResult(dataset=dataset, path=path, row_count=row_count, refetched=False)

    owned_client = client is None
    active = client or AllowlistedClient()
    try:
        rows = socrata_fetch_all(active, dataset.dataset_id, dataset.where or "1=1")
    finally:
        if owned_client:
            active.close()

    log = manifest or Manifest(raw_dir / "manifest.jsonl")
    url = resource_url(dataset)
    check_row_count_drift(log, url, len(rows))
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf-8")
    _write_atomic(path, payload)
    record = DownloadRecord(
        url=url,
        path=str(path),
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        # socrata_fetch_all raises on any non-2xx page, so a record exists only
        # for a pull where every page came back 200.
        status_code=200,
        content_type="application/json",
        row_count=len(rows),
    )
    log.append(record)
    _write_sidecar(dataset, raw_dir, record)
    LOGGER.info("fetch.done stem=%s rows=%d bytes=%d", dataset.stem, len(rows), len(payload))
    return FetchResult(dataset=dataset, path=path, row_count=len(rows), refetched=True)


def fetch_all(
    *,
    raw_dir: Path = RAW_DIR,
    max_age_days: float = DEFAULT_MAX_AGE_DAYS,
    offline: bool = False,
    datasets: Sequence[Dataset] = DATASETS,
    calendar_year: int | None = None,
) -> list[FetchResult]:
    """Fetch every dataset the ETL needs, reusing snapshots that are still fresh.

    The ASP calendar is fetched alongside them but is not in the returned list:
    it is not a Socrata dataset and a failure to refresh it does not stop a
    build (docs/DECISIONS.md D11).
    """
    raw_dir.mkdir(parents=True, exist_ok=True)
    manifest = Manifest(raw_dir / "manifest.jsonl")
    year = calendar_year or datetime.now(UTC).year
    results = []
    if offline:
        for dataset in datasets:
            results.append(fetch_dataset(dataset, raw_dir=raw_dir, manifest=manifest, offline=True))
        fetch_calendar(None, year, raw_dir=raw_dir, manifest=manifest, offline=True)
        return results
    with AllowlistedClient() as client:
        for dataset in datasets:
            results.append(
                fetch_dataset(
                    dataset,
                    client=client,
                    raw_dir=raw_dir,
                    manifest=manifest,
                    max_age_days=max_age_days,
                )
            )
        fetch_calendar(client, year, raw_dir=raw_dir, manifest=manifest)
    return results


def _row_count_of(path: Path) -> int:
    sidecar = path.with_suffix(".meta.json")
    if sidecar.exists():
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        count = meta.get("row_count")
        if isinstance(count, int):
            return count
    return len(json.loads(path.read_text(encoding="utf-8")))


def _write_atomic(path: Path, payload: bytes) -> None:
    """Replace path in one rename so a crash never leaves a half-written snapshot."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _handle, temp_name = tempfile.mkstemp(dir=path.parent, suffix=".part")
    temp_path = Path(temp_name)
    try:
        with Path(temp_path).open("wb") as sink:
            sink.write(payload)
        temp_path.replace(path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _write_calendar_sidecar(path: Path, record: DownloadRecord) -> None:
    meta = {
        "url": record.url,
        "fetched_at_utc": record.fetched_at.isoformat(timespec="seconds"),
        "byte_size": record.size_bytes,
        "sha256": record.sha256,
    }
    path.with_suffix(path.suffix + ".meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8"
    )


def _write_sidecar(dataset: Dataset, raw_dir: Path, record: DownloadRecord) -> None:
    meta = {
        "dataset_id": dataset.dataset_id,
        "url": record.url,
        "where": dataset.where,
        "fetched_at_utc": record.fetched_at.isoformat(timespec="seconds"),
        "row_count": record.row_count,
        "byte_size": record.size_bytes,
        "sha256": record.sha256,
    }
    meta_path(dataset, raw_dir).write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
