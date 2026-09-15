"""Static checks on the frontend that no unit test of the Python code would catch.

Three rules, all of them security rules from SPEC §3.4 and STYLE_GUIDE §4:
the page loads nothing from the network, no script builds markup from data, and
the basemap style points only at paths this server serves. They are cheap to
check by reading the files, and expensive to notice by hand in review.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
INDEX_HTML = WEB_DIR / "index.html"
STYLE_JSON = WEB_DIR / "basemap" / "style.json"

URL_ATTRIBUTES = ("src", "href", "action", "data", "poster", "srcset")
REMOTE_SCHEME = re.compile(r"^\s*(?:https?:)?//", re.IGNORECASE)

# SPEC §3.3/§3.4 and STYLE_GUIDE §4: no code from data, no markup from data.
FORBIDDEN_JS = ("innerHTML", "outerHTML", "eval(", "new Function", "document.write")


class _Links(HTMLParser):
    """Collect (tag, attributes-dict) for every element carrying a URL attribute."""

    def __init__(self) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: (value or "") for name, value in attrs}
        if any(name in attributes for name in URL_ATTRIBUTES):
            self.elements.append((tag, attributes))


def app_js_files() -> list[Path]:
    """Every frontend script we wrote. `web/vendor/` is third-party and excluded."""
    return sorted(
        path for path in WEB_DIR.rglob("*.js") if "vendor" not in path.relative_to(WEB_DIR).parts
    )


def test_index_html_loads_nothing_from_the_network() -> None:
    parser = _Links()
    parser.feed(INDEX_HTML.read_text(encoding="utf-8"))
    assert parser.elements, "index.html has no elements with URL attributes; did the parse fail?"

    for tag, attributes in parser.elements:
        for name in URL_ATTRIBUTES:
            url = attributes.get(name)
            if url is None or not REMOTE_SCHEME.match(url):
                continue
            # The one allowed exception: a visible attribution link the user
            # clicks. It must not hand the target a window reference or a
            # referrer (SPEC §3.4 keeps the destination address private).
            rel = attributes.get("rel", "").lower().split()
            assert tag == "a", f"<{tag} {name}={url!r}> loads a remote resource"
            assert "noopener" in rel and "noreferrer" in rel, (
                f'remote link {url!r} must carry rel="noopener noreferrer"'
            )


def test_index_html_loads_the_pmtiles_global_before_the_module() -> None:
    """pmtiles.js is a classic script; map.js needs its global at import time."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    classic = html.index("vendor/pmtiles/pmtiles.js")
    module = html.index('type="module"')
    assert classic < module


@pytest.mark.parametrize("path", app_js_files(), ids=lambda path: path.name)
def test_frontend_scripts_never_build_markup_or_code_from_data(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    for pattern in FORBIDDEN_JS:
        # The ESLint config bans these too, but ESLint is not installed here and
        # sign text is attacker-controlled, so the rule gets a test of its own.
        assert pattern not in source, f"{path.name} contains {pattern!r}"


def test_basemap_style_points_only_at_local_paths() -> None:
    style = json.loads(STYLE_JSON.read_text(encoding="utf-8"))
    assert style["glyphs"].startswith("/basemap/")
    assert style["sprite"].startswith("/basemap/")
    for name, source in style["sources"].items():
        assert source["url"].startswith("pmtiles:///basemap/"), f"source {name} is not local"
        assert "tiles" not in source, f"source {name} lists remote tile URLs"

    # Nothing anywhere else in the file may point off-host either: the
    # attribution string is the usual place a remote link sneaks back in.
    for match in re.finditer(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\"\s]+", STYLE_JSON.read_text()):
        assert match.group(0).startswith("pmtiles:///basemap/"), match.group(0)


def test_basemap_directory_holds_every_asset_the_style_asks_for() -> None:
    style = json.loads(STYLE_JSON.read_text(encoding="utf-8"))
    sprite = WEB_DIR / style["sprite"].lstrip("/")
    assert sprite.with_suffix(".json").is_file()
    assert sprite.with_suffix(".png").is_file()

    fonts_dir = WEB_DIR / "basemap" / "fonts"
    stacks = {path.name for path in fonts_dir.iterdir() if path.is_dir()}
    assert stacks, "no glyph directories vendored"
    for stack in stacks:
        assert (fonts_dir / stack / "0-255.pbf").is_file()


def test_unparsed_warning_is_reserved_for_signs_the_parser_failed_on() -> None:
    """SPEC §11's amber "could not read it" must not fire on a sign that was read.

    `/api/segment` lists every sign on the parent centerline segment, so the
    detail panel meets signs carrying no rule on *this* stretch for two reasons
    that are not parser failures: a non-regulation panel (D10), and a sign
    governing the other side or another span. D13 gives every sign in this
    stretch's own stack a `regulation` row, unparsed ones included, so the
    no-rule branch of `signCard` can never be a parser failure.
    """
    detail = (WEB_DIR / "detail.js").read_text(encoding="utf-8")
    copy = (WEB_DIR / "copy.js").read_text(encoding="utf-8")

    for name in ("PANEL_STATES_NO_RULE", "SIGN_NOT_ON_THIS_STRETCH"):
        assert f"export const {name}" in copy, f"copy.js lost {name}"
        assert name in detail, f"detail.js no longer uses {name}"

    # UNPARSED_RULE survives in exactly one place: `ruleBlock`, which only ever
    # sees a rule whose own `parse_method` says `unparsed`.
    assert detail.count("UNPARSED_RULE") == 2, "UNPARSED_RULE is used outside ruleBlock"
    assert 'entry.parse_method === "unparsed"' in detail


def test_the_status_line_is_built_from_the_server_counts() -> None:
    """docs/VALIDATION.md U1: `results` can be a capped subset of what is in radius.

    Counting the rows the server sent would restate the cap as a fact about the
    neighbourhood, which is the false confidence SPEC §11 exists to prevent.
    """
    app = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    formats = (WEB_DIR / "format.js").read_text(encoding="utf-8")

    assert "statusLine(response.counts" in app, "app.js no longer uses the server counts"
    assert "export function statusLine" in formats


def test_the_grey_state_distinguishes_a_data_gap_from_a_matching_gap() -> None:
    """docs/VALIDATION.md §5: "no signs here" is a false statement about unmatched curb."""
    copy = (WEB_DIR / "copy.js").read_text(encoding="utf-8")
    detail = (WEB_DIR / "detail.js").read_text(encoding="utf-8")
    index = INDEX_HTML.read_text(encoding="utf-8")

    assert "no_signs:" in copy and "unmatched_signs:" in copy
    assert "could not place them" in copy
    assert detail.count("noDataExplanation") >= 2, "detail.js no longer branches on gap_kind"
    assert "means no data, not no restriction" in index, "the legend lost the no-data sentence"


def test_the_glyph_ranges_the_validation_run_404ed_on_are_vendored() -> None:
    """docs/VALIDATION.md U2: every map load asked for these and got a 404.

    U+0301 and U+0306 (768-1023) and U+1EA1/U+1ED9/U+1EDF (7680-7935) are in
    Manhattan's accented and Vietnamese labels; 8192-8447 holds the en dash that
    street labels themselves use (decision D16(c)).
    """
    fonts_dir = WEB_DIR / "basemap" / "fonts"
    stacks = [path for path in fonts_dir.iterdir() if path.is_dir()]
    assert stacks, "no glyph directories vendored"
    for stack in stacks:
        for glyph_range in ("768-1023", "7680-7935", "8192-8447"):
            assert (stack / f"{glyph_range}.pbf").is_file(), f"{stack.name} lacks {glyph_range}"


def test_a_failed_search_clears_the_previous_answer() -> None:
    """Results for the old destination left under an error banner read as the answer.

    There is no JS test runner here (STYLE_GUIDE §4: no bundler, no framework),
    so this checks the wiring by reading the source: the error path has to call
    `clearResults`, and `clearResults` has to empty both the list and the map.
    """
    app = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    map_js = (WEB_DIR / "map.js").read_text(encoding="utf-8")

    error_handler = app.split("function handleSearchError")[1].split("\n}")[0]
    assert "clearResults()" in error_handler, "a failed search keeps the previous results"
    clear_results = app.split("function clearResults()")[1].split("\n}")[0]
    for expected in ("state.results = []", "clear(dom.results)", "curbMap.clearResults()"):
        assert expected in clear_results, f"clearResults no longer does {expected}"
    assert "clearResults()" in map_js


def test_an_armed_pin_with_no_click_refuses_instead_of_reusing_the_old_pin() -> None:
    """`dropPin` disarms pin mode, so still-armed means no pin was placed.

    Searching then would answer about the previous destination, which the user
    has already said they are leaving. Checked by reading the source for the
    same reason as the test above: there is no JS test runner here.
    """
    app = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    copy = (WEB_DIR / "copy.js").read_text(encoding="utf-8")

    submit = app.split("function onSubmit")[1].split("\n}")[0]
    refusal = submit.split('if (address === "" && state.pinMode)')
    assert len(refusal) == 2, "onSubmit no longer refuses an armed pin with no destination"
    assert "PIN_MODE_NEEDS_A_CLICK" in refusal[1]
    assert "return" in refusal[1].split("}")[0]
    assert "export const PIN_MODE_NEEDS_A_CLICK" in copy
    # The guard has to come before the search runs, not after it.
    assert submit.index("state.pinMode") < submit.index("runSearch(")
