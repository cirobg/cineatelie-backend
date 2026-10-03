"""Read side of `modules/billing` (backend spec §4.3). Every query runs inside a tenant-scoped
`UnitOfWork`, so `subscriptions`, `billing_invoices` and the override tables are filtered by RLS.
The `plans`, `features` and `quotas` catalogues are platform-wide reference data.

Entitlement is decided by `v_active_subscription` in the middleware (ADR-006). These queries
only *describe* the current window to the user, so a lapsed tenant can still see what to renew.

Quota usage is not measured yet: attachments arrive in M6, so `used` is `None` until then, and the
"Meu plano" screen shows the limit without a bar.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from cineatelie.platform.db.uow import UnitOfWork

# The most recent subscription row, whatever its status, so a lapsed tenant still sees its plan.
_CURRENT_SUBSCRIPTION = text(
    """
    SELECT s.plan_code, p.label_pt_br AS plan_label, p.billing_period, s.status,
           s.starts_on, s.ends_on, s.grace_days, s.trial_ends_on,
           s.price_amount, s.currency_code, s.price_locked,
           (s.ends_on - CURRENT_DATE) AS days_remaining,
           (CURRENT_DATE BETWEEN s.starts_on AND (s.ends_on + s.grace_days)) AS is_within_window
      FROM subscriptions s
      JOIN plans p ON p.code = s.plan_code
     WHERE s.tenant_id = :tenant_id
     ORDER BY s.ends_on DESC
     LIMIT 1
    """
)

# Effective feature state: a tenant override beats the plan, and a missing row means "off".
_EFFECTIVE_FEATURES = text(
    """
    SELECT f.key, f.label_pt_br,
           COALESCE(tfo.is_enabled, pf.is_enabled, false) AS enabled
      FROM features f
      LEFT JOIN plan_features pf
             ON pf.feature_key = f.key AND pf.plan_code = :plan_code
      LEFT JOIN tenant_feature_overrides tfo
             ON tfo.feature_key = f.key
            AND tfo.tenant_id = :tenant_id
            AND (tfo.expires_on IS NULL OR tfo.expires_on >= CURRENT_DATE)
     WHERE f.is_active
     ORDER BY f.key
    """
)

# Effective quota ceiling (schema: `quotas`): tenant override, then plan, then unlimited.
# NULL means unlimited, so an override row with a NULL limit lifts the cap for this tenant.
_EFFECTIVE_QUOTAS = text(
    """
    SELECT q.key, q.label_pt_br, q.unit,
           CASE WHEN tqo.tenant_id IS NOT NULL THEN tqo.limit_value
                ELSE pq.limit_value END AS limit_value
      FROM quotas q
      LEFT JOIN plan_quotas pq
             ON pq.quota_key = q.key AND pq.plan_code = :plan_code
      LEFT JOIN tenant_quota_overrides tqo
             ON tqo.quota_key = q.key
            AND tqo.tenant_id = :tenant_id
            AND (tqo.expires_on IS NULL OR tqo.expires_on >= CURRENT_DATE)
     ORDER BY q.key
    """
)

_PUBLIC_PLANS = text(
    """
    SELECT code, label_pt_br, description, price_amount, currency_code,
           billing_period, trial_days
      FROM plans
     WHERE is_public AND is_active
     ORDER BY sort_order, code
    """
)

_INVOICES = text(
    """
    SELECT id, issued_on, due_on, amount, currency_code, status, paid_at, hosted_url
      FROM billing_invoices
     ORDER BY issued_on DESC
     LIMIT 100
    """
)


@dataclass(frozen=True, slots=True)
class CurrentSubscription:
    plan_code: str
    plan_label: str
    billing_period: str
    status: str
    starts_on: date
    ends_on: date
    grace_days: int
    trial_ends_on: date | None
    price_amount: Decimal | None
    currency_code: str
    price_locked: bool
    days_remaining: (
        int  # computed by the database (CURRENT_DATE); negative once the window has passed
    )
    is_within_window: bool


@dataclass(frozen=True, slots=True)
class FeatureState:
    key: str
    label: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class QuotaState:
    key: str
    label: str
    unit: str
    limit: int | None  # None = unlimited
    used: int | None  # None until the quota is measured (M6 for attachments)


@dataclass(frozen=True, slots=True)
class PlanSummary:
    code: str
    label: str
    description: str | None
    price_amount: Decimal
    currency_code: str
    billing_period: str
    trial_days: int


@dataclass(frozen=True, slots=True)
class InvoiceSummary:
    id: uuid.UUID
    issued_on: date
    due_on: date | None
    amount: Decimal
    currency_code: str
    status: str
    paid_at: datetime | None
    hosted_url: str | None


async def get_current_subscription(
    session_factory: async_sessionmaker, *, tenant_id: uuid.UUID, user_id: uuid.UUID
) -> CurrentSubscription | None:
    async with UnitOfWork(session_factory, tenant_id=tenant_id, user_id=user_id) as uow:
        row = (
            await uow.session.execute(_CURRENT_SUBSCRIPTION, {"tenant_id": str(tenant_id)})
        ).one_or_none()
    if row is None:
        return None
    return CurrentSubscription(
        plan_code=row.plan_code,
        plan_label=row.plan_label,
        billing_period=row.billing_period,
        status=row.status,
        starts_on=row.starts_on,
        ends_on=row.ends_on,
        grace_days=row.grace_days,
        trial_ends_on=row.trial_ends_on,
        price_amount=row.price_amount,
        currency_code=row.currency_code,
        price_locked=row.price_locked,
        days_remaining=row.days_remaining,
        is_within_window=row.is_within_window,
    )


async def get_features(
    session_factory: async_sessionmaker,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    plan_code: str,
) -> list[FeatureState]:
    async with UnitOfWork(session_factory, tenant_id=tenant_id, user_id=user_id) as uow:
        rows = (
            await uow.session.execute(
                _EFFECTIVE_FEATURES, {"plan_code": plan_code, "tenant_id": str(tenant_id)}
            )
        ).all()
    return [FeatureState(key=r.key, label=r.label_pt_br, enabled=r.enabled) for r in rows]


async def get_quotas(
    session_factory: async_sessionmaker,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    plan_code: str,
) -> list[QuotaState]:
    async with UnitOfWork(session_factory, tenant_id=tenant_id, user_id=user_id) as uow:
        rows = (
            await uow.session.execute(
                _EFFECTIVE_QUOTAS, {"plan_code": plan_code, "tenant_id": str(tenant_id)}
            )
        ).all()
    return [
        QuotaState(key=r.key, label=r.label_pt_br, unit=r.unit, limit=r.limit_value, used=None)
        for r in rows
    ]


async def list_public_plans(
    session_factory: async_sessionmaker, *, tenant_id: uuid.UUID, user_id: uuid.UUID
) -> list[PlanSummary]:
    async with UnitOfWork(session_factory, tenant_id=tenant_id, user_id=user_id) as uow:
        rows = (await uow.session.execute(_PUBLIC_PLANS)).all()
    return [
        PlanSummary(
            code=r.code,
            label=r.label_pt_br,
            description=r.description,
            price_amount=r.price_amount,
            currency_code=r.currency_code,
            billing_period=r.billing_period,
            trial_days=r.trial_days,
        )
        for r in rows
    ]


async def list_invoices(
    session_factory: async_sessionmaker, *, tenant_id: uuid.UUID, user_id: uuid.UUID
) -> list[InvoiceSummary]:
    async with UnitOfWork(session_factory, tenant_id=tenant_id, user_id=user_id) as uow:
        rows = (await uow.session.execute(_INVOICES)).all()
    return [
        InvoiceSummary(
            id=r.id,
            issued_on=r.issued_on,
            due_on=r.due_on,
            amount=r.amount,
            currency_code=r.currency_code,
            status=r.status,
            paid_at=r.paid_at,
            hosted_url=r.hosted_url,
        )
        for r in rows
    ]
