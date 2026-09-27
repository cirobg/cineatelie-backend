"""The tenant-isolation suite (database spec US-DB-01, OAC-DB-05; ADR-001 "Verification":
"An automated suite seeds two tenants and, for every endpoint, issues tenant A's token
while asserting that none of tenant B's fixture ids is reachable — by list, by direct id,
by filter, or by foreign-key traversal. This suite gates every release."

This is the database/`platform.db` layer of that suite — it proves the mechanism (RLS +
`UnitOfWork`'s `SET LOCAL`) is sound before any HTTP endpoint exists to test through
(implementation plan: "written here, before any business module... every later change
depends on it"). The equivalent test *through* the API arrives with `modules/identity`
and grows with every module after it — this file does not shrink as that happens; a
schema-level guarantee and an endpoint-level guarantee are different claims.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from cineatelie.platform.db.uow import UnitOfWork
from tests.isolation.conftest import TenantFixture

pytestmark = pytest.mark.asyncio


# --- BR-DB-01 / US-DB-01: no context, no rows, never an error ----------------------------


async def test_no_tenant_context_returns_zero_rows_never_an_error(
    session_factory: async_sessionmaker, tenant_a: TenantFixture
) -> None:
    async with UnitOfWork(session_factory) as uow:  # tenant_id=None: no context set
        result = await uow.session.execute(text("SELECT count(*) FROM tenants"))
        assert result.scalar_one() == 0


# --- The core round trip: correct tenant sees its own row, nothing else ------------------


async def test_tenant_sees_only_its_own_row(
    session_factory: async_sessionmaker, tenant_a: TenantFixture, tenant_b: TenantFixture
) -> None:
    async with UnitOfWork(session_factory, tenant_id=tenant_a.id) as uow:
        result = await uow.session.execute(text("SELECT id, slug FROM tenants"))
        rows = result.all()
        assert [r.id for r in rows] == [tenant_a.id]
        assert tenant_b.id not in [r.id for r in rows]


async def test_wrong_tenant_context_sees_nothing_even_by_direct_id(
    session_factory: async_sessionmaker, tenant_a: TenantFixture, tenant_b: TenantFixture
) -> None:
    """ "By direct id" (OAC-DB-05): querying tenant B's own id, under tenant A's context."""
    async with UnitOfWork(session_factory, tenant_id=tenant_a.id) as uow:
        result = await uow.session.execute(
            text("SELECT id FROM tenants WHERE id = :id"), {"id": str(tenant_b.id)}
        )
        assert result.first() is None


async def test_wrong_tenant_context_sees_nothing_by_filter(
    session_factory: async_sessionmaker, tenant_a: TenantFixture, tenant_b: TenantFixture
) -> None:
    """ "By filter" (OAC-DB-05): a WHERE clause naming tenant B cannot override RLS — the
    policy's own predicate is ANDed in regardless of what the query itself asks for."""
    async with UnitOfWork(session_factory, tenant_id=tenant_a.id) as uow:
        result = await uow.session.execute(
            text("SELECT id FROM tenants WHERE slug = :slug"), {"slug": tenant_b.slug}
        )
        assert result.first() is None


# --- Writes: WITH CHECK, not just USING --------------------------------------------------


async def test_cannot_insert_a_row_claiming_another_tenant(
    session_factory: async_sessionmaker, tenant_a: TenantFixture, tenant_b: TenantFixture
) -> None:
    """BR-DB-01: a write carrying a `tenant_id` other than the session's is rejected —
    `assert_tenant_matches` raises 42501 before RLS's own `WITH CHECK` would even apply."""
    from sqlalchemy.exc import DBAPIError

    async with UnitOfWork(session_factory, tenant_id=tenant_a.id) as uow:
        with pytest.raises(DBAPIError) as exc_info:
            await uow.session.execute(
                text("INSERT INTO clients (tenant_id, full_name) VALUES (:tenant_id, 'Intruder')"),
                {"tenant_id": str(tenant_b.id)},
            )
        # asyncpg exposes the SQLSTATE as .sqlstate, not embedded in str() — the trigger's
        # own RAISE EXCEPTION message ("tenant_id mismatch: ...") is what str() shows.
        assert exc_info.value.orig.sqlstate == "42501"


async def test_cannot_update_a_row_into_another_tenant(
    session_factory: async_sessionmaker, tenant_a: TenantFixture, tenant_b: TenantFixture
) -> None:
    from sqlalchemy.exc import DBAPIError

    async with UnitOfWork(session_factory, tenant_id=tenant_a.id) as uow:
        await uow.session.execute(
            text("INSERT INTO clients (tenant_id, full_name) VALUES (:tenant_id, 'Real client')"),
            {"tenant_id": str(tenant_a.id)},
        )
        with pytest.raises(DBAPIError) as exc_info:
            await uow.session.execute(
                text("UPDATE clients SET tenant_id = :other WHERE tenant_id = :mine"),
                {"other": str(tenant_b.id), "mine": str(tenant_a.id)},
            )
        assert exc_info.value.orig.sqlstate == "42501"


# --- By foreign-key traversal (OAC-DB-05) -------------------------------------------------


async def test_join_across_tables_does_not_leak_another_tenants_row(
    session_factory: async_sessionmaker, tenant_a: TenantFixture, tenant_b: TenantFixture
) -> None:
    """A join naming no tenant_id anywhere still isolates correctly, because RLS applies
    independently to *each* table in the join — there is no join shape that adds up to
    visibility neither table grants on its own."""
    async with UnitOfWork(session_factory, tenant_id=tenant_b.id) as uow:
        result = await uow.session.execute(
            text(
                "INSERT INTO clients (tenant_id, full_name) VALUES (:tenant_id, 'Cliente B') "
                "RETURNING id"
            ),
            {"tenant_id": str(tenant_b.id)},
        )
        client_b_id = result.scalar_one()

    async with UnitOfWork(session_factory, tenant_id=tenant_a.id) as uow:
        # Tenant A, querying by tenant B's own client id, through a join that never
        # mentions tenant_id explicitly.
        result = await uow.session.execute(
            text(
                "SELECT cl.full_name FROM clients cl "
                "JOIN tenants t ON t.id = cl.tenant_id "
                "WHERE cl.id = :client_id"
            ),
            {"client_id": str(client_b_id)},
        )
        assert result.first() is None


# --- Nullable tenant_id: audit_log / job_queue (database spec §6, "Two tables take a ----
# --- different policy") -------------------------------------------------------------------


async def test_platform_scoped_rows_are_visible_to_every_tenant_context(
    session_factory: async_sessionmaker, tenant_a: TenantFixture
) -> None:
    """`tenant_id IS NULL OR tenant_id = current_tenant_id()` — a platform-level job_queue
    row (the keep-alive ping) must remain visible regardless of which tenant is active,
    or the worker could never see its own platform-scoped work."""
    async with UnitOfWork(session_factory) as uow:  # no tenant — a platform job
        result = await uow.session.execute(
            text(
                "INSERT INTO job_queue (tenant_id, job_type, run_after) "
                "VALUES (NULL, 'isolation_test_probe', now()) RETURNING id"
            )
        )
        job_id = result.scalar_one()

    async with UnitOfWork(session_factory, tenant_id=tenant_a.id) as uow:
        result = await uow.session.execute(
            text("SELECT id FROM job_queue WHERE id = :id"), {"id": str(job_id)}
        )
        assert result.scalar_one() == job_id


async def test_tenant_scoped_job_is_invisible_to_a_different_tenant(
    session_factory: async_sessionmaker, tenant_a: TenantFixture, tenant_b: TenantFixture
) -> None:
    async with UnitOfWork(session_factory, tenant_id=tenant_a.id) as uow:
        result = await uow.session.execute(
            text(
                "INSERT INTO job_queue (tenant_id, job_type, run_after) "
                "VALUES (:tenant_id, 'isolation_test_probe', now()) RETURNING id"
            ),
            {"tenant_id": str(tenant_a.id)},
        )
        job_id = result.scalar_one()

    async with UnitOfWork(session_factory, tenant_id=tenant_b.id) as uow:
        result = await uow.session.execute(
            text("SELECT id FROM job_queue WHERE id = :id"), {"id": str(job_id)}
        )
        assert result.first() is None


# --- Generic, schema-wide guard (OAC-DB-11): every tenant-scoped table must have RLS -----
# --- ENABLEd and FORCEd — catches "a new table was added and RLS was forgotten" ----------


async def test_every_tenant_scoped_table_has_rls_enabled_and_forced(
    session_factory: async_sessionmaker,
) -> None:
    async with UnitOfWork(session_factory) as uow:
        result = await uow.session.execute(
            text(
                "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity "
                "FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'tenant_id' "
                "WHERE n.nspname = 'cineatelie' AND c.relkind = 'r' AND NOT a.attisdropped "
                "ORDER BY c.relname"
            )
        )
        rows = result.all()

    assert rows, "expected at least one tenant-scoped table — the query itself may be wrong"
    not_protected = [r.relname for r in rows if not (r.relrowsecurity and r.relforcerowsecurity)]
    assert not_protected == [], (
        f"table(s) with a tenant_id column but RLS not both ENABLEd and FORCEd: {not_protected}"
    )


async def test_app_user_owns_no_table(session_factory: async_sessionmaker) -> None:
    """ADR-001: "the application role must not own tables" — ownership bypasses RLS
    entirely if `FORCE` is ever accidentally dropped, so this is checked directly too."""
    async with UnitOfWork(session_factory) as uow:
        result = await uow.session.execute(
            text(
                "SELECT count(*) FROM pg_tables "
                "WHERE schemaname = 'cineatelie' AND tableowner = 'app_user'"
            )
        )
        assert result.scalar_one() == 0
