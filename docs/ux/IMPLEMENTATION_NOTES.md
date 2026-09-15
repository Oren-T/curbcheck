# CurbCheck UI redesign — implementation notes

What was built against `docs/ux/UX_AUDIT.md` and `docs/ux/DESIGN_DIRECTION.md`,
where the build departs from either, and what is left. Written here rather than
in `docs/DECISIONS.md` because that file belongs to the backend change landing
alongside this one; **§1 below is the entry that needs to move there.**

Verified against `curbcheck serve` on `data/curbcheck.sqlite` (74,389 signs) at
1440×900, 1024×768 and 390×844 on 2026-09-15. Screenshots: `screens/after/`.

---

## 1. Decision: SPEC §17 is rendered as a strip plus a disclosure

**Change.** The §17 disclaimer is no longer a 121-word paragraph at the top of
the page. The persistent, undismissible strip carries the two load-bearing
sentences — *"Advisory only. Read the posted sign."* and *"Grey means no data,
not no restriction."* — and a `Full notice` disclosure expands the complete §17
text in place. The same text is printed verbatim at the foot of **every** detail
sheet, so it is inside every verdict as well as at the top of every screen.

**Evidence.** The old banner was 106 px (12%) of a 900 px viewport and 275 px
(33%) of a 390×844 phone, pushing the map entirely off the first screen
(UX_AUDIT P1-11, `screens/06-first-load-390.png`). Nine bolded clauses in one
paragraph is read as boilerplate on the second visit; the sentence that changes
behaviour is read every time.

**Why this is still SPEC §17.** The spec requires a persistent banner plus
per-verdict text. Both survive: the strip cannot be dismissed (there is no close
control and nothing is persisted to storage — a static test pins this), the full
text is one click from any screen, and the per-verdict text is the "Before you
park" block, which is never behind a disclosure.

**Departure from DESIGN_DIRECTION §3.** The design doc wanted the full notice
*expanded* on the first load of each session. It ships collapsed: the brief for
this build asks for a 36 px strip with the full text "one click away", and an
amber wall on first load is the failure mode the compaction exists to fix. No
session state is stored either way.

---

## 2. Tokens: what was adopted and what was added

`docs/ux/tokens.css` is adopted as `web/tokens.css` unchanged — every measured
value (verdict hues, inks, dash patterns, contrast notes) is byte-identical, so
the colour-vision and contrast validation still describes what ships.

Appended to it, under a comment saying so, is one block of **glass tokens** the
floating chrome needs: `--c-glass*`, `--c-strip`, `--blur-panel`, `--r-2xl`,
`--e-glass` / `--e-sheet` / `--e-lift`, `--c-select-glow`, `--strip-h`,
`--drawer-peek`. They exist because the rail and the sheet are now translucent
panels over the map rather than opaque columns beside it; the alphas (0.78–0.90)
were chosen so `--c-text` keeps better than 12:1 over the worst basemap colour a
panel can sit over, and there is a `@supports not (backdrop-filter)` fallback to
solid surfaces. No verdict colour, ink, or dash value was touched.

## 3. Layout

- **Desktop (≥1100 px):** the map is the page, full-bleed under a 36 px strip. A
  400 px floating rail holds the search card and the results; the detail sheet
  floats on the right over the map; the legend is a small floating control top
  right; attribution sits bottom right.
- **Tablet (641–1099 px):** same, rail 340 px, sheet `min(520px, 100%)`.
- **Phone (≤640 px):** map on top, the rail becomes a bottom drawer with three
  detents (peek 148 px / 58 vh / full), dragged by its handle or cycled by
  tapping it; the detail sheet is a second layer over the drawer.
- **The document never scrolls.** `html, body { overflow: hidden }` and every
  scroll region (`.rail-scroll`, `.detail-scroll`, the autocomplete list) owns
  its own `overflow-y` with `overscroll-behavior: contain`. Measured after a
  search at 390×844: `scrollHeight` 844 = `innerHeight` 844. It was 87,609 px
  (UX_AUDIT P0-4).

## 4. Search card

Destination is an ARIA 1.2 combobox (`aria-expanded`, `aria-activedescendant`,
↑/↓/Enter/Escape, one `/api/geocode` call per 150 ms of quiet, minimum two
characters, at most eight rows). Each row is label + muted secondary + a kind
glyph, and the last row is always **Drop a pin instead** — which is also the
whole list when the field is focused and empty, and the fallback row when the
geocoder returns nothing. A dropped pin is labelled by `/api/reverse`
("near 174 E 85 ST"); if reverse fails the coordinates stand in and the search
still runs.

The window is a date, a start time and duration chips (1h/2h/3h/4h/Custom) with
the result written out — *"Wed Sep 16, 10:00 → 12:00"*. Walk radius is a chip
group (5/10/15/20/Custom). `Prefer` is a chip group over the weight presets in
the brief: closer 2/1/0.5, cheaper 1/2/0.5, safer bet 1/1/2, balanced 1/1/0.5
(default). `Advanced` exposes the three raw sliders; moving one switches the
control to a disabled `Custom` chip so the two can never disagree.

*Departure from DESIGN_DIRECTION §5:* the design doc specified 250 ms / three
characters and three presets with different weights; the brief's numbers win.

Each chip group is one Tab stop with a roving tabindex and arrow-key movement.

## 5. Ranking: what re-ranks and what re-queries

`Prefer` and the sliders re-sort the **legal** results client-side from
`walk_min`, `money_value` and `risk` — `score = w_walk*walk + w_money*money +
w_risk*risk` — with no new request (UX_AUDIT P1-3). Two rules keep absence from
winning by having nothing to charge for:

- a legal span with `money_value: null` sorts after every priced one;
- scores within $0.50 order `basis: "posted"` before `basis: "absence"`, which
  is the server's own D27 tie-break mirrored client-side, so pressing a
  preference chip cannot change what the order *means*.

Changing the walk radius or the window needs the server, so those mark the
Search button stale (a glow ring plus *"Walk radius or time changed — press
Search to update."*) instead of re-ranking.

## 6. Results and map

The list shows the top 12 legal with `Show 12 more`, then three collapsed groups
— Ambiguous (n), Illegal (n), No data (n) — always present, always counted from
the server's `counts`, never from `results.length`, with a note when fewer rows
arrived than the count ("3 of 32 drawn, nearest first"). Rank numbers appear
only on the ranked legal shortlist; an illegal span is never "#537" in the same
sequence as a recommendation.

The **map draws every result the server sent** — 590 spans in the verified
search — because the `limit`/`map_limit` split only means something if the red
and grey spans are actually on screen (VALIDATION U1, UX_AUDIT P1-2). Each
verdict has a white casing under it, widths interpolate 13→18 by zoom, the dash
patterns are exactly the token values, and `no_data` is the widest of the four
(3–9 px, dotted) with its dash suppressed below z14.5 where a 0.9-unit dash
would be invisible.

*Departure from DESIGN_DIRECTION §6:* the design doc caps the map at 300 lines
and hides `no_data` beyond that. Not implemented — the brief says draw
everything, and hiding the grey spans is the failure P0-6 is about. 590 spans
render without a frame drop; if a 30-minute search (2,100 spans) proves slow,
cap `map_limit` in the request, not the drawing.

## 7. Detail sheet

Fixed order: verdict chip + reason + what the verdict rests on + the decision
facts → **Before you park** (every caveat, never a disclosure) → the §11
explanation for ambiguous/no-data → **Signs governing this stretch** (verbatim
`sign_description` in an "As posted" monospace block, with code, order number,
distance from the corner, arrow and parse method) → **Elsewhere on this block**
(collapsed, counted) → **What the software read** (parsed rules grouped under
their `sign_id`) → meter rates → provenance → the full §17 notice.

It is `role="dialog"` with `aria-labelledby`; focus moves to Close on open and
back to the originating card on Escape or close. Confidence is printed only when
`confidence_shown`, labelled "Data confidence", and explained in a plain
sentence rather than a tooltip. The §11 no-data explanation is printed **once**
(UX_AUDIT P2-8).

## 8. Copy decisions worth flagging

- `basis: "absence"` reads **"Nothing posted"** in an outlined chip in the legal
  colour family, with the reason line prefixed *"No posted rule —"* and no
  confidence figure anywhere (UX_AUDIT P0-1).
- **A `no_data` span prints no price at all** — no "$0.00", no "no meter", not
  even "Price unknown" (P0-5). Legality by absence also prints no price: the
  engine's "0.00" there means "nothing charges in this window", and a price line
  would be the app's most confident voice about the thing it read nothing of.
  An illegal span prints a price only when it is metered.
- Capacity is shown only on parkable verdicts and phrased "room for ~6 cars when
  empty", because "about 6 cars" invites reading it as availability (P2-4).
- Walk times round to the minute and floor at 1 min (P2-3).
- `street_name` ending in a degenerate pair ("PARK AVE → PARK AVE") renders as
  "E 86 ST, south side, at PARK AVE" (P2-7). The server field is unchanged.
- Error codes map to written sentences in `copy.js`; no exception or framework
  phrasing reaches the screen (P2-6). Failures are persistent blocks with a
  Retry in the rail, never a toast; the toast is reserved for "Pin set".

## 9. Verified

`curbcheck serve`, real database, console clean at all three sizes:

| Flow | Result |
|---|---|
| First load 1440/1024/390 | map full-bleed, rail floating, legend and attribution placed; no console output |
| Autocomplete "1519 3 Ave" | one `/api/geocode` call, one candidate + pin row, ↑/Enter picks it, label echoed in the collapsed summary |
| Search Wed 10:00→12:00, 10 min | 2.6 s, skeletons while waiting; 839 in radius, 590 drawn, groups read 32/404/54 from `counts` |
| Re-rank | Closer / Cheaper / Safer bet / Balanced each produce a different top three, with no request |
| Detail, all four verdicts | correct chip, basis sentence, caveats above the signs, verbatim sign text, no price on grey |
| Keyboard only | 2 skip links → notice → form → 12 cards → groups → map; Enter opens a sheet, focus lands on Close, Escape returns focus to the same card |
| Phone 390×844 | drawer at three detents, `scrollHeight` == `innerHeight` after a search, detail as a second sheet |
| Dark mode (`emulateMedia`) | chrome, chips and sheets dark; basemap and verdict lines stay light, as the design doc requires |
| Outside coverage | 422 → "CurbCheck covers Manhattan only.", coverage bbox outlined and framed, results cleared |
| Server unreachable | results cleared, form and destination kept, persistent notice + Retry, one toast |
| Zero results / no calendar | card in the rail / amber degraded notice under the search card |

## 10. Contract notes for the backend

- `/api/segment` returns `governing` and `other_on_block` as **top-level** keys;
  the brief for this build described them nested under `signs`. The frontend
  accepts either, and falls back to splitting a flat `signs` array on
  `is_regulation` so an older server renders something rather than nothing.
- `/api/geocode` returns few or no candidates for a partial query ("1519 3" is
  empty, "1519 3 Ave" is one row), so the autocomplete usually shows the "no
  match + drop a pin" fallback until the richer local index lands. The dropdown
  consumes whatever the endpoint returns, in its order; nothing needs to change
  here when it does.
- A legal-by-absence span arrives with `money: "0.00"`, `price_known: true`.
  The UI prints no price for it; if the engine ever means "free, and we read a
  sign that says so", that has to arrive as `basis: "posted"`.

## 11. Not done

- **Verdict filter chips** (DESIGN_DIRECTION §3): the legend does not toggle
  verdicts on the map or filter the list. It is a legend only.
- **Recentre on destination** (UX_AUDIT P1-8) and a **map hover readout**
  (P2-9). Hovering a card highlights its line; hovering a line does not yet
  highlight its card.
- **Roving tabindex over the result list** (P1-7): with a 12-card shortlist the
  list is 12 Tab stops rather than 599, so plain buttons were kept. The group
  bodies render their cards collapsed (`hidden`), so they cost no Tab stops but
  do cost DOM — ~500 buttons on a dense search. Render them on expand if that
  ever shows up in a profile.
- **Stale-results dimming during a search** (P1-9): the previous answer is
  replaced by skeletons immediately instead of being dimmed.
- The **About & data sheet** the design doc mentions; the full notice and the
  attribution carry its content for now.
