"""ASGI entrypoint. `uvicorn cineatelie.main:app`.

M0 scope only: the process boots, logs structurally, and answers `/healthz` with no
dependency on anything else (ADR-019 — liveness must never depend on the database, or a
paused database looks identical to a broken app). `/readyz`, the JWT verifier, the tenant
and entitlement middleware, and every business route arrive in M1 onward as
`platform/auth`, `platform/db` and `modules/identity` are built.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from cineatelie import __version__
from cineatelie.core.config import get_settings
from cineatelie.core.errors import AppError
from cineatelie.core.logging import configure_logging

settings = get_settings()
configure_logging(settings.log_level)
logger = logging.getLogger("cineatelie")

app = FastAPI(title="Cine Ateliê API", version=__version__)

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


@app.exception_handler(AppError)
async def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
    envelope = exc.to_envelope()
    return JSONResponse(status_code=exc.status_code, content=envelope.model_dump())


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness only: the process is up and answering. No dependency checked here."""
    return {"status": "ok"}
