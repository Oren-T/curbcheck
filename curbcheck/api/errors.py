"""The one error shape the API returns.

Every failure leaves the server as `{"error": {"code", "message"}}`. Messages
are written for a person reading the UI and deliberately carry no file path,
SQL, or traceback: the server log gets those (SPEC §3.4, threat T4).
"""

from __future__ import annotations

from typing import Any


class ApiError(Exception):
    """A failure with a chosen status code and a message safe to show the user."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def error_payload(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


def database_unavailable() -> ApiError:
    return ApiError(503, "database_unavailable", "database not found; run `curbcheck sync`")
