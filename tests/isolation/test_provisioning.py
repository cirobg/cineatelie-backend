"""WF-01 (spec-20260920-backend.md) end to end against a real, migrated PostgreSQL — every
default row the provisioning transaction is supposed to create, not just that it returns
without raising. Shares this directory's `engine`/`session_factory` fixtures; skips the same
way if `DATABASE_URL` is unset.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from cineatelie.modules.identity.application.provisioning import (
    ProvisioningConflictError,
    provision_or_resolve_user,
)
from cineatelie.modules.identity.domain.slug import slugify
from cineatelie.platform.auth.identity_resolution import resolve_user_id
from cineatelie.platform.auth.ports import VerifiedIdentity
from cineatelie.platform.db.uow import UnitOfWork

pytestmark = pytest.mark.asyncio

_PROVIDER = "supabase"


def _identity(*, subject: str | None = None, email: str | None = None) -> VerifiedIdentity:
    unique = uuid.uuid4().hex[:8]
    # The unique token leads the full name: _derive_trade_name takes only the FIRST word, so
    # this is what actually keeps each test's tenant slug unique across repeated runs against
    # the same long-lived local database (unlike tenant_a/tenant_b's slug, which bakes a uuid
    # suffix in directly). The slug-collision retry itself is exercised deliberately, once, by
    # test_two_signups_with_the_same_trade_name_get_distinct_slugs.
    return VerifiedIdentity(
        subject=subject or f"subject-{unique}",
        email=email or f"costureira-{unique}@example.com",
        email_verified=True,
        raw_claims={"user_metadata": {"full_name": f"Costureira{unique} Teste"}},
    )


async def _count(session_factory: async_sessionmaker, table: str, tenant_id: uuid.UUID) -> int:
    async with UnitOfWork(session_factory, tenant_id=tenant_id) as uow:
        result = await uow.session.execute(
            text(f"SELECT count(*) FROM {table} WHERE tenant_id = :tenant_id"),  # noqa: S608
            {"tenant_id": str(tenant_id)},
        )
        return result.scalar_one()


async def test_new_identity_creates_a_full_default_workspace(
    session_factory: async_sessionmaker,
) -> None:
    identity = _identity()

    outcome = await provision_or_resolve_user(session_factory, identity, provider_name=_PROVIDER)
    assert outcome.is_new_human is True

    async with UnitOfWork(session_factory, user_id=outcome.user_id) as uow:
        user_row = (
            await uow.session.execute(
                text("SELECT email, full_name FROM users WHERE id = :id"),
                {"id": str(outcome.user_id)},
            )
        ).one()
    expected_full_name = identity.raw_claims["user_metadata"]["full_name"]
    expected_trade_name = f"Ateliê {expected_full_name.split(' ')[0]}"
    assert user_row.email == identity.email
    assert user_row.full_name == expected_full_name

    async with UnitOfWork(session_factory) as uow:
        resolved_user_id = await resolve_user_id(
            uow.session, provider=_PROVIDER, provider_subject=identity.subject
        )
    assert resolved_user_id == outcome.user_id

    async with UnitOfWork(session_factory, user_id=outcome.user_id) as uow:
        tenant_row = (
            await uow.session.execute(
                text(
                    "SELECT tenant_id AS id, slug, trade_name, role_code, is_default "
                    "FROM fn_my_tenants()"
                )
            )
        ).one()
    assert tenant_row.slug == slugify(expected_trade_name)
    assert tenant_row.trade_name == expected_trade_name
    assert tenant_row.role_code == "owner"
    assert tenant_row.is_default is True

    tenant_id = tenant_row.id
    assert await _count(session_factory, "document_counters", tenant_id) == 4
    assert await _count(session_factory, "card_fees", tenant_id) == 7
    assert await _count(session_factory, "finance_categories", tenant_id) == 10
    assert await _count(session_factory, "contract_templates", tenant_id) == 4
    assert await _count(session_factory, "subscriptions", tenant_id) == 1
    assert await _count(session_factory, "tenant_settings", tenant_id) == 1

    async with UnitOfWork(session_factory, tenant_id=tenant_id) as uow:
        ready_flags = (
            await uow.session.execute(
                text(
                    "SELECT document_type, is_ready FROM contract_templates "
                    "WHERE tenant_id = :tenant_id ORDER BY document_type"
                ),
                {"tenant_id": str(tenant_id)},
            )
        ).all()
    ready_by_type = {row.document_type: row.is_ready for row in ready_flags}
    assert ready_by_type == {
        "ajuste_conserto": True,
        "aluguel": False,
        "sob_medida": True,
        "venda": False,
    }

    async with UnitOfWork(session_factory, tenant_id=tenant_id) as uow:
        subscription = (
            await uow.session.execute(
                text(
                    "SELECT plan_code, status, price_amount, currency_code FROM subscriptions "
                    "WHERE tenant_id = :tenant_id"
                ),
                {"tenant_id": str(tenant_id)},
            )
        ).one()
    assert subscription.plan_code == "trial"
    assert subscription.status == "trialing"
    # The price snapshot is copied from the plan (BR-SUB-01): a trial snapshots 0 BRL, not null.
    assert subscription.price_amount == 0
    assert subscription.currency_code == "BRL"


async def test_known_identity_resolves_without_creating_a_second_tenant(
    session_factory: async_sessionmaker,
) -> None:
    identity = _identity()
    first = await provision_or_resolve_user(session_factory, identity, provider_name=_PROVIDER)
    assert first.is_new_human is True

    second = await provision_or_resolve_user(session_factory, identity, provider_name=_PROVIDER)
    assert second.is_new_human is False
    assert second.user_id == first.user_id


async def test_two_signups_with_the_same_trade_name_get_distinct_slugs(
    session_factory: async_sessionmaker,
) -> None:
    # Both identities deliberately share one name, to force exactly one slug collision; the
    # run-unique token keeps that shared base from colliding with a *previous run's* leftover
    # row in this long-lived local database (real CI gets a fresh database every run).
    shared_name = f"Joana{uuid.uuid4().hex[:8]} Prado"
    claims = {"user_metadata": {"full_name": shared_name}}
    identity_a = VerifiedIdentity(
        subject=f"subject-{uuid.uuid4().hex[:8]}",
        email=f"a-{uuid.uuid4().hex[:8]}@example.com",
        email_verified=True,
        raw_claims=claims,
    )
    identity_b = VerifiedIdentity(
        subject=f"subject-{uuid.uuid4().hex[:8]}",
        email=f"b-{uuid.uuid4().hex[:8]}@example.com",
        email_verified=True,
        raw_claims=claims,
    )

    outcome_a = await provision_or_resolve_user(
        session_factory, identity_a, provider_name=_PROVIDER
    )
    outcome_b = await provision_or_resolve_user(
        session_factory, identity_b, provider_name=_PROVIDER
    )

    async def _slug(user_id: uuid.UUID) -> str:
        async with UnitOfWork(session_factory, user_id=user_id) as uow:
            row = (await uow.session.execute(text("SELECT slug FROM fn_my_tenants()"))).one()
            return row.slug

    slug_a = await _slug(outcome_a.user_id)
    slug_b = await _slug(outcome_b.user_id)
    expected_base = slugify(f"Ateliê {shared_name.split(' ')[0]}")
    assert slug_a != slug_b
    assert {slug_a, slug_b} == {expected_base, f"{expected_base}-2"}


async def test_duplicate_email_under_a_different_identity_raises_a_clear_conflict(
    session_factory: async_sessionmaker,
) -> None:
    shared_email = f"duplicate-{uuid.uuid4().hex[:8]}@example.com"
    await provision_or_resolve_user(
        session_factory, _identity(email=shared_email), provider_name=_PROVIDER
    )

    with pytest.raises(ProvisioningConflictError):
        await provision_or_resolve_user(
            session_factory, _identity(email=shared_email), provider_name=_PROVIDER
        )
