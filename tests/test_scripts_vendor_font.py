"""The vendored typeface is reproducible from a recipe, and its bytes are checked.

`web/fonts/InterVariable-latin.woff2` is a binary in the repository, so the two
questions a reviewer has are "where did it come from?" and "is this still it?".
`scripts/vendor_font.py` answers both, and this pins the answers: the hashes it
records are the hashes `web/fonts/MANIFEST.md` publishes, and a source archive
that is not the recorded release is refused before anything is unpacked.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# scripts/ is not a package: these are developer tools first and libraries
# second (same arrangement as tests/test_scripts_basemap.py).
sys.path.insert(0, str(ROOT / "scripts"))

import vendor_font  # noqa: E402


def test_the_shipped_font_matches_its_manifest() -> None:
    assert vendor_font.check_shipped() == 0


def test_the_recipe_and_the_manifest_record_the_same_source() -> None:
    manifest = (ROOT / "web" / "fonts" / "MANIFEST.md").read_text(encoding="utf-8")

    assert vendor_font.RELEASE_URL in manifest
    assert vendor_font.RELEASE_SHA256 in manifest
    assert vendor_font.MEMBER_SHA256 in manifest


def test_the_subset_keeps_the_flags_the_ui_depends_on(tmp_path: Path) -> None:
    argv = vendor_font.subset_argv(tmp_path / "in.ttf", tmp_path / "out.woff2")

    assert "--flavor=woff2" in argv
    # Tabular figures in the price and walk-time columns, and the cross-street
    # arrow the result cards draw.
    assert "--layout-features+=tnum" in argv
    assert any(part.startswith("--unicodes=") and "U+2190-2193" in part for part in argv)


def test_an_archive_that_is_not_the_recorded_release_is_refused(tmp_path: Path) -> None:
    """The hash is checked before the zip is opened, so a swapped release cannot ship."""
    impostor = tmp_path / "Inter-4.1.zip"
    impostor.write_bytes(b"PK\x03\x04 not the release")

    assert vendor_font.rebuild(impostor) == 1
