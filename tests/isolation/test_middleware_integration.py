"""End-to-end proof that the middleware chain's DB-backed gates (`AuthenticationMiddleware`'s
`fn_resolve_user_identity` call, `TenantContextMiddleware`, `ClosureMiddleware`,
`EntitlementMiddleware`, `require_permission`) work against a real, migrated PostgreSQL —
not just against the reasoning in each file's docstring. Shares this directory's DB fixtures
(`engine`, `session_factory`) with the RLS isolation suite; skips the same way if
`DATABASE_URL` is unset.

Uses a stub `IdentityProvider` instead of a live Supabase token — the JWT-verification path
is already covered end to end by `tests/unit/test_supabase_identity_provider.py`; what only a
real database can prove is everything downstream of a *successfully verified* token:
`fn_resolve_user_identity` actually resolving through RLS's chicken-and-egg gap, the
membership/closure/subscription queries, and the full chain's wiring order under FastAPI
rather than a bare Starlette app.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta

import httpx
import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from cineatelie.core.ids import uuid7
from cineatelie.platform.auth.ports import RefreshedSession, VerifiedIdentity
from cineatelie.platform.db.uow import UnitOfWork
from cineatelie.platform.middleware import register_middleware
from cineatelie.platform.middleware.permissions import require_permission
from tests.isolation.conftest import TenantFixture

pytestmark = pytest.mark.asyncio

# Must be one of user_identities' provider CHECK constraint values -- reuses "supabase"
# rather than a fake tag, since that is what the real deployment actually uses too.
_TEST_PROVIDER = "supabase"


class _StubIdentityProvider:
    """A fixed mapping of bearer token -> subject, standing in for a verified Supabase JWT."""

    def __init__(self) -> None:
        self._subjects_by_token: dict[str, str] = {}

    def register(self, token: str, subject: str) -> None:
        self._subjects_by_token[token] = subject

    async def verify(self, access_token: str) -> VerifiedIdentity:
        from cineatelie.core.errors import AppError

        subject = self._subjects_by_token.get(access_token)
        if subject is None:
            raise AppError(code="unauthenticated", message="invalid", status_code=401)
        return VerifiedIdentity(subject=subject, email="x@example.com", email_verified=True)

    async def refresh(self, refresh_token: str) -> RefreshedSession:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class UserFixture:
    id: uuid.UUID
    provider_subject: str


async def _create_user(session_factory: async_sessionmaker) -> UserFixture:
    user_id = uuid7()
    provider_subject = f"subject-{uuid.uuid4().hex[:8]}"
    async with UnitOfWork(session_factory, user_id=user_id) as uow:
        await uow.session.execute(
            text("INSERT INTO users (id, email, full_name) VALUES (:id, :email, 'Test User')"),
            {"id": str(user_id), "email": f"{provider_subject}@example.com"},
        )
        await uow.session.execute(
            text(
                "INSERT INTO user_identities (user_id, provider, provider_subject, "
                "email_at_provider) VALUES (:user_id, :provider, :subject, :email)"
            ),
            {
                "user_id": str(user_id),
                "provider": _TEST_PROVIDER,
                "subject": provider_subject,
                "email": f"{provider_subject}@example.com",
            },
        )
    return UserFixture(id=user_id, provider_subject=provider_subject)


async def _add_membership(
    session_factory: async_sessionmaker, *, tenant: TenantFixture, user: UserFixture, role: str
) -> None:
    async with UnitOfWork(session_factory, tenant_id=tenant.id, user_id=user.id) as uow:
        await uow.session.execute(
            text(
                "INSERT INTO memberships (tenant_id, user_id, role_code, status) "
                "VALUES (:tenant_id, :user_id, :role_code, 'active')"
            ),
            {"tenant_id": str(tenant.id), "user_id": str(user.id), "role_code": role},
        )


async def _add_subscription(
    session_factory: async_sessionmaker, *, tenant: TenantFixture, active: bool
) -> None:
    today = date.today()
    starts_on = today - timedelta(days=30)
    ends_on = (today + timedelta(days=30)) if active else (today - timedelta(days=1))
    async with UnitOfWork(session_factory, tenant_id=tenant.id) as uow:
        await uow.session.execute(
            text(
                "INSERT INTO subscriptions (tenant_id, plan_code, status, starts_on, ends_on) "
                "VALUES (:tenant_id, 'starter', 'active', :starts_on, :ends_on)"
            ),
            {"tenant_id": str(tenant.id), "starts_on": starts_on, "ends_on": ends_on},
        )


@pytest_asyncio.fixture
async def user_a(session_factory: async_sessionmaker) -> UserFixture:
    return await _create_user(session_factory)


def _build_app(
    session_factory: async_sessionmaker, identity_provider: _StubIdentityProvider
) -> FastAPI:
    app = FastAPI()

    @app.get("/protected", dependencies=[Depends(require_permission("settings:write"))])
    async def protected() -> dict[str, str]:
        return {"status": "ok"}

    register_middleware(
        app,
        identity_provider=identity_provider,
        session_factory=session_factory,
        provider_name=_TEST_PROVIDER,
        edge_verification_enabled=False,
        edge_shared_secret="",
    )
    app.state.session_factory = session_factory
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    """`httpx.AsyncClient` + `ASGITransport` runs the app in-process, on the *same* event
    loop as the calling test coroutine -- unlike Starlette's `TestClient`, which drives the
    app from a background thread with its own event loop. That mismatch is exactly the
    session-scoped-engine bug this suite's `conftest.py` already documents for fixtures
    (asyncpg connections are bound to the loop that created them): mixing `TestClient`'s
    loop with `session_factory`'s loop (created by the pytest-asyncio test's own loop)
    produced `RuntimeError: ... attached to a different loop`, confirmed by running it.
    """
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_full_chain_grants_access_for_a_permitted_owner(
    session_factory: async_sessionmaker, tenant_a: TenantFixture, user_a: UserFixture
) -> None:
    await _add_membership(session_factory, tenant=tenant_a, user=user_a, role="owner")
    await _add_subscription(session_factory, tenant=tenant_a, active=True)

    identity_provider = _StubIdentityProvider()
    identity_provider.register("valid-token", user_a.provider_subject)
    app = _build_app(session_factory, identity_provider)

    async with _client(app) as client:
        response = await client.get(
            "/protected",
            headers={"Authorization": "Bearer valid-token", "X-Tenant-Id": str(tenant_a.id)},
        )

    assert response.status_code == 200


async def test_missing_bearer_token_is_unauthenticated(
    session_factory: async_sessionmaker, tenant_a: TenantFixture
) -> None:
    app = _build_app(session_factory, _StubIdentityProvider())

    async with _client(app) as client:
        response = await client.get("/protected", headers={"X-Tenant-Id": str(tenant_a.id)})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


async def test_missing_tenant_header_is_a_validation_error(
    session_factory: async_sessionmaker, user_a: UserFixture
) -> None:
    identity_provider = _StubIdentityProvider()
    identity_provider.register("valid-token", user_a.provider_subject)
    app = _build_app(session_factory, identity_provider)

    async with _client(app) as client:
        response = await client.get("/protected", headers={"Authorization": "Bearer valid-token"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"


async def test_wrong_tenant_is_forbidden(
    session_factory: async_sessionmaker,
    tenant_a: TenantFixture,
    tenant_b: TenantFixture,
    user_a: UserFixture,
) -> None:
    """user_a belongs to tenant_a only; claiming tenant_b must be refused even though
    tenant_b genuinely exists (proves the RLS-scoped membership query, not just a null
    check)."""
    await _add_membership(session_factory, tenant=tenant_a, user=user_a, role="owner")

    identity_provider = _StubIdentityProvider()
    identity_provider.register("valid-token", user_a.provider_subject)
    app = _build_app(session_factory, identity_provider)

    async with _client(app) as client:
        response = await client.get(
            "/protected",
            headers={"Authorization": "Bearer valid-token", "X-Tenant-Id": str(tenant_b.id)},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "tenant_forbidden"


async def test_lapsed_subscription_blocks_with_402(
    session_factory: async_sessionmaker, tenant_a: TenantFixture, user_a: UserFixture
) -> None:
    await _add_membership(session_factory, tenant=tenant_a, user=user_a, role="owner")
    await _add_subscription(session_factory, tenant=tenant_a, active=False)

    identity_provider = _StubIdentityProvider()
    identity_provider.register("valid-token", user_a.provider_subject)
    app = _build_app(session_factory, identity_provider)

    async with _client(app) as client:
        response = await client.get(
            "/protected",
            headers={"Authorization": "Bearer valid-token", "X-Tenant-Id": str(tenant_a.id)},
        )

    assert response.status_code == 402
    assert response.json()["error"]["code"] == "subscription_inactive"


async def test_role_without_the_permission_is_denied(
    session_factory: async_sessionmaker, tenant_a: TenantFixture, user_a: UserFixture
) -> None:
    """front_desk has no settings:write (baseline seed) -- proves require_permission's
    role_permissions join, not just "some role is bound"."""
    await _add_membership(session_factory, tenant=tenant_a, user=user_a, role="front_desk")
    await _add_subscription(session_factory, tenant=tenant_a, active=True)

    identity_provider = _StubIdentityProvider()
    identity_provider.register("valid-token", user_a.provider_subject)
    app = _build_app(session_factory, identity_provider)

    async with _client(app) as client:
        response = await client.get(
            "/protected",
            headers={"Authorization": "Bearer valid-token", "X-Tenant-Id": str(tenant_a.id)},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


async def test_closed_tenant_blocks_with_tenant_closed(
    session_factory: async_sessionmaker, tenant_a: TenantFixture, user_a: UserFixture
) -> None:
    await _add_membership(session_factory, tenant=tenant_a, user=user_a, role="owner")
    await _add_subscription(session_factory, tenant=tenant_a, active=True)
    async with UnitOfWork(session_factory, tenant_id=tenant_a.id) as uow:
        await uow.session.execute(
            text("UPDATE tenants SET status = 'closed' WHERE id = :id"), {"id": str(tenant_a.id)}
        )

    identity_provider = _StubIdentityProvider()
    identity_provider.register("valid-token", user_a.provider_subject)
    app = _build_app(session_factory, identity_provider)

    async with _client(app) as client:
        response = await client.get(
            "/protected",
            headers={"Authorization": "Bearer valid-token", "X-Tenant-Id": str(tenant_a.id)},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "tenant_closed"
