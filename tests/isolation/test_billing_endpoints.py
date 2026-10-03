"""Milestone M2 proofs against a real, migrated PostgreSQL (`DATABASE_URL`, as `app_user`).

Done when (implementation plan §2, M2): editing one `subscriptions` row switches a tenant between
active and blocked, with no gateway involved, and blocked tenants can still reach `/me` and billing.
The first test proves exactly that, through the same middleware chain and role the apps use.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from cineatelie.core.config import Settings, get_settings
from cineatelie.modules.billing.adapters.router import router as billing_router
from cineatelie.platform.db.uow import UnitOfWork
from cineatelie.platform.middleware import register_middleware
from cineatelie.platform.middleware.permissions import require_permission
from tests.isolation.conftest import TenantFixture
from tests.isolation.test_middleware_integration import (
    _TEST_PROVIDER,
    UserFixture,
    _add_membership,
    _add_subscription,
    _client,
    _create_user,
    _StubIdentityProvider,
)

pytestmark = pytest.mark.asyncio

_API = "/api/v1"


@pytest_asyncio.fixture
async def owner(session_factory: async_sessionmaker) -> UserFixture:
    return await _create_user(session_factory)


def _build_app(
    session_factory: async_sessionmaker,
    identity_provider: _StubIdentityProvider,
    *,
    upgrade_url: str = "",
) -> FastAPI:
    """The billing routes plus one business route that needs a permission, so the test can show
    what a blocked tenant gets on business routes (402) compared with billing routes (200)."""
    app = FastAPI()

    @app.get(f"{_API}/probe-business", dependencies=[Depends(require_permission("settings:write"))])
    async def probe_business() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(billing_router, prefix=_API)
    register_middleware(
        app,
        identity_provider=identity_provider,
        session_factory=session_factory,
        provider_name=_TEST_PROVIDER,
        edge_verification_enabled=False,
        edge_shared_secret="",
        api_v1_prefix=_API,
    )
    app.state.session_factory = session_factory
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, BILLING_UPGRADE_URL=upgrade_url
    )
    return app


def _headers(provider: _StubIdentityProvider, user: UserFixture, tenant: TenantFixture) -> dict:
    token = f"token-{user.provider_subject}"
    provider.register(token, user.provider_subject)
    return {"Authorization": f"Bearer {token}", "X-Tenant-Id": str(tenant.id)}


async def test_editing_one_subscription_row_switches_the_tenant_on_and_off(
    session_factory: async_sessionmaker, tenant_a: TenantFixture, owner: UserFixture
) -> None:
    """The M2 done-when, exercised through the app's own role (`app_user`) with no gateway call."""
    await _add_membership(session_factory, tenant=tenant_a, user=owner, role="owner")
    await _add_subscription(session_factory, tenant=tenant_a, active=True)
    provider = _StubIdentityProvider()
    headers = _headers(provider, owner, tenant_a)
    app = _build_app(session_factory, provider)

    async with _client(app) as client:
        assert (await client.get(f"{_API}/probe-business", headers=headers)).status_code == 200

        # The "edit": expire the one subscription row for this tenant.
        async with UnitOfWork(session_factory, tenant_id=tenant_a.id) as uow:
            await uow.session.execute(
                text("UPDATE subscriptions SET ends_on = :ends_on WHERE tenant_id = :tenant_id"),
                {"ends_on": date.today() - timedelta(days=1), "tenant_id": str(tenant_a.id)},
            )

        blocked = await client.get(f"{_API}/probe-business", headers=headers)
        assert blocked.status_code == 402
        assert blocked.json()["error"]["code"] == "subscription_inactive"

        # Blocked tenants still reach /billing (to renew) and /me.
        assert (await client.get(f"{_API}/billing/my-plan", headers=headers)).status_code == 200
        assert (
            await client.get(f"{_API}/billing/subscription", headers=headers)
        ).status_code == 200

        # Renew by editing the same row back into the window.
        async with UnitOfWork(session_factory, tenant_id=tenant_a.id) as uow:
            await uow.session.execute(
                text("UPDATE subscriptions SET ends_on = :ends_on WHERE tenant_id = :tenant_id"),
                {"ends_on": date.today() + timedelta(days=30), "tenant_id": str(tenant_a.id)},
            )

        assert (await client.get(f"{_API}/probe-business", headers=headers)).status_code == 200


async def test_my_plan_describes_the_current_window_and_its_limits(
    session_factory: async_sessionmaker, tenant_a: TenantFixture, owner: UserFixture
) -> None:
    await _add_membership(session_factory, tenant=tenant_a, user=owner, role="owner")
    await _add_subscription(session_factory, tenant=tenant_a, active=True)
    provider = _StubIdentityProvider()
    app = _build_app(session_factory, provider)

    async with _client(app) as client:
        response = await client.get(
            f"{_API}/billing/my-plan", headers=_headers(provider, owner, tenant_a)
        )

    body = response.json()
    assert response.status_code == 200
    assert body["plan"]["code"] == "starter"
    assert body["plan"]["is_within_window"] is True
    assert body["plan"]["days_remaining"] == 30
    assert body["upgrade_url"] is None  # BILLING_UPGRADE_URL is empty by default (OI-15)
    assert len(body["included"]) > 0  # every shipped feature is on for starter (seed)
    assert all(f["enabled"] for f in body["included"])
    assert {q["key"] for q in body["quotas"]} >= {"attachments_total"}
    assert all(q["used"] is None for q in body["quotas"])  # not measured until M6


async def test_my_plan_shows_the_upgrade_link_only_when_configured(
    session_factory: async_sessionmaker, tenant_a: TenantFixture, owner: UserFixture
) -> None:
    await _add_membership(session_factory, tenant=tenant_a, user=owner, role="owner")
    await _add_subscription(session_factory, tenant=tenant_a, active=True)
    provider = _StubIdentityProvider()
    app = _build_app(session_factory, provider, upgrade_url="https://example.test/upgrade")

    async with _client(app) as client:
        response = await client.get(
            f"{_API}/billing/my-plan", headers=_headers(provider, owner, tenant_a)
        )

    assert response.json()["upgrade_url"] == "https://example.test/upgrade"


async def test_a_tenant_without_a_subscription_has_no_plan_block(
    session_factory: async_sessionmaker, tenant_a: TenantFixture, owner: UserFixture
) -> None:
    await _add_membership(session_factory, tenant=tenant_a, user=owner, role="owner")
    provider = _StubIdentityProvider()
    app = _build_app(session_factory, provider)

    async with _client(app) as client:
        response = await client.get(
            f"{_API}/billing/my-plan", headers=_headers(provider, owner, tenant_a)
        )

    assert response.status_code == 200
    assert response.json() == {
        "plan": None,
        "included": [],
        "quotas": [],
        "upgrade_url": None,
    }


async def test_plan_catalogue_lists_the_public_plans(
    session_factory: async_sessionmaker, tenant_a: TenantFixture, owner: UserFixture
) -> None:
    await _add_membership(session_factory, tenant=tenant_a, user=owner, role="owner")
    provider = _StubIdentityProvider()
    app = _build_app(session_factory, provider)

    async with _client(app) as client:
        response = await client.get(
            f"{_API}/billing/plans", headers=_headers(provider, owner, tenant_a)
        )

    assert response.status_code == 200
    codes = [p["code"] for p in response.json()]
    assert "starter" in codes
    assert "trial" in codes


@pytest.mark.parametrize("path", ["/billing/subscription", "/billing/invoices"])
async def test_billing_manage_routes_refuse_a_role_without_the_permission(
    session_factory: async_sessionmaker, tenant_a: TenantFixture, owner: UserFixture, path: str
) -> None:
    """front_desk has no billing:manage (baseline seed): proves the permission join."""
    await _add_membership(session_factory, tenant=tenant_a, user=owner, role="front_desk")
    await _add_subscription(session_factory, tenant=tenant_a, active=True)
    provider = _StubIdentityProvider()
    app = _build_app(session_factory, provider)

    async with _client(app) as client:
        response = await client.get(f"{_API}{path}", headers=_headers(provider, owner, tenant_a))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


async def test_owner_reads_the_subscription_and_invoices(
    session_factory: async_sessionmaker, tenant_a: TenantFixture, owner: UserFixture
) -> None:
    await _add_membership(session_factory, tenant=tenant_a, user=owner, role="owner")
    await _add_subscription(session_factory, tenant=tenant_a, active=True)
    provider = _StubIdentityProvider()
    app = _build_app(session_factory, provider)

    async with _client(app) as client:
        subscription = await client.get(
            f"{_API}/billing/subscription", headers=_headers(provider, owner, tenant_a)
        )
        invoices = await client.get(
            f"{_API}/billing/invoices", headers=_headers(provider, owner, tenant_a)
        )

    assert subscription.status_code == 200
    assert subscription.json()["plan_code"] == "starter"
    assert invoices.status_code == 200
    assert invoices.json() == []  # no gateway yet, so no invoices
