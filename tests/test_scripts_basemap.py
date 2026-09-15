"""The one value the basemap scripts take from a remote document before running a process.

`scripts/fetch_basemap_tiles.py` learns the current planet-build key from a JSON
manifest on the network and puts it in a `subprocess` argv. An argv is not a
shell, but a key that is not a dated pmtiles name is still an argument nobody
meant to pass, so it is matched against a strict pattern first
(`docs/SECURITY.md`, threat T3).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# The basemap scripts live in scripts/, which is not a package: they are
# developer tools first and libraries second (same arrangement as eval_gold).
sys.path.insert(0, str(ROOT / "scripts"))

import fetch_basemap_tiles  # noqa: E402


def test_a_dated_pmtiles_key_is_accepted() -> None:
    assert fetch_basemap_tiles.checked_key({"key": "20260914.pmtiles"}) == "20260914.pmtiles"


@pytest.mark.parametrize(
    "key",
    [
        "",
        "latest.pmtiles",
        "2026091.pmtiles",
        "20260914.pmtiles.sh",
        "../../etc/passwd",
        "--bbox=-180,-90,180,90",
        "-o/tmp/evil",
        "20260914.pmtiles\n20260915.pmtiles",
        "20260914.pmtiles ",
        "20260914.pmtiles;rm -rf /",
    ],
)
def test_anything_that_is_not_a_dated_pmtiles_key_is_refused(key: str) -> None:
    with pytest.raises(ValueError, match="dated pmtiles name"):
        fetch_basemap_tiles.checked_key({"key": key})


def test_a_missing_key_is_refused() -> None:
    with pytest.raises(ValueError, match="dated pmtiles name"):
        fetch_basemap_tiles.checked_key({})


def test_the_extract_command_is_argv_with_no_shell_metacharacters() -> None:
    command = fetch_basemap_tiles.extract_command("20260914.pmtiles")

    assert command[:2] == ["pmtiles", "extract"]
    assert command[2] == "https://build.protomaps.com/20260914.pmtiles"
    assert not any(character in " ".join(command) for character in ";|&$`")


def test_the_manifest_host_is_the_only_one_the_script_will_fetch() -> None:
    assert fetch_basemap_tiles.BUILDS_MANIFEST.startswith("https://")
    host = fetch_basemap_tiles.BUILDS_MANIFEST.split("/")[2]
    assert fetch_basemap_tiles.ALLOWED_HOSTS == frozenset({host})
