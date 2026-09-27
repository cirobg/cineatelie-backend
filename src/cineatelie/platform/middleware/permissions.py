"""Route dependency `require_permission("<code>")` (backend spec §3, item 8): the final gate,
run after every middleware above has bound `request.state.role`. A plain FastAPI dependency
rather than a Starlette middleware, because the required permission code differs per route —
exactly what dependencies, not app-wide middleware, are for.

`roles` / `permissions` / `role_permissions` carry no `tenant_id` and have no RLS policy
(platform-wide reference data, never tenant-owned), so the check needs no special session
context.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from cineatelie.core.errors import AppError
from cineatelie.platform.db.uow import UnitOfWork

_PERMISSION_QUERY = text(
    "SELECT EXISTS (SELECT 1 FROM role_permissions "
    "WHERE role_code = :role_code AND permission_code = :permission_code)"
)


def require_permission(permission_code: str) -> Callable[[Request], Awaitable[None]]:
    async def dependency(request: Request) -> None:
        role_code = getattr(request.state, "role", None)
        if role_code is None:
            # Only reachable if a route uses this dependency without also sitting behind
            # TenantContextMiddleware — a wiring bug, not a real user-facing state, but
            # fails closed with the same code a real permission shortfall would.
            raise AppError(
                code="permission_denied",
                message="Sua função não tem permissão para esta ação.",
                status_code=403,
            )

        session_factory: async_sessionmaker = request.app.state.session_factory
        async with UnitOfWork(session_factory) as uow:
            result = await uow.session.execute(
                _PERMISSION_QUERY,
                {"role_code": role_code, "permission_code": permission_code},
            )
            granted = result.scalar_one()

        if not granted:
            raise AppError(
                code="permission_denied",
                message="Sua função não tem permissão para esta ação.",
                status_code=403,
            )

    return dependency
