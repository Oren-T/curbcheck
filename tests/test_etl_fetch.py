"""Snapshot fetching: freshness, offline mode, and the row-count drift guard.

No test here reaches the network; the one test that exercises a download drives
`AllowlistedClient` through an httpx MockTransport.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from curbcheck.etl.fetch import (
    DATASETS_BY_STEM,
    Dataset,
    RawDataMissingError,
    RowCountDriftError,
    check_row_count_drift,
    fetch_dataset,
    is_fresh,
    load_rows,
    resource_url,
)
from curbcheck.net import AllowlistedClient, DownloadRecord, Manifest

SIGNS = DATASETS_BY_STEM["signs_manhattan"]


def mock_client(rows):
    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("$offset", "0"))
        return httpx.Response(200, json=rows if offset == 0 else [])

    return AllowlistedClient(transport=httpx.MockTransport(handler))


def test_dataset_ids_and_filters_match_the_profiled_snapshot():
    # docs/DATA.md's table is the contract: changing either side invalidates
    # every measured number in it.
    assert SIGNS.dataset_id == "nfid-uabd"
    assert SIGNS.where == "borough='Manhattan'"
    assert DATASETS_BY_STEM["centerline_manhattan"].where == "boroughcode='1'"
    assert DATASETS_BY_STEM["meter_rate_zones"].where is None


def test_resource_url_is_stable_across_runs_so_the_manifest_can_compare():
    assert resource_url(SIGNS) == (
        "https://data.cityofnewyork.us/resource/nfid-uabd.json?%24where=borough%3D%27Manhattan%27"
    )


def test_a_snapshot_younger_than_the_cadence_is_fresh(tmp_path):
    path = tmp_path / "signs_manhattan.json"
    path.write_text("[]")

    assert is_fresh(path, 7) is True
    assert is_fresh(path, 7, now=datetime.now(UTC) + timedelta(days=8)) is False
    assert is_fresh(tmp_path / "missing.json", 7) is False


def test_offline_mode_reads_the_snapshot_and_never_builds_a_client(tmp_path):
    (tmp_path / "signs_manhattan.json").write_text(json.dumps([{"a": 1}, {"a": 2}]))

    result = fetch_dataset(SIGNS, raw_dir=tmp_path, offline=True)

    assert result.refetched is False
    assert result.row_count == 2


def test_offline_mode_fails_loudly_when_there_is_no_snapshot(tmp_path):
    with pytest.raises(RawDataMissingError):
        fetch_dataset(SIGNS, raw_dir=tmp_path, offline=True)


def test_a_fresh_snapshot_is_reused_without_a_download(tmp_path):
    (tmp_path / "signs_manhattan.json").write_text("[]")

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a fresh snapshot must not be refetched")

    with AllowlistedClient(transport=httpx.MockTransport(refuse)) as client:
        result = fetch_dataset(SIGNS, client=client, raw_dir=tmp_path, max_age_days=7)

    assert result.refetched is False


def test_a_stale_snapshot_is_replaced_and_recorded_in_the_manifest(tmp_path):
    (tmp_path / "signs_manhattan.json").write_text("[]")
    manifest = Manifest(tmp_path / "manifest.jsonl")

    with mock_client([{"sign_code": "PS-1G"}]) as client:
        result = fetch_dataset(
            SIGNS, client=client, raw_dir=tmp_path, manifest=manifest, max_age_days=0
        )

    assert result.refetched is True
    assert load_rows("signs_manhattan", tmp_path) == [{"sign_code": "PS-1G"}]
    record = manifest.latest_for(resource_url(SIGNS))
    assert record is not None
    assert record.row_count == 1
    assert len(record.sha256) == 64
    sidecar = json.loads((tmp_path / "signs_manhattan.meta.json").read_text())
    assert sidecar["dataset_id"] == "nfid-uabd"
    assert sidecar["row_count"] == 1


def test_row_count_drift_beyond_twenty_percent_aborts_before_overwriting(tmp_path):
    # SPEC §5.7: a snapshot that moved that far means the export changed shape.
    (tmp_path / "signs_manhattan.json").write_text(json.dumps([{"old": True}]))
    manifest = Manifest(tmp_path / "manifest.jsonl")
    manifest.append(
        DownloadRecord(
            url=resource_url(SIGNS),
            path=str(tmp_path / "signs_manhattan.json"),
            sha256="0" * 64,
            size_bytes=1,
            status_code=200,
            content_type="application/json",
            row_count=100,
        )
    )

    with (
        mock_client([{"n": index} for index in range(50)]) as client,
        pytest.raises(RowCountDriftError),
    ):
        fetch_dataset(SIGNS, client=client, raw_dir=tmp_path, manifest=manifest, max_age_days=0)

    assert load_rows("signs_manhattan", tmp_path) == [{"old": True}]


def test_the_first_run_has_nothing_to_compare_against(tmp_path):
    manifest = Manifest(tmp_path / "manifest.jsonl")

    check_row_count_drift(manifest, resource_url(SIGNS), 74_590)


def test_an_unknown_dataset_stem_is_a_missing_snapshot_not_a_crash(tmp_path):
    unknown = Dataset("does_not_exist", "aaaa-bbbb", None)

    with pytest.raises(RawDataMissingError):
        fetch_dataset(unknown, raw_dir=tmp_path, offline=True)
