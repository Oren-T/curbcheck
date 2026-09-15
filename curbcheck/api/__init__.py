"""The local HTTP server: FastAPI app, security headers, and the JSON routes.

`docs/API.md` is the contract this package implements and the frontend codes
against. Nothing here reaches the network; the only I/O is a read-only SQLite
connection and the static files under `web/`.
"""
