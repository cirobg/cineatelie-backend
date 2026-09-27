"""`AuthenticationMiddleware` (backend spec §3, item 4): "verify the JWT; resolve user_id;
bind to context."

Exempts exactly the routes the API contract itself marks as not needing a bearer token
(backend spec §4.1): `/auth/exchange` (permission "—", takes the provider's own tokens in
the request body instead) and `/auth/refresh` (permission "refresh cookie"), plus the two
health probes. Every other route — including `/auth/logout`, which the contract marks
"authenticated" — goes through the full check.

A verified token with no matching `user_identities` row is not a contradiction: the
Supabase user genuinely exists but has never completed `/auth/exchange` on this backend
(BR-ID-01) — e.g. a fresh signup whose SPA call failed after Supabase accepted the OAuth
callback. Treated as `401 unauthenticated` (the route's own contract, not "500 confused"),
since from this API's perspective an un-provisioned subject is indistinguishable from an
invalid one until the exchange is redone.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from cineatelie.core.errors import AppError
from cineatelie.core.logging import user_id_var
from cineatelie.platform.auth.identity_resolution import resolve_user_id
from cineatelie.platform.auth.ports import IdentityProvider
from cineatelie.platform.db.uow import UnitOfWork

# /docs, /redoc and /openapi.json are FastAPI's own framework routes, mounted at root like
# /healthz and /readyz -- never under API_V1_PREFIX, and disabled outright in production
# (main.py), but reachable without a token in local/staging so a developer can see the API
# before they have one.
DEFAULT_EXEMPT_PATHS = frozenset(
    {"/healthz", "/readyz", "/docs", "/redoc", "/openapi.json", "/auth/exchange", "/auth/refresh"}
)


def _unauthenticated() -> AppError:
    return AppError(
        code="unauthenticated",
        message="Sua sessão expirou ou é inválida. Faça login novamente.",
        status_code=401,
    )


class AuthenticationMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        *,
        identity_provider: IdentityProvider,
        session_factory: async_sessionmaker,
        provider_name: str,
        exempt_paths: frozenset[str] = DEFAULT_EXEMPT_PATHS,
    ) -> None:
        super().__init__(app)
        self._identity_provider = identity_provider
        self._session_factory = session_factory
        # The tag stored in user_identities.provider for this deployment's active adapter
        # (ADR-006's AUTH_PROVIDER) — not hardcoded, so a future Keycloak/Ory adapter needs
        # no change here, only a different configured value and a different provider row.
        self._provider_name = provider_name
        # Overridable so `register_middleware` can prefix these with API_V1_PREFIX (the
        # router is mounted under it; the default here is only correct for a bare app, as in
        # this module's own unit tests).
        self._exempt_paths = exempt_paths

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in self._exempt_paths:
            return await call_next(request)

        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise _unauthenticated()

        identity = await self._identity_provider.verify(token)

        async with UnitOfWork(self._session_factory) as uow:
            user_id = await resolve_user_id(
                uow.session, provider=self._provider_name, provider_subject=identity.subject
            )
        if user_id is None:
            raise _unauthenticated()

        # request.state (backed by the shared ASGI scope) is what ErrorHandlerMiddleware
        # reads on failure -- see its docstring. user_id_var is set too, but only for
        # *downward* propagation to this same request's log lines (core.logging's
        # formatter); a contextvar set here is invisible to any middleware layer outside
        # this one regardless of reset, so it is never reset (harmless: one asyncio.Task
        # per request, per RequestIdMiddleware's docstring).
        request.state.user_id = user_id
        request.state.identity = identity
        user_id_var.set(str(user_id))
        return await call_next(request)
