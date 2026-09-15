# CurbCheck web UI — UX audit

Method: walked the real app (`curbcheck serve`, `data/curbcheck.sqlite`, 74,389 signs) in
Chromium at 1440×900, 1280×800 and 390×844 on 2026-09-15. Tasks: first load, address /
intersection / typo / out-of-borough / empty search, pin search, result interpretation,
all four verdict detail panels, weight sliders, error and edge states, keyboard and
accessibility-tree review, phone. Screenshots in `docs/ux/screens/`. Read-only: no app
code was changed.

## (a) Summary

CurbCheck is honest, and that honesty is the best thing about it — the disclaimer is
never dismissible, the four verdicts are never collapsed into two, every verdict is
paired with a word and a symbol as well as a colour, and the raw DOT sign text really is
there. What is missing is *editing*. The answer to "where do I park" arrives as 590 to
2,100 near-identical cards in a single 88,000-pixel column, ranked by a walk-time
difference of tenths of a minute, every one of them carrying the same four grey facts;
the top hundred all say "Legal · 100% confidence · no posted rule covers this window",
which is the app telling you it found *nothing* in its most confident voice. Open the
best-ranked stretch and the first thing you read under the green badge is
`NO STANDING ANYTIME`, with the sentence that neutralises it ("does not govern this
stretch") set in 12px grey below a nine-row table. The states that should shout are the
quietest: grey no-data curb is a 0.7px dotted grey line on a grey basemap and its first
list card is #537 of 590; a pin dropped in New Jersey returns "Nothing within that walk
radius. Try a longer walk" rather than "CurbCheck only covers Manhattan". On a phone the
product does not function: the map sits below all 590 cards, so tapping a result appears
to do nothing at all. The engine's caution is intact; the interface spends it.

## (b) Findings

Priority: **P0** safety or blocking · **P1** major friction · **P2** polish.
Evidence files are relative to `docs/ux/screens/`.

| id | finding | evidence | recommendation |
|---|---|---|---|
| P0-1 | "Legal, 100% confidence" is awarded where the parser found **no rule at all**. All 100 ranked legal results for 1519 3rd Ave, Wed 10:00–12:00 read `no posted rule covers this window` with `100% confidence`. Absence of evidence is rendered in the app's most confident voice. | `05-detail-legal-1440.png`, `04-result-cards-1440.png`, `23-detail-1280.png` | Split the green verdict in two: **"Permitted by a posted sign"** (a rule was read and it allows parking) vs **"No rule found for this window"** — a distinct, less-green treatment. Never print a confidence percentage on a verdict derived from absence; print "no sign governs this stretch in your window" instead. |
| P0-2 | The raw sign text nearest a green verdict contradicts it. Result #1 lists 11 sign cards; **9 do not govern the stretch**, and the two that do are 7th and 9th. The disclaimer that saves the reading is 12px grey under a nine-row table, in a panel 4,271px tall (5.4 screens). | `05-detail-legal-1440.png`, `06-detail-legal-signs-scrolled.png` | Two explicit groups, governing first: "**Signs that produced this verdict**" (expanded), then "Other signs posted on this block — these do not govern this stretch" (collapsed, count in the summary). Move the disclaiming sentence above the sign text, not below the metadata. |
| P0-3 | Out-of-coverage is reported as an empty result. A pin in West New York, NJ was accepted and answered "Nothing within that walk radius. Try a longer walk or a different time." Nothing in the UI ever says CurbCheck is Manhattan-only (only the `<title>` does). | `13-zero-results-hudson.png` | Mask or outline the covered area on the map; reject a destination outside it with "CurbCheck only has data for Manhattan. This point is in New Jersey." Never advise widening the walk radius when the cause is coverage. |
| P0-4 | **The phone build does not work.** After a search the document is 87,609px tall and `.map-area` starts at y≈87,289 — the map is below all 590 cards. Tapping a card opens the detail sheet 86,000px below the viewport: to the user, nothing happens, and the mandatory raw sign text is unreachable. | `21-mobile-tap-card-nothing-happens.png`, `22-mobile-map-and-detail-bottom.png`, `19-mobile-first-load-390.png` | Map first and sticky on phone; results as a draggable bottom drawer; detail as a full-screen sheet with its own close. Never let the two scroll in one document flow. |
| P0-5 | A grey "no data" span asserts a price: `Money — no meter`, `Meter running — not metered`, on curb the app admits it knows nothing about. Same string on all 590 cards in one search. | `09-detail-no-data.png`, `20-mobile-results-390.png` | On `no_data`, and wherever `price_known` is false, print **"Cost unknown"** — never "no meter". Reserve "Free — no meter" for a span with a rule that says so. |
| P0-6 | Grey is the least visible verdict on the map: `#6b7280`, 3.5px, dash `[0.2, 1.6]` (≈0.7px marks, 5.6px gaps) on a grey basemap, against 6px solid green. In the list the first `no_data` card is **#537 of 590**. The legend's "a blank or grey curb means no data" is contradicted by the rendering. | `10-map-state-1440.png`, `15-walk30-2100-results-map.png` | Render `no_data` as a wide, low-saturation **hatched** band that reads as "unsurveyed", at least as heavy as the legal line; surface a "N stretches with no data nearby" entry at the *top* of the results, not the bottom. |
| P0-7 | The time window cannot be read. `datetime-local` renders as `09/15/2026, 08:` at 1440, 1280 and 390 — minutes and AM/PM are clipped off. A user cannot confirm 8 AM vs 8 PM, and the window drives every verdict. | `02-datetime-clipped-1440.png`, `18-focus-ring.png`, `19-mobile-first-load-390.png` | Replace with a compact "Arrive / Leave" control that renders its own text ("Wed 16 Sep, 10:00 AM → 12:00 PM · 2 hrs") plus presets (Now, +1h, +2h, This evening, Tomorrow morning). Always echo the window in words next to the verdict. |
| P1-1 | The result list is a single 88,253px column of 590 cards (111 viewport-heights); a 30-minute walk search renders 2,100. Cards are 148px tall and near-identical, so ~5 are visible at once. | `04-result-cards-1440.png`, `15-walk30-2100-results-map.png` | Show the top 5–8 recommendations only, then "N more legal · N illegal · N unknown" as filter chips. Collapse cards to ~72px. Never put non-legal spans in the same ranked, numbered sequence. |
| P1-2 | Capped results vanish silently from the map: 839 stretches in radius, 590 drawn. The 249 undrawn ones look like curb with no data. | `03-results-address-1440.png` | Either draw every span in the radius (verdict layers are cheap) or state the cap on the map itself: "249 farther stretches not drawn." |
| P1-3 | The weight sliders do not re-rank. Setting Walking 0.0 / Money 3.0 / Risk 3.0 left the top five identical; only a fresh Search applies them. The walk slider *does* redraw the radius circle, so the map shows a 30-min circle over 10-min results while the status line still says "within a 10 min walk". | `14-weights-no-rerank.png` | Re-rank client-side on slider input (all inputs to the score are already in `results`), or disable the sliders until search and mark the form "changed — press Search". Never let the drawn radius disagree with the data. |
| P1-4 | The geocoder silently picks candidate #1 and reports it in a 12px grey hint ("Searching near 1519 3 AVE"). `docs/API.md` notes only 54% of segments publish address ranges, so many house numbers land on the nearest corner at confidence ≈0.5 — never shown. | `03-results-address-1440.png` | Echo the resolved address prominently next to the window, with "approximate — nearest corner" when confidence is low, and a "not this one?" affordance that reopens the candidate list. |
| P1-5 | Typo (`1519 3rd Avenu`), out-of-borough (`350 5th Av, Brooklyn`) and genuine non-matches all produce the same sentence with **zero suggestions**. | `11-error-address-not-found.png` | Fuzzy-match street names and always offer candidates; give out-of-borough its own message; keep "or drop a pin" as a visible button, not prose. |
| P1-6 | Clearing the destination box and pressing Search silently re-answers for the **previous** destination (583 results, empty field, "Searching near 40.778830, -73.953985"). The screen then describes a curb the user has said they are leaving. | — (reproduced; status line + empty field) | Clearing the destination must clear the answer. Require an explicit destination for every search. |
| P1-7 | Keyboard and screen reader: **599 Tab stops** to reach the map after a search; opening the detail panel does not move focus and fires no live region, so the panel's contents are never announced; the panel is an `<aside>` with no `role`, and in DOM order it sits after all 590 cards. | accessibility tree (`browser_snapshot`), `04-result-cards-1440.png` | Make the results list a roving-tabindex composite (one tab stop, arrow keys inside). Move focus to the detail panel heading on open, give it `role="dialog" aria-modal="false"` plus a labelled close, and return focus to the card on Escape. |
| P1-8 | No way back. Selecting a card flies the map to the segment; there is no "back to my destination" control and the destination pin can end up off-screen. | `10-map-state-1440.png` | A persistent "Recentre on destination" button on the map, plus Escape-to-deselect (already works) documented in the UI. |
| P1-9 | During a search the previous answer stays fully rendered — list, map and all — for the whole wait (2.3s typical, **6.5s** at 30-min walk). The only cue is the button label and a status line in the left panel. | `16-stale-results-during-search.png`, `15-walk30-2100-results-map.png` | Dim and mark the stale layer "showing your previous search", put a determinate progress cue on the search control, and warn before running a >20-minute walk radius. |
| P1-10 | With the server stopped, the failure reads like an answer: red 12px text "Could not reach the CurbCheck server: Failed to fetch" in the left panel while the basemap sits there looking normal — visually near-identical to the zero-results state. | `17-server-down.png` | A map-level banner: "CurbCheck can't reach its local server. Results cleared. Is `curbcheck serve` still running?" with Retry. Make "no answer" and "no parking" look different. |
| P1-11 | The disclaimer is one 121-word, 829-character paragraph with four bold runs; 106px / 12% of a 900px viewport and **275px / 33%** of a phone, which pushes the map entirely off the first screen. Wall-of-text is read as boilerplate. | `01-first-load-1440.png`, `19-mobile-first-load-390.png` | Keep it persistent and undismissible, but reduce to one strong line plus a "What CurbCheck can get wrong" expander that keeps the full §17 text verbatim. See (d). |
| P2-1 | "Confidence" is never defined and mixes parse with snap confidence; users see 100% / 76% / 70% / 0% with no anchor, and 0% on a no-data span next to "no sign data on this block". | `09-detail-no-data.png`, `10-map-state-1440.png` | Replace the number with three words — "Reading: clear / uncertain / unreadable" — and expose the numbers in the detail's audit section only. |
| P2-2 | Information hierarchy is inverted: walk, money, capacity and confidence — the decision facts — are 12.5px grey, while the street name is 15px bold. Money carried no information at all in a 590-result search (every card "no meter"). | `04-result-cards-1440.png` | Lead each card with walk time and cost as the largest elements; drop a fact entirely when it is unknown rather than printing a placeholder. |
| P2-3 | False precision: "0.2 min walk", "0.8 min walk". Rank order is decided by differences of seconds. | `04-result-cards-1440.png` | Round to the minute, floor at "1 min", and break ties on capacity and certainty rather than on walking noise. |
| P2-4 | Capacity is printed on illegal and ambiguous spans, and "about 2 cars" invites reading it as availability, which CurbCheck explicitly does not model. | `04-result-cards-1440.png`, `07-detail-illegal.png` | Show capacity only on parkable verdicts, phrased "room for ~2 cars when empty". |
| P2-5 | Internal identifiers surface to users: `Segment id 345eb013726fd6c5`. | `05-detail-legal-1440.png` | Move to a collapsed "Audit / provenance" block with the parse and snap numbers. |
| P2-6 | Raw framework and browser strings reach the status line: "Value error, the parking window must be 24 hours or less", "…: Failed to fetch". | — (reproduced) | Map error codes to written sentences in `copy.js`; never print an exception's phrasing. |
| P2-7 | Degenerate labels: "E 86 ST, south side, PARK AVE → PARK AVE". | `09-detail-no-data.png` | Fall back to "E 86 ST, south side, between Park Ave and Madison Ave" or to the block number when both cross streets are equal. |
| P2-8 | The §11 no-data explanation is printed twice, verbatim, in one panel. | `09-detail-no-data.png` | Print it once, at the top, under the verdict. |
| P2-9 | The legend is an opaque box pinned over the map's top-left corner at every size, and hovering a curb segment gives only a cursor change — no verdict readout. | `10-map-state-1440.png` | Move the legend into the results drawer header; add a hover/focus tooltip giving street, side, verdict and walk time. |
| P2-10 | `.card-selected` uses `outline: 2px solid var(--accent)`, visually the same ring a focused card gets; there is no `:focus-visible` rule anywhere in `styles.css`, and no `prefers-reduced-motion` handling for `flyTo` / `easeTo`. | `18-focus-ring.png` | Distinguish selected (filled left edge + checkmark) from focused (ring); add explicit `:focus-visible` and a reduced-motion branch that jumps instead of animating. |

**Interaction cost measured (task 2).** Address search = 3 fields, ~9 native
sub-field entries (2 of them in a control whose value is clipped), 2 clicks minimum;
5 controls are visible before the Search button on first load. Response times:
10-min walk 2.3s, 24-hour window 2.3s, 30-min walk **6.5s** (2,100 cards rendered).
Default window (next top of the hour, +2h) is sensible for "now"; there is no duration
control, and changing Arrive does not move Leave.

## (c) Proposed information architecture and task flow

**Flow.** Land → (1) *where and when* → (2) *a short ranked shortlist on a map* →
(3) *one stretch, in full, auditable* → (4) *adjust and compare*. One decision per step.

**Always visible, never behind a disclosure:**

- The one-line advisory: *"Advisory only — read the posted sign before you park."*
- The resolved destination and the window, in words.
- The verdict word + symbol + colour on whatever is currently selected.
- The four-state legend wherever coloured curb is drawn, including "grey = no data".
- The OSM / NYC Open Data attribution.

**Layout (desktop ≥1100px).** Map is the page. Over it:

1. **Search bar, top-left, compact** — destination, window chip ("Wed 10:00 AM → 12:00 PM
   · 2 hrs"), walk chip ("≤10 min"). Clicking a chip opens a small popover. "Tune ranking"
   opens the three weights; re-ranking is live.
2. **Results drawer, left, resizable** — status line from `counts`, caveats, then
   **Recommended (5)** cards; below it filter chips "N legal · N illegal · N ambiguous ·
   N no data", each expanding to a virtualised list. Cards ~72px: rank, verdict badge,
   street + side, then walk and cost as the largest facts.
3. **Detail sheet, right, over the map, dismissible** — order fixed: label → verdict badge
   + one-sentence reason → **Before you park** (caveats, always open) → the window
   timeline → **Signs that produced this verdict** (raw text, open) → *Other signs on this
   block* (collapsed) → meter rate → audit block (ids, parse/snap confidence).
4. **Map** — coverage boundary drawn; destination pin with a "recentre" button; walk-radius
   circle; hover readout; selected span heavily emphasised and never hidden behind the
   sheet (offset the fit-bounds padding by the sheet width).

**Progressive disclosure (and only these):** full §17 disclaimer body; non-governing
signs; per-rule field tables; meter zone provenance; ids and confidence numbers; the
weight sliders. **Never progressive:** any caveat attached to the displayed verdict, the
governing sign text, the grey/ambiguous explanations, the window in words.

**1280×800** — same, drawer 320px, sheet 400px, fit-bounds padded so the selection stays
clear of the sheet (today the sheet covers 47% of the map and clips the selection).

**390×844 (phone)** — map fills the screen under a one-line advisory and a single search
chip. Results are a **bottom sheet** at three detents (peek = status line + top card;
half; full). Tapping a card raises a **full-screen detail sheet** with a back control.
Nothing scrolls the document; the map never leaves the DOM above the fold.

## (d) Microcopy proposals

**Persistent advisory (always visible, one line):**
> **Advisory only — the posted sign wins.** CurbCheck reads NYC DOT's sign data; it can be
> stale, incomplete, or misread. **Read the sign at the curb before you park.**
> [What CurbCheck can get wrong ▸]

The expander keeps SPEC §17 verbatim, as four labelled lines: *Data may be out of date ·
Temporary and construction signs are not in the data and always win · Hydrants (15 ft),
crosswalks, bus stops and driveways are illegal with no sign · CurbCheck does not know if
a space is physically free.*

**Verdict labels** (word + symbol + colour + one sentence; never colour alone):

| state | label | sentence |
|---|---|---|
| legal, rule read | **✓ Allowed** | "A posted sign permits parking for your whole window." |
| legal, no rule found | **✓ No rule found** | "No posted sign covers this window. That is not a permission — read the curb." |
| illegal | **✕ Not allowed** | "A posted sign prohibits parking for part or all of your window." |
| ambiguous | **? Can't tell** | "The software could not read these signs, or they conflict. Read them yourself, below." |
| no data, `no_signs` | **– No data** | "NYC DOT lists no sign on this stretch. Unknown, not free. Read the curb." |
| no data, `unmatched_signs` | **– No data** | "DOT publishes signs on this block that CurbCheck could not place. Treat as unknown." |

**Confidence** → "Reading: **clear** / **uncertain** / **unreadable**", with a tooltip:
"How sure the software is that it read these signs and put them on the right stretch —
not how sure it is that you can park."

**Caveats** — heading stays "**Before you park**", each caveat one line, always open:
"Temporary or construction signs override this. · Holiday and street-cleaning suspensions
may not be loaded. · Hydrants, crosswalks, bus stops and driveways are illegal unsigned."

**Empty / error states:**

- Zero results, in coverage: "No curb data within a 10-minute walk of here. Try a longer
  walk, or move the pin."
- Out of coverage: "CurbCheck only covers Manhattan. That point is in New Jersey."
- No address match: "No Manhattan address matches '1519 3rd Avenu'. Did you mean 1519 3
  AVE? Or drop a pin on the map."
- Server unreachable: "CurbCheck can't reach its local server, so these results are gone.
  Check that `curbcheck serve` is still running. [Retry]"
- Window too long: "A parking window can be at most 24 hours."
- Price unknown: "Cost unknown" (never "$0.00", never "no meter").

## (e) Accessibility requirements

1. Every verdict carries **word + symbol + colour + line pattern**; colour is never the
   only channel. Keep the existing badge and dash-pattern scheme.
2. All form controls keep visible `<label>`s. The window control must render its value as
   readable text, not rely on the native clipped widget.
3. Results are a real list (`<ol>` / `<li>`) with **one tab stop**: roving tabindex, arrow
   keys to move, Enter/Space to open. Reaching the map must never cost 599 Tab presses.
4. The detail panel is `role="dialog"`, labelled by its heading; focus moves to it on open
   and returns to the originating card on close; Escape closes (works today); a visible
   Close button is the first or last stop inside it.
5. A skip link: "Skip to map" / "Skip to results".
6. Explicit `:focus-visible` styling on every interactive element, distinct from the
   selected-card treatment; minimum 3:1 against both adjacent colours.
7. Status and result-count changes announced via a polite live region (the `role="status"`
   line already does this). Slider `<output>` elements must not each be a live region —
   dragging currently announces on every step.
8. Touch targets ≥44×44px; cards and chips sized accordingly on phone.
9. Text ≥14px for anything a parking decision depends on — today the decisive facts are
   12.5px and the "does not govern this stretch" notice is 12px.
10. Honour `prefers-reduced-motion`: `flyTo` / `easeTo` become jumps.
11. Honour `prefers-contrast` / forced-colors: verdict distinction must survive; rely on
    pattern and text, not hue.
12. The map canvas keeps a keyboard-reachable equivalent — every span in the drawn set is
    reachable from the results list, including `no_data` spans.
13. Heading order stays h1 → h2 (Results, segment) → h3 (Before you park, Signs) → h4.
14. Screen-reader labels for the destination marker and the walk-radius circle.

## (f) What must NOT change — safety invariants

1. **The disclaimer is persistent and undismissible.** It may be shortened on screen only
   if the full SPEC §17 text stays one interaction away, always available, never behind a
   "don't show again".
2. **Four verdicts, never two.** `legal` / `illegal` / `ambiguous` / `no_data` stay
   visually and verbally distinct. An unknown verdict value renders as `no_data`.
3. **Grey means unknown, never free and never permitted.** No rendering, copy, colour or
   sort order may let an empty or grey curb read as "nothing to worry about" — and the
   fix for P0-6 must make grey *more* visible, not less.
4. **Every verdict is shown next to the raw `sign_description` that produced it**, quoted
   verbatim, byte for byte, with the parse method and confidence reachable. This is
   SPEC §10 and is not negotiable; grouping non-governing signs is allowed, deleting them
   is not.
5. **Caveats are never hidden behind a disclosure on a displayed verdict** — including,
   and especially, a "legal" one.
6. **`money: null` is not `$0.00`.** Unknown price prints as unknown; "unconfirmed" stays
   on unconfirmed prices.
7. **Counts come from the server's `counts`, never from `results.length`**, and the UI
   says plainly when it is showing a subset.
8. **An error clears the previous answer.** Stale results must never sit under a new
   destination, a new window, or an error banner.
9. **No network, no telemetry, no remote asset, no `innerHTML`.** Plain ES modules, no
   framework, no bundler, no CDN, no external font; the strict CSP stays; all untrusted
   text is assigned with `textContent`.
10. **The app never claims a space is available**, only that parking is legal.
