"""The identity module's routes (backend spec §4.1). Registered on `app.include_router`
under `API_V1_PREFIX` (see `main.py`); the middleware chain's exempt-path lists are
prefixed to match (`platform/middleware/__init__.py::_prefixed`).

Dependencies read `request.app.state` directly, the same pattern `require_permission`
already uses — no DI container, just what `main.py` put there at startup.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from cineatelie.core.config import Settings, get_settings
from cineatelie.core.errors import AppError
from cineatelie.modules.identity.adapters.schemas import (
    ExchangeRequest,
    ExchangeResponse,
    MeOut,
    PatchMeRequest,
    PermissionsResponse,
    RefreshResponse,
)
from cineatelie.modules.identity.application.me import build_me_payload
from cineatelie.modules.identity.application.provisioning import (
    ProvisioningConflictError,
    provision_or_resolve_user,
)
from cineatelie.platform.auth.ports import IdentityProvider
from cineatelie.platform.db.uow import UnitOfWork

logger = logging.getLogger(__name__)

router = APIRouter()

_REFRESH_COOKIE_NAME = "refresh_token"


def _get_identity_provider(request: Request) -> IdentityProvider:
    return request.app.state.identity_provider


def _get_session_factory(request: Request) -> async_sessionmaker:
    return request.app.state.session_factory


def _set_refresh_cookie(response: Response, *, refresh_token: str, settings: Settings) -> None:
    response.set_cookie(
        key=_REFRESH_COOKIE_NAME,
        value=refresh_token,
        httponly=True,
        secure=settings.app_env != "local",  # plain HTTP locally; a Secure cookie is dropped
        samesite="lax",
        path=f"{settings.api_v1_prefix}/auth",  # only the two endpoints that need it
    )


@router.post("/auth/exchange", response_model=ExchangeResponse)
async def exchange(
    body: ExchangeRequest,
    response: Response,
    identity_provider: IdentityProvider = Depends(_get_identity_provider),
    session_factory: async_sessionmaker = Depends(_get_session_factory),
    settings: Settings = Depends(get_settings),
) -> ExchangeResponse:
    identity = await identity_provider.verify(body.access_token)
    try:
        outcome = await provision_or_resolve_user(
            session_factory, identity, provider_name=settings.auth_provider
        )
    except ProvisioningConflictError as exc:
        logger.warning("provisioning_conflict", extra={"error": str(exc)})
        raise AppError(
            code="identity_conflict",
            message="Já existe uma conta com este e-mail. Entre em contato com o suporte.",
            status_code=409,
        ) from exc

    _set_refresh_cookie(response, refresh_token=body.refresh_token, settings=settings)
    payload = await build_me_payload(session_factory, user_id=outcome.user_id)
    return ExchangeResponse(
        access_token=body.access_token, **MeOut.from_payload(payload).model_dump()
    )


@router.post("/auth/refresh", response_model=RefreshResponse)
async def refresh(
    request: Request,
    response: Response,
    identity_provider: IdentityProvider = Depends(_get_identity_provider),
    settings: Settings = Depends(get_settings),
) -> RefreshResponse:
    refresh_token = request.cookies.get(_REFRESH_COOKIE_NAME)
    if not refresh_token:
        raise AppError(
            code="unauthenticated",
            message="Sua sessão expirou ou é inválida. Faça login novamente.",
            status_code=401,
        )

    session = await identity_provider.refresh(refresh_token)
    _set_refresh_cookie(response, refresh_token=session.refresh_token, settings=settings)
    return RefreshResponse(access_token=session.access_token)


@router.post("/auth/logout", status_code=204)
async def logout(
    request: Request,
    response: Response,
    identity_provider: IdentityProvider = Depends(_get_identity_provider),
    settings: Settings = Depends(get_settings),
) -> None:
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() == "bearer" and token:
        await identity_provider.revoke(token)  # best-effort; never blocks the logout
    response.delete_cookie(_REFRESH_COOKIE_NAME, path=f"{settings.api_v1_prefix}/auth")


@router.get("/me", response_model=MeOut)
async def get_me(
    request: Request, session_factory: async_sessionmaker = Depends(_get_session_factory)
) -> MeOut:
    payload = await build_me_payload(session_factory, user_id=request.state.user_id)
    return MeOut.from_payload(payload)


@router.patch("/me", response_model=MeOut)
async def patch_me(
    body: PatchMeRequest,
    request: Request,
    session_factory: async_sessionmaker = Depends(_get_session_factory),
) -> MeOut:
    user_id = request.state.user_id
    async with UnitOfWork(session_factory, user_id=user_id) as uow:
        await uow.session.execute(
            text(
                "UPDATE users SET "
                "full_name = COALESCE(:full_name, full_name), "
                "avatar_url = COALESCE(:avatar_url, avatar_url), "
                "timezone = COALESCE(:timezone, timezone), "
                "updated_at = now() "
                "WHERE id = :user_id"
            ),
            {
                "user_id": str(user_id),
                "full_name": body.full_name,
                "avatar_url": body.avatar_url,
                "timezone": body.timezone,
            },
        )
    payload = await build_me_payload(session_factory, user_id=user_id)
    return MeOut.from_payload(payload)


@router.get("/me/permissions", response_model=PermissionsResponse)
async def get_my_permissions(
    # Permission level "membership" (backend spec §4.1) -- TenantContextMiddleware already
    # requires an active membership for every non-exempt route (this one included), so no
    # further require_permission(...) gate belongs here: this endpoint's whole job is
    # telling the caller what permissions their role has, which a specific-permission-code
    # gate would make circular for any role missing that one code.
    request: Request,
    session_factory: async_sessionmaker = Depends(_get_session_factory),
) -> PermissionsResponse:
    async with UnitOfWork(session_factory) as uow:
        result = await uow.session.execute(
            text("SELECT permission_code FROM role_permissions WHERE role_code = :role_code"),
            {"role_code": request.state.role},
        )
        codes = [row[0] for row in result.all()]
    return PermissionsResponse(permissions=codes)
