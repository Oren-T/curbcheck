"""Serve `web/` plus canned API answers, so the frontend can be exercised alone.

**This script and `dev_mock_data.json` exist for frontend work without a
database.** The real API (`curbcheck serve`) needs a synced
`data/curbcheck.sqlite`, which takes minutes to build and cannot be edited into
a particular shape; these two files let a page be developed against any response
the contract allows.

It answers the five routes in docs/API.md from `scripts/dev_mock_data.json`,
serves the vendored static files, and range-serves the PMTiles archive, which
pmtiles.js requires. It also sets the same CSP the real server sets, so a
violation shows up here rather than in production. Bound to 127.0.0.1; never a
production server.

Keep `dev_mock_data.json` in step with docs/API.md: it is the frontend's only
other statement of the response shape, and a stale one teaches the page to
expect fields the server no longer sends.

Run: `python scripts/dev_mock_server.py [--port 8765]`.
"""

from __future__ import annotations

import argparse
import json
import re
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = REPO_ROOT / "web"
PMTILES_PATH = REPO_ROOT / "data" / "basemap" / "manhattan.pmtiles"
MOCK_DATA_PATH = Path(__file__).resolve().parent / "dev_mock_data.json"

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; connect-src 'self'; worker-src 'self' blob:; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
)
SEGMENT_PATH = re.compile(r"^/api/segment/(?P<reg_seg_id>[A-Za-z0-9:_.%-]{1,80})$")


class MockHandler(SimpleHTTPRequestHandler):
    """Static files from web/, canned JSON from /api, ranged bytes for the basemap."""

    def end_headers(self) -> None:
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/basemap/manhattan.pmtiles":
            self._send_pmtiles()
        elif path.startswith("/api/"):
            self._send_json(*self._api_get(path))
        else:
            super().do_GET()

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] != "/api/search":
            self._send_json({"error": {"code": "not_found", "message": "no such route"}}, 404)
            return
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._send_json(mock_data()["search"], 200)

    def _api_get(self, path: str) -> tuple[dict, int]:
        data = mock_data()
        if path == "/api/health":
            return data["health"], 200
        if path == "/api/sync-status":
            return data["sync_status"], 200
        if path == "/api/geocode":
            return data["geocode"], 200
        match = SEGMENT_PATH.match(path)
        if match:
            reg_seg_id = match.group("reg_seg_id").replace("%3A", ":")
            segment = data["segments"].get(reg_seg_id)
            if segment is None:
                return {"error": {"code": "not_found", "message": "no such segment"}}, 404
            return segment, 200
        return {"error": {"code": "not_found", "message": "no such route"}}, 404

    def _send_json(self, payload: dict, status: int) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_pmtiles(self) -> None:
        if not PMTILES_PATH.is_file():
            self.send_error(404, "no basemap archive")
            return
        total = PMTILES_PATH.stat().st_size
        start, end = parse_range(self.headers.get("Range"), total)
        with PMTILES_PATH.open("rb") as handle:
            handle.seek(start)
            body = handle.read(end - start + 1)
        self.send_response(206 if self.headers.get("Range") else 200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(body)))
        if self.headers.get("Range"):
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.end_headers()
        self.wfile.write(body)


def parse_range(header: str | None, total: int) -> tuple[int, int]:
    """`bytes=start-end` to an inclusive pair, clamped to the file. Whole file if absent."""
    if not header or not header.startswith("bytes="):
        return 0, total - 1
    start_text, _, end_text = header[len("bytes=") :].partition("-")
    start = int(start_text) if start_text else 0
    end = int(end_text) if end_text else total - 1
    return max(0, start), min(end, total - 1)


def mock_data() -> dict:
    """Re-read on every request so the canned responses can be edited while serving."""
    return json.loads(MOCK_DATA_PATH.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    handler = partial(MockHandler, directory=str(WEB_DIR))
    server = HTTPServer(("127.0.0.1", args.port), handler)
    print(f"mock CurbCheck on http://127.0.0.1:{args.port} (serving {WEB_DIR})")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
