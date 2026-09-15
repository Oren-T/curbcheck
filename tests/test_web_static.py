"""Static checks on the frontend that no unit test of the Python code would catch.

Three security rules from SPEC §3.4 and STYLE_GUIDE §4: the page loads nothing
from the network, no script builds markup from data, and the basemap style
points only at paths this server serves.

Then the safety wiring the redesign rests on (`docs/ux/UX_AUDIT.md` (f)): the
advisory sentences are on the page, the grey verdict never prints a price, a
confidence figure is gated on `confidence_shown`, an error clears the previous
answer, and the counts come from the server. There is no JS test runner here
(STYLE_GUIDE §4: no bundler, no framework), so these read the source for the
call that has to be there — a coarse check that still fails loudly when the
wiring is deleted.
"""

from __future__ import annotations

import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
INDEX_HTML = WEB_DIR / "index.html"
STYLE_JSON = WEB_DIR / "basemap" / "style.json"

# Every module the page loads, as index.html and the imports expect them. A new
# module is fine; a missing one means the page 404s a script and renders blank.
EXPECTED_MODULES = {
    "about.js",
    "api.js",
    "app.js",
    "autocomplete.js",
    "copy.js",
    "detail.js",
    "dom.js",
    "drawer.js",
    "format.js",
    "legend.js",
    "map.js",
    "rank.js",
    "results.js",
    "searchcard.js",
    "states.js",
    "verdicts.js",
}
EXPECTED_STYLESHEETS = ("tokens.css", "styles.css", "components.css")
FONTS_DIR = WEB_DIR / "fonts"
FONT_MANIFEST = FONTS_DIR / "MANIFEST.md"

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

    assert "statusLine(state.counts" in app, "app.js no longer uses the server counts"
    assert "export function statusLine" in formats
    # The group headings are the other place a count is stated, and they read
    # `counts` too rather than the length of the rows that arrived.
    results = (WEB_DIR / "results.js").read_text(encoding="utf-8")
    assert "view.counts[group.key]" in results, "the verdict groups no longer count from counts"


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

    chooser = app.split("function destinationFor")[1].split("\n}")[0]
    assert "!state.pinMode" in chooser, "an armed pin no longer blocks the stale destination"

    submit = app.split("function onSubmit")[1].split("\n}")[0]
    assert "PIN_MODE_NEEDS_A_CLICK" in submit and "NEEDS_DESTINATION" in submit
    assert "export const PIN_MODE_NEEDS_A_CLICK" in copy
    # The refusal has to come before the search runs, not after it.
    assert submit.index("PIN_MODE_NEEDS_A_CLICK") < submit.index("runSearch(")


def test_the_page_loads_every_module_and_stylesheet_it_has() -> None:
    """A renamed module that index.html still lists is a blank page, not a bug report."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    on_disk = {path.name for path in app_js_files()}
    assert on_disk == EXPECTED_MODULES, f"module set changed: {on_disk ^ EXPECTED_MODULES}"

    for stylesheet in EXPECTED_STYLESHEETS:
        assert f'href="./{stylesheet}"' in html, f"index.html does not link {stylesheet}"
    # tokens.css defines the custom properties the other two read, so it is
    # linked first; the cascade would otherwise resolve them to nothing.
    assert html.index("tokens.css") < html.index("styles.css") < html.index("components.css")


def test_the_stylesheets_load_no_remote_asset() -> None:
    """SPEC §3.4: no web font, no CDN, no remote image — the CSP is the backstop."""
    for path in sorted(WEB_DIR.glob("*.css")):
        source = path.read_text(encoding="utf-8")
        assert "@import" not in source, f"{path.name} imports another stylesheet"
        for match in re.finditer(r"url\(([^)]*)\)", source):
            url = match.group(1).strip("\"' ")
            assert not REMOTE_SCHEME.match(url), f"{path.name} loads {url}"
            assert not url.lower().startswith("http"), f"{path.name} loads {url}"


def test_the_advisory_strip_carries_both_load_bearing_sentences() -> None:
    """UX_AUDIT (f) 1: the §17 text may be shortened on screen, never removed.

    The strip is what every user reads, so the two sentences that change a
    decision are in the markup, and the full text is one disclosure away and
    repeated in every detail sheet.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")
    copy = (WEB_DIR / "copy.js").read_text(encoding="utf-8")
    detail = (WEB_DIR / "detail.js").read_text(encoding="utf-8")

    assert "Advisory only. Read the posted sign." in html
    assert "Grey means no data, not no restriction." in html
    for clause in (
        "Always read the posted sign before parking.",
        "15 feet of a fire hydrant",
        "A blank or grey curb means no data, not no restriction.",
    ):
        assert clause in html, f"the full notice lost {clause!r}"
    # copy.js wraps the same paragraph across source lines, so the clauses are
    # matched on the fragments that survive the wrapping.
    for clause in (
        "Always read the posted sign before parking.",
        "fire hydrant",
        "no data, not no restriction",
    ):
        assert clause in copy, f"copy.js DISCLAIMER lost {clause!r}"

    # The strip is a disclosure, not a dismissal: there is no close control and
    # no persisted "never show again".
    assert 'id="notice-toggle"' in html and 'aria-expanded="false"' in html
    assert "localStorage" not in (WEB_DIR / "app.js").read_text(encoding="utf-8")
    # And the same text is inside every verdict.
    assert "DISCLAIMER" in detail, "the detail sheet no longer prints the full notice"


def test_a_no_data_span_has_no_price_rendering_path() -> None:
    """UX_AUDIT P0-5: the grey verdict asserted `Money — no meter` on unknown curb.

    `priceLabel` is the single place a price string is built, and it returns
    null — print nothing — before it can reach the money fields on a `no_data`
    span or on legality by absence.
    """
    formats = (WEB_DIR / "format.js").read_text(encoding="utf-8")
    body = formats.split("export function priceLabel")[1].split("\n}")[0]
    guard = body.index('key === "no_data"')
    assert guard < body.index("result.money"), "no_data reaches the money fields"
    assert 'result.basis === "absence"' in body, "legality by absence prices itself"
    assert body.index('result.basis === "absence"') < body.index("result.money")
    # "Free" is only ever said about a span whose sign was actually read.
    assert 'result.basis === "posted" ? "Free' in body

    for name in ("results.js", "detail.js"):
        source = (WEB_DIR / name).read_text(encoding="utf-8")
        assert "priceLabel(" in source, f"{name} builds its own price string"
        assert "no meter" not in source, f"{name} says 'no meter' outside priceLabel"


def test_a_confidence_figure_is_gated_on_confidence_shown() -> None:
    """UX_AUDIT P0-1: "100% confidence" was printed over a stack that was empty.

    The server decides whether the number means anything (`confidence_shown`),
    and nothing in the UI prints a percentage without asking.
    """
    formats = (WEB_DIR / "format.js").read_text(encoding="utf-8")
    detail = (WEB_DIR / "detail.js").read_text(encoding="utf-8")
    results = (WEB_DIR / "results.js").read_text(encoding="utf-8")

    shown = formats.split("export function confidenceShown")[1].split("\n}")[0]
    assert "result.confidence_shown" in shown
    text = formats.split("export function confidenceText")[1].split("\n}")[0]
    assert "confidenceShown(result)" in text and "return null" in text

    assert "confidenceText(result)" in results, "the card prints confidence unguarded"
    assert "confidenceShown(result)" in detail, "the sheet prints confidence unguarded"


def test_legality_by_absence_is_never_worded_as_a_permission() -> None:
    """UX_AUDIT P0-1 and DECISIONS D27: absence of a rule is not a posted yes."""
    formats = (WEB_DIR / "format.js").read_text(encoding="utf-8")
    copy = (WEB_DIR / "copy.js").read_text(encoding="utf-8")
    rank = (WEB_DIR / "rank.js").read_text(encoding="utf-8")

    chip = formats.split("export function verdictChip")[1].split("\n}")[0]
    assert '"Nothing posted"' in chip and 'result.basis === "absence"' in chip
    assert "That is not a permission" in copy
    # And it never wins a ranking by having nothing to charge for.
    assert "basisRank" in rank and 'result.basis === "posted" ? 0 : 1' in rank


def test_the_map_draws_every_verdict_with_its_own_pattern_and_the_list_does_not_cap_it() -> None:
    """SPEC §11 and UX_AUDIT P0-6, P1-2.

    Four verdicts, four dash patterns, and the grey line is never the thinnest
    or the easiest to miss. The list caps at a shortlist; the map gets
    everything the server sent, because the U1 fix depends on the red and grey
    spans being drawn.

    The polish pass moved `illegal` to a thin, 0.65-opacity line with no white
    casing, which is what stopped 400 red candy stripes from burying the answer.
    That is a weight change and nothing else: the four hues, the four dash
    patterns and the grey floor below are all still the audited values.
    """
    map_js = (WEB_DIR / "map.js").read_text(encoding="utf-8")
    verdicts = (WEB_DIR / "verdicts.js").read_text(encoding="utf-8")
    app = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    style = verdicts.split("const VERDICT_STYLE = {")[1].split("\n};")[0]
    blocks = {
        verdict: style.split(f"{verdict}: {{")[1].split("},\n")[0]
        for verdict in ("legal", "ambiguous", "illegal", "no_data")
        if f"{verdict}: {{" in style
    }
    assert set(blocks) == {"legal", "ambiguous", "illegal", "no_data"}, "VERDICT_STYLE lost a verdict"
    assert "dash: [0, 2.2]" in blocks["no_data"], "the no_data line lost its dot pattern"
    assert "dash: null" in blocks["legal"], "the legal line stopped being solid"

    widths = {
        verdict: [float(value) for value in block.split("widths: [")[1].split("]")[0].split(",")]
        for verdict, block in blocks.items()
    }
    # Grey is never under 3 px and never thinner than green at any zoom stop:
    # the fix for P0-6 has to make absence more visible, not less.
    assert min(widths["no_data"]) >= 3.0, "the no_data line can be drawn under 3 px"
    assert all(
        grey >= green for grey, green in zip(widths["no_data"], widths["legal"], strict=True)
    ), "grey is thinner than green"
    # Grey is softened by opacity, never by width, and green stays fully opaque.
    assert "opacity: 1," in blocks["legal"]
    assert float(blocks["no_data"].split("opacity: ")[1].split(",")[0]) < 1.0

    # The casing is now selective. Amber keeps one (2.56:1 on the grey road
    # fill); red deliberately has none, and green has a glow instead.
    assert "segments-casing-" in map_js and "MAP_HALO" in map_js
    assert "halo: null" in blocks["illegal"], "the illegal line got its white casing back"
    assert "glow: {" in blocks["legal"], "the legal line lost its glow"
    assert "halo: {" in blocks["ambiguous"], "the ambiguous line lost the casing amber needs"

    setter = app.split("curbMap.setResults(")[1].split(")")[0]
    assert setter == "state.results", "the map is fed something other than every result"
    assert "slice" not in map_js.split("setResults(results)")[1].split("\n  }")[0]


def test_the_detail_sheet_is_a_labelled_dialog_that_leads_with_the_governing_signs() -> None:
    """UX_AUDIT P0-2 and P1-7: order is the fix, and focus has to reach it."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    detail = (WEB_DIR / "detail.js").read_text(encoding="utf-8")
    app = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    assert 'role="dialog"' in html and 'aria-labelledby="detail-title"' in html
    assert 'attrs: { id: "detail-title" }' in detail, "the sheet heading lost its id"

    body = detail.split("function body(view)")[1].split("\n}")[0]
    order = [
        body.index("verdictBlock(result)"),
        body.index("caveatBlock(result.caveats)"),
        body.index("governingSection("),
        body.index("otherSignsSection("),
        body.index("readingSection("),
        body.index("meterSection("),
        body.index("segmentSection("),
    ]
    assert order == sorted(order), "the detail sheet sections moved out of order"

    # Focus moves in on open and back to the card on close (UX_AUDIT (e) 4).
    assert "close.focus()" in app
    assert "state.lastFocused.focus()" in app
    assert 'event.key !== "Escape"' in app, "Escape no longer closes a sheet"


def test_every_scroll_region_owns_its_own_container() -> None:
    """UX_AUDIT P0-4: one shared document scroll made the phone build unusable."""
    styles = (WEB_DIR / "styles.css").read_text(encoding="utf-8")
    components = (WEB_DIR / "components.css").read_text(encoding="utf-8")

    page = styles.split("html,\nbody {")[1].split("}")[0]
    assert "overflow: hidden" in page, "the document can scroll again"
    for block, css in (("rail-scroll", styles), ("detail-scroll", components)):
        rule = css.split(f".{block} {{")[1].split("}")[0]
        assert "overflow-y: auto" in rule, f".{block} lost its own scroll container"


def test_the_vendored_font_matches_its_manifest() -> None:
    """The one web font is local, licensed, and hashed.

    SPEC §3.4 forbids a remote font, so Inter ships in the tree; the OFL forbids
    shipping it without its licence. Both are only true as long as the manifest
    describes the bytes that are actually here, which is what this checks.
    """
    recorded = {
        row[1].strip().strip("`"): (int(row[2]), row[3].strip().strip("`"))
        for row in (line.split("|") for line in FONT_MANIFEST.read_text().splitlines())
        if len(row) > 4 and row[1].strip().startswith("`")
    }
    assert recorded, "web/fonts/MANIFEST.md lists no files"
    assert "LICENSE.txt" in recorded, "the OFL text is not vendored with the font"

    for name, (size, digest) in recorded.items():
        path = FONTS_DIR / name
        assert path.is_file(), f"MANIFEST.md lists {name}, which is not in web/fonts/"
        data = path.read_bytes()
        assert len(data) == size, f"{name} is {len(data)} bytes, manifest says {size}"
        assert hashlib.sha256(data).hexdigest() == digest, f"{name} does not match its SHA-256"

    # A subset that grows past a couple of hundred KB stops being a subset.
    woff2 = next(path for path in FONTS_DIR.glob("*.woff2"))
    assert woff2.stat().st_size <= 150_000, f"{woff2.name} is over the 150 KB budget"
    assert woff2.name in recorded, f"{woff2.name} is not in the manifest"


def test_the_font_is_declared_once_and_served_from_this_origin() -> None:
    """`font-src` is absent from the CSP, so `default-src 'self'` is what allows it."""
    styles = (WEB_DIR / "styles.css").read_text(encoding="utf-8")
    assert styles.count("@font-face") == 1, "more than one @font-face to keep in sync"
    face = styles.split("@font-face {")[1].split("}")[0]
    assert 'url("./fonts/' in face, "the font is not loaded from web/fonts/"
    assert "font-weight: 100 900" in face, "the variable weight range was lost"


def test_a_verdict_filter_cannot_hide_a_count_or_empty_the_map() -> None:
    """The count pills are the legend and the filter, so they are a safety surface.

    UX_AUDIT (f) 3 and (f) 7: hiding a verdict is a press the user made and is
    allowed, but the number of spans that verdict has must stay on screen, and
    there is no sequence of presses that leaves the map blank — a blank map is
    the "nothing here" reading SPEC §11 exists to prevent.
    """
    app = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    results = (WEB_DIR / "results.js").read_text(encoding="utf-8")
    components = (WEB_DIR / "components.css").read_text(encoding="utf-8")

    toggle = app.split("function toggleVerdict(verdict)")[1].split("\n}")[0]
    assert "state.visibleVerdicts.size === 1" in toggle, "the last verdict can be hidden"
    assert toggle.index("size === 1") < toggle.index("delete(verdict)")

    # The counts are the server's, and the pill keeps printing its own.
    stats = results.split("export function renderStats")[1].split("\n}")[0]
    assert "view.counts[verdict]" in stats, "the pills stopped counting from the server"
    assert 'className: "pill-count"' in stats
    pressed_out = components.split('.stat-pill[aria-checked="false"] {')[1].split("}")[0]
    assert "display: none" not in pressed_out, "a pressed-out pill hides its own count"


def test_collapsed_verdict_groups_render_their_cards_on_expand() -> None:
    """IMPLEMENTATION_NOTES §11: ~500 hidden buttons cost DOM on every re-rank."""
    results = (WEB_DIR / "results.js").read_text(encoding="utf-8")
    group = results.split("function groupSection(group, view, cards)")[1].split("\nfunction ")[0]
    assert "const fill = () =>" in group, "the group body is not built lazily"
    assert "body.dataset.filled" in group, "expanding twice would duplicate the cards"
    # And the selection ring has to reach cards that did not exist a moment ago.
    assert "view.onGroupFilled()" in group
    app = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    assert "onGroupFilled: () => markSelected" in app


def test_a_new_search_dims_the_previous_answer_instead_of_clearing_it() -> None:
    """UX_AUDIT P1-9, and the other half of (f) 8.

    An *error* clears the previous answer, because stale results under a new
    destination read as the answer to the new question. A search in flight is
    the opposite case: nothing has failed, and a screen that empties for 2.6 s
    looks exactly like one that found nothing.
    """
    app = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    map_js = (WEB_DIR / "map.js").read_text(encoding="utf-8")

    run = app.split("async function runSearch")[1].split("\n}")[0]
    assert "setStale(true)" in run, "a second search no longer dims the first answer"
    assert "renderSkeleton" in run, "the first search lost its skeletons"
    assert run.index("state.results.length > 0") < run.index("renderSkeleton")

    # The map fades the spans; it must not drop them.
    stale = map_js.split("  setStale(stale) {")[1].split("\n  }")[0]
    assert "STALE_FADE" in stale and "clearResults" not in stale
    assert "const STALE_FADE = 0.35" in map_js, "the fade went to zero"
