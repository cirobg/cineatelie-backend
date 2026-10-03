"""Read and catalogue-write side of materials ("Estoque e Insumos"). Balance changes are NOT here:
they go through `StockService` (BR-STK-07). A material is created at zero stock, and its first
quantity arrives as an `entrada`, so the ledger is the only way a balance can change.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from cineatelie.core.errors import AppError
from cineatelie.platform.db.uow import UnitOfWork

_AVAILABILITY_COLUMNS = (
    "material_id, name, sku, unit, quantity_on_hand, reserved_quantity, "
    "available_quantity, minimum_quantity, is_low_stock, current_unit_cost, storage_location"
)


@dataclass(frozen=True, slots=True)
class MaterialRow:
    id: uuid.UUID
    name: str
    sku: str | None
    unit: str
    quantity_on_hand: Decimal
    reserved_quantity: Decimal
    available_quantity: Decimal
    minimum_quantity: Decimal
    is_low_stock: bool
    current_unit_cost: Decimal
    storage_location: str | None


def _to_row(r) -> MaterialRow:  # type: ignore[no-untyped-def]
    return MaterialRow(
        id=r.material_id,
        name=r.name,
        sku=r.sku,
        unit=r.unit,
        quantity_on_hand=r.quantity_on_hand,
        reserved_quantity=r.reserved_quantity,
        available_quantity=r.available_quantity,
        minimum_quantity=r.minimum_quantity,
        is_low_stock=r.is_low_stock,
        current_unit_cost=r.current_unit_cost,
        storage_location=r.storage_location,
    )


async def list_materials(
    session_factory: async_sessionmaker, *, tenant_id: uuid.UUID, user_id: uuid.UUID, q: str | None
) -> list[MaterialRow]:
    """Active materials with their three quantities, optionally filtered by name or SKU."""
    pattern = f"%{q.strip()}%" if q and q.strip() else None
    async with UnitOfWork(session_factory, tenant_id=tenant_id, user_id=user_id) as uow:
        rows = (
            await uow.session.execute(
                text(
                    f"SELECT {_AVAILABILITY_COLUMNS} FROM v_material_availability "
                    "WHERE (CAST(:pattern AS text) IS NULL OR name ILIKE CAST(:pattern AS text) "
                    "OR sku ILIKE CAST(:pattern AS text)) ORDER BY name"
                ),
                {"pattern": pattern},
            )
        ).all()
    return [_to_row(r) for r in rows]


async def get_material(
    session_factory: async_sessionmaker,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    material_id: uuid.UUID,
) -> MaterialRow:
    async with UnitOfWork(session_factory, tenant_id=tenant_id, user_id=user_id) as uow:
        row = (
            await uow.session.execute(
                text(
                    f"SELECT {_AVAILABILITY_COLUMNS} FROM v_material_availability "
                    "WHERE material_id = :id"
                ),
                {"id": str(material_id)},
            )
        ).one_or_none()
    if row is None:
        raise AppError("material_not_found", "Material não encontrado.", status_code=404)
    return _to_row(row)


async def low_stock_materials(
    session_factory: async_sessionmaker, *, tenant_id: uuid.UUID, user_id: uuid.UUID
) -> list[MaterialRow]:
    """Materials whose available quantity is at or below the minimum (dashboard alert)."""
    async with UnitOfWork(session_factory, tenant_id=tenant_id, user_id=user_id) as uow:
        rows = (
            await uow.session.execute(
                text(
                    f"SELECT {_AVAILABILITY_COLUMNS} FROM v_material_availability "
                    "WHERE is_low_stock ORDER BY name"
                )
            )
        ).all()
    return [_to_row(r) for r in rows]


async def create_material(
    session_factory: async_sessionmaker,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    name: str,
    sku: str | None,
    unit: str,
    minimum_quantity: Decimal,
    storage_location: str | None,
) -> uuid.UUID:
    new_id = uuid.uuid4()
    async with UnitOfWork(session_factory, tenant_id=tenant_id, user_id=user_id) as uow:
        await uow.session.execute(
            text(
                "INSERT INTO materials (id, tenant_id, name, sku, unit, minimum_quantity, "
                "storage_location) VALUES (:id, :tenant_id, :name, :sku, :unit, "
                ":minimum_quantity, :storage_location)"
            ),
            {
                "id": str(new_id),
                "tenant_id": str(tenant_id),
                "name": name,
                "sku": sku,
                "unit": unit,
                "minimum_quantity": minimum_quantity,
                "storage_location": storage_location,
            },
        )
    return new_id


_PATCHABLE = ("name", "sku", "unit", "minimum_quantity", "storage_location")


async def update_material(
    session_factory: async_sessionmaker,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    material_id: uuid.UUID,
    changes: dict[str, object],
) -> None:
    """Catalogue fields only. Cost and quantity are never patched here (BR-STK-06, BR-STK-07)."""
    unknown = set(changes) - set(_PATCHABLE)
    if unknown:
        raise AppError(
            "validation_error",
            "Campo não editável por aqui. Custo e quantidade mudam por movimentação.",
            status_code=422,
            details=[{"fields": sorted(unknown)}],
        )
    if not changes:
        return
    assignments = ", ".join(f"{field} = :{field}" for field in changes)
    async with UnitOfWork(session_factory, tenant_id=tenant_id, user_id=user_id) as uow:
        result = await uow.session.execute(
            text(
                f"UPDATE materials SET {assignments}, updated_at = now() "
                "WHERE id = :material_id AND deleted_at IS NULL"
            ),
            {**changes, "material_id": str(material_id)},
        )
        if result.rowcount == 0:
            raise AppError("material_not_found", "Material não encontrado.", status_code=404)


async def delete_material(
    session_factory: async_sessionmaker,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    material_id: uuid.UUID,
) -> None:
    """Soft delete. The ledger keeps its history: `stock_movements.material_id` is RESTRICT."""
    async with UnitOfWork(session_factory, tenant_id=tenant_id, user_id=user_id) as uow:
        result = await uow.session.execute(
            text("UPDATE materials SET deleted_at = now() WHERE id = :id AND deleted_at IS NULL"),
            {"id": str(material_id)},
        )
        if result.rowcount == 0:
            raise AppError("material_not_found", "Material não encontrado.", status_code=404)


@dataclass(frozen=True, slots=True)
class LedgerRow:
    id: uuid.UUID
    movement_type: str
    quantity: Decimal
    unit_cost: Decimal | None
    total_cost: Decimal
    occurred_at: object
    note: str | None


async def list_movements(
    session_factory: async_sessionmaker,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    material_id: uuid.UUID,
) -> list[LedgerRow]:
    async with UnitOfWork(session_factory, tenant_id=tenant_id, user_id=user_id) as uow:
        rows = (
            await uow.session.execute(
                text(
                    "SELECT id, movement_type, quantity, unit_cost, total_cost, occurred_at, note "
                    "FROM stock_movements WHERE material_id = :id "
                    "ORDER BY occurred_at DESC, id DESC LIMIT 200"
                ),
                {"id": str(material_id)},
            )
        ).all()
    return [
        LedgerRow(
            id=r.id,
            movement_type=r.movement_type,
            quantity=r.quantity,
            unit_cost=r.unit_cost,
            total_cost=r.total_cost,
            occurred_at=r.occurred_at,
            note=r.note,
        )
        for r in rows
    ]
