"""The billing module's routes (backend spec §4.3). Registered under `API_V1_PREFIX` in `main.py`.

Access follows the spec table: `/billing/subscription` and `/billing/invoices` need
`billing:manage`.
`/billing/my-plan` and `/billing/plans` need only membership, since every role must be able to see
why a limit applies (frontend spec §5.11). The entitlement and closure middlewares exempt every
`/billing/*` route, so a lapsed or closed workspace can still reach them to renew.
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import async_sessionmaker

from cineatelie.core.config import Settings, get_settings
from cineatelie.modules.billing.adapters.schemas import (
    FeatureOut,
    InvoiceOut,
    MyPlanOut,
    MyPlanPlanOut,
    PlanOut,
    QuotaOut,
    SubscriptionOut,
)
from cineatelie.modules.billing.application import queries
from cineatelie.platform.middleware.permissions import require_permission

router = APIRouter(prefix="/billing", tags=["billing"])


def _get_session_factory(request: Request) -> async_sessionmaker:
    return request.app.state.session_factory


@router.get(
    "/subscription",
    response_model=SubscriptionOut | None,
    dependencies=[Depends(require_permission("billing:manage"))],
)
async def get_subscription(
    request: Request, session_factory: async_sessionmaker = Depends(_get_session_factory)
) -> SubscriptionOut | None:
    current = await queries.get_current_subscription(
        session_factory, tenant_id=request.state.tenant_id, user_id=request.state.user_id
    )
    if current is None:
        return None
    return SubscriptionOut(
        **asdict(current)
    )  # days_remaining is ignored: /my-plan owns the countdown


@router.get("/my-plan", response_model=MyPlanOut)
async def get_my_plan(
    request: Request,
    session_factory: async_sessionmaker = Depends(_get_session_factory),
    settings: Settings = Depends(get_settings),
) -> MyPlanOut:
    tenant_id, user_id = request.state.tenant_id, request.state.user_id
    upgrade_url = settings.billing_upgrade_url or None
    current = await queries.get_current_subscription(
        session_factory, tenant_id=tenant_id, user_id=user_id
    )
    if current is None:
        return MyPlanOut(plan=None, included=[], quotas=[], upgrade_url=upgrade_url)

    features = await queries.get_features(
        session_factory, tenant_id=tenant_id, user_id=user_id, plan_code=current.plan_code
    )
    quotas = await queries.get_quotas(
        session_factory, tenant_id=tenant_id, user_id=user_id, plan_code=current.plan_code
    )

    return MyPlanOut(
        plan=MyPlanPlanOut(
            code=current.plan_code,
            label=current.plan_label,
            billing_period=current.billing_period,
            status=current.status,
            price_amount=current.price_amount,
            currency_code=current.currency_code,
            acquired_on=current.starts_on,
            renews_on=current.ends_on,
            days_remaining=current.days_remaining,
            is_trial=current.status == "trialing",
            trial_ends_on=current.trial_ends_on,
            is_within_window=current.is_within_window,
        ),
        included=[FeatureOut(**asdict(f)) for f in features if f.enabled],
        quotas=[QuotaOut(**asdict(q)) for q in quotas],
        upgrade_url=upgrade_url,
    )


@router.get("/plans", response_model=list[PlanOut])
async def list_plans(
    request: Request, session_factory: async_sessionmaker = Depends(_get_session_factory)
) -> list[PlanOut]:
    plans = await queries.list_public_plans(
        session_factory, tenant_id=request.state.tenant_id, user_id=request.state.user_id
    )
    return [PlanOut(**asdict(p)) for p in plans]


@router.get(
    "/invoices",
    response_model=list[InvoiceOut],
    dependencies=[Depends(require_permission("billing:manage"))],
)
async def list_invoices(
    request: Request, session_factory: async_sessionmaker = Depends(_get_session_factory)
) -> list[InvoiceOut]:
    invoices = await queries.list_invoices(
        session_factory, tenant_id=request.state.tenant_id, user_id=request.state.user_id
    )
    return [InvoiceOut(**asdict(i)) for i in invoices]
