"""Fixtures for the tenant-isolation suite (database spec §OAC-DB-05, US-DB-01).

Requires a real, migrated PostgreSQL — `DATABASE_URL` pointing at it, connected as
`app_user`. Not run as part of the plain `pytest` unit-test invocation; CI runs these
separately, against a real `postgres:16` service container, after `alembic upgrade head`
(backend spec §3, "Module structure"). Locally: `docker compose up`, then point
`DATABASE_URL` at the running instance and run `pytest tests/isolation`.

Fixture tenants are created by `app_user` itself, the same way a real signup would: a
UUIDv7 is generated application-side first (ADR-002), the tenant GUC is set to that exact
id, and *then* the row is inserted — satisfying `tenants_self_isolation`'s `WITH CHECK
(id = cineatelie.current_tenant_id())` the same way real provisioning (WF-01) will,
without a privileged backdoor connection.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from cineatelie.core.ids import uuid7
from cineatelie.platform.db.uow import UnitOfWork


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set — the isolation suite needs a real, migrated database")
    return url


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """Function-scoped, deliberately not session-scoped: pytest-asyncio gives each test
    function its own event loop by default, and an `asyncpg`-backed engine's connections
    are bound to the loop they were created in. A session-scoped engine here produced
    exactly that mismatch the first time this suite actually ran — `RuntimeError: ...
    attached to a different loop` — confirmed by running it, not by reading the fixture
    code. A fresh engine per test costs a handful of extra connections across ~15 tests;
    trivial against the alternative of debugging loop-affinity bugs in a test suite whose
    entire job is proving correctness."""
    eng = create_async_engine(_database_url())
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(bind=engine, expire_on_commit=False)


@dataclass(frozen=True, slots=True)
class TenantFixture:
    id: uuid.UUID
    slug: str
    trade_name: str


async def _create_tenant(session_factory: async_sessionmaker, *, slug: str) -> TenantFixture:
    tenant_id = uuid7()
    async with UnitOfWork(session_factory, tenant_id=tenant_id) as uow:
        await uow.session.execute(
            text("INSERT INTO tenants (id, slug, trade_name) VALUES (:id, :slug, :trade_name)"),
            {"id": str(tenant_id), "slug": slug, "trade_name": f"Ateliê {slug}"},
        )
    return TenantFixture(id=tenant_id, slug=slug, trade_name=f"Ateliê {slug}")


@pytest_asyncio.fixture
async def tenant_a(session_factory: async_sessionmaker) -> TenantFixture:
    return await _create_tenant(session_factory, slug=f"isolation-a-{uuid.uuid4().hex[:8]}")


@pytest_asyncio.fixture
async def tenant_b(session_factory: async_sessionmaker) -> TenantFixture:
    return await _create_tenant(session_factory, slug=f"isolation-b-{uuid.uuid4().hex[:8]}")
