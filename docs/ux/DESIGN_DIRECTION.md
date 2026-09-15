# CurbCheck — Visual Design Direction

Scope: the look and structure of `web/`. Tokens live in `docs/ux/tokens.css`;
reference screenshots of the build this was written against are in
`docs/ux/screens/design/` (1440×900 and 390×844, taken 2026-09-15 against
`curbcheck serve` with a "3 Ave & E 85 St" search for tomorrow 10:00–12:00).
Task-flow findings are the UX researcher's; this file is colour, layout, and
component anatomy.

---

## 1. Assessment

The current UI is honest, accessible and legible — the safety work is already
there (four verdicts, dash patterns, verbatim sign text, a caveat block above
the facts) — but it is presented as a settings form with a map beside it rather
than as a mapping tool, and the result is that the safety content is technically
present and practically buried. **First**, the §17 disclaimer is a 105 px wall of
nine bolded clauses at desktop and a 280 px wall on a phone (`06-first-load-390.png`);
at that size it stops being read after the first visit, which is exactly the
failure mode a persistent banner exists to prevent, and it costs the map a
tenth of the viewport forever. **Second**, the left rail is a stack of six raw
controls — two `datetime-local` inputs that clip their own date (`/2026, 10:00 AM`
in `01-first-load-1440.png`), a walk slider, and a fieldset of three unlabelled-unit
weight sliders ("Risk of being wrong 0.5") — so the answer, the ranked list,
starts ~720 px down and is never on screen at the same time as the form that
produced it. **Third**, the app renders every result: 604 spans for a 10-minute
radius, drawn as 604 lines that turn the Upper East Side into red-dotted
spaghetti (`02-results-1440.png`) and stacked as 604 cards that make the phone
page 89,783 px tall — on a phone the map is a 320 px sliver below all of them
(`08-phone-map-buried-below-list-390.png`) and tapping a card opens the detail
sheet 88,562 px off screen, so the tap appears to do nothing.

---

## 2. Design principles

1. **The verdict is the interface.** Every screen answers "can I park here?" before it explains anything.
2. **Uncertainty is a first-class state, never a lighter shade of certainty** — grey and amber get the same visual weight as green.
3. **Never colour alone**: every verdict carries a word, a shape/pattern, and a contrast-checked hue.
4. **Quiet chrome, loud content** — one accent, warm neutrals borrowed from the basemap, no decorative colour anywhere.
5. **Show the shortlist, not the database**: rank, cap, and let the map hold the rest.

---

## 3. Layout system

### Desktop (≥1100 px)

```
┌──────────────────────────────────────────────────────────────────────────┐
│ ▲ CurbCheck   Advisory only — always read the posted sign.  [Full text ▾] │ 36px strip
├────────────────────────────┬─────────────────────────────────────────────┤
│ ┌ SEARCH CARD ───────────┐ │                                             │
│ │ Destination [_______]  │ │      MAP (full bleed, edge to edge)         │
│ │ Date [Tue Sep 16 ▾]    │ │                                  ┌────────┐ │
│ │ From [10:00] 1h 2h 3h  │ │   ╔═ detail sheet, right, 460px ═╗│ legend │ │
│ │ Prefer: closer|cheap|  │ │   ║ opens over the map, not over ║└────────┘ │
│ │         safer          │ │   ║ the list                     ║           │
│ │ ▸ Advanced             │ │   ╚══════════════════════════════╝           │
│ │ [      Search      ]   │ │                                             │
│ └────────────────────────┘ │                                             │
│ ─ 841 spans · 337 legal ─  │                                             │
│ [Legal 337][Amb 38][Ill…]  │  ← verdict filter chips, also the legend     │
│ ┌ result card ───────────┐ │                                             │
│ │ #1 ✓Legal  E 85 ST …   │ │                                             │
│ └────────────────────────┘ │                                             │
│ … top 12, then [Show 20 ▾] │                      © OSM · Protomaps · DOT │
└────────────────────────────┴─────────────────────────────────────────────┘
   400px rail, own scroll                           attribution bottom-right
```

The rail is a **fixed sidebar**, not a floating card: at 604 results a floating card
either covers the map or scrolls off it, and a fixed rail lets the list own its own
scroll container (the phone bug in §1 is precisely the absence of one). The search
card is an elevated card (`--e-2`) at the top of the rail, so the rail reads as
*question above, answer below*; after a search it **collapses to one line**
("3 Ave & E 85 St · Tue 10:00–12:00 · 10 min walk  [Edit]").

### Tablet (700–1099 px)

Same two-column split, rail at 340 px. The detail sheet becomes full-width over
the map area (`min(520px, 100%)`), and the verdict filter chips move into the
map's top-left as a floating pill row so the rail keeps its height for results.

### Phone (<700 px)

```
┌───────────────────────┐   ┌───────────────────────┐
│ Advisory only… [▾]    │   │ Advisory only… [▾]    │
├───────────────────────┤   ├───────────────────────┤
│  [Where to? ______ 🔍]│   │                       │
│                       │   │        MAP            │
│        MAP            │   │                       │
│      (fills)          │   ├───────────────────────┤ ← drawer, 2 detents
│                       │   │ ══                    │
│                       │   │ ✓Legal E 85 ST, north │
├───────────────────────┤   │ 2 min · no meter · 3  │
│ ══  12 spots found  ▲ │   │ ─────────────────────  │
└───────────────────────┘   │ BEFORE YOU PARK …     │
   drawer peek (88px)       └───────────────────────┘
```

Map first, full viewport minus the strip. Results are a **bottom drawer** with three
detents — peek (88 px, "12 spots · nearest 2 min"), half (55 vh), full (100 vh minus
strip). Selecting a result pushes the **detail sheet as a second drawer layer** with a
back affordance. The document never scrolls: each layer owns `overflow-y`.

### Disclaimer

- **Persistent strip, top, all breakpoints, never dismissible.** It carries the
  two load-bearing sentences at `--t-small`: *"Advisory only — always read the
  posted sign. A blank or grey curb means no data, not no restriction."*
  Amber left edge (3 px `--v-ambiguous`), `--v-ambiguous-soft` background, ink
  `--v-ambiguous-ink`. 36 px desktop, 2 lines on a phone.
- **`[Full disclaimer ▾]`** expands the complete §17 text in place, `aria-expanded`,
  `--m-fast`. Expanded by default on first load of a session; the collapsed state
  persists per session only, never across restarts.
- The full §17 text also appears verbatim in the "About & data" sheet, and the
  hydrant/crosswalk clause in every detail sheet's **Before you park** block, so
  collapsing the strip never removes the warning from the flow.
- This changes how SPEC §17 is rendered, not what it says, and **needs a
  `docs/DECISIONS.md` entry** before it ships; the evidence is the 280 px phone
  banner in `06-first-load-390.png`.

### Legend and attribution

The legend **is** the verdict filter chip row (one component, two jobs): four chips
with a dash-pattern swatch, the word, and the count; tapping toggles that verdict on
the map and in the list. The no-data sentence sits beneath it as a permanent caption,
never a tooltip. Attribution stays bottom-right in `--t-micro` `--c-text-muted` on
`--c-surface-scrim`, with the DOT/DCP credit and a link to the About sheet.

---

## 4. Tokens

Written to `docs/ux/tokens.css`. Highlights and the measured numbers:

**Verdict colours.** Contrast is stated against the three surfaces a line is
actually drawn on in the vendored basemap — white road `#ffffff`, grey road
`#ebebeb`, earth `#e2dfda` — and, for chips, against the chip's own tint.

| Verdict | Label | Light fill | vs white / grey road / earth | Dark fill | vs `#1b1b1a` | `line-dasharray` |
|---|---|---|---|---|---|---|
| Legal | `✓ Legal` | `#15855a` | **4.63** / 3.88 / 3.48 | `#3fbf88` | **7.40** | *(none — solid, cap round)* |
| Ambiguous | `? Ambiguous` | `#c78700` | **3.05** / 2.56 / 2.29 | `#eab53c` | **9.17** | `[2, 1.25]`, cap butt |
| Illegal | `✕ Illegal` | `#a01b12` | **7.89** / 6.62 / 5.94 | `#df554b` | **4.55** | `[0.9, 0.7]`, cap butt |
| No data | `– No data` | `#4c525d` | **6.73** / 5.64 / 5.06 | `#6f7886` | **3.86** | `[0, 2.2]`, cap round → dots |

Validated with the dataviz skill's `validate_palette.js` (Machado 2009 severity
1.0, OKLab ΔE ×100, `--pairs all`, since all four are on screen at once):

- light on `#ffffff`: worst CVD pair **ΔE 9.7** (target ≥ 8) — amber↔green under protanopia; worst normal-vision pair **16.4** (floor ≥ 15) — grey↔green. Contrast: all four ≥ 3:1. PASS.
- dark on `#14171a`/`#1b1b1a`: worst CVD pair **ΔE 8.5** (protan, grey↔red); worst normal-vision pair **19.4**. Contrast: all four ≥ 3.8:1. PASS.
- Two deliberate exemptions, both documented in the token file: the grey fails
  the validator's chroma floor (grey *is* the semantic — it must read as absence,
  not as a fifth hue), and amber measures 2.56:1 on the grey road fill. Amber is
  the only sub-3:1 value anywhere and is legal only because it is a 5–6 px line
  with a distinct dash and a written label. **Never use `--v-ambiguous` for text**;
  `--v-ambiguous-ink` (`#6f4c00`, 7.0:1 on its tint) is the text token.

The current palette fails the same checks — `#b42318`↔`#0f7b3f` is ΔE **4.9**
under deuteranopia and `#b42318`↔`#b45309` is ΔE **8.6** for *normal* vision.
Today the four verdicts are separated almost entirely by dash pattern.

**Neutrals** are warm (`#1a1917` → `#f6f5f3`) to sit in the basemap's family
(`#e2dfda` earth, `#91888b` labels) instead of clashing with it; the present
`#f3f4f6`/`#16191d` ramp is cool and reads as a dashboard laid over a map.
**Accent** `#1a56b0` (7.0:1) replaces `#1d4ed8`, which is louder than anything it
labels and sits close to the basemap's `#80deea` water. Spacing is a 4 px scale
(`--s-1`…`--s-10`); radii 4/8/12/16/pill; four elevation steps; a 7-step type
scale on the system stack with `--t-body: 15px`; motion 90/140/200/280 ms plus
600 ms for map camera moves, all zeroed under `prefers-reduced-motion`.

**Fonts.** System stack only, no vendored file. At 11–24 px on a UI this dense,
a vendored face (40–120 KB even subsetted) buys no legibility, and every OS in
scope ships tabular figures via `font-variant-numeric` for the price/time columns.

---

## 5. Component specs

### Search card

- **Destination.** One full-width input, 44 px tall, `--r-md`, placeholder
  *"Address or cross street"*. Typing ≥ 3 characters queries `/api/geocode`
  (debounce 250 ms) into a **combobox listbox** below the field — `role="combobox"`,
  `aria-expanded`, `aria-activedescendant`, ↑/↓/Enter/Esc. Each row: bold label,
  muted `kind` on the right. This replaces today's suggestion list, which only
  appears *after* a failed search. A `Drop a pin` ghost button sits to the right
  of the label, and in pin mode the map cursor is a crosshair and the strip under
  the input says *"Tap the map to set your destination."*
- **When.** Three controls in a row, never `datetime-local`:
  `[Today ▾]` date (a small popover with Today / Tomorrow / a 14-day list),
  `[10:00 ▾]` start time (15-minute steps), and a duration chip group
  `1h · 2h · 3h · 4h · Custom`. Selected chip = `--c-accent-soft` fill,
  `--c-accent` 1.5 px border, `--c-accent-ink` text; `role="radiogroup"`.
  "Custom" swaps the chips for an end-time select. The card shows the derived
  window in muted text: *"Tue Sep 16, 10:00 AM – 12:00 PM"*.
- **Prefer.** One segmented control replacing the three weight sliders:
  `Prefer: [ Closer | Cheaper | Safer bet ]` mapping to weight presets —
  closer `{walk 2.0, money 0.6, risk 0.5}`, cheaper `{0.8, 2.0, 0.5}`,
  safer bet `{1.0, 0.8, 2.0}` (defaults to Closer). Under it, a walk cap as a
  chip group `5 · 10 · 15 · 20 min`, not a slider, because the value is the
  radius drawn on the map and a discrete set is easier to compare across searches.
- **`▸ Advanced`** disclosure holds the three raw sliders, each with its unit
  and a plain-language sublabel ("Risk — how hard to avoid spans the software
  is unsure about"). Moving a slider switches the segmented control to a fourth,
  disabled "Custom" segment so the two never disagree.
- **Search** is a full-width primary button, `--c-accent`, `--r-md`, 44 px.

### Result card

```
┌────────────────────────────────────────────────┐
│ ▍ #1   [✓ Legal]                      2 min ↗  │   rank muted, chip, walk bold
│ ▍ E 85 St · north side                         │   --t-lead, --w-semibold
│ ▍ Lexington Ave → 3 Ave                        │   --t-caption, --c-text-secondary
│ ▍ No meter · ~3 cars · 100% confidence         │   --t-caption, muted, tabular
│ ▍ No posted rule covers this window            │   --t-small, --c-text (reason)
└────────────────────────────────────────────────┘
  ▍ 4px left edge in the verdict colour, in the verdict dash pattern
```

Bold: the walk minutes, the street name, and the price when there is one. Muted:
rank, cross streets, capacity, confidence. The verdict chip is the only colour.
The reason line is body ink, not muted — it is the sentence that changes a
decision. Cards are `--r-lg`, `--e-1`, 1 px `--c-border`, `--s-5` padding; hover
lifts to `--e-2` and tints `--c-accent-soft`; selected adds a 2 px `--c-accent`
ring and keeps the tint. Cap the rendered list at 12 with
`[Show 20 more]` + `[Show all 604]`, and put the honest total in the header
("841 spans in radius · showing the 12 best").

A **legal verdict whose reason is "no posted rule covers this window"** is
legality by absence, not by permission. Give it a distinct reason line prefixed
`No posted rule —` in `--v-nodata-ink`, so it never looks like a sign said yes.

### Detail sheet

Order is fixed, top to bottom, and the caveats never move below the fold:

1. **Header** — street label (`--t-title`), side and cross streets muted, `Close` (icon + label).
2. **Verdict block** — large chip + the reason sentence at `--t-lead`, on the verdict tint, 3 px verdict-coloured left edge in the verdict dash pattern.
3. **Before you park** — every caveat, always, on `--v-ambiguous-soft` with an amber edge. Never a disclosure.
4. **§11 explanation** for ambiguous / no-data, in its own verdict tint.
5. **Facts** — walk, money, meter running, capacity, confidence, meter zone, segment id. Two-column `dl`, labels muted, values `tabular-nums`.
6. **As posted** — the raw sign text in `--font-mono` `--t-small` on `--c-surface-sunken` with a 1 px border and a `⌜ AS POSTED ⌝` eyebrow, `white-space: pre-wrap`, never truncated, never scrolled horizontally. The `sign_code` and `order_number` sit directly beneath as a muted caption. *Keep the current dark-on-light inversion only if it passes contrast in both schemes; a bordered sunken block is calmer and passes both.*
7. **What the software read** — parsed rules, each a plain sentence plus its field table; unparsed rules keep the amber notice.
8. **Meter rates**, then **This stretch of curb**, then the ASP caveat.

### States

- **Empty (pre-search):** the map at Manhattan extent, a centred card in the rail — *"Where are you going? Search an address or cross street, or drop a pin."* — and the legend already visible so the four verdicts are learned before results arrive.
- **Loading:** the Search button becomes `Searching…` with a 2 px indeterminate bar across the top of the rail; the list shows 3 skeleton cards (`--c-surface-sunken` blocks, no shimmer). Previous results grey to 40 % rather than disappearing, then are replaced.
- **Zero results:** a card, not a toast — *"Nothing within a 10-minute walk of there. Try a longer walk, a different time, or a nearby corner."* with the walk chips repeated inline.
- **Error:** a bordered block at the top of the rail, `--v-illegal-ink` on `--v-illegal-soft`, with the server's message and a `Retry`. Results clear (as they do today — the right call).
- **Degraded:** database missing / calendar missing render as a strip under the search card in ambiguous tint, not as an error.

### Toast

One toast at a time, bottom-centre on phone, bottom-left above the scale bar on
desktop; `--c-text` background, inverse text, `--r-md`, `--e-3`, 4 s, dismissible,
`role="status"`. Reserved for transient confirmations only ("Pin moved",
"Copied sign text"). **No verdict, caveat, or error ever appears in a toast** —
anything safety-relevant must be persistent.

### Focus, hover, selection

- Focus ring: 2 px `--c-focus` with a 2 px `--c-surface` inner ring (`--focus-ring`) so it reads on cards, on the map, and on tinted chips. Never `outline: none`.
- List hover: `--c-accent-soft` tint + `--e-2`, and the matching map line goes to its hover weight — the pairing must be bidirectional and is currently missing from the list→map direction.
- Map hover: line width ×1.4 and a 2 px white casing; the cursor is a pointer.
- Selected (either direction): map line keeps its own colour and dash but gains a `--c-select-halo` casing at width ×2.2 and 0.9 opacity underneath; the card gets the accent ring; the sheet opens. One selection only, cleared by Esc.

---

## 6. Map styling

- **Line width by zoom** (`interpolate`, `exponential 1.4`): z13 → 1.5 px, z15 → 3 px, z16.5 → 5 px, z18 → 8 px. Below z14 the verdict lines are the *only* thing that should be readable, so drop the dash on `illegal` and `no_data` below z14 (a 0.9-unit dash on a 1.5 px line is invisible) and re-introduce it at z14.5 via a zoom-stepped `line-dasharray`.
- **Casing.** Every verdict line gets a `--c-map-halo` (white) casing one step wider, drawn beneath. This is what makes a dark red line legible over `#e2dfda` earth and a grey dotted line legible over `#ebebeb` road fill.
- **Basemap legibility.** Insert a full-extent `background`-style scrim layer immediately beneath the results layers: `--c-map-scrim` (`rgba(246,245,243,0.45)`), added only while results are on the map. It lifts the roads and buildings toward the paper tone and drops the `#80deea` water and `#9cd3b4` parks a step in saturation, so the four verdict hues are the most saturated things on screen. Also move the POI symbol layer above the scrim but reduce `text-opacity` to 0.6 while results are shown — the current screenshot has 40 restaurant pins competing with the answer.
- **Destination marker.** Not the default MapLibre teardrop: a 14 px `--c-marker` disc with a 3 px white ring and a 1 px `rgba(0,0,0,.25)` outer ring, plus a small `--c-marker` drop shadow. It reads at any zoom and cannot be mistaken for a verdict.
- **Walk radius ring.** Fill `--c-walk-ring` at 0.05, outline 1.5 px at 0.45 with `line-dasharray [4, 3]`, and a label at the top of the ring: *"10 min walk"* — the circle is currently unexplained.
- **Density.** Draw at most 300 lines; beyond that, drop `no_data` spans from the map by default with a chip that says so (`No data 53 — hidden`). Never silently drop `illegal`.

---

## 7. Microcopy tone

1. **Plain, second person, present tense; the shortest true sentence wins** — *"No posted rule covers this window"*, never *"Regulation data unavailable for the specified interval"*.
2. **Never state more certainty than the data supports, and name the source of doubt** — say *which* thing is unknown ("the software could not read this sign"), not just that something is.
3. **Instructions over warnings**: end an uncertain state with the action ("Read the curb"), and never use an exclamation mark, ALL CAPS, or the word "safe".

---

## 8. Implementation checklist (in order)

1. Add `docs/ux/tokens.css` to `web/` as `tokens.css`, link it before `styles.css`, and port `styles.css` to the tokens with no visual change other than the new palette. Delete the old `--legal/--ambiguous/--illegal/--no_data` values.
2. Swap `VERDICT_STYLE` in `web/map.js` to the new fills, dash arrays, caps, and the zoom-interpolated widths; add the white casing layer per verdict. Re-render the legend swatches from the same table so they cannot drift.
3. Add `prefers-color-scheme: dark` support: chrome, panels, chips, and sheets only. Leave the basemap light and say so in a comment — there is no dark style in `web/basemap/`, and a CSS filter over the canvas would falsify the verdict colours. (Follow-up ticket: vendor a dark Protomaps style.)
4. Fix the phone layout: give `.panel`, the results list, and `.detail` their own scroll containers; make `.detail` a fixed-position drawer at `<700 px` so selecting a card is visible. This is the 89,783 px page bug and it is the highest-severity item here.
5. Compact the disclaimer into the persistent strip + `Full disclaimer` disclosure, add the full text to an About sheet, and write the `docs/DECISIONS.md` entry.
6. Cap the rendered result list at 12 with `Show more` / `Show all`, and cap map features at 300 with the `no_data`-hidden chip. Keep `statusLine()`'s honest totals.
7. Rebuild the search card: geocode-backed combobox, date popover, start time, duration chips, `Prefer:` segmented control, `Advanced` disclosure for the sliders, walk-cap chips.
8. Rebuild the result card anatomy (§5) and add the list↔map hover pairing in both directions.
9. Restructure the detail sheet to the fixed order in §5, including the `AS POSTED` block and the "legality by absence" reason prefix.
10. Add the verdict filter/legend chip row with live counts, wired to both the map filters and the list.
11. Add the empty / loading / zero-result / error / degraded states and the toast component.
12. Audit focus rings, 44 px targets, and `prefers-reduced-motion` across everything above.
