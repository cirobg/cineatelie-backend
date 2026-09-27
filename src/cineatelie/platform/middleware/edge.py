"""`EdgeVerificationMiddleware` (backend spec §3, item 3; ADR-015 §3, "Policy enforcement at
the edge"): "the API additionally requires a shared secret header proving the request
traversed Cloudflare (`EDGE_SHARED_SECRET`)... defence in depth, not the gate — bypassing the
edge yields no privilege" (every later gate still runs independently either way).

Enforced in `production` only (backend spec §3: "production only") — local Docker Compose
and any environment without the Cloudflare Pages Function proxy in front have no edge to
prove, and `staging` runs its own Cloud Run service the same way production does once it
sits behind the same proxy.

Header name is this project's own choice (ADR-015 names the *value*'s env var,
`EDGE_SHARED_SECRET`, but never a literal header string) — `X-Edge-Secret`, matching the
`X-Tenant-Id` / `X-Request-Id` naming already used elsewhere in the API contract. The Pages
Function proxy must send exactly this header.
"""

from __future__ import annotations

import hmac

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from cineatelie.core.errors import AppError

HEADER_NAME = "X-Edge-Secret"


class EdgeVerificationMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, enabled: bool, shared_secret: str) -> None:
        super().__init__(app)
        self._enabled = enabled
        self._shared_secret = shared_secret

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if self._enabled:
            provided = request.headers.get(HEADER_NAME, "")
            if not provided or not hmac.compare_digest(provided, self._shared_secret):
                raise AppError(
                    code="unauthenticated",
                    message="Sua sessão expirou ou é inválida. Faça login novamente.",
                    status_code=401,
                )
        return await call_next(request)
