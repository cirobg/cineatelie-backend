"""ASGI entrypoint. `uvicorn cineatelie.main:app`.

M1: the full middleware chain (backend spec §3) is wired here — `platform/auth`'s
`SupabaseIdentityProvider`, `platform/db`'s engine/session factory, and
`platform/middleware.register_middleware`. Business routes arrive with `modules/identity`
onward. Middleware is registered here, synchronously, at import time — not inside a
`lifespan` — because Starlette builds and freezes its middleware stack on the app's first
`__call__` (which is the ASGI `lifespan` scope itself), so `add_middleware` after that point
raises `RuntimeError: Cannot add middleware after an application has started`. `lifespan` is
used only for what is genuinely async: closing the `httpx` client and disposing the engine on
shutdown.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from cineatelie import __version__
from cineatelie.core.config import get_settings
from cineatelie.core.logging import configure_logging
from cineatelie.modules.identity.adapters.router import router as identity_router
from cineatelie.platform.auth.jwks import JwksCache
from cineatelie.platform.auth.ports import IdentityProvider
from cineatelie.platform.auth.supabase import SupabaseIdentityProvider
from cineatelie.platform.db.engine import create_engine, dispose_engine
from cineatelie.platform.db.session import create_session_factory
from cineatelie.platform.middleware import register_middleware

settings = get_settings()
configure_logging(settings.log_level)
logger = logging.getLogger("cineatelie")

http_client = httpx.AsyncClient(timeout=10.0)
engine = create_engine(settings)
session_factory = create_session_factory(engine)

jwks_cache = JwksCache(
    jwks_url=settings.auth_jwks_url,
    ttl_s=settings.auth_jwks_cache_ttl_s,
    http_client=http_client,
)
identity_provider: IdentityProvider = SupabaseIdentityProvider(
    jwks_cache=jwks_cache,
    issuer=settings.auth_jwt_issuer,
    audience=settings.auth_jwt_audience,
    supabase_url=settings.supabase_url,
    service_role_key=settings.supabase_service_role_key,
    http_client=http_client,
    clock_skew_s=settings.auth_clock_skew_s,
)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await http_client.aclose()
    await dispose_engine()


# Docs are disabled outright in production (architecture doc, Appendix A: "APP_ENV ...
# Gates docs exposure"); reachable without a token in local/staging (AuthenticationMiddleware
# exempts /docs, /redoc, /openapi.json) so a developer can see the API before they have one.
_docs_enabled = settings.app_env != "production"
app = FastAPI(
    title="Cine Ateliê API",
    version=__version__,
    lifespan=lifespan,
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)
app.state.session_factory = session_factory
app.state.identity_provider = identity_provider
app.state.jwks_cache = jwks_cache

# Empty by default: the SPA reaches this API through the same-origin Cloudflare Pages
# Function proxy while no domain is bought (launch dependency A0), so same-origin requests
# need no CORS at all. Set CORS_ALLOWED_ORIGINS only for a deployment that calls this API
# cross-origin directly.
if settings.cors_allowed_origins_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

register_middleware(
    app,
    identity_provider=identity_provider,
    session_factory=session_factory,
    provider_name=settings.auth_provider,
    edge_verification_enabled=settings.edge_verification_enabled,
    edge_shared_secret=settings.edge_shared_secret,
    api_v1_prefix=settings.api_v1_prefix,
)

app.include_router(identity_router, prefix=settings.api_v1_prefix)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness only: the process is up and answering. No dependency checked here."""
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> JSONResponse:
    """Readiness: the JWKS cache can be populated (ADR-006's readiness probe — "fails if the
    cache is cold and unfetchable"). `ensure_fetched()` triggers the first attempt itself,
    since nothing else does until a real request carries a token. Does not check the
    database: a paused Supabase project must be diagnosable as a database problem, not a
    generic unready process (ADR-009, "Free-tier realities")."""
    if not await jwks_cache.ensure_fetched():
        return JSONResponse(
            status_code=503, content={"status": "not_ready", "reason": "jwks_cache_cold"}
        )
    return JSONResponse(status_code=200, content={"status": "ok"})
