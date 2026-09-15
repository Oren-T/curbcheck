# Vendored typeface

One file, served from `'self'`. The CSP in `curbcheck/api/app.py` names no
`font-src`, so `default-src 'self'` covers it; nothing here may ever be loaded
from a CDN (SPEC §3.4).

## Inter 4.1 — variable, Latin subset

- License: **SIL Open Font License 1.1**, copied verbatim to `LICENSE.txt`.
  The OFL permits redistribution of the subset as long as the licence travels
  with it and the font is not sold on its own.
- Upstream: `https://github.com/rsms/inter/releases/download/v4.1/Inter-4.1.zip`
  (33,707,794 bytes, SHA-256
  `9883fdd4a49d4fb66bd8177ba6625ef9a64aa45899767dde3d36aa425756b11e`), from
  which `InterVariable.ttf` (879,708 bytes, SHA-256
  `4989b125924991b90d05b2d16e0e388c48f7d5bb8b30539bbf9c755278d0ccaf`,
  `Version 4.001;git-9221beed3`) was extracted.

| File                        | Bytes | SHA-256                                                            |
| --------------------------- | ----- | ------------------------------------------------------------------ |
| `InterVariable-latin.woff2` | 72292 | `4114ff4db2b0a80b833f8d47f3d4fb9b796d2f83eca25410c3f015b98c8bc39e` |
| `LICENSE.txt`               | 4380  | `262481e844521b326f5ecd053e59b98c8b2da78c8ee1bdbb6e8174305e54935a` |

### How the subset was produced

`fontTools` 4.65.0 with `brotli` 1.2.0, both already in the dev environment:

```
pyftsubset InterVariable.ttf \
  --output-file=InterVariable-latin.woff2 \
  --flavor=woff2 \
  --layout-features+=tnum \
  --unicodes="U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,\
U+0304,U+0308,U+0329,U+2000-206F,U+2074,U+20AC,U+2122,U+2190-2193,U+2212,\
U+2215,U+2022,U+25B8,U+2713,U+FEFF,U+FFFD" \
  --name-IDs="*" --name-legacy --name-languages="*"
```

The range list is the Google Fonts `latin` block plus the six characters this
UI actually draws outside it: `→` (U+2192, the cross-street arrow), `·`
(U+00B7), `•` (U+2022), `–`/`—`, `©`/`§` in the notice, and `✓` (U+2713).
`tnum` is added to the default feature set because the price and walk-time
columns are set with `font-variant-numeric: tabular-nums`, which is a no-op
without it.

Both variable axes survive the subset — `opsz` 14–32 and `wght` 100–900 — so
one 72 KB file covers every weight and optical size the UI asks for. Manhattan
street names are Latin; a non-Latin glyph falls through to the system stack in
`--font-sans`, which is why that stack is kept behind Inter rather than
replaced by it. The _basemap's_ label glyphs are a separate, unrelated vendored
set (`web/basemap/fonts/`, Noto PBF ranges) and are not affected by this file.
