"""Config is read from the environment at import time, so these tests reload the module."""

from __future__ import annotations

import importlib
import re
import tomllib
from pathlib import Path

import pytest

import curbcheck.config as config


@pytest.fixture(autouse=True)
def _restore_config() -> object:
    yield
    importlib.reload(config)


def test_data_dir_defaults_to_the_repo_data_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CURBCHECK_DATA_DIR", raising=False)
    reloaded = importlib.reload(config)

    assert reloaded.DATA_DIR == reloaded.REPO_ROOT / "data"
    assert reloaded.DB_PATH == reloaded.DATA_DIR / "curbcheck.sqlite"


def test_curbcheck_data_dir_env_var_overrides_the_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CURBCHECK_DATA_DIR", str(tmp_path))
    reloaded = importlib.reload(config)

    expected = tmp_path.resolve()
    assert reloaded.DATA_DIR == expected
    assert reloaded.RAW_DIR == expected / "raw"


def test_server_binds_loopback_only() -> None:
    assert config.BIND_HOST == "127.0.0.1"


def test_allowlist_holds_exactly_the_hosts_the_threat_model_names() -> None:
    expected = frozenset(
        {
            "data.cityofnewyork.us",
            "api.us.socrata.com",
            "www.nyc.gov",
            "s-media.nyc.gov",
            "build.protomaps.com",
            "api-portal.nyc.gov",
        }
    )
    assert config.ALLOWED_HOSTS == expected


def test_the_package_declares_the_licence_the_repository_carries() -> None:
    """The owner chose MIT on 2026-09-15; the metadata, the file and README agree."""
    root = Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["license"] == "MIT"
    assert pyproject["project"]["license-files"] == ["LICENSE"]
    licence = (root / "LICENSE").read_text(encoding="utf-8")
    assert licence.startswith("MIT License")
    assert "Oren Tirschwell" in licence
    assert "MIT License" in (root / "README.md").read_text(encoding="utf-8")


def test_the_build_backend_is_pinned_hashed_and_used_without_build_isolation() -> None:
    """Threat T1: PEP 517 build isolation used to fetch hatchling from PyPI unhashed.

    It was the one install `--require-hashes` did not cover, in `make setup` and
    in the image alike (docs/SECURITY.md residual 8).
    """
    root = Path(__file__).resolve().parents[1]
    backend = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["build-system"]
    locked = (root / "requirements-dev.txt").read_text(encoding="utf-8")

    assert backend["build-backend"] == "hatchling.build"
    for package in ("hatchling", "editables"):
        assert re.search(rf"^{package}==\S+ \\\n\s+--hash=sha256:", locked, re.M), package

    assert "--no-build-isolation" in (root / "Makefile").read_text(encoding="utf-8")
    assert "--no-build-isolation" in (root / "Dockerfile").read_text(encoding="utf-8")
