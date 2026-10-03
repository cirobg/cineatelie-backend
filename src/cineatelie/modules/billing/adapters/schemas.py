"""Wire schemas for `modules/billing` (ADR-004: Pydantic stays in `adapters/`).

Money is serialised as a decimal string (`"1234.50"`), never a float, so the frontend formats it
with `Intl.NumberFormat` and no rounding happens on the way through.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel


class SubscriptionOut(BaseModel):
    plan_code: str
    plan_label: str
    billing_period: str
    status: str
    starts_on: date
    ends_on: date
    grace_days: int
    trial_ends_on: date | None
    price_amount: Decimal | None  # the snapshot taken at purchase (BR-SUB-01)
    currency_code: str
    price_locked: bool
    is_within_window: bool


class FeatureOut(BaseModel):
    key: str
    label: str
    enabled: bool


class QuotaOut(BaseModel):
    key: str
    label: str
    unit: str
    limit: int | None  # null = unlimited
    used: int | None  # null until measured (M6 for attachments)


class MyPlanPlanOut(BaseModel):
    code: str
    label: str
    billing_period: str
    status: str
    price_amount: Decimal | None
    currency_code: str
    acquired_on: date  # starts_on of the current subscription row
    renews_on: date  # ends_on, inclusive
    days_remaining: int  # negative once the window has passed
    is_trial: bool
    trial_ends_on: date | None
    is_within_window: bool


class MyPlanOut(BaseModel):
    plan: MyPlanPlanOut | None  # null when the tenant has never had a subscription row
    included: list[FeatureOut]  # enabled features only, for the "Incluído" block
    quotas: list[QuotaOut]
    upgrade_url: str | None  # shown only when BILLING_UPGRADE_URL is set (OI-15)


class PlanOut(BaseModel):
    code: str
    label: str
    description: str | None
    price_amount: Decimal
    currency_code: str
    billing_period: str
    trial_days: int


class InvoiceOut(BaseModel):
    id: uuid.UUID
    issued_on: date
    due_on: date | None
    amount: Decimal
    currency_code: str
    status: str
    paid_at: datetime | None
    hosted_url: str | None
