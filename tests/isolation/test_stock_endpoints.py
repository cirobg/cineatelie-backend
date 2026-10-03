"""Milestone M3 proofs for stock, against a real, migrated PostgreSQL (as `app_user`).

Done when (implementation plan §2, M3): a stock entry updates the balance through the ledger, the
cached balance always equals the ledger sum, and the three quantities (on hand, reserved, available)
are distinct in the API. The UI part is covered in the frontend status doc.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from cineatelie.modules.catalog.adapters.router import router as catalog_router
from cineatelie.platform.db.uow import UnitOfWork
from cineatelie.platform.middleware import register_middleware
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


def _build_app(session_factory: async_sessionmaker, provider: _StubIdentityProvider) -> FastAPI:
    app = FastAPI()
    app.include_router(catalog_router, prefix=_API)
    register_middleware(
        app,
        identity_provider=provider,
        session_factory=session_factory,
        provider_name=_TEST_PROVIDER,
        edge_verification_enabled=False,
        edge_shared_secret="",
        api_v1_prefix=_API,
    )
    app.state.session_factory = session_factory
    return app


def _headers(provider: _StubIdentityProvider, user: UserFixture, tenant: TenantFixture) -> dict:
    token = f"token-{user.provider_subject}"
    provider.register(token, user.provider_subject)
    return {"Authorization": f"Bearer {token}", "X-Tenant-Id": str(tenant.id)}


async def _ledger_sum_and_balance(
    session_factory: async_sessionmaker, tenant: TenantFixture, material_id: str
) -> tuple[Decimal, Decimal]:
    async with UnitOfWork(session_factory, tenant_id=tenant.id) as uow:
        row = (
            await uow.session.execute(
                text(
                    "SELECT COALESCE((SELECT SUM(quantity) FROM stock_movements "
                    "WHERE material_id = :id), 0) AS ledger, quantity_on_hand AS balance "
                    "FROM materials WHERE id = :id"
                ),
                {"id": material_id},
            )
        ).one()
    return row.ledger, row.balance


async def _create_material(
    client: httpx.AsyncClient, headers: dict, name: str = "Seda marfim"
) -> str:
    response = await client.post(
        f"{_API}/materials",
        headers=headers,
        json={"name": name, "sku": f"SED-{name[:4]}", "unit": "m", "minimum_quantity": "2"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def test_ledger_sum_always_equals_the_cached_balance(
    session_factory: async_sessionmaker, tenant_a: TenantFixture, owner: UserFixture
) -> None:
    await _add_membership(session_factory, tenant=tenant_a, user=owner, role="owner")
    await _add_subscription(session_factory, tenant=tenant_a, active=True)
    provider = _StubIdentityProvider()
    headers = _headers(provider, owner, tenant_a)
    app = _build_app(session_factory, provider)

    async with _client(app) as client:
        material_id = await _create_material(client, headers)
        for body in (
            {"movement_type": "entrada", "quantity": "10", "unit_cost": "4.50"},
            {"movement_type": "perda", "quantity": "3"},
            {"movement_type": "ajuste", "quantity": "-2", "note": "contagem"},
            {"movement_type": "entrada", "quantity": "5", "unit_cost": "5.10"},
        ):
            response = await client.post(
                f"{_API}/materials/{material_id}/movements", headers=headers, json=body
            )
            assert response.status_code == 201, response.text

    ledger, balance = await _ledger_sum_and_balance(session_factory, tenant_a, material_id)
    assert ledger == Decimal("10")  # 10 - 3 - 2 + 5
    assert balance == ledger


async def test_three_quantities_are_returned_and_available_is_on_hand_minus_reserved(
    session_factory: async_sessionmaker, tenant_a: TenantFixture, owner: UserFixture
) -> None:
    await _add_membership(session_factory, tenant=tenant_a, user=owner, role="owner")
    await _add_subscription(session_factory, tenant=tenant_a, active=True)
    provider = _StubIdentityProvider()
    headers = _headers(provider, owner, tenant_a)
    app = _build_app(session_factory, provider)

    async with _client(app) as client:
        material_id = await _create_material(client, headers)
        await client.post(
            f"{_API}/materials/{material_id}/movements",
            headers=headers,
            json={"movement_type": "entrada", "quantity": "8", "unit_cost": "2"},
        )
        body = (await client.get(f"{_API}/materials/{material_id}", headers=headers)).json()

    assert Decimal(body["quantity_on_hand"]) == Decimal("8")
    assert Decimal(body["reserved_quantity"]) == Decimal("0")  # no quotes exist yet (M5)
    assert Decimal(body["available_quantity"]) == Decimal("8")
    assert body["is_low_stock"] is False  # 8 is above the minimum of 2


async def test_a_withdrawal_larger_than_the_balance_is_refused(
    session_factory: async_sessionmaker, tenant_a: TenantFixture, owner: UserFixture
) -> None:
    await _add_membership(session_factory, tenant=tenant_a, user=owner, role="owner")
    await _add_subscription(session_factory, tenant=tenant_a, active=True)
    provider = _StubIdentityProvider()
    headers = _headers(provider, owner, tenant_a)
    app = _build_app(session_factory, provider)

    async with _client(app) as client:
        material_id = await _create_material(client, headers)
        await client.post(
            f"{_API}/materials/{material_id}/movements",
            headers=headers,
            json={"movement_type": "entrada", "quantity": "2"},
        )
        response = await client.post(
            f"{_API}/materials/{material_id}/movements",
            headers=headers,
            json={"movement_type": "perda", "quantity": "5"},
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "insufficient_stock"
    ledger, balance = await _ledger_sum_and_balance(session_factory, tenant_a, material_id)
    assert ledger == balance == Decimal("2")  # the refused movement left no trace


async def test_a_new_unit_cost_on_entry_records_the_change_and_updates_the_material(
    session_factory: async_sessionmaker, tenant_a: TenantFixture, owner: UserFixture
) -> None:
    await _add_membership(session_factory, tenant=tenant_a, user=owner, role="owner")
    await _add_subscription(session_factory, tenant=tenant_a, active=True)
    provider = _StubIdentityProvider()
    headers = _headers(provider, owner, tenant_a)
    app = _build_app(session_factory, provider)

    async with _client(app) as client:
        material_id = await _create_material(client, headers)
        await client.post(
            f"{_API}/materials/{material_id}/movements",
            headers=headers,
            json={"movement_type": "entrada", "quantity": "3", "unit_cost": "4.00"},
        )
        await client.post(
            f"{_API}/materials/{material_id}/movements",
            headers=headers,
            json={"movement_type": "entrada", "quantity": "3", "unit_cost": "6.00"},
        )
        material = (await client.get(f"{_API}/materials/{material_id}", headers=headers)).json()

    assert Decimal(material["current_unit_cost"]) == Decimal("6.00")
    async with UnitOfWork(session_factory, tenant_id=tenant_a.id) as uow:
        changes = (
            await uow.session.execute(
                text(
                    "SELECT cost_from, cost_to FROM material_cost_history WHERE material_id = :id "
                    "ORDER BY created_at"
                ),
                {"id": material_id},
            )
        ).all()
    assert [(c.cost_from, c.cost_to) for c in changes] == [
        (Decimal("0.00"), Decimal("4.00")),
        (Decimal("4.00"), Decimal("6.00")),
    ]


async def test_a_role_without_stock_write_cannot_post_a_movement(
    session_factory: async_sessionmaker, tenant_a: TenantFixture, owner: UserFixture
) -> None:
    """front_desk has no stock:write (baseline seed), so the ledger stays closed to it."""
    await _add_membership(session_factory, tenant=tenant_a, user=owner, role="owner")
    await _add_subscription(session_factory, tenant=tenant_a, active=True)
    provider = _StubIdentityProvider()
    owner_headers = _headers(provider, owner, tenant_a)
    app = _build_app(session_factory, provider)

    async with _client(app) as client:
        material_id = await _create_material(client, owner_headers)

        front_desk = await _create_user(session_factory)
        await _add_membership(session_factory, tenant=tenant_a, user=front_desk, role="front_desk")
        response = await client.post(
            f"{_API}/materials/{material_id}/movements",
            headers=_headers(provider, front_desk, tenant_a),
            json={"movement_type": "entrada", "quantity": "1"},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


async def test_the_balance_cannot_be_patched_directly(
    session_factory: async_sessionmaker, tenant_a: TenantFixture, owner: UserFixture
) -> None:
    await _add_membership(session_factory, tenant=tenant_a, user=owner, role="owner")
    await _add_subscription(session_factory, tenant=tenant_a, active=True)
    provider = _StubIdentityProvider()
    headers = _headers(provider, owner, tenant_a)
    app = _build_app(session_factory, provider)

    async with _client(app) as client:
        material_id = await _create_material(client, headers)
        response = await client.patch(
            f"{_API}/materials/{material_id}",
            headers=headers,
            json={"name": "Seda marfim nova", "quantity_on_hand": "999"},
        )

    # `MaterialPatch` has no balance field, so the extra key is ignored by the schema and the
    # balance cannot move through PATCH. The name change still applies.
    assert response.status_code == 200
    assert Decimal(response.json()["quantity_on_hand"]) == Decimal("0")


async def test_a_deleted_material_leaves_the_list_but_keeps_its_ledger(
    session_factory: async_sessionmaker, tenant_a: TenantFixture, owner: UserFixture
) -> None:
    await _add_membership(session_factory, tenant=tenant_a, user=owner, role="owner")
    await _add_subscription(session_factory, tenant=tenant_a, active=True)
    provider = _StubIdentityProvider()
    headers = _headers(provider, owner, tenant_a)
    app = _build_app(session_factory, provider)

    async with _client(app) as client:
        material_id = await _create_material(client, headers, name="Renda antiga")
        await client.post(
            f"{_API}/materials/{material_id}/movements",
            headers=headers,
            json={"movement_type": "entrada", "quantity": "1"},
        )
        deleted = await client.delete(f"{_API}/materials/{material_id}", headers=headers)
        listed = (await client.get(f"{_API}/materials", headers=headers)).json()

    assert deleted.status_code == 204
    assert all(item["id"] != material_id for item in listed)
    ledger, _ = await _ledger_sum_and_balance(session_factory, tenant_a, material_id)
    assert ledger == Decimal("1")  # history kept
