"""Resolves a verified provider identity to the platform's own `user_id` (ADR-006's identity
decoupling: domain tables never reference a provider's user id directly; the lookup key is
`(provider, provider_subject)` in `user_identities`).

Goes through `cineatelie.fn_resolve_user_identity`, a `SECURITY DEFINER` function
(ADR-001 addendum, 2026-09-27) — `user_identities`'s own RLS policy is keyed on
`current_user_id()`, which is exactly what this call exists to discover, so an ordinary
`app_user` query run with no context set can never see the row it needs.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def resolve_user_id(
    session: AsyncSession, *, provider: str, provider_subject: str
) -> uuid.UUID | None:
    result = await session.execute(
        text("SELECT cineatelie.fn_resolve_user_identity(:provider, :provider_subject)"),
        {"provider": provider, "provider_subject": provider_subject},
    )
    return result.scalar_one()
