# Vendored frontend libraries

The frontend loads exactly the files in this directory: no bundler, no npm
install at build or run time, no CDN (SPEC §3.4 forbids remote script loading).
Each file below was extracted from the official npm registry tarball, whose
sha512 integrity was checked against `npm view <pkg> dist.integrity`.

Nothing here is edited. To change a version, download the new tarball, verify
its integrity, replace the files, and update this table.

## maplibre-gl 6.9.1

- License: BSD-3-Clause (`maplibre-gl/LICENSE.txt`)
- Tarball: `https://registry.npmjs.org/maplibre-gl/-/maplibre-gl-6.9.1.tgz`
- Tarball integrity: `sha512-piG2jNSq7crSTzj67UWDlTaxDjWvhFpfubZUuaofmaMcQKWw+kYn7mCfeh6S/Egr8+q8prBWcDqEaa6DdIQ6pw==`

| File | Bytes | SHA-256 |
|---|---|---|
| `maplibre-gl/maplibre-gl.mjs` | 585561 | `48c5f351a79881ae1f37ef36009b54e4683174f1c334e17d9ede9c1008371a31` |
| `maplibre-gl/maplibre-gl-shared.mjs` | 513644 | `6d03bca3b5e07d2726b121625281990589e26fcd735c45bcd4413b5d8d44690c` |
| `maplibre-gl/maplibre-gl-worker.mjs` | 19006 | `df44b49f9ab6db9e018a069bc73ceeab7e52f84b81fd65c2f280277244345670` |
| `maplibre-gl/maplibre-gl.css` | 83195 | `8e2dbbab312dc57656fbb76e9fa5308c75c9d7c7ba5808a7d55bcdb64cc813fa` |
| `maplibre-gl/LICENSE.txt` | 5984 | `ee5fc05a0677eaf69601d2c7db0d9ecd6cc27c3abc1d0733bc9ed34707cf8ef2` |

Why these four: v6 ships **ES modules only** — there is no UMD `maplibre-gl.js`
any more. `maplibre-gl.mjs` is the entry point and it `import`s
`./maplibre-gl-shared.mjs` by relative path, so the two must sit in the same
directory. At runtime MapLibre resolves `maplibre-gl-worker.mjs` against
`import.meta.url` and starts it as a module Worker, so that file must be
served from this directory too and cannot be renamed. The `*-dev.mjs` builds
and all `.map` files are deliberately not vendored.

## pmtiles 4.5.0

- License: BSD-3-Clause (declared in `package.json`; the npm tarball ships no
  LICENSE file, so there is nothing to copy here)
- Tarball: `https://registry.npmjs.org/pmtiles/-/pmtiles-4.5.0.tgz`
- Tarball integrity: `sha512-CBeD4SoUluFziGdy/8k7FOjQxQy486n+929W/tWophauvMICpkZ26vGBWhDt/6b1FZQWNaf4jFdT/5+q0Wii5w==`

| File | Bytes | SHA-256 |
|---|---|---|
| `pmtiles/pmtiles.js` | 20229 | `caf981bc46f6327ee7e65d5dc964d89d38a69f60edca2bd4c5c890c21b554c6c` |

`dist/pmtiles.js` is the **browser IIFE build, not an ES module**. Loading it
with a plain `<script src>` defines the global `pmtiles`, verified to export:
`Compression, EtagMismatch, FetchSource, FileSource, PMTiles, Protocol,
ResolvedValueCache, SharedPromiseCache, TileType, bytesToHeader, findTile,
getUint64, leafletRasterLayer, readVarint, tileIdToZxy, tileTypeExt,
zxyToTileId`. It is the right build to vendor because it inlines its one
runtime dependency, `fflate`; the ESM build at `dist/esm/index.js` does a bare
`import "fflate"`, which a browser cannot resolve without an import map or a
second vendored package.

## Wiring

`pmtiles.js` must load before the module that registers the protocol, since a
classic script and a module script do not share an execution order otherwise:

```html
<link rel="stylesheet" href="./vendor/maplibre-gl/maplibre-gl.css" />
<script src="./vendor/pmtiles/pmtiles.js"></script>
<script type="module" src="./app.js"></script>
```

```js
// map.js — a namespace import, not a default one. The v6 build exports named
// bindings only, and `import maplibregl from …` fails the module load with
// "does not provide an export named 'default'" (docs/DECISIONS.md D16(a)).
import * as maplibregl from "./vendor/maplibre-gl/maplibre-gl.mjs";

const protocol = new pmtiles.Protocol();
maplibregl.addProtocol("pmtiles", protocol.tile);
```
