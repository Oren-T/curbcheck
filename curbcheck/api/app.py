"""The FastAPI application: security headers, static files, and the basemap.

Assembly only; the endpoints live in `routes.py`. Two things here are security
controls rather than conveniences: the header middleware, which SPEC §3.4
requires on *every* response, and the absence of CORS middleware, which keeps
a page on any other origin from reading these answers.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from curbcheck.api import routes
from curbcheck.api.errors import ApiError, error_payload

LOGGER = logging.getLogger(__name__)
ACCESS_LOGGER = logging.getLogger("curbcheck.access")

# SPEC §3.4 verbatim, plus `worker-src 'self' blob:`. The vendored
# web/vendor/maplibre-gl/maplibre-gl.mjs starts its worker from
# `URL.createObjectURL(new Blob([...], {type: 'text/javascript'}))`, which
# `worker-src 'self'` alone blocks; verified by reading the vendored file.
# Drop `blob:` if MapLibre is ever revendored in a build that loads the worker
# module by URL instead.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; connect-src 'self'; worker-src 'self' blob:; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
)

BASEMAP_ROUTE = "/basemap/manhattan.pmtiles"


def security_headers(path: str) -> dict[str, str]:
    """The headers every response carries. `no-store` only on the JSON API."""
    headers = {
        "content-security-policy": CONTENT_SECURITY_POLICY,
        "x-content-type-options": "nosniff",
        "referrer-policy": "no-referrer",
    }
    if path.startswith("/api"):
        headers["cache-control"] = "no-store"
    return headers


class SecurityHeadersMiddleware:
    """Set the headers on the response start message.

    Written as raw ASGI rather than as a `BaseHTTPMiddleware`: the basemap is
    served as a streaming `FileResponse` that answers Range requests with 206
    and its own `Content-Range`, and rewrapping that in a `StreamingResponse`
    is exactly the kind of thing that quietly loses a header.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        extra = security_headers(str(scope.get("path", "")))

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in extra.items():
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


class AccessLogMiddleware:
    """One INFO line per request: method, path, status, duration. Never the query.

    uvicorn's own access log writes the whole request line, so
    `GET /api/geocode?q=123+E+85+St` would put the address the user typed on the
    terminal — the one place a destination leaked out of the process (threat T6,
    docs/SECURITY.md residual 2). `curbcheck serve` now passes
    `access_log=False` and this takes its place. It reads `scope["path"]`, which
    by ASGI definition excludes the query string, so there is no stripping step
    that could be forgotten: the query bytes are never in hand.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        # An exception on the way out never reaches a response start message,
        # and what the client sees in that case is the 500 the handler produces.
        status = 500

        async def send_with_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_with_status)
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            ACCESS_LOGGER.info(
                "%s %s %d %.1fms",
                scope.get("method", "?"),
                scope.get("path", ""),
                status,
                elapsed_ms,
            )


def create_app(db_path: Path, *, web_dir: Path, basemap_path: Path | None) -> FastAPI:
    """Build the server that reads `db_path` and serves `web_dir`.

    `basemap_path` may be None, and `web_dir` need not exist: a missing basemap
    or frontend degrades to a 404 on those paths while the API keeps working,
    which is what makes `curbcheck serve` useful before `curbcheck sync` has
    ever run.
    """
    app = FastAPI(
        title="CurbCheck",
        # The generated docs pages load Swagger UI from a CDN, which the CSP
        # forbids and the no-remote-assets rule (CLAUDE.md) forbids outright.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.db_path = Path(db_path)
    app.state.basemap_path = None if basemap_path is None else Path(basemap_path)

    app.add_middleware(SecurityHeadersMiddleware)
    # Added last, so it wraps everything and sees the status actually sent.
    app.add_middleware(AccessLogMiddleware)
    _install_exception_handlers(app)
    app.include_router(routes.router, prefix="/api")

    # Registered before the static mount so an unknown /api path answers in
    # JSON instead of falling through to the frontend's 404.
    app.add_api_route(
        "/api/{rest:path}", _unknown_api_path, methods=["GET", "POST", "PUT", "DELETE"]
    )
    app.add_api_route(BASEMAP_ROUTE, _basemap, methods=["GET", "HEAD"])

    web_root = Path(web_dir)
    if web_root.is_dir():
        # Mounted last: a mount at "/" matches every path, so everything with a
        # real route has to be registered above it.
        app.mount("/", StaticFiles(directory=web_root, html=True), name="web")
    else:
        LOGGER.warning("no web directory at %s; serving the API only", web_root)
    return app


async def _basemap(request: Request) -> FileResponse:
    """The PMTiles archive, with HTTP Range support.

    pmtiles.js reads the 127-byte header and then individual tiles as byte
    ranges, so this route must answer 206 with a `Content-Range`. Starlette's
    `FileResponse` implements RFC 7233 ranges (including multipart and 416) as
    of the pinned 1.6.0, so there is nothing to hand-roll; `tests/test_api.py`
    pins that behaviour so an upgrade that drops it fails loudly.
    """
    path: Path | None = request.app.state.basemap_path
    if path is None or not path.is_file():
        raise ApiError(404, "not_found", "no basemap installed")
    return FileResponse(
        path,
        media_type="application/octet-stream",
        content_disposition_type="inline",
    )


async def _unknown_api_path() -> None:
    """Catch every /api path with no route, so the API always answers in JSON."""
    raise ApiError(404, "not_found", "no such endpoint")


def _install_exception_handlers(app: FastAPI) -> None:
    async def handle_api_error(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, ApiError)
        return _json_error(request, exc.status_code, exc.code, exc.message)

    async def handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, RequestValidationError)
        return _json_error(request, 422, "validation_error", _summarize_validation(exc))

    async def handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, StarletteHTTPException)
        status_code = exc.status_code
        code = {400: "invalid_request", 404: "not_found"}.get(status_code, "http_error")
        return _json_error(request, status_code, code, _http_message(status_code))

    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # The traceback goes to the log, never to the browser (SPEC §3.4).
        LOGGER.exception("unhandled error serving %s", request.url.path, exc_info=exc)
        return _json_error(request, 500, "internal_error", "internal error")

    app.add_exception_handler(ApiError, handle_api_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    # Starlette installs this one outside the middleware stack, so it applies
    # the headers itself rather than relying on SecurityHeadersMiddleware.
    app.add_exception_handler(Exception, handle_unexpected)


def _json_error(request: Request, status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=error_payload(code, message),
        headers=security_headers(request.url.path),
    )


def _summarize_validation(exc: RequestValidationError) -> str:
    """One line naming the fields that failed, with no input values echoed back."""
    parts = []
    for error in exc.errors()[:5]:
        location = ".".join(str(item) for item in error.get("loc", ()) if item != "body")
        parts.append(
            f"{location}: {error.get('msg', 'invalid')}" if location else str(error.get("msg"))
        )
    return "; ".join(parts) or "invalid request"


def _http_message(status_code: int) -> str:
    return {
        400: "bad request",
        404: "not found",
        405: "method not allowed",
    }.get(status_code, "request failed")
