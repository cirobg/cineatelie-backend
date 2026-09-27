"""End-to-end HTTP proof of the identity endpoints (backend spec §4.1) — the real router,
the real middleware chain, a real migrated database, a stub `IdentityProvider` standing in
for a verified Supabase token (JWT verification itself is already covered by
`tests/unit/test_supabase_identity_provider.py`).

Uses `httpx.AsyncClient` + `ASGITransport`, not Starlette's `TestClient`, for the same
loop-affinity reason `test_middleware_integration.py` does — see that module's `_client()`
docstring.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker

from cineatelie.modules.identity.adapters.router import router as identity_router
from cineatelie.platform.auth.ports import RefreshedSession, VerifiedIdentity
from cineatelie.platform.middleware import register_middleware

pytestmark = pytest.mark.asyncio

_API_PREFIX = "/api/v1"


class _StubIdentityProvider:
    def __init__(self) -> None:
        self.revoked_tokens: list[str] = []
        self._subjects_by_token: dict[str, str] = {}

    def register(self, token: str, subject: str) -> None:
        self._subjects_by_token[token] = subject

    async def verify(self, access_token: str) -> VerifiedIdentity:
        from cineatelie.core.errors import AppError

        subject = self._subjects_by_token.get(access_token)
        if subject is None:
            raise AppError(code="unauthenticated", message="invalid", status_code=401)
        return VerifiedIdentity(
            subject=subject,
            email=f"{subject}@example.com",
            email_verified=True,
            # The unique part leads (no space before it): _derive_trade_name only takes the
            # first word, and that is what keeps each test's tenant slug from colliding with
            # another run's leftover row in this long-lived local database.
            raw_claims={"user_metadata": {"full_name": f"Costureira{subject} Teste"}},
        )

    async def refresh(self, refresh_token: str) -> RefreshedSession:
        return RefreshedSession(
            access_token=f"new-access-for-{refresh_token}",
            refresh_token=f"new-refresh-for-{refresh_token}",
            expires_in=3600,
        )

    async def revoke(self, access_token: str) -> None:
        self.revoked_tokens.append(access_token)


def _build_app(
    session_factory: async_sessionmaker, identity_provider: _StubIdentityProvider
) -> FastAPI:
    app = FastAPI()
    app.state.session_factory = session_factory
    app.state.identity_provider = identity_provider
    register_middleware(
        app,
        identity_provider=identity_provider,
        session_factory=session_factory,
        provider_name="supabase",
        edge_verification_enabled=False,
        edge_shared_secret="",
        api_v1_prefix=_API_PREFIX,
    )
    app.include_router(identity_router, prefix=_API_PREFIX)
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_exchange_provisions_a_new_user_and_sets_the_refresh_cookie(
    session_factory: async_sessionmaker,
) -> None:
    identity_provider = _StubIdentityProvider()
    subject = f"subject-{uuid.uuid4().hex[:8]}"
    identity_provider.register("provider-access-token", subject)
    app = _build_app(session_factory, identity_provider)

    async with _client(app) as client:
        response = await client.post(
            f"{_API_PREFIX}/auth/exchange",
            json={"access_token": "provider-access-token", "refresh_token": "provider-refresh"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["access_token"] == "provider-access-token"
    assert body["email"] == f"{subject}@example.com"
    assert len(body["tenants"]) == 1
    assert body["tenants"][0]["role_code"] == "owner"
    assert body["tenants"][0]["is_default"] is True

    set_cookie = response.headers.get("set-cookie", "")
    assert "refresh_token=provider-refresh" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "samesite=lax" in set_cookie.lower()
    assert f"Path={_API_PREFIX}/auth" in set_cookie


async def test_exchange_twice_resolves_the_same_user_without_duplicating(
    session_factory: async_sessionmaker,
) -> None:
    identity_provider = _StubIdentityProvider()
    subject = f"subject-{uuid.uuid4().hex[:8]}"
    identity_provider.register("provider-access-token", subject)
    app = _build_app(session_factory, identity_provider)

    async with _client(app) as client:
        first = await client.post(
            f"{_API_PREFIX}/auth/exchange",
            json={"access_token": "provider-access-token", "refresh_token": "r1"},
        )
        second = await client.post(
            f"{_API_PREFIX}/auth/exchange",
            json={"access_token": "provider-access-token", "refresh_token": "r2"},
        )

    assert first.json()["user_id"] == second.json()["user_id"]
    assert len(second.json()["tenants"]) == 1


async def test_me_returns_the_same_shape_as_exchange(session_factory: async_sessionmaker) -> None:
    identity_provider = _StubIdentityProvider()
    subject = f"subject-{uuid.uuid4().hex[:8]}"
    identity_provider.register("provider-access-token", subject)
    app = _build_app(session_factory, identity_provider)

    async with _client(app) as client:
        exchange_response = await client.post(
            f"{_API_PREFIX}/auth/exchange",
            json={"access_token": "provider-access-token", "refresh_token": "r1"},
        )
        me_response = await client.get(
            f"{_API_PREFIX}/me", headers={"Authorization": "Bearer provider-access-token"}
        )

    assert me_response.status_code == 200
    assert me_response.json()["user_id"] == exchange_response.json()["user_id"]
    assert me_response.json()["tenants"] == exchange_response.json()["tenants"]


async def test_patch_me_updates_full_name(session_factory: async_sessionmaker) -> None:
    identity_provider = _StubIdentityProvider()
    subject = f"subject-{uuid.uuid4().hex[:8]}"
    identity_provider.register("provider-access-token", subject)
    app = _build_app(session_factory, identity_provider)

    async with _client(app) as client:
        await client.post(
            f"{_API_PREFIX}/auth/exchange",
            json={"access_token": "provider-access-token", "refresh_token": "r1"},
        )
        patch_response = await client.patch(
            f"{_API_PREFIX}/me",
            headers={"Authorization": "Bearer provider-access-token"},
            json={"full_name": "Novo Nome"},
        )

    assert patch_response.status_code == 200
    assert patch_response.json()["full_name"] == "Novo Nome"


async def test_my_permissions_lists_every_permission_for_the_owner_role(
    session_factory: async_sessionmaker,
) -> None:
    identity_provider = _StubIdentityProvider()
    subject = f"subject-{uuid.uuid4().hex[:8]}"
    identity_provider.register("provider-access-token", subject)
    app = _build_app(session_factory, identity_provider)

    async with _client(app) as client:
        exchange_response = await client.post(
            f"{_API_PREFIX}/auth/exchange",
            json={"access_token": "provider-access-token", "refresh_token": "r1"},
        )
        tenant_id = exchange_response.json()["tenants"][0]["tenant_id"]
        permissions_response = await client.get(
            f"{_API_PREFIX}/me/permissions",
            headers={
                "Authorization": "Bearer provider-access-token",
                "X-Tenant-Id": tenant_id,
            },
        )

    assert permissions_response.status_code == 200
    assert "settings:write" in permissions_response.json()["permissions"]


async def test_logout_revokes_at_the_provider_and_clears_the_cookie(
    session_factory: async_sessionmaker,
) -> None:
    identity_provider = _StubIdentityProvider()
    subject = f"subject-{uuid.uuid4().hex[:8]}"
    identity_provider.register("provider-access-token", subject)
    app = _build_app(session_factory, identity_provider)

    async with _client(app) as client:
        await client.post(
            f"{_API_PREFIX}/auth/exchange",
            json={"access_token": "provider-access-token", "refresh_token": "r1"},
        )
        logout_response = await client.post(
            f"{_API_PREFIX}/auth/logout",
            headers={"Authorization": "Bearer provider-access-token"},
        )

    assert logout_response.status_code == 204
    assert identity_provider.revoked_tokens == ["provider-access-token"]


async def test_refresh_without_a_cookie_is_unauthenticated(
    session_factory: async_sessionmaker,
) -> None:
    app = _build_app(session_factory, _StubIdentityProvider())

    async with _client(app) as client:
        response = await client.post(f"{_API_PREFIX}/auth/refresh")

    assert response.status_code == 401


async def test_refresh_with_a_cookie_rotates_it_and_returns_a_new_access_token(
    session_factory: async_sessionmaker,
) -> None:
    app = _build_app(session_factory, _StubIdentityProvider())

    async with _client(app) as client:
        client.cookies.set("refresh_token", "old-refresh")
        response = await client.post(f"{_API_PREFIX}/auth/refresh")

    assert response.status_code == 200
    assert response.json()["access_token"] == "new-access-for-old-refresh"
    assert "refresh_token=new-refresh-for-old-refresh" in response.headers.get("set-cookie", "")
