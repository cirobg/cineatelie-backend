"""`LoggingMiddleware` (backend spec §3, item 2): "structured access log with duration and
query count." Query count arrives with `platform/db` instrumentation (SQLAlchemy event
hooks) in a later pass — this is the request-level half: one JSON line per request, with
`request_id` already bound (this middleware sits inside `RequestIdMiddleware`, see
`platform/middleware/__init__.py`'s wiring), so nothing here needs to thread it through by
hand.
"""

from __future__ import annotations

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("cineatelie.access")


class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        started_at = time.perf_counter()
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - started_at) * 1000, 2)

        logger.info(
            "request_handled",
            extra={
                "http_method": request.method,
                "http_path": request.url.path,
                "http_status": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        return response
