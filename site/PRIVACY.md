# Privacy

This is the hosted copy of CurbCheck, at `curbcheck.orentirschwell.com`. The
whole app — the address search, the parking engine, the map — runs in your
browser. Nothing you type is sent anywhere.

The local version (`curbcheck serve`) never opens a socket at all once its data
is downloaded. This copy has to be fetched from somewhere, and that somewhere is
GitHub Pages. This page says exactly what that means.

## The two sentences the site itself shows

The About sheet prints these, from `health().hosting.text`. They are the short
form of everything below; copy them verbatim.

```text
This copy is served by GitHub Pages, so GitHub sees your IP address — which it says is logged and stored for security purposes — and which files and map tiles your browser asks for. Nothing you type or search for leaves the browser: the address index, the parking data and the map all run here, with no cookies, no storage, no analytics and no third-party requests.
```

## What never leaves your browser

- The address you type, and every keystroke of it. The autocomplete reads an
  index of NYC's published doors, corners and place names that was downloaded
  with the rest of the page.
- The destination you search for, the pin you drop, and any coordinate.
- The time window, the walk radius, and the results.

There is no server to send them to. Searching is a function call, not a request.

## What GitHub can see

GitHub Pages serves the files, so like any host it sees each request:

- Your IP address. GitHub says it "is logged and stored for security purposes".
  It does not publish how long those logs are kept, so neither do we.
- Which files your browser asked for — the page, the scripts, the data pack.
- Which map tiles you loaded. Tiles are per-square-of-map, so the set of tiles
  fetched is a coarse trace of which part of Manhattan you looked at. It is not
  your destination, and it is not what you typed, but it is not nothing.

That is the whole difference between this copy and running CurbCheck yourself.
If it matters for a particular search, run the local version.

## What the site does not do

- No cookies, no `localStorage`, no `sessionStorage`, no service worker.
- No analytics, no telemetry, no error reporting.
- No third-party requests of any kind: every file, including the map tiles and
  the one font, comes from this site's own domain.
- No accounts, no sign-in, no email address, no contact form.

## What the operator sees

Nothing. GitHub does not hand repository owners the Pages access logs, and
there is no other server in the picture.

## How current this is

The parking data is rebuilt weekly from NYC Open Data and the date of the build
that produced this copy is on the About sheet. If that build stops running, the
page says so rather than quietly showing old answers.

## The source

The code that does all of the above is public, including the script that builds
this site (`site/build.py`) and the one that makes it work offline
(`site/static/`). Read it rather than taking this page's word for it.
