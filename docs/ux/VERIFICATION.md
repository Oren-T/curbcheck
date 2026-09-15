# CurbCheck web UI — final UX verification

Every P0, P1 and P2 finding in `docs/ux/UX_AUDIT.md`, re-scored against the
rebuilt frontend on the real app: `curbcheck serve` on `data/curbcheck.sqlite`
(74,389 signs), Chromium at 1440×900, 1280×800, 1024×768 and 390×844, light and
dark, with the console watched throughout and `prefers-reduced-motion`
emulated. Screenshots are in `screens/final/`; a bare number below is a file in
that directory.

Scored **Fixed** only where the finding's own failure could not be reproduced.
Twelve defects found during this pass were fixed here, in eleven commits, and
are listed in §4; the ones left open, each with a reproducer, are in §6.

**Score: 25 Fixed · 3 Partially fixed · 0 Not fixed**, over the 28 findings in
the audit. Every P0 and every P1 — 18 of them — is Fixed; the three partials are
all P2.

---

## 1. Re-scored findings

### P0 — safety or blocking

| id | finding | score | evidence |
|---|---|---|---|
| **P0-1** | "Legal, 100% confidence" awarded where no rule was found | **Fixed** | `basis: "absence"` renders as an outlined **Nothing posted** chip, reason "No posted rule covers this window", and the sentence *"No posted sign covers this window. That is not a permission — read the curb."* No confidence figure appears anywhere on it (`confidence_shown` gates it). `04`, `32` |
| **P0-2** | The sign text next to a green verdict contradicts it | **Fixed** | The sheet opens with the verdict, then **Before you park** (open, never a disclosure), then **Signs governing this stretch** — on the audited span, the one sign that produced the verdict. The three that do not are behind *Elsewhere on this block (3)*, counted and never dropped. `04`, `29` |
| **P0-3** | Out-of-coverage reported as an empty result | **Fixed** | A pin in Hoboken and one in Staten Island both answer **"CurbCheck covers Manhattan only. That point is not on a Manhattan street. Move it onto a block inside the dashed outline."** with the coverage box drawn and framed and the results cleared. No "try a longer walk" anywhere on this path. The Hudson (40.7550, −74.0100) is the same 422, not a zero-result. `30`, `14` |
| **P0-4** | The phone build does not work | **Fixed** | 390×844 after a search *and* with a detail sheet open: `document.scrollHeight` 844 = `innerHeight` 844 (was 87,609). Map on top, rail as a three-detent drawer, detail as a full-screen second layer with its own Close. `21`, `23`, `34` |
| **P0-5** | A grey span asserts a price | **Fixed** | `priceLabel()` returns `null` for `no_data` and for `basis: "absence"`, so no price line is built at all — no "$0.00", no "no meter", not even "Price unknown". Confirmed on 54 grey cards and their sheets. `07`, `29` |
| **P0-6** | Grey is the least visible verdict | **Fixed** | `no_data` is 4–6 px (identical to `legal`), round-capped dotted, with a halo; `illegal` is 2–3 px. Grey is never thinner than green at any zoom stop and is softened only by opacity. Its count is a pill in the top 2×2 grid, not card #537. `03`, `32` |
| **P0-7** | The time window cannot be read | **Fixed** | `datetime-local` is gone: a date field, a time field, duration chips, and the window written out — *"Tue Sep 15, 10:45 → 12:45"* — in an `aria-live` line, echoed again in the collapsed summary. Readable at all four widths. `01`, `21` |

### P1 — major friction

| id | finding | score | evidence |
|---|---|---|---|
| **P1-1** | 88,253 px of near-identical cards | **Fixed** | Top 12 legal, `Show 12 more`, then three collapsed groups. Non-legal spans are never numbered in the ranked sequence. `03` |
| **P1-2** | Capped results vanish silently from the map | **Fixed** | The map draws every row the server sent, and the status line states the cap from the server's `counts`: *"2,469 stretches within a 20 min walk · 1,780 drawn, nearest first"*. `32` |
| **P1-3** | The weight sliders do not re-rank | **Fixed** | `Prefer` and the sliders re-sort client-side with **0** new requests (measured). Walk radius and window instead mark Search stale with a glow ring and *"Walk radius or time changed — press Search to update."* The drawn radius and the data can no longer disagree. `08` |
| **P1-4** | The geocoder silently picks candidate #1 | **Fixed** | Candidates are a live combobox; the pick is echoed in the box, in the muted secondary line (*"Manhattan · between 1517 and 1529"* — the confidence-0.75 interpolated case the audit worried about), and in the collapsed summary. Search sends the picked `lat`/`lon`, verified in the request body. `02` |
| **P1-5** | Typos and out-of-borough give one sentence and no suggestions | **Fixed** | "bleeker" → `BLEECKER ST`; gibberish → *"No Manhattan match for that. Try a cross street, or drop a pin."* plus a **Drop a pin instead** row that is always present, as a row and not as prose. Out-of-coverage has its own message (P0-3). §2 |
| **P1-6** | Clearing the destination re-answers for the previous one | **Fixed** | Clearing the box drops `state.resolved`; Search then refuses with *"Type an address or a cross street, or drop a pin on the map."* in error styling and runs no request. |
| **P1-7** | 599 tab stops; the detail panel announces nothing | **Fixed** | **12** tab stops from the top of the document to the map, 16 to the end of it (2 skip links → Full notice → destination → date → time → three chip groups at one stop each → Advanced → Search → map → 2 zoom buttons → OSM link → About & data). Was 599. Each chip group is a roving-tabindex radiogroup. The sheet is `role="dialog"` + `aria-labelledby`, focus lands on Close, Escape returns focus to the originating card. One new defect found and fixed here (§4, F4). `17` |
| **P1-8** | No way back to the destination | **Fixed** | **Recentre** frames the destination and its walk ring: marker (1368, 878) → (908, 458) after one press. One new defect found and fixed here — the sheet was standing on the button (§4, F3). `27` |
| **P1-9** | The previous answer stays fully rendered during a search | **Fixed** | The list, the pills, the status line and the drawn spans fade to 0.35–0.4 under a 2 px indeterminate bar; the search button reads "Searching…" and is disabled. Nothing is cleared until an answer or an error arrives. `10` |
| **P1-10** | Server-down failure looks like an answer | **Fixed** | Persistent bordered block **"No answer from the server"** with the sentence and a **Retry**; results cleared from list *and* map, form re-expanded with the destination kept, plus one toast. Retry after restarting the server restores the answer. `26` |
| **P1-11** | A 121-word disclaimer wall | **Fixed** | A 32 px strip carrying both load-bearing sentences, undismissible, plus **Full notice** which expands SPEC §17 verbatim in place; the same text is repeated at the foot of every detail sheet and in the About sheet. `12`, `11` |

### P2 — polish

| id | finding | score | evidence |
|---|---|---|---|
| **P2-1** | "Confidence" undefined, printed on no-data | **Fixed** | Printed only when `confidence_shown`, labelled "Data confidence", and followed by a plain sentence rather than a tooltip. Absent from every `no_data` and every legality-by-absence span. Departure from the audit's wording proposal: the number is kept rather than replaced by three words — the sentence beside it is what the audit was actually asking for. `05` |
| **P2-2** | Decision facts at 12.5 px under a 15 px street name | **Fixed** | The walk time is the one bold number on a card, the price the second; the street is 15 px medium. `03` |
| **P2-3** | "0.2 min walk" | **Fixed** | Rounded to the minute, floored at "1 min". |
| **P2-4** | Capacity printed on illegal spans | **Fixed** | `capacityLabel()` returns null for `illegal` and `no_data`; phrased "room for ~2 cars when empty". `05` |
| **P2-5** | `Segment id 345eb013…` in the reading flow | **Partially fixed** | It is last, under **This stretch of curb** with the snap confidence, rather than under the verdict — but that block is open, not the collapsed "audit / provenance" the audit asked for. `07` |
| **P2-6** | Framework phrasing in the status line | **Fixed** | Every code maps to a sentence in `copy.js`. One case was still leaking and is fixed here (§4, F8): a pin outside the input box was reported as "check the date, the time, and the walk radius". |
| **P2-7** | "PARK AVE → PARK AVE" | **Fixed** | Renders "E 86 St · south side" over "at Park Ave". `07` |
| **P2-8** | The §11 no-data explanation printed twice | **Fixed** | Printed once, under the verdict. `07`, `29` |
| **P2-9** | Legend pinned over the map corner; no hover readout | **Partially fixed** | The legend is a glass pill group along the bottom-left, clear of the rail and (now) of the sheet, with the no-data sentence inside it and never in a tooltip. Hovering a card highlights its line and hovering a line highlights its card — but **no readout is printed on the map**, which is the other half of the finding. |
| **P2-10** | Selected and focused look the same; no reduced-motion branch | **Partially fixed** | There is now an explicit `:focus-visible` rule (2 px surface gap + 4 px accent) distinct from selection (2 px accent + a 5 px soft glow), and `prefers-reduced-motion` is honoured in CSS *and* in `map.js` (`flyTo`/`easeTo` durations become 0; measured: sheet animation 0.01 ms). But both states are still a blue ring rather than the ring-vs-filled-edge the audit proposed. |

### (f) Safety invariants — spot-checked

All ten hold. The four that took the most checking: the strip has no dismiss
control and writes nothing to storage; four verdicts survive every filter
because the **last** count pill cannot be pressed out; a pressed-out pill keeps
printing its own count; and every group heading and the status line read
`counts`, never `results.length` — verified on a response where 627 of 835 rows
arrived.

---

## 2. Autocomplete matrix

Typed into the real field, one row per settled query. "Rows" excludes the
**Drop a pin instead** row, which is always last and always present.

| typed | rows | first row | secondary line |
|---|---|---|---|
| `1519 3` | 4 | `1519 3 Ave` | Manhattan · between 1517 and 1529 |
| `lex & 86` | 1 | `E 86 St & Lexington Ave` | Manhattan |
| `bleeker` | 1 | `Bleecker St` | Manhattan |
| `one world trade` | 1 | `1 World Trade Center` | Manhattan |
| `10021` | 1 | `10021` | Manhattan · ZIP centre of 1732 addresses |
| `86th st` | 3 | `E 86 St`, `W 86 St`, `86 St Transverse` | Manhattan — **E and W both offered** |
| `qwzzxy plffg` | 0 | *"No Manhattan match for that. Try a cross street, or drop a pin."* + pin row | — |
| *(focused, empty)* | 0 | **Drop a pin instead**, alone | — |

Every row carries a secondary line where the API sends one, plus a kind glyph
and a visually hidden ", address" / ", intersection" for the screen reader.

| behaviour | result |
|---|---|
| Debounce | One `/api/geocode` per pause. `1519 3` typed character by character fired **one** request. |
| Abort | Typing `broadway` with 170 ms gaps fired 7 requests; all 7 show `net::ERR_ABORTED` in `browser_network_requests` and only `q=broadway` completed. |
| ↓ then Enter | Selects: `aria-activedescendant` = `ac-option-0`, Enter fills the box and closes the list. Focus stays in the field, so the same Enter cannot also submit. |
| Escape | Closes: `hidden` true, `aria-expanded` false, `aria-activedescendant` removed. |
| Announced count | `role="status"` `aria-live="polite"`: "4 suggestions. Use the arrow keys to review." / "No matches. Drop a pin instead is the only suggestion." Cleared on close, silent on an empty focused field. **Added in this pass** (§4, F11). |
| Selection stores the point | Search sent `{"lat":40.778575223369,"lon":-73.95393367616,…}` — the picked point, not a re-geocode of the text (request body captured). |
| Mobile clipping | 390×844, drawer at the half detent: the list runs 534→785 px inside a rail-scroll of 399→844. Not clipped, at any detent where the field is reachable. `22` |

---

## 3. Numbers

Measured in the browser, warm, at 1440×900. The search round trip is
button-press → cards painted; the autocomplete round trip is keystroke → rows
on screen and includes the 150 ms debounce.

| | browser | server (curl) |
|---|---|---|
| Autocomplete `86th st` | 206 ms | 26–36 ms |
| Autocomplete `lex & 86` | 210 ms | 20 ms |
| Autocomplete `bleeker` | 219 ms | 44 ms |
| Autocomplete `one world trade` | 402 ms | 162–227 ms |
| Autocomplete `1519 3` | **642 ms** | **339–498 ms** |
| `/api/reverse` | — | 149 ms |
| `/api/segment` | 484 ms (sheet painted) | 76 ms |
| Search, 5 min walk (207 stretches) | 1,451 ms | 749 ms · 301 KB |
| Search, 10 min walk (835, 627 drawn) | 2,535 ms | 1,800–2,330 ms · 871 KB |
| Search, 20 min walk (2,469, 1,780 drawn) | 4,970 ms | 4,152 ms · 2.5 MB |
| Search, 30 min walk | — | 7,519 ms · 2.9 MB |

**Geocode is the slow endpoint per keystroke, and only for house numbers.** A
street, corner or ZIP query answers in 20–44 ms; `1519 3` — the address path —
takes 339–498 ms consistently, cold or warm, which is 5–15× the rest of the
matrix and the one shape that fires while the user is still typing. Recorded,
not chased: the backend is being worked on in parallel and its changes landed
mid-pass (search at a 10-minute radius fell from 5,808 ms to 1,800–2,330 ms
between the first and last measurement, and geocode from ~430 ms to 20–44 ms
for everything except the address path).

Rendering is not the bottleneck at any radius: 1,780 drawn spans and 12 cards
paint without a dropped frame, and the whole 20-minute search is within 20% of
its own network time.

---

## 4. Fixes made in this pass

Twelve defects in eleven commits, each verified in the browser and covered by a
test in `tests/test_web_static.py`.

| | defect | commit |
|---|---|---|
| F1 | A reversed pin read **"near near 110 E 84 ST"** — `/api/reverse` already says "near" and `dropPin` said it again. `13` is the before. | *Say "near" once about a dropped pin* |
| F2 | The destination box, the suggestion rows and the collapsed summary printed DOT's capitals (`1519 3 AVE`) next to title-cased cards. `placeLabel` is the cards' own formatter; the published string stays as the field's tooltip and as "As published: …" on the summary. | *Print a place name the way the cards print a street* |
| F3 | With a sheet open, `elementFromPoint` over the **OpenStreetMap link**, the **About & data** button, **Recentre** and MapLibre's zoom buttons all returned the sheet — every one of them unreachable, at 1440, 1280 and 1024. At 1024 the 520 px sheet also buried the legend and its SPEC §11 no-data sentence. | *Stop the sheet standing on the credit and the legend* |
| F4 | Searching from the keyboard left focus on `<body>`; the next Tab skipped both skip links and landed on a count pill, where Enter pressed out a verdict. | *Hand focus to the answer when the search card folds* |
| F5 | The ranked list stopped at 100 cards while the pill above read 308 legal, with no sentence — the subset rule in (f) 7 half-applied. | *Say what the ranked list is not showing* |
| F6 | A 0-legal answer (Bryant Park, 5 min, midday) rendered a "Results" heading over three collapsed group headers and nothing else. `20` is the before. | *(same commit)* |
| F7 | An `unmatched_signs` placeholder headlined "no sign data on this block" directly above the §11 sentence saying DOT does publish signs here, above one of them quoted verbatim. `15` before, `29` after. | *Stop calling a block with unplaced signs empty* |
| F8 | A pin outside the input bounding box came back 422 `validation_error` and was reported as "check the date, the time, and the walk radius". | *Refuse a point off the street network as coverage, not as arithmetic* |
| F9 | "Your destination is outside the outlined area on the map" — while the outline is the coverage **bounding box**, which visibly contains Hoboken. `14` before, `30` after. | *(same commit)* |
| F10 | The status line and the caveats stayed at full strength while the answer they describe dimmed for a second search. | *Dim the count sentence with the answer it counts* |
| F11 | The suggestion list announced nothing: no count, and no way to learn there were no matches, because the pin row means the popup is never empty. | *Say out loud how many suggestions arrived* |
| F12 | `Prefer` wrapped 3 + 1, orphaning the selected default; and `#results` taking focus drew a 4 px ring around 2,113 px of scroll height. | *Keep the selected preference off a line of its own* · *Keep the results hand-off from drawing a 2,113 px ring* |

`tests/test_web_static.py` gained eight tests covering these; no file was added
to `web/`, so its module and stylesheet manifests are unchanged.

---

## 5. What was checked and is clean

- **Console**: zero errors and zero warnings across every flow at all four
  widths, in both schemes. Two entries seen and accounted for: the browser's own
  log line for the 422 a Hoboken `/api/reverse` correctly returns, and one
  MapLibre warning at low zoom (§6).
- **Dark mode on every surface**: strip, rail, search card, count pills, cards,
  detail sheet, About sheet, legend, attribution, notices, toasts. Panels flip,
  the basemap and the verdict lines stay light, and the sheet keeps a real
  shadow (`0 32px 72px rgba(0,0,0,.66)`) against the light map. `19`, `33`
- **Reduced motion**: sheet animation 0.01 ms, card transitions 0.01 ms, map
  `flyTo`/`easeTo` 0 ms. The progress bar stops travelling and sits still.
- **Long street names**: no truncation anywhere. At 390 px,
  `Adam Clayton Powell Jr Blvd → Frederick Douglass Blvd` and
  `Ave of the Americas · west side` wrap in full — 0 elements with
  `scrollWidth > clientWidth` across 230 and 185 cards respectively. `31`
- **Keyboard-only task, end to end**: Tab ×4 → destination, type, ↓, Enter,
  Enter → answer; focus lands on the results; Tab → card #1; Enter → sheet with
  focus on Close; Escape → back on card #1.
- **All four verdict sheets** including both `no_data` gap kinds (`no_signs`
  `e20fbca565cb478f`, `unmatched_signs` `5dd8b7fe8bd4a328`). `04` `05` `06` `07`
  `29`
- **Count-pill filters**: hide a verdict on the map and in the list, keep
  printing its count, and refuse to hide the last one.
- **Zero results in coverage**: Randalls Island at a 1-minute walk gives a card,
  not an error. `16`

---

## 6. Open items

Frontend, not fixed — judgment rather than defect, or out of this pass's scope:

1. **The map prints no readout on hover** (P2-9's other half). Hovering a line
   highlights its card but names nothing on the map itself. A tooltip carrying
   street, side, verdict and walk time is still the audit's recommendation.
2. **Selected and focused are both blue rings.** Distinguishable (soft glow vs
   offset ring) but not the ring-vs-filled-edge the audit proposed. Changing it
   touches the card design the polish pass deliberately flattened.
3. **`Prefer` still wraps 3 + 1 in the 340 px tablet rail** (641–1099 px). It
   fits on one line at 1440, 1280 and on a phone.
4. **The 20-minute walk radius has no warning.** P1-9's recommendation included
   "warn before running a >20-minute walk radius"; the custom field reaches 30,
   which is a 7.5 s server call and a 2.9 MB response. The stale-dimming and the
   progress bar cover the wait; nothing sizes it up front.
5. **`Cheaper` and `Safer bet` can be no-ops and do not say so.** On the audited
   Upper East Side search every legal span arrived `money_value: 0.0`,
   `risk: 0.0`, so all four preference chips produce an identical order. The
   re-rank is correct; pressing a chip and seeing nothing move reads as broken.
   Reproducer: search 1519 3 Ave, Tue 09:45→11:45, 10 min, then press each
   `Prefer` chip and compare the top three.
6. **The basemap has a hard white edge below z12.** The PMTiles extract is
   Manhattan-only, so zooming out far enough to see the coverage box leaves the
   east half of the viewport blank white rather than a neutral fill. `30` shows
   the boundary. `web/basemap/` is outside this pass's scope.
7. **Console warning at low zoom**: `Image "townhall" could not be loaded` from
   MapLibre, a gap in the vendored sprite sheet. Reproducer: load the app, press
   Zoom out four times over New Jersey. Also `web/basemap/`.
8. **The zero-results state says the same sentence twice** — once in the status
   line, once on the card below it. Both are deliberate (the status line is the
   live region); it reads as a duplicate. `16`
9. **Static assets are served with no `Cache-Control`.** Editing a module and
   reloading can serve the previous bytes from the browser's heuristic cache
   (hit repeatedly during this pass: `format.js` kept a stale export). Harmless
   in use, a trap for anyone developing against `curbcheck serve`.

Backend, recorded rather than fixed (a cleanup agent owns `curbcheck/`):

10. **The engine's `reason` for an `unmatched_signs` span is "no sign data on
    this block", which is false of that span.** DOT publishes signs for the
    blockface; 332 of the 499 unmatched blockface-sides carry a NO
    STANDING/PARKING/STOPPING ANYTIME sign (`docs/VALIDATION.md` §5). The
    frontend now overrides this one sentence (F7), which means the UI and the
    API disagree about the same span — the fix belongs in the engine.
    Reproducer:
    ```
    curl -s localhost:8765/api/segment/5dd8b7fe8bd4a328 | jq '.segment.gap_kind'
    # "unmatched_signs", governing: [], other_on_block: one NO STANDING ANYTIME
    curl -s -X POST localhost:8765/api/search -H 'Content-Type: application/json' \
      -d '{"lat":40.754640,"lon":-73.975830,"t1":"2026-09-15T10:00",
           "t2":"2026-09-15T12:00","walk_minutes":5}' \
      | jq '.results[] | select(.reg_seg_id=="5dd8b7fe8bd4a328") | {gap_kind, reason}'
    # {"gap_kind": "unmatched_signs", "reason": "no sign data on this block"}
    ```
11. **`POST /api/search` is the slowest thing in the product and scales with the
    radius**, at 0.75 s / 1.8–2.3 s / 4.2 s / 7.5 s for a 5 / 10 / 20 / 30-minute
    walk. `api.js` times out at 20 s, so a 30-minute search on a slower machine
    is within a factor of three of failing. Reproducer: the `walk_minutes` sweep
    in §3.
12. **`/api/geocode` is 5–15× slower on the house-number path** (339–498 ms) than
    on streets, corners and ZIPs (20–44 ms), and it is the path that fires while
    the user is still typing. Reproducer:
    `curl -o /dev/null -w '%{time_total}\n' 'localhost:8765/api/geocode?q=1519%203'`
    against the same for `q=86th%20st`.
13. **Some server caveat strings start lower case** and are printed verbatim
    under a sentence-case heading: *"part of this window has no posted rule; read
    the curb"* is the first line of **Before you park** on every
    legality-by-absence verdict. `04`
