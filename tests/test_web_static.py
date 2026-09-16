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

from curbcheck.engine.resolve import (
    ABSENCE_CAVEAT,
    NO_DATA_CAVEAT,
    PARTIAL_ABSENCE_CAVEAT,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = REPO_ROOT / "web"
INDEX_HTML = WEB_DIR / "index.html"
STYLE_JSON = WEB_DIR / "basemap" / "style.json"
# The static site's overlay (docs/STATIC_SITE.md): a second frontend, loaded
# from the same origin by the same page, so the same two rules apply to it.
SITE_STATIC_DIR = REPO_ROOT / "site" / "static"

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

# A remote URL written into a script: an absolute `http(s)://` or a
# protocol-relative `//host/…` inside a string literal. SPEC §3.4 and
# docs/STATIC_SITE.md T6: every file either frontend loads is same-origin.
JS_REMOTE_URL = re.compile(r"""['"`](?:https?:)?//[^'"`\s]""")


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


def frontend_js_files() -> list[Path]:
    """`web/` plus the static site's overlay.

    `site/static/` is globbed rather than listed: it lands module by module, and
    a rule that only starts applying once the last file arrives is a rule that
    never caught anything.
    """
    return [*app_js_files(), *sorted(SITE_STATIC_DIR.rglob("*.js"))]


def js_id(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


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


@pytest.mark.parametrize("path", frontend_js_files(), ids=js_id)
def test_frontend_scripts_never_build_markup_or_code_from_data(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    for pattern in FORBIDDEN_JS:
        # The ESLint config bans these too, but ESLint is not installed here and
        # sign text is attacker-controlled, so the rule gets a test of its own.
        assert pattern not in source, f"{js_id(path)} contains {pattern!r}"


@pytest.mark.parametrize("path", frontend_js_files(), ids=js_id)
def test_frontend_scripts_name_no_remote_url(path: Path) -> None:
    """The one remote URL in the repository is the visible attribution anchor in
    index.html. No script fetches anything that is not same-origin — on the
    static copy that is the whole of what keeps a destination off the network."""
    match = JS_REMOTE_URL.search(path.read_text(encoding="utf-8"))
    assert match is None, f"{js_id(path)} names a remote URL: {match.group(0)!r}"


def test_the_remote_url_pattern_catches_what_it_is_for() -> None:
    """A pattern that never fires is not a check. These are the shapes it must catch."""
    for bad in (
        'fetch("https://example.com/tiles")',
        "import(`//cdn.example.com/x.js`)",
        "element.src = 'http://example.com/pixel.gif'",
    ):
        assert JS_REMOTE_URL.search(bad), bad
    for good in (
        'const STYLE_URL = "/basemap/style.json";',
        'url: "pmtiles:///basemap/manhattan.pmtiles"',
        'const done = "";\n// a trailing comment',
    ):
        assert not JS_REMOTE_URL.search(good), good


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

    # UNPARSED_RULE reaches the page from exactly two branches, `unreadableReason`
    # and `ruleBlock`, and both of them key on the rule's own `parse_method`.
    assert detail.count("UNPARSED_RULE") == 3, "UNPARSED_RULE is used outside those two branches"
    assert detail.count('entry.parse_method === "unparsed"') == 2

    # A meta panel ("METERS ARE NOT IN EFFECT ABOVE TIMES") has placeholder hours
    # too, and rendering them produced "Means: no parking at any time · Applies to
    # your window" under a sign that says no such thing.
    reason = detail.split("function unreadableReason(entry)")[1].split("\n}")[0]
    assert "flags.meta" in reason and "META_RULE" in reason
    assert "export const META_RULE" in copy
    for renderer in ("function signReadings", "function ruleBlock", "function absenceSentence"):
        body = detail.split(renderer)[1].split("\n}\n")[0]
        assert "unreadableReason(" in body, f"{renderer} can render an unreadable rule as English"


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
    assert "view.counts[verdict]" in results, "the verdict groups no longer count from counts"


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

    # The chip keeps the outlined, unfilled treatment reserved for absence. It no
    # longer carries a fifth verdict *word*: "No rule in effect" on the chip over
    # "No posted rule covers this window" as the headline over "none is in effect
    # during your window" as the sentence was one fact said three times, which is
    # what the owner read as "still not quite clear enough". The distinction now
    # lives in the treatment and in the "why" line, which says it once.
    chip = formats.split("export function verdictChip")[1].split("\n}")[0]
    assert '"verdict-absence"' in chip and 'result.basis === "absence"' in chip
    assert "That is not a permission" in copy
    detail = (WEB_DIR / "detail.js").read_text(encoding="utf-8")
    assert "ABSENCE_IS_NOT_A_PERMISSION" in detail, "the absence sentence left the panel"
    assert detail.count("ABSENCE_IS_NOT_A_PERMISSION") == 2, "it is said more than once again"
    # And it is said on the absence branch of the "why" line, nowhere else.
    why = detail.split("function whyLine(result, detail, window)")[1].split("\n}")[0]
    assert 'result.basis === "absence"' in why
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
    assert set(blocks) == {"legal", "ambiguous", "illegal", "no_data"}, (
        "VERDICT_STYLE lost a verdict"
    )
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
        body.index("verdictBlock(result, detail, view.window)"),
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
    group = results.split("function groupSection(verdict, view, cards)")[1].split("\nfunction ")[0]
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


def test_nothing_paints_white_text_on_the_dark_scheme_accent() -> None:
    """--c-accent is a deep blue in light mode and a pale blue in dark mode.

    A hard-coded `#fff` label therefore measures 7:1 in one scheme and 2:1 in
    the other. The inverse token flips with the scheme, so it is the only thing
    allowed on an accent fill.
    """
    for name in ("components.css", "styles.css"):
        css = (WEB_DIR / name).read_text(encoding="utf-8")
        for rule in css.split("}"):
            if "var(--c-accent)" not in rule or "background" not in rule:
                continue
            assert "color: #fff" not in rule and "color: white" not in rule, (
                f"{name} puts fixed white on an accent fill:\n{rule}"
            )


def test_a_panel_swatch_follows_the_colour_scheme_and_the_map_does_not() -> None:
    """DESIGN_DIRECTION §6: the panels go dark, the basemap stays light.

    So a chip or legend swatch has to take the *token* — which flips — while the
    map layer takes the literal light hue, or the two schemes would swap the
    contrast guarantees each one was measured for.
    """
    verdicts = (WEB_DIR / "verdicts.js").read_text(encoding="utf-8")
    swatch = verdicts.split("export function swatchBackground")[1]
    assert "var(${style.token})" in swatch, "panel swatches no longer follow the scheme"
    assert "style.color" not in swatch, "a panel swatch is using the map's fixed hue"
    for token in ("--v-legal", "--v-ambiguous", "--v-illegal", "--v-nodata"):
        assert f'token: "{token}"' in verdicts, f"VERDICT_STYLE lost {token}"


def test_a_reversed_pin_says_near_once() -> None:
    """`/api/reverse` already labels the nearest door "near 1519 3 AVE".

    Prefixing the word again put "near near 1519 3 AVE" in the destination box
    and in the collapsed search summary, which is the one line naming the curb
    every verdict on the page is about (docs/API.md, `GET /api/reverse`).
    """
    fmt = (WEB_DIR / "format.js").read_text(encoding="utf-8")
    assert "export function nearLabel" in fmt, "the near-once helper is gone"
    helper = fmt.split("export function nearLabel")[1].split("\n}")[0]
    assert "/^near\\s/i" in helper, "nearLabel stopped checking for the word it adds"

    app = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    drop = app.split("async function dropPin")[1].split("\n}")[0]
    assert "nearLabel(place.label)" in drop, "dropPin is wording the label itself again"
    assert "`near ${place.label}`" not in drop


def test_a_place_name_is_title_cased_for_display_and_kept_raw_beside_it() -> None:
    """The cards have been title-cased since `streetLabelParts`; the box was not.

    CSCL, AddressPoint and CommonPlace store every name in capitals, so the
    destination field and the collapsed search summary — the one line naming the
    curb every verdict on the page is about — read `1519 3 AVE` next to cards
    reading `E 85 St · north side`. Display only: the geocoder's own string is
    kept as the tooltip, and nothing in a request or an "as posted" block moves
    (SPEC §10).
    """
    fmt = (WEB_DIR / "format.js").read_text(encoding="utf-8")
    assert "export function placeLabel" in fmt
    place = fmt.split("export function placeLabel")[1].split("\n}")[0]
    assert "titleCaseStreet" in place, "placeLabel stopped using the cards' own formatter"

    app = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    pick = app.split("function pickCandidate(candidate)")[1].split("\n}")[0]
    assert "placeLabel(candidate.label)" in pick
    assert "raw: candidate.label" in pick, "the published string is no longer kept"
    assert "dom.destination.value = candidate.label" not in pick

    autocomplete = (WEB_DIR / "autocomplete.js").read_text(encoding="utf-8")
    assert "placeLabel(candidate.label)" in autocomplete, "the dropdown still shouts"
    assert "title: candidate.label" in autocomplete, "the row dropped its raw label"

    card = (WEB_DIR / "searchcard.js").read_text(encoding="utf-8")
    collapse = card.split("collapse(destinationLabel")[1].split("\n    },")[0]
    assert "As published:" in collapse, "the summary no longer offers the raw label"


def test_a_floating_sheet_never_stands_on_the_attribution_or_the_legend() -> None:
    """The detail and About sheets float over the map's right edge.

    That edge is where the OpenStreetMap credit, the About & data button, the
    Recentre control and MapLibre's own zoom buttons live, and the bottom-left
    is where the legend carries the sentence that stops an empty curb reading as
    a safe one (SPEC §11). Measured at 1440, 1280 and 1024 the open sheet made
    every one of them unreadable and unclickable — `elementFromPoint` returned
    the sheet. The sheet now stops one band short of the bottom, and the pieces
    in its column step aside by the sheet's own width.
    """
    styles = (WEB_DIR / "styles.css").read_text(encoding="utf-8")
    components = (WEB_DIR / "components.css").read_text(encoding="utf-8")
    app = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    assert "--sheet-offset: 0px" in styles, "the map area lost its sheet inset"
    assert 'setProperty("--sheet-offset"' in app, "nothing measures the open sheet"

    for selector in (".map-control {", ".legend {", ".maplibregl-ctrl-bottom-right {"):
        rule = styles.split(selector)[1].split("}")[0]
        assert "var(--sheet-offset)" in rule, f"{selector.strip(' {')} no longer dodges the sheet"

    # The credit keeps its corner; the sheet is what gets out of the way.
    sheet = components.split("\n.detail {")[1].split("}")[0]
    assert "bottom: calc(var(--s-6) + 30px)" in sheet, "the sheet covers the attribution band again"


def test_a_search_leaves_focus_on_the_answer_and_not_on_the_body() -> None:
    """The search card folds to one line when the answer arrives.

    That removes the control the keyboard was standing on — the Search button
    just pressed, or the destination field Enter was pressed in — so focus fell
    to `<body>`. Measured: the next Tab skipped both skip links and landed on a
    count pill, where Enter presses a verdict filter. `#results` carries
    `tabindex="-1"` for exactly this hand-off.
    """
    app = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    show = app.split("function showResponse(response, walkMinutes)")[1].split("\nfunction ")[0]
    assert "dom.form.contains(document.activeElement)" in show, "nothing notices where focus was"
    assert "dom.results.focus(" in show, "focus is dropped on the floor after a search"
    assert show.index("searchCard.collapse(") < show.index("dom.results.focus(")

    index = INDEX_HTML.read_text(encoding="utf-8")
    results = index.split('id="results"')[1].split(">")[0]
    assert 'tabindex="-1"' in results, "the results region can no longer take focus"


def test_the_ranked_list_says_what_it_is_not_showing() -> None:
    """Two silences measured in the rail, both of them about legal curb.

    A Midtown search at noon comes back 0 legal / 226 illegal / 6 no data, and
    the rail printed a "Results" heading over three collapsed group headers:
    nothing on screen said in words that there is nowhere legal to park, which
    is the question the page exists to answer.

    And `limit` caps the ranked legal list at 100, so "Show more" runs out while
    the pill above still reads 308 legal. The three verdict groups have carried
    an "N of M drawn" note since the rebuild; the shortlist carried none
    (UX_AUDIT (f) 7 — the UI says plainly when it is showing a subset).
    """
    copy = (WEB_DIR / "copy.js").read_text(encoding="utf-8")
    assert "export const NO_LEGAL_NEARBY" in copy
    assert "export const LEGAL_SUBSET_NOTE" in copy

    results = (WEB_DIR / "results.js").read_text(encoding="utf-8")
    render = results.split("export function renderResults")[1].split("\nfunction ")[0]
    assert "NO_LEGAL_NEARBY(view.walkMinutes)" in render, "a zero-legal answer is silent again"
    assert "LEGAL_SUBSET_NOTE(legal.length, legalTotal)" in render, "the cap is silent again"
    # The total is the server's count, never the rows that happened to arrive.
    assert "view.counts.legal" in render, "the subset note is counting its own list"


def test_the_reason_line_is_printed_as_the_engine_wrote_it() -> None:
    """The engine words `no_data` reasons by `gap_kind` (docs/API.md), so the UI
    must not keep a second, diverging sentence of its own."""
    copy = (WEB_DIR / "copy.js").read_text(encoding="utf-8")
    assert "UNMATCHED_SIGNS_REASON" not in copy

    fmt = (WEB_DIR / "format.js").read_text(encoding="utf-8")
    body = fmt.split("export function reasonLine")[1].split("\n}")[0]
    assert "result.reason" in body
    assert "gap_kind" not in body, "the client is rewriting the engine's reason again"

    # Both surfaces that print a reason go through it.
    for name in ("results.js", "detail.js"):
        source = (WEB_DIR / name).read_text(encoding="utf-8")
        assert "reasonLine(result)" in source, f"{name} prints the raw reason again"


def test_a_point_off_the_street_network_is_refused_as_coverage_not_as_arithmetic() -> None:
    """A pin can be dropped anywhere the map goes, including Staten Island.

    `/api/search` bounds `lat`/`lon` to a Manhattan-ish box, so a pin at
    40.597, -74.337 came back 422 `validation_error` and was reported as "that
    search does not add up — check the date, the time, and the walk radius",
    three things that were all fine. And the sentence under "CurbCheck covers
    Manhattan only" said the point was "outside the outlined area on the map"
    while the outline — the coverage *bounding box* from `/api/health` — visibly
    contains Hoboken, Jersey City and half of Queens.

    The box is a hint about where to look; the service-area test is 250 m from a
    street centerline (`docs/DECISIONS.md` D28) and stays the server's.
    """
    copy = (WEB_DIR / "copy.js").read_text(encoding="utf-8")
    assert "export const OUTSIDE_COVERAGE_DETAIL" in copy
    sentence = copy.split("export const OUTSIDE_COVERAGE_DETAIL")[1].split(";")[0]
    assert "outside the outlined area" not in sentence

    app = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    assert "outside the outlined area" not in app, "the sentence is back in the wiring"
    submit = app.split("function onSubmit(event)")[1].split("\n}")[0]
    assert "outsideCoverage(where)" in submit, "a pin off the box is sent as a search again"
    guard = app.split("function outsideCoverage(where)")[1].split("\n}")[0]
    assert "state.coverage" in guard, "the guard invented its own bounding box"


def test_the_suggestion_list_announces_how_long_it_is() -> None:
    """An ARIA 1.2 combobox keeps focus on the input.

    So a list appearing under it is silent to a screen reader until the user
    arrows into it, and there is no way to learn that four candidates arrived
    rather than one — or none, which matters most, because the "Drop a pin
    instead" row means the popup is never actually empty (UX_AUDIT (e) 7).
    """
    index = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="autocomplete-status"' in index
    region = index.split('id="autocomplete-status"')[1].split(">")[0]
    assert 'role="status"' in region and 'aria-live="polite"' in region
    assert "visually-hidden" in region, "the count is duplicated on screen"

    copy = (WEB_DIR / "copy.js").read_text(encoding="utf-8")
    assert "export const CANDIDATE_COUNT" in copy

    auto = (WEB_DIR / "autocomplete.js").read_text(encoding="utf-8")
    assert "announce(CANDIDATE_COUNT(found.length))" in auto, "the list stopped announcing itself"
    # Counted from the candidates, not from the rows: the pin row is always one
    # of those and would turn "no matches" into "1 suggestion".
    assert "CANDIDATE_COUNT(rows.length)" not in auto
    close = auto.split("  function close()")[1].split("\n  }")[0]
    assert 'announce("")' in close, "the announcement outlives the list it describes"


def test_the_panel_answers_the_window_and_names_the_rule_that_decided_it() -> None:
    """The owner's complaint: four restatements of the verdict, no window, no sign hours.

    The panel's first block is now chip, headline, "why". The "why" line has to
    print the window the user asked about and the rule the engine decided on,
    and it can only get the second from `/api/segment`, which only answers it
    when the window is passed (docs/API.md).
    """
    detail = (WEB_DIR / "detail.js").read_text(encoding="utf-8")
    api = (WEB_DIR / "api.js").read_text(encoding="utf-8")
    app = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    block = detail.split("function verdictBlock(result, detail, window)")[1].split("\n}")[0]
    order = [
        block.index("verdictChipNode(result)"),
        block.index("HEADLINE[key]"),
        block.index("whyLine(result, detail, window)"),
    ]
    assert order == sorted(order), "the verdict block no longer reads chip, answer, why"

    why = detail.split("function whyLine(result, detail, window)")[1].split("\n}")[0]
    assert "YOUR_WINDOW(windowSentence(window.start, window.end))" in why, (
        "the why line stopped printing the window it is a claim about"
    )
    assert "decidingRule" in detail and "entry.deciding === true" in detail

    # The window has to reach both of them: the panel renders it, the segment
    # call is what makes the server resolve which rule decided.
    assert "api.segment(regSegId, state.window)" in app
    assert "window: state.window" in app
    assert "t1: window.t1" in api and "t2: window.t2" in api


def test_a_caveat_is_dropped_only_when_it_restates_the_verdict() -> None:
    """UX_AUDIT (f) 5: caveats are never hidden — but a fourth restatement is not a caveat.

    "Before you park" opened with "Part of this window has no posted rule; read
    the curb" under a chip and a headline that both already said so, which
    teaches the driver to skip the box and with it the temporary-signage
    warning. Only the sentences the "why" line has itself said are filtered, and
    the filter matches `curbcheck/engine/resolve.py` verbatim.
    """
    copy = (WEB_DIR / "copy.js").read_text(encoding="utf-8")
    detail = (WEB_DIR / "detail.js").read_text(encoding="utf-8")

    block = copy.split("NOT_ADDED_BY_A_CAVEAT = new Set([")[1].split("]);")[0]
    dropped = set(re.findall(r'^  "(.+?)",$', block, re.MULTILINE))
    # Byte-for-byte the engine's own sentences. A rewording on either side that
    # does not reach the other silently unfilters a duplicate, which is the bug
    # this whole pass is about.
    assert dropped == {ABSENCE_CAVEAT, *NO_DATA_CAVEAT.values()}

    # Everything else survives, including the five that carry real news.
    for keeper in (
        PARTIAL_ABSENCE_CAVEAT,
        "School-day rule assumed active.",
        "A sign here marks itself temporary.",
        "Street cleaning is suspended on this date.",
        "Meters are not in effect for part of this window.",
    ):
        assert keeper not in copy, f"copy.js filters out {keeper!r}, which adds information"

    assert "informativeCaveats(caveats)" in detail
    caveats = detail.split("function caveatBlock(caveats)")[1].split("\n}")[0]
    assert "collapsible" not in caveats, "the caveat block became a disclosure"


def test_one_verdict_vocabulary_reaches_the_chip_the_pills_the_groups_and_the_legend() -> None:
    """UX_AUDIT (f) 2 keeps four verdicts; nothing says one of them in two words.

    The chip read "Legal", the group heading "Legal", the legend "Legal" and the
    absence chip "No rule in effect" — four labels for a state with one meaning,
    none of them the driver's question. `verdictInfo` is now the single source,
    and every surface reads it rather than keeping a table of its own.
    """
    formats = (WEB_DIR / "format.js").read_text(encoding="utf-8")
    results = (WEB_DIR / "results.js").read_text(encoding="utf-8")
    legend = (WEB_DIR / "legend.js").read_text(encoding="utf-8")

    table = formats.split("const VERDICT_INFO = {")[1].split("\n};")[0]
    labels = dict(re.findall(r"(\w+): \{ label: \"(.+?)\" \}", table))
    assert labels == {
        "legal": "Can park",
        "illegal": "Can't park",
        "ambiguous": "Unclear",
        "no_data": "No data",
    }

    assert "verdictInfo(verdict).label" in legend, "the legend keeps its own words"
    assert "verdictInfo(verdict).label.toLowerCase()" in results, "the pills keep their own words"
    assert "label: verdictInfo(verdict).label," in results, "the groups keep their own words"
    assert 'const GROUPS = ["ambiguous", "illegal", "no_data"]' in results


def test_identical_sign_texts_are_quoted_once_and_say_where_the_posts_are() -> None:
    """SPEC §11 keeps the verbatim text; nothing says it has to be printed six times.

    W 24 ST south side carries six posts of `NO PARKING MONDAY-FRIDAY 8AM-6PM`,
    which the panel quoted in six identical bordered blocks. Grouping is allowed
    and deleting is not (UX_AUDIT (f) 4), so every post is still accounted for —
    in the line that says where they are.
    """
    detail = (WEB_DIR / "detail.js").read_text(encoding="utf-8")
    copy = (WEB_DIR / "copy.js").read_text(encoding="utf-8")

    groups = detail.split("function signGroups(signs, rules = null)")[1].split("\n}")[0]
    assert "sign.sign_description" in groups, "signs are no longer grouped on their text"

    card = detail.split("function signCard(group, rules")[1].split("\n}")[0]
    # The verbatim text still comes first, and still goes through textContent.
    assert card.index("as-posted-eyebrow") < card.index('className: "as-posted"')
    assert card.index('className: "as-posted"') < card.index("signReadings(")
    assert "postedAt(group.map((post) => post.distance_ft))" in card
    assert "export function postedAt" in copy


def test_each_quoted_sign_says_what_it_means_and_whether_it_is_on() -> None:
    """Question 2 for a driver holding a sign in their eyeline: does this one apply to me?"""
    detail = (WEB_DIR / "detail.js").read_text(encoding="utf-8")
    formats = (WEB_DIR / "format.js").read_text(encoding="utf-8")

    readings = detail.split("function signReadings(entries, governing)")[1].split("\n}\n")[0]
    assert "describeRegulation(entry.regulation)" in readings
    assert "inEffectLabel(entry.in_effect)" in readings
    # Only on the signs that govern this stretch: answering "applies to your
    # window" about a sign that does not govern the curb is P0-2 again.
    assert "governing ? inEffectLabel" in readings

    labels = formats.split("const IN_EFFECT_LABEL = {")[1].split("\n};")[0]
    assert "Applies to your window" in labels and "Not in effect for your window" in labels
    # `null` is the unreadable sign, and has no label: it must not read as "off".
    assert "IN_EFFECT_LABEL[inEffect] || null" in formats
