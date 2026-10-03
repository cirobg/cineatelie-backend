"""Wire schemas for `modules/catalog` (ADR-004: Pydantic stays in `adapters/`).

Quantities and money are `Decimal` and serialise as decimal strings, never floats (ADR-008).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


class MaterialIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    sku: str | None = Field(default=None, max_length=40)
    unit: str = Field(default="m", max_length=10)
    minimum_quantity: Decimal = Field(default=Decimal("0"), ge=0)
    storage_location: str | None = Field(default=None, max_length=60)


class MaterialPatch(BaseModel):
    """Catalogue fields only. Cost and quantity are not patchable (BR-STK-06, BR-STK-07)."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    sku: str | None = Field(default=None, max_length=40)
    unit: str | None = Field(default=None, max_length=10)
    minimum_quantity: Decimal | None = Field(default=None, ge=0)
    storage_location: str | None = Field(default=None, max_length=60)


class MaterialOut(BaseModel):
    id: uuid.UUID
    name: str
    sku: str | None
    unit: str
    quantity_on_hand: Decimal  # the ledger's sum, kept by trigger
    reserved_quantity: Decimal  # held by live quotes (M5)
    available_quantity: Decimal  # on hand minus reserved (BR-STK-04)
    minimum_quantity: Decimal
    is_low_stock: bool
    current_unit_cost: Decimal
    storage_location: str | None


class MovementIn(BaseModel):
    movement_type: Literal["entrada", "ajuste", "perda"]
    quantity: Decimal
    unit_cost: Decimal | None = Field(default=None, ge=0)
    note: str | None = Field(default=None, max_length=300)


class MovementOut(BaseModel):
    id: uuid.UUID
    movement_type: str
    quantity: Decimal
    unit_cost: Decimal | None
    total_cost: Decimal
    occurred_at: datetime
    note: str | None


class CreatedOut(BaseModel):
    id: uuid.UUID
