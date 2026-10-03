"""`StockService` (backend spec BR-STK-06, BR-STK-07): the only code that writes `stock_movements`.

Every balance change is a ledger row. The trigger `stock_movements_apply` maintains
`materials.quantity_on_hand`, so the cached balance always equals the sum of the ledger
(milestone M3, "Done when"). The material row is locked with `FOR UPDATE` first, so two
concurrent withdrawals cannot both pass the non-negative check.

Movements a user can make by hand: `entrada` (stock in), `ajuste` (signed correction), and `perda`
(loss, stored as a negative quantity). Consumption and reservations come with quotes (M5).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cineatelie.core.errors import AppError
from cineatelie.platform.db.uow import UnitOfWork

ManualMovementType = Literal["entrada", "ajuste", "perda"]

_LOCK_MATERIAL = text(
    "SELECT id, quantity_on_hand, current_unit_cost FROM materials "
    "WHERE id = :material_id AND deleted_at IS NULL FOR UPDATE"
)
_INSERT_MOVEMENT = text(
    """
    INSERT INTO stock_movements
        (tenant_id, material_id, movement_type, quantity, unit_cost, reference_type,
         note, created_by)
    VALUES
        (:tenant_id, :material_id, :movement_type, :quantity, :unit_cost, 'manual',
         :note, :created_by)
    RETURNING id, movement_type, quantity, unit_cost, total_cost, occurred_at, note
    """
)
_RECORD_COST_CHANGE = text(
    """
    INSERT INTO material_cost_history
        (tenant_id, material_id, cost_from, cost_to, source, created_by)
    VALUES (:tenant_id, :material_id, :cost_from, :cost_to, 'stock_entry', :created_by)
    """
)
_SET_UNIT_COST = text(
    "UPDATE materials SET current_unit_cost = :cost, updated_at = now() WHERE id = :material_id"
)


@dataclass(frozen=True, slots=True)
class MovementRow:
    id: uuid.UUID
    movement_type: str
    quantity: Decimal
    unit_cost: Decimal | None
    total_cost: Decimal
    occurred_at: datetime
    note: str | None


def _signed_quantity(movement_type: ManualMovementType, quantity: Decimal) -> Decimal:
    """Enforces the sign rules per type, so a caller cannot record a withdrawal as an entry."""
    if movement_type == "entrada":
        if quantity <= 0:
            raise AppError(
                "validation_error", "A entrada precisa ter quantidade positiva.", status_code=422
            )
        return quantity
    if movement_type == "perda":
        if quantity <= 0:
            raise AppError(
                "validation_error",
                "Informe a quantidade perdida, em valor positivo.",
                status_code=422,
            )
        return -quantity
    if quantity == 0:
        raise AppError(
            "validation_error",
            "O ajuste precisa ter quantidade diferente de zero.",
            status_code=422,
        )
    return quantity


async def post_movement(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    material_id: uuid.UUID,
    movement_type: ManualMovementType,
    quantity: Decimal,
    unit_cost: Decimal | None,
    note: str | None,
) -> MovementRow:
    """Records one movement in the caller's transaction. Routes use `StockService.record`."""
    signed = _signed_quantity(movement_type, quantity)

    locked = (
        await session.execute(_LOCK_MATERIAL, {"material_id": str(material_id)})
    ).one_or_none()
    if locked is None:
        raise AppError("material_not_found", "Material não encontrado.", status_code=404)

    on_hand: Decimal = locked.quantity_on_hand
    if on_hand + signed < 0:
        raise AppError(
            "insufficient_stock",
            "Quantidade insuficiente em estoque para esta saída.",
            status_code=409,
            details=[{"available_quantity": str(on_hand)}],
        )

    # BR-STK-06: an entry at a different unit cost records the change and updates the material.
    if (
        movement_type == "entrada"
        and unit_cost is not None
        and unit_cost != locked.current_unit_cost
    ):
        await session.execute(
            _RECORD_COST_CHANGE,
            {
                "tenant_id": str(tenant_id),
                "material_id": str(material_id),
                "cost_from": locked.current_unit_cost,
                "cost_to": unit_cost,
                "created_by": str(user_id),
            },
        )
        await session.execute(_SET_UNIT_COST, {"cost": unit_cost, "material_id": str(material_id)})

    row = (
        await session.execute(
            _INSERT_MOVEMENT,
            {
                "tenant_id": str(tenant_id),
                "material_id": str(material_id),
                "movement_type": movement_type,
                "quantity": signed,
                "unit_cost": unit_cost,
                "note": note,
                "created_by": str(user_id),
            },
        )
    ).one()
    return MovementRow(
        id=row.id,
        movement_type=row.movement_type,
        quantity=row.quantity,
        unit_cost=row.unit_cost,
        total_cost=row.total_cost,
        occurred_at=row.occurred_at,
        note=row.note,
    )


class StockService:
    """Entry point for every stock change. Each call is one transaction, with the tenant bound."""

    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._session_factory = session_factory

    async def record(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        material_id: uuid.UUID,
        movement_type: ManualMovementType,
        quantity: Decimal,
        unit_cost: Decimal | None = None,
        note: str | None = None,
    ) -> MovementRow:
        async with UnitOfWork(self._session_factory, tenant_id=tenant_id, user_id=user_id) as uow:
            return await post_movement(
                uow.session,
                tenant_id=tenant_id,
                user_id=user_id,
                material_id=material_id,
                movement_type=movement_type,
                quantity=quantity,
                unit_cost=unit_cost,
                note=note,
            )
