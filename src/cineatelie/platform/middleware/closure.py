"""`ClosureMiddleware` (backend spec §3, item 6; BR-TEN-09): "refuse every business route
with `403 tenant_closed` when the workspace is closed; exempt `/me`, `/auth/*`,
`/tenant/closure*`, `/tenant/reopen`, `/billing/*`, `/health*`."

Two independent reasons a route passes through untouched:

1. **No tenant is bound at all** — `TenantContextMiddleware` exempted this path (`/me`,
   `/auth/*`, health probes), so there is nothing to check a closure state *against*. This is
   checked first and covers most of the spec's own exemption list without needing to repeat
   it here.
2. **A tenant is bound, but the route is one of the closure/billing surfaces that must stay
   reachable precisely *because* the workspace might be closed** (`/tenant/closure*`,
   `/tenant/reopen`, `/billing/*`) — BR-TEN-09's whole point is that these remain usable while
   everything else is refused, so this list is checked explicitly even when a tenant *is*
   bound.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from cineatelie.core.errors import AppError
from cineatelie.platform.db.uow import UnitOfWork
from cineatelie.platform.middleware.path_match import path_is_exempt

DEFAULT_EXEMPT_EVEN_WITH_A_TENANT_BOUND = frozenset(
    {"/tenant/closure*", "/tenant/reopen", "/billing/*"}
)

_TENANT_STATUS_QUERY = text("SELECT status FROM tenants WHERE id = :tenant_id")


class ClosureMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        *,
        session_factory: async_sessionmaker,
        exempt_even_with_a_tenant_bound: frozenset[str] = DEFAULT_EXEMPT_EVEN_WITH_A_TENANT_BOUND,
    ) -> None:
        super().__init__(app)
        self._session_factory = session_factory
        self._exempt_even_with_a_tenant_bound = exempt_even_with_a_tenant_bound

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        tenant_id = getattr(request.state, "tenant_id", None)
        if tenant_id is None or path_is_exempt(
            request.url.path, self._exempt_even_with_a_tenant_bound
        ):
            return await call_next(request)

        user_id = request.state.user_id
        async with UnitOfWork(self._session_factory, tenant_id=tenant_id, user_id=user_id) as uow:
            result = await uow.session.execute(_TENANT_STATUS_QUERY, {"tenant_id": str(tenant_id)})
            status = result.scalar_one()

        if status == "closed":
            raise AppError(
                code="tenant_closed",
                message="Este workspace está encerrado.",
                status_code=403,
            )

        return await call_next(request)
