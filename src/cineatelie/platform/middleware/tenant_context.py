"""`TenantContextMiddleware` (backend spec §3, item 5): "read `X-Tenant-Id`; assert active
membership; bind `tenant_id` and `role`."

Exempt exactly the routes the API contract marks as needing no tenant header (§4.1): `/me`
(both methods — "No tenant header required"), `/auth/*`, and the two health probes. Every
other route, including `/me/permissions`, requires one. `ClosureMiddleware` and
`EntitlementMiddleware` (items 6-7) key off whether this middleware bound a tenant at all —
see their own docstrings — so this exemption list is the only place that decision is made.

The membership check needs no privileged bypass, unlike `AuthenticationMiddleware`'s user
lookup: `memberships_tenant_isolation` is keyed on `tenant_id = current_tenant_id()`, and the
*claimed* tenant id (from the header) is exactly what this check sets that GUC to before
querying — the same "set the GUC to the id you're about to check, then query" pattern the
isolation suite's own fixtures use. If the caller has no membership there, the row is
invisible either way (RLS or plain absence) and the result is identical: `403
tenant_forbidden`.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from cineatelie.core.errors import AppError
from cineatelie.core.logging import role_var, tenant_id_var
from cineatelie.platform.db.uow import UnitOfWork
from cineatelie.platform.middleware.path_match import path_is_exempt

DEFAULT_EXEMPT_PATHS = frozenset(
    {
        "/healthz",
        "/readyz",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/auth/exchange",
        "/auth/refresh",
        "/auth/logout",
        "/me",
    }
)

_HEADER = "X-Tenant-Id"

_MEMBERSHIP_QUERY = text(
    "SELECT role_code FROM memberships WHERE tenant_id = :tenant_id AND user_id = :user_id "
    "AND status = 'active'"
)


class TenantContextMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        *,
        session_factory: async_sessionmaker,
        exempt_paths: frozenset[str] = DEFAULT_EXEMPT_PATHS,
    ) -> None:
        super().__init__(app)
        self._session_factory = session_factory
        self._exempt_paths = exempt_paths

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if path_is_exempt(request.url.path, self._exempt_paths):
            return await call_next(request)

        raw_tenant_id = request.headers.get(_HEADER)
        if not raw_tenant_id:
            raise AppError(
                code="validation_error",
                message=f"Cabeçalho {_HEADER} é obrigatório.",
                status_code=400,
            )
        try:
            tenant_id = uuid.UUID(raw_tenant_id)
        except ValueError as exc:
            raise AppError(
                code="validation_error",
                message=f"Cabeçalho {_HEADER} inválido.",
                status_code=400,
            ) from exc

        user_id: uuid.UUID = request.state.user_id  # set by AuthenticationMiddleware, upstream
        async with UnitOfWork(self._session_factory, tenant_id=tenant_id, user_id=user_id) as uow:
            result = await uow.session.execute(
                _MEMBERSHIP_QUERY, {"tenant_id": str(tenant_id), "user_id": str(user_id)}
            )
            role_code = result.scalar_one_or_none()

        if role_code is None:
            raise AppError(
                code="tenant_forbidden",
                message="Você não tem acesso a este workspace.",
                status_code=403,
            )

        # request.state is what ClosureMiddleware/EntitlementMiddleware/ErrorHandlerMiddleware
        # read (see AuthenticationMiddleware's comment on why). tenant_id_var/role_var are set
        # too, only for downward propagation into this request's log lines; never reset, same
        # reasoning as user_id_var.
        request.state.tenant_id = tenant_id
        request.state.role = role_code
        tenant_id_var.set(str(tenant_id))
        role_var.set(role_code)
        return await call_next(request)
