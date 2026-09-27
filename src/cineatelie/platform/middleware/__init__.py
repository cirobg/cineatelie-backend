"""Wires the middleware chain (backend spec §3, "Middleware order") onto a FastAPI app.

**Why this file exists, and why the `add_middleware` calls below are in the *reverse* of the
spec's numbered order — the single most important thing to get right in this whole chain:**

Starlette's `app.add_middleware(cls)` does `self.user_middleware.insert(0, Middleware(cls))`
— each call pushes the new middleware to the *front* of the list, and `Starlette.
build_middleware_stack` then wraps outside-in over that list, so **the *last* `add_middleware`
call ends up as the *outermost* layer**, running first on the way in and last on the way out.

The spec's numbered list (1 `RequestIdMiddleware` ... 9 `ErrorHandlerMiddleware`) describes
the *actual runtime nesting* that makes each layer's guarantee hold — `RequestIdMiddleware`
must genuinely run first so every later layer and every log line has a `request_id` to use;
`ErrorHandlerMiddleware` must genuinely wrap *everything*, including `AuthenticationMiddleware`
raising `AppError`, or those exceptions fall through to Starlette's generic 500 instead of the
uniform envelope (see `error_handler.py`'s own docstring for why a FastAPI
`@app.exception_handler` cannot substitute for this). Calling `add_middleware` in the listed
1-9 order would therefore build the chain *backwards* — item 1 would end up innermost, item 9
outermost-but-one, exactly inverted from what's needed. Registering in reverse (7 down to 1,
then `ErrorHandlerMiddleware` last) is what actually produces the intended nesting. This is
exactly the kind of thing that reads as correct and fails only when run — verified below by
`tests/unit/test_middleware_chain.py`, which asserts the real execution order with a
throwaway ASGI app, not by re-reading this comment.

The database session is not opened here — each layer that needs one opens its own short-lived
`UnitOfWork`, per the spec: "a request that touches no data opens no transaction."
"""

from __future__ import annotations

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker

from cineatelie.platform.auth.ports import IdentityProvider
from cineatelie.platform.middleware.authentication import (
    DEFAULT_EXEMPT_PATHS as _AUTH_DEFAULT_EXEMPT_PATHS,
)
from cineatelie.platform.middleware.authentication import AuthenticationMiddleware
from cineatelie.platform.middleware.closure import (
    DEFAULT_EXEMPT_EVEN_WITH_A_TENANT_BOUND as _CLOSURE_DEFAULT_EXEMPT,
)
from cineatelie.platform.middleware.closure import ClosureMiddleware
from cineatelie.platform.middleware.edge import EdgeVerificationMiddleware
from cineatelie.platform.middleware.entitlement import (
    DEFAULT_EXEMPT_EVEN_WITH_A_TENANT_BOUND as _ENTITLEMENT_DEFAULT_EXEMPT,
)
from cineatelie.platform.middleware.entitlement import EntitlementMiddleware
from cineatelie.platform.middleware.error_handler import ErrorHandlerMiddleware
from cineatelie.platform.middleware.logging import LoggingMiddleware
from cineatelie.platform.middleware.request_id import RequestIdMiddleware
from cineatelie.platform.middleware.tenant_context import (
    DEFAULT_EXEMPT_PATHS as _TENANT_DEFAULT_EXEMPT_PATHS,
)
from cineatelie.platform.middleware.tenant_context import TenantContextMiddleware

_ROOT_LEVEL_PATHS = frozenset({"/healthz", "/readyz", "/docs", "/redoc", "/openapi.json"})


def _prefixed(prefix: str, paths: frozenset[str]) -> frozenset[str]:
    """Each middleware's exempt list is written relative to the router (`/auth/exchange`,
    `/me`...); the router is actually mounted under `API_V1_PREFIX` (backend spec §4: "Base:
    `/api/v1`"), so the *running* request path is `/api/v1/auth/exchange`. The two health
    probes and FastAPI's own docs routes are the exception — registered/mounted at root in
    `main.py` (health checks match Cloud Run's own convention; docs are framework routes,
    never business routes) — so those literal entries are left unprefixed."""
    return frozenset(path if path in _ROOT_LEVEL_PATHS else f"{prefix}{path}" for path in paths)


def register_middleware(
    app: FastAPI,
    *,
    identity_provider: IdentityProvider,
    session_factory: async_sessionmaker,
    provider_name: str,
    edge_verification_enabled: bool,
    edge_shared_secret: str,
    api_v1_prefix: str = "/api/v1",
) -> None:
    # Registered 7 -> 1, then ErrorHandlerMiddleware last: see module docstring.
    app.add_middleware(
        EntitlementMiddleware,
        session_factory=session_factory,
        exempt_even_with_a_tenant_bound=_prefixed(api_v1_prefix, _ENTITLEMENT_DEFAULT_EXEMPT),
    )
    app.add_middleware(
        ClosureMiddleware,
        session_factory=session_factory,
        exempt_even_with_a_tenant_bound=_prefixed(api_v1_prefix, _CLOSURE_DEFAULT_EXEMPT),
    )
    app.add_middleware(
        TenantContextMiddleware,
        session_factory=session_factory,
        exempt_paths=_prefixed(api_v1_prefix, _TENANT_DEFAULT_EXEMPT_PATHS),
    )
    app.add_middleware(
        AuthenticationMiddleware,
        identity_provider=identity_provider,
        session_factory=session_factory,
        provider_name=provider_name,
        exempt_paths=_prefixed(api_v1_prefix, _AUTH_DEFAULT_EXEMPT_PATHS),
    )
    app.add_middleware(
        EdgeVerificationMiddleware,
        enabled=edge_verification_enabled,
        shared_secret=edge_shared_secret,
    )
    app.add_middleware(LoggingMiddleware)
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(ErrorHandlerMiddleware)
