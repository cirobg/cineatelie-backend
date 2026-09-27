"""`ErrorHandlerMiddleware` (backend spec §3, item 9): "map exceptions to the uniform error
envelope."

This must be real Starlette middleware, not a FastAPI `@app.exception_handler` — the latter
is wired into Starlette's `ExceptionMiddleware`, which sits **innermost**, right next to the
router (see `Starlette.build_middleware_stack`: `[ServerErrorMiddleware] + user_middleware +
[ExceptionMiddleware]`, wrapped outside-in). An `AppError` raised by `AuthenticationMiddleware`
or any other *user* middleware never reaches it — it propagates straight to
`ServerErrorMiddleware` and becomes a bare 500. This class must therefore be the
**outermost** layer of the whole chain (see `platform/middleware/__init__.py`'s wiring,
which explains why it is registered last), so it wraps every other middleware here as well
as the router.

**Reads `request.state.request_id`, never the `request_id_var` contextvar.** `BaseHTTPMiddleware`
runs each layer's `call_next` continuation in a separately spawned `anyio` task (see its
`starlette/middleware/base.py` source: `task_group.start_soon(coro)`), and a `contextvars.set()`
made inside a spawned child task is invisible once control returns to the parent task that
spawned it — confirmed by running
`tests/unit/test_middleware_chain.py::test_error_envelope_carries_the_request_id_bound_by_request_id_middleware`,
which failed against `request_id_var.get()` even with `RequestIdMiddleware` registered strictly
inside this one. `request.state`, by contrast, is backed by the single `scope["state"]` dict
shared by *every* layer's own `Request` wrapper around the same ASGI `scope` (`Starlette.
HTTPConnection.state`), so a value set by an inner layer is visible here regardless of the
task-spawn boundary. Contextvars still work fine for *downward* propagation (an outer layer's
`.set()` before calling `call_next`, read by anything nested further in — how `LoggingMiddleware`
and ordinary business-logic log lines pick up `request_id`/`tenant_id`/`user_id` via
`core.logging`'s formatter); only the *upward* direction this class needs is broken.
"""

from __future__ import annotations

import logging

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from cineatelie.core.errors import AppError

logger = logging.getLogger("cineatelie.errors")


class ErrorHandlerMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        try:
            return await call_next(request)
        except AppError as exc:
            envelope = exc.to_envelope(request_id=getattr(request.state, "request_id", None))
            return JSONResponse(status_code=exc.status_code, content=envelope.model_dump())
        except Exception:
            # Same request.state-not-contextvar reasoning as the request_id above: whichever
            # of Authentication/TenantContextMiddleware ran before the failure bound these.
            logger.exception(
                "unhandled_exception",
                extra={
                    "tenant_id": getattr(request.state, "tenant_id", None),
                    "user_id": getattr(request.state, "user_id", None),
                },
            )
            envelope = AppError(
                code="internal_error",
                message="Ocorreu um erro inesperado. Tente novamente em instantes.",
                status_code=500,
            ).to_envelope(request_id=getattr(request.state, "request_id", None))
            return JSONResponse(status_code=500, content=envelope.model_dump())
