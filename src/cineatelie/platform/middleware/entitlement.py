"""`EntitlementMiddleware` (backend spec §3, item 7; ADR-006 "Access gates"): "assert the
subscription window; exempt `/me`, `/billing/*`, `/health*`." Unconditional otherwise —
"previously authenticated users with a lapsed entitlement are refused on every business
route... locking someone out of the page where they would pay is self-defeating," hence
`/billing/*` staying reachable.

As with `ClosureMiddleware`, a route with no tenant bound (checked first) has nothing to
assert a subscription window *for* and passes through untouched.

**Open question, not yet resolved by the backend spec, left for `modules/identity`:**
`/tenant/closure*` and `/tenant/reopen` are `ClosureMiddleware`-exempt (BR-TEN-09) but are
*not* in this middleware's own exempt list, so a tenant that is both closed and lapsed would
get `402 subscription_inactive` on `GET /tenant/closure` rather than seeing the closure
status. `/tenant/reopen` separately re-checks `plan_active` per BR-TEN-11 inside its own
handler regardless, so the practical impact is narrow — but the spec is unambiguous only
about `/billing/*`, so this middleware exempts only what it explicitly says.
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

DEFAULT_EXEMPT_EVEN_WITH_A_TENANT_BOUND = frozenset({"/billing/*"})

_SUBSCRIPTION_QUERY = text(
    "SELECT is_within_window FROM v_active_subscription WHERE tenant_id = :tenant_id"
)


class EntitlementMiddleware(BaseHTTPMiddleware):
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
            result = await uow.session.execute(_SUBSCRIPTION_QUERY, {"tenant_id": str(tenant_id)})
            is_within_window = result.scalar_one_or_none()

        # No row at all (never subscribed) counts as inactive, same as an expired window.
        if not is_within_window:
            raise AppError(
                code="subscription_inactive",
                message="A assinatura deste workspace está inativa.",
                status_code=402,
            )

        return await call_next(request)
