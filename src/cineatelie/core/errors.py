"""The one error shape every endpoint returns (ADR-014 §"Errors").

    { "error": { "code", "message", "details", "request_id" } }

`code` is the stable, machine-readable identifier the client switches on. `message` is
pt-BR and user-displayable — the backend owns user-facing error copy (ADR-014). Full
wiring — the `ErrorHandlerMiddleware`, `request_id` correlation with structured logging —
lands in M1 with the rest of the middleware chain (backend spec §3.2). What is here now is
the shape itself and the exception type application code raises, so both are stable before
anything depends on them.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ErrorBody(BaseModel):
    code: str
    message: str
    details: list[dict[str, Any]] = []
    request_id: str | None = None


class ErrorEnvelope(BaseModel):
    error: ErrorBody


class AppError(Exception):
    """Base for every application-raised error that must reach the client as the uniform
    envelope. Raised with a stable `code`, an HTTP `status_code`, and a pt-BR `message`.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or []

    def to_envelope(self, *, request_id: str | None = None) -> ErrorEnvelope:
        return ErrorEnvelope(
            error=ErrorBody(
                code=self.code,
                message=self.message,
                details=self.details,
                request_id=request_id,
            )
        )
