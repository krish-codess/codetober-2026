from __future__ import annotations

from typing import Any


class ApiError(Exception):
    """An expected, client-actionable failure rendered as the standard error body."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: list[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details
        self.headers = headers or {}
