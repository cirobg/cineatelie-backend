"""The catalogue module's routes (backend spec §4.5). Registered under `API_V1_PREFIX` in `main.py`.

Stock changes go through `StockService` only (BR-STK-07). These routes never write `stock_movements`
themselves. `GET /materials/low-stock` is declared before `/materials/{material_id}` so the path
segment "low-stock" is not read as an id.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import async_sessionmaker

from cineatelie.modules.catalog.adapters.schemas import (
    CreatedOut,
    MaterialIn,
    MaterialOut,
    MaterialPatch,
    MovementIn,
    MovementOut,
)
from cineatelie.modules.catalog.application import materials as material_queries
from cineatelie.modules.catalog.application.stock import StockService
from cineatelie.platform.middleware.permissions import require_permission

router = APIRouter(prefix="/materials", tags=["catalog"])


def _get_session_factory(request: Request) -> async_sessionmaker:
    return request.app.state.session_factory


@router.get(
    "",
    response_model=list[MaterialOut],
    dependencies=[Depends(require_permission("stock:read"))],
)
async def list_materials(
    request: Request,
    q: str | None = None,
    session_factory: async_sessionmaker = Depends(_get_session_factory),
) -> list[MaterialOut]:
    rows = await material_queries.list_materials(
        session_factory, tenant_id=request.state.tenant_id, user_id=request.state.user_id, q=q
    )
    return [MaterialOut(**asdict(r)) for r in rows]


@router.get(
    "/low-stock",
    response_model=list[MaterialOut],
    dependencies=[Depends(require_permission("stock:read"))],
)
async def low_stock(
    request: Request, session_factory: async_sessionmaker = Depends(_get_session_factory)
) -> list[MaterialOut]:
    rows = await material_queries.low_stock_materials(
        session_factory, tenant_id=request.state.tenant_id, user_id=request.state.user_id
    )
    return [MaterialOut(**asdict(r)) for r in rows]


@router.post(
    "",
    response_model=CreatedOut,
    status_code=201,
    dependencies=[Depends(require_permission("stock:write"))],
)
async def create_material(
    body: MaterialIn,
    request: Request,
    session_factory: async_sessionmaker = Depends(_get_session_factory),
) -> CreatedOut:
    new_id = await material_queries.create_material(
        session_factory,
        tenant_id=request.state.tenant_id,
        user_id=request.state.user_id,
        name=body.name,
        sku=body.sku,
        unit=body.unit,
        minimum_quantity=body.minimum_quantity,
        storage_location=body.storage_location,
    )
    return CreatedOut(id=new_id)


@router.get(
    "/{material_id}",
    response_model=MaterialOut,
    dependencies=[Depends(require_permission("stock:read"))],
)
async def get_material(
    material_id: uuid.UUID,
    request: Request,
    session_factory: async_sessionmaker = Depends(_get_session_factory),
) -> MaterialOut:
    row = await material_queries.get_material(
        session_factory,
        tenant_id=request.state.tenant_id,
        user_id=request.state.user_id,
        material_id=material_id,
    )
    return MaterialOut(**asdict(row))


@router.patch(
    "/{material_id}",
    response_model=MaterialOut,
    dependencies=[Depends(require_permission("stock:write"))],
)
async def patch_material(
    material_id: uuid.UUID,
    body: MaterialPatch,
    request: Request,
    session_factory: async_sessionmaker = Depends(_get_session_factory),
) -> MaterialOut:
    changes = body.model_dump(exclude_unset=True)
    await material_queries.update_material(
        session_factory,
        tenant_id=request.state.tenant_id,
        user_id=request.state.user_id,
        material_id=material_id,
        changes=changes,
    )
    row = await material_queries.get_material(
        session_factory,
        tenant_id=request.state.tenant_id,
        user_id=request.state.user_id,
        material_id=material_id,
    )
    return MaterialOut(**asdict(row))


@router.delete(
    "/{material_id}",
    status_code=204,
    dependencies=[Depends(require_permission("stock:write"))],
)
async def delete_material(
    material_id: uuid.UUID,
    request: Request,
    session_factory: async_sessionmaker = Depends(_get_session_factory),
) -> Response:
    await material_queries.delete_material(
        session_factory,
        tenant_id=request.state.tenant_id,
        user_id=request.state.user_id,
        material_id=material_id,
    )
    return Response(status_code=204)


@router.post(
    "/{material_id}/movements",
    response_model=MovementOut,
    status_code=201,
    dependencies=[Depends(require_permission("stock:write"))],
)
async def post_movement(
    material_id: uuid.UUID,
    body: MovementIn,
    request: Request,
    session_factory: async_sessionmaker = Depends(_get_session_factory),
) -> MovementOut:
    row = await StockService(session_factory).record(
        tenant_id=request.state.tenant_id,
        user_id=request.state.user_id,
        material_id=material_id,
        movement_type=body.movement_type,
        quantity=body.quantity,
        unit_cost=body.unit_cost,
        note=body.note,
    )
    return MovementOut(**asdict(row))


@router.get(
    "/{material_id}/movements",
    response_model=list[MovementOut],
    dependencies=[Depends(require_permission("stock:read"))],
)
async def list_movements(
    material_id: uuid.UUID,
    request: Request,
    session_factory: async_sessionmaker = Depends(_get_session_factory),
) -> list[MovementOut]:
    rows = await material_queries.list_movements(
        session_factory,
        tenant_id=request.state.tenant_id,
        user_id=request.state.user_id,
        material_id=material_id,
    )
    return [MovementOut(**asdict(r)) for r in rows]
