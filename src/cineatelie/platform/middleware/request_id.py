"""`RequestIdMiddleware` (backend spec §3, "Middleware order", item 1): generate/propagate
`X-Request-Id`; bind to the log context. Reuses `uuid7()` (ADR-002) rather than introducing
a second id format (e.g. ULID) just to match the illustrative id shown in the error-model
example — this project already has exactly one sortable, unique id generator.

Deliberately never calls `request_id_var.reset(...)` — harmless to skip, since each request
runs in its own `asyncio.Task` and a `contextvars.Context` mutated inside one task never
leaks into another. This value is set purely for *downward* propagation to anything nested
further in this same request (`LoggingMiddleware`'s access log, any business-logic log line,
via `core.logging`'s formatter) — `ErrorHandlerMiddleware`, sitting *outside* this layer,
cannot read it at all regardless of reset, because `BaseHTTPMiddleware` runs each layer's
continuation in a separately spawned `anyio` task and a contextvar set inside one is invisible
once control returns to the layer that spawned it. See `error_handler.py`'s docstring for why
that class reads `request.state.request_id` instead — confirmed by running
`tests/unit/test_middleware_chain.py::test_error_envelope_carries_the_request_id_bound_by_request_id_middleware`,
which fails against the contextvar even with no reset anywhere in the chain.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from cineatelie.core.ids import uuid7
from cineatelie.core.logging import request_id_var

_HEADER = "X-Request-Id"


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get(_HEADER) or str(uuid7())
        request_id_var.set(request_id)
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers[_HEADER] = request_id
        return response
