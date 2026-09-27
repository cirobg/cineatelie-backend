"""Builds the `/me` payload (backend spec §4.1: "User, memberships, roles, plans,
subscription status") — shared by `GET /me` and `/auth/exchange`'s response (WF-01: "session +
/me payload").

`fn_my_tenants()` (ADR-001) is exactly this shape already: `SECURITY DEFINER`, keyed on
`current_user_id()`, returns `tenant_id`/`slug`/`trade_name`/role/plan/subscription per
membership — built for this endpoint even before the endpoint existed.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from cineatelie.modules.identity.application.dto import MePayload, TenantMembership
from cineatelie.platform.db.uow import UnitOfWork

_USER_QUERY = text(
    "SELECT email, full_name, avatar_url, locale, timezone FROM users WHERE id = :user_id"
)
_TENANTS_QUERY = text(
    "SELECT tenant_id, slug, trade_name, logo_url, role_code, is_default, plan_code, "
    "subscription_active FROM fn_my_tenants()"
)


async def build_me_payload(session_factory: async_sessionmaker, *, user_id: uuid.UUID) -> MePayload:
    async with UnitOfWork(session_factory, user_id=user_id) as uow:
        user_row = (await uow.session.execute(_USER_QUERY, {"user_id": str(user_id)})).one()
        tenant_rows = (await uow.session.execute(_TENANTS_QUERY)).all()

    return MePayload(
        user_id=user_id,
        email=user_row.email,
        full_name=user_row.full_name,
        avatar_url=user_row.avatar_url,
        locale=user_row.locale,
        timezone=user_row.timezone,
        tenants=[
            TenantMembership(
                tenant_id=row.tenant_id,
                slug=row.slug,
                trade_name=row.trade_name,
                logo_url=row.logo_url,
                role_code=row.role_code,
                is_default=row.is_default,
                plan_code=row.plan_code,
                subscription_active=row.subscription_active,
            )
            for row in tenant_rows
        ],
    )
